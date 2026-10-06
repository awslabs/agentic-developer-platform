"""Original paid admission -> protected dispatch -> real pod bootstrap -> lease.

Only the AWS/Kubernetes transports and dedicated DSN factory are replaced. The
paid database checks, HMAC, registry, BootstrapStore and TaskDelivery remain real.
"""

import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import boto3
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.routes import AgentRuntime
from src.auth.agent_registry import AgentRegistryService
from src.internal import controller_execution_routes as controller_routes
from src.internal import domain_current_identity
from src.internal import domain_operation_dispatch as dispatch_module
from src.internal import domain_operation_routes as routes
from src.internal import domain_operation_runtime as runtime_module
from src.internal.domain_operation_store import DomainBinding
from src.internal.vault_evidence_routes import DELIVERY_SCOPE
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.agentauth.test_bootstrap_routes import DIGEST, ENV, kubernetes, store  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness/jobs"))
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest  # noqa: E402
from harness_jobs.schema import apply  # noqa: E402

ORG = "10000000-0000-0000-0000-000000000001"
WORKSPACE = "20000000-0000-0000-0000-000000000002"
PREFIX = "/internal/v1/controller-execution"


@pytest.fixture
async def paid(pg_url, store, kubernetes, monkeypatch):  # noqa: F811
    @asynccontextmanager
    async def connect(binding=None):
        connection = await asyncpg.connect(pg_url)
        try:
            yield connection
        finally:
            await connection.close()

    async with connect() as c:
        await apply(c)
        await c.execute("CREATE TABLE organizations(id uuid PRIMARY KEY,adp_org_id text NOT NULL)")
        await c.execute("INSERT INTO organizations VALUES($1::text::uuid,'tenant')", ORG)
        request = OperationRequest(action="provision", idempotency_key="approved-key")
        await c.execute(
            "INSERT INTO harness_operations(operation_id,attempt_id,job_id,org_id,workspace_id,action,"
            "idempotency_key,plan_digest,request_payload,contract_version,state) "
            "VALUES('original-operation','original-admission-attempt','original-job',$1,$2,'provision',"
            "'approved-key',$3,$4,'v1','pending')",
            ORG,
            WORKSPACE,
            payload_digest(request),
            encode_payload(request),
        )
        await c.execute(
            "INSERT INTO harness_approval_consumption(approval_id,operation_id,org_id,workspace_id,plan_digest,"
            "requester,approved_by,max_resource_units,max_runtime_seconds,max_cost_micros,reservation_state,reservation_id) "
            "VALUES('approval-paid','original-operation',$1,$2,$3,'human','approver',1,600,100,'confirmed','reservation')",
            ORG,
            WORKSPACE,
            payload_digest(request),
        )
        await c.execute("""
            CREATE TABLE operation_budget_reservations(reservation_id text PRIMARY KEY, job_id text,
                attempt_id text,org_id text,workspace_id text,state text,max_resource_units bigint,max_runtime_seconds bigint,max_cost_micros bigint);
            CREATE TABLE operation_approvals(approval_id text PRIMARY KEY,org_id text,workspace_id text,requester text,
                plan_digest text,request_payload text,approvers_json text,max_resource_units bigint,max_runtime_seconds bigint,max_cost_micros bigint,
                result text,decided_by text,decided_at timestamptz,expires_at timestamptz,revoked boolean,organization_scope boolean);
            CREATE TABLE workspaces(id uuid PRIMARY KEY);
            CREATE TABLE workspace_grants(workspace_id uuid,org_id uuid,principal text,principal_type text,permissions text,revoked_at timestamptz);
            CREATE TABLE organization_grants(org_id uuid,principal text,principal_type text,permissions text,revoked_at timestamptz);
        """)
        await c.execute(
            "INSERT INTO operation_budget_reservations VALUES('reservation','original-job','original-admission-attempt',$1,$2,'confirmed',1,600,100)",
            ORG,
            WORKSPACE,
        )
        await c.execute(
            "INSERT INTO operation_approvals VALUES('approval-paid',$1,$2,'human',$3,$4,'[\"approver\"]',1,600,100,"
            "'allowed-once','approver',clock_timestamp(),clock_timestamp()+interval '1 hour',false,false)",
            ORG,
            WORKSPACE,
            payload_digest(request),
            encode_payload(request),
        )
        await c.execute("INSERT INTO workspaces VALUES($1::text::uuid)", WORKSPACE)
        await c.execute(
            "INSERT INTO workspace_grants VALUES($1::text::uuid,$2::text::uuid,'human','human','workspace:provision',NULL),"
            "($1::text::uuid,$2::text::uuid,'approver','human','workspace:administer',NULL)",
            WORKSPACE,
            ORG,
        )
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="paid-operations")["QueueUrl"]
    binding = DomainBinding(
        "superplane",
        ORG,
        "tenant",
        "producer",
        "worker",
        "domain-secret",
        "superplane",
        queue,
        "adp-agents",
        "agent-scaledjob-sa",
        "agent-worker",
        (DIGEST,),
        "org/repo",
        "https://domain.example",
        "observer-secret",
    )
    monkeypatch.setenv("ADP_DOMAIN_OPERATION_BINDINGS", json.dumps([asdict(binding)]))
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    runtime = AgentRuntime(store=store, workloads=kubernetes[0], env=ENV)
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def operation_session(binding):
        async with sessions() as session:
            yield session

    monkeypatch.setattr(controller_routes, "operation_session", operation_session)
    for module in (routes, runtime_module, dispatch_module):
        monkeypatch.setattr(module, "operation_connect", connect)
    for module in (routes, runtime_module):
        monkeypatch.setattr(module, "runtime_for", lambda binding: runtime)
        monkeypatch.setattr(module, "bootstrap_store", lambda: store)
    monkeypatch.setattr(dispatch_module, "aws_client", lambda service: sqs if service == "sqs" else boto3.client(service, region_name="us-east-1"))
    monkeypatch.setattr(runtime_module, "aws_client", lambda service: sqs if service == "sqs" else boto3.client(service, region_name="us-east-1"))
    store.client.create_table(
        TableName="paid-registry",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "agent_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "agent_id", "AttributeType": "S"}, {"AttributeName": "role_arn", "AttributeType": "S"}],
        GlobalSecondaryIndexes=[
            {"IndexName": "by-role-arn", "KeySchema": [{"AttributeName": "role_arn", "KeyType": "HASH"}], "Projection": {"ProjectionType": "ALL"}}
        ],
    )
    for name, scopes in (
        ("producer", [runtime_module.PRODUCER_SCOPE]),
        ("worker", [runtime_module.EXECUTOR_SCOPE, runtime_module.RECOVERY_SCOPE, DELIVERY_SCOPE]),
    ):
        store.client.put_item(
            TableName="paid-registry",
            Item={
                "agent_id": {"S": name},
                "role_arn": {"S": f"arn:aws:iam::123456789012:role/{name}"},
                "agent_name": {"S": name},
                "org_id": {"S": "tenant"},
                "team_id": {"S": "team"},
                "scope": {"S": "internal"},
                "status": {"S": "active"},
                "credential_scopes": {"SS": scopes},
            },
        )
    registry = AgentRegistryService(table_name="paid-registry")
    registry._dynamodb = store.client
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: registry)
    settings = SimpleNamespace(trust_apigw_headers=True, apigw_provenance_secret="edge-proof", internal_api_key="test-internal")
    monkeypatch.setattr("src.internal.auth_deps.get_settings", lambda: settings)
    monkeypatch.setattr("src.auth.middleware.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(controller_routes.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.example")

    async def post(path, body=None, *, role="worker", credential=None):
        headers = {
            "X-Caller-Identity": f"arn:aws:sts::123456789012:assumed-role/{role}/pod",
            "X-Adp-Edge-Provenance": "edge-proof",
            "X-Adp-Workload-Token": "pod-token",
        }
        if credential:
            headers["X-Adp-Run-Credential"] = credential
        return await client.post(PREFIX + path, json={} if body is None else body, headers=headers)

    body = dict(
        domain="superplane",
        org_id=ORG,
        workspace_id=WORKSPACE,
        operation_id="original-operation",
        job_id="original-job",
        attempt_id="original-admission-attempt",
        mode="execution",
    )
    try:
        yield SimpleNamespace(post=post, connect=connect, store=store, runtime=runtime, body=body, kubernetes=kubernetes, sqs=sqs, queue=queue)
    finally:
        await client.aclose()
        await engine.dispose()


