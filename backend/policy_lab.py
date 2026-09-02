"""Deterministic counterfactual evaluation for bounded recovery policies.

This module is intentionally separate from verified Razorpay KPIs. It creates
seeded synthetic journeys with potential outcomes for every action, allowing
the same batch to be evaluated fairly under multiple policies.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from dataclasses import dataclass


ACTIONS = ("retry", "payment_link", "discount_nudge", "human_escalation", "no_action")
CONTACT_ACTIONS = {"payment_link", "discount_nudge", "human_escalation"}

ROOT_CAUSE_WEIGHTS = {
    "bank_server_issue": 17,
    "network_drop": 12,
    "gateway_issue": 10,
    "insufficient_funds": 20,
    "otp_failure": 11,
    "card_expired": 8,
    "issuer_declined": 7,
    "checkout_abandonment": 13,
    "unknown": 2,
}

BASE_RECOVERY_PROBABILITY = {
    "bank_server_issue": {"retry": .72, "payment_link": .38, "discount_nudge": .31, "human_escalation": .16},
    "network_drop": {"retry": .68, "payment_link": .42, "discount_nudge": .34, "human_escalation": .14},
    "gateway_issue": {"retry": .65, "payment_link": .40, "discount_nudge": .32, "human_escalation": .15},
    "insufficient_funds": {"retry": .08, "payment_link": .51, "discount_nudge": .62, "human_escalation": .27},
    "otp_failure": {"retry": .12, "payment_link": .59, "discount_nudge": .49, "human_escalation": .22},
    "card_expired": {"retry": .02, "payment_link": .68, "discount_nudge": .52, "human_escalation": .25},
    "issuer_declined": {"retry": .03, "payment_link": .29, "discount_nudge": .25, "human_escalation": .56},
    "checkout_abandonment": {"retry": 0, "payment_link": .39, "discount_nudge": .65, "human_escalation": .14},
    "unknown": {"retry": .09, "payment_link": .22, "discount_nudge": .25, "human_escalation": .31},
}


@dataclass(frozen=True)
class Journey:
    journey_id: str
    amount: float
    segment: str
    root_cause: str
    opted_out: bool
    potential_outcomes: dict[str, bool]


def _weighted_choice(rng: random.Random, weighted: dict[str, int]) -> str:
    return rng.choices(list(weighted), weights=list(weighted.values()), k=1)[0]


def _probability(root_cause: str, action: str, segment: str) -> float:
    if action == "no_action":
        return 0.0
    probability = BASE_RECOVERY_PROBABILITY[root_cause][action]
    if segment == "new_customer" and action == "discount_nudge":
        probability += .07
    if segment == "high_value" and action == "human_escalation":
        probability += .10
    if segment == "high_value" and action == "discount_nudge":
        probability -= .05
    return max(0.0, min(.95, probability))


def generate_journeys(seed: int, n: int) -> list[Journey]:
    rng = random.Random(seed)
    journeys = []
    segments = ["regular", "new_customer", "high_value"]
    segment_weights = [60, 25, 15]
    ranges = {
        "regular": (500, 6000),
        "new_customer": (200, 2500),
        "high_value": (8000, 45000),
    }
    for index in range(n):
        segment = rng.choices(segments, segment_weights, k=1)[0]
        low, high = ranges[segment]
        amount = round(rng.uniform(low, high), 2)
        cause = _weighted_choice(rng, ROOT_CAUSE_WEIGHTS)
        opted_out = rng.random() < .06
        outcomes = {
            action: rng.random() < _probability(cause, action, segment)
            for action in ACTIONS
        }
        journeys.append(Journey(
            journey_id=f"sim-{seed}-{index:05d}",
            amount=amount,
            segment=segment,
            root_cause=cause,
            opted_out=opted_out,
            potential_outcomes=outcomes,
        ))
    return journeys


def always_retry(_: Journey) -> tuple[str, str]:
    return "retry", "Baseline retries every failure without diagnosis."


def simple_rules(journey: Journey) -> tuple[str, str]:
    if journey.opted_out:
        return "no_action", "Opt-out stopping rule."
    if journey.root_cause in {"bank_server_issue", "network_drop", "gateway_issue"}:
        return "retry", "Known transient failure."
    if journey.root_cause == "checkout_abandonment":
        return "discount_nudge", "Static abandonment rule."
    if journey.root_cause == "issuer_declined":
        return "human_escalation", "Issuer declines require review."
    if journey.root_cause == "unknown":
        return "human_escalation", "Unknown cause is not auto-actioned."
    return "payment_link", "Static hard-failure rule."


def _expected_net_value(journey: Journey, action: str) -> float:
    probability = _probability(journey.root_cause, action, journey.segment)
    fixed_cost = {
        "retry": 2.0,
        "payment_link": 1.5,
        "discount_nudge": 1.5,
        "human_escalation": 50.0,
        "no_action": 0.0,
    }[action]
    expected_discount = probability * journey.amount * .10 if action == "discount_nudge" else 0.0
    return probability * journey.amount - fixed_cost - expected_discount


def contextual_policy(journey: Journey) -> tuple[str, str]:
    if journey.opted_out:
        return "no_action", "Abstained: customer opted out."
    if journey.root_cause == "unknown":
        return "human_escalation", "Abstained from automation: diagnosis is unknown."
    candidates = ("retry", "payment_link", "discount_nudge", "human_escalation", "no_action")
    values = {action: _expected_net_value(journey, action) for action in candidates}
    action = max(values, key=values.get)
    return action, f"Highest bounded expected net value: INR {values[action]:.2f}."


POLICIES = {
    "always_retry": always_retry,
    "simple_rules": simple_rules,
    "contextual_policy": contextual_policy,
}


def _realized_value(journey: Journey, action: str) -> tuple[float, float, bool]:
    recovered = journey.potential_outcomes[action]
    recovered_amount = journey.amount if recovered else 0.0
    cost = {
        "retry": 2.0,
        "payment_link": 1.5,
        "discount_nudge": 1.5 + (journey.amount * .10 if recovered else 0.0),
        "human_escalation": 50.0,
        "no_action": 0.0,
    }[action]
    return recovered_amount, recovered_amount - cost, recovered


def _oracle_value(journey: Journey) -> float:
    allowed = ("no_action",) if journey.opted_out else ACTIONS
    return max(_realized_value(journey, action)[1] for action in allowed)


def evaluate_policy(name: str, journeys: list[Journey]) -> dict:
    policy = POLICIES[name]
    recovered_amount = 0.0
    net_value = 0.0
    recovered_count = 0
    contacts = 0
    unnecessary_contacts = 0
    compliance_violations = 0
    regret = 0.0
    actions = Counter()
    exceptions = []

    for journey in journeys:
        action, reason = policy(journey)
        actions[action] += 1
        recovered, realized, did_recover = _realized_value(journey, action)
        recovered_amount += recovered
        net_value += realized
        recovered_count += int(did_recover)
        if action in CONTACT_ACTIONS:
            contacts += 1
            unnecessary_contacts += int(not did_recover)
        if journey.opted_out and action != "no_action":
            compliance_violations += 1
        regret += max(0.0, _oracle_value(journey) - realized)
        if name == "contextual_policy" and action in {"no_action", "human_escalation"} and len(exceptions) < 25:
            exceptions.append({
                "journey_id": journey.journey_id,
                "root_cause": journey.root_cause,
                "segment": journey.segment,
                "amount": journey.amount,
                "action": action,
                "reason": reason,
            })

    count = len(journeys)
    return {
        "policy": name,
        "records": count,
        "recovered_count": recovered_count,
        "recovered_amount": round(recovered_amount, 2),
        "net_recovered_value": round(net_value, 2),
        "recovery_rate_pct": round(recovered_count / count * 100, 2),
        "contacts": contacts,
        "unnecessary_contacts": unnecessary_contacts,
        "unnecessary_contact_rate_pct": round(unnecessary_contacts / contacts * 100, 2) if contacts else 0.0,
        "compliance_violations": compliance_violations,
        "average_oracle_regret": round(regret / count, 2),
        "action_counts": dict(actions),
        "exceptions": exceptions,
    }


def run_experiment(seed: int = 42, n: int = 1000) -> dict:
    started = time.perf_counter()
    journeys = generate_journeys(seed, n)
    results = [evaluate_policy(name, journeys) for name in POLICIES]
    elapsed = max(time.perf_counter() - started, .000001)
    return {
        "experiment_type": "seeded_counterfactual_simulation",
        "seed": seed,
        "batch_size": n,
        "included_in_verified_kpis": False,
        "ground_truth": "Potential outcomes are generated once per journey and shared by every policy.",
        "elapsed_ms": round(elapsed * 1000, 2),
        "records_per_second": round((n * len(POLICIES)) / elapsed, 2),
        "policies": results,
        "contextual_exceptions": next(r["exceptions"] for r in results if r["policy"] == "contextual_policy"),
    }
