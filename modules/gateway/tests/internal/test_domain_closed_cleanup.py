# ruff: noqa: F811 -- Shared provider fixture is injected by pytest.
"""Closed cleanup shares current identity checks without relaxing paid delivery."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.internal import domain_closed_cleanup_authority as cleanup
from src.internal import domain_provider_authority as provider
from src.shared.domain_provider_contract import digest
from tests.internal.test_domain_provider_authority import context  # noqa: F401


@pytest.fixture
def closed(context, monkeypatch):
    context.value["expires_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    permit = {
        "permit_id": "sp-cleanup:" + "a" * 64,
        "authority": copy.deepcopy(context.value),
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "operation": {"operation_id": "closed-operation"},
    }
    current = {"permit": permit}
    monkeypatch.setattr(cleanup, "read_permit", lambda key: current["permit"])
    monkeypatch.setattr(cleanup, "aws_client", lambda service: context.store)
    authority = cleanup.ServiceAuthority(permit, context.db, context.sm, AsyncMock())
    authority.record_session = AsyncMock()
    return authority, current


async def test_expired_enrollment_is_identity_only_normal_delivery_refuses(context, closed):
    service, _ = closed
    await service.revalidate()
    with pytest.raises(HTTPException):
        await provider.resolve(context.db, context.sm, context.value["credential_id"], subject="subject")
    assert context.db.commit.await_count == 0
    assert service.baseline["expires_at"] == context.value["expires_at"]


@pytest.mark.parametrize("drift", ["permit-revoked", "permit-replaced", "enrollment-revoked", "policy", "human", "membership", "secret"])
async def test_current_identity_and_both_protected_records(context, closed, monkeypatch, drift):
    service, current = closed
    await service.revalidate()
    if drift == "permit-revoked":
        current["permit"] = {**service.permit, "status": "revoked"}
    elif drift == "permit-replaced":
        current["permit"] = {**service.permit, "expires_at": "2099-01-01T00:00:00+00:00"}
    elif drift == "enrollment-revoked":
        context.value["status"] = "revoked"
    elif drift == "policy":
        monkeypatch.setattr(provider, "policy_identity", lambda *args: ("replacement", "b" * 64))
    elif drift == "human":
        context.identity["enabled"] = False
    elif drift == "membership":
        context.identity["membership_id"] = "replacement"
    else:
        context.sm.current_version_id = lambda arn: "replacement"
    with pytest.raises(HTTPException):
        await service.revalidate()


@pytest.mark.parametrize("revoke", [False, True])
async def test_post_sts_revocation_and_cached_authority(context, closed, monkeypatch, revoke):
    service, current = closed
    baseline = service.baseline

    def assume(**kwargs):
        assert kwargs["session_duration_seconds"] == 900 and kwargs["agent_id"] == "superplane-operation"
        assert kwargs["task_id"] == "closed-operation"
        if revoke:
            current["permit"] = {**service.permit, "status": "revoked"}
        name = "native-cleanup"
        return SimpleNamespace(
            access_key_id="synthetic-access",
            secret_access_key="synthetic-secret",
            session_token="synthetic-token",
            expiration=(datetime.now(UTC) + timedelta(seconds=899)).isoformat(),
            assumed_role_arn=baseline["role_arn"].replace(":iam:", ":sts:").replace(":role/", ":assumed-role/") + "/" + name,
            assumed_role_id=baseline["role_id"] + ":" + name,
        )

    monkeypatch.setattr(cleanup, "assume_role", assume)
    policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"}]}
    if revoke:
        with pytest.raises(HTTPException):
            await service.provider(policy)
        assert not service.sessions
    else:
        session = await service.provider(policy)
        assert not isinstance(session, dict) and session.get_credentials().access_key == "synthetic-access"
        service.record_session.assert_awaited_once()
        current["permit"] = {**service.permit, "status": "revoked"}
        with pytest.raises(HTTPException):
            await service.provider(policy)
    assert context.db.commit.await_count == 0


def test_protected_reader_compares_record_and_running_source(monkeypatch):
    cleanup.native_paths()
    from superplane_bootstrap import closed_cleanup_permit

    p = {"permit_id": "sp-cleanup:" + "a" * 64, "service_source_sha256": "b" * 64}
    monkeypatch.setattr(closed_cleanup_permit, "validate", lambda value: value)
    monkeypatch.setattr(cleanup, "source_digest", lambda: "b" * 64)
    monkeypatch.setattr(provider, "tables", lambda: ("authority", "evidence"))
    row = {"record_id": {"S": p["permit_id"]}, "document": {"S": closed_cleanup_permit.encode_document(p)}, "revision": {"S": digest(p)}}
    monkeypatch.setattr(cleanup, "aws_client", lambda service: SimpleNamespace(get_item=lambda **kw: {"Item": row}))
    assert cleanup.read_permit(p["permit_id"]) == p
    monkeypatch.setattr(cleanup, "source_digest", lambda: "c" * 64)
    with pytest.raises(HTTPException):
        cleanup.read_permit(p["permit_id"])
    row["revision"]["S"] = "forged"
    with pytest.raises(HTTPException):
        cleanup.read_permit(p["permit_id"])


def test_verifier_iam_scopes_separate_tagging_observation_and_immutable_deletion(monkeypatch):
    from superplane_bootstrap.closed_cleanup_permit import VIEW_POLICY

    from src.internal import domain_closed_cleanup as command

    p = {
        "permit_id": "sp-cleanup:" + "a" * 64,
        "authority": {
            "account_id": "000000000000",
            "role_arn": "arn:aws:iam::000000000000:role/provider",
            "org_id": "org",
            "workspace_id": "workspace",
        },
        "qualification": {"cluster_arn": "arn:aws:eks:us-east-1:000000000000:cluster/test", "generation": "original"},
    }
    scopes, methods = [], []

    class Session:
        def client(self, *args, **kw):
            return SimpleNamespace(create_access_entry=lambda **kw: methods.append("create"), list_associated_access_policies=lambda **kw: {})

    def registrar(scope):
        scopes.append(scope)
        return Session()

    authority = SimpleNamespace(registrar=registrar, revalidate=lambda: None)
    reader = SimpleNamespace(describe_access_entry=lambda **kw: {})
    monkeypatch.setattr(command, "tables", lambda: ("authority", "evidence"))
    monkeypatch.setattr(command.stores, "aws_client", lambda service: object())
    verifier = command.make_verifier(p, authority, lambda value: value, reader)
    verifier.client.create_access_entry()
    assert methods == ["create"]
    assert scopes[0]["Statement"][0]["Condition"]["Null"]["eks:kubernetesGroups"] == "true"
    assert scopes[0]["Statement"][1]["Action"] == ["eks:TagResource"]
    verifier.observer.list_associated_access_policies()
    assert scopes[1]["Statement"][0]["Action"] == ["eks:ListAssociatedAccessPolicies"]
    arn = "arn:aws:eks:us-east-1:000000000000:access-entry/test/role/000000000000/provider/immutable-uuid"
    verifier.entry_client(arn)
    assert all(statement["Resource"] == arn for statement in scopes[2]["Statement"])
    by_action = {tuple(statement["Action"]): statement for statement in scopes[2]["Statement"]}
    assert set(by_action) == {("eks:AssociateAccessPolicy",), ("eks:DisassociateAccessPolicy",), ("eks:DeleteAccessEntry",)}
    assert by_action[("eks:AssociateAccessPolicy",)]["Condition"] == {"StringEquals": {"eks:policyArn": VIEW_POLICY, "eks:accessScope": "cluster"}}
    # The disassociation API carries policyArn but no accessScope. An IAM
    # StringEquals on the absent key would deny this independent teardown.
    assert by_action[("eks:DisassociateAccessPolicy",)]["Condition"] == {"StringEquals": {"eks:policyArn": VIEW_POLICY}}
    assert "Condition" not in by_action[("eks:DeleteAccessEntry",)]
    with pytest.raises(HTTPException):
        verifier.entry_client(arn.replace("/provider/", "/foreign/"))
    assert len(scopes) == 3
