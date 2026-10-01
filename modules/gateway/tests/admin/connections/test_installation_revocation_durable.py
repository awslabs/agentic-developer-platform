"""Durable installation revocation: real SQL, Moto, and production readers."""

import hashlib
import hmac
import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException, Request
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import src.admin.connections.service as connections
import src.admin.installations.revocation as revocation
from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.identity.user_identity_index import UserIdentityIndexClient
from src.admin.identity_index import IdentityIndexClient
from src.admin.installations.guards import InstallationClaimError
from src.admin.org_connections.schemas import GitHubConnectionAttachRequest
from src.admin.org_connections.service import OrgConnectionsService
from src.internal.routes import ResolveInstallationRequest, resolve_installation
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import ChannelTenantMap, InstallationRevocation, UserIdentity

INSTALL = 123456
OTHER = 654321
ORG = "org-acme"


@pytest.fixture
def projection(monkeypatch):
    monkeypatch.setenv("IDENTITY_INDEX_TABLE", "inert-installations")
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "false")
    monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "true")
    monkeypatch.setattr(connections, "_get_github_app_credentials", lambda: ("", ""))
    monkeypatch.setattr("src.admin.identity_index.MAX_RETRIES", 1)
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="inert-installations",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "identity_type", "KeyType": "HASH"}, {"AttributeName": "identity_value", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "identity_type", "AttributeType": "S"},
                {"AttributeName": "identity_value", "AttributeType": "S"},
            ],
        )
        index = IdentityIndexClient("inert-installations", client)
        writer = IdentityIndexWriter(client=index, user_identity_client=UserIdentityIndexClient("inert-users", client))
        monkeypatch.setattr("src.admin.identity_index.IdentityIndexClient", lambda: index)
        monkeypatch.setattr(revocation, "IdentityIndexClient", lambda: index)
        monkeypatch.setattr("src.admin.identity.identity_index_writer.IdentityIndexWriter", lambda: writer)
        yield SimpleNamespace(client=client, index=index, writer=writer)


@pytest.fixture
def webhook(monkeypatch, projection):
    def owned(name):
        return name in {"common", "github"} or name.startswith(("common.", "github."))

    saved = {name: value for name, value in sys.modules.items() if owned(name)}
    for name in saved:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "agent-factory/webhook-ingress/lambda"))
    try:
        resolver = importlib.import_module("common.identity_resolver")
        reverse = importlib.import_module("common.installation_resolver")
        authority = importlib.import_module("common.agent_authority")
        gateway = importlib.import_module("common.gateway_client")
        negative = importlib.import_module("common.negative_cache")
        handler = importlib.import_module("github.handler")
        monkeypatch.setattr(gateway, "resolve_user_state", lambda *args, **kwargs: {"state": "error", "reason": "inert-unavailable"})
        monkeypatch.setattr(handler, "_webhook_secret", "inert-webhook-secret")
        monkeypatch.setattr(handler, "_log_outcome", MagicMock())
        monkeypatch.setattr(handler, "_emit_metric", MagicMock())
        monkeypatch.setattr(handler, "_auto_provision_tenant_github_app_secret", MagicMock())
        yield SimpleNamespace(resolver=resolver, reverse=reverse, authority=authority, gateway=gateway, handler=handler, negative=negative)
    finally:
        for name in list(sys.modules):
            if owned(name):
                del sys.modules[name]
        sys.modules.update(saved)


