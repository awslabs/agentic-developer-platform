"""Service layer for vault credential and identity CRUD.

Issue #135: Vault Phase 2a — Credential + Identity CRUD

Ownership scoping rules:
  user       — user_id == caller.user_id (AND org_id == caller.org_id)
  team       — team_id == caller.team_id, user_id IS NULL
  org        — all three owner cols NULL, org_id == caller.org_id
  domain_app — domain_app_id IS NOT NULL, org_id == caller.org_id (admin-only)

Enumeration protection: non-owned resources return 404, never 403.
Cross-tenant access: hard 404 via org_id filter on every query.

Issue #3989: shared scopes (org, team, domain_app) are admin-gated for WRITES —
create, update and delete. Visibility is not authority to mutate. For org/team the
denial is 403 (the row is already visible to the caller via list_credentials, so
the status leaks nothing); domain_app keeps 404 because its visibility is
admin-only and a 403 there would newly confirm existence.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.vault import UserCredential, UserIdentity
from src.shared.schemas.auth import TokenContext
from src.shared.services.secrets_manager import SecretsManagerHelper

from .org_id_resolver import resolve_effective_org_id
from .vault_schemas import CredentialCreate, CredentialUpdate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions (mapped to HTTP codes by routes)
# ---------------------------------------------------------------------------


class CredentialNotFoundError(Exception):
    """Credential not found or caller does not own it (→ 404)."""


class IdentityNotFoundError(Exception):
    """Identity not found or caller does not own it (→ 404)."""


class InsufficientPrivilegesError(Exception):
    """Caller lacks admin role required for this operation (→ 403)."""


class InvalidScopeConfigError(Exception):
    """Request is missing required scope-specific fields (→ 422)."""


class DuplicateCredentialError(Exception):
    """A credential with this (scope, service, label) already exists (→ 409)."""


# ---------------------------------------------------------------------------
# Helper: caller's visible credentials (used by both list and ownership check)
# ---------------------------------------------------------------------------


def _visible_credential_filter(caller: TokenContext, scope_filter: str | None = None):
    """Build SQLAlchemy OR condition covering credentials visible to *caller*.

    Returns a list of conditions to be combined with or_().
    An empty list means no credentials are visible (caller has no team, etc.)
    """
    org_cond = UserCredential.org_id == caller.org_id
    conditions = []

    show_user = scope_filter in (None, "user")
    show_team = scope_filter in (None, "team")
    show_org = scope_filter in (None, "org")
    show_domain_app = scope_filter in (None, "domain_app")

    if show_user and caller.user_id:
        conditions.append(
            and_(
                org_cond,
                UserCredential.user_id == caller.user_id,
            )
        )

    if show_team and caller.team_id:
        conditions.append(
            and_(
                org_cond,
                UserCredential.team_id == caller.team_id,
                UserCredential.user_id.is_(None),
            )
        )

    if show_org:
        conditions.append(
            and_(
                org_cond,
                UserCredential.user_id.is_(None),
                UserCredential.team_id.is_(None),
                UserCredential.domain_app_id.is_(None),
            )
        )

    if show_domain_app and caller.is_admin:
        conditions.append(
            and_(
                org_cond,
                UserCredential.domain_app_id.isnot(None),
            )
        )

    return conditions


# ---------------------------------------------------------------------------
# Credential service functions
# ---------------------------------------------------------------------------


async def list_credentials(
    db: AsyncSession,
    caller: TokenContext,
    scope_filter: str | None = None,
) -> list[UserCredential]:
    """Return all credentials visible to *caller*, optionally filtered by scope."""
    conditions = _visible_credential_filter(caller, scope_filter)
    if not conditions:
        return []

    stmt = select(UserCredential).where(or_(*conditions))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _is_org_admin(db: AsyncSession, caller: TokenContext) -> bool:
    """Return True if *caller* holds org-admin authority in their own org.

    Issue #3989: shared-scope credential MUTATIONS need an admin gate, and the
    gate must be the org-admin *permission*, not ``caller.is_admin``.
    ``TokenContext.is_admin`` means PLATFORM admin and deliberately excludes
    ``org_admin`` (see the warning in ``auth/dependencies.py``), so gating on it
    would mean only a platform operator could ever rotate or retire a tenant's
    own shared secret — a multi-tenant functional regression.

    Resolution goes through ``AccessControl``, so it inherits #3987's DB-backed
    role lookup. Since #3987 PR 2 flipped ``rbac_least_privilege_default`` to
    True, a principal with no admin-level membership row resolves to MEMBER and
    this gate is fully effective (it was latent before the flip).
    """
    from src.admin.access_control import AccessControl
    from src.admin.config import Permission

    try:
        await AccessControl(db).check_permission(caller, Permission.ORG_UPDATE, target_org_id=caller.org_id)
        return True
    except Exception:
        return False


async def _get_owned_credential(
    cred_id: str,
    db: AsyncSession,
    caller: TokenContext,
) -> UserCredential:
    """Fetch a credential by ID for the given caller, for MUTATION.

    Both call sites (``update_credential``, ``delete_credential``) are mutations,
    so this helper applies the shared-scope admin gate. It must NOT be reused for
    read paths without revisiting that.

    Raises CredentialNotFound (→ 404) if:
    - The ID does not exist in this org (cross-tenant protection)
    - The credential exists but the caller does not own / see it

    Raises InsufficientPrivilegesError (→ 403) if the credential is org- or
    team-scoped and the caller is not an org admin (Issue #3989).
    """
    # Tenant-scoped fetch first
    stmt = select(UserCredential).where(
        UserCredential.id == cred_id,
        UserCredential.org_id == caller.org_id,
    )
    result = await db.execute(stmt)
    cred = result.scalar_one_or_none()
    if cred is None:
        raise CredentialNotFoundError(cred_id)

    # Ownership check using the same visibility logic as list
    conds = _visible_credential_filter(caller)
    if not conds:
        raise CredentialNotFoundError(cred_id)

    # Build a quick per-row ownership check
    scope = cred.owner_scope
    if scope == "user" and cred.user_id == caller.user_id:
        return cred

    # Issue #3989 (f-7800b0cc): org- and team-scoped credentials are SHARED, so
    # visibility is not authority to mutate. Creation of these scopes was already
    # admin-gated (create_credential below); update/delete were not, which let any
    # org member rewrite or irrecoverably destroy a shared credential.
    #
    # 403 (not the usual 404) is correct for these two scopes: a non-admin org
    # member can already see the row via list_credentials, so the status code
    # leaks nothing and tells the caller the truth. domain_app stays 404 below —
    # its visibility is admin-only, so 403 there would newly confirm existence.
    if scope in ("team", "org"):
        in_scope = scope == "org" or (cred.team_id == caller.team_id and bool(caller.team_id))
        if in_scope:
            if await _is_org_admin(db, caller):
                return cred
            raise InsufficientPrivilegesError(f"Organization administrator role required to modify {scope!r}-scoped credentials")

    if scope == "domain_app" and caller.is_admin:
        return cred

    # Not owned — return 404, not 403 (enumeration protection)
    raise CredentialNotFoundError(cred_id)


async def create_credential(
    data: CredentialCreate,
    db: AsyncSession,
    caller: TokenContext,
    sm: SecretsManagerHelper,
) -> UserCredential:
    """Register a new credential.

    Writes the raw value to Secrets Manager; stores only the ARN in DB.
    """
    # Resolve effective org_id — falls back to users.org_id when token is empty
    # (Issue #600: GitHub-federated users may have empty org_id in token)
    effective_org_id = await resolve_effective_org_id(caller, db)

    scope_hint = data.scope_hint

    # Non-user scopes require admin privileges
    if scope_hint != "user" and not caller.is_admin:
        raise InsufficientPrivilegesError(f"Admin role required to create {scope_hint!r}-scoped credentials")

    # Resolve owner columns and SM namespace arguments
    user_id: str | None = None
    team_id: str | None = None
    domain_app_id: str | None = None

    if scope_hint == "user":
        user_id = caller.user_id
        sm_kwargs: dict = {"user_sub": caller.user_id}
    elif scope_hint == "team":
        if not caller.team_id:
            raise InvalidScopeConfigError("Caller has no team_id; cannot create team-scoped credential")
        team_id = caller.team_id
        sm_kwargs = {"team_id": caller.team_id}
    elif scope_hint == "org":
        sm_kwargs = {"org_id": caller.org_id}
    elif scope_hint == "domain_app":
        # domain_app_id already validated as present by CredentialCreate validator
        domain_app_id = data.domain_app_id
        sm_kwargs = {"domain_app_id": domain_app_id, "org_id": caller.org_id}
    else:
        raise InvalidScopeConfigError(f"Unknown scope_hint: {scope_hint!r}")

    # Write to Secrets Manager (synchronous boto3 call, offloaded to thread pool)
    secret_arn: str = await asyncio.to_thread(
        sm.create_secret,
        data.service,
        data.label,
        data.value,
        **sm_kwargs,
    )

    # Persist metadata row
    cred = UserCredential(
        org_id=effective_org_id,
        user_id=user_id,
        team_id=team_id,
        domain_app_id=domain_app_id,
        service=data.service,
        credential_type=data.credential_type,
        label=data.label,
        secret_arn=secret_arn,
        scopes=data.scopes,
        expires_at=data.expires_at,
        strict=data.strict,
    )
    db.add(cred)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # SM secret was already created — delete it to prevent orphaned secrets (F2)
        #
        # Issue #3989: this call intentionally keeps the force=True default. It is
        # NOT a user-driven deletion — the credential was never successfully
        # created, so the value the user just submitted must be destroyed
        # immediately. Switching it to force=False would park that value in a
        # 7-30 day pending-deletion window for a row that never existed.
        # Only the delete_credential path below changes.
        try:
            await asyncio.to_thread(sm.delete_secret, secret_arn)
        except Exception:
            logger.exception(
                "Failed to delete orphaned SM secret %s after duplicate credential attempt",
                secret_arn,
            )
        raise DuplicateCredentialError(f"A credential with service={data.service!r} label={data.label!r} already exists")
    await db.refresh(cred)

    logger.info(
        "Created credential id=%s service=%s scope=%s org=%s",
        cred.id,
        data.service,
        scope_hint,
        caller.org_id,
    )
    return cred


async def update_credential(
    cred_id: str,
    data: CredentialUpdate,
    db: AsyncSession,
    caller: TokenContext,
) -> UserCredential:
    """Update mutable metadata fields (label, expires_at, strict).

    Value updates are not allowed — callers must delete + re-register.
    """
    cred = await _get_owned_credential(cred_id, db, caller)

    if data.label is not None:
        cred.label = data.label
    if data.expires_at is not None:
        cred.expires_at = data.expires_at
    if data.strict is not None:
        cred.strict = data.strict

    await db.commit()
    await db.refresh(cred)
    return cred


async def delete_credential(
    cred_id: str,
    db: AsyncSession,
    caller: TokenContext,
    sm: SecretsManagerHelper,
) -> None:
    """Delete the DB row AND the Secrets Manager secret synchronously."""
    cred = await _get_owned_credential(cred_id, db, caller)
    secret_arn = cred.secret_arn

    # Delete DB row first so the secret is never accessible via our API
    # even if the SM delete call is slow.
    await db.delete(cred)
    await db.commit()

    # Delete the SM secret (best-effort; log failures but don't re-raise
    # because the DB row is already gone and the caller expects success).
    #
    # Issue #3989: force=False so AWS's 7-30 day recovery window applies to
    # user-driven deletes — the default (force=True) sets
    # ForceDeleteWithoutRecovery, making a delete unrecoverable. Secret names carry
    # a random uuid suffix (SecretsManagerHelper._build_secret_name), so a
    # pending-deletion secret can never collide with a re-register.
    try:
        await asyncio.to_thread(sm.delete_secret, secret_arn, force=False)
    except Exception:
        logger.exception("Failed to delete SM secret %s for credential %s; DB row already removed", secret_arn, cred_id)

    logger.info("Deleted credential id=%s secret_arn=%s org=%s", cred_id, secret_arn, caller.org_id)


# ---------------------------------------------------------------------------
# Identity service functions
# ---------------------------------------------------------------------------


async def list_identities(
    db: AsyncSession,
    caller: TokenContext,
) -> list[UserIdentity]:
    """Return all identities linked to the calling user."""
    stmt = select(UserIdentity).where(
        UserIdentity.user_id == caller.user_id,
        UserIdentity.org_id == caller.org_id,
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def unlink_identity(
    identity_id: str,
    db: AsyncSession,
    caller: TokenContext,
) -> None:
    """Remove a linked identity (user_identities row).

    Returns 404 if the identity does not exist or belongs to another user.
    """
    stmt = select(UserIdentity).where(
        UserIdentity.id == identity_id,
        UserIdentity.user_id == caller.user_id,
        UserIdentity.org_id == caller.org_id,
    )
    result = await db.execute(stmt)
    identity = result.scalar_one_or_none()
    if identity is None:
        raise IdentityNotFoundError(identity_id)

    await db.delete(identity)
    await db.commit()
    logger.info("Unlinked identity id=%s provider=%s user=%s", identity_id, identity.provider, caller.user_id)
