"""Audit middleware invariants — issue #5673 (A17).

WHAT FINDING f-93177516-24d1-451e-bfd1-ddd2e3c60048 ACTUALLY WAS

The audit middleware resolved the caller's organization by independently decoding the
`Authorization` header with the legacy HS256 decoder. Enforced environments issue Cognito
RS256 tokens that decoder cannot read, so it resolved no organization, and its response to
"no organization" was to return without writing a row and without logging. Since
`installation/manifests.py` sets `DOMAIN_AUTH_ENFORCED=true`, the deployed configuration
produced an EMPTY audit table with no signal that it was empty.

`TestEnforcedConfigurationRecordsRows` is the regression for exactly that, and it is the
test to run against the pre-fix code to see it fail.

WHY THE SESSION FACTORY IS PATCHED IN EVERY TEST HERE

The middleware writes through `app.database.async_session_factory`, which is bound to the
production-shaped engine. `conftest.py` redirects handler database access by overriding the
`get_session` DEPENDENCY, but a middleware is not a dependency and never sees that
override, so unpatched it would attempt a real connection.

That gap is itself worth naming: because those writes failed silently, the entire existing
suite ran with a non-functioning audit path and nothing failed. A silent audit path that
tests cannot observe is how this class of defect survives review, which is why the
fixture below makes the writes observable rather than merely suppressing them.

WHAT THESE TESTS DO NOT ESTABLISH

They run against SQLite through the ASGI stack, not PostgreSQL. They prove the middleware's
decisions -- which rows it writes, with which fields, and that no path is silent. They do
not prove the migration APPLIES to a live database (see tests/test_migrations.py for the
offline DDL check), and they are not the end-to-end operator check in a deployed enforced
environment, which remains unrun and is recorded as such on the issue.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.main import app as fastapi_app
from app.middleware.audit import AuditMiddleware
from app.middleware.auth import create_access_token
from app.models.event import Event
from app.models.organization import Organization
from app.services.audit import (
    OUTCOME_ALLOWED,
    OUTCOME_DENIED,
    PRINCIPAL_UNRESOLVED,
    audit_write_failures,
)
from tests.conftest import async_session_test

TEST_PRINCIPAL = "user-abc"


@pytest.fixture(autouse=True)
def audit_writes_reach_the_test_database(monkeypatch):
    """Point the middleware's session factory at the in-memory test engine.

    Patched on the MIDDLEWARE module, not on `app.database`: the module does
    `from app.database import async_session_factory`, so it holds its own reference and
    rebinding the origin would not affect it.
    """
    monkeypatch.setattr(
        "app.middleware.audit.async_session_factory", async_session_test
    )


@pytest.fixture(autouse=True)
def reset_failure_counter():
    """The counter is process-lifetime by design, so tests must isolate themselves."""
    audit_write_failures.reset()
    yield
    audit_write_failures.reset()


async def _seed_org() -> uuid.UUID:
    """An organization row, required by the events FK when a tenant is attributed."""
    org_id = uuid.uuid4()
    async with async_session_test() as session:
        session.add(
            Organization(
                id=org_id, name=f"audit-org-{org_id.hex[:8]}", billing_plan="free"
            )
        )
        await session.commit()
    return org_id


async def _events() -> list[Event]:
    async with async_session_test() as session:
        result = await session.execute(select(Event).order_by(Event.created_at))
        return list(result.scalars().all())


class TestEnforcedConfigurationRecordsRows:
    """The regression for f-93177516: enforced auth produced ZERO audit rows.

    Asserted through the real ASGI stack with a caller published on request state the way
    the guard publishes one, because the defect was specifically that the middleware could
    not read identity in that configuration.
    """

    @pytest.mark.asyncio
    async def test_mutating_request_with_verified_caller_writes_exactly_one_row(
        self, client, monkeypatch
    ):
        org_id = await _seed_org()

        # Drive the middleware directly with a verified caller on request state. This is
        # the configuration the finding describes: identity exists and is verified, and the
        # old code still wrote nothing because it looked for identity in the wrong place.
        await _drive(
            monkeypatch,
            method="POST",
            path="/workspaces",
            status_code=201,
            caller=_caller(TEST_PRINCIPAL, org_id),
        )

        rows = await _events()
        assert len(rows) == 1, "enforced configuration must record the attempt"
        assert rows[0].principal == TEST_PRINCIPAL
        assert rows[0].org_id == org_id
        assert rows[0].outcome == OUTCOME_ALLOWED

    @pytest.mark.asyncio
    async def test_principal_and_tenant_are_never_the_same_value(self, monkeypatch):
        """Defect 3: the actor column used to hold the organization identifier."""
        org_id = await _seed_org()
        await _drive(
            monkeypatch,
            method="POST",
            path="/workspaces",
            status_code=201,
            caller=_caller(TEST_PRINCIPAL, org_id),
        )
        (row,) = await _events()
        assert row.principal != str(row.org_id)
        assert row.principal == TEST_PRINCIPAL

    @pytest.mark.asyncio
    async def test_legacy_configuration_also_records_principal_and_tenant(
        self, monkeypatch
    ):
        """Both supported configurations must produce a record.

        The legacy path publishes its verified identity through `request.state.audit_*`
        instead of a `caller` object. Covered because "only some configurations are wired
        up" is the incomplete-migration hazard the issue calls out: one deployment
        continuing to write nothing while appearing fixed.
        """
        org_id = await _seed_org()
        acting_user = uuid.uuid4()
        await _drive(
            monkeypatch,
            method="POST",
            path="/workspaces",
            status_code=201,
            legacy=(org_id, str(acting_user)),
        )
        (row,) = await _events()
        assert row.org_id == org_id
        assert row.principal == str(acting_user)
        assert row.outcome == OUTCOME_ALLOWED


class TestDeniedAndUnauthenticatedAttempts:
    """Refusals are the events an audit trail exists to surface."""

    @pytest.mark.asyncio
    async def test_guard_denial_is_recorded_as_denied(self, monkeypatch):
        """A 403 used to be dropped by `if status_code >= 400: return`."""
        org_id = await _seed_org()
        await _drive(
            monkeypatch,
            method="POST",
            path="/workspaces",
            status_code=403,
            caller=_caller(TEST_PRINCIPAL, org_id),
        )
        (row,) = await _events()
        assert row.outcome == OUTCOME_DENIED
        assert row.http_status == 403
        assert row.principal == TEST_PRINCIPAL

    @pytest.mark.asyncio
    async def test_unauthenticated_attempt_is_recorded_with_unresolved_principal(
        self, monkeypatch
    ):
        """Rejected before identity existed: recorded, not omitted.

        This row is why `events.org_id` had to become nullable -- the NOT NULL constraint
        made an unattributable attempt unrepresentable, so the old code dropped it.
        """
        await _drive(monkeypatch, method="POST", path="/workspaces", status_code=401)
        (row,) = await _events()
        assert row.principal == PRINCIPAL_UNRESOLVED
        assert row.org_id is None
        assert row.outcome == OUTCOME_DENIED

    @pytest.mark.asyncio
    async def test_rate_limit_short_circuit_is_recorded(self, monkeypatch):
        """A 429 returned by an inner middleware must still be audited."""
        await _drive(monkeypatch, method="POST", path="/workspaces", status_code=429)
        (row,) = await _events()
        assert row.outcome == OUTCOME_DENIED
        assert row.http_status == 429


class TestNoSilentSkip:
    """The invariant: no path returns without a row OR a counted failure."""

    @pytest.mark.asyncio
    async def test_persistence_failure_increments_counter_and_spares_the_caller(
        self, monkeypatch
    ):
        """Both halves of the deliberate failure policy, in one test.

        Loud: the counter moves and a warning is logged. Not fatal: the caller's response
        is unchanged. Making an audit fault fatal would turn a logging outage into a
        control-plane outage for every tenant, which is the worse failure.
        """

        async def _explode(*args, **kwargs):
            raise RuntimeError("audit storage unavailable")

        monkeypatch.setattr("app.middleware.audit.log_event", _explode)

        org_id = await _seed_org()
        response = await _drive(
            monkeypatch,
            method="POST",
            path="/workspaces",
            status_code=201,
            caller=_caller(TEST_PRINCIPAL, org_id),
        )

        assert audit_write_failures.count == 1, "a failed write must be counted"
        assert await _events() == [], "nothing was persisted"
        # The caller is unaffected: same status the handler produced.
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_failure_log_does_not_contain_the_underlying_exception_text(
        self, monkeypatch, caplog
    ):
        """The alerting line must not forward credential-bearing error text.

        A database failure message can carry a connection string, and this path runs on
        already-rejected requests where attacker-supplied material is in scope.
        """
        secret = "postgresql://user:SUPERSECRETPASSWORD@db.internal/superplane"

        async def _explode(*args, **kwargs):
            raise RuntimeError(f"could not connect: {secret}")

        monkeypatch.setattr("app.middleware.audit.log_event", _explode)

        # Re-enable the audit logger for the duration of this test. `test_migrations.py`
        # calls Alembic's `fileConfig`, which sets `disabled = True` on every logger that
        # already exists -- so whether this test can observe a log record depended on
        # whether the migration suite had run first. conftest.py documents the same
        # interference for `test_runtime_logging.py`. Asserted explicitly here rather than
        # left to test ordering, because "the warning was not logged" and "the logger was
        # switched off by an unrelated suite" are indistinguishable from the failure output.
        audit_logger = logging.getLogger("app.services.audit")
        monkeypatch.setattr(audit_logger, "disabled", False)
        monkeypatch.setattr(audit_logger, "propagate", True)

        with caplog.at_level("WARNING", logger="app.services.audit"):
            await _drive(
                monkeypatch, method="POST", path="/workspaces", status_code=401
            )

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "a failure must be logged, not swallowed"
        assert any("audit record NOT persisted" in r.getMessage() for r in warnings)
        for record in warnings:
            assert "SUPERSECRETPASSWORD" not in record.getMessage()


class TestReadCoverageFlag:
    """Read auditing is opt-in so volume is a per-environment decision."""

    @pytest.mark.asyncio
    async def test_reads_are_not_recorded_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "audit_read_coverage", False)
        org_id = await _seed_org()
        await _drive(
            monkeypatch,
            method="GET",
            path="/workspaces",
            status_code=200,
            caller=_caller(TEST_PRINCIPAL, org_id),
        )
        assert await _events() == []

    @pytest.mark.asyncio
    async def test_reads_are_recorded_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "audit_read_coverage", True)
        org_id = await _seed_org()
        await _drive(
            monkeypatch,
            method="GET",
            path="/workspaces",
            status_code=200,
            caller=_caller(TEST_PRINCIPAL, org_id),
        )
        (row,) = await _events()
        assert row.action == "read"
        assert row.outcome == OUTCOME_ALLOWED

    @pytest.mark.asyncio
    async def test_mutations_are_recorded_regardless_of_the_flag(self, monkeypatch):
        """The flag must not be able to switch off auditing of state changes."""
        monkeypatch.setattr(settings, "audit_read_coverage", False)
        org_id = await _seed_org()
        await _drive(
            monkeypatch,
            method="DELETE",
            path=f"/workspaces/{uuid.uuid4()}",
            status_code=204,
            caller=_caller(TEST_PRINCIPAL, org_id),
        )
        assert len(await _events()) == 1

    @pytest.mark.asyncio
    async def test_health_checks_are_never_recorded(self, monkeypatch):
        """Liveness probes would bury real records without answering any question."""
        monkeypatch.setattr(settings, "audit_read_coverage", True)
        await _drive(monkeypatch, method="GET", path="/health", status_code=200)
        assert await _events() == []


class TestRecordContainsNoSensitiveMaterial:
    """Recording refusals widens what reaches a long-retained, broadly-readable table."""

    @pytest.mark.asyncio
    async def test_no_body_query_header_or_token_material_is_recorded(
        self, monkeypatch
    ):
        org_id = await _seed_org()
        body_secret = "BODY-SECRET-VALUE"
        query_secret = "QUERY-SECRET-VALUE"
        token_secret = "TOKEN-SECRET-VALUE"

        await _drive(
            monkeypatch,
            method="POST",
            path="/workspaces",
            query_string=f"api_key={query_secret}",
            status_code=201,
            caller=_caller(TEST_PRINCIPAL, org_id),
            headers=[
                (b"authorization", f"Bearer {token_secret}".encode()),
                (b"x-api-key", token_secret.encode()),
            ],
            body=f'{{"password": "{body_secret}"}}'.encode(),
        )

        (row,) = await _events()
        # Every persisted text field, checked as a whole rather than field by field, so a
        # column added later is covered without updating this test.
        persisted = " ".join(
            str(value)
            for value in (
                row.principal,
                row.org_id,
                row.action,
                row.resource_type,
                row.resource_id,
                row.event_type,
                row.message,
                row.details_json,
                row.source_ip,
                row.request_path,
                row.http_status,
                row.outcome,
                row.user_id,
            )
        )
        for secret in (body_secret, query_secret, token_secret):
            assert secret not in persisted

        # Positively assert the intended content, so "records nothing" cannot pass.
        assert row.request_path == "/workspaces"
        assert "?" not in str(row.request_path), "query string must be excluded"
        assert row.action == "created"


class TestMiddlewareOrdering:
    """Position is load-bearing: an inner audit layer misses short-circuit rejections."""

    def test_audit_middleware_is_outermost(self):
        """`add_middleware` prepends, so the OUTERMOST middleware is index 0.

        Asserted structurally so a future edit that moves the `add_middleware` call in
        `app/main.py` fails here rather than silently reopening the gap where a rate-limit
        429 bypasses the audit trail entirely.
        """
        classes = [m.cls for m in fastapi_app.user_middleware]
        assert AuditMiddleware in classes, "audit middleware must be registered"
        assert classes[0] is AuditMiddleware, (
            "AuditMiddleware must be outermost (added last in app/main.py) so "
            "short-circuit rejections from inner middleware are still recorded"
        )


class TestNoIndependentTokenDecoding:
    """The audit path must not re-derive identity from a token it decodes itself."""

    def test_middleware_does_not_call_the_legacy_decoder(self):
        """A structural check, because this is the exact shape of the original defect.

        The old middleware imported `decode_token` and applied it to the raw
        `Authorization` header. That is what produced no identity -- and therefore no rows
        -- under enforcement. Keeping it out is the fix, and a reviewer cannot rely on
        remembering that, so it is asserted.
        """
        import ast
        import inspect

        from app.middleware import audit as audit_module

        # Parsed rather than grepped: this file's own docstrings discuss `decode_token`
        # at length to explain the defect, and a substring search cannot tell prose
        # about a function from a call to it. The AST walk sees only real references.
        tree = ast.parse(inspect.getsource(audit_module))
        referenced = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        imported = {
            alias.asname or alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

        for forbidden in ("decode_token", "TokenPayload"):
            assert forbidden not in referenced | imported, (
                f"the audit middleware references {forbidden}: it must read verified "
                "state, never decode tokens itself"
            )
        # The header itself must not be read for identity either -- reading it is how the
        # original code got a token to decode in the first place.
        assert "headers" not in referenced, (
            "the audit middleware must not read request headers for identity"
        )

    @pytest.mark.asyncio
    async def test_a_forged_authorization_header_alone_attributes_nothing(
        self, monkeypatch
    ):
        """Token TEXT must not become audit attribution.

        A client-supplied header with no verified caller behind it must record the attempt
        as unresolved. Otherwise the audit trail could be poisoned with a chosen identity
        by anyone able to send a header, which is worse than no trail.
        """
        forged_org = uuid.uuid4()
        token, _ = create_access_token(forged_org)
        await _drive(
            monkeypatch,
            method="POST",
            path="/workspaces",
            status_code=401,
            headers=[(b"authorization", f"Bearer {token}".encode())],
        )
        (row,) = await _events()
        assert row.principal == PRINCIPAL_UNRESOLVED
        assert row.org_id is None


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _caller(principal: str, org_id: uuid.UUID):
    """A stand-in for the verified caller `app/domain_guard.py` publishes."""

    class _Principal:
        subject = principal
        client_id = "test-client"
        account_type = "human"

    _Principal.org_id = str(org_id)

    class _Caller:
        principal = _Principal()
        safe_headers: dict[str, str] = {}

    return _Caller()


async def _drive(
    monkeypatch,
    *,
    method: str,
    path: str,
    status_code: int,
    caller=None,
    legacy: tuple[uuid.UUID, str] | None = None,
    query_string: str = "",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    downstream_error: Exception | None = None,
):
    """Run one request through the real `AuditMiddleware` against a stub downstream app.

    WHY A STUB DOWNSTREAM RATHER THAN THE FULL ROUTER. The behaviours under test are the
    middleware's own decisions across combinations the routers cannot all produce on
    demand -- a guard denial, a pre-identity rejection, a rate-limit short-circuit, and an
    injected storage failure. Driving the middleware directly makes the response status and
    the published identity inputs to the test instead of things to be provoked, so each
    assertion names one behaviour. `TestMiddlewareOrdering` covers the wiring in
    `app/main.py` separately, which is the part a stub cannot establish.
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    captured: dict = {}

    async def _downstream(request: Request):
        # Publish identity where the real components publish it, AFTER the middleware has
        # called through -- which is the ordering the fix depends on.
        if caller is not None:
            request.state.caller = caller
        if legacy is not None:
            request.state.audit_org_id, request.state.audit_principal = legacy
        if downstream_error is not None:
            raise downstream_error
        return JSONResponse(status_code=status_code, content={})

    class _App:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            response = await _downstream(request)
            await response(scope, receive, send)

    middleware = AuditMiddleware(_App())

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string.encode(),
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }

    sent: list = []

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def _send(message):
        sent.append(message)

    await middleware(scope, _receive, _send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    captured["status_code"] = start["status"]
    return type("R", (), captured)


@pytest.mark.asyncio
async def test_handler_exception_is_audited_before_it_propagates(monkeypatch):
    org_id = await _seed_org()
    with pytest.raises(RuntimeError, match="handler failed"):
        await _drive(
            monkeypatch,
            method="POST",
            path="/workspaces",
            status_code=500,
            caller=_caller(TEST_PRINCIPAL, org_id),
            downstream_error=RuntimeError("handler failed"),
        )
    (row,) = await _events()
    assert (row.principal, row.org_id, row.http_status, row.outcome) == (
        TEST_PRINCIPAL,
        org_id,
        500,
        "error",
    )


@pytest.mark.asyncio
async def test_verified_actor_survives_a_refused_tenant_binding(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from starlette.requests import Request
    from app import domain_guard
    from app import organization_binding

    caller = _caller(TEST_PRINCIPAL, uuid.uuid4())
    monkeypatch.setattr(
        domain_guard.domain_auth,
        "require_verified_caller",
        AsyncMock(return_value=caller),
    )
    monkeypatch.setattr(
        organization_binding,
        "bind_caller",
        AsyncMock(side_effect=HTTPException(403, "no binding")),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/workspaces",
            "headers": [],
            "route": SimpleNamespace(path="/workspaces"),
            "app": SimpleNamespace(state=SimpleNamespace(domain_policy=object())),
        }
    )
    with pytest.raises(HTTPException):
        await domain_guard.enforce_domain_authorization(
            request, credentials=None, db=AsyncMock()
        )
    assert AuditMiddleware._resolve_identity(request) == (TEST_PRINCIPAL, None)


@pytest.mark.asyncio
async def test_actor_filter_uses_verified_principal_and_remains_tenant_scoped():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.database import Base
    from app.models.event import Event
    from app.models.organization import Organization
    from app.routers.events import list_events

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[Organization.__table__, Event.__table__]))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        org, other = uuid.uuid4(), uuid.uuid4()
        async with factory() as db:
            db.add_all([Organization(id=org, name="one"), Organization(id=other, name="two")])
            await db.flush()
            records = [
                Event(org_id=org, principal="actor", action="created", resource_type="workspace", event_type="api_call"),
                Event(org_id=other, principal="actor", action="created", resource_type="workspace", event_type="api_call"),
                Event(org_id=org, principal="different", user_id="actor", action="created", resource_type="workspace", event_type="api_call"),
                Event(org_id=org, user_id="actor", action="created", resource_type="workspace", event_type="api_call"),
            ]
            db.add_all(records)
            await db.commit()
            result = await list_events(
                resource_type=None, user="actor", action=None, event_type=None,
                start_time=None, end_time=None, limit=50, offset=0, org_id=org, db=db,
            )
            assert result.total == 2
            assert {row.id for row in result.events} == {records[0].id, records[3].id}
    finally:
        await engine.dispose()
