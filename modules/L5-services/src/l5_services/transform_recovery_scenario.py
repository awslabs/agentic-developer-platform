"""Reconcile a bounded offline recovery sequence with native effect ledger snapshots."""

from datetime import datetime, timedelta

from l5_services.transform_recovery_probe import FAULT_ACTIONS, probe_recovery


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("fault or ledger timestamp is invalid") from None
    _require(parsed.tzinfo is not None, "fault or ledger timestamp lacks a zone")
    return parsed


async def verify_ec04(driver, *, run_id, scope, started_at, deadline_seconds, max_reconcile_checks):
    _require(isinstance(run_id, str) and bool(run_id)
             and type(deadline_seconds) is int and 1 <= deadline_seconds <= 300,
             "recovery requires a pinned run and bounded deadline")
    started = _time(started_at)
    deadline = started + timedelta(seconds=deadline_seconds)
    report = await probe_recovery(driver, run_id=run_id, scope=scope,
                                  max_reconcile_checks=max_reconcile_checks)
    attempts = await driver.read_attempts(run_id)
    trace = await driver.read_fault_trace(run_id)
    ledger = await driver.read_effect_ledger(run_id)
    _require(isinstance(attempts, list) and len(attempts) == 1
             and isinstance(trace, list) and len(trace) == len(FAULT_ACTIONS)
             and isinstance(ledger, dict) and ledger.get("source") == "native-provider-ledger"
             and ledger.get("run_id") == run_id and ledger.get("scope") == scope
             and isinstance(ledger.get("evidence_ref"), str) and bool(ledger["evidence_ref"])
             and ledger["evidence_ref"] not in report["evidence_refs"]
             and isinstance(ledger.get("effects"), list) and len(ledger["effects"]) == 1
             and isinstance(ledger.get("samples"), list)
             and len(ledger["samples"]) == len(FAULT_ACTIONS),
             "native effect ledger is missing or not scoped to one effect")
    attempt, effect = attempts[0], ledger["effects"][0]
    _require(isinstance(attempt, dict) and attempt.get("attempt_id") == report["attempt_id"]
             and isinstance(effect, dict) and effect.get("source") == "native-provider"
             and effect.get("native_id") == report["native_id"]
             and effect.get("attempt_token") == attempt.get("idempotency_token")
             and effect.get("input_hash") == attempt.get("input_hash")
             and effect.get("evidence_ref") in report["evidence_refs"]
             and started <= _time(effect.get("observed_at")) <= deadline,
             "native ledger effect differs from the committed attempt")
    references = set(report["evidence_refs"]) | {ledger["evidence_ref"]}
    previous = _time(effect["observed_at"])
    completed = False
    for (fault, _), event, sample in zip(FAULT_ACTIONS, trace, ledger["samples"]):
        _require(isinstance(sample, dict) and sample.get("source") == "native-provider-ledger"
                 and sample.get("fault") == fault and sample.get("fault_ref") == event["evidence_ref"]
                 and sample.get("native_id") == report["native_id"]
                 and sample.get("attempt_token") == attempt["idempotency_token"]
                 and sample.get("effect_count") == 1
                 and sample.get("state") in {"running", "completed"}
                 and isinstance(sample.get("evidence_ref"), str)
                 and bool(sample["evidence_ref"]) and sample["evidence_ref"] not in references,
                 "native effect count or identity changed across recovery faults")
        observed = _time(sample.get("observed_at"))
        _require(previous < observed <= deadline and started < _time(event["observed_at"]) <= observed
                 and not (completed and sample["state"] != "completed"),
                 "recovery ledger is out of order, unbounded or regressed")
        completed = sample["state"] == "completed"
        references.add(sample["evidence_ref"])
        previous = observed
    return {"phase": "offline-recovery-faults", "live_qualified": False,
            "attempt_id": report["attempt_id"], "native_id": report["native_id"],
            "effect_count": 1, "reconcile_checks": report["reconcile_checks"],
            "evidence_refs": sorted(references)}
