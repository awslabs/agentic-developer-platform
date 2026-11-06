"""Check bounded Transform recovery against SQL attempts and native effect readback."""

from datetime import datetime


FAULT_ACTIONS = (
    ("lost_ack", "submit"), ("restart", "read"), ("replay", "read"),
    ("stale", "denied"), ("cancel", "local_cancel"),
)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("recovery trace timestamp is invalid") from None
    _require(stamp.tzinfo is not None, "recovery trace timestamp lacks zone")
    return stamp


async def probe_recovery(driver, *, run_id, scope, max_reconcile_checks):
    _require(type(max_reconcile_checks) is int and 1 <= max_reconcile_checks <= 10
             and isinstance(scope, dict) and bool(scope.get("tenant_id")),
             "recovery needs bounded, scoped authority")
    run = await driver.read_run(run_id)
    attempts = await driver.read_attempts(run_id)
    trace = await driver.read_fault_trace(run_id)
    effects = await driver.read_native_effects(run_id)
    _require(isinstance(run, dict) and run.get("run_id") == run_id
             and run.get("scope") == scope and run.get("state") == "cancelled"
             and isinstance(attempts, list) and len(attempts) == 1
             and isinstance(effects, list) and len(effects) == 1,
             "recovery did not preserve one scoped paid attempt and effect")
    attempt = attempts[0]
    native = run.get("native_job_ref")
    effect = effects[0]
    _require(isinstance(attempt, dict) and attempt.get("run_id") == run_id
             and attempt.get("tenant_id") == scope["tenant_id"]
             and attempt.get("operation") == "transform_provider_submit"
             and isinstance(attempt.get("attempt_id"), str) and bool(attempt["attempt_id"])
             and isinstance(attempt.get("idempotency_token"), str) and bool(attempt["idempotency_token"])
             and isinstance(attempt.get("input_hash"), str) and bool(attempt["input_hash"])
             and type(attempt.get("reconcile_checks")) is int
             and 1 <= attempt["reconcile_checks"] <= max_reconcile_checks
             and isinstance(native, dict) and attempt.get("native_job_ref") == native
             and isinstance(effect, dict) and effect.get("source") == "native-provider"
             and isinstance(effect.get("evidence_ref"), str) and bool(effect["evidence_ref"])
             and effect.get("attempt_token") == attempt["idempotency_token"]
             and effect.get("input_hash") == attempt["input_hash"]
             and effect.get("native_id") == native.get("native_id")
             and native.get("attempt_token") == attempt["idempotency_token"]
             and native.get("input_hash") == attempt["input_hash"]
             and isinstance(native.get("executor_uid"), str) and bool(native["executor_uid"])
             and isinstance(native.get("native_id"), str) and bool(native["native_id"]),
             "native session or effect differs from the committed attempt")
    readback = await driver.read_native(native)
    _require(isinstance(readback, dict)
             and readback.get("native_id") == native["native_id"]
             and readback.get("attempt_token") == attempt["idempotency_token"]
             and readback.get("input_hash") == attempt["input_hash"]
             and readback.get("state") in {"running", "completed"}
             and run.get("provider_state") == readback["state"],
             "native readback is unknown, stopped without proof or different")
    _require(isinstance(trace, list) and len(trace) == len(FAULT_ACTIONS),
             "fault trace is incomplete")
    previous = None
    references = {effect["evidence_ref"]}
    for number, (event, (fault, action)) in enumerate(zip(trace, FAULT_ACTIONS), start=1):
        _require(isinstance(event, dict) and event.get("fault") == fault
                 and event.get("action") == action and event.get("step") == number
                 and event.get("source") == "fault-injector"
                 and event.get("attempt_id") == attempt["attempt_id"]
                 and event.get("attempt_token") == attempt["idempotency_token"]
                 and event.get("input_hash") == attempt["input_hash"]
                 and isinstance(event.get("evidence_ref"), str)
                 and bool(event["evidence_ref"]) and event["evidence_ref"] not in references,
                 "fault trace is stale, unattributed or reuses evidence")
        references.add(event["evidence_ref"])
        observed_at = _timestamp(event.get("observed_at"))
        _require(previous is None or observed_at > previous, "fault trace is not ordered")
        previous = observed_at
        if fault == "lost_ack":
            _require(event.get("status") == "outcome_unknown"
                     and event.get("native_id") is None, "lost acknowledgement claimed success")
        elif fault == "stale":
            _require(event.get("status") == "denied" and event.get("native_id") is None,
                     "stale owner could read or replace a session")
        else:
            _require(event.get("native_id") == native["native_id"]
                     and event.get("status") == ("cancelled" if fault == "cancel" else "observed"),
                     "recovery changed native session or claimed unsupported success")
    return {"phase": "recovery-observations", "live_qualified": False,
            "attempt_id": attempt["attempt_id"], "native_id": native["native_id"],
            "effect_count": len(effects), "reconcile_checks": attempt["reconcile_checks"],
            "evidence_refs": sorted(references)}
