"""Authenticated handle HTTP writes through real paid admission and live leases.

Only Gateway's external verified-run response is transported offline. Submitter
authentication, workspace lookup, both adapters and persistence remain real.
"""

import json
import os
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from harness_jobs import OperationRequest, OperationStore, ResolvedPrincipal
from harness_jobs.admission import admit_operation
from harness_jobs.approval import (
    ApprovalBinding,
    ApprovalRecord,
    ApprovalResult,
    ApproverStatus,
)
from harness_jobs.leases import acquire
from harness_jobs.schema import apply
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from superplane_contracts import Submitter

from app.adapters.harness_execution_authority import HarnessExecutionAuthority
from app.adapters.harness_provider_authority import HarnessProviderAuthority
from app.adapters.operation_authority_source import (
    ActingPrincipal,
    acting_principal,
    reset_acting_principal,
    set_acting_principal,
)
from app.database import Base, get_session
from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.organization import Organization
from app.models.provider_handle import (
    ProviderAllocation,
    ProviderAllocationResource,
    ProviderOperation,
    ProviderReferenceConflict,
)
from app.models.workspace import Workspace
from app.routers.provider_handles import router
from app.services import observations, provider_authority
from tests.test_operation_budget_ledger_postgres import (
    envelope,
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    postgres_available,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def handles(ledger, installation_postgres_url, monkeypatch):  # noqa: F811
    budget, connections, _ = ledger
    org, workspace = uuid.uuid4(), uuid.uuid4()
    subject = "actual-invocation#1"
    async with connections.connect() as connection:
        await apply(connection)
        schema = await connection.fetchval("SELECT current_schema()")
    engine = create_async_engine(
        installation_postgres_url,
        connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                model.__table__
                for model in (
                    Organization,
                    CloudAccount,
                    Cluster,
                    Workspace,
                    ProviderAllocation,
                    ProviderOperation,
                    ProviderReferenceConflict,
                    ProviderAllocationResource,
                )
            ],
        )
    async with sessions() as session:
        session.add(Organization(id=org, name="org", adp_org_id="adp-original"))
        await session.flush()
        session.add(
            Workspace(id=workspace, org_id=org, name="ws", isolation_mode="dedicated")
        )
        await session.commit()
    body = {
        "workspace": str(workspace),
        "allocation_id": "allocation",
        "operation": "provision",
        "provider": "aws",
        "resource_name": "node",
        "idempotency_key": "provider-call",
    }
    actor = ResolvedPrincipal(
        str(org), str(workspace), "human", frozenset({"workspace:provision"})
    )
    records = {}

    async def admit(*, missing=None):
        parameters = {
            key: value
            for key, value in body.items()
            if key not in {"workspace", "operation_authority", missing}
        }
        request = OperationRequest(
            action="provision", idempotency_key=str(uuid.uuid4()), parameters=parameters
        )
        now = datetime.now(UTC)
        approval = ApprovalRecord(
            approval_id=str(uuid.uuid4()),
            binding=ApprovalBinding.for_request(actor, request),
            envelope=envelope(),
            result=ApprovalResult.ALLOWED_ONCE,
            approvers=frozenset({"reviewer"}),
            decided_by="reviewer",
            decided_at=now,
            expires_at=now + timedelta(hours=1),
        )
        async with connections.connect() as connection:
            admitted = await admit_operation(
                connection,
                OperationStore(),
                budget,
                principal=actor,
                request=request,
                approval=approval,
                requested_envelope=envelope(),
                approver_statuses={
                    "reviewer": ApproverStatus(
                        "reviewer", True, frozenset({"workspace:administer"})
                    )
                },
                now=now,
            )
            record = admitted.operation.record
            lease = await acquire(
                connection,
                operation_id=record.operation_id,
                holder=subject,
                attempt_id=subject,
            )
        records[record.operation_id] = (record, lease)
        return record.operation_id

    operation_id = await admit()
    body["operation_authority"] = operation_id
    state = SimpleNamespace(revoked=False, altered={}, calls=[])

    async def verify_run(**identity):
        state.calls.append(identity)
        if state.revoked or identity["operation_id"] not in records:
            return None
        record, lease = records[identity["operation_id"]]
        return {
            "version": 1,
            **asdict(lease),
            "subject": subject,
            "domain_org_id": str(org),
            "adp_org_id": "adp-original",
            "invocation_id": "actual-invocation",
            "job_id": record.job_id,
            "admission_attempt_id": record.attempt_id,
            "plan_digest": record.plan_digest,
            "permissions": ["workspace:provision"],
            "not_after": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
            **state.altered,
        }

    execution = HarnessExecutionAuthority(connections.connect, verify_run=verify_run)
    validator = HarnessProviderAuthority(execution)
    monkeypatch.setattr(provider_authority, "_validator", validator)
    configured = [
        {
            "submitter_id": subject,
            "credential": "configured-test-token",
            "signing_key": "offline-signing-key",
            "workspaces": [str(workspace)],
        }
    ]
    monkeypatch.setattr(
        observations.settings, "observation_submitters", json.dumps(configured)
    )
    app = FastAPI()
    app.include_router(router)

    async def session_dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://domain.example"
    ) as client:

        async def post(**changes):
            return await client.post(
                "/internal/provider-operations",
                json={**body, **changes},
                headers={"Authorization": "configured-test-token"},
            )

        try:
            yield SimpleNamespace(
                post=post,
                body=body,
                state=state,
                connections=connections,
                execution=execution,
                validator=validator,
                configured=configured,
                admit=admit,
                org=str(org),
                workspace=str(workspace),
                subject=subject,
            )
        finally:
            await engine.dispose()


