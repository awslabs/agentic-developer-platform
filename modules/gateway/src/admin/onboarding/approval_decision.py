"""One derivation of "what role does approving this request grant?" — #5666 (A11).

Why this module exists
----------------------
Approving an access request is the moment a person acquires authority inside a
tenant: it writes ``users.role``, the ``tenant_memberships`` row that
``access_control`` resolves org permissions from, and the Cognito claims the SPA
and the pre-token-generation Lambda read. Getting the role wrong here is not a
cosmetic defect — it is an unearned grant of power.

There are two approver branches (platform admin and org admin) and two request
classes, and the role was derived on only ONE of the four combinations. #4018 fixed
the org-admin branch by deriving the role from GitHub org membership and routing it
through ``attach_approved_member``, whose ``granted_role`` is a parameter precisely
so the grant cannot be hardcoded. The platform-admin branch kept calling
``approve_request``, whose existing-org branch hardcoded ``role="org_admin"`` — so a
platform admin approving somebody's request to JOIN an existing org silently made
them a co-administrator of it, and synced ``custom:role=org_admin`` onto their
Cognito user as well.

That is the same defect #4018 fixed, surviving on the parallel path. The fix is not
to patch the second branch too — it is to make the derivation something neither
branch can skip, which is what this module is.

The two classes
---------------
``JOIN_EXISTING`` — the request names an org that ALREADY exists. The requester is
joining somebody else's tenant, so the default is ``member``. It rises to
``org_admin`` only when GitHub itself answers that the requester is an admin of that
org, which is a fact about the org's own membership, resolved server-side, never
taken from the client or from an editable profile field. This is the derivation
#4018 established for the org-admin branch; it now governs both.

``CREATE_NEW`` — the request names an org that does not exist yet, and approving it
CREATES it with the requester as its owner. ``org_admin`` is the correct and
intended grant here; the class is platform-admin-only (``_authorize_decision``
refuses it for anyone else) because creating a tenant is a platform act.

Note the classes are distinguished by looking the org up in the database, never by a
field on the request — ``TenantAccessRequest`` has no class column, and a
client-supplied discriminator would let a requester choose the branch that grants
more.

The guards
----------
Deriving the role is necessary but not sufficient. Two ceilings also apply, and they
answer different questions:

* ``require_assignable_role`` — may this caller grant THIS role? Blocks a
  platform-level string from an org-scoped caller. Note it does NOT block an
  org_admin granting ``org_admin``: ranks are equal and the comparison is strict
  ``>``. The derivation above is what prevents that, which is why both are needed.
* ``require_modifiable_target`` — may this caller touch THIS person at all? Without
  it, an approval targeting someone who already holds a higher role could rewrite
  their role downward. Ranks are resolved from the authoritative membership row,
  not from ``users.role`` (a display mirror nothing in authz reads).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.config import PLATFORM_LEVEL_ROLES
from src.shared.models.onboarding import TenantAccessRequest, TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)

# The least-privilege default for joining an organization somebody else owns.
MEMBER_ROLE: Final[str] = "member"

# The grant that belongs to the person an organization is created for.
OWNER_ROLE: Final[str] = "org_admin"


class RequestClass(Enum):
    """Which kind of approval this is. Derived from the database, never the client."""

    JOIN_EXISTING = "join_existing"
    CREATE_NEW = "create_new"


@dataclass(frozen=True)
class ApprovalDecision:
    """The authorized outcome of an approval, derived once and used everywhere.

    ``granted_role`` is the single value that must reach ``users.role``, the
    membership row AND the Cognito claims. Returning it as one object is what stops
    the three from drifting — the previous shape recomputed or hardcoded the role at
    each write, which is how the membership row and the claims could disagree.
    """

    request_class: RequestClass
    granted_role: str

    @property
    def creates_organization(self) -> bool:
        return self.request_class is RequestClass.CREATE_NEW


async def target_account_for_request(db: AsyncSession, request: TenantAccessRequest) -> User | None:
    """Use the canonical login's authorized target workspace account."""
    from src.shared.identity.workspaces import workspace_user

    username = f"github_{request.provider_user_id}" if request.provider == "github" else ""
    return await workspace_user(db, request.cognito_sub, request.proposed_tenant_id, username=username)


