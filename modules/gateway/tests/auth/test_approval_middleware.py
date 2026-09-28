"""Tests for ApprovalEnforcementMiddleware (Issue #4144).

The story: a valid Cognito JWT used to be enough to bill Bedrock, regardless of
whether a platform admin had approved the caller. This middleware gates the
enforced spend paths on approval.

Every test asserts the OUTCOME — was the request admitted to the downstream app,
or was a 409 written — never the plumbing. The most important test in the file is
``test_blank_claim_but_users_row_is_allowed``: it is the regression guard for the
login-time auto-match approval path, which approves a user in Postgres but never
syncs their Cognito attributes, so they hold tokens with no ``org_id`` forever. A
claim-only gate would lock every such user out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.approval_middleware import ApprovalEnforcementMiddleware
from src.shared.config import Settings
from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, User
from src.shared.schemas.auth import TokenContext

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# A cognito_sub that IS approved in the DB fixture but carries no org_id claim —
# i.e. the auto-match approval path (src/admin/onboarding/handler.py).
APPROVED_SUB = "sub-approved-no-claim"
# A cognito_sub with no users row at all — never approved.
UNAPPROVED_SUB = "sub-never-approved"


def _ctx(
    *,
    user_id: str = UNAPPROVED_SUB,
    org_id: str = "",
    account_type: str = "human",
    is_admin: bool = False,
) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type=account_type,
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture(autouse=True)
def flag_on(monkeypatch):
    """Enforcement ON by default in this file; the flag-off test overrides it.

    Patched via the env var so it goes through the real Settings/env_prefix
    plumbing (BG_ENFORCE_ORG_ASSIGNMENT), which is what an operator flips.
    """
    monkeypatch.setenv("BG_ENFORCE_ORG_ASSIGNMENT", "true")


@pytest.fixture
async def session_factory():
    """A real SQLite session factory seeded with one approved, org-less-token user.

    Async fixture (pytest-asyncio ``asyncio_mode = "auto"``) so the aiosqlite
    connection lives on the same event loop as the test body, and the engine is
    disposed afterwards rather than being GC'd against a closed loop.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Organization(id="org-acme", name="Acme Corp"))
        session.add(Team(id="team-eng", name="Eng", org_id="org-acme", department_id="dept-eng"))
        session.add(
            User(
                id="db-user-1",
                org_id="org-acme",
                team_id="team-eng",
                email="approved@example.com",
                name="Approved User",
                cognito_sub=APPROVED_SUB,
            )
        )
        session.add(TenantMembership(user_id="db-user-1", tenant_id="org-acme", role="member", is_active=True, joined_via="onboarding_approval"))
        await session.commit()

    yield factory

    await engine.dispose()


