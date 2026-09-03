# Vasooli AI — Verified Revenue Recovery

Built for Razorpay's AI Buildathon, Track 3: AI Revenue Recovery.

Vasooli detects failed or abandoned payments, diagnoses the failure, creates
one bounded recovery action, and records every state transition. **Revenue is
counted as recovered only after a signed Razorpay test-mode webhook verifies
the payment.** Mock links, messages, and legacy simulated outcomes are never
included in verified KPIs.

## Day 1 trust boundary

```text
At-risk transaction
        |
        v
Diagnosis + deterministic policy
        |
        +-- opt-out / limits ----------> stopped or escalated
        |
        +-- quiet hours ---------------> next_action_at (UTC)
        |
        v
action_created -- idempotency key --> Razorpay Payment Link
        |                                  |
        v                                  v
awaiting_payment                 signed raw-body webhook
                                           |
                              HMAC-SHA256 verification
                                           |
                              x-razorpay-event-id dedupe
                                           |
                              link id / reference correlation
                                           |
                   +-----------------------+------------------+
                   v                       v                  v
          payment_verified              expired            failed
```

## Guarantees implemented

- The exact raw webhook body is verified with HMAC-SHA256 before JSON parsing.
- `x-razorpay-event-id` is stored uniquely; duplicate deliveries return 200 but
  do not repeat state changes or audit entries.
- Payment Links carry `reference_id=<transaction id>` and their Razorpay link
  ID is stored for two-way correlation.
- Paid events are quarantined when currency, amount, link ID, or reference do
  not agree with the local transaction.
- Late expiry/cancellation events cannot undo a verified payment.
- Partial payments remain `awaiting_payment` and are excluded from the verified
  recovery KPI until the link becomes fully paid.
- Quiet-hour work is stored in `next_action_at` as UTC and evaluated in the
  configured recovery timezone; it no longer depends on a fixed fake hour.
- Old/random `recovered_amount` is returned only below a clearly labelled
  `simulation` object and is never included in verified metrics.

## Transaction statuses

Core recovery lifecycle:

```text
action_created -> awaiting_payment -> payment_verified | expired | failed
```

Pre-action safety states such as `at_risk`, `diagnosed`, `stopped`, and
`escalated` remain available so the audit trail explains why no action ran.

## Recovery Policy Lab (Day 2)

The dashboard includes a reproducible counterfactual experiment across 1,000
seeded synthetic customer journeys. It compares three policies against the
same potential outcomes:

- `always_retry`: deliberately naive baseline.
- `simple_rules`: fixed root-cause-to-action mappings with stopping rules.
- `contextual_policy`: chooses the bounded action with the highest expected net
  value using root cause, amount and customer segment, while abstaining for
  opt-outs and escalating unknown diagnoses.

Each run reports gross recovered amount, net recovered value after retry,
contact, escalation and discount costs, contact volume, unnecessary-contact
rate, compliance violations, action distribution and regret versus an oracle
that knows the generated potential outcomes. The contextual policy also emits
an honest exception list for abstentions and escalations.

This is explicitly a `seeded_counterfactual_simulation`. Its values are stored
separately and `included_in_verified_kpis` is always false. It never alters the
signed-webhook recovery ledger.

Run it from the dashboard or with:

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/api/policy-lab/run?seed=42&n=1000"
```

## Bounded AI diagnosis and Chaos Lab (Day 3)

Unstructured gateway logs are treated as untrusted diagnostic evidence. With
`GEMINI_API_KEY` configured, Gemini must return a strict typed proposal:

```json
{
  "root_cause": "card_expired",
  "confidence": 0.98,
  "evidence": ["instrument validation failed"],
  "proposed_action": "payment_link",
  "risk_flags": []
}
```

The schema rejects prose, unknown fields, unknown actions and out-of-range
confidence. API errors, timeouts and malformed model output fail closed to a
typed deterministic diagnosis. The model cannot call executors.

A separate deterministic policy guard checks opt-out status, contact budget,
confidence threshold, prompt-injection flags, trusted structured error codes
and the action allow-list. Every transaction records `AI_PROPOSED` followed by
`POLICY_APPROVED`, `POLICY_OVERRIDDEN`, `POLICY_BLOCKED` or
`POLICY_ABSTAINED` before `ACTION_CREATED` is possible.

The dashboard Chaos Lab deliberately injects five failures: malformed output,
unsafe retry, low confidence, customer opt-out and instructions hidden inside
an untrusted gateway log. Run it from the dashboard or:

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/api/chaos-lab/run"
```

## Transaction evidence view (Day 4)