async def _seed(db, projection, *, org_assertion=True, mapped=True, other=False):
    ids = [str(INSTALL)] if org_assertion else []
    if other:
        ids.append(str(OTHER))
    db.add_all(
        [
            Organization(
                id=ORG,
                name="Acme",
                aws_accounts=[],
                settings={},
                github_installation_ids=ids,
                cognito_client_ids=[],
                github_org_id="999",
                github_app_id="app-1",
            ),
            Department(id="dept", org_id=ORG, name="Eng"),
            Team(id="team", org_id=ORG, department_id="dept", name="Eng"),
            User(id="installer", org_id=ORG, team_id="team", email="installer@example.com", role="member"),
            UserIdentity(
                user_id="installer",
                org_id=ORG,
                team_id="team",
                provider="github",
                provider_user_id="777",
                verification_method="oauth",
                verified_at=datetime.now(UTC),
            ),
        ]
    )
    if mapped:
        db.add(
            ChannelTenantMap(provider="github", org_id=ORG, provider_scope_id="999", installation_id=str(INSTALL), installed_by_user_id="installer")
        )
    if other:
        db.add(ChannelTenantMap(provider="github", org_id=ORG, provider_scope_id="888", installation_id=str(OTHER), installed_by_user_id="installer"))
    await db.commit()
    await projection.index.put_identity("github_installation_id", str(INSTALL), ORG)
    if other:
        await projection.index.put_identity("github_installation_id", str(OTHER), ORG)
    await projection.index.write_reverse_installation_identity(ORG, INSTALL)
    await projection.index.update_user_identity_core("777", "installer", ORG, verification_method="oauth")


def _provider(*, fails=False):
    return SimpleNamespace(delete_installation=AsyncMock(side_effect=RuntimeError("inert provider timeout") if fails else None))


async def _disconnect(db, provider=None, *, admin=False):
    return await connections.delete_connection(
        installation_id=INSTALL,
        caller_org_id=ORG,
        db=db,
        github_client=provider or _provider(),
        caller_user_id="installer",
        caller_is_admin=admin,
    )


async def _canonical(db):
    request = Request({"type": "http", "headers": []})
    request.state.token_context = SimpleNamespace(
        user_id="iam-agent:ingress",
        auth_source="iam",
        scope="internal",
        org_id=ORG,
        credential_scopes=["internal:installation:resolve", "internal:cross-tenant"],
    )
    try:
        answer = await resolve_installation(ResolveInstallationRequest(installation_id=str(INSTALL)), request=request, db=db, _=None)
        return {"state": "resolved", "tenant_id": answer.tenant_id, "created_via": answer.created_via, "revocation_checked": True}
    except HTTPException as exc:
        assert exc.status_code in (404, 410)
        return {"state": "revoked" if exc.status_code == 410 else "not_found"}


def _event(webhook):
    resolved, reason = webhook.resolver.resolve(INSTALL, 777)
    if resolved is None:
        assert reason in {"unknown_installation", "installation_revoked", "installation_unavailable"}
        return None
    assert reason == "ok"
    return webhook.authority.VerifiedHumanEvent.from_verified_webhook(
        body=b"{}",
        event_type="issue_comment",
        resolved=resolved,
        sender={"type": "User"},
        tenant_id=ORG,
        repo="inert/repo",
    )


def _fail_delete(projection, monkeypatch, identity_type):
    original = projection.client.delete_item

    def delete_item(**kwargs):
        if kwargs["Key"]["identity_type"]["S"] == identity_type:
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "inert DDB failure"}}, "DeleteItem")
        return original(**kwargs)

    monkeypatch.setattr(projection.client, "delete_item", delete_item)


@pytest.mark.parametrize("mode", ["normal", "map_only", "forward_delete_failure"])
async def test_successful_disconnect_stops_real_webhook_authority(db_session, projection, webhook, monkeypatch, mode):
    await _seed(db_session, projection, org_assertion=mode != "map_only")
    if mode == "forward_delete_failure":
        _fail_delete(projection, monkeypatch, "github_installation_id")
    result = await _disconnect(db_session)
    canonical = await _canonical(db_session)
    assert canonical == {"state": "revoked"}
    monkeypatch.setattr(webhook.gateway, "resolve_installation_by_id", lambda *args: canonical)
    event = _event(webhook)
    reverse = webhook.reverse.resolve_installation_for_tenant(ORG)
    print(f"mode={mode} response={result.model_dump()} canonical={canonical} authority={event} reverse={reverse}")
    assert event is None, "Disconnected installation still grants protected human authority despite canonical 404"
    assert reverse is None, "Forward-scan fallback recreated the revoked reverse mapping"


