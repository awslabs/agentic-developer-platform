"""Regression tests for src.auth.dependencies._cognito_claims_to_context.

These tests guard the privilege-escalation fix where an organization-scoped
"org_admin" role was admitted into the is_admin (platform-admin) predicate.
AccessControl.get_user_role maps is_admin=True to PLATFORM_ADMIN with no org
scope, so treating org_admin as is_admin let any tenant administrator act
across every organization on the platform.

The existing _cognito_claims_to_context tests in tests/auth/test_cognito_jwt.py
exercise the copy in src.auth.middleware; the copy used by the admin API lives
in src.auth.dependencies and was previously untested — hence this file.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from starlette.datastructures import Headers

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.auth.cognito_jwt import CognitoTokenClaims
from src.auth.dependencies import _cognito_claims_to_context, get_current_user
from src.shared.schemas.auth import TokenContext


def _claims(role: str | None = None, groups: list[str] | None = None, org_id: str = "org-attacker") -> CognitoTokenClaims:
    """Build access-token claims shaped exactly as Cognito issues them."""
    return CognitoTokenClaims(
        sub="user-123",
        iss="https://test",
        client_id="web-client",
        token_use="access",
        exp=1234571490,
        iat=1234567890,
        username="user@example.com",
        org_id=org_id,
        role=role,
        cognito_groups=groups or [],
    )


class TestCognitoClaimsToContextIsAdmin:
    """The is_admin predicate must mean PLATFORM admin only."""

    def test_org_admin_is_not_platform_admin(self):
        # The regression: org_admin is an org-scoped role, never a platform admin.
        context = _cognito_claims_to_context(_claims(role="org_admin"))
        assert context.is_admin is False
        assert context.org_id == "org-attacker"

    def test_platform_admin_is_admin(self):
        assert _cognito_claims_to_context(_claims(role="platform_admin")).is_admin is True

    def test_admin_role_is_admin(self):
        assert _cognito_claims_to_context(_claims(role="admin")).is_admin is True

    def test_admins_group_is_admin(self):
        assert _cognito_claims_to_context(_claims(groups=["users", "admins"])).is_admin is True

    def test_platform_admins_group_is_admin(self):
        assert _cognito_claims_to_context(_claims(groups=["platform-admins"])).is_admin is True

    def test_regular_user_is_not_admin(self):
        context = _cognito_claims_to_context(_claims(role=None))
        assert context.is_admin is False
        assert context.org_id == "org-attacker"


class TestOrgAdminHasNoCrossOrgAccess:
    """End-to-end: an org_admin token, run through the real AccessControl,
    stays scoped to its own organization and cannot reach another tenant."""

    async def test_org_admin_token_does_not_resolve_to_platform_admin(self):
        """The #3981 contract: an org_admin *claim* never confers platform authority.

        Issue #3987 PR 2 tightened the outcome further — with no
        ``tenant_memberships`` row (this ``AccessControl()`` has no session at
        all) the caller now resolves to MEMBER rather than the old no-row
        ORG_ADMIN fallback. Either way the invariant this test exists for holds:
        not PLATFORM_ADMIN, and the scope stays pinned to the caller's own org
        rather than widening to None (== all orgs).
        """
        context = _cognito_claims_to_context(_claims(role="org_admin", org_id="org-attacker"))
        access = AccessControl()

        role, org_id, dept_id = await access.get_user_role(context)

        assert role != AdminRole.PLATFORM_ADMIN
        assert role == AdminRole.MEMBER
        assert org_id == "org-attacker"
        assert dept_id is None

    async def test_org_admin_accessible_orgs_is_own_org_only(self):
        context = _cognito_claims_to_context(_claims(role="org_admin", org_id="org-attacker"))
        access = AccessControl()

        # Must be the caller's own org, NOT None (None == all orgs == platform admin).
        assert await access.get_accessible_organizations(context) == ["org-attacker"]

    async def test_org_admin_denied_cross_org_resource(self):
        """A resolved ORG_ADMIN must still be denied another tenant's resources.

        Issue #3987 PR 2: the resolved role is seeded into the cache so the caller
        genuinely IS an org admin. Without this the caller would resolve to MEMBER
        (no membership row) and the denial would prove nothing about the cross-org
        boundary — a principal with no authority anywhere is denied trivially.
        """
        context = _cognito_claims_to_context(_claims(role="org_admin", org_id="org-attacker"))
        access = AccessControl()
        access._cache_put((context.user_id, context.org_id), (AdminRole.ORG_ADMIN, "org-attacker", None))

        with pytest.raises(AccessDeniedError):
            await access.validate_resource_access(context, resource_org_id="org-victim")

    async def test_org_admin_denied_cross_org_read_permission(self):
        """Cross-org ORG_READ must fail on SCOPE, not on a missing permission.

        Issue #3987 PR 2: seeded as above. A MEMBER would raise AccessDeniedError
        from the permission-set check before the scope check ran, silently
        replacing the InvalidScopeError this test exists to pin.
        """
        context = _cognito_claims_to_context(_claims(role="org_admin", org_id="org-attacker"))
        access = AccessControl()
        access._cache_put((context.user_id, context.org_id), (AdminRole.ORG_ADMIN, "org-attacker", None))

        with pytest.raises(InvalidScopeError):
            await access.check_permission(context, Permission.ORG_READ, target_org_id="org-victim")

    async def test_platform_admin_still_sees_all_orgs(self):
        # Guard the fix from over-correcting: real platform admins keep global scope.
        context = _cognito_claims_to_context(_claims(role="platform_admin", org_id="platform"))
        access = AccessControl()

        role, org_id, _ = await access.get_user_role(context)
        assert role == AdminRole.PLATFORM_ADMIN
        assert org_id is None
        assert await access.get_accessible_organizations(context) is None


class TestGetCurrentUserIamIdentity:
    """Issue #3985 (f-bab519a0): X-Caller-Identity must not mint identity.

    get_current_user backs ~18 routers (admin, usage, budget, knowledge,
    activity, ratelimit, ...). It previously returned a fabricated authenticated
    TokenContext — account_type="service", org_id="", user_id taken from the
    ARN's trailing segment — for any *parseable* assumed-role ARN that was ABSENT
    from the agent registry. So any caller who could reach the pod could assert
    an arbitrary user_id on every one of those routers.

    src.auth.middleware.extract_iam_identity_from_headers has always raised
    UnregisteredServiceAccountError for the same case; this brings the two into
    agreement. The fabricated-context branch had zero test coverage (every other
    test overrides the dependency), which is why these are new rather than
    inverted.
    """

    @staticmethod
    def _request(caller_identity: str | None):
        headers = {}
        if caller_identity is not None:
            headers["x-caller-identity"] = caller_identity

        class _State:
            pass

        request = MagicMock(spec=Request)
        request.headers = Headers(headers)
        request.state = _State()
        return request

    @staticmethod
    def _trust_enabled():
        settings = MagicMock()
        settings.trust_apigw_headers = True
        return settings

    async def test_unregistered_role_arn_raises_403(self):
        """The core assertion: an ARN absent from the registry is rejected."""
        registry = MagicMock()
        registry.get_agent_by_role_arn.return_value = None

        with (
            patch("src.auth.dependencies.get_settings", return_value=self._trust_enabled()),
            patch("src.auth.agent_registry.get_agent_registry_service", return_value=registry),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(
                    self._request("arn:aws:sts::123456789012:assumed-role/unregistered-role/session"),
                    authorization=None,
                )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == "agent_not_registered"

    async def test_unregistered_role_arn_does_not_fabricate_context(self):
        """Explicitly guards the removed fallback's shape.

        If someone reinstates it, this fails with an org_id="" service context
        rather than the 403, naming the exact regression.
        """
        registry = MagicMock()
        registry.get_agent_by_role_arn.return_value = None

        with (
            patch("src.auth.dependencies.get_settings", return_value=self._trust_enabled()),
            patch("src.auth.agent_registry.get_agent_registry_service", return_value=registry),
        ):
            with pytest.raises(HTTPException):
                await get_current_user(
                    self._request("arn:aws:sts::123456789012:assumed-role/unregistered-role/session"),
                    authorization=None,
                )

    async def test_malformed_arn_raises_403_and_does_not_reach_jwt(self):
        """X-Caller-Identity presence is terminal.

        An unparseable ARN must reject, not fall through to the JWT branch — a
        forged header should never be a way to reach a different auth path.
        """
        with patch("src.auth.dependencies.get_settings", return_value=self._trust_enabled()):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(self._request("not-an-arn"), authorization=None)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == "invalid_caller_identity"

    async def test_registered_role_arn_still_authenticates(self):
        """Regression: the legitimate agent path is unchanged."""
        entry = {
            "agent_name": "registered-agent",
            "org_id": "org-real",
            "team_id": "team-real",
        }
        registry = MagicMock()
        registry.get_agent_by_role_arn.return_value = entry

        expected = TokenContext(
            user_id="registered-agent",
            org_id="org-real",
            team_id="team-real",
            department_id="",
            account_type="service",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_source="iam",
        )

        with (
            patch("src.auth.dependencies.get_settings", return_value=self._trust_enabled()),
            patch("src.auth.agent_registry.get_agent_registry_service", return_value=registry),
            patch("src.auth.agent_registry.agent_entry_to_token_context", return_value=expected),
        ):
            context = await get_current_user(
                self._request("arn:aws:sts::123456789012:assumed-role/registered-role/session"),
                authorization=None,
            )

        assert context.org_id == "org-real"
        assert context.user_id == "registered-agent"

    async def test_header_ignored_when_trust_disabled(self):
        """With BG_TRUST_APIGW_HEADERS=false the header is inert (break-glass).

        Flipping the flag in the configmap is the documented no-code-change
        rollback for this change; it must land on the JWT path, not a 403.
        """
        settings = MagicMock()
        settings.trust_apigw_headers = False

        with patch("src.auth.dependencies.get_settings", return_value=settings):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(
                    self._request("arn:aws:sts::123456789012:assumed-role/whatever/session"),
                    authorization=None,
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "missing_token"
