"""Expired AWS source evidence can be refreshed without weakening its authority."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.internal.sts_assume_service import STSAssumeError
from src.products.models import ProductSourceBinding
from src.products.verification import observe_binding
from tests.products.test_i08_api import mutation
from tests.products.test_i10 import setup_aws


async def setup_environment(test_client, db_session, app, source_member, monkeypatch):
    path, selection, credential, secret = await setup_aws(test_client, db_session, app, source_member, monkeypatch)
    monkeypatch.setattr("src.auth.vault_routes.get_secrets_manager", lambda: secret)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_environment_refresh_123"))
    assert created.status_code == 201, created.text
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    old = datetime.now(UTC) - timedelta(hours=1)
    binding.observed = {**binding.observed, "observed_at": old.isoformat(), "stale_after": (old + timedelta(minutes=15)).isoformat()}
    await db_session.commit()
    calls = []

    def assume(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(assumed_role_arn=f"arn:aws:sts::{secret.account}:assumed-role/ADP-Agent-Role/verified")

    monkeypatch.setattr("src.products.aws_environment_verification.assume_role", assume)
    return path, binding, credential, secret, calls


@pytest.mark.asyncio
async def test_expired_aws_binding_refreshes_through_mounted_route(test_client, db_session, app, source_member, monkeypatch):
    path, binding, credential, secret, calls = await setup_environment(test_client, db_session, app, source_member, monkeypatch)
    assert (await test_client.get(path)).json()["items"][0]["status"] == "degraded"
    old_observation = binding.observed.copy()
    url = f"{path}/{binding.id}/verify"
    headers = mutation(2, "refresh_environment_12345")
    result = await test_client.post(url, headers=headers)
    assert result.status_code == 202, result.text
    assert result.json()["status"] == "succeeded"
    readback = (await test_client.get(path)).json()["items"][0]
    assert readback["status"] == "healthy"
    assert readback["source_revision"] == secret.version
    assert readback["observed_at"] > old_observation["observed_at"]
    assert datetime.fromisoformat(readback["stale_after"]) - datetime.fromisoformat(readback["observed_at"]) == timedelta(minutes=15)
    assert readback["verified_environment"]["permission_tier"] == "unknown"
    assert calls[0]["user_id"] == credential.user_id
    assert calls[0]["role_arn"] == secret.role
    assert calls[0]["external_id"] == credential.aws_external_id
    assert calls[0]["task_id"] == f"binding-{binding.id}"
    policy = json.loads(calls[0]["session_policy"])["Statement"][0]
    assert policy == {"Effect": "Deny", "NotAction": "sts:GetCallerIdentity", "Resource": "*"}
    assert (await test_client.post(url, headers=headers)).json() == result.json()
    assert len(calls) == 1
    assert "external_id" not in json.dumps(readback)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rotated_before", "rotated_after", "wrong_role", "denied", "throttled"])
async def test_failed_aws_refresh_never_replaces_source_evidence(test_client, db_session, app, source_member, monkeypatch, failure):
    path, binding, credential, secret, calls = await setup_environment(test_client, db_session, app, source_member, monkeypatch)
    binding_id, old_observation = binding.id, binding.observed.copy()
    if failure == "rotated_before":
        secret.version = "replacement-version"
    else:

        def failing_assume(**kwargs):
            calls.append(kwargs)
            if failure == "rotated_after":
                secret.version = "replacement-version"
                return SimpleNamespace(assumed_role_arn=f"arn:aws:sts::{secret.account}:assumed-role/ADP-Agent-Role/verified")
            if failure == "wrong_role":
                return SimpleNamespace(assumed_role_arn=f"arn:aws:sts::{secret.account}:assumed-role/OtherRole/verified")
            raise STSAssumeError("Provider response", code="AccessDenied" if failure == "denied" else "Throttling")

        monkeypatch.setattr("src.products.aws_environment_verification.assume_role", failing_assume)
    result = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "failed_environment_refresh_123"))
    assert result.status_code == 202, result.text
    assert result.json()["status"] == "failed"
    assert result.json()["coverage"] == ("denied" if failure == "denied" else "unavailable")
    db_session.expire_all()
    stored = await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))
    assert stored.observed == old_observation
    assert len(calls) == (0 if failure == "rotated_before" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["connection", "tenant", "account", "region"])
async def test_unmatched_aws_selection_never_assumes_role(test_client, db_session, app, source_member, monkeypatch, change):
    _, binding, credential, _, calls = await setup_environment(test_client, db_session, app, source_member, monkeypatch)
    if change == "connection":
        binding.credential_id = "different-connection"
    elif change == "tenant":
        binding.tenant_id = "different-tenant"
    elif change == "account":
        binding.namespace = "111111111111"
    else:
        binding.native_id = "eu-west-2"
    result = await observe_binding(binding, credential)
    assert result["coverage"] == "denied" and "observed" not in result
    assert calls == []


@pytest.mark.asyncio
async def test_new_connection_generation_fences_late_aws_readback(test_client, db_session, app, source_member, monkeypatch):
    path, binding, credential, _, calls = await setup_environment(test_client, db_session, app, source_member, monkeypatch)
    binding_id, old_observation = binding.id, binding.observed.copy()

    async def changed_after_read(selected, connection):
        result = await observe_binding(selected, connection)
        assert result["coverage"] == "complete"
        credential.aws_verification_attempt = "new-verification-generation"
        await db_session.commit()
        return result

    monkeypatch.setattr("src.products.source_routes.observe_binding", changed_after_read)
    result = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "changed_environment_refresh_123"))
    assert result.status_code == 202, result.text
    assert result.json()["status"] == "cancelled"
    db_session.expire_all()
    assert (await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))).observed == old_observation
    assert len(calls) == 1
