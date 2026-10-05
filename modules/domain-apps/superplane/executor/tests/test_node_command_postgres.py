"""Real shared allocation/lease and separate domain journal with simulated AWS I/O."""

import ast
import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
from harness_jobs import OperationStore
from harness_jobs.execution import record_intent
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import OperationRequest, OperationRefused
from harness_jobs.inventory import AllocationResource, InventoryAuthority
from harness_jobs.leases import acquire
from superplane_executor.authority import VerifiedOperation
from superplane_executor.node_command import execute
from superplane_executor.node_command_journal import Journal
from superplane_executor.node_command_plan import DOCUMENTS, canonical, digest
from superplane_executor.provider import Provider
from superplane_executor.workspace import Workspace
from test_node_runners import manifest
from tests.conftest import admit_paid, requires_postgres
from tests.test_admission_postgres import principal

pytestmark = requires_postgres


@pytest.fixture
async def native_command(pool, postgres_server):
    domain_schema = "native_" + uuid4().hex
    admin = await asyncpg.connect(postgres_server)
    await admin.execute(f'CREATE SCHEMA "{domain_schema}"')
    domain = await asyncpg.create_pool(
        postgres_server,
        min_size=1,
        max_size=6,
        server_settings={"search_path": domain_schema},
    )
    migration = (
        Path(__file__).resolve().parents[2]
        / "src/superplane-api/alembic/versions/039_controller_node_commands.py"
    )
    tree = ast.parse(migration.read_text())
    upgrade = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
    )
    async with domain.acquire() as c:
        # Match SQLAlchemy/asyncpg's prepared-statement boundary: a raw
        # Connection.execute without arguments silently accepts multiple SQL
        # statements, unlike the production Alembic transport.
        for statement in upgrade.body:
            sql = ast.literal_eval(statement.value.args[0])
            await (await c.prepare(sql)).fetch()
    actor = principal()
    request = OperationRequest(
        action="provision",
        idempotency_key=uuid4().hex,
        parameters={
            "allocation_id": "native-allocation",
            "execution_steps": canonical(
                [
                    {
                        "step_id": "1",
                        "provider": "aws",
                        "operation_kind": "launch",
                        "target": "original",
                    },
                    {
                        "step_id": "2",
                        "provider": "aws",
                        "operation_kind": "run-node-bootstrap",
                        "target": "original",
                    },
                    {
                        "step_id": "3",
                        "provider": "aws",
                        "operation_kind": "deploy",
                        "target": "original",
                    },
                ]
            ),
        },
    )
    async with pool.acquire() as c:
        admitted = await admit_paid(OperationStore(), c, actor, request)
        lease = await acquire(
            c,
            operation_id=admitted.record.operation_id,
            holder=actor.subject,
            attempt_id="native-attempt",
        )
    grant = ExecutionGrant(actor, lease)
    operation = VerifiedOperation(
        grant,
        admitted.record.job_id,
        admitted.record.plan_digest,
        admitted.record.request_payload,
        "confirmed",
        4,
        3600,
        5000000,
    )
    state = SimpleNamespace(
        sends=0,
        fail_send=False,
        revoke_after_send=False,
        pending_until=None,
        last_contract=None,
        invocation_change={},
        empty_instances=False,
    )
    instance = {
        "InstanceId": "i-11111111111111111",
        "ImageId": "ami-11111111",
        "Placement": {"AvailabilityZone": "us-east-1a"},
        "State": {"Name": "running"},
        "IamInstanceProfile": {
            "Arn": "arn:aws:iam::123456789012:instance-profile/worker"
        },
    }
    runtime = manifest()
    instance["Tags"] = [
        {"Key": "superplane-org", "Value": lease.org_id},
        {"Key": "superplane-workspace", "Value": lease.workspace_id},
    ]
    plan = SimpleNamespace(
        cleanup_graph=None,
        node_bootstrap={
            "version": 1,
            "runtime_manifest": runtime,
            "bootstrap_wrapper_sha256": "c" * 64,
            "probe_wrapper_sha256": "d" * 64,
        },
        data={
            "provider_account_id": "123456789012",
            "node_count": 1,
            "workload": {"command": ["/app/run"], "name": "batch"},
            "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/approved",
            "endpoint": "https://eks.example",
            "certificate_authority": "cHVibGljLWNh",
        },
        network={"cluster": {"network": {"vpc_cidr": "10.0.0.0/16"}}},
        cluster_region="us-east-1",
        cloud_cluster_name="original",
        region_bindings=[
            {
                "region": "us-east-1",
                "image_id": "ami-11111111",
                "instance_profile": "worker",
            }
        ],
    )
    plan.resource_reference = (
        lambda kind, value, region: f"arn:aws:ec2:{region}:123456789012:{kind}/{value}"
    )
    plan.node_config = lambda op: {
        "apiVersion": "node.eks.aws/v1alpha1",
        "kind": "NodeConfig",
        "spec": {"cluster": {}, "kubelet": {}},
    }
    # This suite validates transport/journalling; fixed-wrapper config validation
    # and actual Plan generation have their own tests, not a simulated success claim.
    command_id = str(uuid4())

    class AWS:
        def client(self, service, *, region_name, **kwargs):
            if service == "ssm":
                assert kwargs["config"].retries["total_max_attempts"] == 1
            return self

        def get_caller_identity(self):
            return {
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/worker/session",
            }

        def get_paginator(self, name):
            assert name == "describe_instances"
            return self

        def paginate(self, **kwargs):
            return [{"Reservations": [{"Instances": [deepcopy(instance)]}]}]

        def describe_instances(self, **kwargs):
            return {
                "Reservations": [
                    {"Instances": [] if state.empty_instances else [deepcopy(instance)]}
                ]
            }

        def describe_images(self, **kwargs):
            return {
                "Images": [
                    {
                        "ImageId": "ami-11111111",
                        "OwnerId": "123456789012",
                        "State": "available",
                        "Architecture": "x86_64",
                        "Tags": [
                            {
                                "Key": "superplane-node-runtime-sha256",
                                "Value": runtime["artifact_sha256"],
                            }
                        ],
                    }
                ]
            }

        def describe_document(self, *, Name, DocumentVersion):
            name = Name.split("/")[-1]
            value = next(v for v in DOCUMENTS.values() if v[0] == name)
            return {
                "Document": {
                    "Name": name,
                    "Owner": "123456789012",
                    "DocumentVersion": "1",
                    "DocumentType": "Command",
                    "Status": "Active",
                    "HashType": "Sha256",
                    "Hash": value[1],
                }
            }

        def get_document(self, *, Name, **kwargs):
            name = Name.split("/")[-1]
            plugin = next(v[2] for v in DOCUMENTS.values() if v[0] == name)
            path = (
                Path(__file__).resolve().parents[1]
                / "node-command"
                / (plugin + "-document.json")
            )
            return {
                "Name": name,
                "DocumentVersion": "1",
                "DocumentType": "Command",
                "Content": path.read_text(),
            }

        def describe_instance_information(self, **kwargs):
            return {
                "InstanceInformationList": [
                    {
                        "InstanceId": instance["InstanceId"],
                        "PingStatus": "Online",
                        "PlatformType": "Linux",
                        "AgentVersion": runtime["ssm_agent_version"],
                    }
                ]
            }

        def send_command(self, **kwargs):
            state.sends += 1
            state.last_contract = json.loads(
                base64.b64decode(kwargs["Parameters"]["Contract"][0])
            )
            if state.fail_send:
                raise TimeoutError("accepted reply lost")
            return {"Command": {"CommandId": command_id}}

        def get_command_invocation(self, **kwargs):
            result = {
                "CommandId": command_id,
                "InstanceId": instance["InstanceId"],
                "DocumentName": DOCUMENTS["node-bootstrap"][0],
                "DocumentVersion": "1",
                "PluginName": "bootstrap",
                "Status": "Success",
                "StatusDetails": "Success",
                "ResponseCode": 0,
                "StandardErrorContent": "",
            }
            contract = state.last_contract
            receipt = {
                k: contract[k]
                for k in (
                    "version",
                    "purpose",
                    "nonce",
                    "instance_id",
                    "account_id",
                    "region",
                    "availability_zone",
                    "image_id",
                )
            }
            receipt.update(
                contract_sha256=digest(contract), status="succeeded", probe=None
            )
            result["StandardOutputContent"] = canonical(receipt)
            result.update(state.invocation_change)
            return result

    aws = AWS()

    async def delivery_role(op):
        return {"role_arn": "arn:aws:iam::123456789012:role/worker"}

    async def authenticate(token):
        return grant

    async def authorize():
        async with pool.acquire() as c:
            live = await c.fetchval(
                "SELECT 1 FROM harness_operation_leases WHERE operation_id=$1 AND holder=$2 AND attempt_id=$3 AND fence_token=$4 AND closed_at IS NULL AND expires_at>clock_timestamp() AND runtime_deadline>clock_timestamp()",
                lease.operation_id,
                lease.holder,
                lease.attempt_id,
                lease.fence_token,
            )
            if not live:
                raise OperationRefused("native lease revoked")

    provider = Provider(
        sky=None,
        workspace=Workspace("/unused", "https://management.example"),
        domain_pool=domain,
        execution_pool=pool,
        session=aws,
    )
    provider.registry = SimpleNamespace(
        authority=SimpleNamespace(delivery_role=delivery_role),
        authenticate=authenticate,
    )
    steps = admitted_steps(admitted.record)
    async with pool.acquire() as c:
        launch = await record_intent(
            c,
            lease,
            idempotency_key=step_key(admitted.record, steps[0]),
            provider="aws",
            operation_kind="launch",
            target="original",
        )
        call = await record_intent(
            c,
            lease,
            idempotency_key=step_key(admitted.record, steps[1]),
            provider="aws",
            operation_kind="run-node-bootstrap",
            target="original",
        )
        ref = plan.resource_reference("instance", instance["InstanceId"], "us-east-1")
        inventory = InventoryAuthority(connect=pool.acquire, authenticate=authenticate)
        await inventory.enumerate_resources(
            c,
            lease,
            resources=(
                AllocationResource(
                    ref, "aws", ref, "instance", frozenset({launch.idempotency_key})
                ),
            ),
        )
    fixture = SimpleNamespace(
        provider=provider,
        operation=operation,
        plan=plan,
        call=call,
        authorize=authorize,
        domain=domain,
        pool=pool,
        state=state,
        instance=instance,
        record=admitted.record,
    )
    try:
        yield fixture
    finally:
        await domain.close()
        await admin.execute(f'DROP SCHEMA "{domain_schema}" CASCADE')
        await admin.close()