class _Harness:
    """Drives a middleware instance over one ASGI request and records the outcome."""

    def __init__(self, middleware_factory, path: str = "/v1/chat/completions"):
        self.reached: list[str] = []
        self.sent: list[dict] = []
        self.path = path

        async def inner_app(scope, receive, send):
            self.reached.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        self.middleware = middleware_factory(inner_app)

    async def run(self, token_context: TokenContext | None, body: bytes = b'{"model":"x"}') -> None:
        state = {} if token_context is None else {"token_context": token_context}
        scope = {
            "type": "http",
            "path": self.path,
            "method": "POST",
            "headers": [],
            "state": state,
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            self.sent.append(message)

        await self.middleware(scope, receive, send)

    @property
    def admitted(self) -> bool:
        return self.reached == [self.path]

    @property
    def status(self) -> int | None:
        for msg in self.sent:
            if msg["type"] == "http.response.start":
                return msg["status"]
        return None

    @property
    def json_body(self) -> dict:
        for msg in self.sent:
            if msg["type"] == "http.response.body":
                return json.loads(msg["body"])
        raise AssertionError("no response body was sent")

    @property
    def headers(self) -> dict[str, str]:
        """Response headers, lowercased — #5666 (A11) asserts Retry-After."""
        for msg in self.sent:
            if msg["type"] == "http.response.start":
                return {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
        return {}


def _with_session_factory(factory):
    """Build a middleware whose DB session comes from the given factory."""

    def _factory(app):
        mw = ApprovalEnforcementMiddleware(app)
        mw._get_session = lambda: factory()  # type: ignore[method-assign]
        return mw

    return _factory


def _with_failing_session(exc: Exception):
    def _factory(app):
        mw = ApprovalEnforcementMiddleware(app)

        def _boom():
            raise exc

        mw._get_session = _boom  # type: ignore[method-assign]
        return mw

    return _factory


class TestFlagOff:
    async def test_flag_off_admits_regardless_of_org_state(self, monkeypatch, session_factory):
        """The default ships inert: no gating at all when the flag is off."""
        monkeypatch.setenv("BG_ENFORCE_ORG_ASSIGNMENT", "false")
        h = _Harness(_with_session_factory(session_factory))
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert h.admitted, "flag off must admit an un-approved human"
        assert h.status == 200

    async def test_flag_defaults_to_enforcing(self, monkeypatch):
        """#5666 (A11): the shipping default is now ON.

        This test previously asserted ``is False`` with the rationale "this is what
        makes merge safe". Merge-safe and deploy-safe are different properties: a
        default of off meant the only SERVER-SIDE proof of approval on the paid
        paths was inert in every environment that had not been hand-flipped, so the
        documented protection was not the shipped behaviour. A security control
        whose default is off is a control nobody has.
        """
        monkeypatch.delenv("BG_ENFORCE_ORG_ASSIGNMENT", raising=False)
        assert Settings().enforce_org_assignment is True

    async def test_configmap_ships_enforcement_on(self):
        """The Python default is not what runs in the cluster — the ConfigMap is.

        Both must agree, or flipping only the dataclass default leaves the deployed
        pods with the old permissive value and the fix is cosmetic.
        """
        from pathlib import Path

        configmap = Path(__file__).resolve().parents[2] / "k8s" / "configmap.yaml"
        text = configmap.read_text()
        assert 'BG_ENFORCE_ORG_ASSIGNMENT: "true"' in text, (
            "k8s/configmap.yaml must ship approval enforcement ON; a 'false' here disables the gate "
            "in the cluster no matter what the Settings default says"
        )
        assert 'BG_APPROVAL_FAIL_OPEN: "false"' in text, "the fail-open break-glass must ship disabled"

    async def test_flag_is_configurable_from_the_environment(self, monkeypatch):
        """The documented rollback (env flip + pod recycle) must actually work."""
        monkeypatch.setenv("BG_ENFORCE_ORG_ASSIGNMENT", "true")
        assert Settings().enforce_org_assignment is True


class TestApprovalGate:
    async def test_unapproved_human_is_blocked_with_409(self, session_factory):
        """Human, blank claim, no users row → 409 in the org_id_resolver shape."""
        h = _Harness(_with_session_factory(session_factory))
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert not h.admitted, "an un-approved human must never reach the proxy"
        assert h.status == 409
        assert h.json_body == {
            "detail": {
                "error": "user_not_assigned_to_org",
                "message": "Your account is pending approval. Ask a platform admin to approve your access.",
            }
        }

    async def test_blank_claim_but_users_row_is_allowed(self, session_factory):
        """THE regression test for the auto-match approval path.

        ``attach_approved_member(..., sync_cognito_claims=False)`` approves the
        user in Postgres but never writes custom:org_id, and the
        pre-token-generation Lambda reads Cognito attributes rather than Postgres.
        So a fully approved user's tokens are permanently org-less. Keying the
        gate on the claim would lock all of them out — the single
        highest-probability failure mode in this story.
        """
        h = _Harness(_with_session_factory(session_factory))
        await h.run(_ctx(user_id=APPROVED_SUB, org_id=""))
        assert h.admitted, "an approved user with an org-less token must be admitted via the DB fallback"
        assert h.status == 200

    async def test_populated_claim_requires_current_membership(self, session_factory):
        h = _Harness(_with_session_factory(session_factory))
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id="org-acme"))
        assert not h.admitted
        assert h.status == 409

    async def test_whitespace_only_claim_falls_through_to_the_db(self, session_factory):
        """A claim of spaces is not an org assignment."""
        h = _Harness(_with_session_factory(session_factory))
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id="   "))
        assert h.status == 409


