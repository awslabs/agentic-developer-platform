"""Real shared leases and PostgreSQL effect intents across lost acknowledgements."""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest

from harness_jobs import OperationFacadeService, OperationStore, REQUIRED_PERMISSION
from harness_jobs.execution import OperationExecutor
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import decode_payload

from account_factory.modes import OwnershipMode
from workspace_provisioning.artifacts import canonical, digest, initial_execution_steps
from workspace_provisioning.effects import LifecycleEffects
from workspace_provisioning.lifecycle_policy import policy_digest
from workspace_provisioning.preview import preview_workspace
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_policy import policy
from .test_preview import request_for, authority
from .test_retirement_execution_postgres import (
    _Approves,
    _Ledger,
    _Resolver,
    _principal,
)
from .postgres_bridge import Harness, requires_harness_postgres

pytestmark = requires_harness_postgres


def effects_ddl(migration="020_lifecycle_effects.py"):
    path = (
        Path(__file__).resolve().parents[2]
        / "src/superplane-api/alembic/versions"
        / migration
    )
    spec = importlib.util.spec_from_file_location("lifecycle_effect_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module.upgrade()
    return output.getvalue()


@pytest.fixture
def harness(tmp_path_factory, request):
    with Harness.started(tmp_path_factory, request.node.name) as value:

        async def migrate():
            async with value.connect() as connection:
                await connection.execute(effects_ddl())

        value.run(migrate())
        yield value


async def setup(
    harness, *, record_intent=True, mode=OwnershipMode.EXISTING_ACCOUNT_MANAGED
):
    deployment = policy()
    deployment.update(
        aws_organization_id="o-testorg1234", management_cluster="fixture-management"
    )
    request = replace(request_for(mode), workspace_id="ws-1")
    authorization = replace(
        authority(),
        workspace_id="ws-1",
        operation_org_id="org-a",
        permitted_modes=frozenset(
            {
                OwnershipMode.EXISTING_ACCOUNT_MANAGED,
                OwnershipMode.BRING_EXISTING_CLUSTER,
            }
        ),
        permitted_organizational_units=frozenset(),
    )
    public = {"isolation_mode": "namespace"}
    capacity = {
        "max_resource_units": 1,
        "max_runtime_seconds": 900,
        "max_cost_micros": 3000000,
        "request": public,
        "allocation_id": "fixture-allocation",
        "policy_revision": policy_digest(deployment),
    }
    revision = preview_workspace(
        request,
        authorization=authorization,
        requested_capacity=capacity,
        cost_estimate=None,
        approval_required=True,
    ).revision
    parameters = {
        "plan_revision": revision,
        "workspace_name": "fixture",
        "lifecycle_request": canonical(asdict(request)),
        "lifecycle_inputs": canonical(public),
        "runtime_config_sha256": digest(deployment["runtime"]),
        "lifecycle_policy_sha256": policy_digest(deployment),
        "allocation_id": "fixture-allocation",
        "max_resource_units": "1",
        "max_runtime_seconds": "900",
        "max_cost_micros": "3000000",
        "lifecycle_allocation_max_resource_units": "1",
        "lifecycle_allocation_max_runtime_seconds": "900",
        "lifecycle_allocation_max_cost_micros": "3000000",
        "idempotency_key": "fixture-lifecycle",
    }
    parameters["execution_steps"] = initial_execution_steps(parameters)
    facade = OperationFacadeService(
        connect=harness.connect,
        resolver=_Resolver(_principal()),
        approvals=_Approves(),
        ledger=_Ledger(),
    )
    progress = await facade.open_operation(
        action="provision",
        workspace_id="ws-1",
        org_id="org-a",
        permission=REQUIRED_PERMISSION,
        parameters=parameters,
    )
    lease = await harness.lease(progress.operation_id)
    async with harness.connect() as connection:
        record = await OperationStore().get(
            connection, _principal(), progress.operation_id
        )
    operation = SimpleNamespace(
        grant=ExecutionGrant(_principal(lease.holder), lease),
        job_id=record.job_id,
        plan_digest=record.plan_digest,
        request_payload=record.request_payload,
        request=decode_payload(record.request_payload),
        reservation_state="confirmed",
    )

    class Gateway:
        async def resolve(self, operation_id):
            assert operation_id == lease.operation_id
            return operation

    context = SimpleNamespace(
        connect=harness.connect,
        domain_connect=harness.connect,
        authority=Gateway(),
        policy=deployment,
        policy_fixture=True,
    )

    async def impossible(_):
        raise AssertionError("this test never dispatches a cloud call")

    executor = OperationExecutor(
        lease, connect=harness.connect, provider_call=impossible
    )
    step = admitted_steps(record)[0]
    if record_intent:
        await executor.record_intent(
            idempotency_key=step_key(record, step),
            provider=step.provider,
            operation_kind=step.operation_kind,
            target=step.target,
        )
    recipe = {
        "example-role": {
            "service": "iam",
            "method": "create_role",
            "account_id": "000000000002",
            "arguments": {"RoleName": "approved", "AssumeRolePolicyDocument": "{}"},
        }
    }
    return operation, context, recipe


def test_commit_before_mutation_and_ambiguous_intent_never_replays(harness):
    async def scenario():
        operation, context, recipe = await setup(harness)
        journal = LifecycleEffects(
            operation, context, phase="prepare-infrastructure", recipe=recipe
        )
        assert await journal.intend("example-role", recipe["example-role"]) is None
        async with harness.connect() as reader:
            assert (
                await reader.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_effects WHERE event='intended'"
                )
                == 1
            )
        with pytest.raises(LifecycleRefused, match="ambiguous"):
            await journal.intend("example-role", recipe["example-role"])
        with pytest.raises(LifecycleRefused, match="unresolved"):
            await journal.complete()
        await journal.confirm(
            "example-role", recipe["example-role"], {"RoleId": "actual-role-id"}
        )
        assert await journal.intend("example-role", recipe["example-role"]) == {
            "RoleId": "actual-role-id"
        }

    harness.run(scenario())