async def start(paid):
    dispatched = await paid.post("/dispatch", paid.body, role="producer")
    assert dispatched.status_code == 200, dispatched.text
    acquired = await paid.post("/task/acquire")
    assert acquired.status_code == 200, acquired.text
    envelope = json.loads(acquired.json()["body"])
    boot = await paid.post("/bootstrap", {"invocation_id": envelope["message_id"], "envelope_digest": envelope_digest(envelope)})
    assert boot.status_code == 200, boot.text
    return dispatched.json(), boot.json()["credential"]


async def test_original_ids_survive_retry_bootstrap_and_lost_lease_response(paid):
    dispatched, credential = await start(paid)
    retry = await paid.post("/dispatch", paid.body, role="producer")
    assert retry.status_code == 200, retry.text
    assert retry.json() == dispatched
    assert dispatched["org_id"] == ORG and dispatched["adp_org_id"] == "tenant"
    assert dispatched["attempt_id"] == "original-admission-attempt"
    assert dispatched["invocation_id"] != "original-operation"
    first = await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)
    second = await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json() == second.json()
    assert first.json()["holder"] == dispatched["principal"]
    assert first.json()["attempt_id"] == dispatched["principal"]
    assert first.json()["fence_token"] == 1
    authority = await paid.post("/authority", {"operation_id": "original-operation"}, credential=credential)
    assert authority.status_code == 200, authority.text
    assert authority.json()["org_id"] == ORG
    assert authority.json()["holder"] == dispatched["principal"]
    verified = await paid.post(
        "/verify-run",
        {k: paid.body[k] for k in ("domain", "org_id", "workspace_id", "operation_id")} | {"subject": dispatched["principal"]},
        role="producer",
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["admission_attempt_id"] == "original-admission-attempt"
    assert verified.json()["subject"] == dispatched["principal"]
    assert "credential" not in verified.json()
    paid.kubernetes[1]["deleted"] = True
    assert (await paid.post("/task/heartbeat", credential=credential)).status_code == 403


@pytest.mark.parametrize("mutation", ["foreign-org", "foreign-workspace", "foreign-job", "foreign-attempt", "cancelled", "unpaid", "wrong-producer"])
async def test_dispatch_refuses_foreign_or_withdrawn_paid_authority(paid, mutation):
    body = dict(paid.body)
    role = "producer"
    field = {"foreign-org": "org_id", "foreign-workspace": "workspace_id", "foreign-job": "job_id", "foreign-attempt": "attempt_id"}.get(mutation)
    if field:
        body[field] = "foreign"
    elif mutation == "wrong-producer":
        role = "worker"
    else:
        async with paid.connect() as c:
            await c.execute(
                "UPDATE harness_operations SET cancel_requested_at=clock_timestamp()"
                if mutation == "cancelled"
                else "UPDATE harness_approval_consumption SET reservation_state='retained'"
            )
    response = await paid.post("/dispatch", body, role=role)
    assert response.status_code in {403, 503}, response.text
    assert "Messages" not in paid.sqs.receive_message(QueueUrl=paid.queue)


async def test_terminal_ack_retry_is_idempotent_and_cannot_reopen_execution(paid):
    _, credential = await start(paid)
    response = await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)
    assert response.status_code == 200, response.text
    async with paid.connect() as c:
        await c.execute("UPDATE harness_operations SET state='succeeded'")
    assert (await paid.post("/task/ack", credential=credential)).status_code == 200
    assert (await paid.post("/task/ack", credential=credential)).status_code == 200
    assert (await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)).status_code == 403


