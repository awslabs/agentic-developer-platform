"""Audit middleware — records API attempts, allowed and denied, to the events table.

Issue #5673 (A17), finding f-93177516-24d1-451e-bfd1-ddd2e3c60048.

WHAT THIS IS THE ONLY COPY OF

This is the sole application-level audit trail in Superplane. There is no database
trigger, no outbox and no second writer behind it; the infrastructure access logs carry
request lines but no authenticated principal, so they cannot answer "who". Every gap here
is a gap in the only record that exists.

THE FOUR DEFECTS THIS FILE USED TO HAVE, AND WHAT REPLACED EACH

1. IT RECORDED NOTHING AT ALL IN THE DEPLOYED CONFIGURATION. The middleware resolved the
   caller's organization by independently decoding the `Authorization` header with the
   legacy HS256 decoder (`app.middleware.auth.decode_token`). Enforced environments issue
   Cognito RS256 tokens, which that decoder cannot verify -- deliberately, since
   `decode_token` pins the server's own algorithm list. So the decode raised, the
   middleware got no org, and its response to "no org" was `return response` with no row
   and no log. `installation/manifests.py` sets `DOMAIN_AUTH_ENFORCED=true`, so this was
   the SHIPPED state: an empty audit table and no signal that it was empty.

   Now identity is READ from the domain guard's verified decision instead of re-derived.
   `app/domain_guard.py` publishes `request.state.caller` after verifying the token
   signature and confirming a server-held grant for the specific operation, so it is the
   one component that already knows who is calling. Reading it means every
   authentication configuration produces the same audit fields.

   The legacy decode path is REMOVED, not kept as a fallback. A second identity resolver
   that answers the same question more weakly is how one deployment keeps silently
   recording nothing while appearing fixed -- the original defect in a harder-to-detect
   form. The legacy path still authenticates requests (`get_current_org`), and those
   requests are still audited: their org arrives via the same `request.state` publication
   (see `_resolve_identity`), not via a token this middleware decodes itself.

2. IT DROPPED EVERY REFUSAL. `if response.status_code >= 400: return response` meant no
   denied authorization and no unauthenticated call was ever recorded. That is the
   opposite of the priority: a 403 on another tenant's workspace is the event an audit
   trail exists to surface, and a probe across tenants left no footprint at all. Replaced
   by an explicit `outcome` field, so a refusal is a recorded outcome rather than a
   reason to skip.

3. IT NAMED THE TENANT WHERE IT SHOULD NAME THE PERSON. `user_id = str(org_id)` put the
   organization into the actor column, so an incident reviewer asking "who took this
   action" learned only which company owned it. Now `principal` carries the verified
   subject and `org_id` carries the tenant, and they are never the same value.

4. ITS FAILURES WERE INVISIBLE. Two of the three no-row paths logged nothing. Every path
   through `dispatch` now either persists a record or increments
   `audit_write_failures` with a warning -- asserted by
   `tests/test_audit_middleware.py::TestNoSilentSkip`.

THE DELIBERATE FAILURE POLICY, STATED BECAUSE IT IS A TRADEOFF AND NOT AN OVERSIGHT

An audit write that fails does NOT fail the caller's request. "Fail loudly" here means
visible in logs and metrics, never fatal to the request. The issue asks for loudness and
also warns of the inverse hazard, so the boundary is chosen explicitly: a transient
database fault in the audit path would otherwise reject legitimate provisioning calls for
every tenant at once, turning a logging outage into a control-plane outage. A gap in the
trail that operators can SEE is the lesser failure. `TestNoSilentSkip` asserts both
halves -- the counter moves and the caller's response is untouched.

WHERE THIS SITS IN THE STACK, AND WHY IT MOVED

`app/main.py` adds this middleware LAST so it is OUTERMOST. Starlette's `add_middleware`
prepends, so the last one added wraps all the others. Before this change the rate limiter
was outermost and this middleware sat inside it, so a 429 short-circuit returned without
ever reaching the audit layer -- an attacker could stay entirely out of the audit trail by
tripping the rate limiter, which is the traffic pattern most worth recording. Outermost
means a short-circuit rejection from any inner middleware is still recorded.
"""

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings
from app.database import async_session_factory
from app.services.audit import (
    HTTP_METHOD_TO_ACTION,
    OUTCOME_ALLOWED,
    OUTCOME_DENIED,
    OUTCOME_ERROR,
    PRINCIPAL_UNRESOLVED,
    audit_write_failures,
    extract_resource_from_path,
    log_event,
)

logger = logging.getLogger(__name__)

