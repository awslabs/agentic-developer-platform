"""
Approval (org-assignment) enforcement middleware (pure ASGI) — Issue #4144.

Authentication and authorization were decoupled on the inference path: any valid
Cognito JWT built a ``TokenContext`` regardless of whether a platform admin had
approved the caller, so an un-approved human could bill Bedrock directly (curl,
SDK, Claude Code) while the SPA still showed them a "request access" screen. The
``user_not_assigned_to_org`` 409 existed only on BYO-credential routes via
``src/auth/org_id_resolver.py`` — never on inference.

This middleware closes that gap: human callers who are not approved are rejected
on the spend paths, while platform admins and agents are exempt.

IMPORTANT: This is a raw ASGI middleware — NOT BaseHTTPMiddleware. Same reason as
``src/budget/enforcement_middleware.py``: BaseHTTPMiddleware has a known Starlette
bug where returning a response from dispatch() without calling call_next() hangs
indefinitely. We write the 409 directly via the ASGI ``send`` callable. For the
same reason we cannot reuse ``resolve_effective_org_id`` — it raises
``HTTPException``, and no exception-handler middleware sits above this one, so the
raise would never become a response. The query is reimplemented inline instead.

"Approved" is resolved from Postgres, NOT from the token's ``org_id`` claim alone
(Issue #600's precedent). Keying on the claim would lock out approved users: the
login-time auto-match approval path calls ``attach_approved_member(...,
sync_cognito_claims=False)`` (``src/admin/onboarding/handler.py``), and the
pre-token-generation Lambda reads Cognito *user attributes* rather than Postgres,
so those users are approved in the database and permanently org-less in every
token they mint. Even on the admin-approval path ``sync_cognito_role_claims`` is
explicitly best-effort. Postgres ``users`` is the source of truth; the claim is a
cache and only ever used as a fast path.

Fail-open policy: if the approval lookup itself raises, the request is ADMITTED
and a warning + CloudWatch metric are emitted. This deliberately diverges from
the budget middleware's fail-closed policy (Issue #4075). Budget failing open
means uncapped spend with no cap to stop it; approval failing closed would mean a
total inference outage for *every* user during a transient DB blip. The residual
risk here is bounded — an un-approved user spends during a DB outage — and is
still capped by budget and rate limits.
"""

from __future__ import annotations

import json

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

        if await self._is_approved(token_context):
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

        await self._send_not_approved(send, token_context)

    def _should_enforce(self, path: str) -> bool:
        return any(path.startswith(p) for p in ENFORCED_PATHS)

    async def _is_approved(self, ctx: TokenContext) -> bool:
        """Whether this caller may spend.

        Order matters: every exemption is checked before the DB read, so the
        hot path for an approved user with a populated claim stays DB-free.
        """
        if not get_settings().enforce_org_assignment:
            # Flag off — inert. Read per-request (not captured at import time)
            # so the flag is flippable by env change + pod recycle.
            return True

        if ctx.account_type != "human":
            # Agents / service accounts (IAM/SigV4 path). The pre-token-generation
            # Lambda stamps custom:account_type="service" for the
            # client-credentials flow. Note the default when the claim is absent
            # is "human" (src/auth/middleware.py), which fails safe in the right
            # direction: an unstamped caller is gated, not waved through.
            return True

        if ctx.is_admin:
            # Platform admins are never gated. The admin who approves everyone
            # would otherwise be locked out first (the #3984 self-lockout class).
            # is_admin is derived from role/groups independently of org_id, so an
            # admin with no org still passes.
            return True

        if (ctx.org_id or "").strip():
            # Fast path: the token already carries an org assignment.
            return True

        return await self._db_has_org(ctx.user_id)

    async def _db_has_org(self, user_id: str) -> bool:
        """Source-of-truth approval check: does Postgres have an org for this sub?

        A ``users`` row exists only after approval (``/access/status`` returns
        "registered" precisely when it does), and ``org_id`` is non-nullable on
        the row, so "has a users row with a non-empty org_id" is the correct
        DB-backed definition of approved. Mirrors ``resolve_effective_org_id``'s
        query exactly; ``cognito_sub`` is indexed.

        Fails OPEN on error — see the module docstring for why.
        """
        try:
            async with self._get_session() as session:
                stmt = select(User.org_id).where(User.cognito_sub == user_id)
                result = await session.execute(stmt)
                return bool(result.scalar_one_or_none())
        except Exception as e:
            # Greppable marker for CloudWatch Logs Insights.
            logger.warning(
                f"approval_check_failed_fail_open: admitting request without approval check user_id={user_id} error={e}",
                extra={"event": "approval_check_failed_fail_open", "user_id": user_id},
            )
            self._emit_metric("ApprovalCheckFailedFailOpen")
            return True

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
