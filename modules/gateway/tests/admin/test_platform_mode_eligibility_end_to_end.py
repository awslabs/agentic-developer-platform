"""Issue #4844: the P1a regression guard, across the whole eligibility chain.

The story's most dangerous correction. An earlier draft defined the predicate as
"≥1 **active** ``tenant_memberships`` row". That is wrong: ``is_active`` marks
which single workspace the user currently has **selected** — at most one row per
user carries it, and ``switch_tenant`` deactivates every row before activating the
target. Shipping the ``is_active`` reading would have denied sign-in to:

- every member whose selected workspace is a *different* org, and
- every member who has never selected one at all.

``tests/admin/test_member_org_ids_projection.py`` already proves the *projection*
does not filter on the flag. That is one link. This file joins the whole chain —
Postgres rows → the real projection → the DynamoDB item → the shared reader → the
broker's ``_check_allowlist`` — and asserts the thing a user actually cares about:
they are **allowed to sign in**.

Why an end-to-end test and not two unit tests: the failure mode being guarded is
an *integration* one. Every link could be individually correct while the composed
answer is "denied" — a key-shape mismatch between writer and reader, or a filter
reintroduced at either end, reads as a total login denial for valid members, and
no single-layer test would catch it. The layers are wired together here with a
fake DynamoDB table (dict-backed, honouring the real key shapes) rather than
mocks, so a divergence in those key shapes fails this test.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.memberships import project_member_org_ids
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity

pytestmark = pytest.mark.asyncio

_GATEWAY_ROOT = Path(__file__).resolve().parent.parent.parent
_GITHUB_ID = "20402445"
_LOGIN = "octocat"
_LEGACY_TABLE = "adp-test-identity-index"

# The identity_type literal the writer uses for GitHub user rows. Hard-coded here
# on purpose: if the writer and the reader ever disagree on it, this test's fake
# table will not find the row and the test fails — which is the point.
_GITHUB_USER_TYPE = "github_user"


class _FakeTable:
    """Minimal dict-backed stand-in for a DynamoDB Table resource.

    Stores items under the real composite key so a writer/reader key-shape
    mismatch surfaces as a miss rather than being papered over by a mock.
    """

    def __init__(self, key_attrs: tuple[str, ...]):
        self._key_attrs = key_attrs
        self.items: dict[tuple, dict] = {}

    def _key_of(self, item: dict) -> tuple:
        return tuple(str(item[a]) for a in self._key_attrs)

    def put(self, item: dict) -> None:
        self.items[self._key_of(item)] = item

    def get_item(self, Key: dict):  # noqa: N803 - boto3's parameter name
        key = tuple(str(Key[a]) for a in self._key_attrs)
        item = self.items.get(key)
        return {"Item": item} if item is not None else {}


@pytest.fixture
def reader(monkeypatch):
    """The real shared reader, pointed at a fake legacy identity-index table."""
    shared = _GATEWAY_ROOT / "lambda" / "shared"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    # Import fresh so module-level config picks up the env below.
    for name in ("membership_eligibility",):
        sys.modules.pop(name, None)

    monkeypatch.setenv("IDENTITY_INDEX_TABLE", _LEGACY_TABLE)
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "false")

    import membership_eligibility

    table = _FakeTable(("identity_type", "identity_value"))
    resource = MagicMock()
    resource.Table.return_value = table
    monkeypatch.setattr(membership_eligibility, "_get_resource", lambda: resource)
    membership_eligibility._table = table  # exposed for the writer stub below
    return membership_eligibility


def _writer_into(table: _FakeTable) -> MagicMock:
    """An IdentityIndexWriter stub that writes the row shape the real one writes.

    ``update_user_membership_orgs(provider_user_id=…, member_org_ids=…)`` is the
    writer's contract (``src/admin/identity/identity_index_writer.py``); the row it
    lands is keyed ``(identity_type, identity_value)`` on the legacy table.
    """
    writer = MagicMock()

    async def _update(*, provider_user_id: str, member_org_ids: list[str], **kw):
        table.put(
            {
                "identity_type": _GITHUB_USER_TYPE,
                "identity_value": str(provider_user_id),
                "member_org_ids": list(member_org_ids),
            }
        )
        return True

    writer.update_user_membership_orgs = AsyncMock(side_effect=_update)
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


async def _identity(db: AsyncSession, user: User, org_id: str = "acme") -> UserIdentity:
    identity = UserIdentity(
        id=new_uuid(),
        user_id=user.id,
        team_id="default",
        org_id=org_id,
        provider="github",
        provider_user_id=_GITHUB_ID,
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
        joined_via="admin_invite",
    )
    db.add(membership)
    await db.flush()
    return membership


def _broker():
    """The broker handler, loaded with its own dir on sys.path (flat sibling imports)."""
    import importlib.util

    mod_name = "e2e_broker_handler"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    broker_dir = _GATEWAY_ROOT / "lambda" / "github-auth-broker"
    if str(broker_dir) not in sys.path:
        sys.path.insert(0, str(broker_dir))
    spec = importlib.util.spec_from_file_location(mod_name, broker_dir / "handler.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[mod_name]
        raise
    return module


def _broker_verdict(reader_module) -> str | None:
    """Run the broker's platform-mode gate against the real reader. None ⇒ allowed."""
    broker = _broker()
    broker.ALLOWLIST_MODE = "platform"
    with patch.dict(sys.modules, {"membership_eligibility": reader_module}):
        return broker._check_allowlist(_LOGIN, "gh-token", _GITHUB_ID)


