"""Real preview, human approval, shared admission and continuation registration.

Only authenticated request context and deployment policy are supplied by the test.
The API routes, grants, approval service, shared facade/outbox and domain budget
ledger use real PostgreSQL. These tests run in remote CI, never against a cloud.
"""

import asyncio
import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.facade import OperationFacadeService
from harness_jobs.identity import OperationRequest, decode_payload
from harness_jobs.schema import apply
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.runtime_config import LifecycleRefused

from app import database
from app.adapters.harness_operation_facade import HarnessOperationFacade
from app.adapters.operation_dispatch import OperationDispatcher
from app.adapters.operation_authority_source import (
    ActingPrincipal,
    GrantBackedAuthority,
    reset_acting_principal,
    set_acting_principal,
)
from app.config import settings
from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.controller_deployment import ControllerDeploymentOperation
from app.models.deployment import Deployment
from app.models.lifecycle import WorkspaceLifecycleArtifact
from app.models.operation_approval import OperationApproval
from app.models.organization import Organization
from app.models.organization_grant import OrganizationGrantRecord
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.routers.workspaces import create_workspace
from app.schemas.workspace import CreateWorkspaceRequest
from app.services import lifecycle_proposals, onboarding, provisioning
from app.services.operation_approvals import ApprovalService
from tests.test_operation_budget_ledger_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    postgres_available,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


def policy():
    return {
        "adp_org_id": "adp-test",
        "aws_organization_id": "o-fixture1234",
        "management_account_id": "000000000001",
        "management_cluster": "management",
        "permitted_modes": ["managed", "adopt"],
        "permitted_target_accounts": ["000000000002"],
        "permitted_regions": ["us-west-2"],
        "isolation_modes": ["dedicated"],
        "workspace_defaults": {
            "vpc_cidr": "10.64.0.0/16",
            "availability_zones": ["us-west-2a", "us-west-2b"],
            "cluster_version": "1.31",
        },
        "operation_max_runtime_seconds": 900,
        "credential_references": {
            "000000000002": {
                "credential_id": "cred-fixture",
                "credential_service": "aws",
                "credential_label": "fixture",
            }
        },
        "runtime": {
            "version": 1,
            "backend": {
                "bucket": "fixture-state",
                "region": "us-west-2",
                "lock_table": "fixture-lock",
            },
            "environment": "dev",
            "workspace_variables": {},
            "actor_role_names": {
                "registrar": "registrar",
                "installer": "installer",
                "supervisor": "supervisor",
            },
            "namespace": "superplane-system",
            "enforce_version": "v1.31",
            "management_security_group_id": "sg-a123",
            "management_api_origin": "https://management.example.invalid",
            "bootstrap_credential_reference_id": "cred-fixture",
            "binaries": {
                key: "/opt/bin/" + key
                for key in ("python", "terraform", "kubectl", "aws")
            },
            "controller_image": "fixture/controller@sha256:" + "a" * 64,
            "imds_probe_image": "fixture/probe@sha256:" + "b" * 64,
        },
    }


