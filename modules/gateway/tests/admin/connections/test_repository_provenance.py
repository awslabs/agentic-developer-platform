"""The repository list must say whether it came from GitHub or from a snapshot.

Issue #5184. ``list_connections`` degrades to the stored metadata snapshot
whenever the live GitHub read fails (#2983), and until now the two were
indistinguishable in the response. A caller that must PROVE access to one exact
repository — ``adp github connect --repo owner/name`` — cannot treat a snapshot as
proof, so ``verification.repositories_live`` carries the provenance.

Tri-state, per the #4016 convention: True = read live, False = snapshot served
because GitHub could not be reached, None = no read was attempted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
from src.shared.models.vault import ChannelTenantMap

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.fixture(autouse=True)
def clear_caches():
    """The repo-list cache is process-global; a leak between tests would mask a bug."""
    from src.admin.connections.service import _repo_list_cache

    _repo_list_cache.clear()
    yield
    _repo_list_cache.clear()


def _mapping(*, installation_id: int, org_id: str, repositories: list[str]) -> ChannelTenantMap:
    return ChannelTenantMap(
        provider="github",
        provider_scope_id=str(installation_id),
        org_id=org_id,
        install_metadata={
            "installation_id": installation_id,
            "account_login": "sophos-it",
            "account_type": "Organization",
            "repository_selection": "selected",
            "repository_count": len(repositories),
            "repositories": repositories,
        },
    )


@pytest.mark.asyncio
async def test_live_read_is_marked_live(db_session: AsyncSession):
    from src.admin.connections.service import list_connections

    db_session.add(_mapping(installation_id=90001, org_id="tenant-a", repositories=["sophos-it/stale"]))
    await db_session.commit()

    client = MagicMock()
    client.list_installation_repository_names = AsyncMock(return_value=["sophos-it/project"])

    resp = await list_connections(caller_org_id="tenant-a", caller_user_id="user-1", db=db_session, github_client=client)

    conn = resp.connections[0]
    assert conn.repositories == ["sophos-it/project"]
    assert conn.verification.repositories_live is True


@pytest.mark.asyncio
async def test_snapshot_fallback_is_marked_not_live(db_session: AsyncSession):
    """GitHub unreachable → the snapshot is served, and says so.

    This is the case that would otherwise let a caller report confirmed access to
    a repository the installation may no longer have.
    """
    from src.admin.connections.service import list_connections

    db_session.add(_mapping(installation_id=90002, org_id="tenant-a", repositories=["sophos-it/snapshot-only"]))
    await db_session.commit()

    client = MagicMock()
    client.list_installation_repository_names = AsyncMock(side_effect=RuntimeError("GitHub is down"))

    resp = await list_connections(caller_org_id="tenant-a", caller_user_id="user-1", db=db_session, github_client=client)

    conn = resp.connections[0]
    # The list still renders — degradation stays graceful.
    assert conn.repositories == ["sophos-it/snapshot-only"]
    # But it is explicitly not proof.
    assert conn.verification.repositories_live is False


@pytest.mark.asyncio
async def test_no_github_client_is_marked_not_live(db_session: AsyncSession):
    """No app credentials → no read happened, so nothing is proven."""
    from src.admin.connections.service import list_connections

    db_session.add(_mapping(installation_id=90003, org_id="tenant-a", repositories=["sophos-it/from-metadata"]))
    await db_session.commit()

    with patch("src.admin.connections.service._get_github_app_credentials", return_value=("", "")):
        resp = await list_connections(caller_org_id="tenant-a", caller_user_id="user-1", db=db_session)

    conn = resp.connections[0]
    assert conn.repositories == ["sophos-it/from-metadata"]
    assert conn.verification.repositories_live is False


@pytest.mark.asyncio
async def test_provenance_survives_a_verification_check_failure(db_session: AsyncSession):
    """The other checks failing must not erase what we already know about the list.

    Provenance is decided when the list is fetched, independently of the
    Secrets Manager / DynamoDB checks, so it outlives their failure.
    """
    from src.admin.connections.service import list_connections

    db_session.add(_mapping(installation_id=90004, org_id="tenant-a", repositories=["sophos-it/project"]))
    await db_session.commit()

    client = MagicMock()
    client.list_installation_repository_names = AsyncMock(return_value=["sophos-it/project"])

    with patch(
        "src.admin.connections.service._compute_connection_verification",
        new=AsyncMock(side_effect=RuntimeError("checks unavailable")),
    ):
        resp = await list_connections(caller_org_id="tenant-a", caller_user_id="user-1", db=db_session, github_client=client)

    conn = resp.connections[0]
    assert conn.verification is not None
    assert conn.verification.repositories_live is True
    # The checks that genuinely could not be computed stay unknown, not False.
    assert conn.verification.tenant_secret_seeded is None


@pytest.mark.asyncio
async def test_existing_verification_checks_are_unchanged(db_session: AsyncSession):
    """The new field is additive: #4016's checks keep their values."""
    from src.admin.connections.service import list_connections

    db_session.add(_mapping(installation_id=90005, org_id="tenant-a", repositories=["sophos-it/project"]))
    await db_session.commit()

    client = MagicMock()
    client.list_installation_repository_names = AsyncMock(return_value=["sophos-it/project"])

    resp = await list_connections(caller_org_id="tenant-a", caller_user_id="user-1", db=db_session, github_client=client)

    verification = resp.connections[0].verification
    assert verification.record_present is True
    assert {"record_present", "tenant_secret_seeded", "identity_index_row", "reverse_identity_row", "repositories_live"} <= set(
        verification.model_dump()
    )


@pytest.mark.asyncio
async def test_provenance_is_per_installation(db_session: AsyncSession):
    """One installation failing its read must not mislabel another's."""
    from src.admin.connections.service import list_connections

    db_session.add(_mapping(installation_id=90006, org_id="tenant-a", repositories=["sophos-it/one"]))
    db_session.add(_mapping(installation_id=90007, org_id="tenant-a", repositories=["sophos-it/two"]))
    await db_session.commit()

    async def per_install(installation_id):
        if installation_id == 90007:
            raise RuntimeError("GitHub is down for this one")
        return ["sophos-it/one"]

    client = MagicMock()
    client.list_installation_repository_names = AsyncMock(side_effect=per_install)

    resp = await list_connections(caller_org_id="tenant-a", caller_user_id="user-1", db=db_session, github_client=client)

    by_id = {c.installation_id: c for c in resp.connections}
    assert by_id[90006].verification.repositories_live is True
    assert by_id[90007].verification.repositories_live is False


@pytest.mark.asyncio
async def test_verification_never_carries_repository_secrets(db_session: AsyncSession):
    """Machine-readable output is the easiest thing to log by accident."""
    from src.admin.connections.service import list_connections

    db_session.add(_mapping(installation_id=90008, org_id="tenant-a", repositories=["sophos-it/project"]))
    await db_session.commit()

    client = MagicMock()
    client.list_installation_repository_names = AsyncMock(return_value=["sophos-it/project"])

    resp = await list_connections(caller_org_id="tenant-a", caller_user_id="user-1", db=db_session, github_client=client)

    blob = str(resp.connections[0].verification.model_dump()).casefold()
    for forbidden in ("private_key", "client_secret", "webhook_secret", "token"):
        assert forbidden not in blob
