"""Offline setup fixtures that retain real identity, nonce and control checks."""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import select

from src.admin.connections import service
from src.admin.connections.github_client import GitHubAppClient
from src.admin.identity.identities_service import IdentitiesService
from src.admin.identity.schemas import IdentityCreateRequest
from src.shared.models.vault import MagicLinkNonce, UserIdentity

PROVEN_GITHUB_USER_ID = "12345"


@pytest.fixture
def offline_setup_boundaries(monkeypatch):
    """Stub external writes, preserving membership/projection SQL and authority."""
    monkeypatch.setenv("WEBHOOK_URL", "https://webhook.example.test/inert")
    monkeypatch.setenv("ORG_TENANT_AUTO_CREATE", "false")
    monkeypatch.setattr(service, "_get_github_app_slug", lambda: "test-adp-agent")
    monkeypatch.setattr(service, "_get_github_app_credentials", lambda: ("inert-app", "inert-key"))
    monkeypatch.setattr(service, "_invalidate_verification_cache", lambda: None)
    monkeypatch.setattr(service, "_invalidate_login_enabled_cache", lambda: None)
    provider = MagicMock()
    provider.get_slug.return_value = "test-adp-agent"
    monkeypatch.setattr(service, "get_github_app_provider", lambda: provider)
    monkeypatch.setattr("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", AsyncMock())
    writer = MagicMock()
    writer.put_user_identity = AsyncMock(return_value=True)
    writer.update_user_membership_orgs = AsyncMock(return_value=True)
    monkeypatch.setattr("src.admin.identity.identity_index_writer.IdentityIndexWriter", lambda: writer)
    monkeypatch.setattr("src.admin.connections.bot_identity.IdentityIndexWriter", lambda: writer)
    return writer


async def issue_install_nonce(db, user, *, jti):
    """Persist real admin-attested proof, then issue the normal setup capability."""
    proof = await db.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == user.id,
            UserIdentity.provider == "github",
            UserIdentity.provider_user_id == PROVEN_GITHUB_USER_ID,
        )
    )
    if proof is None:
        response = await IdentitiesService(db).add_identity(
            user.id,
            IdentityCreateRequest(provider="github", provider_user_id=PROVEN_GITHUB_USER_ID, provider_username="proven-installer"),
        )
        assert response.verification_method == "admin_manual"
    # Only entropy is deterministic. The production issuer resolves the human,
    # checks the selected workspace, serializes its context and commits the nonce.
    with patch.object(service.uuid, "uuid4", return_value=jti):
        started = await service.install_start(cognito_sub=user.cognito_sub, user_id=user.id, org_id=user.org_id, db=db)
    assert started.state_token == jti
    return await db.get(MagicLinkNonce, jti)


async def issue_register_nonce(db, user, *, jti, owner_type="org", owner="Acme-Corp"):
    """Use the real platform-role and registration-target binding issuer."""
    with patch.object(service.uuid, "uuid4", return_value=jti):
        started = await service.register_app_start(
            owner_type=owner_type,
            org=owner if owner_type == "org" else None,
            cognito_sub=user.cognito_sub,
            user_id=user.id,
            db=db,
        )
    assert started.state == jti
    return jti


def bind_real_org_control(client):
    """Run GitHub's immutable-ID/admin checker over explicit HTTP responses.

    The caller may control the GitHub organization while lacking membership in
    the existing ADP tenant. Those are distinct boundaries in the victim tests.
    """
    metadata = client.get_installation.return_value
    account = metadata["account"]
    provider_http = AsyncMock(spec=httpx.AsyncClient)

    async def request(method, path, **kwargs):
        if method == "POST" and path == f"/app/installations/{metadata['id']}/access_tokens":
            payload = {"token": "inert-installation-token"}
        elif method == "GET" and path == f"/user/{PROVEN_GITHUB_USER_ID}":
            payload = {"id": int(PROVEN_GITHUB_USER_ID), "login": "proven-installer", "type": "User"}
        elif method == "GET" and path == f"/orgs/{quote(account['login'], safe='')}/memberships/proven-installer":
            payload = {"state": "active", "role": "admin", "user": {"id": int(PROVEN_GITHUB_USER_ID)}, "organization": {"id": account["id"]}}
        else:
            raise AssertionError(f"Unstubbed provider request: {method} {path}")
        return httpx.Response(200, json=payload, request=httpx.Request(method, f"https://api.github.com{path}"))

    async def get(path, **kwargs):
        return await request("GET", path, **kwargs)

    async def post(path, **kwargs):
        return await request("POST", path, **kwargs)

    provider_http.get.side_effect = get
    provider_http.post.side_effect = post
    provider = GitHubAppClient("inert-app", "inert-key", http_client=provider_http)
    provider._auth_headers = lambda: {"Authorization": "Bearer inert-app-token"}
    client.has_org_admin_membership = provider.has_org_admin_membership
    return client
