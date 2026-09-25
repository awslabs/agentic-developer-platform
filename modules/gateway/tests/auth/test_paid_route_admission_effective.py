"""Author checks for paid-route inventory and middleware decisions (#5666 A11).

Inventory checks inspect create_app registration. Decision checks exercise an
isolated middleware with a synthetic downstream app and mocked database; they do
not establish composed authentication-to-provider behavior or independent review.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from tests.auth.test_approval_middleware import APPROVED_SUB, session_factory  # noqa: F401 -- shared local database fixture

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("BG_TOKEN_SECRET_KEY", "test-secret-key-do-not-use-in-production")

from src.shared.enforced_paths import ENFORCED_PATHS  # noqa: E402
from src.shared.schemas.auth import TokenContext  # noqa: E402

pytestmark = pytest.mark.asyncio


# How the paid route set is decided — and why NOT by a literal path list.
#
# The first version of this file listed the paid paths literally, with a comment
# claiming that was independent of ENFORCED_PATHS. It was not: the list was
# character-for-character the registry, so `test_no_paid_route_escapes_the_registry`
# compared the registry against itself and could never fail. It would have passed
# with flying colours through the entire #2792/#2809 defect it claims to guard.
#
# So the criterion here is route PROVENANCE, which is genuinely independent: any
# route mounted by the proxy router is presumed to spend money unless it appears in
# the small, individually-justified non-spending set below. That inverts the
# default — a new POST added to src/proxy/routes.py is enforced-or-failing, never
# silently unenforced — and it is exactly the #2792 defect's shape: the
# `/openai/v1/responses` passthrough was a new route in this very module that
# nobody added to the path lists.
_PAID_ROUTE_MODULES = ("src.proxy.routes",)

# Routes in a paid module that provably do not reach a provider. Each entry needs a
# reason, and adding one is a deliberate act a reviewer can see.
_NON_SPENDING = {
    "/v1/health": "liveness probe; no provider call, and must stay reachable",
    "/v1/models": "builds the list from local alias config — no AWS call (src/proxy/model_resolver.py)",
    "/v1/messages/count_tokens": "local `total_chars // 4` estimate; also covered by the /v1/messages entry",
}


def _mounted_paid_routes(app) -> set[str]:
    """Paid routes as the running app actually mounts them."""
    paid = set()
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        module = getattr(endpoint, "__module__", "") if endpoint else ""
        path = getattr(route, "path", "")
        if module in _PAID_ROUTE_MODULES and path not in _NON_SPENDING:
            paid.add(path)
    return paid


# Paths that MUST stay reachable for an un-approved user, or the gate becomes a
# lockout with no route back to approval.
RECOVERY_PATHS = (
    "/health",
    "/access/status",
    "/access/request",
)


def _app():
    from src.app import create_app

    return create_app()


def _ctx(*, user_id: str, org_id: str, account_type: str = "human", is_admin: bool = False) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type=account_type,
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


class TestMiddlewareIsEffectivelyWired:
    async def test_approval_middleware_is_in_the_real_stack(self):
        """Present in ``create_app()``, not merely importable."""
        from src.auth.approval_middleware import ApprovalEnforcementMiddleware

        stack = [m.cls for m in _app().user_middleware]
        assert ApprovalEnforcementMiddleware in stack, (
            "the approval gate is not registered in create_app(); every unit test of the class would "
            "still pass while the deployed app enforces nothing"
        )

    async def test_it_runs_after_authentication(self):
        """Ordering is load-bearing, and inverted relative to registration.

        Starlette runs middleware in reverse registration order, so
        TokenContextMiddleware must be registered LATER to execute FIRST. If the
        approval gate ran before it, ``token_context`` would always be None and the
        gate would skip every request — failing open globally and silently.
        """
        from src.auth.approval_middleware import ApprovalEnforcementMiddleware
        from src.auth.middleware import TokenContextMiddleware

        stack = [m.cls for m in _app().user_middleware]
        assert stack.index(TokenContextMiddleware) < stack.index(ApprovalEnforcementMiddleware), (
            "TokenContextMiddleware must execute before the approval gate, or token_context is None and the gate skips every request"
        )

    async def test_enforcement_is_on_by_default(self, monkeypatch):
        """The effective configuration, not the documented intent."""
        from src.shared.config import Settings

        monkeypatch.delenv("BG_ENFORCE_ORG_ASSIGNMENT", raising=False)
        assert Settings().enforce_org_assignment is True


class TestEveryMountedPaidRouteIsEnforced:
    async def test_no_paid_route_escapes_the_registry(self):
        """#2792/#2809 recurrence guard, run against mounted reality.

        ``/openai/v1/responses`` slipped enforcement twice historically because the
        path lists were duplicated. This asserts over the routes the app actually
        mounts, so a new paid route added without a registry entry fails here.
        """
        paid = _mounted_paid_routes(_app())
        assert paid, "found no paid routes at all — the proxy router module or the app wiring has changed"

        unenforced = sorted(p for p in paid if not any(p.startswith(e) for e in ENFORCED_PATHS))
        assert not unenforced, f"paid routes not covered by ENFORCED_PATHS (approval, budget and rate limits all skip them): {unenforced}"

    async def test_the_non_spending_exemptions_still_exist(self):
        """Guards the guard: an exemption for a deleted route is a hole in waiting.

        ``_NON_SPENDING`` is the one place this file can lose coverage, because an
        entry there removes a route from the paid set. If a path is renamed, the
        stale exemption keeps matching nothing while the NEW name lands in the paid
        set — that direction is caught above. This catches the opposite bookkeeping
        error, and keeps the exemption list honest about what it is suppressing.
        """
        paths = {getattr(r, "path", "") for r in _app().routes}
        stale = sorted(p for p in _NON_SPENDING if p not in paths)
        assert not stale, f"_NON_SPENDING exempts paths that are no longer mounted; re-verify and remove them: {stale}"

    async def test_registry_entries_all_correspond_to_something_mounted(self):
        """A stale entry is a false sense of coverage.

        Not a security hole by itself, but an entry matching nothing means the route
        was renamed — and the new name is probably unenforced.
        """
        paths = {getattr(r, "path", "") for r in _app().routes}
        stale = sorted(e for e in ENFORCED_PATHS if not any(p.startswith(e) for p in paths))
        assert not stale, f"ENFORCED_PATHS entries match no mounted route (renamed or removed?): {stale}"


class TestUnapprovedCallerIsDeniedOnMountedPaidRoutes:
    """Middleware-only decisions for paths also checked in the mounted inventory."""

    async def _run(self, path: str, ctx: TokenContext | None, *, verdict_source):
        """Drive the real ApprovalEnforcementMiddleware over ``path``."""
        from src.auth.approval_middleware import ApprovalEnforcementMiddleware

        reached: list[str] = []
        sent: list[dict] = []

        async def inner(scope, receive, send):
            reached.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"paid-call-made"})

        mw = ApprovalEnforcementMiddleware(inner)
        mw._get_session = verdict_source  # type: ignore[method-assign]

        scope = {
            "type": "http",
            "path": path,
            "method": "POST",
            "headers": [],
            "state": {} if ctx is None else {"token_context": ctx},
        }

        async def receive():
            return {"type": "http.request", "body": b'{"model":"anthropic.claude-3"}', "more_body": False}

        async def send(message):
            sent.append(message)

        await mw(scope, receive, send)
        status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
        body = next((json.loads(m["body"]) for m in sent if m["type"] == "http.response.body" and m["body"] != b"paid-call-made"), None)
        return reached, status, body

    @pytest.fixture
    def empty_db(self):
        """A session factory that finds no users row — the un-approved case."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        @asynccontextmanager
        async def _factory():
            session = MagicMock()
            result = MagicMock()
            result.all = MagicMock(return_value=[])
            session.scalars = AsyncMock(return_value=result)
            yield session

        return _factory

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/chat/completions",
            "/v1/messages",
            "/bedrock/invoke",
            "/bedrock/invoke-with-response-stream",
            "/model/anthropic.claude-3-sonnet/invoke",
            "/model/anthropic.claude-3-sonnet/invoke-with-response-stream",
            "/openai/v1/responses",
        ],
    )
    async def test_unapproved_human_never_reaches_the_provider(self, path, empty_db, monkeypatch):
        monkeypatch.setenv("BG_ENFORCE_ORG_ASSIGNMENT", "true")
        reached, status, body = await self._run(path, _ctx(user_id="sub-unapproved", org_id=""), verdict_source=empty_db)
        assert reached == [], f"{path}: an un-approved human reached the paid provider"
        assert status == 409
        assert body["detail"]["error"] == "user_not_assigned_to_org"

    @pytest.mark.parametrize("path", ["/v1/chat/completions", "/model/x/invoke", "/openai/v1/responses"])
    async def test_indeterminate_membership_denies_on_paid_routes(self, path, monkeypatch):
        """Missing, unapproved, mismatched or indeterminate membership must all deny."""
        from contextlib import asynccontextmanager

        monkeypatch.setenv("BG_ENFORCE_ORG_ASSIGNMENT", "true")
        monkeypatch.delenv("BG_APPROVAL_FAIL_OPEN", raising=False)

        @asynccontextmanager
        async def _broken():
            raise RuntimeError("database unavailable")
            yield  # pragma: no cover

        reached, status, body = await self._run(path, _ctx(user_id="sub-unapproved", org_id=""), verdict_source=_broken)
        assert reached == [], f"{path}: an indeterminate approval lookup admitted a paid call"
        assert status == 503
        assert body["detail"]["error"] == "approval_check_unavailable"

    @pytest.mark.parametrize("path", ["/v1/chat/completions", "/model/x/invoke"])
    async def test_approved_caller_still_passes(self, path, monkeypatch, session_factory):  # noqa: F811 -- imported pytest fixture
        """A current database membership admits the middleware-only request."""
        monkeypatch.setenv("BG_ENFORCE_ORG_ASSIGNMENT", "true")
        reached, status, _ = await self._run(path, _ctx(user_id=APPROVED_SUB, org_id=""), verdict_source=session_factory)
        assert reached == [path]
        assert status == 200