class TestExemptions:
    async def test_platform_admin_with_no_org_is_allowed(self, session_factory):
        """#3984 self-lockout guard: the admin who approves everyone must get in.

        ``is_admin`` is derived from role/groups independently of org_id, so an
        admin with no org and no users row still passes.
        """
        h = _Harness(_with_session_factory(session_factory))
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id="", is_admin=True))
        assert h.admitted, "a platform admin must never be gated"
        assert h.status == 200

    async def test_service_account_requires_registered_authority(self, session_factory):
        h = _Harness(_with_session_factory(session_factory))
        await h.run(_ctx(user_id="agent-client-id", org_id="org-acme", account_type="service"))
        assert not h.admitted
        assert h.status == 409

    async def test_registered_iam_agent_is_allowed(self, session_factory):
        context = _ctx(user_id="registered-agent", org_id="org-acme", account_type="service")
        context.auth_source = "iam"
        context.agent_registry_id = "registry-row-id"
        h = _Harness(_with_session_factory(session_factory))
        await h.run(context)
        assert h.admitted

    async def test_unstamped_account_type_is_treated_as_human(self, session_factory):
        """Fail-safe direction: an unstamped caller is gated, not waved through.

        ``account_type`` defaults to "human" when the claim is absent
        (src/auth/middleware.py), so the gate applies.
        """
        h = _Harness(_with_session_factory(session_factory))
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id="", account_type="human"))
        assert h.status == 409


class TestPassthrough:
    async def test_missing_token_context_passes_through(self, session_factory):
        """Unauthenticated → the route returns 401, not a misleading 409."""
        h = _Harness(_with_session_factory(session_factory))
        await h.run(None)
        assert h.admitted, "no token_context must pass through so the route can 401"
        assert h.status == 200

    @pytest.mark.parametrize("path", ["/v1/models", "/v1/health", "/access/status", "/access/request", "/admin/users"])
    async def test_non_enforced_paths_are_untouched(self, path, session_factory):
        """An un-approved user must still be able to check status and request access."""
        h = _Harness(_with_session_factory(session_factory), path=path)
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert h.admitted, f"{path} is not a spend path and must not be gated"
        assert h.status == 200

    async def test_count_tokens_is_prefix_matched_by_v1_messages(self, session_factory):
        """Documents intentional incidental coverage — do NOT "fix" the prefix match.

        ``/v1/messages/count_tokens`` performs no Bedrock call, so gating it is
        harmless; it is covered because ENFORCED_PATHS matching is ``startswith``.
        """
        h = _Harness(_with_session_factory(session_factory), path="/v1/messages/count_tokens")
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert h.status == 409

    async def test_non_http_scope_passes_through(self):
        """Websocket/lifespan scopes must not be inspected."""
        reached = []

        async def inner_app(scope, receive, send):
            reached.append(scope["type"])

        mw = ApprovalEnforcementMiddleware(inner_app)
        await mw({"type": "lifespan"}, AsyncMock(), AsyncMock())
        assert reached == ["lifespan"]

    async def test_documented_scope_boundary_knowledge_ingestion_is_not_gated(self, session_factory):
        """Knowledge-ingestion routes are OUT OF SCOPE for this story — by design.

        ``POST /api/agent-context/assets`` (and reindex / bulk commit) trigger real
        Bedrock spend asynchronously via SQS → ingestion worker → LiteLLM. They
        authenticate through a route dependency (``get_current_user``), NOT the
        ENFORCED_PATHS middleware, so this middleware provably does not reach
        them. This test pins that boundary so the gap stays a visible, tracked
        scope decision rather than being mistaken for coverage.

        Tracked separately as issue #4149, "Approval gate for indirect
        (knowledge-ingestion) Bedrock spend". The fix there must be a route-level
        dependency (or an extension of dispatch_ingestion) — NOT an
        ENFORCED_PATHS addition, since that tuple also drives budget and
        rate-limit enforcement, which assume a token-metered LLM request/response
        shape. When #4149 lands, UPDATE this test to assert these routes ARE
        gated; do not delete it.
        """
        for path in (
            "/api/agent-context/assets",
            "/api/agent-context/assets/asset-1/reindex",
            "/api/agent-context/assets/bulk/commit",
        ):
            h = _Harness(_with_session_factory(session_factory), path=path)
            await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
            assert h.admitted, f"{path} is not gated by this middleware (documented scope boundary)"


