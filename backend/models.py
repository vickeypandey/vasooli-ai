import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def gen_id() -> str:
    return uuid.uuid4().hex[:12]


class Transaction(Base):
    """
    One at-risk payment: either a payment that FAILED after being attempted,
    or a checkout that was ABANDONED before payment was attempted.
    """

    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=gen_id)
    customer_id = Column(String, nullable=False)
    customer_name = Column(String, nullable=False)
    segment = Column(String, nullable=False)  # high_value | regular | new_customer

    amount = Column(Float, nullable=False)
    payment_method = Column(String, nullable=False)  # upi | card | netbanking | wallet
    failure_type = Column(String, nullable=False)  # payment_failed | checkout_abandoned
    error_code = Column(String, nullable=True)  # null for checkout_abandoned

    simulated_hour = Column(Integer, nullable=False)  # 0-23, hour-of-day this event occurred
    customer_opted_out = Column(Boolean, default=False)  # do-not-contact list
    created_at = Column(DateTime, default=datetime.utcnow)

    # Mutable pipeline state. Recovery is only verified by a signed webhook.
    status = Column(String, default="at_risk")
    # at_risk -> diagnosed -> action_created -> awaiting_payment
    # -> payment_verified | expired | failed (or stopped/escalated)
    root_cause = Column(String, nullable=True)
    retry_count = Column(Integer, default=0)
    contact_count = Column(Integer, default=0)
    # Legacy/simulation-only value. It is never included in verified metrics.
    recovered_amount = Column(Float, nullable=True)
    verified_recovered_amount = Column(Float, nullable=False, default=0.0)
    payment_link = Column(String, nullable=True)
    razorpay_payment_link_id = Column(String, nullable=True, index=True)
    payment_link_reference_id = Column(String, nullable=True, index=True)
    razorpay_payment_id = Column(String, nullable=True)
    action_idempotency_key = Column(String, nullable=True, unique=True)
    last_action_at = Column(DateTime, nullable=True)
    next_action_at = Column(DateTime, nullable=True, index=True)

    audit_logs = relationship(
        "AuditLog", back_populates="transaction", cascade="all, delete-orphan"
    )


class AuditLog(Base):
    """
    Every decision the agent makes, with the reasoning behind it, so the
    whole recovery workflow is explainable and reviewable end to end.
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    stage = Column(String, nullable=False)
    # DETECTED | DIAGNOSED | DECIDED | ACTED | STOPPED | RESOLVED
    root_cause = Column(String, nullable=True)
    action_taken = Column(String, nullable=True)
    reasoning = Column(Text, nullable=True)
    reasoning_source = Column(String, default="rule_based")  # rule_based | claude
    outcome = Column(String, nullable=True)

    transaction = relationship("Transaction", back_populates="audit_logs")


class WebhookEvent(Base):
    """A verified Razorpay event. event_id is the idempotency boundary."""

    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, nullable=False, unique=True, index=True)
    event_type = Column(String, nullable=False)
    payload_sha256 = Column(String, nullable=False)
    signature_valid = Column(Boolean, nullable=False, default=True)
    payment_link_id = Column(String, nullable=True, index=True)
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)
    outcome = Column(String, nullable=False, default="received")
    error = Column(Text, nullable=True)
    raw_payload = Column(Text, nullable=False)


class PolicyExperiment(Base):
    """Persisted, reproducible simulation output; never a verified-money row."""

    __tablename__ = "policy_experiments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seed = Column(Integer, nullable=False)
    batch_size = Column(Integer, nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
