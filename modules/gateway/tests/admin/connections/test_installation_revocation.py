"""Durable installation revocation — #5664 (A10), repair item 3.

A "disconnect" that leaves any durable record of the installation's authority
behind is not a revocation, and one that *reports* success it did not achieve is
worse than a visible failure: the operator stops looking.

These tests pin the three properties the previous implementation lacked.

1. **Consistency.** ``delete_connection`` used to remove exactly one of the two
   records of ownership (the ``ChannelTenantMap`` row) and leave
   ``organizations.github_installation_ids`` populated. Because
   ``resolve_installation`` answers from that JSON list, and the webhook Lambda's
   auto-register gate consults it, the deleted DynamoDB projections were
   re-created from it on the next inbound event. Revocation has to clear both,
   in that order, or it undoes itself.

2. **Honesty.** The GitHub revoke was wrapped in ``except Exception:
   logger.warning(...)`` followed by ``deleted=True``. It is now a precondition:
   no confirmed uninstall, no disconnect, nothing changed.

3. **Idempotent recovery.** Ordering is what makes this possible. The local
   claims are the only thing authorizing the operation, so revoking at the
   provider FIRST means every failure leaves them intact and the call simply
   retryable. The reverse order is unrecoverable: the retry resolves NOT_FOUND
   and raises while the installation stays live at GitHub forever.

Plus the keying defect that made all three moot: ownership was matched on
``provider_scope_id`` (the GitHub *account*) rather than the canonical
``installation_id`` column, so the wrong installation could be revoked.

The DynamoDB clients are mocked throughout — these tests must not touch AWS.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import delete_connection
from src.shared.models.base import Base
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

INSTALL_A = 124731131
INSTALL_B = 999888777
ACCOUNT_ID = "98765"


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    """Block App-credential lookup so no test can reach Secrets Manager."""
    monkeypatch.setenv("BG_GITHUB_APP_SLUG", "test-adp-agent")
    with patch(
        "src.admin.connections.service._get_github_app_credentials",
        return_value=("", ""),
    ):
        yield


@pytest.fixture
def index_spies():
    """Stand-ins for the two DDB clients the projection cleanup drives.

    Returned as a pair of MagicMocks with the real method names, so an assertion
    here fails if the production call is renamed or dropped.
    """
    writer = MagicMock()
    writer.sync_org_channels = AsyncMock(return_value=None)

    index = MagicMock()
    index.get_reverse_installation_identity = AsyncMock(return_value=None)
    index.delete_identity = AsyncMock(return_value=True)
    index.write_reverse_installation_identity = AsyncMock(return_value=True)

    with (
        patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer),
        patch("src.admin.identity_index.IdentityIndexClient", return_value=index),
    ):
        yield writer, index


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        import src.admin.models  # noqa: F401
        import src.shared.models.organization  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(db_engine) -> AsyncSession:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


def _org(org_id: str, *, installs: list[str], github_org_id: str | None = ACCOUNT_ID) -> Organization:
    return Organization(
        id=org_id,
        name=f"Org {org_id}",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=installs,
        cognito_client_ids=[],
        github_org_id=github_org_id,
        github_app_id="app-1",
    )


def _map_row(org_id: str, installation_id: int, *, scope_id: str = ACCOUNT_ID, installed_by: str | None = None) -> ChannelTenantMap:
    return ChannelTenantMap(
        provider="github",
        provider_scope_id=scope_id,
        installation_id=str(installation_id),
        org_id=org_id,
        install_metadata={"installation_id": installation_id, "account_login": "acme", "account_type": "Organization"},
        installed_by_user_id=installed_by,
    )


def _gh(*, revoke_fails: bool = False) -> MagicMock:
    client = MagicMock(spec=GitHubAppClient)
    client.get_installation = AsyncMock(return_value={"id": INSTALL_A, "account": {"type": "Organization", "login": "acme", "id": int(ACCOUNT_ID)}})
    if revoke_fails:
        client.delete_installation = AsyncMock(side_effect=RuntimeError("GitHub 500"))
    else:
        client.delete_installation = AsyncMock(return_value=None)
    return client


async def _seed(db: AsyncSession, *, installs: list[str], rows: list[ChannelTenantMap]) -> None:
    db.add(_org("org-1", installs=installs))
    for row in rows:
        db.add(row)
    await db.commit()


# ---------------------------------------------------------------------------
# 1. Consistency across representations
# ---------------------------------------------------------------------------


class TestAuthorityIsRevokedEverywhere:
    async def test_the_org_json_claim_is_cleared_not_just_the_map_row(self, db: AsyncSession, index_spies):
        """The regression that made every other cleanup pointless.

        `organizations.github_installation_ids` is what `resolve_installation`
        answers from, and the webhook Lambda re-registers the DDB routing rows
        from a positive answer. Leaving it populated means the installation keeps
        resolving to this tenant and the projections come back by themselves.
        """
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        assert result.deleted is True
        org = await db.get(Organization, "org-1")
        assert str(INSTALL_A) not in [str(i) for i in (org.github_installation_ids or [])]

    async def test_the_map_row_is_gone(self, db: AsyncSession, index_spies):
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        rows = (await db.execute(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(INSTALL_A)))).scalars().all()
        assert rows == []

    async def test_the_installation_no_longer_resolves_to_the_tenant(self, db: AsyncSession, index_spies):
        """The end-to-end property, asserted through the canonical resolver
        rather than by re-reading the columns the delete just wrote."""
        from src.admin.installations.resolver import OwnerState, resolve_installation_owner

        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        owner, state = await resolve_installation_owner(INSTALL_A, db=db)
        assert owner is None
        assert state is OwnerState.NOT_FOUND

    async def test_the_forward_projection_is_synced_with_the_revoked_id_removed(self, db: AsyncSession, index_spies):
        """`sync_org_channels` must be told both the new list and the old one —
        it deletes exactly the ids that disappeared between them."""
        writer, _index = index_spies
        await _seed(db, installs=[str(INSTALL_A), str(INSTALL_B)], rows=[_map_row("org-1", INSTALL_A)])

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        kwargs = writer.sync_org_channels.await_args.kwargs
        assert kwargs["github_installation_ids"] == [str(INSTALL_B)]
        assert str(INSTALL_A) in kwargs["old_github_installation_ids"]

    async def test_a_surviving_installation_keeps_its_routing(self, db: AsyncSession, index_spies):
        """Revoking one installation must not disconnect the tenant's others.

        The per-ACCOUNT fields (`github_org_id`, `github_app_id`) are what make an
        installation attestable, so clearing them while a sibling remains would
        turn that sibling UNATTESTABLE and silently break its routing.
        """
        await _seed(
            db,
            installs=[str(INSTALL_A), str(INSTALL_B)],
            rows=[_map_row("org-1", INSTALL_A), _map_row("org-1", INSTALL_B, scope_id="55555")],
        )

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        org = await db.get(Organization, "org-1")
        assert [str(i) for i in org.github_installation_ids] == [str(INSTALL_B)]
        assert org.github_org_id == ACCOUNT_ID
        assert org.github_app_id == "app-1"
        survivor = (await db.execute(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(INSTALL_B)))).scalars().all()
        assert len(survivor) == 1

    async def test_the_account_fields_are_cleared_once_nothing_remains(self, db: AsyncSession, index_spies):
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        org = await db.get(Organization, "org-1")
        assert org.github_org_id is None
        assert org.github_app_id is None

    async def test_the_verification_caches_are_invalidated(self, db: AsyncSession, index_spies):
        """These caches key on the tenant and gate the connections card's
        "seeded / indexed" signals. Their own docstring claims they clear "after
        register / rotate / disconnect"; the disconnect half was never wired up,
        so a revoked installation kept presenting as healthy for the TTL."""
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        with patch("src.admin.connections.service._invalidate_verification_cache") as spy:
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(),
                caller_is_admin=True,
            )

        spy.assert_called_once()


class TestReverseRowRevocation:
    """org_installation/<org> -> installation_id: the row adp-trigger reads to
    pick the tenant's outbound credential. Nothing in the repo deleted it."""

    async def test_it_is_deleted_when_it_names_the_revoked_installation(self, db: AsyncSession, index_spies):
        _writer, index = index_spies
        index.get_reverse_installation_identity = AsyncMock(return_value={"installation_id": {"N": str(INSTALL_A)}})
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        index.delete_identity.assert_awaited_once_with("org_installation", "org-1")
        assert result.residual == []

    async def test_it_is_repointed_at_a_survivor_by_delete_then_write(self, db: AsyncSession, index_spies):
        """Ordering matters and is not cosmetic: `write_reverse_installation_identity`
        refuses to clobber a row lacking `auto_registered` (Postgres-owned) and
        returns True for that no-op. Writing over it in place would therefore
        report success while leaving the REVOKED id as the tenant's dispatch
        credential. Deleting first makes the write a create, which the guard allows.
        """
        _writer, index = index_spies
        index.get_reverse_installation_identity = AsyncMock(return_value={"installation_id": {"N": str(INSTALL_A)}})
        await _seed(
            db,
            installs=[str(INSTALL_A), str(INSTALL_B)],
            rows=[_map_row("org-1", INSTALL_A), _map_row("org-1", INSTALL_B, scope_id="55555")],
        )

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        index.delete_identity.assert_awaited_once_with("org_installation", "org-1")
        index.write_reverse_installation_identity.assert_awaited_once_with("org-1", INSTALL_B)

    async def test_a_row_naming_a_different_installation_is_left_alone(self, db: AsyncSession, index_spies):
        """It is correct for a surviving installation, so touching it would break
        a connection this operation never named."""
        _writer, index = index_spies
        index.get_reverse_installation_identity = AsyncMock(return_value={"installation_id": {"N": str(INSTALL_B)}})
        await _seed(
            db,
            installs=[str(INSTALL_A), str(INSTALL_B)],
            rows=[_map_row("org-1", INSTALL_A), _map_row("org-1", INSTALL_B, scope_id="55555")],
        )

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        index.delete_identity.assert_not_awaited()
        index.write_reverse_installation_identity.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. Honest reporting
