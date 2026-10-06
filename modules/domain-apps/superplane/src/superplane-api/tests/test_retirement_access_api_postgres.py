"""Real approval/admission and separate registration over supplied ownership facts.

Bootstrap completion and its historical ownership are fixture inputs. Human grants,
approval, budgets, operation records, source locking and control registration are
real PostgreSQL. This suite performs no provider calls or bootstrap execution.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from harness_jobs.identity import decode_payload, payload_digest
from harness_jobs.store import OperationStore
from workspace_provisioning.artifacts import canonical
from workspace_provisioning.retirement_inventory import (
    ComponentOwnership,
    RetirementInventory,
)
from workspace_provisioning.retirement_plan import compose_retirement_plan

from app import database
from app.adapters.operation_authority_source import GrantBackedAuthority
from app.models.workspace import Workspace
from app.services import onboarding, provisioning, retirement, retirement_access
from app.services.provisioning import ProvisioningRefused
from tests.test_lifecycle_api_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    lifecycle as lifecycle,
    pytestmark as pytestmark,
)


def test_incomplete_cleanup_access_is_not_mounted_or_allowlisted():
    from pathlib import Path
    from app.main import app
    from app.routers.retirement_access import router

    paths = {route.path for route in router.routes}
    assert paths

    def mounted_paths(routes):
        for route in routes:
            if hasattr(route, "path"):
                yield route.path
            else:
                yield from mounted_paths(route.original_router.routes)

    assert paths.isdisjoint(set(mounted_paths(app.routes)))
    gateway = (
        Path(__file__).resolve().parents[5]
        / "gateway/src/domain_proxy/superplane_routes.json"
    )
    assert paths.isdisjoint({path for _, path in json.loads(gateway.read_text())})


@pytest.fixture
async def cleanup(lifecycle, monkeypatch):  # noqa: F811
    fixture = lifecycle
    _, _, workspace = await fixture.prepare()
    monkeypatch.setattr(retirement_access, "async_session_factory", fixture.sessions)
    monkeypatch.setattr(retirement, "async_session_factory", fixture.sessions)
    # The registry is tested using the same declared table as the deployed API;
    # its migration/model parity is independently checked by the registry suite.
    from app.adapters import lifecycle_control_registry  # noqa: F401

    async with fixture.sessions() as db:
        await db.run_sync(
            lambda session: database.Base.metadata.tables[
                "workspace_lifecycle_control_operations"
            ].create(session.connection())
        )
        await db.commit()
    async with fixture.connections.connect() as connection:
        prepared = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1",
            workspace.provisioning_operation_id,
        )
    parameters = dict(decode_payload(prepared["request_payload"]).parameters)
    original = json.loads(parameters["lifecycle_request"])
    original.update(
        mode="bring-existing-cluster",
        existing_cluster_name="adopted",
        vpc_cidr=None,
        cluster_version=None,
        node_instance_type=None,
        availability_zones=[],
    )
    parameters.update(
        lifecycle_phase="bootstrap-workspace",
        lifecycle_request=canonical(original),
        lifecycle_artifact_id="a" * 64,
        allocation_id=str(uuid.uuid4()),
    )
    source_id = str(uuid.uuid4())
    await fixture.approve(
        {
            "approval_request": {
                "workspace_id": str(workspace.id),
                "action": "provision",
                "idempotency_key": source_id,
                "parameters": parameters,
            }
        }
    )
    with fixture.actor(workspace_id=workspace.id):
        source_progress = await provisioning.start_planned_provision(
            operation_id=source_id,
            workspace_id=str(workspace.id),
            org_id=str(fixture.org_id),
            parameters=parameters,
        )
    async with fixture.connections.connect() as connection:
        await connection.execute(
            "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
            source_progress.operation_id,
        )
    async with fixture.sessions() as db:
        current = await db.get(Workspace, workspace.id)
        current.status = "Active"
        current.provisioning_operation_id = source_progress.operation_id
        await db.commit()
    inventory = RetirementInventory(
        org_id=str(fixture.org_id),
        workspace_id=str(workspace.id),
        cluster_arn="arn:aws:eks:us-west-2:000000000002:cluster/adopted",
        cluster_ownership="adopted",
        namespace="superplane-system",
        namespace_uid="fixture-namespace-uid",
        remove_namespace=False,
        grants=(),
        prerequisites=(),
        components=(
            ComponentOwnership(
                {
                    "kind": "ServiceAccount",
                    "metadata": {"name": "owned", "namespace": "superplane-system"},
                },
                {"uid": "owned-uid", "digest": "b" * 64},
                True,
            ),
        ),
        components_complete=True,
    )

    async def facts(composition, db, org_id, workspace_id, *, access_review=False):
        assert access_review
        current = await db.get(Workspace, workspace_id)
        principal = await GrantBackedAuthority(fixture.sessions).resolve(
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            permission="workspace:provision",
        )
        async with fixture.connections.connect() as connection:
            source = await OperationStore().get(
                connection, principal, source_progress.operation_id
            )
        policy = onboarding.policy_for(org_id)
        return (
            current,
            principal,
            source,
            {},
            inventory,
            compose_retirement_plan(inventory),
            policy,
            policy.runtime,
        )

    monkeypatch.setattr(retirement_access, "retirement_facts", facts)

    async def preview(request_id):
        with fixture.actor(workspace_id=workspace.id):
            async with fixture.sessions() as db:
                return (
                    await retirement_access.preview_access(
                        fixture.composition,
                        db,
                        fixture.org_id,
                        workspace.id,
                        request_id,
                    )
                )[3]

    async def admit(request_id, review, approval_id):
        with fixture.actor(workspace_id=workspace.id):
            async with fixture.sessions() as db:
                return await retirement_access.admit_access(
                    fixture.composition,
                    db,
                    fixture.org_id,
                    workspace.id,
                    request_id,
                    review["revision"],
                    approval_id,
                )

    return SimpleNamespace(
        fixture=fixture,
        workspace_id=workspace.id,
        source_id=source_progress.operation_id,
        preview=preview,
        admit=admit,
    )


async def test_exact_human_approval_admits_one_zero_spend_control_and_keeps_bootstrap(
    cleanup,
):
    request_id = uuid.uuid4()
    review = await cleanup.preview(request_id)
    assert review["request_id"] != str(request_id)
    assert review["allocation_id"] != review["original_allocation_id"]
    assert "secrets" in review["authority"]["registrar"]["description"]
    approval_id = await cleanup.fixture.approve(review)
    result = await cleanup.admit(request_id, review, approval_id)
    assert await cleanup.admit(request_id, review, approval_id) == result
    async with cleanup.fixture.connections.connect() as connection:
        control = await connection.fetchrow(
            "SELECT * FROM workspace_lifecycle_control_operations WHERE operation_id=$1",
            result["control_operation_id"],
        )
        stored = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1",
            result["control_operation_id"],
        )
        reserve = await connection.fetchrow(
            "SELECT * FROM operation_budget_reservations WHERE job_id=$1 AND attempt_id=$2",
            stored["job_id"],
            stored["attempt_id"],
        )
    assert control["source_bootstrap_operation_id"] == cleanup.source_id
    assert control["plan_digest"] == payload_digest(
        decode_payload(stored["request_payload"])
    )
    assert reserve["max_cost_micros"] == reserve["max_resource_units"] == 0
    async with cleanup.fixture.sessions() as db:
        workspace = await db.get(Workspace, cleanup.workspace_id)
        assert workspace.provisioning_operation_id == cleanup.source_id
        assert workspace.status == "Active"
        assert workspace.teardown_operation_id is None


async def test_different_requests_cannot_purchase_competing_cleanup_access(cleanup):
    identities = [uuid.uuid4(), uuid.uuid4()]
    reviews = [await cleanup.preview(identity) for identity in identities]
    approvals = [await cleanup.fixture.approve(review) for review in reviews]
    results = await asyncio.gather(
        *(
            cleanup.admit(identity, review, approval)
            for identity, review, approval in zip(identities, reviews, approvals)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ProvisioningRefused) for result in results) == 1


async def test_lost_registration_recovers_original_admission_and_blocks_competitor(
    cleanup, monkeypatch
):
    from app.adapters import lifecycle_control_registry

    first, other = uuid.uuid4(), uuid.uuid4()
    review, alternative = await cleanup.preview(first), await cleanup.preview(other)
    approval, competitor = (
        await cleanup.fixture.approve(review),
        await cleanup.fixture.approve(alternative),
    )
    register = lifecycle_control_registry.register_control_operation

    async def lost(*args, **kwargs):
        raise RuntimeError("fixture: registry unavailable after shared admission")

    monkeypatch.setattr(lifecycle_control_registry, "register_control_operation", lost)
    with pytest.raises(RuntimeError, match="registry unavailable"):
        await cleanup.admit(first, review, approval)
    monkeypatch.setattr(
        lifecycle_control_registry, "register_control_operation", register
    )
    with pytest.raises(ProvisioningRefused):
        await cleanup.admit(other, alternative, competitor)
    result = await cleanup.admit(first, review, approval)
    assert result == await cleanup.admit(first, review, approval)


@pytest.mark.parametrize("change", ["revision", "approval", "policy"])
async def test_changed_review_or_approval_cannot_admit_access(cleanup, change):
    identity = uuid.uuid4()
    review = await cleanup.preview(identity)
    approval = await cleanup.fixture.approve(review)
    if change == "revision":
        review["revision"] = "f" * 64
    elif change == "approval":
        approval = uuid.uuid4()
    else:
        cleanup.fixture.document["tenants"][str(cleanup.fixture.org_id)]["runtime"][
            "actor_role_names"
        ]["registrar"] = "different-registrar"
        cleanup.fixture.config.write_text(canonical(cleanup.fixture.document))
    with pytest.raises(ProvisioningRefused):
        await cleanup.admit(identity, review, approval)
    async with cleanup.fixture.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM workspace_lifecycle_control_operations"
            )
            == 0
        )