Click any row in the Transactions table to open its evidence view. It keeps the
full demonstration in one place:

- current status, root cause, amount, contact count and next scheduled action;
- a direct test Payment Link when the action created one;
- the ordered AI, policy and execution decision trail;
- matched Razorpay events, signature status, event ID and processing outcome.

This view makes the trust boundary visible. `AI_PROPOSED` is advice,
`POLICY_*` is authorization, `ACTION_CREATED` is durable intent, and only
`PAYMENT_VERIFIED` from a signed webhook changes the verified KPI.

## Architecture

```text
Synthetic at-risk input
        |
        v
Structured diagnosis ---- Gemini proposal or typed fallback
        |                              |
        +------------------------------+
                       |
              deterministic guard
                       |
          approve / override / stop
                       |
              idempotent action
                       |
             Razorpay Payment Link
                       |
             signed webhook endpoint
                       |
       signature -> dedupe -> correlation
                       |
        verified ledger + audit evidence
```

The Policy Lab and Chaos Lab are evaluation tools. Neither writes recovered
money into the verified ledger.

## Configure Razorpay test mode

```powershell
Copy-Item .env.example .env
```

Fill these values in `.env`:

```dotenv
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=choose-a-separate-long-random-secret
```

The webhook secret is separate from the API key secret. In the Razorpay test
Dashboard, create a webhook using the same webhook secret and subscribe to:

- `payment_link.paid`
- `payment_link.partially_paid`
- `payment_link.expired`
- `payment_link.cancelled`

Point it at:

```text
https://YOUR-PUBLIC-HOST/api/webhooks/razorpay
```

Razorpay cannot send webhooks directly to localhost. Deploy the app or use a
Razorpay-supported public tunnel while testing.

## Configure Gemini

Create a Gemini API key in Google AI Studio and store it only as a server-side
environment variable:

```dotenv
GEMINI_API_KEY=your_key
GEMINI_MODEL=gemini-3.6-flash
```

When the key is absent, unavailable or returns invalid output, Vasooli uses the
typed deterministic fallback. The deterministic policy guard remains the only
component allowed to authorize an action.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
```

Open `http://localhost:8000`, generate a batch, and run the agent. A mock link
can demonstrate state creation without credentials, but only a signed event
against a real Razorpay test Payment Link can produce `payment_verified`.

## API surface

| Endpoint | Purpose |
|---|---|
| `POST /api/generate-batch?n=12` | Generate synthetic at-risk inputs |
| `POST /api/run-agent` | Process due, non-terminal transactions |
| `POST /api/policy-lab/run?seed=42&n=1000` | Reproducible policy comparison |
| `GET /api/policy-lab/latest` | Latest persisted policy experiment |
| `POST /api/chaos-lab/run` | Inject five AI-boundary failures |
| `POST /api/webhooks/razorpay` | Receive signed Razorpay events |
| `GET /api/webhook-events` | Inspect verified event outcomes and exceptions |
| `GET /api/metrics` | Verified KPIs plus isolated simulation values |
| `GET /api/transactions` | Current recovery state |
| `GET /api/transactions/{id}` | Transaction, decision trail and matched webhook evidence |
| `GET /api/audit-log` | Explainable state-transition history |

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The test suite covers raw-body signatures, duplicate rejection, payment
verification, amount and reference mismatch quarantine, unmatched events,
out-of-order expiry protection, quiet-hour scheduling, verified/simulated KPI
separation, policy evaluation, AI guardrails and transaction evidence.

## Five-minute demo order

1. Generate 12 records and run the recovery agent.
2. Click an `awaiting_payment` row and show `AI_PROPOSED`, `POLICY_*` and
   `ACTION_CREATED` in its decision trail.
3. Open its test Payment Link and complete a successful Razorpay test payment.
4. Refresh, reopen the row and show `PAYMENT_VERIFIED` plus the signed webhook
   event. Point out that the verified KPI changed only now.
5. Run the Policy Lab, then the Chaos Lab, and explain what each proves.

## Security note

Never commit `.env`, API keys, or webhook secrets. If a key has ever appeared
in a committed file, screenshot, chat, or shared archive, rotate it in the
Razorpay Dashboard before using the project.

## What broke, and how I got out

The first version marked transactions recovered using action-specific random
probabilities. That produced an attractive funnel but could not prove that any
payment occurred. The recovery result is now event-driven: a transaction only
becomes `payment_verified` after validating Razorpay's signature, deduplicating
the event ID, correlating its Payment Link, and checking the amount and
currency. Simulation remains available for later policy experiments, but its
money is isolated from verified KPIs.
