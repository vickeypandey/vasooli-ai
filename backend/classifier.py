"""
Root-cause diagnosis: turns a raw error_code / failure_type into a category
the decision agent can act on, plus whether the failure is "transient"
(worth an automatic retry) or "hard" (needs the customer to do something,
so a message/nudge is the right move instead).
"""

from dataclasses import dataclass


@dataclass
class Diagnosis:
    root_cause: str
    is_transient: bool
    description: str


_RULES: dict[str, Diagnosis] = {
    "INSUFFICIENT_FUNDS": Diagnosis(
        "insufficient_funds",
        is_transient=False,
        description="Customer's account did not have enough balance at the time of charge.",
    ),
    "BANK_SERVER_TIMEOUT": Diagnosis(
        "bank_server_issue",
        is_transient=True,
        description="Issuing bank's server timed out mid-authorization — not the customer's fault.",
    ),
    "OTP_MISMATCH": Diagnosis(
        "otp_failure",
        is_transient=False,
        description="Customer entered the wrong OTP or it expired before submission.",
    ),
    "CARD_EXPIRED": Diagnosis(
        "card_expired",
        is_transient=False,
        description="Saved card has expired; a retry with the same instrument will always fail.",
    ),
    "NETWORK_ERROR": Diagnosis(
        "network_drop",
        is_transient=True,
        description="Connection dropped between customer and gateway before confirmation.",
    ),
    "GATEWAY_TIMEOUT": Diagnosis(
        "gateway_issue",
        is_transient=True,
        description="Razorpay/PSP gateway itself timed out — safe to retry shortly.",
    ),
    "ISSUER_DECLINED": Diagnosis(
        "issuer_declined",
        is_transient=False,
        description="Card issuer declined the transaction (risk rules, limits, etc.).",
    ),
}

_ABANDONMENT = Diagnosis(
    "checkout_abandonment",
    is_transient=False,
    description="Customer reached checkout but did not attempt payment at all.",
)


def diagnose(failure_type: str, error_code: str | None) -> Diagnosis:
    if failure_type == "checkout_abandoned" or error_code is None:
        return _ABANDONMENT
    return _RULES.get(
        error_code,
        Diagnosis("unknown", is_transient=False, description="Unrecognized error code."),
    )
