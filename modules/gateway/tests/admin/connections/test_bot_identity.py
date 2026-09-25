"""Tests for src.admin.connections.bot_identity.seed_bot_identity.

Covers the fix that folds platform/scripts/seed-bot-identities.py's manual
one-off into the install-callback flow: the agent's own bot account should
resolve as a known identity from the very first installation, instead of
403'ing as unknown_user on its own comment activity until someone notices
and runs the script by hand.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from src.admin.connections.bot_identity import seed_bot_identity
from src.shared.models.organization import Department, Team, User


@pytest.fixture(autouse=True)
def identity_writer(monkeypatch):
    writer = MagicMock()
    writer.put_user_identity = AsyncMock(return_value=True)
    monkeypatch.setattr("src.admin.connections.bot_identity.IdentityIndexWriter", lambda: writer)
    return writer


def _github_client(bot_id: int = 317952797, bot_login: str = "es-adp[bot]"):
    client = MagicMock()
    client.get_bot_user = AsyncMock(return_value={"id": bot_id, "login": bot_login, "type": "Bot"})
    return client


@pytest.mark.asyncio
async def test_creates_department_team_and_bot_user(db_session):
    github_client = _github_client()

    await seed_bot_identity(
        installation_id=111,
        org_id="org-001",
        app_slug="es-adp",
        github_client=github_client,
        db=db_session,
    )

    dept = (await db_session.execute(select(Department).where(Department.org_id == "org-001"))).scalar_one()
    assert dept.name == "bot-agents"

    team = (await db_session.execute(select(Team).where(Team.org_id == "org-001"))).scalar_one()
    assert team.name == "bot-agents"
    assert team.department_id == dept.id

    user = (await db_session.execute(select(User).where(User.org_id == "org-001", User.user_kind == "bot"))).scalar_one()
    assert user.bot_kind == "es-adp"
    assert user.team_id == team.id
    assert user.is_shadow is False


@pytest.mark.asyncio
async def test_writes_ddb_identity_index_with_bot_fields(db_session, monkeypatch):
    github_client = _github_client(bot_id=42, bot_login="my-app[bot]")

    mock_writer = MagicMock()
    mock_writer.put_user_identity = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.admin.connections.bot_identity.IdentityIndexWriter",
        lambda: mock_writer,
    )

    await seed_bot_identity(
        installation_id=111,
        org_id="org-002",
        app_slug="my-app",
        github_client=github_client,
        db=db_session,
    )

    mock_writer.put_user_identity.assert_awaited_once()
    call_kwargs = mock_writer.put_user_identity.call_args.kwargs
    assert call_kwargs["provider_user_id"] == "42"
    assert call_kwargs["org_id"] == "org-002"
    assert call_kwargs["provider_username"] == "my-app[bot]"
    assert call_kwargs["user_kind"] == "bot"
    assert call_kwargs["bot_kind"] == "my-app"
    from src.shared.models.vault import UserIdentity

    row = (await db_session.scalars(select(UserIdentity))).one()
    assert row.verification_method == call_kwargs["verification_method"] == "admin_attested"


@pytest.mark.asyncio
async def test_second_call_is_idempotent(db_session):
    """A reinstall (or the two install-callback call sites both firing) must
    not create duplicate department/team/user rows."""
    github_client = _github_client()

    await seed_bot_identity(installation_id=111, org_id="org-003", app_slug="es-adp", github_client=github_client, db=db_session)
    await seed_bot_identity(installation_id=111, org_id="org-003", app_slug="es-adp", github_client=github_client, db=db_session)

    depts = (await db_session.execute(select(Department).where(Department.org_id == "org-003"))).scalars().all()
    users = (await db_session.execute(select(User).where(User.org_id == "org-003", User.user_kind == "bot"))).scalars().all()
    assert len(depts) == 1
    assert len(users) == 1


@pytest.mark.asyncio
async def test_no_github_client_skips_without_raising(db_session):
    """No GitHub client available (e.g. app credentials not configured) —
    must not raise, since this is a best-effort seed that must never block
    the install it's attached to."""
    await seed_bot_identity(installation_id=111, org_id="org-004", app_slug="es-adp", github_client=None, db=db_session)

    users = (await db_session.execute(select(User).where(User.org_id == "org-004"))).scalars().all()
    assert users == []


@pytest.mark.asyncio
async def test_github_lookup_failure_skips_without_raising(db_session):
    """The bot's numeric id can't be resolved (rate-limited, network error,
    app not yet fully provisioned) — must degrade to a no-op, not raise."""
    github_client = MagicMock()
    github_client.get_bot_user = AsyncMock(side_effect=Exception("boom"))

    await seed_bot_identity(installation_id=111, org_id="org-005", app_slug="es-adp", github_client=github_client, db=db_session)

    users = (await db_session.execute(select(User).where(User.org_id == "org-005"))).scalars().all()
    assert users == []


