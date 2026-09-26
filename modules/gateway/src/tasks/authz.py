"""Owner-only authorization for the Task API read surface.

Four checks run on every operation, in this order (design section 4: "Every
operation checks the active alias, canonical principal, task ownership and
current task policy"):

1. the credential is a valid Cognito access token carrying the required
   ``adp-tasks/*`` resource-server scope;
2. the caller resolves to a canonical service principal through an *active*
   alias — no fallback to ``sub``, a body-supplied name or a legacy alias;
3. that canonical principal is the task's recorded owner;
4. the task is readable in its current state.

The original service-owner path remains unchanged. Explicitly enabled human
Task enrollment resolves a separate human:<User.id> owner namespace from live
membership and standing Task policy, never from a service alias.

The three things that are *not* here matter as much
as the four that are: a same-tenant service gets no implicit access, there is no
public delegation, and there is no human-admin override. Each would widen the
accepted access model, which this story may not do.

**Why this authenticates rather than reusing ``get_current_user``.** The scope
check needs the token's ``scope`` claim, and the shared path drops it:
``CognitoTokenClaims.scope`` is parsed, but ``_cognito_claims_to_context`` does
not copy it onto ``TokenContext``, and ``TokenContext.credential_scopes`` is the
Agent Registry's unrelated concept (empty for a Cognito M2M caller). The two
alternatives were adding a field to ``TokenContext`` — shared by every auth path
in the gateway, for one additive surface — or validating twice. Instead this
module calls the same ``CognitoJWTValidator`` and the same claim-mapping helpers
the shared dependency uses, once, and reads the scope from the claims it already
has. Nothing existing changes, and there is no second validation rule to drift.

Design reference: implementation-design.md section 4; ``identity-and-lifecycle.json``
``ownership``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.persona_models import service as principal_service
from src.auth.dependencies import _cognito_claims_to_context, _get_cognito_validator, _stamp_trusted_alias_source
from src.shared.schemas.auth import TokenContext
from src.tasks import errors
from src.tasks.read_store import TaskRecord, TaskStore, TaskStoreError

logger = logging.getLogger(__name__)

#: Task API resource-server scopes from design section 4.
SCOPE_READ = "adp-tasks/read"
SCOPE_ARTIFACTS = "adp-tasks/artifacts"

#: The one alias source a Cognito ``client_credentials`` caller may resolve
#: through. Pinned rather than derived from ``auth_source`` because that field is
#: too coarse to tell an M2M client from a legacy service-account exchange —
#: both present as ``account_type="service", auth_source="jwt"`` — and resolving
#: a task owner through the wrong namespace is an ownership error, not a miss.
TASK_ALIAS_SOURCE = "cognito_m2m"


@dataclass(frozen=True)
class Caller:
    """An authenticated, canonically resolved Task API caller.

    ``principal_id`` is the canonical service principal, the only identity task
    ownership is recorded against. ``tenant_id`` is the authenticated ``org_id``
    — never ``attributed_org_id``, which is caller-influenced attribution and
    documented as never an authorization input.
    """

    principal_id: str
    tenant_id: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise errors.disallowed_scope(f"This credential does not carry {scope}.")


def authenticate(request: Request) -> tuple[TokenContext, frozenset[str]]:
    """Validate the bearer token and return its context plus granted scopes.

    Refusals are deliberately coarse. A missing, malformed, expired or
    unallowlisted token all produce ``401 invalid_credential`` with one message,
    because distinguishing them tells an unauthenticated caller which of its
    guesses was closer.

    **Known imprecision, stated rather than papered over.** Design section 4 wants
    an unavailable authorization dependency to deny with ``503``, not to imply the
    caller's credential is bad. ``CognitoJWTValidator.validate_token`` re-raises a
    JWKS fetch failure as ``jwt.InvalidTokenError``, so a signing-key outage is
    type-indistinguishable from a forged token and surfaces here as ``401``. The
    fix belongs in the shared validator, where a distinct exception type would
    change the refusal behaviour of every authenticated route in the gateway —
    out of scope for an additive story. The effect on a caller is a retryable
    condition reported as a permanent one; it is logged server-side.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise errors.TaskApiError(401, "invalid_credential", "A bearer access token is required.")

    validator = _get_cognito_validator()
    if validator is None:
        # Not a caller error: the deployment cannot verify credentials at all. A
        # 401 here would tell a correctly-credentialed client its token is bad.
        raise errors.prerequisite_unavailable("Credential validation is not configured.")

    try:
        from src.auth.tenant_context import split_token

        token, tenant_lease = split_token(header[7:])
        claims = validator.validate_token(token)
    except (jwt.InvalidTokenError, HTTPException):
        raise errors.TaskApiError(401, "invalid_credential", "The access token is invalid or expired.") from None
    except Exception:
        logger.warning("Task API token validation failed unexpectedly", exc_info=True)
        raise errors.prerequisite_unavailable("Credential validation is unavailable.") from None

    if claims.token_use != "access":
        # An ID token is a proof of authentication for a browser session, not a
        # client_credentials authorization to call an API, and it carries no
        # resource-server scope. Accepting one would let a signed-in human's
        # front-end token act as a registered service.
        raise errors.TaskApiError(401, "invalid_credential", "An access token is required.")

    try:
        context = _stamp_trusted_alias_source(_cognito_claims_to_context(claims))
    except Exception:
        raise errors.TaskApiError(401, "invalid_credential", "The access token is not coherent.") from None

    context._task_tenant_lease = tenant_lease
    scopes = frozenset(scope for scope in (claims.scope or "").split() if scope.startswith("adp-tasks/"))
    return context, scopes