async def run(f):
    return await execute(f.provider, f.operation, {}, f.plan, f.call, f.authorize)


async def test_native_command_registers_real_dependency_before_dispatch(native_command):
    f = native_command
    reference = await run(f)
    assert f.state.sends == 1
    async with f.domain.acquire() as c:
        row = await c.fetchrow("SELECT * FROM controller_node_commands")
        assert row["state"] == "succeeded" and row["command_id"]
    async with f.pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT provider_reference FROM harness_allocation_resource WHERE kind='node_command'"
            )
            == reference
        )
    assert await run(f) == reference
    assert f.state.sends == 1


async def test_lost_response_never_retries_send_across_execution_restart(
    native_command,
):
    f = native_command
    f.state.fail_send = True
    with pytest.raises(TimeoutError):
        await run(f)
    f.state.fail_send = False
    with pytest.raises(OperationRefused, match="never resubmit"):
        await run(f)
    assert f.state.sends == 1
    async with f.domain.acquire() as c:
        assert (
            await c.fetchval("SELECT command_id FROM controller_node_commands") is None
        )


async def test_same_tag_replacement_cannot_receive_original_bootstrap(native_command):
    f = native_command
    f.instance["InstanceId"] = "i-22222222222222222"
    with pytest.raises(OperationRefused, match="original launch membership"):
        await run(f)
    assert f.state.sends == 0