class TestFailClosed:
    """#5666 (A11): an indeterminate approval lookup must DENY, not admit.

    This class previously asserted the opposite. The fail-open reasoning was not
    wrong about the outage risk it named — it was wrong about the remedy. Admitting
    unproven callers means the one fault an attacker can most easily induce
    (database pressure) is also the fault that disables the gate on the paid paths:
    "cannot prove approved" was being treated as "approved".

    The #4075 budget precedent is applied instead — separate the POLICY answer from
    the AVAILABILITY answer, so failing closed does not require a terminal error:
    409 for proven-not-approved, retryable 503 for indeterminate. The outage the old
    comment feared is further bounded by the exemption ORDER: admins, agents and
    humans with a populated org claim never reach the DB read at all.
    """

    async def test_db_error_denies_the_request(self, session_factory):
        """The core inversion of this finding."""
        exc = OperationalError("SELECT 1", {}, Exception("PAM authentication failed"))
        h = _Harness(_with_failing_session(exc))
        with patch("src.auth.approval_middleware.ApprovalEnforcementMiddleware._emit_metric") as emit:
            await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert not h.admitted, (
            "an indeterminate approval check must NOT reach the paid provider — a DB fault is the "
            "easiest condition to induce, so admitting on it makes the gate optional"
        )
        assert h.status == 503
        emit.assert_any_call("ApprovalCheckFailedFailClosed")

    async def test_indeterminate_is_503_not_409(self, session_factory):
        """The two denials must be distinguishable by clients AND by operators.

        A 409 would tell this caller to go find an admin (wrong — they may be
        approved) and would spike the entitlement-denial rate on a dashboard during
        a database incident, sending someone to hunt a phantom approvals bug.
        """
        h = _Harness(_with_failing_session(OperationalError("SELECT 1", {}, Exception("boom"))))
        with patch.object(ApprovalEnforcementMiddleware, "_emit_metric"):
            await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert h.status == 503
        assert h.json_body == {
            "detail": {
                "error": "approval_check_unavailable",
                "message": "Unable to verify your access approval right now. Please retry shortly.",
            }
        }
        assert h.json_body["detail"]["error"] != "user_not_assigned_to_org"

    async def test_503_is_retryable(self, session_factory):
        """Retry-After is what lets clients recover without operator action."""
        h = _Harness(_with_failing_session(OperationalError("SELECT 1", {}, Exception("boom"))))
        with patch.object(ApprovalEnforcementMiddleware, "_emit_metric"):
            await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert h.headers.get("retry-after") == "2"

    async def test_db_error_logs_a_greppable_error(self, session_factory, caplog):
        """An operator must be able to find fail-closed denials in CloudWatch."""
        exc = OperationalError("SELECT 1", {}, Exception("boom"))
        h = _Harness(_with_failing_session(exc))
        with caplog.at_level("WARNING"), patch.object(ApprovalEnforcementMiddleware, "_emit_metric"):
            await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert "approval_check_failed_fail_closed" in caplog.text

    async def test_outage_preserves_recovery_but_does_not_trust_org_claims(self, session_factory):
        exc = OperationalError("SELECT 1", {}, Exception("db is down"))
        for ctx, status in (
            (_ctx(user_id="sub-admin", org_id="", is_admin=True), 200),
            (_ctx(user_id="sub-agent", org_id="org-acme", account_type="service"), 409),
            (_ctx(user_id=APPROVED_SUB, org_id="org-acme"), 503),
        ):
            h = _Harness(_with_failing_session(exc))
            with patch.object(ApprovalEnforcementMiddleware, "_emit_metric"):
                await h.run(ctx)
            assert h.status == status

    async def test_break_glass_restores_admission_explicitly(self, session_factory, monkeypatch):
        """An operator can still choose availability — consciously, and visibly."""
        monkeypatch.setenv("BG_APPROVAL_FAIL_OPEN", "true")
        exc = OperationalError("SELECT 1", {}, Exception("boom"))
        h = _Harness(_with_failing_session(exc))
        with patch("src.auth.approval_middleware.ApprovalEnforcementMiddleware._emit_metric") as emit:
            await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert h.admitted, "BG_APPROVAL_FAIL_OPEN=true must restore the pre-#5666 behaviour"
        emit.assert_any_call("ApprovalCheckFailedFailOpen")

    async def test_break_glass_defaults_off(self, monkeypatch):
        monkeypatch.delenv("BG_APPROVAL_FAIL_OPEN", raising=False)
        assert Settings().approval_fail_open is False, "the break-glass must be opt-in"

    async def test_break_glass_is_loudly_logged_while_active(self, session_factory, monkeypatch, caplog):
        """It must be impossible to leave this on unnoticed."""
        monkeypatch.setenv("BG_APPROVAL_FAIL_OPEN", "true")
        h = _Harness(_with_failing_session(OperationalError("SELECT 1", {}, Exception("boom"))))
        with caplog.at_level("WARNING"), patch.object(ApprovalEnforcementMiddleware, "_emit_metric"):
            await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert "BG_APPROVAL_FAIL_OPEN=true" in caplog.text

    async def test_metric_failure_never_breaks_a_request(self, session_factory):
        """Metrics are best-effort — a CloudWatch outage must not 500 the gate."""
        h = _Harness(_with_session_factory(session_factory))
        with patch("src.admin.cognito_claims.emit_metric", side_effect=RuntimeError("cw down")):
            await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert h.status == 409


