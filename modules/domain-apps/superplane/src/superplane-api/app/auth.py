"""Domain auth enforcement — where U9's policy meets an actual request.

Issue #5055 (U14). The decisions live in ``superplane_auth.policy`` (issue #5044,
U9); this module supplies what that policy requires and cannot obtain for itself:

1. **Cryptographically verified claims.** The policy decides *policy*, not
   authenticity — its docstring is explicit that ``claims`` must already be
   signature-verified. So this module verifies the RS256 signature against the
   user pool's published keys before any claim is read.
2. **A named validation path.** ``admit_token`` refuses to admit claims without
   being told which validator produced them, because "authenticated by some ADP
   path" is not sufficient: the API-authorizer path checks neither ``token_use``
   nor a client allowlist, so accepting its output would let the weaker path
   satisfy the stricter path's policy. This module verifies to the strict path's
   standard and therefore names ``TRUSTED_VALIDATION_PATH``.
3. **Server-retrieved grant and operation objects.** Read from the database at
   decision time, constructed here, and never accepted from the request.

WHAT THE LEGACY PATH IS AND WHY IT SURVIVES
-------------------------------------------
This service mints its own HS256 JWT whose subject *is* an organization id
(``app/middleware/auth.py``), so it carries no user identity at all on the
API-key path. Retiring that is U21's explicitly conditional story, so this
module runs beside it rather than deleting it.

The flag switches which path is authoritative; it does **not** soften the strict
path. When ``domain_auth_enforced`` is on there is NO fallback to the legacy
validator on failure — a permissive alternate validator answering the same
question is a bypass, not a fallback, and it is exactly the "legacy validator
must not bypass policy" requirement. The two never both get a vote on one
request.

FAILING TO START vs FAILING A REQUEST
-------------------------------------
A missing client allowlist or issuer is an operator error, so it raises at
construction and the process does not serve. A bad token is a normal 401. The
policy already draws that line (``TokenPolicyError`` vs ``TokenRejectedError``);
this module preserves it instead of collapsing both into a 500.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import JOSEError, JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.endpoint_inventory import Scope
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from superplane_auth.policy import (
    TRUSTED_VALIDATION_PATH,
    AuthorizationDeniedError,
    DomainPrincipal,
    DomainTokenPolicy,
    OperationAuthorization,
    Permission,
    TokenPolicyError,
    TokenRejectedError,
    WorkspaceAuthorizationModel,
    WorkspaceGrant,
    expand_permissions,
    strip_identity_headers,
)

logger = logging.getLogger(__name__)

# auto_error=False so a missing credential reaches our handler and becomes a
# deliberate 401 with the policy's reason, rather than HTTPBearer's bare 403.
domain_bearer = HTTPBearer(auto_error=False)

# Workspace states that no longer represent a usable workspace. Excluded when
# computing organization authority (see `authorize_organization_operation`),
# because `DELETE /workspaces/{id}` is a soft delete: the row survives with
# `status="Teardown"` and can never acquire a grant, so counting it would make
# org authority permanently unholdable for every member of the org.
#
# The values are the literals `app/routers/workspaces.py` writes and tests for
# (`workspace.status in ("Teardown", "Deleted")`), not the `STATUS_*` constants
# in `app/models/workspace.py` — those cover the provisioning lifecycle and none
# of them names a torn-down workspace. Matching the writer is what makes the
# filter true; matching the constants would have read as correct and excluded
# nothing.
DECOMMISSIONED_WORKSPACE_STATUSES: tuple[str, ...] = ("Teardown", "Deleted")


# ---------------------------------------------------------------------------
# Startup: build the policy once, or refuse to serve
# ---------------------------------------------------------------------------


def build_domain_policy() -> DomainTokenPolicy | None:
    """Construct the token policy from settings.

    Returns ``None`` when enforcement is off — the legacy path is authoritative
    and there is no policy to build. With enforcement ON, a missing allowlist or
    issuer raises :class:`TokenPolicyError` from the policy's own constructor, so
    the process fails to start rather than serving with a policy that admits
    every client in the user pool.
    """
    if not settings.domain_auth_enforced:
        return None
    # Both arguments are passed through unvalidated on purpose: the policy owns
    # the "empty allowlist is a startup failure, not a default" rule, and
    # re-checking it here would create a second place for that rule to drift.
    return DomainTokenPolicy(
        allowed_client_ids=settings.domain_auth_allowed_client_ids,
        expected_issuer=settings.cognito_issuer,
    )


@dataclass(frozen=True)
class VerifiedCaller:
    """A caller admitted by the strict path, with the sanitized request headers.

    ``principal`` comes entirely from verified claims. ``safe_headers`` is the
    mapping the policy returned after stripping every client-supplied identity
    header; consumers must forward only this, never the raw request headers.
    """

    principal: DomainPrincipal
    safe_headers: dict[str, str]
    # Original verified ADP identity when the domain uses an explicit binding.
    source_org_id: str | None = None


# ---------------------------------------------------------------------------
# Token verification (the part the policy cannot do for itself)
# ---------------------------------------------------------------------------


class _JWKSCache:
    """Caches the user pool's public keys.

    Fetched lazily and held for the process lifetime. Cognito's signing keys are
    long-lived; a per-request fetch would put an external dependency on the hot
    path of every authorization decision, and its failure mode would be an
    outage rather than a denial.
    """

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Any]] | None = None

    def load(self, keys: list[dict[str, Any]]) -> None:
        """Install keys directly. Used by tests and by an explicit warm-up."""
        self._keys = {k["kid"]: k for k in keys if "kid" in k}

    def clear(self) -> None:
        self._keys = None

    @property
    def loaded(self) -> bool:
        return self._keys is not None

    def get(self, kid: str) -> dict[str, Any] | None:
        if self._keys is None:
            self._fetch()
        return (self._keys or {}).get(kid)

    def _fetch(self) -> None:
        url = settings.cognito_jwks_url
        if not url:
            raise TokenPolicyError(
                "domain auth is enforced but no JWKS URL is configured; "
                "token signatures cannot be verified"
            )
        # Imported here rather than at module scope: with enforcement off this
        # path never runs, and the module must import cleanly in test and
        # offline environments regardless.
        import json
        from urllib.request import urlopen

        with urlopen(url, timeout=5) as response:  # noqa: S310 - fixed https config value
            document = json.loads(response.read())
        self.load(document.get("keys", []))


jwks_cache = _JWKSCache()


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify a token's signature and return its claims.

    Authenticity only. Every *policy* question — token use, issuer, client
    allowlist, account type, org claim — is the policy's, and is applied by
    :func:`admit`. Splitting it this way is what keeps one definition of the
    rules: this function cannot accidentally accept something the policy would
    reject, because it does not decide.
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise TokenRejectedError("token header is unreadable") from exc

    kid = header.get("kid")
    if not kid:
        raise TokenRejectedError("token has no key id")
    # A JWT header is attacker-controlled JSON, so `kid` may be any JSON type.
    # A dict or list is unhashable and would raise TypeError out of the cache
    # lookup below — an unauthenticated 500 with no WWW-Authenticate, where this
    # module's contract is that a bad token is a 401.
    if not isinstance(kid, str):
        raise TokenRejectedError("token key id is not a string")

    # Only RS256. The algorithm is pinned rather than read from the header,
    # because honouring the header's choice is what allows the `alg: none` and
    # HS256-with-the-public-key-as-secret confusions. It also keeps this
    # verifier from ever accepting the legacy HS256 token: a caller cannot
    # present a self-signed org token here and have it verified.
    if header.get("alg") != "RS256":
        raise TokenRejectedError("token is not signed with RS256")

    # A missing JWKS URL stays a TokenPolicyError: that is an operator
    # misconfiguration, and the caller must not be told their token was bad when
    # the service cannot verify any token. Every OTHER retrieval failure —
    # unreachable endpoint, timeout, non-JSON body — is an availability problem
    # that must not surface as a 500 on an unauthenticated path either, so it is
    # answered as a denial and logged with its real cause.
    try:
        key = jwks_cache.get(kid)
    except TokenPolicyError:
        raise
    except Exception as exc:
        logger.error("could not retrieve signing keys for domain auth: %s", exc)
        raise TokenRejectedError("token signing key is unavailable") from exc
    if key is None:
        raise TokenRejectedError("token key id is not published by the user pool")

    try:
        # Signature and expiry only. Issuer is checked by the policy so there is
        # one place that decides it; audience verification is off because
        # Cognito access tokens carry `client_id` rather than `aud`, and the
        # policy checks that against its allowlist.
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": False,
                "verify_iss": False,
                "require_exp": True,
            },
        )
    except (JOSEError, ValueError, TypeError) as exc:
        # The reason is logged, not returned: a denial should not tell a caller
        # which part of their forgery was wrong.
        #
        # JOSEError rather than JWTError: `JWKError` is a sibling of `JWTError`
        # under `JOSEError`, not a subclass, so a structurally unusable published
        # key escaped a `JWTError`-only handler. `ValueError`/`TypeError` cover
        # the same class from the underlying key construction (e.g. "e must be
        # >= 3 and < n"), which raises neither. All of them are reachable without
        # a credential, so none may become a 500.
        logger.warning("domain token verification failed: %s", exc)
        raise TokenRejectedError("token signature or expiry is invalid") from exc


# ---------------------------------------------------------------------------
# Request-time admission
# ---------------------------------------------------------------------------


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


# The single denial message for every workspace-authorization refusal. Held as a
# constant so the "one indistinguishable answer" property is visible in one
# place and cannot drift apart as new refusal branches are added.
_WORKSPACE_NOT_AUTHORIZED = "not authorized for this workspace"


async def require_verified_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(domain_bearer),
) -> VerifiedCaller:
    """Admit a caller under the strict token policy.

    Header stripping happens FIRST, before a principal exists, so no later step
    can read a client-supplied identity even by mistake. The stripped mapping is
    stashed on ``request.state`` for downstream consumers; the raw headers are
    left untouched, which is why consumers must take the sanitized copy rather
    than reading ``request.headers`` again.
    """
    safe_headers = strip_identity_headers(request.headers)
    request.state.safe_headers = safe_headers

    policy = getattr(request.app.state, "domain_policy", None)
    if policy is None:
        # Enforcement is off. Reaching here means a route required the strict
        # path while the service was configured without one — refuse rather than
        # fall through to the weaker validator.
        raise _forbidden("domain authorization is not configured on this deployment")

    if credentials is None or not credentials.credentials:
        raise _unauthorized("a bearer access token is required")

    try:
        claims = verify_access_token(credentials.credentials)
        # The policy applies token_use, issuer, client allowlist, subject, org
        # claim and account type — in that order, and for the reasons its module
        # docstring gives. Naming the trusted validation path is required, and
        # is honest here only because the verification above meets that path's
        # standard (JWKS signature + expiry + pinned algorithm).
        principal = policy.admit(claims, validation_path=TRUSTED_VALIDATION_PATH)
    except TokenRejectedError as exc:
        logger.warning("domain token rejected: %s", exc)
        raise _unauthorized(str(exc)) from exc

    return VerifiedCaller(principal=principal, safe_headers=safe_headers)


# ---------------------------------------------------------------------------
# Grant retrieval and the authorization decision
# ---------------------------------------------------------------------------


def _grant_to_policy_object(record: WorkspaceGrantRecord) -> WorkspaceGrant:
    """Translate a stored row into the policy's grant object.

    Unrecognized stored permission values are dropped rather than passed
    through: a value this build cannot reason about grants nothing. The result
    is closed over the policy's implication rules, so a stored ADMINISTER
    implies the rest exactly as the model defines it — the closure is not
    re-implemented here.
    """
    known: set[Permission] = set()
    for value in record.permission_values():
        try:
            known.add(Permission(value))
        except ValueError:
            logger.warning(
                "workspace grant %s carries unrecognized permission %r; ignoring it",
                record.id,
                value,
            )
    return WorkspaceGrant(
        workspace_id=str(record.workspace_id),
        org_id=str(record.org_id),
        principal=record.principal,
        permissions=expand_permissions(known),
    )


async def load_workspace_authorization(
    db: AsyncSession, workspace_id: uuid.UUID, principal: str
) -> tuple[WorkspaceAuthorizationModel, str | None]:
    """Read the live grant for one (workspace, principal) from the server.

    Returns the populated model and the workspace's stored org id. Read per
    operation rather than cached on the session: caching the result is authority
    inherited from sign-in, which is the thing R6 removes. A revoked grant is
    simply absent from the model, so revocation between admission and execution
    denies the operation.
    """
    workspace = (
        await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        return WorkspaceAuthorizationModel(), None

    record = (
        await db.execute(
            select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_id == workspace_id,
                WorkspaceGrantRecord.principal == principal,
                WorkspaceGrantRecord.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    model = WorkspaceAuthorizationModel()
    if record is not None:
        model.record_grant(_grant_to_policy_object(record))
    return model, str(workspace.org_id)


async def authorize_workspace_operation(
    db: AsyncSession,
    caller: VerifiedCaller,
    workspace_id: uuid.UUID,
    permission: Permission,
) -> WorkspaceGrant:
    """Authorize one workspace operation against server-held state.

    The operation authorization object is constructed HERE, bound to this
    invocation, from the verified principal and the resolved workspace. It is
    never accepted from the request: the point of requiring the object is that
    the server issued it, so a fabricated envelope arriving with the payload is
    not one.
    """
    model, workspace_org_id = await load_workspace_authorization(
        db, workspace_id, caller.principal.subject
    )
    # One message for every "this workspace is not yours to see" case:
    # nonexistent, owned by another organization, or existing with no grant for
    # this principal. Returning 403 for all three is not sufficient on its own —
    # a differing `detail` string is just as enumerable as a differing status
    # code, which is what a test comparing the two response bodies caught. The
    # precise reason is logged for operators instead.
    if workspace_org_id is None or workspace_org_id != caller.principal.org_id:
        logger.warning(
            "workspace authorization refused: principal=%s workspace=%s "
            "reason=%s caller_org=%s workspace_org=%s",
            caller.principal.subject,
            workspace_id,
            "workspace does not exist" if workspace_org_id is None else "cross-org",
            caller.principal.org_id,
            workspace_org_id,
        )
        raise _forbidden(_WORKSPACE_NOT_AUTHORIZED)

    authorization = OperationAuthorization(
        operation_id=str(uuid.uuid4()),
        workspace_id=str(workspace_id),
        principal=caller.principal.subject,
        permission=permission,
    )
    try:
        return model.authorize_operation(
            caller.principal,
            authorization,
            str(workspace_id),
            permission,
            workspace_org_id=workspace_org_id,
        )
    except AuthorizationDeniedError as exc:
        logger.warning(
            "workspace authorization denied: principal=%s workspace=%s permission=%s reason=%s",
            caller.principal.subject,
            workspace_id,
            permission.value,
            exc,
        )
        # The policy's reason is deliberately NOT returned. It distinguishes
        # "no grant" from "grant for the wrong org" from "insufficient
        # permission", and that distinction is exactly what makes workspace ids
        # and grant state enumerable. Operators get it from the log above.
        raise _forbidden(_WORKSPACE_NOT_AUTHORIZED) from exc


async def authorize_organization_operation(
    db: AsyncSession,
    caller: VerifiedCaller,
    permission: Permission,
) -> None:
    """Authorize an operation over the organization or one of its collections.

    Separate from the workspace path because the policy's ``authorize_request``
    deliberately REFUSES org-scoped endpoints against a workspace grant, and it
    is right to: holding one workspace grant is not organization authority, and
    treating it as such is how a single-workspace member reaches an org-wide
    collection.

    Organization authority is therefore defined as holding the required
    permission on every LIVE workspace in the org — the conservative reading,
    and the only one available without a separate org-grant table. It is
    conservative in the safe direction: it can refuse someone who should be
    allowed (an operator adds the missing grant), where the permissive reading
    would admit someone who should not be.

    "Live" excludes the decommissioned states in
    :data:`DECOMMISSIONED_WORKSPACE_STATUSES`, and the query below applies that
    filter. It has to: ``DELETE /workspaces/{id}`` is a SOFT delete that leaves
    the row behind with ``status="Teardown"``, and a torn-down workspace never
    acquires a grant. Counting those rows made organization authority
    monotonically harder to hold over the lifetime of an org — every workspace
    ever deleted became a permanent, ungrantable denial for every member — which
    is an availability failure rather than the intended conservatism. An earlier
    revision of this docstring said "active" while the query filtered on
    ``org_id`` alone; the filter is now real rather than described.

    NOTE ON COLLECTION LISTINGS. This check is currently the ONLY gate on
    org-scoped collection reads: no handler narrows its result set per caller,
    and :func:`authorized_workspace_ids` has no production callers yet. Nor
    would applying it here change any answer — passing the test below requires
    ``every active workspace in the org`` to be covered by this principal's
    grants, so the function would return the whole org and filter nothing. The
    two statements a reader might expect are therefore both false: a
    partially-granted caller does not "still read their own workspaces", it is
    REFUSED outright and never reaches the listing.

    Research rows carry a workspace foreign key rather than an org column.  The
    research handlers therefore inner-join that server-held workspace and scope
    every collection, aggregate, direct-id read and mutation to its stored org.
    Null/dangling legacy rows fail closed under strict enforcement.
    """
    org_id = caller.principal.org_id
    workspace_rows = (
        (
            await db.execute(
                select(Workspace.id).where(
                    Workspace.org_id == _as_uuid(org_id),
                    Workspace.status.notin_(DECOMMISSIONED_WORKSPACE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )

    grants = (
        (
            await db.execute(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.org_id == _as_uuid(org_id),
                    WorkspaceGrantRecord.principal == caller.principal.subject,
                    WorkspaceGrantRecord.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not grants:
        raise _forbidden(
            "endpoint requires organization authority; this principal holds no grant"
        )

    granted_ids = {str(g.workspace_id) for g in grants}
    if (
        not all(permission in _grant_to_policy_object(g).permissions for g in grants)
        or not {str(w) for w in workspace_rows} <= granted_ids
    ):
        raise _forbidden(
            f"organization authority requires {permission.value} across the organization"
        )


async def authorized_workspace_ids(
    db: AsyncSession, caller: VerifiedCaller, permission: Permission
) -> list[uuid.UUID]:
    """Workspace ids this caller may exercise ``permission`` on.

    NOT YET WIRED INTO ANY HANDLER. Stated plainly because the previous wording
    ("the filter applied to every collection response") described an intended
    end state as though it were current behaviour, which overstates what the
    service enforces today: every org-scoped collection read returns the
    organization's rows, gated only by
    :func:`authorize_organization_operation`.

    It is kept, exported and tested because it is the right primitive for a
    per-caller narrowing once organization authority stops being conjunctive
    ("holds the permission on EVERY active workspace"). Under the conjunctive
    rule it is provably a no-op — a caller who passes the org check holds grants
    covering the whole org, so this returns the whole org.
    """
    records = (
        (
            await db.execute(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.org_id == _as_uuid(caller.principal.org_id),
                    WorkspaceGrantRecord.principal == caller.principal.subject,
                    WorkspaceGrantRecord.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        r.workspace_id
        for r in records
        if permission in _grant_to_policy_object(r).permissions
    ]


def _as_uuid(value: str) -> uuid.UUID:
    """Parse a verified org claim into a UUID.

    The claim is verified but still externally supplied, so a malformed value is
    a denial rather than a 500 from the query layer.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise _forbidden("token organization claim is not a valid identifier") from exc


__all__ = [
    "Scope",
    "VerifiedCaller",
    "authorize_organization_operation",
    "authorize_workspace_operation",
    "authorized_workspace_ids",
    "build_domain_policy",
    "jwks_cache",
    "load_workspace_authorization",
    "require_verified_caller",
    "verify_access_token",
]
