"""EXT02-t1: linked identities require a proven account and active connection."""

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.activity.external_identity import resolve_provider_identities
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity

INSTANCE = "https://gitlab.example.invalid/instance"
PROVIDER_ID = hashlib.sha256(INSTANCE.encode()).hexdigest()[:24]


@pytest.fixture
async def connected(db_session):
    for org_id in ("tenant-a", "tenant-b"):
        db_session.add(
            Organization(
                id=org_id,
                name=org_id,
                aws_accounts=[],
                cognito_client_ids=[],
                github_installation_ids=["900"] if org_id == "tenant-a" else [],
                settings={
                    "gitlab_cli_v1": {
                        "provider_id": PROVIDER_ID,
                        "projects": {"project-7": {"owner": "owner", "active": True, "instance": INSTANCE, "repo": "org/repo", "project_id": 7}},
                    }
                }
                if org_id == "tenant-a"
                else {},
            )
        )
        db_session.add(Department(id=org_id + "-dept", org_id=org_id, name="Default"))
        db_session.add(Team(id=org_id + "-team", org_id=org_id, department_id=org_id + "-dept", name="Default"))
    for user_id, org_id in (("owner", "tenant-a"), ("neighbor", "tenant-a"), ("outsider", "tenant-b")):
        db_session.add(User(id=user_id, org_id=org_id, team_id=org_id + "-team", name=user_id, email=user_id + "@example.invalid", role="member"))
    await db_session.flush()
    return db_session


def link(db, *, actor="owner", tenant="tenant-a", provider="github", external_id="17", method="oauth", verified=True, username="old-login"):
    db.add(
        UserIdentity(
            org_id=tenant,
            team_id=tenant + "-team",
            user_id=actor,
            provider=provider,
            provider_user_id=external_id,
            provider_username=username,
            verification_method=method,
            verified_at=datetime(2026, 10, 1, tzinfo=UTC) if verified else None,
        )
    )


@pytest.mark.asyncio
async def test_connected_owner_resolves_immutable_aliases_without_same_tenant_or_cross_tenant_leaks(connected):
    link(connected, external_id="17", username="old-login")
    link(connected, external_id="18", username="new-login", method="admin_attested")
    link(connected, external_id="19", method="self_asserted", verified=False)
    link(connected, actor="neighbor", external_id="20", username="old-login")
    link(connected, actor="outsider", tenant="tenant-b", external_id="21", username="old-login")
    link(connected, provider="gitlab", external_id=INSTANCE + "#42", method="credential_verified", username="renamed")
    link(connected, provider="gitlab", external_id=INSTANCE + "#43", method="credential_verified", verified=False)
    link(connected, provider="gitlab", external_id="https://other.example.invalid#44", method="oauth")
    await connected.flush()
    resolved = await resolve_provider_identities(connected, user_id="owner", tenant_id="tenant-a")
    assert resolved.github_ids == frozenset({"17", "18"})
    assert resolved.gitlab_ids == {INSTANCE: frozenset({"42"})}
    assert resolved.unavailable == {}
    github_row = await connected.scalar(select(UserIdentity).where(UserIdentity.provider_user_id == "17"))
    gitlab_row = await connected.scalar(select(UserIdentity).where(UserIdentity.provider_user_id == INSTANCE + "#42"))
    github_row.provider_username = "renamed-again"
    gitlab_row.provider_username = "another-alias"
    await connected.flush()
    again = await resolve_provider_identities(connected, user_id="owner", tenant_id="tenant-a")
    assert again.github_ids == resolved.github_ids and again.gitlab_ids == resolved.gitlab_ids


@pytest.mark.asyncio
async def test_stale_links_cannot_resurrect_disconnected_providers(connected):
    link(connected)
    link(connected, provider="gitlab", external_id=INSTANCE + "#42", method="credential_verified")
    await connected.flush()
    org = await connected.get(Organization, "tenant-a")
    org.github_installation_ids = []
    org.settings = {}
    resolved = await resolve_provider_identities(connected, user_id="owner", tenant_id="tenant-a")
    assert resolved.github_ids == frozenset() and resolved.gitlab_ids == {}
    assert resolved.unavailable == {"github": "disconnected", "gitlab": "disconnected"}


@pytest.mark.asyncio
async def test_unproven_or_malformed_provider_ids_are_not_authority(connected):
    link(connected, external_id="old-login", method="admin_attested")
    link(connected, external_id="18", method="magic_link", verified=True)
    link(connected, provider="gitlab", external_id=INSTANCE + "#alias", method="credential_verified")
    link(connected, provider="gitlab", external_id=INSTANCE + "#42", method="self_asserted", verified=False)
    await connected.flush()
    resolved = await resolve_provider_identities(connected, user_id="owner", tenant_id="tenant-a")
    assert resolved.github_ids == frozenset() and resolved.gitlab_ids == {}
    assert resolved.unavailable == {"github": "identity_unverified", "gitlab": "identity_unverified"}
    assert (await resolve_provider_identities(connected, user_id="outsider", tenant_id="tenant-a")).unavailable == {
        "github": "user_not_available",
        "gitlab": "user_not_available",
    }
