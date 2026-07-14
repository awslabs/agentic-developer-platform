"""Tests for tenant org-linking routes (rule 3).

Issue #2954: Platform-admin can link/unlink GitHub orgs to a parent tenant.
Tests cover:
- Platform admin links org B -> tenant A -> matcher resolves B's members to A
- Non-platform-admin -> 403
- Link org already linked elsewhere -> 409
- Unlink reverts resolution
- List linked orgs
- Idempotent link (same tenant twice)
- Cannot link a tenant to itself
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
from src.shared.models.organization import Organization
from src.shared.schemas.auth import TokenContext

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
def platform_admin_context() -> TokenContext:
    """A platform admin user."""
    return TokenContext(
        user_id="admin-user-1",
        org_id="tenant-a",
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def non_admin_context() -> TokenContext:
    """A regular (non-admin) user."""
    return TokenContext(
        user_id="regular-user-1",
        org_id="tenant-a",
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
async def seeded_orgs(db_engine):
    """Seed two organizations: tenant-a (parent) and org-b (to be linked)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        org_a = Organization(
            id="tenant-a",
            name="sophos",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["100"],
            github_org_id="11111",
            member_approval_policy="auto_approve_org_members",
        )
        org_b = Organization(
            id="org-b",
            name="sophos-research",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["200"],
            github_org_id="22222",
            member_approval_policy="auto_approve_org_members",
        )
        org_c = Organization(
            id="org-c",
            name="other-company",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["300"],
            github_org_id="33333",
            member_approval_policy="auto_approve_org_members",
        )
        session.add_all([org_a, org_b, org_c])
        await session.commit()
    return {"tenant_a": "tenant-a", "org_b": "org-b", "org_c": "org-c"}


@pytest.fixture
async def admin_client(db_engine, platform_admin_context):
    """Test client with platform admin auth."""
    from httpx import ASGITransport, AsyncClient

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def override_auth():
        return platform_admin_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def non_admin_client(db_engine, non_admin_context):
    """Test client with non-admin auth."""
    from httpx import ASGITransport, AsyncClient

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def override_auth():
        return non_admin_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test: Non-platform-admin gets 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_org_non_admin_returns_403(non_admin_client, seeded_orgs):
    """Non-platform-admin user cannot link orgs."""
    resp = await non_admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp.status_code == 403
    assert "Platform administrator" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_linked_orgs_non_admin_returns_403(non_admin_client, seeded_orgs):
    """Non-platform-admin user cannot list linked orgs."""
    resp = await non_admin_client.get("/admin/tenants/tenant-a/orgs")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unlink_org_non_admin_returns_403(non_admin_client, seeded_orgs):
    """Non-platform-admin user cannot unlink orgs."""
    resp = await non_admin_client.delete("/admin/tenants/tenant-a/orgs/22222")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test: Successfully link an org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_org_success(admin_client, seeded_orgs, db_engine):
    """Platform admin can link org-b to tenant-a."""
    resp = await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["linked"] is True
    assert data["tenant_id"] == "tenant-a"
    assert data["github_org_id"] == "22222"
    assert data["org_name"] == "sophos-research"

    # Verify in DB
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        org_b = await session.get(Organization, "org-b")
        assert org_b.parent_tenant_id == "tenant-a"


# ---------------------------------------------------------------------------
# Test: Idempotent link (same org to same tenant twice)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_org_idempotent(admin_client, seeded_orgs):
    """Linking the same org to the same tenant twice is idempotent."""
    # First link
    resp1 = await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp1.status_code == 200

    # Second link (same org, same tenant)
    resp2 = await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["linked"] is True


