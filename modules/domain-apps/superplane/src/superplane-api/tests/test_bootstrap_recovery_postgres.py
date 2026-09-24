"""Canonical result recovery and partial effects use real remote PostgreSQL."""

from uuid import UUID
from builtins import BaseExceptionGroup
from copy import deepcopy

import pytest

# This import declares the shared fixture's checkout root before loading source
# test transports that deliberately are not part of deployed runtime wheels.
from .test_account_creation_recovery_postgres import (
    account_harness as canonical_harness,
    next_sweep,
    recovery_composition,
)
from workspace_provisioning import account_runtime, bootstrap_runtime, runtime
from workspace_provisioning.artifacts import continuation_parameters
from workspace_provisioning.tests.test_account_bootstrap_postgres import Child
from workspace_provisioning.tests.test_account_canonical_postgres import (
    CanonicalScenario,
    canonical_account_chain,
    install_infrastructure,
)

account_harness = canonical_harness


class ProcessCrash(BaseException):
    pass


async def recover_partial(scenario, original, tmp_path, monkeypatch, phase):
    lease = original.grant.lease
    async with scenario.harness.connect() as connection:
        await connection.execute(
            "INSERT INTO workspaces(id,org_id,name,isolation_mode,is_default,status,provisioning_operation_id) "
            "VALUES($1,$2,'interrupted-workspace','dedicated',false,'pending',$3) "
            "ON CONFLICT(id) DO UPDATE SET provisioning_operation_id=EXCLUDED.provisioning_operation_id",
            UUID(lease.workspace_id),
            UUID(lease.org_id),
            lease.operation_id,
        )
    await next_sweep(scenario, original)
    async with recovery_composition(
        scenario, original, tmp_path, monkeypatch, observation_path="bootstrap"
    ) as (recovery, _, reads, delivered):
        for _ in range(3):
            result = await recovery.run(limit=1)
            if result[0].action != "deferred":
                break
            await next_sweep(scenario, original)
        assert result[0].action == "unknown"
        assert not reads and len(delivered) == 1
        accounting = delivered[0]["accounting"]
        assert (
            accounting["allocation_id"] == original.request.parameters["allocation_id"]
        )
        assert accounting["budget"] == "retain"
        assert accounting["release_permitted"] is False
        assert accounting["may_mark_released"] is False
        assert accounting["inventory_complete"] is False
        assert "lifecycle_proposal" not in accounting
        partial = accounting["partial_lifecycle"]
        assert partial["phase"] == phase
        assert partial["workflow_complete"] is False
        assert partial["cleanup_authorized"] is False
        assert partial["provider_absence_verified"] is False
    async with scenario.harness.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_call_intent WHERE operation_id=$1",
                lease.operation_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1",
                lease.operation_id,
            )
            == 0
        )
    return partial


def test_lost_apply_reply_retains_original_allocation_without_another_apply(
    account_harness, tmp_path, monkeypatch
):
    scenario = CanonicalScenario(account_harness, tmp_path, monkeypatch)
    install_infrastructure(scenario)

    async def run():
        _, source = await scenario.created()
        account = await scenario.admit(continuation_parameters(source))
        result = await account_runtime.run_account_bootstrap(account, scenario.context)
        source = await scenario.row(result["artifact_id"])
        prepare = await scenario.admit(continuation_parameters(source))
        result = await account_runtime.run_account_infrastructure(
            prepare, scenario.context
        )
        source = await scenario.row(result["artifact_id"])
        original = await scenario.admit(continuation_parameters(source))
        scenario.lose_apply_reply = True
        with pytest.raises(Exception):
            await account_runtime.run_account_infrastructure(original, scenario.context)
        calls = list(scenario.process_calls)
        assert sum("apply_workspace_plan.py" in call[1] for call in calls) == 1
        mutations = deepcopy(scenario.child.mutations)
        partial = await recover_partial(
            scenario, original, tmp_path, monkeypatch, "apply-infrastructure"
        )
        assert partial["source_artifact_id"] == source["artifact_id"]
        assert scenario.process_calls == calls
        assert scenario.child.mutations == mutations
        assert not scenario.bootstrap_calls
        assert len(scenario.accounts) == 1

    account_harness.run(run())


