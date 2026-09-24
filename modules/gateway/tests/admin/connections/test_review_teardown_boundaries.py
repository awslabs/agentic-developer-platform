"""Independent durable teardown review at actual SQL/provider/index boundaries."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from src.admin.connections import service as connections
from src.admin.identity.organizations_service import OrganizationsService
from src.admin.org_connections.service import OrgConnectionsService
from src.shared.models.vault import ChannelTenantMap, InstallationRevocation
from tests.admin.connections import test_installation_revocation_durable as fx

projection = fx.projection
webhook = fx.webhook


async def test_no_nonce_callback_cannot_recreate_revoked_mapping_or_seed_credentials(db_session, projection, monkeypatch):
    await fx._seed(db_session, projection, other=True)
    await OrgConnectionsService(db_session, identity_index=projection.writer).detach_github(fx.ORG, fx.INSTALL)
    record = await db_session.get(InstallationRevocation, str(fx.INSTALL))
    assert record.restored_at is None and not record.cleanup_pending
    provider = MagicMock()
    provider.get_installation = AsyncMock(
        return_value={
            "id": fx.INSTALL,
            "account": {"id": 999, "login": "inert-org", "type": "Organization"},
            "repository_selection": "selected",
        }
    )
    provider.list_installation_repository_names = AsyncMock(return_value=[])
    seed = AsyncMock()
    index = AsyncMock()
    monkeypatch.setattr("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", seed)
    monkeypatch.setattr(connections, "_write_installation_identity_index", index)
    monkeypatch.setattr(connections, "get_github_app_provider", lambda: MagicMock(get_slug=lambda: ""))
    try:
        result = await connections._handle_no_nonce_install(installation_id=fx.INSTALL, db=db_session, github_client=provider)
    except PermissionError:
        result = {"success": False}
    mapping = await db_session.scalar(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(fx.INSTALL)))
    assert mapping is None and seed.await_count == 0 and index.await_count == 0, (
        f"revoked no-nonce mutation: result={result}, mapping={mapping is not None}, seed_calls={seed.await_count}, index_calls={index.await_count}"
    )
    assert not result["success"]


async def test_new_organization_cannot_claim_revoked_installation_id(db_session, projection):
    from src.admin.identity.schemas import ChannelEntry, ChannelsConfig, OrganizationCreateRequest
    from src.admin.installations.guards import InstallationClaimError

    await fx._seed(db_session, projection)
    await OrgConnectionsService(db_session, identity_index=projection.writer).detach_github(fx.ORG, fx.INSTALL)
    cognito = AsyncMock()
    cognito.ensure_org_group.return_value = True
    service = OrganizationsService(db_session, identity_index=projection.writer, cognito_sync=cognito)
    request = OrganizationCreateRequest(
        id="new-owner",
        name="New owner",
        channels=ChannelsConfig(github=[ChannelEntry(installation_id=str(fx.INSTALL), org_login="inert-org")]),
    )
    with pytest.raises(InstallationClaimError):
        await service.create_organization(request)
    cognito.ensure_org_group.assert_not_awaited()


@pytest.mark.parametrize("failure", ["marker_delete", "database_commit", "conflicting_scope"])
async def test_failed_explicit_restore_keeps_canonical_denial_and_all_readers_closed(db_session, projection, webhook, monkeypatch, failure):
    from unittest.mock import patch

    from src.admin.installations.guards import InstallationClaimError
    from src.admin.org_connections.schemas import GitHubConnectionAttachRequest
    from src.shared.models.organization import Organization

    await fx._seed(db_session, projection)
    service = OrgConnectionsService(db_session, identity_index=projection.writer)
    await service.detach_github(fx.ORG, fx.INSTALL)
    if failure == "conflicting_scope":
        db_session.add(Organization(id="other-owner", name="Other owner"))
        db_session.add(ChannelTenantMap(provider="github", provider_scope_id="999", org_id="other-owner", installation_id=str(fx.OTHER)))
        await db_session.commit()

    with monkeypatch.context() as fault:
        if failure == "marker_delete":
            fault.setattr(projection.index, "clear_installation_revocation", AsyncMock(return_value=False))
        if failure == "database_commit":
            fault.setattr(db_session, "commit", AsyncMock(side_effect=RuntimeError("inert restore commit failure")))
        with pytest.raises((InstallationClaimError, RuntimeError)):
            await service.attach_github(fx.ORG, GitHubConnectionAttachRequest(installation_id=fx.INSTALL, github_org_id="999", restore_revoked=True))
    await db_session.rollback()
    assert (await db_session.get(InstallationRevocation, str(fx.INSTALL))).restored_at is None
    canonical = await fx._canonical(db_session)
    assert canonical == {"state": "revoked"}
    monkeypatch.setattr(webhook.gateway, "resolve_installation_by_id", lambda *args: canonical)
    assert fx._event(webhook) is None
    assert webhook.reverse.resolve_installation_for_tenant(fx.ORG) is None
    assert not webhook.handler._auto_register_installation(fx.INSTALL, fx.ORG, bypass_negative_cache=True).tenant_id
    assert await db_session.scalar(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(fx.INSTALL))) is None
    if failure != "conflicting_scope":
        with patch.object(webhook.gateway, "resolve_installation_by_id", return_value={"state": "error"}):
            assert fx._event(webhook) is None
        restored = await service.attach_github(
            fx.ORG, GitHubConnectionAttachRequest(installation_id=fx.INSTALL, github_org_id="999", restore_revoked=True)
        )
        assert restored.routable


@pytest.mark.parametrize("denial", ["foreign_tenant", "pending_cleanup"])
async def test_explicit_restore_rejects_wrong_owner_or_incomplete_cleanup_before_marker_change(db_session, projection, denial):
    from src.admin.installations.guards import InstallationClaimError
    from src.admin.org_connections.schemas import GitHubConnectionAttachRequest
    from src.shared.models.organization import Organization

    await fx._seed(db_session, projection)
    service = OrgConnectionsService(db_session, identity_index=projection.writer)
    await service.detach_github(fx.ORG, fx.INSTALL)
    target = fx.ORG
    if denial == "foreign_tenant":
        target = "other-owner"
        db_session.add(Organization(id=target, name="Other owner"))
    else:
        record = await db_session.get(InstallationRevocation, str(fx.INSTALL))
        record.cleanup_pending = ["identity_index_forward_row"]
    await db_session.commit()
    with pytest.raises(InstallationClaimError):
        await service.attach_github(target, GitHubConnectionAttachRequest(installation_id=fx.INSTALL, restore_revoked=True))
    assert projection.client.get_item(
        TableName="inert-installations",
        Key={"identity_type": {"S": "github_installation_revoked"}, "identity_value": {"S": str(fx.INSTALL)}},
    ).get("Item")
    assert await fx._canonical(db_session) == {"state": "revoked"}


async def test_cleanup_attempt_cannot_cross_completed_explicit_restore(db_session, projection):
    from src.admin.installations import revocation
    from src.admin.org_connections.schemas import GitHubConnectionAttachRequest

    await fx._seed(db_session, projection)
    service = OrgConnectionsService(db_session, identity_index=projection.writer)
    await service.detach_github(fx.ORG, fx.INSTALL)
    await service.attach_github(fx.ORG, GitHubConnectionAttachRequest(installation_id=fx.INSTALL, github_org_id="999", restore_revoked=True))
    provider = fx._provider()
    with pytest.raises(PermissionError, match="explicitly restored"):
        await revocation._complete_revocation(
            installation_id=fx.INSTALL,
            org_id=fx.ORG,
            db=db_session,
            user_id="installer",
            is_admin=False,
            github_client=provider,
            index=projection.index,
        )
    provider.delete_installation.assert_not_awaited()
    assert (await fx._canonical(db_session))["state"] == "resolved"


@pytest.mark.parametrize("protocol", ["missing", False, "true", True, "gone", "unavailable"])
async def test_actual_gateway_transport_requires_explicit_current_revocation_protocol(db_session, projection, webhook, monkeypatch, protocol):
    import json
    import urllib.error

    await fx._seed(db_session, projection)
    monkeypatch.setattr(webhook.gateway, "GATEWAY_API_URL", "https://inert-gateway.test")
    monkeypatch.setattr(webhook.gateway, "_resolve_internal_api_key", lambda: "inert-review-key")
    body = {"tenant_id": fx.ORG, "created_via": "operator"}
    if protocol != "missing":
        body["revocation_checked"] = protocol

    def request(req, *, timeout):
        assert json.loads(req.data) == {"installation_id": str(fx.INSTALL)}
        if protocol in ("gone", "unavailable"):
            raise urllib.error.HTTPError(req.full_url, 410 if protocol == "gone" else 503, "inert failure", {}, None)
        response = MagicMock(status=200)
        response.read.return_value = json.dumps(body).encode()
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr(webhook.gateway.urllib.request, "urlopen", request)
    event = fx._event(webhook)
    reverse = webhook.reverse.resolve_installation_for_tenant(fx.ORG)
    auto = webhook.handler._auto_register_installation(fx.INSTALL, fx.ORG, bypass_negative_cache=True)
    if protocol is True:
        assert event.human_id == "installer"
        assert reverse == fx.INSTALL
        assert auto.tenant_id == fx.ORG and auto.authoritative
    else:
        assert event is None and reverse is None and auto.tenant_id is None


async def test_unreadable_marker_denies_all_readers_despite_positive_canonical_answer(db_session, projection, webhook, monkeypatch):
    await fx._seed(db_session, projection)
    table = webhook.resolver._get_table()
    original = table.get_item

    def read(**kwargs):
        if kwargs["Key"]["identity_type"] == "github_installation_revoked":
            raise RuntimeError("inert unreadable marker")
        return original(**kwargs)

    monkeypatch.setattr(table, "get_item", read)
    monkeypatch.setattr(webhook.resolver, "_get_table", lambda: table)
    monkeypatch.setattr(webhook.reverse, "_get_table", lambda: table)
    monkeypatch.setattr(
        webhook.gateway, "resolve_installation_by_id", lambda *args: {"state": "resolved", "tenant_id": fx.ORG, "revocation_checked": True}
    )
    assert fx._event(webhook) is None
    assert webhook.reverse.resolve_installation_for_tenant(fx.ORG) is None
    assert not webhook.handler._auto_register_installation(fx.INSTALL, fx.ORG, bypass_negative_cache=True).tenant_id