# ---------------------------------------------------------------------------


class TestProviderFailureIsReported:
    async def test_a_failed_github_revoke_does_not_report_success(self, db: AsyncSession, index_spies):
        """The defect: `except Exception: logger.warning(...)` then `deleted=True`.
        The one step an operator cannot do locally failed silently."""
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        with pytest.raises(RuntimeError, match="retry the disconnect"):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(revoke_fails=True),
                caller_is_admin=True,
            )

    async def test_a_failed_revoke_changes_nothing_locally(self, db: AsyncSession, index_spies):
        """The recoverability property. Leaving the claims intact is what lets the
        retry authorize itself; it also means there is no half-state to reconcile.
        """
        writer, index = index_spies
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        with pytest.raises(RuntimeError):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(revoke_fails=True),
                caller_is_admin=True,
            )

        org = await db.get(Organization, "org-1")
        assert [str(i) for i in org.github_installation_ids] == [str(INSTALL_A)]
        rows = (await db.execute(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(INSTALL_A)))).scalars().all()
        assert len(rows) == 1
        # No projection was touched either — a deleted routing row with the claim
        # still in Postgres is the self-resurrecting state item 3 exists to avoid.
        writer.sync_org_channels.assert_not_awaited()
        index.delete_identity.assert_not_awaited()

    async def test_a_revoke_that_cannot_be_attempted_is_refused(self, db: AsyncSession, index_spies):
        """No App credentials => no client => no call. Proceeding here would
        report a revocation that never happened, by a different route."""
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        with pytest.raises(ValueError, match="credentials are unavailable"):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=None,
                caller_is_admin=True,
            )

        org = await db.get(Organization, "org-1")
        assert [str(i) for i in org.github_installation_ids] == [str(INSTALL_A)]

    async def test_an_already_uninstalled_app_still_completes_locally(self, db: AsyncSession, index_spies):
        """`delete_installation` treats GitHub's 404 as success, which is what
        makes recovery idempotent: an operator who uninstalled in the GitHub UI
        (or a retry after a crash) must still be able to clear the local claims."""
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),  # returns None, as the client does on 404
            caller_is_admin=True,
        )

        assert result.deleted is True
        org = await db.get(Organization, "org-1")
        assert org.github_installation_ids == []

    async def test_a_successful_revoke_reports_cleanly(self, db: AsyncSession, index_spies):
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        assert (result.deleted, result.provider_revoked, result.residual, result.warning) == (True, True, [], None)

    async def test_a_failed_projection_cleanup_is_named_in_residual(self, db: AsyncSession, index_spies):
        """A stale routing projection is how a disconnected installation keeps
        delivering events, so "best effort" must not mean "unreported"."""
        writer, index = index_spies
        writer.sync_org_channels = AsyncMock(side_effect=RuntimeError("DDB down"))
        index.delete_identity = AsyncMock(return_value=False)
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        assert result.deleted is True
        assert "identity_index_forward_row" in result.residual
        assert result.warning is not None

    async def test_one_failing_cleanup_does_not_skip_the_others(self, db: AsyncSession, index_spies):
        """They were sequential in one try block, so the first exception
        abandoned every later cleanup."""
        writer, index = index_spies
        writer.sync_org_channels = AsyncMock(side_effect=RuntimeError("DDB down"))
        index.delete_identity = AsyncMock(return_value=False)
        index.get_reverse_installation_identity = AsyncMock(return_value={"installation_id": {"N": str(INSTALL_A)}})
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        # The forward-row failure did not prevent the reverse row from being
        # attempted, and both are reported.
        assert "identity_index_forward_row" in result.residual
        assert "identity_index_reverse_row" in result.residual
        index.delete_identity.assert_any_await("org_installation", "org-1")