# ---------------------------------------------------------------------------
# Test: Link org already linked elsewhere -> 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_org_already_linked_elsewhere_returns_409(admin_client, seeded_orgs):
    """409 when the org is already linked to a different tenant."""
    # Link org-b to tenant-a first
    resp1 = await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp1.status_code == 200

    # Try to link org-b to org-c (different tenant)
    resp2 = await admin_client.post(
        "/admin/tenants/org-c/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp2.status_code == 409
    assert "already linked" in resp2.json()["detail"]


# ---------------------------------------------------------------------------
# Test: Link to non-existent tenant -> 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_org_to_nonexistent_tenant_returns_404(admin_client, seeded_orgs):
    """404 when the target tenant does not exist."""
    resp = await admin_client.post(
        "/admin/tenants/nonexistent-tenant/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp.status_code == 404
    assert "Tenant not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test: Link non-existent org -> 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_nonexistent_org_returns_404(admin_client, seeded_orgs):
    """404 when the github_org_id does not exist."""
    resp = await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "99999"},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test: Cannot link a tenant to itself -> 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_tenant_to_itself_returns_409(admin_client, seeded_orgs):
    """409 when trying to link a tenant's own org to itself."""
    resp = await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "11111"},
    )
    assert resp.status_code == 409
    assert "itself" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test: Unlink an org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlink_org_success(admin_client, seeded_orgs, db_engine):
    """Platform admin can unlink a previously linked org."""
    # Link first
    await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )

    # Unlink
    resp = await admin_client.delete("/admin/tenants/tenant-a/orgs/22222")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unlinked"] is True
    assert data["tenant_id"] == "tenant-a"
    assert data["github_org_id"] == "22222"

    # Verify in DB
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        org_b = await session.get(Organization, "org-b")
        assert org_b.parent_tenant_id is None


# ---------------------------------------------------------------------------
# Test: Unlink org not linked to this tenant -> 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlink_org_not_linked_returns_404(admin_client, seeded_orgs):
    """404 when trying to unlink an org that is not linked to this tenant."""
    resp = await admin_client.delete("/admin/tenants/tenant-a/orgs/22222")
    assert resp.status_code == 404
    assert "not linked" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test: List linked orgs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_linked_orgs_empty(admin_client, seeded_orgs):
    """List returns empty when no orgs are linked."""
    resp = await admin_client.get("/admin/tenants/tenant-a/orgs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == "tenant-a"
    assert data["linked_orgs"] == []


@pytest.mark.asyncio
async def test_list_linked_orgs_with_links(admin_client, seeded_orgs):
    """List returns linked orgs after linking."""
    # Link org-b and org-c to tenant-a
    await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )
    await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "33333"},
    )

    resp = await admin_client.get("/admin/tenants/tenant-a/orgs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == "tenant-a"
    assert len(data["linked_orgs"]) == 2

    org_names = {o["org_name"] for o in data["linked_orgs"]}
    assert "sophos-research" in org_names
    assert "other-company" in org_names


# ---------------------------------------------------------------------------
# Test: List linked orgs for non-existent tenant -> 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_linked_orgs_nonexistent_tenant_returns_404(admin_client, seeded_orgs):
    """404 when listing linked orgs for a tenant that does not exist."""
    resp = await admin_client.get("/admin/tenants/nonexistent-tenant/orgs")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: Chain guard — cannot link to a tenant that is itself a child
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_to_child_tenant_returns_409(admin_client, seeded_orgs):
    """409 when the target parent tenant is itself a child (daisy-chain)."""
    # Make org-b a child of tenant-a
    resp1 = await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp1.status_code == 200

    # Try to link org-c to org-b (org-b is a child, not a valid parent)
    resp2 = await admin_client.post(
        "/admin/tenants/org-b/orgs",
        json={"github_org_id": "33333"},
    )
    assert resp2.status_code == 409
    assert "itself linked" in resp2.json()["detail"]


# ---------------------------------------------------------------------------
# Test: Cycle guard — cannot make a parent into a child
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_parent_with_children_returns_409(admin_client, seeded_orgs):
    """409 when trying to make an org that has children into a child itself."""
    # Link org-b to tenant-a (tenant-a now has children)
    resp1 = await admin_client.post(
        "/admin/tenants/tenant-a/orgs",
        json={"github_org_id": "22222"},
    )
    assert resp1.status_code == 200

    # Try to link tenant-a to org-c (tenant-a has children, cannot become a child)
    resp2 = await admin_client.post(
        "/admin/tenants/org-c/orgs",
        json={"github_org_id": "11111"},
    )
    assert resp2.status_code == 409
    assert "already a parent" in resp2.json()["detail"]