async def test_invalid_body_stays_validation_error(paid):
    assert (await paid.post("/dispatch", {"caller_grant": "forged"}, role="producer")).status_code == 422


async def test_recovery_scope_claim_subject_and_fence_are_current_paid_run(paid):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import acquire, fence_expired_lease

    async with paid.connect() as c:
        await acquire(c, operation_id="original-operation", holder="dead-run#1", attempt_id="dead-run#1")
        await c.execute("UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second'")
    paid.body["mode"] = "recovery"
    dispatched, credential = await start(paid)
    scope = await paid.post("/recovery/scope", credential=credential)
    assert scope.status_code == 200, scope.text
    data = scope.json()
    assert data["permissions"] == ["workspace:recover"]
    assert data["subject"] == dispatched["principal"]
    actor = ResolvedPrincipal(ORG, WORKSPACE, data["subject"], frozenset(data["permissions"]))
    async with paid.connect() as c:
        takeover = await fence_expired_lease(c, operation_id="original-operation", recovery_principal=actor)
        lease = takeover.lease
    claim = {key: getattr(lease, key) for key in ("operation_id", "org_id", "workspace_id", "holder", "attempt_id", "fence_token")}
    result = await paid.post("/recovery/authority", {"claim": claim}, credential=credential)
    assert result.status_code == 200, result.text
    assert result.json()["claim"] == claim
    assert (await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)).status_code == 403
    async with paid.connect() as c:
        await c.execute("UPDATE harness_recovery_claim_bindings SET subject='foreign-run#1'")
    assert (await paid.post("/recovery/authority", {"claim": claim}, credential=credential)).status_code == 403