class TestRecoveryPathsStayReachable:
    """A gate with no route back to approval is an outage — the #3984 class."""

    async def test_recovery_paths_are_not_in_the_enforced_registry(self):
        """Health, login and access-request must not be gated on being approved.

        Gating ``/access/request`` on approval is a deadlock by construction: the
        endpoint whose purpose is to ask for approval would require approval.
        """
        for path in RECOVERY_PATHS:
            assert not any(path.startswith(e) for e in ENFORCED_PATHS), f"{path} must stay reachable for an un-approved user"

    async def test_no_auth_or_access_path_is_enforced(self):
        """Broader sweep: nothing under /health, /auth or /access is gated."""
        gated = sorted(e for e in ENFORCED_PATHS if e.startswith(("/health", "/auth", "/access")))
        assert not gated, f"approval enforcement must never cover authentication or recovery paths: {gated}"

    async def test_unapproved_user_passes_through_on_recovery_paths(self, monkeypatch):
        """Asserted through the middleware, not just the registry."""
        from unittest.mock import MagicMock

        from src.auth.approval_middleware import ApprovalEnforcementMiddleware

        monkeypatch.setenv("BG_ENFORCE_ORG_ASSIGNMENT", "true")

        for path in RECOVERY_PATHS:
            reached: list[str] = []

            async def inner(scope, receive, send, _reached=reached):
                _reached.append(scope["path"])
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            mw = ApprovalEnforcementMiddleware(inner)
            # The DB must not even be consulted on a non-enforced path.
            mw._get_session = MagicMock(side_effect=AssertionError(f"{path} triggered an approval DB read"))  # type: ignore[method-assign]

            scope = {
                "type": "http",
                "path": path,
                "method": "GET",
                "headers": [],
                "state": {"token_context": _ctx(user_id="sub-unapproved", org_id="")},
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                pass

            await mw(scope, receive, send)
            assert reached == [path], f"{path} was blocked for an un-approved user, leaving them no route to approval"
