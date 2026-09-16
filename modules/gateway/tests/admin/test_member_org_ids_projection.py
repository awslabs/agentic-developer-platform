"""Issue #4849: the consolidated member_org_ids write-through.

``src/admin/memberships.py::project_member_org_ids`` replaced four divergent
inline copies of this projection (and gave five membership-write call sites that
had none). These tests pin the contract every one of those callers now depends
on:

- the projected list is the user's FULL committed membership set
- ``is_active`` is NOT a filter (it marks the selected workspace, not validity)
- a multi-org user's several ``user_identities`` rows all get projected
- a projection failure is reported, never raised — the committed Postgres row
  must not be turned into an error response

The last group covers the *idempotent* branches (re-approve, reinstall). Those
early-return before their caller's normal post-commit work, so they historically
skipped the projection — which is backwards: an idempotent replay is exactly the
recurring event that can heal a projection whose write previously failed.

SQLite caveat, same as ``test_onboarding_membership_write.py``: this suite runs
on SQLite, so it proves the portable query semantics, not the PostgreSQL-only
partial unique index on ``tenant_memberships``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.memberships import project_member_org_ids
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity

pytestmark = pytest.mark.asyncio


def _writer(ok: bool = True) -> MagicMock:
    """A stub IdentityIndexWriter recording update_user_membership_orgs calls."""
    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(return_value=ok)
    return writer


async def _user(db: AsyncSession, *, org_id: str = "acme") -> User:
    user = User(
        id=new_uuid(),
        email=f"{new_uuid()}@example.com",
        cognito_sub=f"sub-{new_uuid()}",
        org_id=org_id,
        team_id="default",
        role="member",
    )
    db.add(user)
    await db.flush()
    return user


async def _identity(db: AsyncSession, user: User, provider_user_id: str, org_id: str = "acme") -> UserIdentity:
    identity = UserIdentity(
        id=new_uuid(),
        user_id=user.id,
        team_id="default",
        org_id=org_id,
        provider="github",
        provider_user_id=provider_user_id,
        verification_method="oauth",
    )
    db.add(identity)
    await db.flush()
    return identity


async def _membership(db: AsyncSession, user: User, tenant_id: str, *, is_active: bool = False) -> TenantMembership:
    membership = TenantMembership(
        user_id=user.id,
        tenant_id=tenant_id,
        role="member",
        is_active=is_active,
        joined_via="test",
    )
    db.add(membership)
    await db.flush()
    return membership


# ---------------------------------------------------------------------------
# What gets projected
# ---------------------------------------------------------------------------


async def test_projects_single_membership(db_session: AsyncSession):
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")
    await _membership(db_session, user, "acme", is_active=True)

    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is True

    writer.update_user_membership_orgs.assert_awaited_once()
    kwargs = writer.update_user_membership_orgs.await_args.kwargs
    assert kwargs["provider_user_id"] == "20402445"
    assert kwargs["provider"] == "github"
    assert kwargs["member_org_ids"] == ["acme"]


async def test_projects_every_membership_not_just_the_active_one(db_session: AsyncSession):
    """The regression this consolidation exists to prevent.

    ``is_active`` marks which single membership is the user's currently-selected
    workspace — at most one row per user carries it. Filtering on it would
    project exactly one org for a multi-org user and silently strip the rest,
    which for a fail-closed reader is a denial.
    """
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")
    await _membership(db_session, user, "acme", is_active=True)
    await _membership(db_session, user, "globex", is_active=False)
    await _membership(db_session, user, "initech", is_active=False)

    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is True

    projected = writer.update_user_membership_orgs.await_args.kwargs["member_org_ids"]
    assert sorted(projected) == ["acme", "globex", "initech"]


async def test_projects_memberships_with_no_active_row_at_all(db_session: AsyncSession):
    """A user whose only membership is inactive still holds that membership."""
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")
    await _membership(db_session, user, "acme", is_active=False)

    writer = _writer()
    await project_member_org_ids(db_session, user_id=user.id, writer=writer)

    assert writer.update_user_membership_orgs.await_args.kwargs["member_org_ids"] == ["acme"]


async def test_projects_empty_list_when_all_memberships_revoked(db_session: AsyncSession):
    """Revocation must publish the empty list, not skip the write.

    Skipping would leave the previous org list in place and keep a revoked user
    reading as eligible until the next reconciliation.
    """
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")

    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is True

    writer.update_user_membership_orgs.assert_awaited_once()
    assert writer.update_user_membership_orgs.await_args.kwargs["member_org_ids"] == []


# ---------------------------------------------------------------------------
# Multi-identity fan-out (the latent bug the old copies carried)
# ---------------------------------------------------------------------------


async def test_fans_out_over_every_github_identity_row(db_session: AsyncSession):
    """``user_identities`` is unique per (provider, provider_user_id, org_id).

    One GitHub account joining two orgs therefore has two rows. All four previous
    inline copies used ``scalar_one_or_none()``, which raises MultipleResultsFound
    on exactly this shape — so the projection silently never happened for the
    multi-org users who most need it.
    """
    user = await _user(db_session)
    await _identity(db_session, user, "20402445", org_id="acme")
    await _identity(db_session, user, "20402445", org_id="globex")
    await _membership(db_session, user, "acme", is_active=True)
    await _membership(db_session, user, "globex")

    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is True

    # Same GitHub id in both rows -> one DDB key -> one write, not a crash.
    writer.update_user_membership_orgs.assert_awaited_once()
    assert sorted(writer.update_user_membership_orgs.await_args.kwargs["member_org_ids"]) == ["acme", "globex"]


async def test_projects_each_distinct_github_id(db_session: AsyncSession):
    """Two different GitHub accounts linked to one ADP user -> two DDB writes."""
    user = await _user(db_session)
    await _identity(db_session, user, "20402445", org_id="acme")
    await _identity(db_session, user, "99999999", org_id="globex")
    await _membership(db_session, user, "acme", is_active=True)

    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is True

    ids = {c.kwargs["provider_user_id"] for c in writer.update_user_membership_orgs.await_args_list}
    assert ids == {"20402445", "99999999"}


async def test_ignores_non_github_identities(db_session: AsyncSession):
    """The projection is keyed on the GitHub id; a Slack row is not a key."""
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")
    slack = UserIdentity(
        id=new_uuid(),
        user_id=user.id,
        team_id="default",
        org_id="acme",
        provider="slack",
        provider_user_id="U0123SLACK",
        verification_method="oauth",
    )
    db_session.add(slack)
    await _membership(db_session, user, "acme", is_active=True)
    await db_session.flush()

    writer = _writer()
    await project_member_org_ids(db_session, user_id=user.id, writer=writer)

    ids = {c.kwargs["provider_user_id"] for c in writer.update_user_membership_orgs.await_args_list}
    assert ids == {"20402445"}


async def test_union_across_users_sharing_a_github_identity(db_session: AsyncSession):
    """PR #4916 review F1: a single-user write must not clobber a sibling user.

    The DDB key is per GITHUB ACCOUNT, and the per-org unique index on
    ``user_identities`` means one GitHub account maps to N ``users.id`` rows —
    one per org it joined. A projection computed as ``WHERE user_id = :the_one``
    would shrink the shared key to just the mutated user's orgs, silently
    erasing the sibling's memberships from what a fail-closed reader sees. The
    helper must write the UNION across all user rows holding the identity — the
    backfill script's semantics — so write-through and reconciliation agree.
    """
    u1 = await _user(db_session, org_id="org1")
    u2 = await _user(db_session, org_id="org2")
    await _identity(db_session, u1, "123", org_id="org1")
    await _identity(db_session, u2, "123", org_id="org2")
    await _membership(db_session, u1, "org1", is_active=True)
    await _membership(db_session, u2, "org2", is_active=True)

    # A mutation touching only U2 still projects the whole account.
    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=u2.id, writer=writer) is True

    writer.update_user_membership_orgs.assert_awaited_once()
    kwargs = writer.update_user_membership_orgs.await_args.kwargs
    assert kwargs["provider_user_id"] == "123"
    # U1's org1 survives: union, not ["org2"].
    assert sorted(kwargs["member_org_ids"]) == ["org1", "org2"]


async def test_shared_identity_revocation_keeps_the_siblings_orgs(db_session: AsyncSession):
    """Revoking U2's last membership must leave U1's orgs on the shared key."""
    u1 = await _user(db_session, org_id="org1")
    u2 = await _user(db_session, org_id="org2")
    await _identity(db_session, u1, "123", org_id="org1")
    await _identity(db_session, u2, "123", org_id="org2")
    await _membership(db_session, u1, "org1", is_active=True)
    # U2 holds no memberships (just revoked).

    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=u2.id, writer=writer) is True

    writer.update_user_membership_orgs.assert_awaited_once()
    assert writer.update_user_membership_orgs.await_args.kwargs["member_org_ids"] == ["org1"]


async def test_scoped_to_the_requested_user(db_session: AsyncSession):
    """Another user's memberships must never leak into this projection."""
    user = await _user(db_session)
    other = await _user(db_session)
    await _identity(db_session, user, "20402445", org_id="acme")
    await _identity(db_session, other, "88888888", org_id="globex")
    await _membership(db_session, user, "acme", is_active=True)
    await _membership(db_session, other, "globex", is_active=True)

    writer = _writer()
    await project_member_org_ids(db_session, user_id=user.id, writer=writer)

    writer.update_user_membership_orgs.assert_awaited_once()
    kwargs = writer.update_user_membership_orgs.await_args.kwargs
    assert kwargs["provider_user_id"] == "20402445"
    assert kwargs["member_org_ids"] == ["acme"]