@pytest.fixture
async def lifecycle(ledger, installation_postgres_url, monkeypatch, tmp_path):  # noqa: F811
    budget, connections, _ = ledger
    async with connections.connect() as connection:
        await apply(connection)
        schema = await connection.fetchval("SELECT current_schema()")
    engine = create_async_engine(
        installation_postgres_url,
        connect_args={
            "server_settings": {"search_path": schema, "statement_timeout": "10000"}
        },
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            database.Base.metadata.create_all,
            tables=[
                model.__table__
                for model in (
                    Organization,
                    CloudAccount,
                    Cluster,
                    Workspace,
                    Deployment,
                    ControllerDeploymentOperation,
                    OrganizationGrantRecord,
                    WorkspaceGrantRecord,
                    OperationApproval,
                    WorkspaceLifecycleArtifact,
                )
            ],
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    for module in (database, onboarding, lifecycle_proposals):
        monkeypatch.setattr(module, "async_session_factory", sessions)
    authority = GrantBackedAuthority(sessions)
    org_id = uuid.uuid4()
    binding = {
        "producer_registry_id": "producer",
        "worker_registry_id": "worker",
        "worker_namespace": "domain-system",
        "worker_service_account": "paid-worker",
        "worker_role_arn": "arn:aws:iam::123456789012:role/paid-worker",
        "worker_image_digest": "sha256:" + "a" * 64,
        "operation_schema": "superplane",
        "queue_arn": "arn:aws:sqs:us-east-1:123456789012:paid-operations",
    }
    binding_file = tmp_path / "worker-binding.json"
    binding_file.write_text(json.dumps(binding))
    monkeypatch.setattr(settings, "superplane_paid_worker_mode", "native-lifecycle")
    monkeypatch.setattr(
        settings, "superplane_paid_worker_binding_file", str(binding_file)
    )
    monkeypatch.setattr(
        settings, "superplane_operation_gateway_url", "https://gateway.example"
    )
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", True)

    async def binding_proof(route, payload):
        assert (route, payload) == (
            "/binding-proof",
            {"domain": "superplane", "org_id": str(org_id)},
        )
        return {
            "version": 1,
            "installed": True,
            "checked_at": datetime.now(UTC).isoformat(),
            "domain": "superplane",
            "org_id": str(org_id),
            "adp_org_id": "adp-test",
            **binding,
        }

    dispatcher = OperationDispatcher(
        connections.connect,
        SimpleNamespace(post=binding_proof),
        policy_for=lambda _: SimpleNamespace(adp_org_id="adp-test"),
    )
    facade = HarnessOperationFacade(
        OperationFacadeService(
            connect=connections.connect,
            resolver=authority,
            approvals=authority,
            ledger=budget,
        ),
        lifecycle_verify=dispatcher.binding_ready,
    )
    monkeypatch.setattr(provisioning, "_facade", facade)
    document = {"version": 1, "tenants": {str(org_id): policy()}}
    config = tmp_path / "lifecycle-policy.json"
    config.write_text(canonical(document))
    monkeypatch.setattr(settings, "superplane_lifecycle_config_file", str(config))
    async with sessions() as session:
        session.add(
            Organization(
                id=org_id,
                name="lifecycle",
                adp_org_id="adp-test",
                billing_plan="enterprise",
            )
        )
        await session.flush()
        session.add(
            CloudAccount(
                id=uuid.uuid4(),
                org_id=org_id,
                provider="aws",
                account_identifier="000000000002",
                friendly_name="target",
                provisioning_mode="customer_onboarded",
                status="Active",
                adp_credential_ids_json='["cred-fixture"]',
            )
        )
        session.add_all(
            [
                OrganizationGrantRecord(
                    org_id=org_id,
                    principal=subject,
                    principal_type="human",
                    permissions="organization:administer",
                    granted_by="test",
                )
                for subject in ("requester", "approver")
            ]
        )
        await session.commit()

    class Context:
        composition = SimpleNamespace(operation_connect=connections.connect)

        @contextmanager
        def actor(self, subject="requester", workspace_id=""):
            token = set_acting_principal(
                ActingPrincipal(subject, str(org_id), str(workspace_id))
            )
            try:
                yield
            finally:
                reset_acting_principal(token)

        async def approve(self, review):
            values = review["approval_request"]
            with self.actor(workspace_id=values["workspace_id"]):
                ticket = await ApprovalService(sessions).issue(
                    workspace_id=values["workspace_id"],
                    request=OperationRequest(
                        values["action"],
                        values["idempotency_key"],
                        values["parameters"],
                    ),
                )
            with self.actor("approver", values["workspace_id"]):
                await ApprovalService(sessions).decide(
                    ticket["approval_id"], "allowed-once"
                )
            return uuid.UUID(ticket["approval_id"])

        async def prepare(self):
            body = CreateWorkspaceRequest(
                name="managed",
                mode="managed",
                account="target",
                region="us-west-2",
                isolation_mode="dedicated",
                operation_id=uuid.uuid4(),
                budget_max_gpus=2,
                budget_max_daily_usd="3.00",
            )
            with self.actor():
                async with sessions() as session:
                    review = await onboarding.preview(session, org_id, body)
            approval_id = await self.approve(review)
            body = body.model_copy(
                update={"approval_id": approval_id, "plan_revision": review["revision"]}
            )
            with self.actor():
                async with sessions() as session:
                    result = await create_workspace(body, org_id, session)
            async with sessions() as session:
                session.add(
                    WorkspaceGrantRecord(
                        org_id=org_id,
                        workspace_id=result.id,
                        principal="approver",
                        principal_type="human",
                        permissions="workspace:administer",
                    )
                )
                await session.commit()
            return body, review, result

        async def artifact(self, result):
            async with connections.connect() as connection:
                source = await connection.fetchrow(
                    "SELECT * FROM harness_operations WHERE operation_id=$1",
                    result.provisioning_operation_id,
                )
                await connection.execute(
                    "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
                    result.provisioning_operation_id,
                )
            params = dict(decode_payload(source["request_payload"]).parameters)
            row = dict(
                org_id=str(org_id),
                workspace_id=str(result.id),
                source_operation_id=source["operation_id"],
                source_job_id=source["job_id"],
                source_attempt_id=source["attempt_id"],
                source_payload_digest=source["plan_digest"],
                source_request_payload=source["request_payload"],
                producer_holder="worker#1",
                producer_attempt_id="worker-attempt",
                producer_fence_token=1,
                request_revision=params["plan_revision"],
                account_id="000000000002",
                target_json=canonical({"account_id": "000000000002"}),
                parameters_json=canonical(params),
                artifact_metadata_json=canonical(
                    {
                        "next_phase": "apply-infrastructure",
                        "plan_file_sha256": "c" * 64,
                        "plan_json_sha256": "d" * 64,
                        "inventory": [],
                        "estimate": None,
                    }
                ),
            )
            row["artifact_id"] = digest(row)
            async with sessions() as session:
                session.add(WorkspaceLifecycleArtifact(**row))
                await session.commit()
            return row["artifact_id"]

        async def review(self, workspace_id, artifact_id, request_id):
            with self.actor(workspace_id=workspace_id):
                async with sessions() as session:
                    *_, review = await lifecycle_proposals.preview_continuation(
                        self.composition,
                        session,
                        org_id,
                        workspace_id,
                        artifact_id,
                        request_id,
                    )
                    return review

        async def continue_(self, workspace_id, artifact_id, request_id, approval_id):
            with self.actor(workspace_id=workspace_id):
                async with sessions() as session:
                    return await lifecycle_proposals.continue_lifecycle(
                        self.composition,
                        session,
                        org_id,
                        workspace_id,
                        artifact_id,
                        request_id,
                        approval_id,
                    )

    context = Context()
    context.sessions, context.connections, context.org_id = (
        sessions,
        connections,
        org_id,
    )
    context.config, context.document = config, document
    try:
        yield context
    finally:
        await engine.dispose()


async def test_real_preview_approval_admission_registers_once_and_does_not_claim_ready(
    lifecycle,
):
    body, review, result = await lifecycle.prepare()
    assert result.status == "Provisioning"
    assert review["approval_request"]["parameters"]["max_cost_micros"] == "0"
    with lifecycle.actor():
        async with lifecycle.sessions() as session:
            replay = await create_workspace(body, lifecycle.org_id, session)
    assert replay.id == result.id
    async with lifecycle.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations"
            )
            == 1
        )
        assert await connection.fetchval("SELECT count(*) FROM workspaces") == 1


