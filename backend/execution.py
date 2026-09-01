"""
Executes externally visible recovery actions:
  - payment_link: create a Razorpay Payment Link (real, test mode, if keys
    are configured — otherwise a realistic mock link) and "send" it
  - discount_nudge: send a message with an incentive
  - escalate_to_human: hand off, no automated money movement
  - stop: compliance stop, no action taken

This module does not decide or simulate payment outcomes. Recovery is recorded
only by the signed Razorpay webhook path in backend/webhooks.py.
"""

import uuid

from backend.config import settings

_razorpay_client = None
if settings.razorpay_live:
    try:
        import razorpay

        _razorpay_client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
    except Exception as e:
        print(f"[razorpay] client init failed: {e}")
        _razorpay_client = None

def create_payment_link(
    amount: float, customer_name: str, txn_id: str
) -> tuple[str, str, str, bool, str]:
    """
    Returns (url, link_id, reference_id, was_real, error). Uses test-mode
    API if configured, otherwise returns a clearly-labeled mock link.
    error_message is "" on success, or the real exception text on failure —
    surfaced in the audit trail instead of only being printed to the console.
    """
    if _razorpay_client is not None:
        reference_id = txn_id
        try:
            link = _razorpay_client.payment_link.create(
                {
                    "amount": int(amount * 100),  # paise
                    "currency": "INR",
                    "description": f"Complete your payment (ref {txn_id})",
                    "reference_id": reference_id,
                    "customer": {"name": customer_name},
                    "notify": {"sms": False, "email": False},
                    "reminder_enable": True,
                    "notes": {"source": "vasooli-ai-recovery-agent", "txn_ref": txn_id},
                }
            )
            return (
                link.get("short_url", ""),
                link.get("id", ""),
                link.get("reference_id", reference_id),
                True,
                "",
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"[razorpay] payment_link.create failed, falling back to mock: {err}", flush=True)
            return "", "", reference_id, False, err
    mock_id = f"mock_plink_{uuid.uuid4().hex[:12]}"
    return (
        f"https://rzp.io/mock/{mock_id}",
        mock_id,
        txn_id,
        False,
        "no_razorpay_keys_configured",
    )


def send_message(channel: str, customer_name: str, amount: float, link: str) -> str:
    """
    Simulated send (SMS/WhatsApp/email). Logs what WOULD have been sent.
    Swap this for a real Twilio/WhatsApp Business API call when ready.
    """
    return (
        f"[SIMULATED {channel.upper()}] To {customer_name}: Your payment of "
        f"₹{amount:,.2f} needs one more step — complete it here: {link}"
    )
