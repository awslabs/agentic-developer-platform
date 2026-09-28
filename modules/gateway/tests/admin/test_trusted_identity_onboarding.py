"""Author regression tests for onboarding's immutable identity boundary.

These tests use local database fixtures and mocked provider transport. They are
ordinary author validation, not the independently rejected behavioral review or
live identity/provider/session evidence.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from src.admin.onboarding.handler import (
    resolve_trusted_github_identity,
    sync_memberships_on_login,
)
from src.admin.onboarding.trusted_identity import (
    NO_LINKED_IDENTITY,
    SELF_WRITABLE_ATTRIBUTES,
    TRUSTED_LOGIN_ATTRIBUTE,
    TrustedGitHubIdentity,
    trusted_login_from_attributes,
)
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User


@contextmanager
def _provider_membership(*, provider_id=12345, role="member"):
    """Keep production matcher and role resolution; stub only provider I/O."""
    client = MagicMock()
    client.get_installation_token = AsyncMock(return_value="local-test-token")
    client.aclose = AsyncMock()

    def response(path, **kwargs):
        if "/org-alpha/" not in path:
            return MagicMock(status_code=404)
        return MagicMock(
            status_code=200,
            json=lambda: {
                "state": "active",
                "role": role,
                "user": {"id": provider_id},
            },
        )

    client._http_client.get = AsyncMock(side_effect=response)
    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app", "test-key")),
    ):
        yield client


# ---------------------------------------------------------------------------
# The resolver itself
# ---------------------------------------------------------------------------


class TestTrustedLoginAccessor:
    """``trusted_login_from_attributes`` reads ONE attribute and offers no fallback."""

    def test_reads_the_platform_written_attribute(self):
        assert trusted_login_from_attributes({TRUSTED_LOGIN_ATTRIBUTE: "octocat"}) == "octocat"

    @pytest.mark.parametrize("editable", sorted(SELF_WRITABLE_ATTRIBUTES))
    def test_ignores_every_self_writable_attribute(self, editable: str):
        """The core regression: no editable attribute may yield a login.

        Parametrized over the whole self-writable set rather than ``name`` alone, so
        adding an attribute to that set without excluding it here fails loudly.
        """
        assert trusted_login_from_attributes({editable: "victim-login"}) == ""

    def test_editable_attribute_cannot_win_even_beside_the_trusted_one(self):
        attrs = {TRUSTED_LOGIN_ATTRIBUTE: "real-user", "name": "victim-login"}
        assert trusted_login_from_attributes(attrs) == "real-user"

    def test_blank_trusted_attribute_is_not_a_login(self):
        """Whitespace must not pass as a login — it would match nothing but is not a value."""
        assert trusted_login_from_attributes({TRUSTED_LOGIN_ATTRIBUTE: "   "}) == ""


class TestResolverReportsAbsence:
    """Absence is a value the caller must read, not an empty string to guess from."""

    def test_no_verified_identity_when_only_editable_value_present(self):
        with patch(
            "src.admin.onboarding.handler._fetch_github_identity_from_cognito",
            return_value=("", ""),
        ):
            identity = resolve_trusted_github_identity({"name": "victim-login"}, "sub-1")

        assert identity == NO_LINKED_IDENTITY
        assert identity.linked is False
        assert identity.login == ""

    def test_complete_identity_comes_from_subject_bound_provider_read(self):
        with patch("src.admin.onboarding.handler._fetch_github_identity_from_cognito", return_value=("octocat", "12345")) as read:
            identity = resolve_trusted_github_identity({TRUSTED_LOGIN_ATTRIBUTE: "untrusted-payload", "cognito:username": "github_999"}, "sub-1")
        read.assert_called_once_with("sub-1")
        assert identity == TrustedGitHubIdentity(login="octocat", numeric_id="12345", linked=True)

    @pytest.mark.parametrize("numeric_id", ["", "id-of-octocat", "１２３"])
    def test_login_without_immutable_numeric_id_is_unlinked(self, numeric_id):
        with patch("src.admin.onboarding.handler._fetch_github_identity_from_cognito", return_value=("octocat", numeric_id)):
            assert resolve_trusted_github_identity({}, "sub-1") == NO_LINKED_IDENTITY

    def test_falls_back_to_platform_side_cognito_read_not_to_a_string(self):
        """The second source is another platform-written read, not a laxer guess."""
        with patch(
            "src.admin.onboarding.handler._fetch_github_identity_from_cognito",
            return_value=("octocat", "12345"),
        ):
            identity = resolve_trusted_github_identity({"name": "victim-login"}, "sub-1")

        assert identity.linked is True
        assert identity.login == "octocat"

    def test_unlinked_identity_carries_no_usable_login(self):
        """A caller that forgets to check ``linked`` still matches nothing."""
        assert NO_LINKED_IDENTITY.login == ""
        assert NO_LINKED_IDENTITY.complete is False


# ---------------------------------------------------------------------------
# No membership row from an unverified identity
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_orgs(db_session):
    """Two unrelated tenants, so "no membership in ANY org" is a real assertion."""
    teams = {}
    for slug in ("org-alpha", "org-beta"):
        org = Organization(
            id=slug,
            name=slug,
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["1"],
            cognito_client_ids=[],
        )
        db_session.add(org)
        dept = Department(id=new_uuid(), org_id=slug, name="default")
        db_session.add(dept)
        team = Team(id=new_uuid(), org_id=slug, department_id=dept.id, name="default")
        db_session.add(team)
        teams[slug] = team.id
    await db_session.flush()
    return teams


@pytest.fixture
async def alpha_user(db_session, two_orgs):
    user = User(
        id=new_uuid(),
        org_id="org-alpha",
        team_id=two_orgs["org-alpha"],
        email="u@example.com",
        name="u",
        cognito_sub="sub-alpha",
        role="member",
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _memberships(db_session) -> list[TenantMembership]:
    return list((await db_session.execute(select(TenantMembership))).scalars().all())


class TestUnverifiedIdentityCreatesNoMembership:
    async def test_unlinked_session_writes_no_membership_in_any_org(self, db_session, alpha_user, two_orgs):
        """The invariant: no verified link → no membership row anywhere.

        Asserted at the sync entry point with the org matcher left un-patched to
        prove the matcher is never even consulted — if the guard failed open, the
        matcher would run and the assertion on call count would catch it.
        """
        with patch(
            "src.admin.onboarding.handler._find_matching_tenants_for_user",
            new=AsyncMock(return_value=[]),
        ) as matcher:
            await sync_memberships_on_login(db_session, alpha_user, "", github_id="", resolved_for_sub="sub-alpha")

            assert matcher.await_count == 0

        assert await _memberships(db_session) == []

    async def test_verified_login_still_creates_membership(self, db_session, alpha_user, two_orgs):
        """The fix must not lock out a legitimately linked user (over-restriction)."""
        with (
            _provider_membership(),
            patch("src.admin.onboarding.handler.project_member_org_ids", new=AsyncMock()),
        ):
            await sync_memberships_on_login(
                db_session,
                alpha_user,
                "octocat",
                github_id="12345",
                resolved_for_sub="sub-alpha",
            )

        rows = await _memberships(db_session)
        assert [r.tenant_id for r in rows] == ["org-alpha"]
        assert rows[0].role == "member"


class TestIdentityCannotBeBorrowedByAnotherUser:
    """A verified login belonging to one person must not grant another authority."""

    async def test_membership_refused_when_login_resolved_for_a_different_sub(self, db_session, alpha_user, two_orgs):
        with patch(
            "src.admin.onboarding.handler._find_matching_tenants_for_user",
            new=AsyncMock(return_value=[]),
        ) as matcher:
            await sync_memberships_on_login(
                db_session,
                alpha_user,  # cognito_sub="sub-alpha"
                "octocat",
                github_id="12345",
                resolved_for_sub="sub-someone-else",
            )

            # Refused BEFORE the matcher runs, so no GitHub call is made on
            # behalf of a mismatched pairing either.
            assert matcher.await_count == 0

        assert await _memberships(db_session) == []


class TestProvenLinkConflictsAreRefused:
    """A10's stored evidence is consulted, not a parallel notion of "verified".

    ``resolved_for_sub`` only proves the login came from this session's own Cognito
    record. It cannot detect that the login is already proven to belong to somebody
    else, because the platform writes that attribute on its own authority. These
    cover the gap.
    """

    async def _github_identity(self, db_session, *, user_id, login, method, org="org-alpha", team=None):
        from src.shared.models.vault import UserIdentity

        row = UserIdentity(
            id=new_uuid(),
            user_id=user_id,
            org_id=org,
            team_id=team,
            provider="github",
            provider_user_id="12345",
            provider_username=login,
            verification_method=method,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    async def _other_user(self, db_session, two_orgs):
        other = User(
            id=new_uuid(),
            org_id="org-beta",
            team_id=two_orgs["org-beta"],
            email="other@example.com",
            name="other",
            cognito_sub="sub-other",
            role="member",
        )
        db_session.add(other)
        await db_session.flush()
        return other

    async def test_login_proven_for_another_user_writes_no_membership(self, db_session, alpha_user, two_orgs):
        """The borrowing case the attribute check cannot see."""
        other = await self._other_user(db_session, two_orgs)
        await self._github_identity(
            db_session,
            user_id=other.id,
            login="octocat",
            method="oauth",
            org="org-beta",
            team=two_orgs["org-beta"],
        )

        with patch(
            "src.admin.onboarding.handler._find_matching_tenants_for_user",
            new=AsyncMock(return_value=[]),
        ) as matcher:
            await sync_memberships_on_login(
                db_session,
                alpha_user,
                "octocat",
                github_id="12345",
                resolved_for_sub="sub-alpha",  # binding is satisfied — only A10's evidence catches this
            )
            assert matcher.await_count == 0

        assert await _memberships(db_session) == []

    async def test_case_difference_does_not_evade_the_conflict_check(self, db_session, alpha_user, two_orgs):
        """GitHub logins are case-insensitive, so the comparison must be too."""
        other = await self._other_user(db_session, two_orgs)
        await self._github_identity(
            db_session,
            user_id=other.id,
            login="OctoCat",
            method="oauth",
            org="org-beta",
            team=two_orgs["org-beta"],
        )

        with patch(
            "src.admin.onboarding.handler._find_matching_tenants_for_user",
            new=AsyncMock(return_value=[]),
        ) as matcher:
            await sync_memberships_on_login(db_session, alpha_user, "octocat", github_id="12345", resolved_for_sub="sub-alpha")
            assert matcher.await_count == 0

        assert await _memberships(db_session) == []

    async def test_unproven_row_for_another_user_does_not_block(self, db_session, alpha_user, two_orgs):
        """An UNPROVEN row is somebody's unverified claim — it must not be able to
        deny a legitimate user their memberships. Otherwise a self-asserted row
        becomes a denial-of-service against the real account holder."""
        other = await self._other_user(db_session, two_orgs)
        await self._github_identity(
            db_session,
            user_id=other.id,
            login="octocat",
            method="self_asserted",
            org="org-beta",
            team=two_orgs["org-beta"],
        )

        with (
            _provider_membership(),
            patch("src.admin.onboarding.handler.project_member_org_ids", new=AsyncMock()),
        ):
            await sync_memberships_on_login(db_session, alpha_user, "octocat", github_id="12345", resolved_for_sub="sub-alpha")

        assert [r.tenant_id for r in await _memberships(db_session)] == ["org-alpha"]

    async def test_users_own_proven_link_permits_sync(self, db_session, alpha_user, two_orgs):
        """The ordinary case: the proven row names this user and this login."""
        await self._github_identity(
            db_session,
            user_id=alpha_user.id,
            login="octocat",
            method="oauth",
            team=two_orgs["org-alpha"],
        )

        with (
            _provider_membership(),
            patch("src.admin.onboarding.handler.project_member_org_ids", new=AsyncMock()),
        ):
            await sync_memberships_on_login(db_session, alpha_user, "octocat", github_id="12345", resolved_for_sub="sub-alpha")

        assert [r.tenant_id for r in await _memberships(db_session)] == ["org-alpha"]

    async def test_fresh_provider_proof_permits_first_signup_without_stored_link(self, db_session, alpha_user, two_orgs):
        """No historical row is required when broker ID and provider ID agree."""
        with (
            _provider_membership(),
            patch("src.admin.onboarding.handler.project_member_org_ids", new=AsyncMock()),
        ):
            await sync_memberships_on_login(db_session, alpha_user, "octocat", github_id="12345", resolved_for_sub="sub-alpha")

        assert [r.tenant_id for r in await _memberships(db_session)] == ["org-alpha"]


class TestBindingIsEnforcedNotConventional:
    """``resolved_for_sub`` is required, so omitting it cannot silently skip the check."""

    def test_caller_cannot_omit_the_binding(self):
        import inspect

        parameter = inspect.signature(sync_memberships_on_login).parameters["resolved_for_sub"]
        assert parameter.default is inspect.Parameter.empty, (
            "resolved_for_sub has acquired a default. With one, any caller that omits it skips the "
            "identity-binding check silently — the same shape of permissive-by-omission defect #5666 fixes."
        )
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


class TestTrustedIdentityInvariantHolds:
    """The module-level invariant, restated as a test for visibility in CI output."""

    def test_trusted_attribute_is_not_self_writable(self):
        assert TRUSTED_LOGIN_ATTRIBUTE not in SELF_WRITABLE_ATTRIBUTES

    def test_dataclass_defaults_are_fail_closed(self):
        assert TrustedGitHubIdentity().linked is False


async def test_provider_response_for_different_numeric_account_cannot_create_membership(db_session, alpha_user, two_orgs):
    with _provider_membership(provider_id=99999, role="admin"):
        await sync_memberships_on_login(db_session, alpha_user, "octocat", github_id="12345", resolved_for_sub="sub-alpha")
    assert await _memberships(db_session) == []


async def test_confirmed_provider_admin_role_is_preserved(db_session, alpha_user, two_orgs):
    with _provider_membership(role="admin"), patch("src.admin.onboarding.handler.project_member_org_ids", new=AsyncMock()):
        await sync_memberships_on_login(db_session, alpha_user, "octocat", github_id="12345", resolved_for_sub="sub-alpha")
    rows = await _memberships(db_session)
    assert [(row.tenant_id, row.role) for row in rows] == [("org-alpha", "org_admin")]


async def test_login_sync_cannot_bypass_target_approval_policy(db_session, alpha_user, two_orgs):
    org = await db_session.get(Organization, "org-alpha")
    org.member_approval_policy = "require_admin_approval"
    await db_session.flush()
    with _provider_membership(role="admin"), patch("src.admin.onboarding.handler.project_member_org_ids", new=AsyncMock()):
        await sync_memberships_on_login(db_session, alpha_user, "octocat", github_id="12345", resolved_for_sub="sub-alpha")
    assert await _memberships(db_session) == []