async def test_continuation_replay_recovers_registration_after_approval_and_artifact_expire(
    lifecycle,
):
    _, _, result = await lifecycle.prepare()
    artifact_id = await lifecycle.artifact(result)
    request_id = uuid.uuid4()
    review = await lifecycle.review(result.id, artifact_id, request_id)
    assert review["approval_request"]["parameters"]["max_cost_micros"] == "3000000"
    approval_id = await lifecycle.approve(review)
    first = await lifecycle.continue_(result.id, artifact_id, request_id, approval_id)
    async with lifecycle.connections.connect() as connection:
        await connection.execute(
            "UPDATE workspaces SET provisioning_operation_id=$1 WHERE id=$2",
            result.provisioning_operation_id,
            result.id,
        )
        await connection.execute(
            "UPDATE workspace_lifecycle_artifacts SET created_at=now()-interval '2 hours'"
        )
        await connection.execute(
            "UPDATE operation_approvals SET expires_at=now()-interval '1 hour'"
        )
    replay = await lifecycle.continue_(result.id, artifact_id, request_id, approval_id)
    assert replay["provisioning_operation_id"] == first["provisioning_operation_id"]
    async with lifecycle.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 2
        assert (
            await connection.fetchval(
                "SELECT provisioning_operation_id FROM workspaces"
            )
            == first["provisioning_operation_id"]
        )


