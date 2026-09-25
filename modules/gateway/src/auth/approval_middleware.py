"""Require current tenant membership or registered machine authority on spend paths.

Platform recovery administrators retain their existing exemption. Human org
claims select a tenant but do not prove current membership. Database failures
produce retryable 503; missing or ambiguous membership produces policy denial.
"""

from __future__ import annotations

import json
from enum import Enum

from sqlalchemy import select
from starlette.types import ASGIApp, Receive, Scope, Send

from src.shared.config import get_settings
from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.logging import get_logger
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext

logger = get_logger(__name__)

# Reused verbatim from src/auth/org_id_resolver.py so the error code is
# consistent platform-wide (a client cannot tell a middleware-produced 409 from
# a route-produced one).
_ERROR_CODE = "user_not_assigned_to_org"
_ERROR_MESSAGE = "Your account is pending approval. Ask a platform admin to approve your access."

_METRIC_NAMESPACE = "ADP/Approval"

# #5666 (A11): the 503 counterpart to the 409 above, following the budget
# middleware's #4075 split between "policy says no" and "policy is unknown".
_UNAVAILABLE_CODE = "approval_check_unavailable"
_UNAVAILABLE_MESSAGE = "Unable to verify your access approval right now. Please retry shortly."
_UNAVAILABLE_RETRY_AFTER = b"2"


class Verdict(Enum):
    """Three-state approval outcome — #5666 (A11).

    A bool cannot distinguish "proven not approved" from "could not determine",
    and that is precisely how the fail-open defect hid: the error handler returned
    the same ``True`` an approved user gets, so an admitted-because-unprovable
    request was indistinguishable from an admitted-because-approved one at the
    call site AND in the response. Naming INDETERMINATE forces every caller to say
    what it does about it.
    """

    APPROVED = "approved"
    NOT_APPROVED = "not_approved"
    INDETERMINATE = "indeterminate"