async def test_failed_forward_cleanup_is_reported_by_real_writer(db_session, projection, monkeypatch):
    await _seed(db_session, projection)
    _fail_delete(projection, monkeypatch, "github_installation_id")
    result = await _disconnect(db_session)
    assert await projection.index.get_installation_identity(INSTALL) is not None
    assert "identity_index_forward_row" in result.residual


async def test_reverse_cleanup_does_not_depend_on_fallible_read(db_session, projection, monkeypatch):
    await _seed(db_session, projection)
    original = projection.client.get_item

    def get_item(**kwargs):
        if kwargs["Key"]["identity_type"]["S"] == "org_installation":
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "inert reverse read failure"}}, "GetItem")
        return original(**kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(projection.client, "get_item", get_item)
        result = await _disconnect(db_session)
    assert await projection.index.get_reverse_installation_identity(ORG) is None
    assert result.residual == []


@pytest.mark.parametrize("boundary", ["after_sql_commit", "reverse_delete_failure"])
async def test_original_installer_can_resume_cleanup_after_restart(db_session, db_engine, projection, monkeypatch, boundary):
    await _seed(db_session, projection)
    if boundary == "after_sql_commit":
        with patch.object(revocation, "_complete_revocation", AsyncMock(side_effect=RuntimeError("inert process crash"))):
            with pytest.raises(RuntimeError, match="inert process crash"):
                await _disconnect(db_session)
    else:
        with monkeypatch.context() as failure:
            _fail_delete(projection, failure, "org_installation")
            first = await _disconnect(db_session)
        assert "identity_index_reverse_row" in first.residual
    assert await _canonical(db_session) == {"state": "revoked"}
    provider = _provider()
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as restarted_db:
        second = await _disconnect(restarted_db, provider)
    assert second.deleted
    assert await projection.index.get_installation_identity(INSTALL) is None
    assert await projection.index.get_reverse_installation_identity(ORG) is None


async def test_provider_failure_does_not_leave_local_authority_live(db_session, projection, webhook, monkeypatch):
    await _seed(db_session, projection)
    result = await _disconnect(db_session, _provider(fails=True))
    assert result.local_revoked and not result.provider_revoked
    assert "provider_uninstall" in result.residual
    canonical = await _canonical(db_session)
    monkeypatch.setattr(webhook.gateway, "resolve_installation_by_id", lambda *args: canonical)
    event = _event(webhook)
    print(f"after_provider_failure canonical={canonical} authority={event}")
    assert event is None, "Provider failure left local human dispatch authority active"


@pytest.mark.parametrize("race", [False, True])
async def test_reverse_cleanup_preserves_concurrent_replacement(db_session, projection, monkeypatch, race):
    await _seed(db_session, projection, other=True)
    if not race:
        await projection.index.write_reverse_installation_identity(ORG, OTHER)
    else:
        original = projection.client.delete_item

        def delete_item(**kwargs):
            if kwargs["Key"]["identity_type"]["S"] == "org_installation":
                projection.client.put_item(
                    TableName="inert-installations",
                    Item={
                        "identity_type": {"S": "org_installation"},
                        "identity_value": {"S": ORG},
                        "installation_id": {"N": str(OTHER)},
                        "concurrent_marker": {"S": "must-survive"},
                    },
                )
            return original(**kwargs)

        monkeypatch.setattr(projection.client, "delete_item", delete_item)
    result = await _disconnect(db_session)
    assert result.residual == []
    reverse = await projection.index.get_reverse_installation_identity(ORG)
    assert reverse["installation_id"] == {"N": str(OTHER)}
    if race:
        assert reverse.get("concurrent_marker") == {"S": "must-survive"}, "Unconditional delete erased a concurrently replaced reverse row"
    org = await db_session.get(Organization, ORG)
    assert org.github_org_id == "999"
    assert org.github_installation_ids == [str(OTHER)]


@pytest.mark.parametrize("operation", ["uninstall", "local_detach"])
@pytest.mark.parametrize("action", ["created", "new_permissions_accepted"])
@pytest.mark.parametrize("gateway_state", ["not_found", "error"])
async def test_delayed_signed_lifecycle_event_cannot_restore_revoked_routing(
    db_session, projection, webhook, monkeypatch, operation, action, gateway_state
):
    await _seed(db_session, projection)
    if operation == "uninstall":
        await _disconnect(db_session)
    else:
        await OrgConnectionsService(db_session, identity_index=projection.writer).detach_github(ORG, INSTALL)
    assert await projection.index.get_installation_identity(INSTALL) is None
    assert await _canonical(db_session) == {"state": "revoked"}
    # Expire any ordinary negative-cache record; it is not a durable revoke.
    table = webhook.resolver._get_table()
    table.put_item(Item={"identity_type": "github_installation_negative", "identity_value": str(INSTALL), "ttl": 1})
    monkeypatch.setattr(webhook.gateway, "resolve_installation_by_id", lambda *args: {"state": gateway_state, "reason": "inert"})
    body = json.dumps({"action": action, "installation": {"id": INSTALL, "account": {"login": ORG}}})
    signature = "sha256=" + hmac.new(b"inert-webhook-secret", body.encode(), hashlib.sha256).hexdigest()
    response = webhook.handler.handler({"headers": {"x-github-event": "installation", "x-hub-signature-256": signature}, "body": body}, None)
    assert response["statusCode"] == 200
    webhook.handler._auto_provision_tenant_github_app_secret.assert_not_called()
    event = _event(webhook)
    print(f"operation={operation} action={action} gateway={gateway_state} authority_after_delayed_signature={event}")
    assert await projection.index.get_installation_identity(INSTALL) is None, "Delayed signed event recreated routing after revocation"
    assert event is None


async def test_local_detach_removes_reverse_routing(db_session, projection, webhook):
    await _seed(db_session, projection)
    result = await OrgConnectionsService(db_session, identity_index=projection.writer).detach_github(ORG, INSTALL)
    assert result.detached
    assert await _canonical(db_session) == {"state": "revoked"}
    assert webhook.reverse.resolve_installation_for_tenant(ORG) is None


async def test_legacy_metadata_mapping_is_removed_with_its_installation(db_session, projection):
    await _seed(db_session, projection, mapped=False)
    db_session.add(
        ChannelTenantMap(
            provider="github",
            org_id=ORG,
            provider_scope_id="999",
            installation_id=None,
            install_metadata={"installation_id": INSTALL, "account_login": "org-acme"},
            installed_by_user_id="installer",
        )
    )
    await db_session.commit()
    result = await _disconnect(db_session, admin=True)
    assert result.deleted
    assert await db_session.scalar(select(ChannelTenantMap).where(ChannelTenantMap.provider == "github")) is None


@pytest.mark.parametrize("admin", [False, True])
async def test_unattested_claim_retraction_never_calls_provider(db_session, projection, admin):
    """A local self-assertion does not authorize provider DELETE, even for an operator."""
    await _seed(db_session, projection, mapped=False)
    provider = _provider()
    if admin:
        result = await _disconnect(db_session, provider, admin=True)
        assert not result.provider_revoked
        provider.delete_installation.assert_not_awaited()
        assert await db_session.get(InstallationRevocation, str(INSTALL)) is None
    else:
        with pytest.raises(PermissionError):
            await _disconnect(db_session, provider)
        provider.delete_installation.assert_not_awaited()


@pytest.mark.parametrize("caller", ["other_tenant", "other_member", "absent_install"])
async def test_unauthorized_or_absent_installation_does_not_revoke_provider(db_session, projection, caller):
    await _seed(db_session, projection)
    provider = _provider()
    with pytest.raises((PermissionError, ValueError)):
        await connections.delete_connection(
            installation_id=OTHER if caller == "absent_install" else INSTALL,
            caller_org_id="other-tenant" if caller == "other_tenant" else ORG,
            caller_user_id="other-user" if caller == "other_member" else "installer",
            caller_is_admin=False,
            db=db_session,
            github_client=provider,
        )
    provider.delete_installation.assert_not_awaited()
    assert await db_session.scalar(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(INSTALL))) is not None