# ---------------------------------------------------------------------------
# Never raises — a projection fault must not fail a committed write
# ---------------------------------------------------------------------------


async def test_no_github_identity_is_a_no_op_success(db_session: AsyncSession):
    """Memberships can exist before any GitHub identity is linked."""
    user = await _user(db_session)
    await _membership(db_session, user, "acme", is_active=True)

    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is True
    writer.update_user_membership_orgs.assert_not_awaited()


async def test_unknown_user_id_is_a_no_op_success(db_session: AsyncSession):
    writer = _writer()
    assert await project_member_org_ids(db_session, user_id=new_uuid(), writer=writer) is True
    writer.update_user_membership_orgs.assert_not_awaited()


async def test_failed_write_returns_false_without_raising(db_session: AsyncSession):
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")
    await _membership(db_session, user, "acme", is_active=True)

    assert await project_member_org_ids(db_session, user_id=user.id, writer=_writer(ok=False)) is False


async def test_writer_exception_returns_false_without_raising(db_session: AsyncSession):
    """The caller has already committed; an exception here would surface a 500
    for a membership change that in fact succeeded."""
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")
    await _membership(db_session, user, "acme", is_active=True)

    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(side_effect=RuntimeError("ddb down"))

    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is False


async def test_partial_fan_out_failure_returns_false_and_still_tries_the_rest(db_session: AsyncSession):
    """One bad DDB key must not abort the other identities' projections."""
    user = await _user(db_session)
    await _identity(db_session, user, "11111111", org_id="acme")
    await _identity(db_session, user, "22222222", org_id="globex")
    await _membership(db_session, user, "acme", is_active=True)

    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(side_effect=[False, True])

    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is False
    assert writer.update_user_membership_orgs.await_count == 2