async def _project(db: AsyncSession, user: User, reader_module) -> None:
    writer = _writer_into(reader_module._table)
    assert await project_member_org_ids(db, user_id=user.id, writer=writer) is True


# ---------------------------------------------------------------------------
# The P1a guard: is_active must not gate sign-in
# ---------------------------------------------------------------------------


async def test_member_whose_active_row_points_at_another_org_can_sign_in(db_session: AsyncSession, reader):
    """THE P1a regression guard.

    This user's selected workspace is ``globex``; they also belong to ``acme``.
    Under the rejected "≥1 active row" reading, an implementation asking "is there
    an active membership in the org I care about?" denies them. Row existence is
    the predicate, so they sign in.
    """
    user = await _user(db_session)
    await _identity(db_session, user)
    await _membership(db_session, user, "acme", is_active=False)
    await _membership(db_session, user, "globex", is_active=True)

    await _project(db_session, user, reader)

    assert _broker_verdict(reader) is None


async def test_member_with_no_active_row_at_all_can_sign_in(db_session: AsyncSession, reader):
    """The second half of P1a: never having selected a workspace is not ineligibility.

    A freshly-invited member has memberships and no selection. Filtering on
    ``is_active`` would lock out exactly the cohort the ``platform`` mode exists to
    let in.
    """
    user = await _user(db_session)
    await _identity(db_session, user)
    await _membership(db_session, user, "acme", is_active=False)

    await _project(db_session, user, reader)

    assert _broker_verdict(reader) is None


async def test_multi_org_member_signs_in_with_every_org_projected(db_session: AsyncSession, reader):
    """A multi-org member is eligible, and the whole set survives the round trip.

    The set assertion is what would catch a re-introduced ``is_active`` filter that
    happens to leave the user eligible: they would still sign in, but with one org
    instead of three, which is a bug this chain must not hide.
    """
    user = await _user(db_session)
    await _identity(db_session, user)
    await _membership(db_session, user, "acme", is_active=True)
    await _membership(db_session, user, "globex", is_active=False)
    await _membership(db_session, user, "initech", is_active=False)

    await _project(db_session, user, reader)

    assert _broker_verdict(reader) is None
    item = reader._table.get_item(Key={"identity_type": _GITHUB_USER_TYPE, "identity_value": _GITHUB_ID})["Item"]
    assert sorted(item["member_org_ids"]) == ["acme", "globex", "initech"]


# ---------------------------------------------------------------------------
# The other side of the gate
# ---------------------------------------------------------------------------


async def test_user_with_no_memberships_is_denied(db_session: AsyncSession, reader):
    """A GitHub identity with an identity row but zero memberships is denied.

    This is the reader being deliberately stricter than webhook-ingress's
    ``identity_resolver``, which defaults a missing projection to the row's own
    org. That default is right for "may this user trigger work in org X?" and
    fail-OPEN for "does this person belong to any org here?".
    """
    user = await _user(db_session)
    await _identity(db_session, user)

    await _project(db_session, user, reader)

    assert _broker_verdict(reader) == "not_authorized"


async def test_github_identity_with_no_row_at_all_is_denied(db_session: AsyncSession, reader):
    """Never-seen GitHub account ⇒ no row ⇒ denied. The default state is closed."""
    assert _broker_verdict(reader) == "not_authorized"


async def test_membership_revocation_denies_after_reprojection(db_session: AsyncSession, reader):
    """Removing the last membership revokes sign-in once the projection is refreshed.

    Proves the projection is a live authority rather than a one-way grant: the
    admin-offboarding path has to be able to take eligibility away.
    """
    user = await _user(db_session)
    await _identity(db_session, user)
    membership = await _membership(db_session, user, "acme", is_active=True)
    await _project(db_session, user, reader)
    assert _broker_verdict(reader) is None

    await db_session.delete(membership)
    await db_session.flush()
    await _project(db_session, user, reader)

    assert _broker_verdict(reader) == "not_authorized"