async def _target_current_role(db: AsyncSession, request: TenantAccessRequest) -> tuple[str | None, bool]:
    """Resolve the requester's CURRENT authority, for ``require_modifiable_target``.

    Returns ``(membership_role_in_target_org, holds_platform_authority)``.

    The membership role is read from ``tenant_memberships`` scoped to the org being
    approved, because that row is what ``access_control`` actually resolves org
    permissions from. ``users.role`` is deliberately not used for it — it is a
    display mirror.

    Platform authority is not representable in a membership row (#3981), so it is
    inferred from ``users.role`` holding a platform-level string. Using the mirror is
    acceptable here and only here, because the consequence is fail-closed: a false
    positive refuses a non-platform caller's approval rather than permitting one.
    """
    # Platform protection is account-wide; tenant role resolution is not.
    from src.shared.identity.workspaces import linked_user_ids

    canonical = await db.scalar(select(User).where(User.cognito_sub == request.cognito_sub))
    username = f"github_{request.provider_user_id}" if request.provider == "github" else ""
    account_ids = await linked_user_ids(db, canonical, username=username) if canonical is not None else set()
    platform_user = await db.scalar(
        select(User.id)
        .where(
            User.id.in_(account_ids),
            func.lower(func.trim(User.role)).in_(PLATFORM_LEVEL_ROLES),
        )
        .limit(1)
    )
    user = await target_account_for_request(db, request)
    if user is None:
        return None, platform_user is not None

    membership_role = await db.scalar(
        select(TenantMembership.role).where(
            TenantMembership.user_id == user.id,
            TenantMembership.tenant_id == request.proposed_tenant_id,
        )
    )
    return membership_role, platform_user is not None


async def derive_approval_decision(
    db: AsyncSession,
    access,
    caller: TokenContext,
    request: TenantAccessRequest,
    *,
    role_for_existing_org,
) -> ApprovalDecision:
    """Classify ``request``, derive the granted role, and enforce both ceilings.

    Every approval branch — platform admin and org admin alike — must go through
    this function. It is deliberately the only place that decides a granted role, so
    a new approver path cannot reintroduce a hardcoded grant.

    Args:
        access: ``AccessControl`` for the request. Passed in rather than imported to
            keep the per-request instance (its role cache must not be shared across
            callers).
        role_for_existing_org: Async callable ``(login, org_name, install_id, provider_id) -> str``
            resolving the requester's GitHub org role. Injected so this module does
            not import the handler's GitHub helpers (which would be circular) and so
            a test can exercise the derivation without a GitHub round-trip.

    Raises:
        InvalidScopeError / AccessDeniedError / InvalidRoleError: from the ceiling
            guards, surfaced as 403s by the global handlers.
    """
    org = await db.get(Organization, request.proposed_tenant_id)

    if org is None:
        # CREATE_NEW: approving this creates the org for the requester, who is
        # therefore its owner. _authorize_decision has already refused this class
        # for every non-platform caller.
        decision = ApprovalDecision(request_class=RequestClass.CREATE_NEW, granted_role=OWNER_ROLE)
    else:
        # JOIN_EXISTING: least privilege unless GitHub says this person already
        # administers the org they are joining.
        install_id = int(org.github_installation_ids[0]) if org.github_installation_ids else 0
        derived = await role_for_existing_org(request.target_login, org.name or "", install_id, request.provider_user_id)
        granted = (derived or "").strip().lower() or MEMBER_ROLE
        decision = ApprovalDecision(request_class=RequestClass.JOIN_EXISTING, granted_role=granted)

    # Which role may be granted.
    await access.require_assignable_role(caller, decision.granted_role, target_org_id=request.proposed_tenant_id)

    # Whether this target may be touched at all.
    target_role, target_is_platform_admin = await _target_current_role(db, request)
    await access.require_modifiable_target(
        caller,
        target_current_role=target_role,
        target_is_platform_admin=target_is_platform_admin,
    )

    logger.info(
        "approval_role_derived request=%s tenant=%s class=%s granted_role=%s actor=%s",
        request.id,
        request.proposed_tenant_id,
        decision.request_class.value,
        decision.granted_role,
        caller.user_id,
    )
    return decision