# ---------------------------------------------------------------------------
# 3. Idempotent recovery
# ---------------------------------------------------------------------------


class TestRetryIsRecoverable:
    async def test_a_disconnect_that_failed_at_the_provider_succeeds_on_retry(self, db: AsyncSession, index_spies):
        """The property the old ordering could not have. Had the local claims been
        deleted before the failed revoke, this retry would resolve NOT_FOUND and
        raise — leaving the installation live at GitHub with nothing left locally
        to authorize removing it.
        """
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        with pytest.raises(RuntimeError):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(revoke_fails=True),
                caller_is_admin=True,
            )

        gh = _gh()
        second = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=gh,
            caller_is_admin=True,
        )

        assert (second.deleted, second.provider_revoked) == (True, True)
        gh.delete_installation.assert_awaited_once_with(INSTALL_A)
        org = await db.get(Organization, "org-1")
        assert org.github_installation_ids == []

    async def test_the_routing_row_is_deleted_directly_when_the_org_sync_fails(self, db: AsyncSession, index_spies):
        """The forward row is the one cleanup that IS a security property — the
        webhook path reads DDB first, so a surviving row keeps delivering events to
        a tenant whose authority was just revoked.

        It cannot be left to a retry: the Postgres claims are already gone, so a
        re-run resolves NOT_FOUND and raises (pinned below). Hence a second,
        narrower attempt in-line. `sync_org_channels` diffs the org's whole channel
        set and upserts survivors, so it has many more ways to fail than the single
        DeleteItem that is all routing actually requires.
        """
        writer, index = index_spies
        writer.sync_org_channels = AsyncMock(side_effect=RuntimeError("DDB down"))
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        index.delete_identity.assert_any_await("github_installation_id", str(INSTALL_A))
        assert result.residual == []

    async def test_the_forward_row_is_residual_only_when_both_attempts_fail(self, db: AsyncSession, index_spies):
        writer, index = index_spies
        writer.sync_org_channels = AsyncMock(side_effect=RuntimeError("DDB down"))
        index.delete_identity = AsyncMock(return_value=False)
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        assert "identity_index_forward_row" in result.residual
        assert result.warning is not None

    async def test_a_residual_cleanup_cannot_be_retried_by_rerunning(self, db: AsyncSession, index_spies):
        """Pins the honest limit, so the docstring cannot drift back to claiming
        re-running repairs residual state. Revocation has already succeeded, so
        there is nothing left to resolve and the retry 404s by design — which is
        precisely why `residual` is reported for operator action."""
        writer, index = index_spies
        writer.sync_org_channels = AsyncMock(side_effect=RuntimeError("DDB down"))
        index.delete_identity = AsyncMock(return_value=False)
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        first = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )
        assert first.residual

        with pytest.raises(ValueError, match="not connected to any ADP tenant"):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(),
                caller_is_admin=True,
            )

    async def test_a_half_torn_down_installation_can_still_be_finished(self, db: AsyncSession, index_spies):
        """Only the map row was deleted — the exact state the OLD implementation
        left behind. The org JSON claim still grants ownership, so the retry must
        resolve it and clear that claim rather than reporting nothing to do."""
        await _seed(db, installs=[str(INSTALL_A)], rows=[])

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        assert result.deleted is True
        org = await db.get(Organization, "org-1")
        assert org.github_installation_ids == []

    async def test_a_fully_revoked_installation_reports_not_found(self, db: AsyncSession, index_spies):
        """Once nothing claims it, there is nothing to revoke — a 404, not a
        silent success that would imply a provider call had been made."""
        await _seed(db, installs=[], rows=[])

        with pytest.raises(ValueError, match="not connected to any ADP tenant"):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(),
                caller_is_admin=True,
            )


