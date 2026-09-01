"""Razorpay webhook verification, correlation and idempotent state updates."""

import hashlib
import hmac
import json
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import AuditLog, Transaction, WebhookEvent


SUPPORTED_EVENTS = {
    "payment_link.paid",
    "payment_link.partially_paid",
    "payment_link.expired",
    "payment_link.cancelled",
}


def valid_signature(raw_body: bytes, received: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return bool(received) and hmac.compare_digest(expected, received)


def _payment_link_entity(payload: dict) -> dict:
    return payload.get("payload", {}).get("payment_link", {}).get("entity", {}) or {}


def _payment_entity(payload: dict) -> dict:
    return payload.get("payload", {}).get("payment", {}).get("entity", {}) or {}


def process_verified_event(
    db: Session, event_id: str, raw_body: bytes, payload: dict
) -> dict:
    """Persist a verified event once, correlate it, then apply a monotonic update."""
    existing = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
    if existing:
        return {"accepted": True, "duplicate": True, "event_id": event_id}

    event_type = str(payload.get("event", ""))
    link = _payment_link_entity(payload)
    link_id = link.get("id")
    reference_id = link.get("reference_id") or (link.get("notes") or {}).get("txn_ref")

    txn = None
    if link_id:
        txn = db.query(Transaction).filter(Transaction.razorpay_payment_link_id == link_id).first()
    if txn is None and reference_id:
        txn = db.query(Transaction).filter(Transaction.id == reference_id).first()

    record = WebhookEvent(
        event_id=event_id,
        event_type=event_type or "unknown",
        payload_sha256=hashlib.sha256(raw_body).hexdigest(),
        signature_valid=True,
        payment_link_id=link_id,
        transaction_id=txn.id if txn else None,
        raw_payload=raw_body.decode("utf-8", errors="replace"),
    )
    db.add(record)

    if event_type not in SUPPORTED_EVENTS:
        record.outcome = "ignored_event_type"
    elif txn is None:
        record.outcome = "unmatched"
        record.error = "No transaction matched payment-link id or reference_id."
    elif event_type == "payment_link.paid":
        # Monotonic: a late expiry/cancel event can never undo verified payment.
        amount_paid = float(link.get("amount_paid") or link.get("amount") or 0) / 100
        payment = _payment_entity(payload)
        currency = link.get("currency", "INR")
        correlation_conflict = bool(reference_id and reference_id != txn.id)
        amount_mismatch = abs(amount_paid - float(txn.amount)) > 0.01
        if currency != "INR" or correlation_conflict or amount_mismatch:
            record.outcome = "quarantined_mismatch"
            record.error = (
                f"currency={currency}; reference={reference_id}; "
                f"expected_amount={txn.amount}; amount_paid={amount_paid}"
            )
        else:
            txn.status = "payment_verified"
            txn.verified_recovered_amount = round(amount_paid, 2)
            txn.razorpay_payment_id = payment.get("id")
            txn.next_action_at = None
            record.outcome = "payment_verified"
            db.add(AuditLog(
                transaction_id=txn.id,
                stage="PAYMENT_VERIFIED",
                root_cause=txn.root_cause,
                action_taken="webhook_confirmation",
                reasoning=f"Signed Razorpay event verified payment of INR {amount_paid:,.2f}.",
                reasoning_source="razorpay_webhook",
                outcome="payment_verified",
            ))
    elif event_type == "payment_link.partially_paid":
        # Partial money is visible in the event log but excluded from the
        # project's verified recovery KPI until the link is fully paid.
        if txn.status != "payment_verified":
            txn.status = "awaiting_payment"
        record.outcome = "partial_payment_waiting"
    elif event_type == "payment_link.expired":
        if txn.status != "payment_verified":
            txn.status = "expired"
            txn.next_action_at = None
        record.outcome = "expired"
    elif event_type == "payment_link.cancelled":
        if txn.status != "payment_verified":
            txn.status = "failed"
            txn.next_action_at = None
        record.outcome = "cancelled"

    record.processed_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        # Handles concurrent deliveries that both passed the initial lookup.
        db.rollback()
        return {"accepted": True, "duplicate": True, "event_id": event_id}
    return {
        "accepted": True,
        "duplicate": False,
        "event_id": event_id,
        "event_type": event_type,
        "outcome": record.outcome,
        "transaction_id": txn.id if txn else None,
    }


def parse_json(raw_body: bytes) -> dict:
    value = json.loads(raw_body)
    if not isinstance(value, dict):
        raise ValueError("Webhook payload must be a JSON object.")
    return value