@pytest.mark.parametrize("action", ["observe", "inventory", "lifecycle", "account-creation", "bootstrap"])
async def test_recovery_observations_refuse_same_org_foreign_workspace_before_domain_call(paid, monkeypatch, action):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import acquire, fence_expired_lease

    foreign_workspace = "20000000-0000-0000-0000-000000000003"
    async with paid.connect() as connection:
        await connection.execute("INSERT INTO workspaces VALUES($1::text::uuid)", foreign_workspace)
        await acquire(connection, operation_id="original-operation", holder="dead-run#1", attempt_id="dead-run#1")
        await connection.execute("UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second'")
    paid.body["mode"] = "recovery"
    dispatched, credential = await start(paid)
    actor = ResolvedPrincipal(ORG, WORKSPACE, dispatched["principal"], frozenset({"workspace:recover"}))
    async with paid.connect() as connection:
        takeover = await fence_expired_lease(connection, operation_id="original-operation", recovery_principal=actor)
    claim = {key: getattr(takeover.lease, key) for key in ("operation_id", "org_id", "workspace_id", "holder", "attempt_id", "fence_token")}
    assert (await paid.post("/recovery/authority", {"claim": claim}, credential=credential)).status_code == 200

    domain_request = AsyncMock(return_value={"claim": claim, "query_id": "query-1"})
    monkeypatch.setattr(routes, "domain_request", domain_request)
    body = {"claim": claim, "query_id": "query-1"}
    body.update({"allocation_id": "allocation-1"} if action == "inventory" else {"idempotency_key": "observation-1"})
    allowed = await paid.post(f"/recovery/{action}", body, credential=credential)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["claim"]["workspace_id"] == WORKSPACE

    substituted = await paid.post(
        f"/recovery/{action}",
        {**body, "claim": {**claim, "workspace_id": foreign_workspace}},
        credential=credential,
    )
    assert substituted.status_code == 403, substituted.text
    domain_request.assert_awaited_once()
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_call_intent") == 0


async def test_expired_recovery_bootstrap_retries_but_active_claim_does_not(paid, monkeypatch):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import acquire, fence_expired_lease

    async with paid.connect() as connection:
        await acquire(connection, operation_id="original-operation", holder="dead-run#1", attempt_id="dead-run#1")
        await connection.execute("UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second'")
    paid.body["mode"] = "recovery"
    first = await paid.post("/dispatch", paid.body, role="producer")
    assert first.status_code == 200, first.text
    deadline = datetime.fromisoformat(first.json()["not_after"].replace("Z", "+00:00"))

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline + timedelta(seconds=1)

    with monkeypatch.context() as patch:
        patch.setattr(dispatch_module, "datetime", Later)
        successor = await paid.post("/dispatch", paid.body, role="producer")
        assert successor.status_code == 200, successor.text
        assert successor.json()["invocation_id"] != first.json()["invocation_id"]
        assert all(successor.json()[key] == value for key, value in paid.body.items())
        duplicate = await paid.post("/dispatch", paid.body, role="producer")
        assert duplicate.json() == successor.json()
    actor = ResolvedPrincipal(ORG, WORKSPACE, "current-recovery#1", frozenset({"workspace:recover"}))
    async with paid.connect() as connection:
        assert await fence_expired_lease(connection, operation_id="original-operation", recovery_principal=actor)
    assert (await paid.post("/dispatch", paid.body, role="producer")).status_code == 403


