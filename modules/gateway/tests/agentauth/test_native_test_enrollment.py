"""Offline PostgreSQL/Moto/Lua checks; no native login or provider success claimed."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import EndpointConnectionError
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.responses import JSONResponse

from src.agentauth.native_test_enrollment import (
    AUDIENCE,
    CAPABILITY_HEADER,
    OIDC_AUDIENCE,
    OPERATION_HEADER,
    ApprovalRequest,
    ApprovedJob,
    FixtureDesignation,
    NativeTestEnrollment,
    NativeTestStore,
)
from src.agentauth.native_test_model import NativeModelHandoff, NativeTestModelMiddleware
from src.agentauth.test_run_admission import PreparedModelAdmission, TestRunAdmissionService
from src.agentauth.test_run_admission_store import AdmissionRefusedError
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.internal.tools_policy import ToolsPolicyStore
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.tool_entitlement import ToolEntitlement
from src.shared.models.vault import UserIdentity
from tests.agentauth import test_test_run_admission as primitive
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

admission = primitive.admission
BODY = primitive.BODY
TOOL = "computer.use"


@pytest.fixture
async def sessions(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    tables = [
        Organization.__table__,
        User.__table__,
        Team.__table__,
        TeamMembership.__table__,
        TenantMembership.__table__,
        UserIdentity.__table__,
        ToolEntitlement.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Organization(id="tenant-one", name="one"))
        await db.flush()
        db.add(Team(id="team-one", org_id="tenant-one", department_id="department-one", name="team"))
        db.add_all(
            [
                User(id="canonical-human", org_id="tenant-one", cognito_sub="jwt-subject", email="fixture@example.test", team_id="team-one"),
                User(id="owner", org_id="tenant-one", cognito_sub="owner-subject", email="owner@example.test", team_id="team-one"),
            ]
        )
        await db.flush()
        db.add_all([TenantMembership(user_id=user, tenant_id="tenant-one", is_active=True) for user in ("canonical-human", "owner")])
        db.add(TeamMembership(user_id="canonical-human", org_id="tenant-one", team_id="team-one", is_primary=True))
        db.add(ToolEntitlement(org_id="tenant-one", team_id="", tool_name=TOOL, enabled=True, max_cost_usd=Decimal("1")))
        await db.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def native(admission, sessions, monkeypatch):
    f = admission
    f.clock[0] = datetime.now(UTC)
    store = NativeTestStore(table_name=f.store.table, client=f.client)
    designation = FixtureDesignation(
        subject_id="jwt-subject",
        tenant_id="tenant-one",
        user_id="canonical-human",
        team_id="team-one",
        department_id="department-one",
        owner_id="owner",
        deployment_id="offline-gateway",
        revision=str(uuid4()),
        revocation_epoch=0,
        state="active",
    )
    # This is explicit fixture setup, not a production designation writer.
    f.client.put_item(
        TableName=store.table, Item=store.item(store.fixture_key(designation.subject_id), "native_test_fixture_v1", designation.model_dump())
    )
    policy = {
        "record_type": "TOOLS_POLICY",
        "schema_version": "1.0",
        "tenant_id": "tenant-one",
        "owner_id": "canonical-human",
        "status": "active",
        "allowed_tools": [TOOL],
        "consented_tools": [TOOL],
        "tool_max_cost_usd": {TOOL: Decimal("1")},
        "revision": 1,
    }
    policy_key = {"pk": {"S": "TENANT#tenant-one"}, "sk": {"S": "TOOLS_POLICY#canonical-human"}}
    f.client.put_item(TableName=store.table, Item={**policy_key, **{k: TypeSerializer().serialize(v) for k, v in policy.items()}})
    monkeypatch.setattr("src.internal.tools_catalogue.gateway_catalogue", lambda: {"tools": [{"name": TOOL}]})
    issuer = NativeTestEnrollment(
        store=store,
        session_factory=sessions,
        policy_store=ToolsPolicyStore(table_name=store.table, client=f.client),
        reservations=f.reservations,
        deployment_id="offline-gateway",
        clock=lambda: f.clock[0],
    )
    service = TestRunAdmissionService(store=store, budget=f.budget, identity_verifier=issuer, clock=lambda: f.clock[0])
    downstream = AsyncMock()
    middleware = NativeTestModelMiddleware(downstream, enrollment=issuer, admission=service)
    now = int(f.clock[0].timestamp())
    job = ApprovedJob(
        repository="owner/repo",
        repository_id="123",
        workflow_ref="owner/repo/.github/workflows/native.yml@refs/heads/main",
        ref="refs/heads/main",
        sha="a" * 40,
        workflow_sha="b" * 40,
        event_name="workflow_dispatch",
        run_id="456",
        run_attempt="1",
        actor_id="789",
        sub="repo:owner/repo:environment:native-test",
        runner_environment="self-hosted",
        runner_role="arn:aws:iam::123456789012:role/offline-runner",
    )
    request = ApprovalRequest(
        run_id=str(uuid4()),
        scenario="SCN-8064-04",
        job=job,
        allowed_models=["offline-model"],
        allowed_routes=["/v1/messages", "/openai/v1/responses"],
        tools=[TOOL],
        dispatch_expires_at=now + 300,
        cleanup_expires_at=now + 600,
        meter_expires_at=now + 86400,
    )
    login = primitive.context(f)
    owner = login.model_copy(update={"user_id": "owner-subject"})
    f.native = SimpleNamespace(
        store=store,
        issuer=issuer,
        service=service,
        middleware=middleware,
        downstream=downstream,
        designation=designation,
        request=request,
        login=login,
        owner=owner,
        sessions=sessions,
        policy=policy,
        policy_key=policy_key,
        fixture=f,
    )
    return f.native


async def enroll(native, monkeypatch):
    approval = await native.issuer.approve(native.owner, subject_id=native.designation.subject_id, request=native.request)
    # Most tests isolate issuance/accounting; signature and exact job binding are
    # exercised separately below. This substitute proves no real CI identity.
    monkeypatch.setattr(native.issuer, "verify_job", AsyncMock(return_value={"exp": approval.dispatch_expires_at}))
    token = await native.issuer.exchange(native.login, run_id=approval.run_id, github_oidc_token="offline-proof", producer_proof="offline-aws")
    native.approval, native.token = approval, token
    return approval, token


async def prepare(native, *, operation_id=None, route="/v1/messages"):
    return await native.service.prepare_model(
        context=native.login, credential=native.token, operation_id=operation_id or str(uuid4()), body=BODY, route=route
    )


async def test_real_owner_existing_fixture_capability_and_once_only_meter(native, monkeypatch):
    approval, token = await enroll(native, monkeypatch)
    identity = await native.issuer.verify(native.login, token)
    assert identity.user_id == "canonical-human" and identity.subject_id == "jwt-subject"
    assert identity == approval.identity
    capability = native.store.capability("tenant-one", approval.credential_id)
    assert capability["audience"] == AUDIENCE
    assert token not in json.dumps(capability)
    assert capability["token_digest"] == hashlib.sha256(token.encode()).hexdigest()
    target = native.service._target(approval.authority())
    assert (await native.fixture.reservations.snapshot(target)).total_usd == 0
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        await native.issuer.exchange(native.login, run_id=approval.run_id, github_oidc_token="offline", producer_proof="offline")
    assert native.store.capability("tenant-one", approval.credential_id) == capability
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        await native.issuer.approve(native.owner, subject_id="jwt-subject", request=native.request.model_copy(update={"run_id": str(uuid4())}))
    assert native.issuer.policy_store.get(tenant_id="tenant-one", owner_id="canonical-human") == {
        **native.policy,
        "pk": "TENANT#tenant-one",
        "sk": "TOOLS_POLICY#canonical-human",
    }


@pytest.mark.parametrize(
    "change",
    ["wrong_owner", "service", "iam", "expired", "no_designation", "no_enforcer", "revoked_membership", "no_team", "no_consent", "no_entitlement"],
)
async def test_approval_refuses_without_existing_authority(native, change):
    owner = native.owner
    if change == "wrong_owner":
        owner = native.login
    elif change == "service":
        owner = owner.model_copy(update={"account_type": "service"})
    elif change == "iam":
        owner = owner.model_copy(update={"auth_source": "iam"})
    elif change == "expired":
        owner = owner.model_copy(update={"expires_at": native.fixture.clock[0]})
    elif change == "no_designation":
        native.fixture.client.delete_item(TableName=native.store.table, Key=native.store.fixture_key("jwt-subject"))
    elif change == "no_enforcer":
        native.issuer._enforcer = None
    elif change == "no_consent":
        native.fixture.client.update_item(
            TableName=native.store.table,
            Key=native.policy_key,
            UpdateExpression="SET consented_tools = :empty",
            ExpressionAttributeValues={":empty": {"L": []}},
        )
    else:
        async with native.sessions() as db:
            if change == "revoked_membership":
                await db.execute(update(TenantMembership).where(TenantMembership.user_id == "owner").values(revoked_at=datetime.now(UTC)))
            elif change == "no_team":
                await db.execute(delete(TeamMembership))
            else:
                await db.execute(update(ToolEntitlement).values(enabled=False))
            await db.commit()
    with pytest.raises(Exception):
        await native.issuer.approve(owner, subject_id="jwt-subject", request=native.request)
    assert native.store.document(native.store.key("tenant-one", "APPROVAL", native.request.run_id), "native_test_approval_v1") is None


async def test_ambiguous_issuance_does_not_reinitialize_or_rotate(native, monkeypatch):
    approval = await native.issuer.approve(native.owner, subject_id="jwt-subject", request=native.request)
    monkeypatch.setattr(native.issuer, "verify_job", AsyncMock(return_value={"exp": approval.dispatch_expires_at}))
    original = native.store.client.transact_write_items

    def lost_ack(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="offline")

    monkeypatch.setattr(native.store.client, "transact_write_items", lost_ack)
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        await native.issuer.exchange(native.login, run_id=approval.run_id, github_oidc_token="offline", producer_proof="offline")
    capability = native.store.capability("tenant-one", approval.credential_id)
    assert capability["status"] == "issuing"
    monkeypatch.setattr(native.store.client, "transact_write_items", original)
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        await native.issuer.exchange(native.login, run_id=approval.run_id, github_oidc_token="offline", producer_proof="offline")
    assert await native.fixture.reservations.snapshot(native.service._target(approval.authority())) is None


@pytest.mark.parametrize("change", ["tenant", "subject", "token", "audience", "expired", "revoked", "consent", "team"])
async def test_capability_rechecks_current_authority(native, monkeypatch, change):
    approval, token = await enroll(native, monkeypatch)
    context = native.login
    if change in {"tenant", "subject"}:
        context = context.model_copy(update={"org_id" if change == "tenant" else "user_id": "other"})
    elif change == "token":
        token += "x"
    elif change == "audience":
        cap = native.store.capability("tenant-one", approval.credential_id)
        native.store.transaction(
            [
                native.store.put(
                    native.store.key("tenant-one", "CAPABILITY", approval.credential_id),
                    "native_test_capability_v1",
                    {**cap, "audience": "other"},
                    cap,
                )
            ]
        )
    elif change == "expired":
        native.fixture.clock[0] += timedelta(seconds=301)
    elif change == "revoked":
        await native.issuer.revoke(native.owner, run_id=approval.run_id)
    elif change == "consent":
        native.fixture.client.update_item(
            TableName=native.store.table,
            Key=native.policy_key,
            UpdateExpression="SET consented_tools = :empty",
            ExpressionAttributeValues={":empty": {"L": []}},
        )
    else:
        async with native.sessions() as db:
            await db.execute(delete(TeamMembership))
            await db.commit()
    with pytest.raises(AdmissionRefusedError):
        await native.issuer.verify(context, token)


async def test_revocation_after_verifier_read_fences_dispatch_cas(native, monkeypatch):
    await enroll(native, monkeypatch)
    prepared = await prepare(native)
    original = native.store.transition

    def revoked_before_cas(authority, operation, **kwargs):
        if kwargs["status"] == "dispatch_started":
            cap = native.store.capability("tenant-one", native.approval.credential_id)
            native.store.transaction(
                [
                    native.store.put(
                        native.store.key("tenant-one", "CAPABILITY", native.approval.credential_id),
                        "native_test_capability_v1",
                        {**cap, "status": "revoked"},
                        cap,
                    )
                ]
            )
        return original(authority, operation, **kwargs)

    monkeypatch.setattr(native.store, "transition", revoked_before_cas)
    with pytest.raises(AdmissionRefusedError):
        await native.service.begin_model_dispatch(prepared, context=native.login, credential=native.token, body=BODY, route="/v1/messages")
    assert (await native.fixture.reservations.snapshot(native.service._target(native.approval.authority()))) is not None
    assert native.store.read_operation("tenant-one", native.approval.run_id, prepared.receipt.operation_id)["status"] == "admitted"


async def test_cap20_is_shared_across_clients_and_operations(native, monkeypatch):
    await enroll(native, monkeypatch)
    native.fixture.cost = Decimal("11")
    first = await prepare(native)
    assert isinstance(first, PreparedModelAdmission)
    with pytest.raises(AdmissionRefusedError, match="test_budget_refused"):
        await prepare(native, route="/openai/v1/responses")
    target = native.service._target(native.approval.authority())
    assert (await native.fixture.reservations.snapshot(target)).total_usd == 11
    assert native.store.approval("tenant-one", native.approval.run_id).meter_epoch == native.approval.meter_epoch


async def test_signed_oidc_is_separate_audience_exact_job_source_and_aws_envelope(native, monkeypatch):
    from src.agentauth import arc_model, work_routes
    from src.agentauth.bootstrap import envelope_digest

    approval = await native.issuer.approve(native.owner, subject_id="jwt-subject", request=native.request)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "offline-key"
    producer = AsyncMock(return_value=approval.job.runner_role)
    monkeypatch.setattr(work_routes, "verify_producer", producer)

    class OfflineJWKS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            assert url == arc_model.ISSUER + "/.well-known/jwks"
            return httpx.Response(200, json={"keys": [jwk]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(arc_model.httpx, "AsyncClient", lambda **kwargs: OfflineJWKS())
    now = int(datetime.now(UTC).timestamp())
    claims = {
        **approval.job.model_dump(exclude={"runner_role", "job_workflow_ref"}),
        "iss": arc_model.ISSUER,
        "aud": OIDC_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
    }

    def signed(**changes):
        return jwt.encode({**claims, **changes}, key, algorithm="RS256", headers={"kid": "offline-key"})

    token = signed()
    await native.issuer.verify_job(approval, token, "aws-proof")
    producer.assert_awaited_with(
        "aws-proof",
        envelope_digest(
            {
                "tenant_id": "tenant-one",
                "run_id": approval.run_id,
                "approval_revision": approval.revision,
                "github_oidc_token": token,
                "deployment_id": "offline-gateway",
            }
        ),
        allowed_roles={approval.job.runner_role},
    )
    for changes in (
        {"aud": arc_model.AUDIENCE},
        {"sha": "c" * 40},
        {"workflow_sha": "d" * 40},
        {"run_id": "457"},
        {"run_attempt": "2"},
        {"sub": "repo:owner/repo:environment:unapproved"},
        {"job_workflow_ref": "owner/repo/job.yml@main"},
    ):
        with pytest.raises(Exception):
            await native.issuer.verify_job(approval, signed(**changes), "aws-proof")
    with pytest.raises(ValidationError):
        ApprovedJob.model_validate({**approval.job.model_dump(), "job_workflow_ref": "owner/repo/job.yml@main"})


async def request(native, *, context=None, route="/v1/messages", headers=None, body=BODY, app=None):
    scope = {
        "type": "http",
        "path": route,
        "method": "POST",
        "headers": headers or [],
        "state": {"token_context": context or native.login, "request_id": "ordinary-id"},
    }
    messages = []
    receive = AsyncMock(return_value={"type": "http.request", "body": body})
    if app is not None:
        native.middleware.app = app
    await native.middleware(scope, receive, AsyncMock(side_effect=messages.append))
    return scope, messages


@pytest.mark.parametrize("change", ["omitted", "service", "iam", "tenant", "attribution", "revoked", "store_fault", "unsupported", "duplicate"])
async def test_designation_never_downgrades_to_ordinary_paid_route(native, monkeypatch, change):
    await enroll(native, monkeypatch)
    context = native.login
    route = "/v1/messages"
    headers = [(CAPABILITY_HEADER.encode(), native.token.encode()), (OPERATION_HEADER.encode(), str(uuid4()).encode())]
    if change == "omitted":
        headers = []
    elif change in {"service", "iam", "tenant", "attribution"}:
        field, value = {
            "service": ("account_type", "service"),
            "iam": ("auth_source", "iam"),
            "tenant": ("org_id", "another-tenant"),
            "attribution": ("attributed_org_id", "another-tenant"),
        }[change]
        context = context.model_copy(update={field: value})
    elif change == "revoked":
        designation = native.designation.model_copy(update={"state": "revoked", "revocation_epoch": 1})
        native.store.transaction(
            [
                native.store.put(
                    native.store.fixture_key("jwt-subject"), "native_test_fixture_v1", designation.model_dump(), native.designation.model_dump()
                )
            ]
        )
    elif change == "store_fault":
        monkeypatch.setattr(native.store, "designation", lambda _: (_ for _ in ()).throw(EndpointConnectionError(endpoint_url="offline")))
    elif change == "unsupported":
        route = "/v1/chat/completions"
    else:
        headers += headers[:1]
    _, messages = await request(native, context=context, headers=headers, route=route)
    assert messages[0]["status"] == 403
    native.downstream.assert_not_awaited()


async def test_exact_body_and_only_admission_reach_existing_budget_route(native, monkeypatch):
    await enroll(native, monkeypatch)
    seen = []
    ordinary_budget = SimpleNamespace(prepare_enforcement_context=AsyncMock(), check_budget_hierarchy=AsyncMock(), reconcile_reservation=AsyncMock())

    async def route(scope, receive, send):
        context = scope["state"]["token_context"]
        body = (await receive())["body"]
        assert body == BODY
        assert CAPABILITY_HEADER.encode() not in dict(scope["headers"])
        assert context._native_test_handoff.prepared._context is context
        assert scope["state"]["request_id"] == context._policy_request_id
        assert context._policy_flow_target in context._budget_admission_targets
        seen.append(context)
        await JSONResponse({"status": "offline-only"})(scope, receive, send)

    headers = [(CAPABILITY_HEADER.encode(), native.token.encode()), (OPERATION_HEADER.encode(), str(uuid4()).encode())]
    _, messages = await request(native, headers=headers, app=BudgetEnforcementMiddleware(route, enforcement_service=ordinary_budget))
    assert messages[0]["status"] == 200 and len(seen) == 1
    ordinary_budget.prepare_enforcement_context.assert_not_awaited()
    ordinary_budget.check_budget_hierarchy.assert_not_awaited()
    ordinary_budget.reconcile_reservation.assert_not_awaited()
    _, replay = await request(native, headers=headers)
    assert replay[0]["status"] == 409 and len(seen) == 1


async def test_ordinary_request_requires_negative_read_then_keeps_errors(native):
    context = native.login.model_copy(update={"user_id": "unregistered-subject"})
    native.downstream.side_effect = RuntimeError("ordinary error")
    with pytest.raises(RuntimeError, match="ordinary error"):
        await request(native, context=context)
    native.downstream.assert_awaited_once()


@pytest.mark.parametrize("change", ["none", "body", "model", "request_id", "retry", "context", "replay"])
async def test_translated_bedrock_handoff_is_single_use_and_bound(native, monkeypatch, change):
    from src.proxy.format_translator import FormatTranslator
    from src.proxy.schemas import AnthropicMessagesRequest

    await enroll(native, monkeypatch)
    prepared = await prepare(native)
    handoff = NativeModelHandoff(native.service, prepared, native.login, native.token, BODY, "/v1/messages")
    sdk = SimpleNamespace(meta=SimpleNamespace(config=SimpleNamespace(retries={"total_max_attempts": 1})))
    client = SimpleNamespace(_invoke_client=sdk)
    body = FormatTranslator().anthropic_to_bedrock(AnthropicMessagesRequest.model_validate_json(BODY)).model_dump(exclude_none=True)
    params = {
        "body": json.dumps(body).encode(),
        "model": "offline-model",
        "route": "/v1/messages",
        "request_id": prepared.receipt.reservation_id,
        "client": client,
    }
    context = prepared._context
    if change == "body":
        params["body"] = json.dumps({**body, "max_tokens": 100000}).encode()
    elif change == "model":
        params["model"] = "other-model"
    elif change == "request_id":
        params["request_id"] = "new-reservation"
    elif change == "retry":
        sdk.meta.config.retries["total_max_attempts"] = 2
    elif change == "context":
        context = context.model_copy()
    if change in {"none", "replay"}:
        await handoff.dispatch(context, **params)
        assert native.store.read_operation("tenant-one", native.approval.run_id, prepared.receipt.operation_id)["status"] == "dispatch_started"
        if change == "replay":
            with pytest.raises(AdmissionRefusedError):
                await handoff.dispatch(context, **params)
    else:
        with pytest.raises(AdmissionRefusedError):
            await handoff.dispatch(context, **params)
        assert native.store.read_operation("tenant-one", native.approval.run_id, prepared.receipt.operation_id)["status"] == "admitted"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("late_expiry", [False, True])
async def test_actual_bedrock_boundary_rechecks_after_preparation(native, monkeypatch, stream, late_expiry):
    from io import BytesIO

    from src.proxy.format_translator import FormatTranslator
    from src.proxy.pricing_capture import PricingCapture
    from src.proxy.schemas import AnthropicMessagesRequest
    from src.proxy.service import ProxyService

    await enroll(native, monkeypatch)
    body = json.dumps({**json.loads(BODY), "stream": stream}).encode()
    prepared = await native.service.prepare_model(
        context=native.login, credential=native.token, operation_id=str(uuid4()), body=body, route="/v1/messages"
    )
    context = prepared._context
    context._native_test_handoff = NativeModelHandoff(native.service, prepared, native.login, native.token, body, "/v1/messages")
    capture = PricingCapture(prepared.receipt.reservation_id, "offline-model")
    sdk = SimpleNamespace(
        meta=SimpleNamespace(
            config=SimpleNamespace(retries={"total_max_attempts": 1}),
            region_name="us-east-1",
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        )
    )
    calls = []

    async def invoke(**kwargs):
        kwargs.pop("_native_test_before_submit")()
        operation = native.store.read_operation("tenant-one", native.approval.run_id, prepared.receipt.operation_id)
        assert operation["status"] == "dispatch_started"
        assert context._budget_provider_started
        calls.append(kwargs)
        if stream:
            return {"body": [{"chunk": {"bytes": b'{"type":"offline-test-frame"}'}}]}
        return {
            "body": BytesIO(
                json.dumps(
                    {
                        "content": [],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "id": "offline",
                        "model": "offline-model",
                        "role": "assistant",
                        "type": "message",
                        "stop_reason": "end_turn",
                    }
                ).encode()
            )
        }

    client = SimpleNamespace(_invoke_client=sdk, _streaming_client=sdk, invoke_model=invoke, invoke_model_with_response_stream=invoke)
    proxy = ProxyService(SimpleNamespace())
    translated = FormatTranslator().anthropic_to_bedrock(AnthropicMessagesRequest.model_validate_json(body))
    if late_expiry:
        native.fixture.clock[0] += timedelta(seconds=301)

    async def call():
        if stream:
            return [
                part async for part in proxy._invoke_bedrock_stream(client, "offline-model", translated, pricing_capture=capture, context=context)
            ]
        return await proxy._invoke_bedrock(client, "offline-model", translated, pricing_capture=capture, context=context)

    if late_expiry:
        with pytest.raises(Exception):
            await call()
        assert not calls
    else:
        await call()
        assert len(calls) == 1
        with pytest.raises(Exception):
            await call()
        assert len(calls) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("known_usage", [False, True])
async def test_actual_responses_boundary_and_usage_keep_original_scopes(native, monkeypatch, stream, known_usage):
    from dataclasses import replace
    from unittest.mock import MagicMock

    from src.orchestration import provider_quotes
    from src.proxy import mantle_service

    model = "openai.gpt-5.6-luna"
    native.request = native.request.model_copy(update={"allowed_models": [model]})
    await enroll(native, monkeypatch)
    original_quote = provider_quotes.quote_request

    async def offline_quote(*args, **kwargs):
        return replace(await original_quote(*args, **kwargs), billing_model_id=model)

    monkeypatch.setattr(provider_quotes, "quote_request", offline_quote)
    body = json.dumps({"model": model, "input": "offline test", "max_output_tokens": 16, "stream": stream}).encode()
    prepared = await native.service.prepare_model(
        context=native.login, credential=native.token, operation_id=str(uuid4()), body=body, route="/openai/v1/responses"
    )
    context = prepared._context
    targets = list(context._budget_admission_targets)
    context._native_test_handoff = NativeModelHandoff(native.service, prepared, native.login, native.token, body, "/openai/v1/responses")
    calls = []
    response = {"id": "offline-response", "output": []}
    if known_usage:
        response["usage"] = {"input_tokens": 2, "output_tokens": 1}
    wire = json.dumps(response).encode()
    if stream:
        wire = b'data: {"type":"response.completed","response":' + wire + b"}\n\n"

    def upstream(request):
        assert json.loads(request.content)["model"] == "us." + model
        assert native.store.read_operation("tenant-one", native.approval.run_id, prepared.receipt.operation_id)["status"] == "dispatch_started"
        calls.append(request)
        return httpx.Response(200, content=wire)

    async def routing(*args, **kwargs):
        # A real awaited route/preparation step must finish before the CAS.
        assert native.store.read_operation("tenant-one", native.approval.run_id, prepared.receipt.operation_id)["status"] == "admitted"
        return mantle_service.RoutingDecision()

    monkeypatch.setattr(mantle_service, "resolve_routing_decision", routing)
    monkeypatch.setattr(mantle_service, "get_session_factory", lambda: lambda: AsyncMock())
    log = AsyncMock()
    monkeypatch.setattr(mantle_service, "UsageService", lambda db: SimpleNamespace(log_request=log))
    reconcile = AsyncMock(side_effect=native.fixture.budget.reconcile_reservation)
    monkeypatch.setattr(mantle_service, "reconcile_budget_reservation", reconcile)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    auth = MagicMock()
    auth.sign.return_value = {}
    service = mantle_service.MantlePassthroughService(
        auth, "https://bedrock-runtime.us-east-1.amazonaws.com", inference_profile_prefix="us", http_client=client
    )
    service._chat_logger = MagicMock()
    async with client:
        try:
            result = await service.create_response(body, context, stream=stream, model=model, request_id=prepared.receipt.reservation_id)
            if stream:
                _ = [chunk async for chunk in result]
        except mantle_service.MantleUpstreamError:
            assert not known_usage
    assert len(calls) == 1
    log.assert_awaited_once()
    args = log.await_args.kwargs
    assert args["context"] is context and args["context"]._budget_admission_targets == targets
    assert args["request_id"] == prepared.receipt.reservation_id == context._policy_request_id
    assert args["reservation_usage_known"] is known_usage
    assert reconcile.await_args.kwargs["context"] is context
    snapshot = await native.fixture.reservations.snapshot(native.service._target(native.approval.authority()))
    if known_usage:
        assert snapshot.total_usd == args["cost_usd"] < native.fixture.cost
    else:
        assert snapshot.total_usd == native.fixture.cost


@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_executor_delay_cannot_submit_expired_consumed_intent(native, monkeypatch, stream):
    from src.pool import simple_pool
    from src.proxy.format_translator import FormatTranslator
    from src.proxy.schemas import AnthropicMessagesRequest

    await enroll(native, monkeypatch)
    body = json.dumps({**json.loads(BODY), "stream": stream}).encode()
    prepared = await native.service.prepare_model(
        context=native.login, credential=native.token, operation_id=str(uuid4()), body=body, route="/v1/messages"
    )
    handoff = NativeModelHandoff(native.service, prepared, native.login, native.token, body, "/v1/messages")
    calls = []
    sdk = SimpleNamespace(
        meta=SimpleNamespace(config=SimpleNamespace(retries={"total_max_attempts": 1})),
        invoke_model=lambda **kwargs: calls.append(kwargs),
        invoke_model_with_response_stream=lambda **kwargs: calls.append(kwargs),
    )
    client = simple_pool.AsyncBedrockClient.__new__(simple_pool.AsyncBedrockClient)
    client._invoke_client = client._streaming_client = sdk
    translated = FormatTranslator().anthropic_to_bedrock(AnthropicMessagesRequest.model_validate_json(body)).model_dump(exclude_none=True)
    await handoff.dispatch(
        prepared._context,
        body=json.dumps(translated).encode(),
        model="offline-model",
        route="/v1/messages",
        request_id=prepared.receipt.reservation_id,
        client=client,
        stream=stream,
    )

    async def delayed_executor(call, *args, **kwargs):
        native.fixture.clock[0] += timedelta(seconds=121)
        return call(*args, **kwargs)

    monkeypatch.setattr(simple_pool.asyncio, "to_thread", delayed_executor)
    submit = client.invoke_model_with_response_stream if stream else client.invoke_model
    with pytest.raises(AdmissionRefusedError, match="submission_expired"):
        await submit(_native_test_before_submit=handoff.before_submit, modelId="offline-model", body=json.dumps(translated))
    assert not calls
    assert native.store.read_operation("tenant-one", native.approval.run_id, prepared.receipt.operation_id)["status"] == "dispatch_started"
    raw = await native.fixture.redis.hgetall(native.service._target(native.approval.authority()).key())
    assert prepared.receipt.reservation_id in raw


async def test_lost_activation_ack_leaves_one_meter_and_no_token_rotation(native, monkeypatch):
    approval = await native.issuer.approve(native.owner, subject_id="jwt-subject", request=native.request)
    monkeypatch.setattr(native.issuer, "verify_job", AsyncMock(return_value={"exp": approval.dispatch_expires_at}))
    original = native.store.client.transact_write_items
    count = 0

    def lose_activation(**kwargs):
        nonlocal count
        count += 1
        result = original(**kwargs)
        if count == 2:
            raise EndpointConnectionError(endpoint_url="offline")
        return result

    monkeypatch.setattr(native.store.client, "transact_write_items", lose_activation)
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        await native.issuer.exchange(native.login, run_id=approval.run_id, github_oidc_token="offline", producer_proof="offline")
    capability = native.store.capability("tenant-one", approval.credential_id)
    target = native.service._target(approval.authority())
    assert capability["status"] == "active" and (await native.fixture.reservations.snapshot(target)).total_usd == 0
    await native.fixture.redis.hset(target.key(), "offline-unknown", "5:" + str(approval.meter_expires_at))
    with pytest.raises(AdmissionRefusedError, match="write_unconfirmed"):
        await native.issuer.exchange(native.login, run_id=approval.run_id, github_oidc_token="offline", producer_proof="offline")
    assert native.store.capability("tenant-one", approval.credential_id) == capability
    assert (await native.fixture.reservations.snapshot(target)).total_usd == 5


@pytest.mark.parametrize("header", [CAPABILITY_HEADER, OPERATION_HEADER])
async def test_absent_native_middleware_never_ignores_asserted_test_scope(native, header):
    downstream = AsyncMock()
    ordinary = SimpleNamespace(prepare_enforcement_context=AsyncMock())
    middleware = BudgetEnforcementMiddleware(downstream, enforcement_service=ordinary)
    scope = {
        "type": "http",
        "path": "/v1/messages",
        "method": "POST",
        "headers": [(header.encode(), b"assertion")],
        "state": {"token_context": native.login},
    }
    messages = []
    await middleware(scope, AsyncMock(), AsyncMock(side_effect=messages.append))
    assert messages[0]["status"] == 403
    assert b"mandatory_native_test_enforcement_unavailable" in messages[1]["body"]
    ordinary.prepare_enforcement_context.assert_not_awaited()
    downstream.assert_not_awaited()
