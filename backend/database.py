import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.config import settings
from backend.models import Base

os.makedirs("data", exist_ok=True)

engine = create_engine(
    settings.DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    Base.metadata.create_all(bind=engine)
    # create_all does not add columns to an existing SQLite database. These
    # additive migrations preserve the user's current local demo data.
    columns = {c["name"] for c in inspect(engine).get_columns("transactions")}
    additions = {
        "verified_recovered_amount": "FLOAT NOT NULL DEFAULT 0",
        "razorpay_payment_link_id": "VARCHAR",
        "payment_link_reference_id": "VARCHAR",
        "razorpay_payment_id": "VARCHAR",
        "action_idempotency_key": "VARCHAR",
        "next_action_at": "DATETIME",
    }
    with engine.begin() as connection:
        for name, ddl in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE transactions ADD COLUMN {name} {ddl}"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_action_idempotency_key "
            "ON transactions(action_idempotency_key)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_transactions_payment_link_id "
            "ON transactions(razorpay_payment_link_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_transactions_next_action_at "
            "ON transactions(next_action_at)"
        ))
        # Old 'recovered' rows came from random simulation and must never appear
        # as verified recovery after this migration.
        connection.execute(text("UPDATE transactions SET status='failed' WHERE status='recovered'"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
