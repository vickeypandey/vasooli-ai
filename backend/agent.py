from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from backend.ai_diagnosis import propose_diagnosis
from backend.classifier import diagnose
from backend.config import settings
from backend.execution import create_payment_link, send_message
from backend.models import AuditLog, Transaction
from backend.policy_guard import authorize_proposal


def _in_quiet_hours(hour: int) -> bool:
    start, end = settings.QUIET_HOURS_START, settings.QUIET_HOURS_END
    if start > end:  # wraps past midnight, e.g. 22 -> 8
        return hour >= start or hour < end
    return start <= hour < end


def _local_time(now_utc: datetime) -> datetime:
    aware_utc = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc
    return aware_utc.astimezone(ZoneInfo(settings.RECOVERY_TIMEZONE))


def _next_contact_time(now_utc: datetime) -> datetime:
    """Return next permitted time as naive UTC for SQLite persistence."""
    local_now = _local_time(now_utc)
    if not _in_quiet_hours(local_now.hour):
        return now_utc.replace(tzinfo=None)
    candidate = local_now.replace(hour=settings.QUIET_HOURS_END, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def _log(db: Session, txn: Transaction, stage: str, root_cause, action, reasoning, source, outcome):
    entry = AuditLog(
        transaction_id=txn.id,
        stage=stage,
        root_cause=root_cause,
        action_taken=action,
        reasoning=reasoning,
        reasoning_source=source,
        outcome=outcome,
    )
    db.add(entry)


def _execution_action(guard_action: str, txn: Transaction) -> str:
    if guard_action == "retry":
        return "instant_retry" if txn.retry_count == 0 else "delayed_retry"
    if guard_action == "human_escalation":
        return "escalate_to_human"
    return guard_action


def process_transaction(db: Session, txn: Transaction) -> None:
    """Runs one transaction through the full bounded pipeline once."""

    now = datetime.utcnow()
    local_now = _local_time(now)
    if txn.next_action_at and txn.next_action_at > now:
        return

    _log(db, txn, "DETECTED", None, None, "At-risk transaction picked up by the agent.", "rule_based", None)

    if txn.customer_opted_out:
        txn.status = "stopped"
        _log(
            db, txn, "STOPPED", None, "none",
            "Customer is on the do-not-contact list. No retry, no message sent. "
            "Compliance rule takes precedence over recovery.",
            "rule_based", "stopped_compliance",
        )
        return

    diagnosis = diagnose(txn.failure_type, txn.error_code)
    txn.root_cause = diagnosis.root_cause
    txn.status = "diagnosed"
    _log(db, txn, "DIAGNOSED", diagnosis.root_cause, None, diagnosis.description, "rule_based", None)

    if diagnosis.root_cause != "unknown" and txn.contact_count >= settings.MAX_CONTACT_ATTEMPTS and not diagnosis.is_transient:
        txn.status = "lost"
        _log(
            db, txn, "STOPPED", diagnosis.root_cause, "none",
            f"Contact budget ({settings.MAX_CONTACT_ATTEMPTS}) exhausted with no response. "
            "Marking as lost rather than continuing to message the customer.",
            "rule_based", "lost_unresponsive",
        )
        return

    proposal, proposal_source, fallback_reason = propose_diagnosis(
        txn.failure_type, txn.error_code, txn.gateway_log
    )
    _log(
        db, txn, "AI_PROPOSED", proposal.root_cause, proposal.proposed_action,
        f"confidence={proposal.confidence:.2f}; evidence={proposal.evidence}; "
        f"risk_flags={proposal.risk_flags}; fallback={fallback_reason or 'none'}",
        proposal_source, "proposal_only_no_execution_authority",
    )
    guard = authorize_proposal(
        proposal,
        customer_opted_out=txn.customer_opted_out,
        contact_count=txn.contact_count,
        max_contacts=settings.MAX_CONTACT_ATTEMPTS,
        trusted_root_cause=diagnosis.root_cause,
    )
    _log(
        db, txn, f"POLICY_{guard.disposition.upper()}", diagnosis.root_cause,
        guard.final_action, guard.reason, "deterministic_policy_guard", guard.disposition,
    )
    if guard.final_action == "no_action":
        txn.status = "stopped"
        return
    action = _execution_action(guard.final_action, txn)

    contact_actions = {"payment_link", "discount_nudge"}
    if action in contact_actions and _in_quiet_hours(local_now.hour):
        txn.next_action_at = _next_contact_time(now)
        txn.status = "diagnosed"
        _log(
            db, txn, "DECIDED", diagnosis.root_cause, action,
            f"Chosen action '{action}' deferred — event occurred at hour "
            f"{local_now.hour}, inside quiet hours in {settings.RECOVERY_TIMEZONE} "
            f"({settings.QUIET_HOURS_START}:00-{settings.QUIET_HOURS_END}:00). "
            f"Customer will not be contacted before {txn.next_action_at.isoformat()}.",
            "rule_based", "deferred_quiet_hours",
        )
        return

    _log(db, txn, "DECIDED", diagnosis.root_cause, action, guard.reason,
         "deterministic_policy_guard", None)

    txn.last_action_at = now
    txn.next_action_at = None
    txn.status = "action_created"
    txn.action_idempotency_key = txn.action_idempotency_key or f"recovery:{txn.id}:1"
    _log(db, txn, "ACTION_CREATED", diagnosis.root_cause, action,
         f"Durable action created with idempotency key {txn.action_idempotency_key}.",
         "rule_based", "action_created")

    if action in ("instant_retry", "delayed_retry"):
        txn.retry_count += 1
        txn.status = "failed"
        _log(db, txn, "ACTED", diagnosis.root_cause, action,
             "Automatic retry is not connected to a charge executor; marked failed rather than simulating recovery.",
             "rule_based", "failed_unimplemented_executor")

    elif action in ("payment_link", "discount_nudge"):
        txn.contact_count += 1
        link, link_id, reference_id, is_real, link_error = create_payment_link(
            txn.amount, txn.customer_name, txn.id
        )
        txn.payment_link = link
        txn.razorpay_payment_link_id = link_id
        txn.payment_link_reference_id = reference_id
        channel = "sms" if txn.payment_method != "upi" else "whatsapp"
        message = send_message(channel, txn.customer_name, txn.amount, link)
        link_status = (
            "razorpay_test_mode" if is_real
            else f"MOCK (real link failed: {link_error})" if link_error and link_error != "no_razorpay_keys_configured"
            else "mock (no keys configured)"
        )
        _log(
            db, txn, "ACTED", diagnosis.root_cause, action,
            f"{message} | link_type={link_status}",
            "rule_based", "awaiting_verified_webhook" if link else "failed",
        )
        txn.status = "awaiting_payment" if link else "failed"

    elif action == "escalate_to_human":
        txn.status = "escalated"
        _log(db, txn, "ACTED", diagnosis.root_cause, action,
             "Issuer-declined transactions are not auto-retried or auto-messaged; "
             "handed to a human agent for manual follow-up.",
             "rule_based", "escalated")
        return


def run_agent_on_batch(db: Session, transactions: list[Transaction]) -> dict:
    """Runs every eligible (non-terminal) transaction through the pipeline once."""
    terminal = {
        "action_created", "awaiting_payment", "payment_verified", "expired",
        "failed", "lost", "stopped", "escalated",
    }
    processed = 0
    for txn in transactions:
        if txn.status in terminal:
            continue
        process_transaction(db, txn)
        processed += 1
    db.commit()
    return {"processed": processed}