@pytest.mark.parametrize("existing_kind", ["legacy", "random", "linked"])
async def test_preserves_existing_bot_identity(db_session, identity_writer, existing_kind):
    import uuid

    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.vault import UserIdentity

    user_id = str(uuid.uuid5(uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890"), "github:42")) if existing_kind == "legacy" else "existing-bot"
    bot_kind = "my-app" if existing_kind == "random" else "agent-developer"
    user = User(id=user_id, org_id="original-org", team_id="original-team", email="original@bot.adp.local", user_kind="bot", bot_kind=bot_kind)
    db_session.add(user)
    await db_session.flush()
    if existing_kind == "linked":
        db_session.add(
            UserIdentity(
                org_id=user.org_id,
                user_id=user.id,
                team_id=user.team_id,
                provider="github",
                provider_user_id="42",
                verification_method="admin_manual",
            )
        )
    await db_session.commit()

    await seed_bot_identity(installation_id=111, org_id="new-org", app_slug="my-app", github_client=_github_client(42, "my-app[bot]"), db=db_session)

    assert len((await db_session.scalars(select(User))).all()) == 1
    call = identity_writer.put_user_identity.call_args.kwargs
    assert call["user_id"] == user_id
    assert call["org_id"] == "original-org"
    assert call["bot_kind"] == bot_kind
    assert call["member_org_ids"] == ["new-org", "original-org"]
    rows = (await db_session.scalars(select(UserIdentity))).all()
    assert rows and {row.verification_method for row in rows} == {"admin_attested"}
    assert call["verification_method"] == "admin_attested"
    memberships = (await db_session.scalars(select(TenantMembership))).all()
    assert {m.role for m in memberships} == {"member"}


@pytest.mark.parametrize("conflict", ["human", "multiple_bots", "different_github_id"])
async def test_conflicting_identity_is_not_reassigned(db_session, identity_writer, conflict):
    from src.shared.models.vault import UserIdentity

    user = User(
        id="existing-user",
        org_id="original-org",
        team_id="team",
        email="existing@example.com",
        user_kind="human" if conflict == "human" else "bot",
        bot_kind="my-app",
    )
    db_session.add(user)
    await db_session.flush()
    if conflict == "multiple_bots":
        db_session.add(User(id="another-user", org_id="other-org", team_id="team", email="another@example.com", user_kind="bot", bot_kind="my-app"))
    else:
        db_session.add(
            UserIdentity(
                org_id=user.org_id,
                user_id=user.id,
                team_id=user.team_id,
                provider="github",
                provider_user_id="42" if conflict == "human" else "99",
                verification_method="admin_manual",
            )
        )
    await db_session.commit()

    await seed_bot_identity(installation_id=111, org_id="new-org", app_slug="my-app", github_client=_github_client(42, "my-app[bot]"), db=db_session)

    identity_writer.put_user_identity.assert_not_awaited()
    await db_session.refresh(user)
    assert user.org_id == "original-org"
    assert user.user_kind == ("human" if conflict == "human" else "bot")


async def test_ddb_failure_is_repaired_by_reinstall(db_session, identity_writer):
    from src.shared.models.vault import UserIdentity

    identity_writer.put_user_identity.side_effect = [RuntimeError("DDB unavailable"), True]
    client = _github_client()
    await seed_bot_identity(installation_id=111, org_id="org-a", app_slug="es-adp", github_client=client, db=db_session)
    user = (await db_session.scalars(select(User))).one()
    assert (await db_session.scalars(select(UserIdentity))).one().user_id == user.id
    await seed_bot_identity(installation_id=111, org_id="org-b", app_slug="es-adp", github_client=client, db=db_session)
    assert identity_writer.put_user_identity.call_args.kwargs["user_id"] == user.id
    assert identity_writer.put_user_identity.call_args.kwargs["member_org_ids"] == ["org-a", "org-b"]


@pytest.mark.parametrize("failure", [False, True])
async def test_seed_never_commits_or_rolls_back_callers_work(db_session, identity_writer, monkeypatch, failure):
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.admin.connections import bot_identity
    from src.shared.models.organization import Organization

    if failure:

        async def fail(db, **kwargs):
            db.add(User(id="partial-bot", org_id="org-a", team_id="team", email="partial@example.com"))
            await db.flush()
            raise RuntimeError("Postgres seed failed")

        monkeypatch.setattr(bot_identity, "_ensure_bot_user", fail)
    pending = Organization(id="pending-org", name="Uncommitted caller change")
    db_session.add(pending)
    await seed_bot_identity(installation_id=111, org_id="org-a", app_slug="es-adp", github_client=_github_client(), db=db_session)
    assert pending in db_session.new
    async with AsyncSession(bind=db_session.bind) as independent:
        assert await independent.get(Organization, "pending-org") is None
        if failure:
            assert (await independent.scalars(select(User))).all() == []
            identity_writer.put_user_identity.assert_not_awaited()


@pytest.mark.parametrize(
    "bot_response",
    [
        {"id": 42, "login": "my-app[bot]", "type": "User"},
        {"id": 42, "login": "different-app[bot]", "type": "Bot"},
        {"id": None, "login": "my-app[bot]", "type": "Bot"},
    ],
)
async def test_invalid_github_lookup_does_not_seed(db_session, identity_writer, bot_response):
    client = _github_client()
    client.get_bot_user.return_value = bot_response
    await seed_bot_identity(installation_id=111, org_id="org-a", app_slug="my-app", github_client=client, db=db_session)
    assert (await db_session.scalars(select(User))).all() == []
    identity_writer.put_user_identity.assert_not_awaited()