@pytest.mark.parametrize(
    "mutation",
    [
        "revoked-approval",
        "expired-approval",
        "revoked-approver",
        "revoked-requester",
        "swapped-decision",
        "unselected-approver",
        "released-budget",
        "retained-budget",
        "foreign-reservation",
        "changed-envelope",
    ],
)
async def test_current_domain_approval_and_exact_reservation_gate_every_execution_read(paid, mutation):
    dispatched, credential = await start(paid)
    assert (await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)).status_code == 200
    statements = {
        "revoked-approval": "UPDATE operation_approvals SET revoked=true",
        "expired-approval": "UPDATE operation_approvals SET expires_at=clock_timestamp()-interval '1 second'",
        "revoked-approver": "UPDATE workspace_grants SET revoked_at=clock_timestamp() WHERE principal='approver'",
        "revoked-requester": "UPDATE workspace_grants SET revoked_at=clock_timestamp() WHERE principal='human'",
        "swapped-decision": "UPDATE operation_approvals SET decided_by='other-human'",
        "unselected-approver": "UPDATE operation_approvals SET approvers_json='[\"other-human\"]'",
        "released-budget": "UPDATE operation_budget_reservations SET state='released'",
        "retained-budget": "UPDATE operation_budget_reservations SET state='retained'",
        "foreign-reservation": "UPDATE operation_budget_reservations SET attempt_id='foreign-admission-attempt'",
        "changed-envelope": "UPDATE operation_budget_reservations SET max_cost_micros=101",
    }
    async with paid.connect() as c:
        await c.execute(statements[mutation])
    for path in ("/authority", "/lease"):
        response = await paid.post(path, {"operation_id": "original-operation"}, credential=credential)
        assert response.status_code == 403, (mutation, path, response.text)
    verified = await paid.post(
        "/verify-run",
        {key: paid.body[key] for key in ("domain", "org_id", "workspace_id", "operation_id")} | {"subject": dispatched["principal"]},
        role="producer",
    )
    assert verified.status_code == 403, verified.text
    async with paid.connect() as c:
        assert await c.fetchval("SELECT fence_token FROM harness_operation_leases") == 1
        assert await c.fetchval("SELECT count(*) FROM harness_provider_call_intent") == 0


@pytest.mark.parametrize("revoked_subject", ["human", "approver"])
async def test_original_human_membership_is_rechecked_before_dispatch_and_paid_effects(paid, db_session, monkeypatch, revoked_subject):
    db_session.add(Organization(id="tenant", name="Current ADP tenant"))
    await db_session.flush()
    memberships = {}
    for subject in ("human", "approver"):
        user = User(
            id=f"adp-{subject}",
            org_id="tenant",
            team_id="",
            email=f"{subject}@example.test",
            cognito_sub=subject,
        )
        db_session.add(user)
        await db_session.flush()
        membership = TenantMembership(user_id=user.id, tenant_id="tenant", is_active=True)
        db_session.add(membership)
        memberships[subject] = membership
    await db_session.commit()

    @asynccontextmanager
    async def membership_session():
        yield db_session

    monkeypatch.setattr(domain_current_identity, "get_session_factory", lambda: membership_session)
    monkeypatch.setattr(domain_current_identity, "cognito_user_pool_id", lambda: "fixture-pool")
    cognito = MagicMock()
    cognito.list_users.side_effect = lambda **kwargs: {"Users": [{"Username": kwargs["Filter"].split('"')[1]}]}
    cognito.admin_get_user.side_effect = lambda **kwargs: {
        "Enabled": True,
        "UserAttributes": [
            {"Name": "sub", "Value": kwargs["Username"]},
            {"Name": "custom:org_id", "Value": "tenant"},
        ],
    }
    monkeypatch.setattr(domain_current_identity, "aws_client", lambda service: cognito)
    bindings = json.loads(os.environ["ADP_DOMAIN_OPERATION_BINDINGS"])
    bindings[0]["current_identity_enforced"] = True
    monkeypatch.setenv("ADP_DOMAIN_OPERATION_BINDINGS", json.dumps(bindings))

    memberships[revoked_subject].revoked_at = datetime.now(UTC)
    await db_session.commit()
    refused = await paid.post("/dispatch", paid.body, role="producer")
    assert refused.status_code == 403, refused.text
    assert "Messages" not in paid.sqs.receive_message(QueueUrl=paid.queue)

    memberships[revoked_subject].revoked_at = None
    await db_session.commit()
    dispatched, credential = await start(paid)
    first_lease = await paid.post(
        "/lease",
        {"operation_id": "original-operation"},
        credential=credential,
    )
    assert first_lease.status_code == 200, first_lease.text
    assert cognito.admin_get_user.call_count >= 4

    memberships[revoked_subject].revoked_at = datetime.now(UTC)
    await db_session.commit()
    for path, body, role in [
        ("/authority", {"operation_id": "original-operation"}, "worker"),
        ("/lease", {"operation_id": "original-operation"}, "worker"),
        ("/renew", None, "worker"),
        (
            "/verify-run",
            {key: paid.body[key] for key in ("domain", "org_id", "workspace_id", "operation_id")} | {"subject": dispatched["principal"]},
            "producer",
        ),
    ]:
        response = await paid.post(path, body, credential=credential, role=role)
        assert response.status_code == 403, (revoked_subject, path, response.text)
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT fence_token FROM harness_operation_leases") == 1
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_call_intent") == 0
