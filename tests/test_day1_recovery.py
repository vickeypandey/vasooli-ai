import hashlib
import hmac
import json
import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from backend.agent import _next_contact_time
from backend.config import settings
from backend.database import get_db
from backend.main import api_metrics, app
from backend.models import Base, Transaction, WebhookEvent
from backend.policy_lab import run_experiment
from backend.webhooks import process_verified_event, valid_signature


class DayOneRecoveryTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        def override_db():
            yield self.db
        app.dependency_overrides[get_db] = override_db

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()

    def transaction(self, **overrides):
        values = dict(
            id="txn_test_1",
            customer_id="customer_1",
            customer_name="Test Customer",
            segment="regular",
            amount=500.0,
            payment_method="upi",
            failure_type="checkout_abandoned",
            simulated_hour=12,
            status="awaiting_payment",
            razorpay_payment_link_id="plink_test_1",
            payment_link_reference_id="txn_test_1",
        )
        values.update(overrides)
        txn = Transaction(**values)
        self.db.add(txn)
        self.db.commit()
        return txn

    @staticmethod
    def paid_payload(amount=50000):
        return {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {
                    "id": "plink_test_1",
                    "reference_id": "txn_test_1",
                    "amount": amount,
                    "amount_paid": amount,
                    "currency": "INR",
                }},
                "payment": {"entity": {"id": "pay_test_1"}},
            },
        }

    def test_signature_uses_exact_raw_body(self):
        body = b'{"event":"payment_link.paid"}'
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(valid_signature(body, signature, "secret"))
        self.assertFalse(valid_signature(body + b" ", signature, "secret"))

    def test_http_endpoint_rejects_bad_signature_and_accepts_valid_one(self):
        self.transaction()
        payload = self.paid_payload()
        body = json.dumps(payload, separators=(",", ":")).encode()
        old_secret = settings.RAZORPAY_WEBHOOK_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = "endpoint-secret"
        try:
            client = TestClient(app)
            bad = client.post(
                "/api/webhooks/razorpay",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-razorpay-event-id": "http_event_bad",
                    "x-razorpay-signature": "invalid",
                },
            )
            signature = hmac.new(b"endpoint-secret", body, hashlib.sha256).hexdigest()
            good = client.post(
                "/api/webhooks/razorpay",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-razorpay-event-id": "http_event_good",
                    "x-razorpay-signature": signature,
                },
            )
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = old_secret
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()["outcome"], "payment_verified")

    def test_paid_event_verifies_once_and_duplicate_is_rejected(self):
        txn = self.transaction(recovered_amount=4999.0)
        payload = self.paid_payload()
        raw = json.dumps(payload, separators=(",", ":")).encode()

        first = process_verified_event(self.db, "event_1", raw, payload)
        second = process_verified_event(self.db, "event_1", raw, payload)
        self.db.refresh(txn)

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(txn.status, "payment_verified")
        self.assertEqual(txn.verified_recovered_amount, 500.0)
        self.assertEqual(txn.razorpay_payment_id, "pay_test_1")
        self.assertEqual(self.db.query(WebhookEvent).count(), 1)

    def test_amount_mismatch_is_quarantined_not_counted(self):
        txn = self.transaction()
        payload = self.paid_payload(amount=40000)
        raw = json.dumps(payload).encode()
        result = process_verified_event(self.db, "event_bad_amount", raw, payload)
        self.db.refresh(txn)

        self.assertEqual(result["outcome"], "quarantined_mismatch")
        self.assertEqual(txn.status, "awaiting_payment")
        self.assertEqual(txn.verified_recovered_amount, 0.0)

    def test_late_expiry_cannot_undo_verified_payment(self):
        txn = self.transaction()
        paid = self.paid_payload()
        process_verified_event(self.db, "event_paid", json.dumps(paid).encode(), paid)
        expired = {
            "event": "payment_link.expired",
            "payload": {"payment_link": {"entity": {
                "id": "plink_test_1", "reference_id": "txn_test_1"
            }}},
        }
        process_verified_event(self.db, "event_expired", json.dumps(expired).encode(), expired)
        self.db.refresh(txn)
        self.assertEqual(txn.status, "payment_verified")
        self.assertEqual(txn.verified_recovered_amount, 500.0)

    def test_metrics_separate_verified_and_simulated_money(self):
        self.transaction(
            status="payment_verified",
            verified_recovered_amount=500.0,
            recovered_amount=9000.0,
        )
        metrics = api_metrics(self.db)
        self.assertEqual(metrics["verified_recovered_amount"], 500.0)
        self.assertEqual(metrics["simulation"]["recovered_amount"], 9000.0)
        self.assertFalse(metrics["simulation"]["included_in_verified_kpis"])

    def test_quiet_hours_schedule_to_next_allowed_time(self):
        # 18:00 UTC = 23:30 Asia/Kolkata; next 08:00 IST = 02:30 UTC.
        scheduled = _next_contact_time(datetime(2026, 9, 1, 18, 0))
        self.assertEqual(scheduled, datetime(2026, 9, 2, 2, 30))

    def test_policy_lab_is_reproducible_and_separate_from_verified_kpis(self):
        first = run_experiment(seed=42, n=1000)
        second = run_experiment(seed=42, n=1000)
        self.assertEqual(first["policies"], second["policies"])
        self.assertFalse(first["included_in_verified_kpis"])
        self.assertEqual(first["batch_size"], 1000)

    def test_contextual_policy_beats_rules_without_compliance_violations(self):
        result = run_experiment(seed=42, n=1000)
        policies = {p["policy"]: p for p in result["policies"]}
        contextual = policies["contextual_policy"]
        rules = policies["simple_rules"]
        self.assertEqual(contextual["compliance_violations"], 0)
        self.assertGreater(contextual["net_recovered_value"], rules["net_recovered_value"])
        self.assertLess(
            contextual["unnecessary_contact_rate_pct"],
            rules["unnecessary_contact_rate_pct"],
        )

    def test_policy_lab_http_run_is_persisted_as_latest(self):
        client = TestClient(app)
        run = client.post("/api/policy-lab/run?seed=77&n=100")
        latest = client.get("/api/policy-lab/latest")
        self.assertEqual(run.status_code, 200)
        self.assertEqual(latest.status_code, 200)
        self.assertTrue(latest.json()["available"])
        self.assertEqual(latest.json()["seed"], 77)
        self.assertEqual(latest.json()["batch_size"], 100)


if __name__ == "__main__":
    unittest.main()
