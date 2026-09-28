"""Tests for matcher resolution with parent_tenant_id (rule 3).

Issue #2954: Verifies that _find_matching_tenants_for_user resolves to
parent_tenant_id when an org is linked to a parent tenant, and reverts
to org.id when unlinked.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
from src.shared.models.organization import Organization

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncSession:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def seeded_linked_orgs(db_session):
    """Seed: org-parent + org-child (linked via parent_tenant_id)."""
    parent = Organization(
        id="parent-tenant",
        name="sophos",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=["100"],
        github_org_id="11111",
        member_approval_policy="auto_approve_org_members",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    child = Organization(
        id="child-org",
        name="sophos-research",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=["200"],
        github_org_id="22222",
        parent_tenant_id="parent-tenant",  # linked to parent
        member_approval_policy="auto_approve_org_members",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    standalone = Organization(
        id="standalone-org",
        name="standalone-co",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=["300"],
        github_org_id="33333",
        member_approval_policy="auto_approve_org_members",
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    db_session.add_all([parent, child, standalone])
    await db_session.commit()


@pytest.mark.asyncio
async def test_matcher_resolves_linked_org_to_parent_tenant(db_session, seeded_linked_orgs):
    """When a user is a member of a linked org, the matcher resolves to parent_tenant_id."""
    from src.admin.onboarding.handler import _find_matching_tenants_for_user

    # Mock GitHub client to say user is member of sophos-research (install 200)
    mock_client = MagicMock()
    mock_client.check_org_membership = AsyncMock(
        side_effect=lambda installation_id, org_login, username: installation_id == 200,
    )
    mock_client.aclose = AsyncMock()

    with (
        patch(
            "src.admin.connections.github_client.GitHubAppClient",
            return_value=mock_client,
        ),
        patch(
            "src.admin.connections.service._get_github_app_credentials",
            return_value=("app-id", "fake-pem"),
        ),
    ):
        matched = await _find_matching_tenants_for_user(db_session, "researcher-user")

    # Should resolve to parent-tenant, not child-org
    assert len(matched) == 1
    assert matched[0].org_id == "parent-tenant"
    assert matched[0].org_name == "sophos-research"
    assert matched[0].install_id == 200


@pytest.mark.asyncio
async def test_matcher_resolves_standalone_org_to_itself(db_session, seeded_linked_orgs):
    """When a user is a member of a standalone org, the matcher resolves to org.id."""
    from src.admin.onboarding.handler import _find_matching_tenants_for_user

    # Mock GitHub client to say user is member of standalone-co (install 300)
    mock_client = MagicMock()
    mock_client.check_org_membership = AsyncMock(
        side_effect=lambda installation_id, org_login, username: installation_id == 300,
    )
    mock_client.aclose = AsyncMock()

    with (
        patch(
            "src.admin.connections.github_client.GitHubAppClient",
            return_value=mock_client,
        ),
        patch(
            "src.admin.connections.service._get_github_app_credentials",
            return_value=("app-id", "fake-pem"),
        ),
    ):
        matched = await _find_matching_tenants_for_user(db_session, "standalone-user")

    # Should resolve to standalone-org (its own id)
    assert len(matched) == 1
    assert matched[0].org_id == "standalone-org"
    assert matched[0].org_name == "standalone-co"


@pytest.mark.asyncio
async def test_matcher_resolves_unlinked_org_to_itself(db_session, seeded_linked_orgs):
    """After unlinking, the matcher resolves back to org.id."""
    # Unlink the child
    from sqlalchemy import select

    stmt = select(Organization).where(Organization.id == "child-org")
    child = (await db_session.execute(stmt)).scalar_one()
    child.parent_tenant_id = None
    await db_session.commit()

    from src.admin.onboarding.handler import _find_matching_tenants_for_user

    # Mock GitHub client to say user is member of sophos-research (install 200)
    mock_client = MagicMock()
    mock_client.check_org_membership = AsyncMock(
        side_effect=lambda installation_id, org_login, username: installation_id == 200,
    )
    mock_client.aclose = AsyncMock()

    with (
        patch(
            "src.admin.connections.github_client.GitHubAppClient",
            return_value=mock_client,
        ),
        patch(
            "src.admin.connections.service._get_github_app_credentials",
            return_value=("app-id", "fake-pem"),
        ),
    ):
        matched = await _find_matching_tenants_for_user(db_session, "researcher-user")

    # Should now resolve to child-org (its own id) since unlinked
    assert len(matched) == 1
    assert matched[0].org_id == "child-org"
    assert matched[0].org_name == "sophos-research"


@pytest.mark.asyncio
async def test_matcher_multi_org_member_with_linked_org(db_session, seeded_linked_orgs):
    """User in both the parent org and linked org resolves to parent for both."""
    from src.admin.onboarding.handler import _find_matching_tenants_for_user

    # Mock: user is member of both orgs (installs 100 and 200)
    mock_client = MagicMock()
    mock_client.check_org_membership = AsyncMock(
        side_effect=lambda installation_id, org_login, username: installation_id in (100, 200),
    )
    mock_client.aclose = AsyncMock()

    with (
        patch(
            "src.admin.connections.github_client.GitHubAppClient",
            return_value=mock_client,
        ),
        patch(
            "src.admin.connections.service._get_github_app_credentials",
            return_value=("app-id", "fake-pem"),
        ),
    ):
        matched = await _find_matching_tenants_for_user(db_session, "multi-org-user")

    # Both orgs resolve to parent-tenant — dedup ensures only one entry returned
    # (prevents UniqueConstraint violation in _create_memberships_for_matches).
    assert len(matched) == 1
    assert matched[0].org_id == "parent-tenant"