# ---------------------------------------------------------------------------
# Keying: the defect that made the wrong installation revocable
# ---------------------------------------------------------------------------


class TestOwnershipIsKeyedOnTheInstallation:
    async def test_revoking_one_installation_does_not_delete_a_siblings_mapping(self, db: AsyncSession, index_spies):
        """Both checks used to match `provider_scope_id`, the GitHub ACCOUNT scope
        key, so the row deleted was whichever one that account matched rather than
        the installation the caller named.

        The two rows carry different scope keys because
        `uq_channel_tenant_map_provider_scope` makes that unique — which is
        exactly why keying ownership on it was wrong: it is a per-account key with
        one slot, and it cannot represent an account's second installation at all.
        The account here reinstalled (INSTALL_B on scope 98765) while a stale row
        for INSTALL_A survived under the login-form scope key, the mixed-keyspace
        shape migration 026 documents.
        """
        await _seed(
            db,
            installs=[str(INSTALL_A), str(INSTALL_B)],
            rows=[_map_row("org-1", INSTALL_A, scope_id="acme"), _map_row("org-1", INSTALL_B)],
        )

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        remaining = (await db.execute(select(ChannelTenantMap.installation_id))).scalars().all()
        assert remaining == [str(INSTALL_B)]

    async def test_a_surviving_personal_install_keeps_the_accounts_identity_fields(self, db: AsyncSession, index_spies):
        """`github_org_id` must not be nulled while any installation is still connected.

        The "is anything left?" test used to read only `github_installation_ids`.
        A PERSONAL install never lands there — `install_callback` appends to that
        column only for `account_type == "Organization"` — so a tenant whose
        connections are all personal has an empty column and live map rows.
        Disconnecting one of them therefore looked like "nothing left" and cleared
        the account identity fields out from under the survivors, and
        `resolve_installation_owner` refuses to attest a tenant with no
        `github_org_id` (UNATTESTABLE). That is the loss of routing the guard was
        written to prevent, caused by the guard consulting one of the two stores.
        """
        db.add(_org("org-1", installs=[]))
        db.add(_map_row("org-1", INSTALL_A, scope_id="acct-a"))
        db.add(_map_row("org-1", INSTALL_B, scope_id="acct-b"))
        await db.commit()

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        org = (await db.execute(select(Organization).where(Organization.id == "org-1"))).scalar_one()
        assert org.github_org_id == ACCOUNT_ID
        assert org.github_app_id == "app-1"
        # The named installation is still gone; only the survivor's attestability
        # is preserved.
        assert (await db.execute(select(ChannelTenantMap.installation_id))).scalars().all() == [str(INSTALL_B)]

    async def test_the_last_installation_still_clears_the_identity_fields(self, db: AsyncSession, index_spies):
        """The other side of the guard: with nothing left in EITHER store, clear.

        Without this, widening the check to the map rows could silently turn the
        clearing behaviour off altogether.
        """
        await _seed(db, installs=[str(INSTALL_A)], rows=[_map_row("org-1", INSTALL_A)])

        await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        org = (await db.execute(select(Organization).where(Organization.id == "org-1"))).scalar_one()
        assert org.github_org_id is None
        assert org.github_app_id is None

    async def test_a_cross_tenant_disconnect_is_refused(self, db: AsyncSession, index_spies):
        db.add(_org("org-1", installs=[]))
        db.add(_org("org-2", installs=[str(INSTALL_A)]))
        db.add(_map_row("org-2", INSTALL_A))
        await db.commit()

        with pytest.raises(PermissionError, match="different ADP tenant"):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(),
                caller_is_admin=True,
            )

        rows = (await db.execute(select(ChannelTenantMap))).scalars().all()
        assert len(rows) == 1

    async def test_a_quarantined_installation_is_refused_not_guessed(self, db: AsyncSession, index_spies):
        """Migration 026 records cross-tenant conflicts and deliberately does not
        pick a winner. Deleting "the" mapping here would resolve the conflict by
        guessing, in the caller's favour."""
        from src.shared.models.vault import InstallationOwnershipConflict

        db.add(_org("org-1", installs=[str(INSTALL_A)]))
        db.add(_org("org-2", installs=[str(INSTALL_A)]))
        db.add(_map_row("org-1", INSTALL_A))
        db.add(InstallationOwnershipConflict(id="c1", installation_id=str(INSTALL_A), org_id="org-1", source="channel_tenant_map"))
        db.add(InstallationOwnershipConflict(id="c2", installation_id=str(INSTALL_A), org_id="org-2", source="organizations.github_installation_ids"))
        await db.commit()

        with pytest.raises(PermissionError, match="quarantined"):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(),
                caller_is_admin=True,
            )

        org = await db.get(Organization, "org-1")
        assert [str(i) for i in org.github_installation_ids] == [str(INSTALL_A)]