class TestAsgiResponseIntegrity:
    def test_409_reaches_a_real_client_with_a_non_trivial_body(self):
        """The request must not hang: the body is drained before the 409 is sent.

        This is the whole reason the middleware is pure ASGI rather than
        BaseHTTPMiddleware (returning a response from a BaseHTTPMiddleware
        dispatch() without calling call_next() hangs indefinitely), and why the
        drain loop exists. Asserted end-to-end through a real TestClient posting a
        non-trivial JSON body — if either detail were wrong this test would time
        out rather than fail.

        Sync test on purpose: TestClient drives its own event loop, so it must not
        be nested inside a pytest-asyncio one. The DB is stubbed rather than using
        the ``session_factory`` fixture for the same reason.
        """
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def _completions():  # pragma: no cover - must never be reached
            return {"ok": True}

        app.add_middleware(_unapproved_caller_middleware())

        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": "anthropic.claude-3-5-sonnet", "messages": [{"role": "user", "content": "x" * 5000}]},
            )

        assert response.status_code == 409
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {
            "detail": {
                "error": "user_not_assigned_to_org",
                "message": "Your account is pending approval. Ask a platform admin to approve your access.",
            }
        }

    async def test_client_disconnect_during_drain_sends_no_response(self, session_factory):
        """If the client vanishes mid-drain we return silently rather than writing."""
        sent: list = []

        async def inner_app(scope, receive, send):  # pragma: no cover - must not be reached
            raise AssertionError("un-approved request must not reach the app")

        mw = ApprovalEnforcementMiddleware(inner_app)
        mw._get_session = lambda: session_factory()  # type: ignore[method-assign]

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "path": "/v1/chat/completions",
            "method": "POST",
            "headers": [],
            "state": {"token_context": _ctx(user_id=UNAPPROVED_SUB, org_id="")},
        }
        await mw(scope, receive, send)
        assert sent == []

    async def test_multi_chunk_body_is_fully_drained(self, session_factory):
        """A streamed body must be consumed to completion before responding."""
        chunks = [
            {"type": "http.request", "body": b'{"a":1,', "more_body": True},
            {"type": "http.request", "body": b'"b":2}', "more_body": False},
        ]
        consumed: list = []

        async def inner_app(scope, receive, send):  # pragma: no cover
            raise AssertionError("un-approved request must not reach the app")

        mw = ApprovalEnforcementMiddleware(inner_app)
        mw._get_session = lambda: session_factory()  # type: ignore[method-assign]

        async def receive():
            msg = chunks.pop(0)
            consumed.append(msg)
            return msg

        sent: list = []

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "path": "/v1/chat/completions",
            "method": "POST",
            "headers": [],
            "state": {"token_context": _ctx(user_id=UNAPPROVED_SUB, org_id="")},
        }
        await mw(scope, receive, send)
        assert len(consumed) == 2, "the drain loop must follow more_body to the end"
        assert sent[0]["status"] == 409


