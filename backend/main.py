import json
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.agent import run_agent_on_batch
from backend.chaos_lab import run_chaos_suite
from backend.config import settings
from backend.data_generator import generate_batch
from backend.database import SessionLocal, get_db, init_db
from backend.models import AuditLog, PolicyExperiment, Transaction, WebhookEvent
from backend.policy_lab import run_experiment
from backend.webhooks import parse_json, process_verified_event, valid_signature

app = FastAPI(title="Vasooli AI — Revenue Recovery Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()


# ---------------------------------------------------------------- helpers --
def _txn_dict(t: Transaction) -> dict:
    return {
        "id": t.id,
        "customer_name": t.customer_name,
        "segment": t.segment,
        "amount": t.amount,
        "payment_method": t.payment_method,
        "failure_type": t.failure_type,
        "error_code": t.error_code,
        "gateway_log": t.gateway_log,
        "root_cause": t.root_cause,
        "status": t.status,
        "retry_count": t.retry_count,
        "contact_count": t.contact_count,
        "recovered_amount": t.recovered_amount,
        "verified_recovered_amount": t.verified_recovered_amount,
        "payment_link": t.payment_link,
        "razorpay_payment_link_id": t.razorpay_payment_link_id,
        "payment_link_reference_id": t.payment_link_reference_id,
        "razorpay_payment_id": t.razorpay_payment_id,
        "next_action_at": t.next_action_at.isoformat() if t.next_action_at else None,
        "simulated_hour": t.simulated_hour,
        "customer_opted_out": t.customer_opted_out,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _log_dict(l: AuditLog) -> dict:
    return {
        "id": l.id,
        "transaction_id": l.transaction_id,
        "timestamp": l.timestamp.isoformat() if l.timestamp else None,
        "stage": l.stage,
        "root_cause": l.root_cause,
        "action_taken": l.action_taken,
        "reasoning": l.reasoning,
        "reasoning_source": l.reasoning_source,
        "outcome": l.outcome,
    }


# ------------------------------------------------------------------ API ---
@app.post("/api/generate-batch")
def api_generate_batch(n: int = 120, db: Session = Depends(get_db)):
    batch = generate_batch(n=n)
    db.add_all(batch)
    db.commit()
    return {"generated": len(batch)}


@app.post("/api/run-agent")
def api_run_agent(db: Session = Depends(get_db)):
    txns = db.query(Transaction).all()
    result = run_agent_on_batch(db, txns)
    return result


@app.post("/api/reset")
def api_reset(db: Session = Depends(get_db)):
    db.query(WebhookEvent).delete()
    db.query(AuditLog).delete()
    db.query(Transaction).delete()
    db.commit()
    return {"reset": True}


@app.get("/api/transactions")
def api_transactions(db: Session = Depends(get_db)):
    txns = db.query(Transaction).order_by(Transaction.created_at.desc()).all()
    return [_txn_dict(t) for t in txns]


@app.get("/api/audit-log")
def api_audit_log(limit: int = 500, db: Session = Depends(get_db)):
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()
    return [_log_dict(l) for l in logs]


@app.get("/api/webhook-events")
def api_webhook_events(limit: int = 100, db: Session = Depends(get_db)):
    events = db.query(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(min(limit, 500)).all()
    return [{
        "event_id": e.event_id,
        "event_type": e.event_type,
        "transaction_id": e.transaction_id,
        "payment_link_id": e.payment_link_id,
        "signature_valid": e.signature_valid,
        "outcome": e.outcome,
        "error": e.error,
        "received_at": e.received_at.isoformat() if e.received_at else None,
    } for e in events]


@app.post("/api/chaos-lab/run")
def api_run_chaos_lab():
    return run_chaos_suite()


@app.post("/api/policy-lab/run")
def api_run_policy_lab(
    seed: int = Query(42, ge=0, le=2_147_483_647),
    n: int = Query(1000, ge=50, le=10_000),
    db: Session = Depends(get_db),
):
    result = run_experiment(seed=seed, n=n)
    experiment = PolicyExperiment(
        seed=seed,
        batch_size=n,
        result_json=json.dumps(result, separators=(",", ":")),
    )
    db.add(experiment)
    db.commit()
    db.refresh(experiment)
    result["experiment_id"] = experiment.id
    result["created_at"] = experiment.created_at.isoformat()
    return result


@app.get("/api/policy-lab/latest")
def api_latest_policy_lab(db: Session = Depends(get_db)):
    experiment = db.query(PolicyExperiment).order_by(PolicyExperiment.created_at.desc()).first()
    if experiment is None:
        return {"available": False}
    result = json.loads(experiment.result_json)
    result["available"] = True
    result["experiment_id"] = experiment.id
    result["created_at"] = experiment.created_at.isoformat()
    return result


@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Validate the exact raw body before parsing or changing any state."""
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="RAZORPAY_WEBHOOK_SECRET is not configured")
    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    event_id = request.headers.get("x-razorpay-event-id", "")
    if not event_id:
        raise HTTPException(status_code=400, detail="x-razorpay-event-id header is required")
    if not valid_signature(raw_body, signature, settings.RAZORPAY_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid Razorpay webhook signature")
    try:
        payload = parse_json(raw_body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return process_verified_event(db, event_id, raw_body, payload)


@app.get("/api/metrics")
def api_metrics(db: Session = Depends(get_db)):
    total = db.query(func.count(Transaction.id)).scalar() or 0
    total_at_risk_amount = db.query(func.coalesce(func.sum(Transaction.amount), 0.0)).scalar() or 0.0

    by_status = dict(
        db.query(Transaction.status, func.count(Transaction.id))
        .group_by(Transaction.status)
        .all()
    )

    verified_recovered_amount = (
        db.query(func.coalesce(func.sum(Transaction.verified_recovered_amount), 0.0)).scalar() or 0.0
    )
    verified_recovered_count = by_status.get("payment_verified", 0)
    simulated_recovered_amount = (
        db.query(func.coalesce(func.sum(Transaction.recovered_amount), 0.0)).scalar() or 0.0
    )

    funnel = {
        "at_risk": total,
        "action_created": by_status.get("action_created", 0),
        "awaiting_payment": by_status.get("awaiting_payment", 0),
        "payment_verified": verified_recovered_count,
        "expired": by_status.get("expired", 0),
        "failed": by_status.get("failed", 0),
        "escalated": by_status.get("escalated", 0),
        "stopped_compliance": by_status.get("stopped", 0),
        "still_in_flight": sum(by_status.get(s, 0) for s in (
            "at_risk", "diagnosed", "action_created", "awaiting_payment"
        )),
    }

    return {
        "total_transactions": total,
        "total_at_risk_amount": round(total_at_risk_amount, 2),
        "verified_recovered_amount": round(verified_recovered_amount, 2),
        "verified_recovered_count": verified_recovered_count,
        "verified_recovery_rate_pct": round((verified_recovered_amount / total_at_risk_amount * 100), 2)
        if total_at_risk_amount
        else 0.0,
        "simulation": {
            "recovered_amount": round(simulated_recovered_amount, 2),
            "included_in_verified_kpis": False,
        },
        "by_status": by_status,
        "funnel": funnel,
        "mode": {
            "razorpay_live": settings.razorpay_live,
            "llm_live": settings.llm_live,
            "webhook_ready": settings.webhook_ready,
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "razorpay_live": settings.razorpay_live,
        "webhook_ready": settings.webhook_ready,
        "llm_live": settings.llm_live,
    }


# ------------------------------------------------------------- dashboard --
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
def dashboard():
    return FileResponse("frontend/index.html")