async def test_command_handle_retained_after_fence_revocation(
    native_command, monkeypatch
):
    f = native_command
    original = Journal.remember_handle

    async def late(c, row, handle):
        async with f.pool.acquire() as shared:
            await shared.execute(
                "UPDATE harness_operation_leases SET fence_token=fence_token+1 WHERE operation_id=$1",
                f.operation.grant.lease.operation_id,
            )
        return await original(c, row, handle)

    monkeypatch.setattr(Journal, "remember_handle", staticmethod(late))
    with pytest.raises(OperationRefused, match="revoked"):
        await run(f)
    async with f.domain.acquire() as c:
        row = await c.fetchrow("SELECT * FROM controller_node_commands")
        assert (
            row["command_id"] and row["state"] == "accepted" and row["result"] is None
        )


@pytest.mark.parametrize(
    "change",
    [
        {"InstanceId": "i-22222222222222222"},
        {"StandardOutputContent": ""},
        {"StatusDetails": "Failed"},
        {"PluginName": "unexpected"},
    ],
)
async def test_invocation_mismatch_retains_command_without_receipt(
    native_command, change
):
    f = native_command
    f.state.invocation_change = change
    with pytest.raises(OperationRefused):
        await run(f)
    async with f.domain.acquire() as c:
        row = await c.fetchrow("SELECT * FROM controller_node_commands")
    assert row["command_id"] and row["result"] is None
    assert row["state"] == "accepted" and f.state.sends == 1