async def resolve_caller(context: TokenContext, scopes: frozenset[str], db: AsyncSession) -> Caller:
    """Resolve the canonical service principal behind a validated token.

    Explicitly enrolled humans use their own ownership namespace.
    The service path refuses a service token with no
    tenant, one whose alias source is not the M2M one, and one whose alias or
    principal is not active. Which applies is information about the platform's
    registration state that an unregistered caller has no claim to.
    """
    if getattr(context, "_task_tenant_lease", None) is not None:
        from src.auth.tenant_context import apply_context

        try:
            context = await apply_context(context, context._task_tenant_lease, db)
        except HTTPException:
            raise errors.disallowed_scope("The signed tenant selection is no longer authorized.") from None
    if context.account_type == "human":
        from src.tasks.human_authority import resolve_human

        principal, tenant, granted = await resolve_human(context, db)
        return Caller(principal_id=principal, tenant_id=tenant, scopes=granted)
    if context.account_type != "service":
        # A human operator reading another principal's task would be exactly the
        # human-admin override the design excludes from v1.
        raise errors.disallowed_scope("The Task API v1 surface is available to registered service principals only.")
    if not context.org_id:
        raise errors.disallowed_scope("The credential resolves to no tenant.")
    if context.canonical_alias_source != TASK_ALIAS_SOURCE:
        raise errors.disallowed_scope("This credential's authentication path is not registered for Task API access.")

    try:
        canonical_id, matched_source = await principal_service.resolve_by_exact_source(
            db,
            alias_source=TASK_ALIAS_SOURCE,
            alias_id=context.user_id,
            org_id=context.org_id,
        )
    except Exception:
        # The identity directory is an authorization dependency, so its failure
        # denies. Logged without caller identifiers; its message is not returned.
        logger.warning("Task API caller resolution failed against the identity directory", exc_info=True)
        raise errors.prerequisite_unavailable("The identity directory is unavailable.") from None

    if not canonical_id or matched_source != TASK_ALIAS_SOURCE:
        raise errors.disallowed_scope("This credential is not a registered, active Task API principal.")

    return Caller(principal_id=canonical_id, tenant_id=context.org_id, scopes=scopes)


def authorize_task(caller: Caller, store: TaskStore, task_id: str) -> TaskRecord:
    """Load a task the caller owns, or refuse indistinguishably.

    Tenant *and* principal must both match. Tenant alone would grant every
    service in an organization access to every other's tasks — the implicit
    same-tenant access the design denies. Principal alone would rely on canonical
    IDs never colliding across tenants, which is a property of the current
    generator rather than a guarantee of the identity model.
    """
    try:
        record = store.load_task(task_id=task_id)
    except TaskStoreError:
        logger.warning("Task API authorization read failed", exc_info=True)
        raise errors.prerequisite_unavailable("Task storage is unavailable.") from None

    if record is None or record.tenant_id != caller.tenant_id or record.owner_principal_id != caller.principal_id:
        raise errors.not_found()
    store.require_policy(tenant=caller.tenant_id, principal=caller.principal_id, persona=record.persona)
    return record


def authorize_artifact(caller: Caller, store: TaskStore, *, task_id: str, artifact_id: str):
    """Authorize a task-scoped artifact download.

    Both the task and the *exact* artifact binding are checked, so an artifact ID
    legitimately readable through one task cannot be read through another the
    caller also owns. Design section 5 requires both scopes on this path: reading
    through a task needs read and ownership, and the artifact surface needs its
    own scope.
    """
    caller.require(SCOPE_READ)
    caller.require(SCOPE_ARTIFACTS)
    authorize_task(caller, store, task_id)

    try:
        artifact = store.load_artifact(artifact_id=artifact_id)
    except TaskStoreError:
        raise errors.prerequisite_unavailable("Task storage is unavailable.") from None

    if (
        artifact is None
        or artifact.task_id != task_id
        or artifact.tenant_id != caller.tenant_id
        or artifact.owner_principal_id != caller.principal_id
    ):
        raise errors.not_found()
    return artifact