async def test_projection_failure_leaves_postgres_state_intact(db_session: AsyncSession):
    """The Postgres row is authoritative and already committed.

    Pins the ordering contract: the helper only reads through the session, so a
    DDB failure cannot roll back, mutate, or invalidate the membership.
    """
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")
    await _membership(db_session, user, "acme", is_active=True)

    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await project_member_org_ids(db_session, user_id=user.id, writer=writer) is False

    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == user.id))).scalars().all())
    assert [r.tenant_id for r in rows] == ["acme"]
    assert rows[0].is_active is True


async def test_uses_targeted_update_not_a_full_row_write(db_session: AsyncSession):
    """Wipe-safety: the projection goes through ``update_user_membership_orgs``.

    That method issues an UpdateItem touching only ``member_org_ids``. Routing it
    through ``put_user_identity`` instead would overwrite the whole row, so a
    caller lacking the full identity context would blank attributes it never
    meant to touch.
    """
    user = await _user(db_session)
    await _identity(db_session, user, "20402445")
    await _membership(db_session, user, "acme", is_active=True)

    writer = _writer()
    writer.put_user_identity = AsyncMock(return_value=True)

    await project_member_org_ids(db_session, user_id=user.id, writer=writer)

    writer.update_user_membership_orgs.assert_awaited_once()
    writer.put_user_identity.assert_not_awaited()