class TestEveryEnforcedPathIsGated:
    @pytest.mark.parametrize("path", ENFORCED_PATHS)
    async def test_unapproved_human_blocked_on_every_enforced_path(self, path, session_factory):
        """No spend route may miss the gate (the #2792 / #2809 class of hole)."""
        request_path = f"{path}some-model/invoke" if path.endswith("/") else path
        h = _Harness(_with_session_factory(session_factory), path=request_path)
        await h.run(_ctx(user_id=UNAPPROVED_SUB, org_id=""))
        assert h.status == 409, f"{request_path} must be gated"

    @pytest.mark.parametrize("path", ENFORCED_PATHS)
    async def test_approved_human_admitted_on_every_enforced_path(self, path, session_factory):
        """Regression: enforcement must not block healthy, approved traffic."""
        request_path = f"{path}some-model/invoke" if path.endswith("/") else path
        h = _Harness(_with_session_factory(session_factory), path=request_path)
        await h.run(_ctx(user_id=APPROVED_SUB, org_id=""))
        assert h.admitted, f"approved traffic must still reach the app on {request_path}"


class TestMiddlewareOrdering:
    """The approval gate must sit between token-context and budget/rate-limit.

    Middleware executes in reverse order of addition, and ``src/app.py`` adds
    rate-limit → budget → approval → token-context. So at runtime the chain is
    token-context → approval → budget → rate-limit.
    """

    async def test_unapproved_request_is_rejected_before_the_budget_read(self, session_factory):
        """Denying before the ledger read avoids a DB round-trip on a doomed request."""
        budget_checked: list[str] = []

        async def budget_stage(scope, receive, send):  # pragma: no cover - must not run
            budget_checked.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = ApprovalEnforcementMiddleware(budget_stage)
        mw._get_session = lambda: session_factory()  # type: ignore[method-assign]

        sent: list = []

        async def receive():
            return {"type": "http.request", "body": b'{"model":"x"}', "more_body": False}

        async def send(message):
            sent.append(message)

        await mw(
            {
                "type": "http",
                "path": "/v1/chat/completions",
                "method": "POST",
                "headers": [],
                "state": {"token_context": _ctx(user_id=UNAPPROVED_SUB, org_id="")},
            },
            receive,
            send,
        )

        assert budget_checked == [], "the budget/ledger read must never happen for a denied caller"
        assert sent[0]["status"] == 409

    async def test_approved_request_reaches_the_budget_stage(self, session_factory):
        """Approved traffic must still flow into budget + rate-limit enforcement."""
        budget_checked: list[str] = []

        async def budget_stage(scope, receive, send):
            budget_checked.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = ApprovalEnforcementMiddleware(budget_stage)
        mw._get_session = lambda: session_factory()  # type: ignore[method-assign]

        async def receive():
            return {"type": "http.request", "body": b'{"model":"x"}', "more_body": False}

        await mw(
            {
                "type": "http",
                "path": "/v1/chat/completions",
                "method": "POST",
                "headers": [],
                "state": {"token_context": _ctx(user_id=APPROVED_SUB, org_id="")},
            },
            receive,
            AsyncMock(),
        )

        assert budget_checked == ["/v1/chat/completions"], "approved traffic must still be budget-enforced"

    def test_app_registers_approval_between_budget_and_token_context(self):
        """Pin the registration slot — ordering here IS the correctness property.

        If approval were added after TokenContextMiddleware it would run before
        it, see no token_context, and pass every request through: a silently
        inert gate. If it were added before budget it would run after the ledger
        read, wasting a DB round-trip on denied requests.
        """
        from src.app import create_app
        from src.auth.middleware import TokenContextMiddleware
        from src.budget.enforcement_middleware import BudgetEnforcementMiddleware

        # Starlette inserts each add_middleware() at the FRONT of user_middleware,
        # so this list reads in EXECUTION order: index 0 is the outermost
        # middleware and runs first.
        classes = [m.cls for m in create_app().user_middleware]
        approval_idx = classes.index(ApprovalEnforcementMiddleware)
        token_idx = classes.index(TokenContextMiddleware)
        budget_idx = classes.index(BudgetEnforcementMiddleware)

        assert token_idx < approval_idx < budget_idx, (
            "approval must run AFTER TokenContextMiddleware (so token_context is populated) "
            f"and BEFORE BudgetEnforcementMiddleware (so denials skip the ledger read); got {[c.__name__ for c in classes]}"
        )