# Paths to skip for audit logging (health checks, docs, etc.)
#
# These are unauthenticated infrastructure endpoints with no tenant resource behind them.
# Auditing a liveness probe would add a row per health check per pod -- volume that buries
# the records an incident reviewer is looking for, with no security question answered.
SKIP_PATHS = frozenset(
    {
        "/health",
        "/healthz",
        "/readyz",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)

# Mutating methods are always audited: they change tenant state.
AUDITABLE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Reads are audited only when the deployment opts in (`AUDIT_READ_COVERAGE=true`).
#
# Gated, and defaulting to OFF, for two reasons the issue names. Reads are the bulk of
# traffic, so enabling this multiplies row volume and adds a write to the hot path of
# every GET -- a latency and storage change each environment should choose deliberately.
# And an unbounded record of refusals is itself an amplifier: a caller who can generate
# rejected reads can drive audit writes at will. Off by default means this change does not
# silently alter load in any existing environment.
READ_METHODS = frozenset({"GET", "HEAD"})


class AuditMiddleware(BaseHTTPMiddleware):
    """Records who attempted what, on which tenant, and whether it was allowed."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Execute the request, then record the attempt.

        Recording happens AFTER `call_next` because the outcome is part of the record:
        whether the guard admitted the caller is only known once the request has been
        through the stack. The verified caller is readable at this point -- Starlette
        backs `request.state` with `scope["state"]`, which the guard's later write
        mutates in place, so a value published by a route dependency is visible here.
        Confirmed empirically before relying on it, including for denials.
        """
        method = request.method.upper()
        path = request.url.path

        if not self._is_auditable(method, path):
            return await call_next(request)

        try:
            response = await call_next(request)
        except Exception:
            # The server's outer error middleware has not produced its 500 yet.
            # Record the attempt, then preserve the original exception/response policy.
            await self._record_safely(
                request, Response(status_code=500), method=method, path=path
            )
            raise
        await self._record_safely(request, response, method=method, path=path)
        return response

    async def _record_safely(self, request, response, *, method, path):
        try:
            await self._record(request, response, method=method, path=path)
        except Exception:
            # SQL/driver exceptions can contain parameters or connection strings.
            # Publish only the fixed failure classification.
            audit_write_failures.record_failure(
                method=method, path=path, reason="persist_failed"
            )

    @staticmethod
    def _is_auditable(method: str, path: str) -> bool:
        """Whether this request is in scope for auditing."""
        if path in SKIP_PATHS:
            return False
        if method in AUDITABLE_METHODS:
            return True
        # Read coverage is re-read from settings on each request rather than captured at
        # construction, so a test (and an operator restarting with a changed value) gets
        # the configured behaviour without rebuilding the middleware stack.
        return method in READ_METHODS and bool(
            getattr(settings, "audit_read_coverage", False)
        )

    async def _record(
        self, request: Request, response: Response, *, method: str, path: str
    ) -> None:
        """Persist exactly one audit record for this attempt."""
        principal, org_id = self._resolve_identity(request)

        # The outcome is taken from the RESPONSE STATUS, not from whether an identity was
        # resolved. A 403 from the guard and a 200 from a handler are both facts about
        # what the server decided; conflating "unidentified" with "denied" would mislabel
        # a legitimate unauthenticated public call.
        outcome = (
            OUTCOME_ERROR
            if response.status_code >= 500
            else OUTCOME_DENIED
            if response.status_code >= 400
            else OUTCOME_ALLOWED
        )

        resource_type, resource_id = extract_resource_from_path(path)
        action = HTTP_METHOD_TO_ACTION.get(method, method.lower())

        # CONTENT BOUNDARY. Only route, method, outcome, principal, tenant, status and
        # time are recorded. No request body, no query string, no headers, no token
        # material -- asserted by `TestRecordContainsNoSensitiveMaterial`.
        #
        # `request.url.path` deliberately, never `str(request.url)`: the full URL carries
        # the query string, and secrets arrive in query strings. This matters more now
        # that refusals are recorded, since a rejected request is disproportionately
        # likely to carry malformed or attacker-supplied content, and `events` is
        # long-retained and readable by everyone with audit access.
        #
        # `message` is composed from the method and path only -- both already recorded
        # above, so it introduces no new material.
        async with async_session_factory() as session:
            await log_event(
                db=session,
                org_id=org_id,
                principal=principal,
                outcome=outcome,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                event_type="api_call",
                message=f"{method} {path}",
                source_ip=request.client.host if request.client else None,
                request_path=path,
                http_status=response.status_code,
            )

    @staticmethod
    def _resolve_identity(request: Request) -> tuple[str, uuid.UUID | None]:
        """The acting principal and target tenant, from ALREADY-VERIFIED state only.

        This middleware never decodes a token. Both sources below are values some earlier
        component verified and published; nothing here is derived from header or body text
        the client controls, which is what makes the recorded attribution trustworthy
        rather than merely present.

        Returns `(PRINCIPAL_UNRESOLVED, None)` when no identity was established -- an
        unauthenticated call, or one rejected before the guard admitted a token. That is
        recorded, not dropped: "someone unidentified attempted this" is exactly the
        signal an incident reviewer needs, and it is the row the old NOT NULL `org_id`
        made unrepresentable.
        """
        caller = getattr(request.state, "caller", None)
        if caller is not None:
            principal = getattr(caller.principal, "subject", None)
            raw_org = getattr(caller.principal, "org_id", None)
            org_id = None
            if raw_org is not None:
                try:
                    org_id = uuid.UUID(str(raw_org))
                except (ValueError, TypeError):
                    # Verified but unusable as an identifier. The guard answers this with
                    # a 403; here it must not lose the record, so the attempt is kept as
                    # unattributed rather than discarded.
                    org_id = None
            return (principal or PRINCIPAL_UNRESOLVED, org_id)

        # Legacy org-scoped path: enforcement off, so no `caller` exists. `get_current_org`
        # publishes the org it verified (see app/middleware/auth.py), which keeps this
        # middleware out of the business of validating tokens on that path too.
        legacy_org = getattr(request.state, "audit_org_id", None)
        legacy_principal = getattr(request.state, "audit_principal", None)
        if legacy_org is not None:
            return (
                str(legacy_principal) if legacy_principal else PRINCIPAL_UNRESOLVED,
                legacy_org,
            )

        return (
            str(legacy_principal) if legacy_principal else PRINCIPAL_UNRESOLVED,
            None,
        )
