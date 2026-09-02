"""
Generates a synthetic batch of at-risk transactions that looks like what
Razorpay's own failure/abandonment stream would produce: a mix of payment
failures (with realistic gateway error codes) and checkout abandonments.

This is what the buildathon brief calls "a batch" — everything downstream
(root-cause diagnosis, recovery decisions, execution) runs against this data.
"""

import random

from faker import Faker

from backend.models import Transaction

fake = Faker("en_IN")

PAYMENT_METHODS = ["upi", "card", "netbanking", "wallet"]

# error_code -> relative frequency weight (roughly mirrors real-world
# distributions: insufficient funds and bank timeouts dominate)
FAILURE_ERROR_CODES = {
    "INSUFFICIENT_FUNDS": 28,
    "BANK_SERVER_TIMEOUT": 18,
    "OTP_MISMATCH": 14,
    "CARD_EXPIRED": 10,
    "NETWORK_ERROR": 12,
    "GATEWAY_TIMEOUT": 10,
    "ISSUER_DECLINED": 8,
}

GATEWAY_LOGS = {
    "INSUFFICIENT_FUNDS": "issuer_response=declined balance_check=insufficient instrument=customer_account",
    "BANK_SERVER_TIMEOUT": "issuer_host timed out after 3000ms; authorization result unknown; retryable=true",
    "OTP_MISMATCH": "3ds authentication failed: otp mismatch or expired challenge",
    "CARD_EXPIRED": "instrument validation failed: expiry date is in the past",
    "NETWORK_ERROR": "client connection reset before gateway confirmation; no authorization received",
    "GATEWAY_TIMEOUT": "psp gateway deadline exceeded; upstream status unavailable; retryable=true",
    "ISSUER_DECLINED": "issuer returned do_not_honor; retryable=false; manual review recommended",
}

SEGMENTS = ["high_value", "regular", "new_customer"]
SEGMENT_WEIGHTS = [15, 60, 25]

AMOUNT_RANGES = {
    "high_value": (8000, 45000),
    "regular": (500, 6000),
    "new_customer": (200, 2500),
}


def _pick_weighted(options_weights: dict[str, int]) -> str:
    options = list(options_weights.keys())
    weights = list(options_weights.values())
    return random.choices(options, weights=weights, k=1)[0]


def generate_batch(n: int = 120, abandonment_ratio: float = 0.30) -> list[Transaction]:
    """
    Returns a list of unpersisted Transaction ORM objects. Caller adds/commits.
    """
    batch: list[Transaction] = []
    n_abandoned = int(n * abandonment_ratio)
    n_failed = n - n_abandoned

    for _ in range(n_failed):
        segment = _pick_weighted(dict(zip(SEGMENTS, SEGMENT_WEIGHTS)))
        lo, hi = AMOUNT_RANGES[segment]
        error_code = _pick_weighted(FAILURE_ERROR_CODES)
        txn = Transaction(
            customer_id=fake.uuid4()[:8],
            customer_name=fake.name(),
            segment=segment,
            amount=round(random.uniform(lo, hi), 2),
            payment_method=random.choice(PAYMENT_METHODS),
            failure_type="payment_failed",
            error_code=error_code,
            gateway_log=GATEWAY_LOGS[error_code],
            simulated_hour=random.randint(0, 23),
            customer_opted_out=random.random() < 0.06,  # ~6% do-not-contact
            status="at_risk",
        )
        batch.append(txn)

    for _ in range(n_abandoned):
        segment = _pick_weighted(dict(zip(SEGMENTS, SEGMENT_WEIGHTS)))
        lo, hi = AMOUNT_RANGES[segment]
        txn = Transaction(
            customer_id=fake.uuid4()[:8],
            customer_name=fake.name(),
            segment=segment,
            amount=round(random.uniform(lo, hi), 2),
            payment_method=random.choice(PAYMENT_METHODS),
            failure_type="checkout_abandoned",
            error_code=None,
            gateway_log="checkout_opened=true payment_attempted=false session_expired=true",
            simulated_hour=random.randint(0, 23),
            customer_opted_out=random.random() < 0.06,
            status="at_risk",
        )
        batch.append(txn)

    random.shuffle(batch)
    return batch
