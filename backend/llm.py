"""
Generates the human-readable reasoning string attached to every audit-log
entry. If ANTHROPIC_API_KEY is configured, Claude writes a short, specific
explanation. If not, a deterministic rule-based template is used instead —
the pipeline is fully functional either way, only the prose quality differs.
"""

from backend.classifier import Diagnosis
from backend.config import settings

_client = None
if settings.llm_live:
    try:
        import anthropic

        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    except Exception:
        _client = None


def _fallback_reasoning(
    diagnosis: Diagnosis, action: str, amount: float, segment: str
) -> str:
    return (
        f"Diagnosed as '{diagnosis.root_cause}' ({diagnosis.description}) "
        f"Chose '{action}' because the failure is "
        f"{'transient, so an automatic retry is safe' if diagnosis.is_transient else 'not transient, so it needs the customer to act'}. "
        f"Customer segment '{segment}', amount at risk ₹{amount:,.2f}."
    )


def explain_decision(
    diagnosis: Diagnosis, action: str, amount: float, segment: str, retry_count: int
) -> tuple[str, str]:
    """
    Returns (reasoning_text, source) where source is 'claude' or 'rule_based'.
    """
    if _client is None:
        return _fallback_reasoning(diagnosis, action, amount, segment), "rule_based"

    prompt = (
        "You are the reasoning module of a payment-recovery agent for an Indian "
        "fintech. In 1-2 short sentences, explain WHY the chosen recovery action "
        "is appropriate for this failed/abandoned transaction. Be specific and "
        "concise, no preamble, no markdown.\n\n"
        f"Root cause: {diagnosis.root_cause} ({diagnosis.description})\n"
        f"Transient failure: {diagnosis.is_transient}\n"
        f"Chosen action: {action}\n"
        f"Amount at risk: INR {amount:,.2f}\n"
        f"Customer segment: {segment}\n"
        f"Prior retry attempts: {retry_count}\n"
    )
    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
        return (text or _fallback_reasoning(diagnosis, action, amount, segment)), "claude"
    except Exception:
        return _fallback_reasoning(diagnosis, action, amount, segment), "rule_based"