class TestSelfAssertedClaimsCanBeRetracted:
    """An UNATTESTABLE claim (org JSON only, no server-written map row) must stay
    revocable. The resolver withholds ownership there so a self-assertion cannot
    GRANT authority — but this path only REMOVES it, and refusing would leave the
    tenant listed as an owner with no way to stop being one."""

    async def test_an_admin_can_retract_their_own_unattestable_claim(self, db: AsyncSession, index_spies):
        db.add(_org("org-1", installs=[str(INSTALL_A)], github_org_id=None))
        await db.commit()

        result = await delete_connection(
            installation_id=INSTALL_A,
            caller_org_id="org-1",
            db=db,
            github_client=_gh(),
            caller_is_admin=True,
        )

        assert result.deleted is True
        org = await db.get(Organization, "org-1")
        assert org.github_installation_ids == []

    async def test_a_tenant_cannot_retract_another_tenants_claim(self, db: AsyncSession, index_spies):
        """The bypass this allowance must not open: if UNATTESTABLE let any
        caller through, org-1 could revoke org-2's installation."""
        db.add(_org("org-1", installs=[], github_org_id=None))
        db.add(_org("org-2", installs=[str(INSTALL_A)], github_org_id=None))
        await db.commit()

        with pytest.raises(PermissionError, match="different ADP tenant"):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(),
                caller_is_admin=True,
            )

        org2 = await db.get(Organization, "org-2")
        assert [str(i) for i in org2.github_installation_ids] == [str(INSTALL_A)]

    async def test_a_non_admin_cannot_retract_a_claim_with_no_recorded_installer(self, db: AsyncSession, index_spies):
        """With no map row there is no `installed_by_user_id` to appeal to, so
        admin is the only standing that can retract a tenant-level claim."""
        db.add(_org("org-1", installs=[str(INSTALL_A)], github_org_id=None))
        db.add(
            User(
                id="user-1",
                org_id="org-1",
                team_id="team-1",
                email="u@test.local",
                cognito_sub="sub-1",
            )
        )
        await db.commit()

        with pytest.raises(PermissionError, match="do not have permission"):
            await delete_connection(
                installation_id=INSTALL_A,
                caller_org_id="org-1",
                db=db,
                github_client=_gh(),
                caller_user_id="user-1",
                caller_is_admin=False,
            )