@pytest.mark.parametrize("change", ["descriptor", "recipe", "policy", "child-account"])
def test_changed_effect_policy_or_unproven_target_is_refused(harness, change):
    async def scenario():
        operation, context, recipe = await setup(harness)
        journal = LifecycleEffects(
            operation, context, phase="prepare-infrastructure", recipe=recipe
        )
        await journal.intend("example-role", recipe["example-role"])
        descriptor = deepcopy(recipe["example-role"])
        if change == "descriptor":
            descriptor["arguments"]["RoleName"] = "different"
        elif change == "recipe":
            changed = deepcopy(recipe)
            changed["example-role"]["arguments"]["RoleName"] = "different"
            journal = LifecycleEffects(
                operation, context, phase="prepare-infrastructure", recipe=changed
            )
            descriptor = changed["example-role"]
        elif change == "child-account":
            descriptor["account_id"] = "000000000003"
        else:
            context.policy["runtime"]["actor_role_names"]["installer"] = "different"
        with pytest.raises(LifecycleRefused):
            await journal.confirm("example-role", descriptor, {"RoleId": "invented"})

    harness.run(scenario())


def test_stale_lease_after_awaited_provider_call_cannot_confirm(harness):
    async def scenario():
        operation, context, recipe = await setup(harness)
        journal = LifecycleEffects(
            operation, context, phase="prepare-infrastructure", recipe=recipe
        )
        await journal.intend("example-role", recipe["example-role"])
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                operation.grant.lease.operation_id,
            )
        with pytest.raises(LifecycleRefused, match="lease"):
            await journal.confirm(
                "example-role", recipe["example-role"], {"RoleId": "actual"}
            )
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_effects WHERE event='confirmed'"
                )
                == 0
            )

    harness.run(scenario())


def test_lost_intent_write_ack_never_reaches_provider_or_reissues(harness):
    async def scenario():
        operation, context, recipe = await setup(harness)

        @asynccontextmanager
        async def lost_ack():
            async with harness.connect() as connection:

                class Connection:
                    def __getattr__(self, name):
                        return getattr(connection, name)

                    async def execute(self, sql, *args):
                        await connection.execute(sql, *args)
                        raise ConnectionError("lost committed write acknowledgement")

                yield Connection()

        context.domain_connect = lost_ack
        journal = LifecycleEffects(
            operation, context, phase="prepare-infrastructure", recipe=recipe
        )
        with pytest.raises(ConnectionError):
            await journal.intend("example-role", recipe["example-role"])
        context.domain_connect = harness.connect
        with pytest.raises(LifecycleRefused, match="ambiguous"):
            await journal.intend("example-role", recipe["example-role"])

    harness.run(scenario())


@pytest.mark.parametrize("partial", [False, True])
def test_empty_or_partial_recipe_cannot_complete_phase(harness, partial):
    async def scenario():
        operation, context, recipe = await setup(harness)
        recipe["required-second-role"] = deepcopy(recipe["example-role"])
        recipe["required-second-role"]["arguments"]["RoleName"] = "required-second"
        journal = LifecycleEffects(
            operation, context, phase="prepare-infrastructure", recipe=recipe
        )
        if partial:
            await journal.intend("example-role", recipe["example-role"])
            await journal.confirm(
                "example-role", recipe["example-role"], {"RoleId": "actual"}
            )
        with pytest.raises(LifecycleRefused, match="missing required"):
            await journal.complete()

    harness.run(scenario())