async def test_competing_continuations_only_admit_one_allocation(lifecycle):
    _, _, result = await lifecycle.prepare()
    artifact_id = await lifecycle.artifact(result)
    ids = [uuid.uuid4(), uuid.uuid4()]
    approvals = [
        await lifecycle.approve(
            await lifecycle.review(result.id, artifact_id, request_id)
        )
        for request_id in ids
    ]
    results = await asyncio.gather(
        *(
            lifecycle.continue_(result.id, artifact_id, request_id, approval_id)
            for request_id, approval_id in zip(ids, approvals)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in results) == 1
    assert (
        sum(isinstance(value, provisioning.ProvisioningRefused) for value in results)
        == 1
    )
    async with lifecycle.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 2
        assert (
            await connection.fetchval(
                "SELECT sum(max_cost_micros) FROM operation_budget_reservations"
            )
            == 3000000
        )


@pytest.mark.parametrize(
    "change", ["runtime", "credential", "artifact", "expired", "source"]
)
async def test_changed_artifact_policy_or_original_admission_never_admits(
    lifecycle, change
):
    _, _, result = await lifecycle.prepare()
    artifact_id = await lifecycle.artifact(result)
    if change in {"runtime", "credential"}:
        policy = lifecycle.document["tenants"][str(lifecycle.org_id)]
        if change == "runtime":
            policy["runtime"]["actor_role_names"]["installer"] = "other-installer"
        else:
            policy["credential_references"]["000000000002"]["credential_id"] = (
                "other-credential"
            )
        lifecycle.config.write_text(canonical(lifecycle.document))
    else:
        sql = {
            "artifact": "UPDATE workspace_lifecycle_artifacts SET target_json='{}'",
            "expired": "UPDATE workspace_lifecycle_artifacts SET created_at=now()-interval '2 hours'",
            "source": "UPDATE harness_operations SET attempt_id='another-original-attempt'",
        }[change]
        async with lifecycle.connections.connect() as connection:
            await connection.execute(sql)
    with pytest.raises((LifecycleRefused, provisioning.ProvisioningRefused)):
        await lifecycle.review(result.id, artifact_id, uuid.uuid4())
    async with lifecycle.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1


async def test_lost_workspace_commit_cannot_admit_a_different_paid_successor(
    lifecycle, monkeypatch
):
    _, _, result = await lifecycle.prepare()
    artifact_id = await lifecycle.artifact(result)
    original_id, competitor_id = uuid.uuid4(), uuid.uuid4()
    original_approval = await lifecycle.approve(
        await lifecycle.review(result.id, artifact_id, original_id)
    )
    competitor_approval = await lifecycle.approve(
        await lifecycle.review(result.id, artifact_id, competitor_id)
    )
    with lifecycle.actor(workspace_id=result.id):
        async with lifecycle.sessions() as session:

            async def lost_registration():
                raise RuntimeError("lost workspace commit")

            monkeypatch.setattr(session, "commit", lost_registration)
            with pytest.raises(RuntimeError, match="lost workspace commit"):
                await lifecycle_proposals.continue_lifecycle(
                    lifecycle.composition,
                    session,
                    lifecycle.org_id,
                    result.id,
                    artifact_id,
                    original_id,
                    original_approval,
                )
    with pytest.raises(
        provisioning.ProvisioningRefused, match="already has an admitted continuation"
    ):
        await lifecycle.continue_(
            result.id, artifact_id, competitor_id, competitor_approval
        )
    recovered = await lifecycle.continue_(
        result.id, artifact_id, original_id, original_approval
    )
    async with lifecycle.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 2
        assert (
            await connection.fetchval(
                "SELECT sum(max_cost_micros) FROM operation_budget_reservations"
            )
            == 3000000
        )
        assert (
            await connection.fetchval(
                "SELECT provisioning_operation_id FROM workspaces"
            )
            == recovered["provisioning_operation_id"]
        )


async def test_supplied_managed_networking_is_refused_before_approval_or_admission(
    lifecycle,
):
    current = lifecycle.document["tenants"][str(lifecycle.org_id)]
    current["runtime"]["workspace_variables"]["networking_mode"] = "supplied"
    lifecycle.config.write_text(canonical(lifecycle.document))
    with pytest.raises(provisioning.ProvisioningUnavailable, match="not executable"):
        await lifecycle.prepare()
    async with lifecycle.connections.connect() as connection:
        for table in (
            "workspaces",
            "harness_operations",
            "operation_budget_reservations",
            "operation_approvals",
        ):
            assert await connection.fetchval("SELECT count(*) FROM " + table) == 0


@pytest.mark.parametrize(
    "network,allowed,expected",
    [
        ("owned", ["managed", "adopt", "new-account-managed"], ["adopt", "managed"]),
        ("supplied", ["managed", "adopt"], ["adopt"]),
        ("supplied", ["managed"], []),
    ],
)
async def test_capabilities_only_advertise_executable_policy_modes(
    lifecycle, network, allowed, expected
):
    from app.routers.onboarding import onboarding_capabilities

    current = lifecycle.document["tenants"][str(lifecycle.org_id)]
    current["runtime"]["workspace_variables"]["networking_mode"] = network
    current["permitted_modes"] = allowed
    lifecycle.config.write_text(canonical(lifecycle.document))
    dispatcher = SimpleNamespace(ready=AsyncMock(return_value=True))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                trust_composition=SimpleNamespace(dispatcher=dispatcher)
            )
        )
    )
    report = await onboarding_capabilities(request, lifecycle.org_id)
    assert report["modes"] == expected
    assert report["ready"] is bool(expected)
    # Provider connection identity is independent of workspace execution modes.
    # Lifecycle readiness gates only the create/adopt contracts.
    features = set(report["features"])
    lifecycle_features = {"create-operation-id-v1", "adopt-operation-id-v1"}
    assert features & lifecycle_features == (lifecycle_features if expected else set())
    assert "provider-connection-operation-id-v1" in features
