"""Queued controls recheck both workers and live authority through the HTTP route."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import update

from src.agentauth.envelope import SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV, sign_envelope
from src.agentauth.registration import AgentRegistrationService
from src.orchestration.models import OrchestrationFlow
from tests.agentauth.test_wave_dispatch import (  # noqa: F401
    child_dispatch,
    engine,
    graph_context,
    session,
    session_factory,
    start_wave,
    store,
)
from tests.agentauth.test_wave_dispatch import (
    wave_context as wave_context_fixture,
)

wave_context = wave_context_fixture


@pytest.fixture
async def queued_context(wave_context):
    ctx = wave_context
    launch, headers = await start_wave(ctx)
    ctx.target_invocation = launch.json()["invocation_id"]
    ctx.target_headers = headers
    key = Ed25519PrivateKey.generate()
    ctx.runtime.env.update(
        {
            SIGNING_KEY_ENV: key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
            SIGNING_KEY_ID_ENV: "test-key",
        }
    )
    # Set up an explicitly delegated control for testing the dormant path.
    grant = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")
    grant["allowed_actions"]["SS"].append("pause")
    ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
    registration = AgentRegistrationService(
        policy=ctx.child.service.policy, authority_table=ctx.store.table, events_table="events", dynamodb_client=ctx.store.client, env=ctx.runtime.env
    )
    pod = ctx.runtime.workloads.verify(headers["X-Adp-Workload-Token"])
    registration.register_control(
        credential_token=headers["X-Adp-Run-Credential"],
        pod=pod,
        token="a" * 64,
        token_expires_at=(datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    ctx.control_grant = ctx.store.live_grant(invocation_id=ctx.child.invocation, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    return ctx


def request(ctx, *, epoch=None, now=None):
    raw = json.dumps({"command_id": "queued-pause"}).encode()
    grant = ctx.control_grant
    return {
        "action": "pause",
        "command_id": "queued-pause",
        "body_base64": base64.b64encode(raw).decode(),
        "envelope": sign_envelope(
            tenant_id="tenant",
            principal=f"{ctx.child.invocation}#1",
            target_run_id=ctx.target_invocation,
            target_generation=1,
            action="pause",
            command_id="queued-pause",
            request_body=raw,
            grant_id=grant.grant_id,
            revocation_epoch=epoch or grant.revocation_epoch,
            flow_id=grant.flow_id,
            authority_reference_id=grant.authority.reference_id,
            env=ctx.runtime.env,
            now=now,
        ),
    }


async def test_online_revalidation_and_unsupported_runtime(queued_context, monkeypatch):
    ctx = queued_context
    body = request(ctx)
    # The deployed runtime still cannot claim a live control succeeded.
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert response.status_code == 501, response.text
    monkeypatch.setattr(ctx.child.service.policy, "require_supported", lambda action: None)
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=ctx.target_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"allowed": True, "command_id": "queued-pause", "generation": 1, "max_round_trip_ms": 1000}


@pytest.mark.parametrize("attack", ["future_epoch", "expired", "changed_body", "revoked", "cancelled", "wrong_worker", "forged"])
async def test_queued_revocation_and_attack_refusals(queued_context, monkeypatch, attack):
    ctx = queued_context
    monkeypatch.setattr(ctx.child.service.policy, "require_supported", lambda action: None)
    body = request(
        ctx, epoch=999 if attack == "future_epoch" else None, now=datetime.now(UTC) - timedelta(seconds=31) if attack == "expired" else None
    )
    headers = ctx.target_headers
    if attack == "changed_body":
        body["body_base64"] = base64.b64encode(b'{"command_id":"queued-pause","reason":"changed"}').decode()
    elif attack == "revoked":
        row = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")
        row["revoked"] = {"BOOL": True}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=row)
    elif attack == "cancelled":
        async with ctx.session_factory() as sql:
            await sql.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).values(state="halted"))
            await sql.commit()
    elif attack == "wrong_worker":
        headers = ctx.headers
    elif attack == "forged":
        body["envelope"] = body["envelope"][:-8] + "AAAAAAAA"
    response = await ctx.client.post("/internal/v1/agent/revalidate", json=body, headers=headers)
    assert response.status_code == 404, response.text