def test_bootstrap_worker_crash_retains_outstanding_authority_without_revocation(
    account_harness, tmp_path, monkeypatch
):
    real_bootstrap = bootstrap_runtime.bootstrap
    scenario = CanonicalScenario(account_harness, tmp_path, monkeypatch)
    outputs = install_infrastructure(scenario)

    async def run():
        interrupted = {}

        async def before_final(operation):
            interrupted["operation"] = operation

            def crash(**kwargs):
                raise ProcessCrash("worker died after component journal commit")

            monkeypatch.setattr(
                "superplane_bootstrap.workspace.prepare_system_workloads", crash
            )

        with pytest.raises(BaseExceptionGroup):
            await canonical_account_chain(
                scenario,
                outputs,
                tmp_path,
                monkeypatch,
                real_bootstrap,
                before_final=before_final,
            )
        original = interrupted["operation"]
        async with account_harness.connect() as connection:
            authority = await connection.fetch(
                "SELECT * FROM workspace_bootstrap_authority ORDER BY generation"
            )
            effects = await connection.fetch(
                "SELECT * FROM workspace_lifecycle_effects ORDER BY operation_id,effect_key,event"
            )
        assert authority and any(not row["revoked"] for row in authority)
        cloud = scenario.cloud
        cloud_before = deepcopy(
            (cloud.entries, cloud.policies, cloud.objects, cloud.events)
        )
        calls = list(scenario.process_calls)
        mutations = deepcopy(scenario.child.mutations)
        partial = await recover_partial(
            scenario, original, tmp_path, monkeypatch, "bootstrap-workspace"
        )
        assert partial["confirmed_effect_keys"]
        assert any(not row["revoked"] for row in partial["authority_generations"])
        assert (
            cloud.entries,
            cloud.policies,
            cloud.objects,
            cloud.events,
        ) == cloud_before
        assert scenario.process_calls == calls
        assert scenario.child.mutations == mutations
        async with account_harness.connect() as connection:
            assert (
                await connection.fetch(
                    "SELECT * FROM workspace_bootstrap_authority ORDER BY generation"
                )
                == authority
            )
            assert (
                await connection.fetch(
                    "SELECT * FROM workspace_lifecycle_effects ORDER BY operation_id,effect_key,event"
                )
                == effects
            )

    account_harness.run(run())


