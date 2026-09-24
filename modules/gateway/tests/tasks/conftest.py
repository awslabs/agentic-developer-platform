"""Shared harness for the Task API route tests.

Two deliberate choices here, both about what these tests are allowed to assume.

**A minimal app, not the gateway's.** These tests mount only the Task API routers.
Building the full application would drag in every middleware the gateway has, and a
failure anywhere in that stack would present as a Task API route failure. Worse, a
middleware that happened to reject or authorize a request would make an
authorization test pass without the route's own check ever running. Here the
routers are mounted with their real dependencies and exactly two seams are
substituted, named below, so what each test proves is unambiguous.

**A real contract validator, not hand-written field assertions.** Every emitted
body is checked against the frozen schemas in ``docs/task-api/contracts/v1/schemas``
using T0's own validator — the one an external evaluator runs. Asserting
field-by-field in a test would let the implementation and the test drift together
away from the contract, which is the single failure mode a conformance test exists
to prevent.

What *is* substituted, and why it has to be:

* ``authz.authenticate`` / ``authz.resolve_caller`` — validating a Cognito access
  token and resolving a canonical M2M principal needs a live JWKS endpoint and an
  identity directory. Driving them through HTTP would test Cognito. The part these
  tests are about — ``authorize_task``, ``authorize_artifact``, scope requirements
  — runs for real against the store.
* ``report_routes._AUTHENTICATOR`` — minting a real run credential needs an HMAC
  key and a TokenReview-verified pod, and T3/T4 own those routes. Substituting the
  proven attempt is what makes the property testable at all: the route must compare
  the body against whatever the transport proved and refuse every mismatch.
* ``routes.get_session_factory`` — the stream's mid-flight re-authorization opens
  its own session, because the request-scoped one is closed long before a
  ten-minute stream ends. With ``resolve_caller`` substituted that session is never
  read, so it is replaced with one that yields nothing rather than standing up an
  RDS engine to hold a value no assertion depends on.

Both substitutions are of *inputs*, never of the decisions under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.tasks import artifacts as artifacts_module
from src.tasks import authz, http, report_routes
from src.tasks import routes as routes_module
from src.tasks.store import ArtifactRecord, InMemoryTaskStore, TaskRecord
from src.tasks.streaming import StreamRegistry

REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_DIR = REPO_ROOT / "docs" / "task-api" / "contracts" / "v1" / "schemas"

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
OTHER_TASK = "tsk_4e6f9b21-3c5d-4f70-8b92-8d4f6e0a2c31"
INVOCATION = "5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40"
ATTEMPT = "a1b2c3d4-e5f6-4718-9a2b-3c4d5e6f7081"
OTHER_ATTEMPT = "b2c3d4e5-f6a7-4829-ab3c-4d5e6f708192"
ARTIFACT = "art_7c1e4d92-5a6b-4c8d-9e01-2f3a4b5c6d70"
OTHER_ARTIFACT = "art_8d2f5ea3-6b7c-4d9e-8f12-3a4b5c6d7e81"
REPORT = "3d852922-4be7-4319-98c7-dfc1b31a5a2f"

OWNER = authz.Caller(principal_id="svc-alpha", tenant_id="org-alpha", scopes=frozenset({authz.SCOPE_READ, authz.SCOPE_ARTIFACTS}))
#: Same tenant, different service. V1 grants no implicit same-tenant access, so
#: this caller must be refused exactly as a cross-tenant one is — indistinguishably.
SAME_TENANT_OTHER = authz.Caller(principal_id="svc-beta", tenant_id="org-alpha", scopes=OWNER.scopes)
OTHER_TENANT = authz.Caller(principal_id="svc-gamma", tenant_id="org-beta", scopes=OWNER.scopes)
READ_ONLY = authz.Caller(principal_id="svc-alpha", tenant_id="org-alpha", scopes=frozenset({authz.SCOPE_READ}))
ARTIFACTS_ONLY = authz.Caller(principal_id="svc-alpha", tenant_id="org-alpha", scopes=frozenset({authz.SCOPE_ARTIFACTS}))
NO_SCOPES = authz.Caller(principal_id="svc-alpha", tenant_id="org-alpha", scopes=frozenset())


@pytest.fixture(scope="session")
def contract():
    """Validate an instance against a frozen v1 schema, returning error strings.

    T0's validator is imported by path rather than vendored: a copy would be a
    second validator to keep in step with the contract, and the point of checking
    against T0's is that it is the one the conformance baseline uses.
    """
    scripts = str(REPO_ROOT / "scripts" / "task-api")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import _schema

    registry = _schema.Registry(SCHEMA_DIR)

    def check(instance, ref: str) -> list[str]:
        document = ref.split("#", 1)[0]
        target, doc = registry.resolve(ref, document)
        return _schema.validate(instance, target, registry, doc)

    return check


def make_record(**overrides) -> TaskRecord:
    """A running task owned by ``OWNER``.

    Field-for-field the contract's own ``task-snapshot-running`` fixture, so a
    snapshot rendered from this record is comparable with the evidence the contract
    ships rather than with values invented here.
    """
    base = {
        "task_id": TASK,
        "invocation_id": INVOCATION,
        "tenant_id": "org-alpha",
        "owner_principal_id": "svc-alpha",
        "persona": "agent-task-investigator",
        "status": "running",
        "version": 4,
        "created_at": "2026-09-24T14:42:03Z",
        "updated_at": "2026-09-24T14:43:20Z",
        "deadline_at": "2026-09-24T15:12:03Z",
        "runtime_attempt_id": ATTEMPT,
        "external_reference": "incident-INC-2026-0924-0031",
        "queue_ack_status": "confirmed",
    }
    return TaskRecord(**{**base, **overrides})


def make_artifact(**overrides) -> ArtifactRecord:
    base = {
        "artifact_id": ARTIFACT,
        "version": 1,
        "tenant_id": "org-alpha",
        "owner_principal_id": "svc-alpha",
        "content_type": "text/plain",
        "content_sha256": "a" * 64,
        "content_length": 11,
        "created_at": "2026-09-24T14:42:03Z",
        "expires_at": "2026-09-25T14:42:03Z",
        "task_id": TASK,
        "storage_key": "tasks/t/p/artifact/1",
    }
    return ArtifactRecord(**{**base, **overrides})


def emit(store: InMemoryTaskStore, count: int = 1, *, task_id: str = TASK, event_type: str = "progress.updated", data: dict | None = None) -> None:
    """Append ``count`` committed events, letting the store allocate sequences."""
    for index in range(count):
        store.append_event(
            task_id=task_id,
            report_id=None,
            event_type=event_type,
            data=data or {"message": f"step {index}", "stage": "analysis"},
            producer_timestamp=None,
            timestamp=f"2026-09-24T14:43:1{index % 10}Z",
        )


def finish(store: InMemoryTaskStore, *, task_id: str = TASK) -> None:
    """Drive the task to ``completed`` with its terminal event committed.

    Used by every stream test whose subject is content rather than liveness: a
    terminal task's stream closes itself, so it can be read with an ordinary HTTP
    client. Liveness — that progress arrives *before* the run exits — is the one
    thing that cannot be shown this way, and those tests use ``StreamProbe``.
    """
    record = store.tasks[task_id]
    store.set_status(task_id, status="completed", result={"summary": "root cause identified"})
    emit(
        store,
        1,
        task_id=task_id,
        event_type="task.completed",
        data={"status": "completed", "version": record.version + 1, "outcome": "completed"},
    )


@contextlib.asynccontextmanager
async def _null_session():
    """Stand in for a database session the substituted ``resolve_caller`` never reads."""
    yield None


@pytest.fixture
def store() -> InMemoryTaskStore:
    backing = InMemoryTaskStore()
    backing.put_task(make_record())
    return backing


@pytest.fixture
def caller() -> list[authz.Caller]:
    """The caller each request resolves to, in a one-element list.

    A mutable container rather than a plain value so a test can change identity
    *between* requests — which is how revocation is exercised without an identity
    directory to revoke in.
    """
    return [OWNER]


@pytest.fixture
def attempt() -> list[report_routes.VerifiedAttempt]:
    """The attempt the transport is taken to have proven, in a mutable container."""
    return [report_routes.VerifiedAttempt(task_id=TASK, invocation_id=INVOCATION, generation=1, runtime_attempt_id=ATTEMPT)]


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    """Both surfaces enabled by default; the gating tests turn them off.

    Autouse because the flags default false in production, so without this every
    route test would assert against a 503 and prove nothing about the route.
    """
    monkeypatch.setenv(http.FLAG_READ, "true")
    monkeypatch.setenv(http.FLAG_WORKER, "true")


@pytest.fixture(autouse=True)
def isolated_module_state(monkeypatch):
    """Give each test its own stream registry and no inherited store.

    ``routes._STREAMS`` is process-wide by design — the caps bound what one replica
    holds open — which makes it shared mutable state in a test process. A test that
    abandoned two streams would push the next test over the per-task cap of 2 and
    fail it with a 429 that has nothing to do with what it asserts. Replacing the
    registry per test removes that coupling without weakening the production
    behaviour.
    """
    monkeypatch.setattr(routes_module, "_STREAMS", StreamRegistry())
    monkeypatch.setattr(routes_module, "_STORE", None)
    monkeypatch.setattr(report_routes, "_STORE", None)
    monkeypatch.setattr(report_routes, "_AUTHENTICATOR", None)


@pytest.fixture
def api(monkeypatch, store, caller, attempt) -> FastAPI:
    """The Task API routers mounted with the two input seams substituted."""

    def fake_authenticate(request):
        return object(), caller[0].scopes

    async def fake_resolve(context, scopes, db):
        return caller[0]

    monkeypatch.setattr(authz, "authenticate", fake_authenticate)
    monkeypatch.setattr(authz, "resolve_caller", fake_resolve)
    monkeypatch.setattr(report_routes, "_AUTHENTICATOR", lambda request: attempt[0])
    monkeypatch.setattr(routes_module, "get_session_factory", lambda: _null_session)

    routes_module.set_store(store)
    report_routes.set_store(store)

    app = FastAPI()
    app.include_router(routes_module.router)
    app.include_router(artifacts_module.router)
    app.include_router(report_routes.router)

    # The session is never used once ``resolve_caller`` is substituted, but the
    # dependency still has to resolve to something.
    from src.shared.database import get_db

    async def no_db():
        yield None

    app.dependency_overrides[get_db] = no_db
    # The transport guard itself (IRSA/internal identity) is exercised by the
    # agentauth tests that own it; here it is satisfied so the report route's own
    # checks are what the assertions attribute a refusal to.
    app.dependency_overrides[report_routes.require_agent_transport] = lambda: None
    return app


@pytest.fixture
async def client(api):
    """An HTTP client for requests that complete.

    Usable for every route except a stream against a *nonterminal* task.
    ``ASGITransport`` runs the whole application to completion before it returns a
    response object, so a live SSE stream — which by design polls for up to ten
    minutes — would hang the test rather than yield frames. Those cases use
    ``StreamProbe`` below.
    """
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as async_client:
        yield async_client


class StreamProbe:
    """Drives an SSE route over the raw ASGI interface, one frame at a time.

    This exists because the property T6-AC01 states cannot be observed through a
    buffering client. The criterion fails on "buffered final output", so a test that
    reads the whole response and then counts frames would pass against an
    implementation that emitted everything at the end — it would be measuring the
    test harness, not the surface.

    Talking ASGI directly gives two things no HTTP client wrapper can:

    * Each frame is observed as its own ``http.response.body`` message with
      ``more_body: True``, *while the application is still running*. That is the
      actual wire-level statement "this arrived before the run exited".
    * ``receive`` can answer ``http.disconnect``, which is how a client going away
      mid-stream is exercised — including that the route releases its stream slot on
      the path that abandons the generator rather than returning from it.
    """

    def __init__(self, app, path: str, headers: dict[str, str] | None = None) -> None:
        self.app = app
        self.path, _, self.query = path.partition("?")
        self.headers = headers or {}
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._disconnected = asyncio.Event()
        self._started = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.response_headers = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
            self._started.set()
        elif message["type"] == "http.response.body":
            if message.get("body"):
                await self.chunks.put(message["body"])
            if not message.get("more_body", False):
                await self.chunks.put(None)

    async def __aenter__(self) -> StreamProbe:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "root_path": "",
            "query_string": self.query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in self.headers.items()],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
            "state": {},
        }
        self._task = asyncio.create_task(self.app(scope, self._receive, self._send))
        await asyncio.wait_for(self._started.wait(), timeout=5)
        return self

    async def next_frame(self, *, timeout: float = 5.0) -> dict[str, str] | None:
        """The next parsed SSE frame, or None once the server closes the response.

        One frame per ``http.response.body`` message, which holds because the loop
        yields exactly one encoded frame at a time. Asserting that correspondence is
        part of the point: a route that coalesced frames into one write would fail
        the AC01 test here rather than passing it with the right total.
        """
        chunk = await asyncio.wait_for(self.chunks.get(), timeout=timeout)
        if chunk is None:
            return None
        parsed = frames(chunk)
        assert len(parsed) == 1, "each SSE frame must be written separately, not coalesced"
        return parsed[0]

    async def take(self, count: int, *, timeout: float = 5.0) -> list[dict[str, str]]:
        collected = []
        for _ in range(count):
            frame = await self.next_frame(timeout=timeout)
            if frame is None:
                break
            collected.append(frame)
        return collected

    async def disconnect(self) -> None:
        """Signal a client disconnect and let the route observe it."""
        self._disconnected.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()

    async def __aexit__(self, *exc) -> None:
        self._disconnected.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


@pytest.fixture
def probe(api):
    """Open a ``StreamProbe`` against the mounted app."""

    def open_stream(path: str, headers: dict[str, str] | None = None) -> StreamProbe:
        return StreamProbe(api, path, headers)

    return open_stream


def report_body(**overrides) -> dict:
    body = {
        "schema_version": "1.0",
        "attempt": {
            "run": {"task_id": TASK, "invocation_id": INVOCATION, "generation": 1},
            "runtime_attempt_id": ATTEMPT,
        },
        "report_id": REPORT,
        "event_type": "progress.updated",
        "producer_timestamp": "2026-09-24T14:43:17Z",
        "data": {"message": "Correlating 503 responses against pool acquisition timeouts.", "stage": "analysis"},
    }
    return {**body, **overrides}


def frames(payload: bytes) -> list[dict[str, str]]:
    """Split an SSE byte stream into parsed frames.

    Heartbeats are comment frames (``: {...}``) and are returned with a ``comment``
    key, because a test asserting that heartbeats never advance a position has to be
    able to see them *and* see that they carry no ``id``.
    """
    parsed: list[dict[str, str]] = []
    for block in payload.decode().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith(": "):
            parsed.append({"comment": block[2:]})
            continue
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            name, _, value = line.partition(": ")
            fields[name] = value
        parsed.append(fields)
    return parsed
