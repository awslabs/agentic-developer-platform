"""Expired apply evidence needs genuine, freshly completed recovery to be reviewed."""

import pytest

# ruff: noqa: F811

from workspace_provisioning.artifacts import continuation_parameters, read_artifact
from workspace_provisioning.bootstrap_review import read_continuation_artifact
from workspace_provisioning.runtime import validate_phase
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_applied_recovery_postgres import interrupted_apply
from .test_lifecycle_runtime_postgres import harness as harness  # noqa: F401
from .postgres_bridge import requires_harness_postgres

pytestmark = requires_harness_postgres


@pytest.mark.parametrize(
    "change",
    [
        None,
        "old-proof",
        "not-succeeded",
        "wrong-producer",
        "wrong-fence",
        "wrong-recovery-actor",
        "wrong-recovery-attempt",
        "wrong-transition-time",
        "denied",
        "missing-proof",
    ],
)
def test_only_fresh_fenced_recovery_allows_original_bootstrap_review(
    harness,
    tmp_path,
    monkeypatch,
    change,  # noqa: F811
):
    async def run():
        scenario, _, applying, artifact, recovery, state = await interrupted_apply(
            harness, tmp_path, monkeypatch, observed=True
        )
        oid = applying.grant.lease.operation_id
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE workspace_lifecycle_artifacts SET created_at=now()-interval '2 hours' WHERE artifact_id=$1",
                artifact["artifact_id"],
            )
        scope = dict(
            artifact_id=artifact["artifact_id"],
            org_id=artifact["org_id"],
            workspace_id=artifact["workspace_id"],
        )
        original = await read_artifact(harness.connect, **scope, require_fresh=False)

        async def read():
            return await read_continuation_artifact(
                harness.connect, harness.connect, **scope
            )

        with pytest.raises(LifecycleRefused, match="expired"):
            await read()
        (result,) = await recovery.run(limit=1)
        assert result.action == "succeeded" and state.reads
        if change:
            sql = {
                "old-proof": "UPDATE harness_execution_audit SET recorded_at=now()-interval '2 hours' WHERE operation_id=$1 AND event='recovery.settled'",
                "not-succeeded": "UPDATE harness_operations SET state='unknown' WHERE operation_id=$1",
                "wrong-producer": "UPDATE harness_operations SET job_id='another-job' WHERE operation_id=$1",
                "wrong-fence": "UPDATE harness_operation_leases SET fence_token=fence_token+1 WHERE operation_id=$1",
                "wrong-recovery-actor": "UPDATE harness_operation_leases SET closed_holder='another-actor' WHERE operation_id=$1",
                "wrong-recovery-attempt": "UPDATE harness_operation_leases SET closed_attempt_id='another-attempt' WHERE operation_id=$1",
                "wrong-transition-time": "UPDATE harness_operations SET updated_at=updated_at-interval '1 second' WHERE operation_id=$1",
                "denied": "UPDATE harness_execution_audit SET allowed=false WHERE operation_id=$1 AND event='recovery.settled'",
                "missing-proof": "DELETE FROM harness_execution_audit WHERE operation_id=$1 AND event='recovery.settled'",
            }[change]
            async with harness.connect() as connection:
                await connection.execute(sql, oid)
            with pytest.raises(LifecycleRefused, match="expired"):
                await read()
        else:
            assert await read() == original
            bootstrap = await scenario.admit(continuation_parameters(original))
            *_, source, step = await validate_phase(bootstrap, scenario.context)
            assert source == original and step.step_id == "bootstrap-workspace"
        assert (
            await read_artifact(harness.connect, **scope, require_fresh=False)
            == original
        )
        assert not scenario.bootstrap_calls

    harness.run(run())