@pytest.mark.parametrize("change", [None, "outstanding-grant", "missing-output"])
def test_completed_canonical_bootstrap_recovery_never_replays_mutations(
    account_harness, tmp_path, monkeypatch, change
):
    real_bootstrap = bootstrap_runtime.bootstrap
    scenario = CanonicalScenario(account_harness, tmp_path, monkeypatch)
    outputs = install_infrastructure(scenario)

    async def run():
        interrupted = {}
        record_artifact = runtime.record_artifact

        async def before_final(operation):
            interrupted["operation"] = operation

            async def lose_reply(*args, **kwargs):
                if change != "missing-output":
                    interrupted["result"] = await record_artifact(*args, **kwargs)
                raise ProcessCrash(
                    "bootstrap completed; worker died before shared reply"
                )

            monkeypatch.setattr(runtime, "record_artifact", lose_reply)

        with pytest.raises(BaseExceptionGroup):
            await canonical_account_chain(
                scenario,
                outputs,
                tmp_path,
                monkeypatch,
                real_bootstrap,
                before_final=before_final,
            )
        original = interrupted["operation"]
        lease = original.grant.lease
        async with account_harness.connect() as connection:
            await connection.execute(
                "UPDATE workspaces SET provisioning_operation_id=$1 WHERE id=$2",
                lease.operation_id,
                UUID(lease.workspace_id),
            )
            if change == "outstanding-grant":
                await connection.execute(
                    "UPDATE workspace_bootstrap_authority SET revoked=false WHERE operation_id=$1",
                    lease.operation_id,
                )
            before = await connection.fetch(
                "SELECT * FROM workspace_bootstrap_authority ORDER BY generation"
            )
        creates, sdk_mutations = len(scenario.creates), list(scenario.child.mutations)
        await next_sweep(scenario, original)
        async with recovery_composition(
            scenario, original, tmp_path, monkeypatch, observation_path="bootstrap"
        ) as (recovery, _, reads, delivered):
            for _ in range(3 if change == "missing-output" else 1):
                result = await recovery.run(limit=1)
                if result[0].action != "deferred":
                    break
                await next_sweep(scenario, original)
            if change is None:
                assert result[0].action == "succeeded" and len(delivered) == 1
                recovered = delivered[0]["accounting"]["lifecycle_proposal"]
                assert (
                    recovered == interrupted["result"]
                    and recovered["status"] == "ready"
                )
            elif change == "missing-output":
                assert result[0].action == "unknown" and len(delivered) == 1
                facts = delivered[0]["accounting"]["partial_lifecycle"]
                assert facts["workflow_complete"] is False
                assert facts["cleanup_authorized"] is False
                assert facts["authority_generations"]
                assert "lifecycle_proposal" not in delivered[0]["accounting"]
            else:
                assert result[0].action == "deferred" and not delivered
            assert not reads
        assert (
            len(scenario.creates) == creates
            and scenario.child.mutations == sdk_mutations
        )
        async with account_harness.connect() as connection:
            assert (
                await connection.fetch(
                    "SELECT * FROM workspace_bootstrap_authority ORDER BY generation"
                )
                == before
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent WHERE operation_id=$1",
                    lease.operation_id,
                )
                == 1
            )

    account_harness.run(run())


def test_partial_account_bootstrap_retains_uncertain_effect_without_replay(
    account_harness, tmp_path, monkeypatch
):
    scenario = CanonicalScenario(account_harness, tmp_path, monkeypatch)

    async def run():
        _, source = await scenario.created()
        scenario.child = Child(scenario)
        original_create = scenario.child.create_role

        def lost_role(**arguments):
            original_create(**arguments)
            raise OSError("IAM role created but reply lost")

        scenario.child.create_role = lost_role
        original = await scenario.admit(continuation_parameters(source))
        with pytest.raises(Exception):
            await account_runtime.run_account_bootstrap(original, scenario.context)
        lease = original.grant.lease
        async with account_harness.connect() as connection:
            await connection.execute(
                "INSERT INTO workspaces(id,org_id,name,isolation_mode,is_default,status,provisioning_operation_id) "
                "VALUES($1,$2,'partial-account','dedicated',false,'pending',$3)",
                UUID(lease.workspace_id),
                UUID(lease.org_id),
                lease.operation_id,
            )
            rows = await connection.fetch(
                "SELECT * FROM workspace_lifecycle_effects WHERE operation_id=$1 ORDER BY effect_key,event",
                lease.operation_id,
            )
        mutations = list(scenario.child.mutations)
        await next_sweep(scenario, original)
        async with recovery_composition(scenario, original, tmp_path, monkeypatch) as (
            recovery,
            _,
            reads,
            delivered,
        ):
            for _ in range(3):
                result = await recovery.run(limit=1)
                if result[0].action != "deferred":
                    break
                await next_sweep(scenario, original)
            assert result[0].action == "unknown"
            assert not reads and len(delivered) == 1
            inventory = delivered[0]["accounting"]["partial_lifecycle"]
            assert (
                inventory["uncertain_effect_keys"]
                and inventory["confirmed_effect_keys"]
            )
            assert (
                inventory["workflow_complete"] is False
                and inventory["cleanup_authorized"] is False
            )
        assert scenario.child.mutations == mutations
        async with account_harness.connect() as connection:
            assert (
                await connection.fetch(
                    "SELECT * FROM workspace_lifecycle_effects WHERE operation_id=$1 ORDER BY effect_key,event",
                    lease.operation_id,
                )
                == rows
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1",
                    lease.operation_id,
                )
                == 0
            )

    account_harness.run(run())
