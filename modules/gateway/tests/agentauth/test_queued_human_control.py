"""A real 14-minute CI wait retains intent, never expired delivery privilege."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import update

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.envelope import SIGNING_KEY_ID_ENV, EnvelopeError, _signing_key, verify_envelope
from src.agentauth.human_control import HumanControlSession
from src.agentauth.queued_human_control import _key, issue_queued_steer
from src.shared.models.onboarding import TenantMembership
from src.shared.models.token import Token
from tests.agentauth.test_human_control import (  # noqa: F401
    child_dispatch,
    engine,
    graph_context,
    queued_context,
    session,
    session_factory,
    store,
    wave_context,
)
from tests.agentauth.test_human_control import human_queued as human_queued_fixture

human_queued = human_queued_fixture


def queued(ctx, *, principal="human", generation=1, lifetime=3600, token_id="", ago=840):
    issued = datetime.now(UTC) - timedelta(seconds=ago)
    human = HumanControlSession(principal, "tenant", issued + timedelta(seconds=lifetime), token_id)
    raw = b'{"command_id":"original-steer","instruction":"apply the retained Python fix"}'
    envelope = issue_queued_steer(
        ctx.store,
        session=human,
        run_id=ctx.target_invocation,
        generation=generation,
        command_id="original-steer",
        raw=raw,
        env=ctx.runtime.env,
        now=issued,
    )
    proof = verify_envelope(
        envelope,
        public_keys={ctx.runtime.env[SIGNING_KEY_ID_ENV]: _signing_key(ctx.runtime.env).public_key()},
        expected_run_id=ctx.target_invocation,
        expected_generation=generation,
        expected_action="steer",
        expected_command_id="original-steer",
        request_body=raw,
        now=issued,
    )
    return (
        {
            "queued_grant_ref": proof.queued_grant_ref,
            "action": "steer",
            "command_id": "original-steer",
            "body_base64": base64.b64encode(raw).decode(),
        },
        envelope,
        human,
        raw,
    )


async def test_fourteen_minute_wait_uses_original_session_and_consumes_once(human_queued):
    ctx = human_queued
    body, envelope, human, raw = queued(ctx)
    with pytest.raises(EnvelopeError, match="expired"):
        verify_envelope(
            envelope,
            public_keys={ctx.runtime.env[SIGNING_KEY_ID_ENV]: _signing_key(ctx.runtime.env).public_key()},
            expected_run_id=ctx.target_invocation,
            expected_generation=1,
            expected_action="steer",
            expected_command_id="original-steer",
            request_body=raw,
        )
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"allowed": True, "command_id": "original-steer", "generation": 1, "max_round_trip_ms": 1000, "delivery_consumed": True}
    # A lost allowed response cannot turn a retry into a second delivery decision.
    repeat = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert repeat.status_code == 404, repeat.text
    key = _key("tenant", ctx.target_invocation, 1, "original-steer")
    retained = ctx.store._read(key["pk"]["S"], key["sk"]["S"])
    assert retained["session_expires_at"] == {"S": human.expires_at.isoformat()}
    assert retained["consumed"] == {"BOOL": True}
    assert raw.decode() not in json.dumps(retained)
    assert envelope not in json.dumps(retained)


@pytest.mark.parametrize("envelope_first", [False, True])
async def test_old_worker_fresh_envelope_path_consumes_the_same_queued_grant(human_queued, envelope_first):
    ctx = human_queued
    body, envelope, _, _ = queued(ctx, ago=0)
    legacy = {k: v for k, v in body.items() if k != "queued_grant_ref"}
    legacy["envelope"] = envelope
    first = legacy if envelope_first else body
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=first, headers=ctx.target_headers)
    assert response.status_code == 200, response.text
    assert response.json()["delivery_consumed"] is True
    repeat = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert repeat.status_code == 404, repeat.text
    legacy_repeat = await ctx.client.post("/internal/v1/agent/revalidate", json=legacy, headers=ctx.target_headers)
    assert legacy_repeat.status_code == 404, legacy_repeat.text


async def test_expired_new_format_envelope_cannot_enter_compatibility_delivery(human_queued):
    ctx = human_queued
    body, envelope, _, _ = queued(ctx)
    legacy = {k: v for k, v in body.items() if k != "queued_grant_ref"}
    legacy["envelope"] = envelope
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=legacy, headers=ctx.target_headers)
    assert response.status_code == 404, response.text
    # Expired-proof refusal neither consumes nor creates queue authority.
    original = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert original.status_code == 200, original.text


@pytest.mark.parametrize(
    "attack",
    [
        "expired",
        "revoked_membership",
        "revoked_target_grant",
        "changed_target_epoch",
        "stale_generation",
        "cross_principal",
        "wrong_worker",
        "changed_body",
        "wrong_reference",
        "expired_envelope",
    ],
)
async def test_delayed_steer_refuses_lost_authority(human_queued, attack):
    ctx = human_queued
    body, envelope, _, _ = queued(
        ctx,
        principal="other-human" if attack == "cross_principal" else "human",
        generation=2 if attack == "stale_generation" else 1,
        lifetime=60 if attack == "expired" else 3600,
    )
    headers = ctx.target_headers
    if attack == "revoked_membership":
        async with ctx.session_factory() as db:
            await db.execute(update(TenantMembership).where(TenantMembership.user_id == "human").values(revoked_at=datetime.now(UTC)))
            await db.commit()
    elif attack == "revoked_target_grant":
        grant = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.target_invocation}#1")
        grant["revoked"] = {"BOOL": True}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
    elif attack == "changed_target_epoch":
        grant = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.target_invocation}#1")
        grant["revocation_epoch"] = {"N": str(int(grant["revocation_epoch"]["N"]) + 1)}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
    elif attack == "wrong_worker":
        headers = ctx.headers
    elif attack == "changed_body":
        body["body_base64"] = base64.b64encode(b'{"command_id":"original-steer","instruction":"different"}').decode()
    elif attack == "wrong_reference":
        body["queued_grant_ref"] = "0" * 64
    elif attack == "expired_envelope":
        # No branch ever accepts an expired envelope as an alternative authority.
        body["envelope"] = envelope
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=headers)
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("revoked", [False, True])
async def test_legacy_original_token_revocation_is_preserved(human_queued, revoked):
    ctx = human_queued
    async with ctx.session_factory() as db:
        db.add(
            Token(
                id="original-token",
                org_id="tenant",
                token_hash="a" * 64,
                entity_type="user",
                entity_id="human",
                team_id="team",
                department_id="dept",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                revoked_at=datetime.now(UTC) if revoked else None,
            )
        )
        await db.commit()
    body, _, _, _ = queued(ctx, token_id="original-token")
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert response.status_code == (404 if revoked else 200), response.text


async def test_new_login_cannot_extend_existing_command_authority(human_queued):
    ctx = human_queued
    _, _, original, raw = queued(ctx)
    renewed = HumanControlSession("human", "tenant", original.expires_at + timedelta(hours=1))
    with pytest.raises(BootstrapRefusedError, match="cannot be replaced"):
        issue_queued_steer(
            ctx.store,
            session=renewed,
            run_id=ctx.target_invocation,
            generation=1,
            command_id="original-steer",
            raw=raw,
            env=ctx.runtime.env,
            now=datetime.now(UTC),
        )


async def test_expired_session_cannot_establish_queued_grant(human_queued):
    ctx = human_queued
    with pytest.raises(EnvelopeError):
        queued(ctx, lifetime=-1, ago=0)
    key = _key("tenant", ctx.target_invocation, 1, "original-steer")
    assert ctx.store._read(key["pk"]["S"], key["sk"]["S"]) is None


async def test_concurrent_delivery_revalidation_consumes_original_grant_once(human_queued):
    ctx = human_queued
    body, _, _, _ = queued(ctx)
    results = await asyncio.gather(*(ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers) for _ in range(2)))
    assert sorted(response.status_code for response in results) == [200, 404]


@pytest.mark.parametrize("remaining_ms", [50, 1000, 1500])
async def test_delivery_reserves_full_worker_window_before_original_session_expiry(human_queued, monkeypatch, remaining_ms):
    ctx = human_queued
    body, _, original, _ = queued(ctx)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return original.expires_at - timedelta(milliseconds=remaining_ms)

    monkeypatch.setattr("src.agentauth.queued_human_control.datetime", Clock)
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert response.status_code == (200 if remaining_ms > 1000 else 404), response.text


async def test_real_legacy_authentication_to_command_preserves_original_revocation_record(human_queued):
    from src.activity.control_service import ControlService
    from src.agentauth.human_control import authorize_human_session
    from src.auth.schemas import TenantInfo
    from src.auth.token_manager import TokenManager

    ctx = human_queued
    manager = TokenManager("test-only-signing-key-that-is-at-least-32-bytes")
    tenant = TenantInfo(org_id="tenant", org_name="Tenant", department_id="dept", team_id="team", account_type="human", entity_id="human")
    token, expiry = manager.generate_token(tenant, duration_hours=1)
    async with ctx.session_factory() as db:
        token_id = await manager.store_token(token, tenant, expiry, db)
        context = await manager.validate_token(token, db)
        human = await authorize_human_session(context, db)
    assert human.token_record_id == token_id
    assert "_authenticated_token_record_id" not in context.model_dump()

    service = ControlService(authority_store=ctx.store, env=ctx.runtime.env)
    service.authorize_command = Mock(return_value=SimpleNamespace(run_id=ctx.target_invocation, generation=1))
    service.forward_command = AsyncMock(return_value=("forwarded", 202))
    command_id = "2a4a1772-b6ac-4b61-9fc8-933d069f7ce5"
    raw = json.dumps({"command_id": command_id, "instruction": "apply the retained fix"}).encode()
    await service.command(ctx.target_invocation, "steer", request_body=raw, session=human)
    forwarded = service.forward_command.call_args.kwargs["envelope"]
    proof = verify_envelope(
        forwarded,
        public_keys={ctx.runtime.env[SIGNING_KEY_ID_ENV]: _signing_key(ctx.runtime.env).public_key()},
        expected_run_id=ctx.target_invocation,
        expected_generation=1,
        expected_action="steer",
        expected_command_id=command_id,
        request_body=raw,
    )
    key = _key("tenant", ctx.target_invocation, 1, command_id)
    row = ctx.store._read(key["pk"]["S"], key["sk"]["S"])
    assert row["session_token_record_id"] == {"S": token_id}
    assert token not in json.dumps(row)
    async with ctx.session_factory() as db:
        assert await manager.revoke_token(token, db)
    response = await ctx.client.post(
        "/internal/v1/agent/revalidate",
        headers=ctx.target_headers,
        json={
            "queued_grant_ref": proof.queued_grant_ref,
            "action": "steer",
            "command_id": command_id,
            "body_base64": base64.b64encode(raw).decode(),
        },
    )
    assert response.status_code == 404, response.text