async def test_interrupted_dependency_registration_can_resume_without_early_send(
    native_command, monkeypatch
):
    f = native_command
    original = InventoryAuthority.enumerate_resources

    async def unavailable(*args, **kwargs):
        raise ConnectionError("shared membership unavailable")

    monkeypatch.setattr(InventoryAuthority, "enumerate_resources", unavailable)
    with pytest.raises(ConnectionError):
        await run(f)
    assert f.state.sends == 0
    async with f.domain.acquire() as c:
        assert (
            await c.fetchval("SELECT state FROM controller_node_commands") == "prepared"
        )
    monkeypatch.setattr(InventoryAuthority, "enumerate_resources", original)
    await run(f)
    assert f.state.sends == 1


async def test_split_pool_inventory_binds_original_plan_and_contract(native_command):
    from superplane_executor.node_command_inventory import discover, rows

    f = native_command
    reference = await run(f)
    resources = await discover(f.provider, f.operation)
    assert set(resources) == {reference}
    assert resources[reference].kind == "node_command"
    async with f.domain.acquire() as c:
        await c.execute(
            "UPDATE controller_node_commands SET contract_sha256=$1", "0" * 64
        )
    with pytest.raises(OperationRefused, match="contract binding"):
        await rows(f.provider, f.operation)


async def test_unapproved_step_key_cannot_bind_command_inventory(native_command):
    from superplane_executor.node_command_inventory import rows

    f = native_command
    await run(f)
    async with f.domain.acquire() as c:
        await c.execute("UPDATE controller_node_commands SET step_key='unapproved'")
    with pytest.raises(OperationRefused, match="inventory binding"):
        await rows(f.provider, f.operation)


@pytest.mark.parametrize("missing_handle", [False, True])
async def test_teardown_tracks_original_command_until_positive_termination(
    native_command, missing_handle
):
    from harness_jobs.inventory import ResourcePresence
    from superplane_executor.node_command_inventory import discover, observe

    f = native_command
    f.state.fail_send = missing_handle
    if missing_handle:
        with pytest.raises(TimeoutError):
            await run(f)
    else:
        await run(f)
    teardown = OperationRequest(
        action="teardown",
        idempotency_key=uuid4().hex,
        parameters={
            "allocation_id": "native-allocation",
            "controller_source_operation_id": f.operation.grant.lease.operation_id,
        },
    )
    async with f.pool.acquire() as c:
        admitted = await admit_paid(
            OperationStore(), c, f.operation.grant.principal, teardown
        )
    # A distinct admitted teardown identity resolves the original source graph;
    # this test supplies the protected observation callback, not execution rights.
    operation = replace(
        f.operation,
        plan_digest=admitted.record.plan_digest,
        request_payload=admitted.record.request_payload,
        grant=replace(
            f.operation.grant,
            lease=replace(
                f.operation.grant.lease, operation_id=admitted.record.operation_id
            ),
        ),
    )
    f.provider.node_observation_authorize = f.authorize
    resources = await discover(f.provider, operation)
    assert len(resources) == 1
    resource = next(iter(resources.values()))
    assert (
        await observe(f.provider, operation, f.plan, resource)
    ).presence is ResourcePresence.PRESENT
    f.state.empty_instances = True
    assert (
        await observe(f.provider, operation, f.plan, resource)
    ).presence is ResourcePresence.PRESENT
    f.state.empty_instances = False
    f.instance["State"]["Name"] = "terminated"
    assert (
        await observe(f.provider, operation, f.plan, resource)
    ).presence is ResourcePresence.ABSENT