def _unapproved_caller_middleware():
    """Middleware subclass that presents an un-approved human with no DB row.

    Used by the TestClient test, where there is no TokenContextMiddleware to
    populate ``scope["state"]``. ``_db_has_org`` is stubbed False rather than
    wired to a real engine so the test stays on TestClient's own event loop.
    """

    class _Wrapper(ApprovalEnforcementMiddleware):
        async def _db_has_org(self, user_id: str, *, context=None) -> bool:
            return False

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                scope.setdefault("state", {})["token_context"] = _ctx(user_id=UNAPPROVED_SUB, org_id="")
            await super().__call__(scope, receive, send)

    return _Wrapper


async def test_selected_second_membership_is_current_even_when_not_active(session_factory):
    async with session_factory() as session:
        session.add(Organization(id="org-second", name="Second"))
        session.add(TenantMembership(user_id="db-user-1", tenant_id="org-second", role="member", is_active=False, joined_via="onboarding_approval"))
        await session.commit()
    h = _Harness(_with_session_factory(session_factory))
    await h.run(_ctx(user_id=APPROVED_SUB, org_id="org-second"))
    assert h.status == 200


async def test_multiple_memberships_without_selected_tenant_are_policy_denial(session_factory):
    async with session_factory() as session:
        session.add(Organization(id="org-second", name="Second"))
        session.add(TenantMembership(user_id="db-user-1", tenant_id="org-second", role="member", is_active=False, joined_via="onboarding_approval"))
        await session.commit()
    h = _Harness(_with_session_factory(session_factory))
    await h.run(_ctx(user_id=APPROVED_SUB))
    assert h.status == 409


async def test_deleted_membership_cannot_be_replaced_by_stale_org_claim(session_factory):
    from sqlalchemy import delete

    async with session_factory() as session:
        await session.execute(delete(TenantMembership).where(TenantMembership.user_id == "db-user-1"))
        await session.commit()
    h = _Harness(_with_session_factory(session_factory))
    await h.run(_ctx(user_id=APPROVED_SUB, org_id="org-acme"))
    assert h.status == 409
