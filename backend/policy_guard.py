"""Deterministic authorization boundary between AI proposals and execution."""

from typing import Literal

from pydantic import BaseModel

from backend.ai_diagnosis import DEFAULT_ACTION, DiagnosisProposal


class GuardDecision(BaseModel):
    disposition: Literal["approved", "overridden", "blocked", "abstained"]
    final_action: Literal[
        "retry", "payment_link", "discount_nudge", "human_escalation", "no_action"
    ]
    reason: str
    ai_action: str


def authorize_proposal(
    proposal: DiagnosisProposal,
    *,
    customer_opted_out: bool,
    contact_count: int,
    max_contacts: int,
    trusted_root_cause: str | None = None,
) -> GuardDecision:
    ai_action = proposal.proposed_action
    if customer_opted_out:
        return GuardDecision(
            disposition="blocked", final_action="no_action", ai_action=ai_action,
            reason="Opt-out invariant blocks every recovery action.",
        )
    if "prompt_injection_pattern" in proposal.risk_flags:
        return GuardDecision(
            disposition="abstained", final_action="human_escalation", ai_action=ai_action,
            reason="Untrusted log contains instruction-like text; automation abstained.",
        )
    if proposal.confidence < .75:
        return GuardDecision(
            disposition="abstained", final_action="human_escalation", ai_action=ai_action,
            reason=f"Confidence {proposal.confidence:.2f} is below the 0.75 automation threshold.",
        )
    if trusted_root_cause and proposal.root_cause != trusted_root_cause:
        safe_action = DEFAULT_ACTION[trusted_root_cause]
        return GuardDecision(
            disposition="overridden", final_action=safe_action, ai_action=ai_action,
            reason=(f"AI diagnosis '{proposal.root_cause}' conflicts with trusted structured "
                    f"evidence '{trusted_root_cause}'; policy requires '{safe_action}'."),
        )
    if contact_count >= max_contacts and ai_action in {"payment_link", "discount_nudge"}:
        return GuardDecision(
            disposition="blocked", final_action="no_action", ai_action=ai_action,
            reason="Contact budget exhausted.",
        )
    permitted = DEFAULT_ACTION[proposal.root_cause]
    if ai_action != permitted:
        return GuardDecision(
            disposition="overridden", final_action=permitted, ai_action=ai_action,
            reason=f"Action '{ai_action}' is not permitted for {proposal.root_cause}; policy requires '{permitted}'.",
        )
    return GuardDecision(
        disposition="approved", final_action=ai_action, ai_action=ai_action,
        reason="Typed proposal passed confidence, compliance, risk, and action allow-list checks.",
    )
