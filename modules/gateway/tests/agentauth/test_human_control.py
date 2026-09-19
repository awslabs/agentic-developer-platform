"""Human-session control crosses real routes, SQL membership and signed proofs."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.envelope import SIGNING_KEY_ID_ENV, sign_envelope, verify_envelope
from src.agentauth.human_control import authorize_human_session
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext
from tests.activity.test_control_proxy import (
    CANONICAL_USER_ID,
    COMMAND_ID,
    ENV,
    RUN_ID,
    TENANT_ID,
    build_client,
    command_path,
    make_service,
    row,
)
from tests.agentauth.test_revalidation import (  # noqa: F401
    child_dispatch,
    engine,
    graph_context,
    session,
    session_factory,
    store,
    wave_context,
)
from tests.agentauth.test_revalidation import (
    queued_context as queued_context_fixture,
)

queued_context = queued_context_fixture


def human_context(**changes):
    return TokenContext(
        **{
            "user_id": CANONICAL_USER_ID,
            "org_id": TENANT_ID,
            "team_id": "team",
            "department_id": "dept",
            "account_type": "human",
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"account_type": "service"},
        {"auth_source": "iam"},
        {"org_id": ""},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
async def test_only_current_direct_human_sessions_can_authorize(changes):
    db = MagicMock(scalar=AsyncMock(return_value=CANONICAL_USER_ID))
    with pytest.raises(BootstrapRefusedError):
        await authorize_human_session(human_context(**changes), db)
    db.scalar.assert_not_called()


@pytest.fixture
async def human_queued(queued_context):
    ctx = queued_context
    async with ctx.session_factory() as db:
        user = await db.get(User, "human")
        assert user is not None
        user.is_shadow = False
        membership = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "human", TenantMembership.tenant_id == "tenant"))
        if membership is None:
            db.add(TenantMembership(user_id="human", tenant_id="tenant", is_active=True))
        else:
            membership.is_active = True
        await db.commit()
    return ctx


def human_request(ctx, **changes):
    raw = json.dumps({"command_id": COMMAND_ID}).encode()
    params = dict(
        tenant_id="tenant",
        principal="human",
        authority_kind="human_session",
        target_run_id=ctx.target_invocation,
        target_generation=1,
        action="pause",
        command_id=COMMAND_ID,
        request_body=raw,
        env=ctx.runtime.env,
    )
    params.update(changes)
    return {"action": "pause", "command_id": COMMAND_ID, "body_base64": base64.b64encode(raw).decode(), "envelope": sign_envelope(**params)}


async def test_human_queued_command_revalidates_without_delegated_initiator(human_queued, monkeypatch):
    ctx = human_queued
    from src.agentauth.human_control import require_live_human_membership, require_protected_human_owner

    require_protected_human_owner(ctx.store, user_id="human", tenant_id="tenant", run_id=ctx.target_invocation, generation=1, now=datetime.now(UTC))
    async with ctx.session_factory() as db:
        await require_live_human_membership(db, user_id="human", tenant_id="tenant")
    monkeypatch.setattr(ctx.child.service.policy, "require_supported", lambda action: None)
    body = human_request(ctx)
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True
    async with ctx.session_factory() as db:
        await db.execute(update(TenantMembership).where(TenantMembership.user_id == "human").values(is_active=False))
        await db.commit()
    # Revoking membership after acceptance invalidates the very same proof.
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    "changes",
    [
        {"principal": "other-human"},
        {"tenant_id": "other-tenant"},
        {"target_generation": 2},
        {"now": datetime.now(UTC) - timedelta(seconds=31)},
    ],
)
async def test_human_revalidation_refuses_wrong_bindings(human_queued, monkeypatch, changes):
    ctx = human_queued
    monkeypatch.setattr(ctx.child.service.policy, "require_supported", lambda action: None)
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=human_request(ctx, **changes), headers=ctx.target_headers)
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("orchestration", [False, True])
@pytest.mark.parametrize("attack", [None, "service", "wrong_owner", "wrong_tenant", "missing_key", "expired_session", "no_membership"])
def test_both_human_routes_send_exact_signed_bytes(orchestration, monkeypatch, attack):
    from src.agentauth.envelope import SIGNING_KEY_ENV
    from src.agentauth.execution import ExecutionStatus
    from tests.agentauth.test_envelope import _keypair

    pem, public = _keypair()
    env = {**ENV, SIGNING_KEY_ENV: pem, SIGNING_KEY_ID_ENV: "test-key"}
    service = make_service(
        items=[row(control_token_expires_at=(datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))],
        env=env,
        pod_status=202,
        pod_body={
            "state": "running",
            "command": {"command_id": COMMAND_ID, "action": "pause", "status": "pending"},
            "private_token": "must-not-leak",
        },
    )
    service._now = lambda: datetime.now(UTC)
    authority = MagicMock()
    authority.authority.load_execution.return_value = SimpleNamespace(
        invocation_id=RUN_ID,
        tenant_id=TENANT_ID,
        status=ExecutionStatus.ACTIVE,
        current_attempt=1,
    )
    authority.live_grant.return_value = SimpleNamespace(authority=SimpleNamespace(human_id=CANONICAL_USER_ID, org_id=TENANT_ID))
    authority._read.return_value = {"generation": {"N": "1"}}
    service._authority_store = authority
    monkeypatch.setattr("src.activity.control_service.SUPPORTED_ACTIONS", frozenset({"pause", "resume"}))
    db = MagicMock(scalar=AsyncMock(return_value=CANONICAL_USER_ID))
    raw = ('{ "command_id" : "' + COMMAND_ID + '", "reason": "hold" }').encode()
    context = human_context()
    if attack == "service":
        context.account_type = "service"
    elif attack == "wrong_owner":
        authority.live_grant.return_value.authority.human_id = "someone-else"
    elif attack == "wrong_tenant":
        authority.live_grant.return_value.authority.org_id = "someone-elses-tenant"
    elif attack == "missing_key":
        env.pop(SIGNING_KEY_ENV)
    elif attack == "expired_session":
        context.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif attack == "no_membership":
        # Identity mapping resolves twice; the current membership lookup refuses.
        db.scalar.side_effect = [CANONICAL_USER_ID, CANONICAL_USER_ID, None]
    with build_client(service, context, db, orchestration=orchestration) as client:
        response = client.post(command_path("pause", orchestration), content=raw, headers={"Content-Type": "application/json"})
    if attack is not None:
        assert response.status_code == 404, response.text
        service._http_client.request.assert_not_awaited()
        return
    assert response.status_code == 202, response.text
    assert response.json()["command_status"] == "pending"
    assert "private_token" not in response.text
    call = service._http_client.request.call_args
    assert call.kwargs["content"] == raw
    proof = verify_envelope(
        call.kwargs["headers"]["X-Adp-Control-Authorization"],
        public_keys={"test-key": public},
        expected_run_id=RUN_ID,
        expected_generation=1,
        expected_action="pause",
        expected_command_id=COMMAND_ID,
        request_body=raw,
    )
    assert proof.principal == CANONICAL_USER_ID
    assert proof.authority_kind == "human_session"
    assert proof.grant_id is None and proof.revocation_epoch is None


async def test_delegated_route_forwards_with_original_grant_epoch(queued_context, monkeypatch):
    import boto3
    import httpx

    from src.activity.control_service import ControlService
    from src.agentauth.composition import build_control_adapter
    from src.agentauth.envelope import _signing_key

    ctx = queued_context
    ctx.runtime.env.update({**ENV, "AGENT_CONTROL_CLUSTER_POD_CIDRS": "10.0.0.0/16", "WEBHOOK_EVENTS_TABLE": "events"})
    ctx.runtime._adapter = build_control_adapter(
        authority_table=ctx.store.table,
        events_table="events",
        dynamodb_client=ctx.store.client,
        env=ctx.runtime.env,
    )
    http = MagicMock(
        request=AsyncMock(
            return_value=httpx.Response(
                202,
                json={
                    "state": "running",
                    "command": {"command_id": COMMAND_ID, "action": "pause", "status": "pending"},
                },
            )
        )
    )
    service = ControlService(
        table=boto3.resource("dynamodb", region_name="us-east-1").Table("events"), http_client=http, authority_store=ctx.store, env=ctx.runtime.env
    )
    monkeypatch.setattr("src.activity.control_service.ControlService", lambda **kwargs: service)
    raw = json.dumps({"command_id": COMMAND_ID}).encode()
    response = await ctx.client.post(f"/internal/v1/agent/control/{ctx.target_invocation}/pause", content=raw, headers=ctx.headers)
    assert response.status_code == 202, response.text
    forwarded = http.request.call_args.kwargs
    assert forwarded["content"] == raw
    proof = verify_envelope(
        forwarded["headers"]["X-Adp-Control-Authorization"],
        public_keys={ctx.runtime.env[SIGNING_KEY_ID_ENV]: _signing_key(ctx.runtime.env).public_key()},
        expected_run_id=ctx.target_invocation,
        expected_generation=1,
        expected_action="pause",
        expected_command_id=COMMAND_ID,
        request_body=raw,
    )
    assert proof.authority_kind == "delegated_grant"
    assert proof.grant_id == ctx.control_grant.grant_id
    assert proof.revocation_epoch == ctx.control_grant.revocation_epoch


@pytest.mark.parametrize(
    "missing", ["AGENT_AUTHORITY_ENABLED", "AGENT_AUTHORITY_TABLE", "AGENT_CONTROL_ENVELOPE_SIGNING_KEY", "AGENT_CONTROL_ENVELOPE_KEY_ID"]
)
async def test_missing_authority_configuration_never_advertises_pause(missing):
    env = dict(ENV)
    env.pop(missing)
    service = make_service(
        items=[row()],
        env=env,
        pod_body={
            "state": "running",
            "capabilities": {"pause": True, "resume": True},
            "commands": [],
        },
    )
    state = await service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)
    assert not state.capabilities.pause and not state.capabilities.resume
    assert state.reason == "live control authorization is unavailable"


async def test_human_proof_expiring_during_membership_read_is_refused(human_queued, monkeypatch):
    from src.agentauth.human_control import require_live_human_membership

    ctx = human_queued
    body = human_request(ctx)
    later = datetime.now(UTC) + timedelta(seconds=31)

    class AfterRead(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    async def delayed_membership(*args, **kwargs):
        await require_live_human_membership(*args, **kwargs)
        monkeypatch.setattr("src.agentauth.revalidation.datetime", AfterRead)

    monkeypatch.setattr("src.agentauth.human_control.require_live_human_membership", delayed_membership)
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert response.status_code == 404, response.text