class ApprovalEnforcementMiddleware:
    """Reject un-approved human callers on the enforced (spend) paths.

    Gating is by ``startswith`` against the shared ``ENFORCED_PATHS`` registry
    rather than per-route ``Depends()``. Adding a dependency to N route functions
    would recreate the duplicated-path-list problem that registry exists to
    prevent (Issue #2792 / #2809: ``/openai/v1/responses`` slipped enforcement
    twice). Importing the registry means a new spend route is registered for
    approval enforcement in exactly one place — the same place it is registered
    for budget and rate limits.

    Registered unconditionally in ``src/app.py``; the ``BG_ENFORCE_ORG_ASSIGNMENT``
    flag short-circuits inside :meth:`_is_approved` so it stays flippable by env
    change + pod recycle, with no code-path difference between on and off.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self._should_enforce(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        state = scope.get("state", {})
        token_context: TokenContext | None = state.get("token_context")

        if token_context is None:
            # Unauthenticated, or auth failed. Let the route return 401 —
            # mirrors the budget middleware's skip-if-no-context behaviour.
            await self.app(scope, receive, send)
            return

        verdict = await self._approval_verdict(token_context)
        if verdict is Verdict.APPROVED:
            await self.app(scope, receive, send)
            return

        # Drain the request body — some ASGI servers (Uvicorn/h11) require the
        # request body to be fully consumed before a response can be sent,
        # otherwise the connection hangs.
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                # Client disconnected before we could respond.
                return
            if not msg.get("more_body", False):
                break

        # #5666 (A11): an indeterminate verdict denies, but as a RETRYABLE 503
        # rather than the 409 policy denial — the caller may well be approved and
        # we simply could not read the database.
        if verdict is Verdict.INDETERMINATE:
            await self._send_check_unavailable(send, token_context)
            return

        await self._send_not_approved(send, token_context)

    def _should_enforce(self, path: str) -> bool:
        return any(path.startswith(p) for p in ENFORCED_PATHS)

    async def _approval_verdict(self, ctx: TokenContext) -> Verdict:
        """Evaluate supported authenticated principal types explicitly."""
        if not get_settings().enforce_org_assignment:
            return Verdict.APPROVED
        if ctx.account_type == "human" and ctx.is_admin:
            return Verdict.APPROVED
        if ctx.account_type != "human":
            # These fields are populated only by authentication adapters after
            # registry/alias resolution, never by request attribution headers.
            if not ctx.org_id.strip():
                return Verdict.NOT_APPROVED
            if ctx.auth_source == "iam" and ctx.agent_registry_id:
                return Verdict.APPROVED
            if ctx.account_type == "service" and ctx.canonical_service_principal_id:
                return await self._db_has_org(ctx.user_id, context=ctx)
            return Verdict.NOT_APPROVED
        return await self._db_has_org(ctx.user_id, context=ctx)

    async def _db_has_org(self, user_id: str, *, context: TokenContext | None = None) -> Verdict:
        """Require a current membership in the effective authenticated tenant.

        ``is_active`` is workspace selection, not a revocation flag. Multiple
        org-local accounts are ordinary; absent selection is resolved only when
        one current membership tenant remains. Ambiguity is a policy decision.
        """
        try:
            async with self._get_session() as session:
                from src.shared.identity.workspaces import linked_user_ids
                from src.shared.models.onboarding import TenantMembership
                from src.shared.models.persona_models import ServicePrincipal

                if context is not None and context.account_type == "service":
                    principal = await session.scalar(
                        select(ServicePrincipal.canonical_service_principal_id).where(
                            ServicePrincipal.canonical_service_principal_id == context.canonical_service_principal_id,
                            ServicePrincipal.org_id == context.org_id,
                            ServicePrincipal.status == "active",
                        )
                    )
                    return Verdict.APPROVED if principal else Verdict.NOT_APPROVED
                users = (await session.scalars(select(User).where(User.cognito_sub == user_id))).all()
                ids: set[str] = set()
                for user in users:
                    ids.update(await linked_user_ids(session, user, username=context.cognito_username if context else ""))
                memberships = (await session.scalars(select(TenantMembership).where(TenantMembership.user_id.in_(ids)))).all()
                tenant_ids = {row.tenant_id for row in memberships if row.tenant_id}
                selected = context.org_id.strip() if context else ""
                if not selected and len(tenant_ids) == 1:
                    selected = next(iter(tenant_ids))
                    if context is not None:
                        context.org_id = selected
                approved = bool(selected and selected in tenant_ids)
            return Verdict.APPROVED if approved else Verdict.NOT_APPROVED
        except Exception as e:
            if get_settings().approval_fail_open:
                # Break-glass: opt-in, loud, and metered separately so an operator
                # can see it is active and how much traffic it is admitting.
                logger.warning(
                    f"approval_check_failed_fail_open: BG_APPROVAL_FAIL_OPEN=true, admitting without approval check user_id={user_id} error={e}",
                    extra={"event": "approval_check_failed_fail_open", "user_id": user_id},
                )
                self._emit_metric("ApprovalCheckFailedFailOpen")
                return Verdict.APPROVED

            # Greppable marker for CloudWatch Logs Insights.
            logger.error(
                f"approval_check_failed_fail_closed: denying request, approval indeterminate user_id={user_id} error={e}",
                extra={"event": "approval_check_failed_fail_closed", "user_id": user_id},
            )
            self._emit_metric("ApprovalCheckFailedFailClosed")
            return Verdict.INDETERMINATE

    @staticmethod
    def _get_session():
        """Session for a non-request context.

        Same pattern as ``BudgetEnforcementService._get_session``: under RDS IAM
        auth the engine is reset first so the connection uses a fresh token,
        otherwise pooled connections fail with 'PAM authentication failed'.
        """
        from src.shared.database import get_session_factory, reset_engine

        settings = get_settings()
        if settings.rds_iam_auth and settings.rds_host:
            reset_engine()
        return get_session_factory()()

    @staticmethod
    def _emit_metric(metric_name: str) -> None:
        try:
            from src.admin.cognito_claims import emit_metric

            emit_metric(_METRIC_NAMESPACE, metric_name)
        except Exception:  # pragma: no cover - metrics must never break a request
            logger.debug("Failed to emit approval metric", exc_info=True)

    async def _send_check_unavailable(self, send: Send, ctx: TokenContext) -> None:
        """Write the retryable 503 for an indeterminate approval — #5666 (A11).

        Deliberately NOT the 409. A 409 tells this caller to go find an admin and
        tells an operator's dashboard that an entitlement is missing; neither is
        true here, and during a database incident the 409 rate would spike and send
        someone hunting a phantom approvals bug. 503 + ``Retry-After`` is the
        honest answer — the request may well be from an approved user — and clients
        recover on their own once the database returns, with no operator action.
        """
        body_bytes = json.dumps({"detail": {"error": _UNAVAILABLE_CODE, "message": _UNAVAILABLE_MESSAGE}}).encode("utf-8")

        logger.error(
            f"Inference blocked - approval indeterminate (fail-closed): user_id={ctx.user_id}",
            extra={"event": "approval_enforcement_unavailable", "user_id": ctx.user_id},
        )
        self._emit_metric("ApprovalEnforcementUnavailable")

        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body_bytes)).encode()),
                    (b"retry-after", _UNAVAILABLE_RETRY_AFTER),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body_bytes})

    async def _send_not_approved(self, send: Send, ctx: TokenContext) -> None:
        """Write the 409 JSON response directly via ASGI send().

        The payload is wrapped under ``detail`` to match FastAPI's
        ``HTTPException`` serialization, so this is indistinguishable from the
        409 ``resolve_effective_org_id`` raises on the BYO-credential routes.
        """
        body_bytes = json.dumps({"detail": {"error": _ERROR_CODE, "message": _ERROR_MESSAGE}}).encode("utf-8")

        logger.warning(
            f"Inference blocked - user not approved (no org assignment): user_id={ctx.user_id}",
            extra={"event": "approval_enforcement_blocked", "user_id": ctx.user_id},
        )
        self._emit_metric("ApprovalEnforcementBlocked")

        await send(
            {
                "type": "http.response.start",
                "status": 409,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body_bytes)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body_bytes})