async def test_marker_and_cleanup_failure_still_denies_during_canonical_outage(db_session, projection, webhook, monkeypatch):
    await _seed(db_session, projection)
    original_put = projection.client.put_item

    def put_item(**kwargs):
        if kwargs["Item"]["identity_type"]["S"] == "github_installation_revoked":
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "inert marker failure"}}, "PutItem")
        return original_put(**kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(projection.client, "put_item", put_item)
        failure.setattr(
            projection.client,
            "delete_item",
            MagicMock(side_effect=ClientError({"Error": {"Code": "InternalServerError", "Message": "inert delete failure"}}, "DeleteItem")),
        )
        first = await _disconnect(db_session)
    assert set(first.residual) == set(revocation.PROJECTIONS)
    assert await projection.index.get_installation_identity(INSTALL) is not None
    assert await _canonical(db_session) == {"state": "revoked"}
    monkeypatch.setattr(webhook.gateway, "resolve_installation_by_id", lambda *args: {"state": "error"})
    assert _event(webhook) is None
    assert webhook.reverse.resolve_installation_for_tenant(ORG) is None
    assert not webhook.handler._auto_register_installation(INSTALL, ORG, bypass_negative_cache=True).tenant_id
    second = await _disconnect(db_session)
    assert second.residual == []
    assert await projection.index.get_installation_identity(INSTALL) is None


async def test_denial_commit_failure_keeps_claims_and_does_not_call_provider(db_session, projection):
    await _seed(db_session, projection)
    provider = _provider()
    with patch.object(db_session, "commit", AsyncMock(side_effect=RuntimeError("inert commit failure"))):
        with pytest.raises(RuntimeError, match="commit failure"):
            await _disconnect(db_session, provider)
    await db_session.rollback()
    provider.delete_installation.assert_not_awaited()
    assert (await _canonical(db_session))["state"] == "resolved"
    assert await db_session.get(InstallationRevocation, str(INSTALL)) is None
    assert await projection.index.get_installation_identity(INSTALL) is not None


async def test_self_assertion_retraction_preserves_other_tenant_and_does_not_global_revoke(db_session, projection):
    await _seed(db_session, projection, mapped=False)
    db_session.add(Organization(id="owner", name="Owner", aws_accounts=[], github_installation_ids=[str(INSTALL)]))
    db_session.add(ChannelTenantMap(provider="github", provider_scope_id="actual-owner", org_id="owner", installation_id=str(INSTALL)))
    await db_session.commit()
    await projection.index.put_identity("github_installation_id", str(INSTALL), "owner")
    provider = _provider()
    result = await _disconnect(db_session, provider, admin=True)
    assert result.local_revoked and not result.provider_revoked
    provider.delete_installation.assert_not_awaited()
    assert await db_session.get(InstallationRevocation, str(INSTALL)) is None
    assert (await _canonical(db_session))["tenant_id"] == "owner"
    assert (await projection.index.get_installation_identity(INSTALL))["org_id"] == {"S": "owner"}


async def test_explicit_restore_only_after_completed_local_detach(db_session, projection, webhook, monkeypatch):
    await _seed(db_session, projection)
    service = OrgConnectionsService(db_session, identity_index=projection.writer)
    await service.detach_github(ORG, INSTALL)
    with pytest.raises(InstallationClaimError):
        await service.attach_github(ORG, GitHubConnectionAttachRequest(installation_id=INSTALL))
    await db_session.rollback()
    result = await service.attach_github(ORG, GitHubConnectionAttachRequest(installation_id=INSTALL, github_org_id="999", restore_revoked=True))
    assert result.routable
    assert (await db_session.get(InstallationRevocation, str(INSTALL))).restored_at is not None
    canonical = await _canonical(db_session)
    monkeypatch.setattr(webhook.gateway, "resolve_installation_by_id", lambda *args: canonical)
    assert _event(webhook).human_id == "installer"


@pytest.mark.parametrize("pending", [False, True])
async def test_provider_uninstall_cannot_be_restored_as_local_detach(db_session, projection, pending):
    await _seed(db_session, projection)
    await _disconnect(db_session, _provider(fails=pending))
    with pytest.raises(InstallationClaimError):
        await OrgConnectionsService(db_session).attach_github(ORG, GitHubConnectionAttachRequest(installation_id=INSTALL, restore_revoked=True))
    assert await _canonical(db_session) == {"state": "revoked"}


async def test_retry_local_detach_never_escalates_into_provider_uninstall(db_session, projection):
    await _seed(db_session, projection)
    await OrgConnectionsService(db_session, identity_index=projection.writer).detach_github(ORG, INSTALL)
    provider = _provider()
    result = await _disconnect(db_session, provider)
    assert result.local_revoked and not result.provider_uninstall_requested and not result.provider_revoked
    provider.delete_installation.assert_not_awaited()


async def test_late_writer_cannot_overwrite_denial_or_recreate_reverse(db_session, projection, webhook, monkeypatch):
    await _seed(db_session, projection)
    await _disconnect(db_session)
    assert not await projection.index.update_installation_identity(str(INSTALL), ORG)
    assert not await projection.index.put_identity("github_installation_id", str(INSTALL), ORG)
    assert not await projection.index.write_reverse_installation_identity(ORG, INSTALL)
    assert await projection.index.get_installation_identity(INSTALL) is None
    assert await projection.index.get_reverse_installation_identity(ORG) is None
    # Even an obsolete successful transport answer cannot bypass the permanent marker.
    monkeypatch.setattr(
        webhook.gateway, "resolve_installation_by_id", lambda *args: {"state": "resolved", "tenant_id": ORG, "revocation_checked": True}
    )
    assert _event(webhook) is None


async def test_old_gateway_without_revocation_protocol_cannot_grant(db_session, projection, webhook, monkeypatch):
    await _seed(db_session, projection)
    monkeypatch.setattr(webhook.gateway, "resolve_installation_by_id", lambda *args: {"state": "resolved", "tenant_id": ORG})
    assert _event(webhook) is None
    assert webhook.reverse.resolve_installation_for_tenant(ORG) is None


async def test_active_installation_can_rebuild_routing_after_write_lag(db_session, projection, webhook, monkeypatch):
    await _seed(db_session, projection)
    await projection.index.delete_identity("github_installation_id", str(INSTALL))
    await projection.index.delete_identity("org_installation", ORG)
    canonical = await _canonical(db_session)
    monkeypatch.setattr(webhook.gateway, "resolve_installation_by_id", lambda *args: canonical)
    registered = webhook.handler._auto_register_installation(INSTALL, ORG, bypass_negative_cache=True)
    assert registered.tenant_id == ORG and registered.authoritative
    assert _event(webhook).human_id == "installer"
    await projection.index.delete_identity("org_installation", ORG)
    assert webhook.reverse.resolve_installation_for_tenant(ORG) == INSTALL
    assert await projection.index.get_reverse_installation_identity(ORG) is not None
