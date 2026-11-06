"""Synthetic recovery receipt controls; native effect counts still need live readback."""

from datetime import UTC, datetime, timedelta

import pytest

from l5_services.transform_recovery_probe import FAULT_ACTIONS, probe_recovery


SCOPE = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"}
NATIVE = {"native_id": "session-a", "executor_uid": "pod-a",
          "attempt_token": "token-a", "input_hash": "hash-a"}


class FixtureDriver:
    def __init__(self):
        self.run = {"run_id": "run-a", "scope": SCOPE, "state": "cancelled",
                    "provider_state": "running", "native_job_ref": NATIVE}
        self.attempts = [{"run_id": "run-a", "tenant_id": "tenant-a",
                          "operation": "transform_provider_submit", "attempt_id": "attempt-a",
                          "idempotency_token": "token-a", "input_hash": "hash-a",
                          "reconcile_checks": 2, "native_job_ref": NATIVE}]
        self.effects = [{"source": "native-provider", "evidence_ref": "private/effect-a",
                         "native_id": "session-a", "attempt_token": "token-a", "input_hash": "hash-a"}]
        self.native = {"native_id": "session-a", "attempt_token": "token-a",
                       "input_hash": "hash-a", "state": "running"}
        starting = datetime(2026, 1, 1, tzinfo=UTC)
        self.trace = [{"fault": fault, "action": action, "step": number,
                       "source": "fault-injector", "attempt_id": "attempt-a",
                       "attempt_token": "token-a", "input_hash": "hash-a",
                       "evidence_ref": f"private/fault-{number}",
                       "observed_at": (starting + timedelta(seconds=number)).isoformat(),
                       "status": "outcome_unknown" if fault == "lost_ack" else
                                 "denied" if fault == "stale" else
                                 "cancelled" if fault == "cancel" else "observed",
                       "native_id": None if fault in {"lost_ack", "stale"} else "session-a"}
                      for number, (fault, action) in enumerate(FAULT_ACTIONS, start=1)]
        self.calls = []

    async def read_run(self, run_id):
        self.calls.append("sql-run")
        return self.run

    async def read_attempts(self, run_id):
        self.calls.append("sql-attempt")
        return self.attempts

    async def read_fault_trace(self, run_id):
        self.calls.append("fault-trace")
        return self.trace

    async def read_native_effects(self, run_id):
        self.calls.append("provider-effects")
        return self.effects

    async def read_native(self, native):
        assert native == NATIVE
        self.calls.append("native-readback")
        return self.native


@pytest.mark.asyncio
async def test_lost_ack_restart_replay_stale_and_cancel_keep_one_native_effect():
    driver = FixtureDriver()
    result = await probe_recovery(driver, run_id="run-a", scope=SCOPE, max_reconcile_checks=3)
    assert result["live_qualified"] is False
    assert result["effect_count"] == 1 and result["native_id"] == "session-a"
    assert result["reconcile_checks"] == 2
    assert driver.calls == ["sql-run", "sql-attempt", "fault-trace", "provider-effects", "native-readback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "duplicate_effect", "replacement_attempt", "changed_native", "unknown_readback",
    "reconcile_exhausted", "lost_ack_claims_success", "stale_owner_read", "cancel_claims_stopped",
    "missing_fault", "out_of_order", "reused_receipt",
])
async def test_false_recovery_claims_are_refused(change):
    driver = FixtureDriver()
    if change == "duplicate_effect":
        driver.effects.append({**driver.effects[0], "evidence_ref": "private/effect-b"})
    elif change == "replacement_attempt":
        driver.attempts.append({**driver.attempts[0], "attempt_id": "replacement"})
    elif change == "changed_native":
        driver.native = {**driver.native, "native_id": "other-session"}
    elif change == "unknown_readback":
        driver.native = {**driver.native, "state": "unknown"}
    elif change == "reconcile_exhausted":
        driver.attempts[0]["reconcile_checks"] = 4
    elif change == "lost_ack_claims_success":
        driver.trace[0]["status"] = "observed"
    elif change == "stale_owner_read":
        driver.trace[3]["native_id"] = "session-a"
    elif change == "cancel_claims_stopped":
        driver.run["provider_state"] = "stopped"
    elif change == "missing_fault":
        driver.trace.pop()
    elif change == "out_of_order":
        driver.trace[2]["observed_at"] = driver.trace[1]["observed_at"]
    elif change == "reused_receipt":
        driver.trace[2]["evidence_ref"] = driver.trace[1]["evidence_ref"]
    with pytest.raises(ValueError):
        await probe_recovery(driver, run_id="run-a", scope=SCOPE, max_reconcile_checks=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_effect", "foreign_effect", "unknown_provider_state"])
async def test_native_effects_and_unknown_provider_outcomes_cannot_be_success(failure):
    driver = FixtureDriver()
    if failure == "missing_effect":
        driver.effects.clear()
    elif failure == "foreign_effect":
        driver.effects[0]["native_id"] = "another-session"
    else:
        driver.run["provider_state"] = "unknown"
        driver.native["state"] = "unknown"
    with pytest.raises(ValueError):
        await probe_recovery(driver, run_id="run-a", scope=SCOPE, max_reconcile_checks=3)