async def test_missing_probe_receipt_refuses_completion(native_command):
    from superplane_executor.node_command_inventory import require_completed

    f = native_command
    await run(f)
    with pytest.raises(OperationRefused, match="incomplete"):
        await require_completed(f.provider, f.operation, f.plan, f.authorize)


async def test_production_provider_dispatches_admitted_native_step(
    native_command, monkeypatch
):
    from harness_jobs.execution import CallOutcome
    from superplane_executor.plan import Plan

    f = native_command
    remembered = []

    async def verify(*args, **kwargs):
        await f.authorize()
        return f.operation, {}, None, None

    async def cloud(*args):
        return None  # SkyPilot cloud configuration belongs to launch, already recorded.

    async def remember(call, plan, request_id):
        remembered.append(request_id)

    f.provider.registry.verify = verify
    f.provider.registry.check_handoff = lambda *args: None
    monkeypatch.setattr(Plan, "read", lambda *args: f.plan)
    monkeypatch.setattr(f.provider, "cloud", cloud)
    monkeypatch.setattr(f.provider, "remember", remember)
    outcome, _, reference = await f.provider(f.call)
    assert outcome is CallOutcome.SUCCEEDED
    assert reference.startswith("superplane-node-command:")
    assert remembered == ["native-command:run-node-bootstrap"]
    assert f.state.sends == 1


async def test_batch_result_capture_refuses_missing_native_receipts_before_workspace_read(
    native_command,
):
    from superplane_executor.results import capture

    f = native_command
    with pytest.raises(OperationRefused, match="incomplete"):
        await capture(
            f.provider, f.operation, {"namespace": "workspace"}, f.plan, [], f.authorize
        )


async def test_production_deploy_refuses_missing_receipts_before_workspace_mutation(
    native_command, monkeypatch
):
    from harness_jobs.execution import CallOutcome
    from superplane_executor.plan import Plan

    f = native_command

    async def verify(*args, **kwargs):
        await f.authorize()
        return f.operation, {}, None, None

    async def cloud(*args):
        return None

    async def forbidden_apply(*args, **kwargs):
        pytest.fail("workspace mutation preceded complete original native receipts")

    f.provider.registry.verify = verify
    f.provider.registry.check_handoff = lambda *args: None
    monkeypatch.setattr(Plan, "read", lambda *args: f.plan)
    monkeypatch.setattr(f.provider, "cloud", cloud)
    monkeypatch.setattr(f.provider.workspace, "apply", forbidden_apply)
    descriptor = admitted_steps(f.record)[2]
    async with f.pool.acquire() as c:
        call = await record_intent(
            c,
            f.operation.grant.lease,
            idempotency_key=step_key(f.record, descriptor),
            provider="aws",
            operation_kind="deploy",
            target="original",
        )
    outcome, _, _ = await f.provider(call)
    assert outcome is CallOutcome.UNKNOWN
    assert f.state.sends == 0


async def test_concurrent_original_command_claim_cannot_dispatch(native_command):
    from superplane_executor.node_command import contract_for

    f = native_command
    instances = await f.provider.instances(f.operation, f.plan)
    contract = contract_for(f.operation, f.plan, instances[0], "node-bootstrap")
    journal = Journal(f.provider, f.operation, f.call, f.authorize)
    async with journal.locked(contract):
        with pytest.raises(OperationRefused, match="already in progress"):
            async with journal.locked(contract):
                pytest.fail("concurrent original command was admitted")
    assert f.state.sends == 0
    await run(f)
    assert f.state.sends == 1


async def test_migration_rollback_preserves_native_evidence(native_command):
    f = native_command
    await run(f)
    migration = (
        Path(__file__).resolve().parents[2]
        / "src/superplane-api/alembic/versions/039_controller_node_commands.py"
    )
    tree = ast.parse(migration.read_text())
    downgrade = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
    )
    async with f.domain.acquire() as c:
        with pytest.raises(asyncpg.RaiseError, match="evidence must be preserved"):
            async with c.transaction():
                for statement in downgrade.body[:2]:
                    await c.execute(ast.literal_eval(statement.value.args[0]))
        assert await c.fetchval("SELECT count(*) FROM controller_node_commands") == 1
