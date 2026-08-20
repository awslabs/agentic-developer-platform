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

import pytest

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.auth.cognito_jwt import CognitoTokenClaims
from src.auth.dependencies import _cognito_claims_to_context


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

    async def test_org_admin_resolves_to_scoped_org_admin(self):
        context = _cognito_claims_to_context(_claims(role="org_admin", org_id="org-attacker"))
        access = AccessControl()

        role, org_id, dept_id = await access.get_user_role(context)

        assert role == AdminRole.ORG_ADMIN
        assert org_id == "org-attacker"
        assert dept_id is None

    async def test_org_admin_accessible_orgs_is_own_org_only(self):
        context = _cognito_claims_to_context(_claims(role="org_admin", org_id="org-attacker"))
        access = AccessControl()

        # Must be the caller's own org, NOT None (None == all orgs == platform admin).
        assert await access.get_accessible_organizations(context) == ["org-attacker"]

    async def test_org_admin_denied_cross_org_resource(self):
        context = _cognito_claims_to_context(_claims(role="org_admin", org_id="org-attacker"))
        access = AccessControl()

        with pytest.raises(AccessDeniedError):
            await access.validate_resource_access(context, resource_org_id="org-victim")

    async def test_org_admin_denied_cross_org_read_permission(self):
        context = _cognito_claims_to_context(_claims(role="org_admin", org_id="org-attacker"))
        access = AccessControl()

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