async def test_authenticated_machine_records_handle_without_user_context(handles):
    assert acting_principal() is None
    result = await handles.post()
    assert result.status_code == 201, result.text
    assert result.json()["durable"] is True
    assert acting_principal() is None
    assert handles.state.calls == [
        {
            "operation_id": handles.body["operation_authority"],
            "org_id": handles.org,
            "workspace_id": handles.workspace,
            "subject": handles.subject,
        }
    ]
    async with handles.connections.connect() as connection:
        row = await connection.fetchrow(
            "SELECT authority_run_id,authority_attempt_id,org_id::text FROM provider_operations"
        )
        assert tuple(row) == ("actual-invocation", handles.subject, handles.org)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "gcp"),
        ("resource_name", "foreign-node"),
        ("allocation_id", "foreign-allocation"),
        ("operation", "release"),
        ("idempotency_key", "another-call"),
        ("workspace", str(uuid.uuid4())),
        ("operation_authority", "foreign-operation"),
    ],
)
async def test_handle_fields_cannot_widen_admitted_authority(handles, field, value):
    result = await handles.post(**{field: value})
    assert result.status_code in {403, 404}, result.text
    async with handles.connections.connect() as connection:
        assert (
            await connection.fetchval("SELECT count(*) FROM provider_operations") == 0
        )


@pytest.mark.parametrize(
    "missing", ["provider", "resource_name", "idempotency_key", "operation"]
)
async def test_omitted_admitted_handle_field_is_never_a_wildcard(handles, missing):
    operation_id = await handles.admit(missing=missing)
    assert (await handles.post(operation_authority=operation_id)).status_code == 403


@pytest.mark.parametrize(
    "changed",
    [
        "subject",
        "org_id",
        "workspace_id",
        "invocation_id",
        "job_id",
        "admission_attempt_id",
        "attempt_id",
        "fence_token",
        "plan_digest",
    ],
)
async def test_gateway_response_must_match_actual_run_and_original_lease(
    handles, changed
):
    handles.state.altered[changed] = 999 if changed == "fence_token" else "forged"
    assert (await handles.post()).status_code == 403


@pytest.mark.parametrize(
    "revocation", ["run", "lease", "credential", "workspace-grant"]
)
async def test_revoked_machine_authority_cannot_record_provider_handle(
    handles, monkeypatch, revocation
):
    if revocation == "run":
        handles.state.revoked = True
    elif revocation == "lease":
        async with handles.connections.connect() as connection:
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second'"
            )
    else:
        configured = handles.configured if revocation == "workspace-grant" else []
        if configured:
            configured[0]["workspaces"] = []
        monkeypatch.setattr(
            observations.settings, "observation_submitters", json.dumps(configured)
        )
    assert (await handles.post()).status_code in {401, 403}


async def test_submitter_scope_does_not_borrow_or_change_ambient_user_context(handles):
    foreign = ActingPrincipal("other-human", "another-org", "another-workspace")
    token = set_acting_principal(foreign)
    try:
        assert (await handles.post()).status_code == 201
        assert acting_principal() is foreign
        assert (
            await handles.execution.resolve_submitter(
                handles.body["operation_authority"],
                submitter=Submitter(handles.subject),
                workspace=handles.workspace,
            )
            is None
        )
    finally:
        reset_acting_principal(token)
