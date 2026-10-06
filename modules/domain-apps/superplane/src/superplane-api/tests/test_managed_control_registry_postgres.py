"""Paid managed control admission/registration/dispatch using real PostgreSQL.

Successful infrastructure and bootstrap results plus reviewed ownership are fixture
facts. Human approvals, paid admissions, seals, registry and outbox reads are real;
no provider call or full retirement execution is claimed.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from app import database
from app.adapters.operation_dispatch import OperationDispatcher
from app.models.workspace import Workspace
from app.operation_activation import expected_lifecycle_binding
from app.services import provisioning
from harness_jobs.identity import (
    OperationRequest,
    decode_payload,
    encode_payload,
    payload_digest,
)
from harness_jobs.store import _record

from tests.test_lifecycle_api_postgres import (
    installation_postgres_url,  # noqa: F401
    ledger,  # noqa: F401
    lifecycle,  # noqa: F401
    policy,
    pytestmark,  # noqa: F401
)
from tests.test_operation_dispatch_postgres import GatewayTransport
from workspace_provisioning.artifacts import digest
from workspace_provisioning.control_registry import register_control_operation
from workspace_provisioning.retirement_access_authority import access_request
from workspace_provisioning.retirement_access_plan import access_identity
from workspace_provisioning.retirement_managed_access import ManagedRetirementAccessPlan
from workspace_provisioning.runtime_config import LifecycleRefused


@pytest.fixture
async def managed_control(lifecycle):  # noqa: F811
    fixture = lifecycle
    _, _, workspace = await fixture.prepare()
    # Dispatch's ownership query includes both supported registry tables.
    from app.adapters import lifecycle_control_registry  # noqa: F401

    async with fixture.sessions() as db:
        await db.run_sync(
            lambda session: database.Base.metadata.create_all(
                session.connection(),
                tables=[
                    database.Base.metadata.tables[name]
                    for name in ("deployments", "controller_deployment_operations")
                ],
            )
        )
        await db.commit()
    org_id, workspace_id = str(fixture.org_id), str(workspace.id)
    async with fixture.connections.connect() as connection:
        initial = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1",
            workspace.provisioning_operation_id,
        )
    parameters = dict(decode_payload(initial["request_payload"]).parameters)

    async def admit(values, identifier, *, succeeded=False):
        await fixture.approve(
            {
                "approval_request": {
                    "workspace_id": workspace_id,
                    "action": "provision",
                    "idempotency_key": identifier,
                    "parameters": values,
                }
            }
        )
        with fixture.actor(workspace_id=workspace.id):
            progress = await provisioning.start_planned_provision(
                operation_id=identifier,
                workspace_id=workspace_id,
                org_id=org_id,
                parameters=values,
            )
        async with fixture.connections.connect() as connection:
            if succeeded:
                await connection.execute(
                    "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
                    progress.operation_id,
                )
            return _record(
                await connection.fetchrow(
                    "SELECT * FROM harness_operations WHERE operation_id=$1",
                    progress.operation_id,
                )
            )

    allocation_id = str(uuid.uuid4())
    apply = await admit(
        {
            **parameters,
            "lifecycle_phase": "apply-infrastructure",
            "allocation_id": allocation_id,
        },
        str(uuid.uuid4()),
        succeeded=True,
    )
    bootstrap = await admit(
        {
            **parameters,
            "lifecycle_phase": "bootstrap-workspace",
            "allocation_id": str(uuid.uuid4()),
            "lifecycle_source_operation_id": apply.operation_id,
            "lifecycle_artifact_id": "a" * 64,
        },
        str(uuid.uuid4()),
        succeeded=True,
    )
    async with fixture.connections.connect() as connection:
        await connection.execute(
            "INSERT INTO harness_allocation_seal "
            "(org_id,workspace_id,allocation_id,sealed_revision,operation_id,"
            "attempt_id,executor_id,fence_token) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            org_id,
            workspace_id,
            allocation_id,
            "reviewed-apply",
            apply.operation_id,
            apply.attempt_id,
            "original-worker",
            1,
        )
    async with fixture.sessions() as db:
        current = await db.get(Workspace, workspace.id)
        current.status = "Active"
        current.provisioning_operation_id = bootstrap.operation_id
        await db.commit()

    retirement_id = str(uuid.uuid4())
    request_id, control_allocation = access_identity(
        org_id, workspace_id, allocation_id, retirement_id
    )
    generation = "b" * 64
    cluster = "arn:aws:eks:us-west-2:000000000002:cluster/managed"
    plan = ManagedRetirementAccessPlan(
        request_id=request_id,
        allocation_id=control_allocation,
        original_allocation_id=allocation_id,
        retirement_request_id=retirement_id,
        org_id=org_id,
        workspace_id=workspace_id,
        cluster_arn=cluster,
        namespace_uid="original-namespace-uid",
        inventory_sha256="c" * 64,
        runtime_config_sha256=digest(policy()["runtime"]),
        generation=generation,
        registrar_namespaces=(),
        owned_objects=(),
        revocation_order=("cleaner-entry",),
        grants=(
            {
                "key": "cleaner-entry",
                "kind": "eks-entry",
                "actor": "cleaner",
                "cluster_arn": cluster,
                "generation": generation,
                "principal_arn": "arn:aws:iam::000000000002:role/installer",
                "groups": ["owned-cleanup"],
                "username": "retire:{{SessionName}}",
                "client_token": "d" * 64,
                "lifetime": "retirement",
            },
        ),
        retained_grants=(),
        cleanup_group="owned-cleanup",
        bootstrap_artifact_id="a" * 64,
    )
    proposed = access_request(plan, bootstrap, policy(), allocation_source=apply)
    control = await admit(dict(proposed.parameters), proposed.idempotency_key)
    identity = {
        "operation_id": control.operation_id,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "source_bootstrap_operation_id": bootstrap.operation_id,
        "request_id": retirement_id,
    }
    return SimpleNamespace(
        fixture=fixture,
        apply=apply,
        bootstrap=bootstrap,
        control=control,
        identity=identity,
        plan=plan,
    )


@pytest.mark.parametrize(
    "mutation", [None, "state", "approval", "seal", "payload", "shared", "apply_target"]
)
async def test_managed_registration_and_dispatch_recheck_original_paid_apply(
    managed_control, mutation
):
    case = managed_control
    connect = case.fixture.connections.connect
    async with connect() as shared, connect() as domain:
        registered = await register_control_operation(
            shared, domain_connection=domain, **case.identity
        )
    assert registered["original_allocation_id"] == case.plan.original_allocation_id
    assert registered["allocation_id"] != case.plan.original_allocation_id
    assert registered["source_bootstrap_operation_id"] == case.bootstrap.operation_id
    if mutation:
        async with connect() as connection:
            if mutation == "state":
                await connection.execute(
                    "UPDATE harness_operations SET state='failed' WHERE operation_id=$1",
                    case.apply.operation_id,
                )
            elif mutation == "approval":
                await connection.execute(
                    "DELETE FROM harness_approval_consumption WHERE operation_id=$1",
                    case.apply.operation_id,
                )
            elif mutation == "seal":
                await connection.execute(
                    "DELETE FROM harness_allocation_seal WHERE allocation_id=$1",
                    case.plan.original_allocation_id,
                )
            elif mutation in {"shared", "apply_target"}:
                source = case.bootstrap if mutation == "shared" else case.apply
                admitted = source.admitted_request()
                parameters = dict(admitted.parameters)
                if mutation == "shared":
                    public = json.loads(parameters["lifecycle_inputs"])
                    public["isolation_mode"] = "shared"
                    parameters["lifecycle_inputs"] = json.dumps(public)
                else:
                    parameters["aws_account_id"] = "000000000003"
                changed = OperationRequest(
                    admitted.action, admitted.idempotency_key, parameters
                )
                await connection.execute(
                    "UPDATE harness_operations SET request_payload=$2,plan_digest=$3 WHERE operation_id=$1",
                    source.operation_id,
                    encode_payload(changed),
                    payload_digest(changed),
                )
            else:
                await connection.execute(
                    "UPDATE harness_operations SET request_payload=replace(request_payload, 'apply-infrastructure', 'prepare-infrastructure') WHERE operation_id=$1",
                    case.apply.operation_id,
                )
        async with connect() as shared, connect() as domain:
            expected = {
                "shared": "dedicated managed",
                "apply_target": "managed apply changed",
            }.get(mutation)
            with pytest.raises((LifecycleRefused, ValueError), match=expected):
                await register_control_operation(
                    shared, domain_connection=domain, **case.identity
                )
    transport = GatewayTransport(expected_lifecycle_binding(), adp_org_id="adp-test")
    dispatcher = OperationDispatcher(
        connect,
        transport,
        domain_connect=connect,
        policy_for=lambda _: SimpleNamespace(adp_org_id="adp-test"),
    )
    async with connect() as connection:
        report = await dispatcher.outbox.drain_once(
            connection, dispatcher, operation_ids=(case.control.operation_id,)
        )
        error = await connection.fetchval(
            "SELECT last_error FROM harness_dispatch_outbox WHERE operation_id=$1",
            case.control.operation_id,
        )
    if mutation:
        assert report.delivered == 0
        assert not transport.calls
    else:
        assert report.delivered == 1, error
        assert len(transport.calls) == 1
        assert transport.calls[0][1]["operation_id"] == case.control.operation_id
