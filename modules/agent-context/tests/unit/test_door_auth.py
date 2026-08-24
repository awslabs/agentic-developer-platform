"""Door authentication regression tests (issue #4073, finding #8).

The vulnerability: the Door derives every ACL decision from caller-supplied
identity headers (``x-github-login``, ``x-github-teams``, ``x-tenant-id``,
``x-owner-sub``) and nothing authenticated the caller. Any workload able to
reach the ClusterIP could assert an arbitrary identity and read any tenant's
indexed source, wikis and agent memory. ``acl.py:7-9``,
``personal_context/identity.py:8-9`` and ``README.md:108`` each claimed an
in-cluster NetworkPolicy made those headers trustworthy; no NetworkPolicy
existed in this module.

These tests assert the OUTCOME (the request is denied / the data is not served),
not the mechanism. Every one of them fails on pre-fix code, where each request
below returns 200.

The ``/mcp`` case is the load-bearing one: ``server.py`` mounts the native MCP
app with ``app.mount()``, which FastAPI route dependencies do not cover. A
``Depends()``-based fix passes the ``/call`` tests and still leaves ``/mcp`` —
the surface agent workers actually use — unauthenticated.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from door import auth as door_auth
from door.config import config

_KEY = "test-door-key-4073"

# Opt this whole module out of the conftest fixture that disables Door auth for
# the pre-#4073 suites. Without this marker the autouse fixture would switch the
# control off and every assertion below would pass vacuously.
pytestmark = pytest.mark.door_auth


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch):
    """Give the Door a known key, auth enabled.

    Patches the live singleton because ``door.auth`` reads ``config`` attributes
    at request time (not import time), which is what makes the kill switch and
    the key hot-readable in tests.
    """
    monkeypatch.setattr(config, "door_api_key", _KEY, raising=False)
    monkeypatch.setattr(config, "door_auth_enabled", True, raising=False)


@pytest.fixture
async def client():
    from door.server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def mcp_client():
    """Client for the mounted ``/mcp`` sub-app.

    ``raise_app_exceptions=False`` because the mounted MCP app's session manager
    is started by its own lifespan, which ASGITransport does not run — reaching
    it raises RuntimeError. This test cares only about whether auth rejected the
    request *before* the mount was entered, so surface the status code instead of
    the exception. ``follow_redirects`` stays off so the /mcp -> /mcp/ redirect
    is observable rather than silently followed.
    """
    from door.server import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c


def _auth(**extra: str) -> dict[str, str]:
    return {door_auth.HEADER_API_KEY: _KEY, **extra}


class TestUnauthenticatedRequestsAreDenied:
    """No key -> no data. Pre-fix every one of these returned 200."""

    async def test_unauthenticated_call_is_denied(self, client):
        """POST /call without a key must not execute a verb."""
        resp = await client.post("/call", json={"name": "search", "arguments": {"query": "x"}})
        assert resp.status_code == 401, (
            f"POST /call without a key returned {resp.status_code}; the Door served a "
            "verb to an unauthenticated caller (issue #4073 finding #8)."
        )

    async def test_unauthenticated_tools_is_denied(self, client):
        """GET /tools without a key must not disclose the verb catalogue."""
        resp = await client.get("/tools")
        assert resp.status_code == 401, (
            f"GET /tools returned {resp.status_code}; the tool catalogue is a "
            "disclosure surface and must be authenticated."
        )

    async def test_forged_identity_without_key_is_denied(self, client):
        """The actual exploit: assert someone else's identity, no credential.

        Pre-fix this returned 200 and the request was ACL-evaluated as
        ``victim-user`` in tenant ``victim-tenant``.
        """
        resp = await client.post(
            "/call",
            json={"name": "search", "arguments": {"query": "secrets"}},
            headers={
                "x-github-login": "victim-user",
                "x-github-teams": "victim-org/admins",
                "x-tenant-id": "victim-tenant",
                "x-owner-sub": "00000000-0000-0000-0000-000000000001",
            },
        )
        assert resp.status_code == 401, (
            f"A forged-identity request with no credential returned {resp.status_code}. "
            "Identity headers are only trustworthy if the caller is authenticated."
        )

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    async def test_unauthenticated_mcp_is_denied(self, mcp_client, path):
        """POST /mcp without a key must be denied — catches the mount gap.

        ``app.mount("/mcp", get_mcp_app())`` is a separate ASGI app; FastAPI
        ``Depends()`` on the parent app does not run for it. This test is what
        distinguishes a real fix from one that only guards the REST routes.

        Both spellings are checked deliberately. ``/mcp`` is what
        ``knowledge-layer-config.ts`` derives when ``CONTEXT_MCP_SERVER_URL`` has
        no trailing slash, and Starlette answers it with a 307 redirect to
        ``/mcp/`` — so a guard that only covers the redirect target would leave
        the un-normalized path answering before auth runs. Pre-fix these returned
        307 and 500 respectively (the mount was reached and began dispatching);
        neither is 401, so both assertions genuinely fail on pre-fix code.

        Uses ``mcp_client`` (``raise_app_exceptions=False``): the mounted MCP app
        raises "Task group is not initialized" under ASGITransport because its
        session-manager lifespan never runs, and an unhandled exception would
        mask the status code this test is about.
        """
        resp = await mcp_client.post(
            path,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        assert resp.status_code == 401, (
            f"POST {path} without a key returned {resp.status_code}. The native MCP "
            "surface is mounted, so a route-dependency guard does not cover it — "
            "this is the endpoint agent workers actually call."
        )

    async def test_wrong_key_is_denied(self, client):
        resp = await client.post(
            "/call",
            json={"name": "search", "arguments": {"query": "x"}},
            headers={door_auth.HEADER_API_KEY: "wrong-key"},
        )
        assert resp.status_code == 401

    async def test_denial_carries_no_authenticate_challenge(self, client):
        """401 without ``WWW-Authenticate`` — do not advertise the scheme."""
        resp = await client.post("/call", json={"name": "search", "arguments": {}})
        assert resp.status_code == 401
        assert "www-authenticate" not in {k.lower() for k in resp.headers}


class TestAuthenticatedCallersStillWork:
    """The fix must not break the legitimate callers (regression guard)."""

    async def test_health_is_public(self, client):
        """Probes cannot present a secret; gating /health CrashLoops the pod."""
        resp = await client.get("/health")
        assert resp.status_code == 200, (
            "GET /health must stay unauthenticated — the readiness and liveness "
            "probes in manifests/context-mcp.yaml would fail otherwise."
        )

    async def test_tools_with_key_succeeds(self, client):
        resp = await client.get("/tools", headers=_auth())
        assert resp.status_code == 200
        assert isinstance(resp.json(), list) and resp.json()

    async def test_call_with_key_reaches_verb_dispatch(self, client):
        """A keyed request is no longer rejected by auth.

        Asserts only that auth stopped rejecting it: an unknown verb yields a
        4xx from the dispatcher, never 401.
        """
        resp = await client.post("/call", json={"name": "", "arguments": {}}, headers=_auth())
        assert resp.status_code != 401

    async def test_keyed_forged_identity_is_still_acl_filtered(self, client):
        """Authentication is not authorization.

        Holding the shared secret must not by itself grant cross-tenant reads —
        the ACL layer still scopes results per principal. This pins that the fix
        did not turn the key into a bypass of ``filter_results``.
        """
        resp = await client.post(
            "/call",
            json={"name": "search", "arguments": {"query": "x"}},
            headers=_auth(**{"x-github-login": "attacker", "x-tenant-id": "other-tenant"}),
        )
        assert resp.status_code != 401
        body = resp.json()
        results = body.get("results", body) if isinstance(body, dict) else body
        if isinstance(results, list):
            assert results == [], (
                "A caller with the shared secret but an unresolvable principal got "
                "non-empty results; the ACL layer must stay fail-closed independently "
                "of authentication."
            )


class TestMisconfigurationFailsClosed:
    """An unset key must not silently reopen the hole."""

    async def test_missing_key_config_rejects(self, client, monkeypatch):
        """No configured key -> 503, not 200.

        Fail-open here is the same class of defect as the ALLOW_OPEN_SIGNUP and
        budget fail-open incidents this repo carries runbooks for: the control
        silently vanishes when its config fails to land.
        """
        monkeypatch.setattr(config, "door_api_key", "", raising=False)
        resp = await client.post("/call", json={"name": "search", "arguments": {}})
        assert resp.status_code == 503, (
            f"With no DOOR_API_KEY configured the Door returned {resp.status_code}. "
            "It must fail closed rather than serve unauthenticated requests."
        )

    async def test_auth_enabled_defaults_to_true(self, monkeypatch):
        """The kill switch defaults ON, so an absent env var is safe.

        Builds a fresh ServerConfig with the env var genuinely removed, rather
        than reading the already-constructed singleton (which the autouse fixture
        has patched).
        """
        from door.config import ServerConfig

        monkeypatch.delenv("DOOR_AUTH_ENABLED", raising=False)
        assert ServerConfig().door_auth_enabled is True, (
            "DOOR_AUTH_ENABLED must default to True — a missing or misspelled env "
            "var must not disable authentication."
        )

    @pytest.mark.parametrize("value", ["ture", "True ", "", "yes-please", "enabled"])
    async def test_unrecognized_kill_switch_value_keeps_auth_enabled(
        self, monkeypatch, value
    ):
        """A typo or stray value must NOT disable authentication.

        The other enable-flags in ServerConfig default to "false" and parse with
        ``in ("true","1","yes")``, where an unrecognized value safely means off.
        This flag defaults to "true", so that same idiom would make ``"ture"`` or
        a trailing-space ``"True "`` evaluate to False and silently switch the
        control off — exactly the fail-open shape of the ALLOW_OPEN_SIGNUP
        incident. Only an explicit recognized false-y value may disable it.
        """
        monkeypatch.setenv("DOOR_AUTH_ENABLED", value)
        from door.config import ServerConfig

        assert ServerConfig().door_auth_enabled is True, (
            f"DOOR_AUTH_ENABLED={value!r} disabled Door authentication. An "
            "unrecognized value must fail CLOSED (auth stays on)."
        )

    @pytest.mark.parametrize("value", ["false", "0", "no", "FALSE", " false "])
    async def test_explicit_false_disables_auth(self, monkeypatch, value):
        """The switch still works when set deliberately."""
        monkeypatch.setenv("DOOR_AUTH_ENABLED", value)
        from door.config import ServerConfig

        assert ServerConfig().door_auth_enabled is False


class TestDnsRebindingProtection:
    """#3254 disabled Host-header validation entirely; #4073 re-enables it.

    Asserted against the SDK's own matcher rather than through the app, because
    the mounted MCP app's session manager cannot be driven under ASGITransport
    (its lifespan never runs) and the resulting 500 masks the status code.
    """

    def _middleware(self):
        from mcp.server.transport_security import TransportSecurityMiddleware

        from door.mcp_app import mcp_server

        return TransportSecurityMiddleware(mcp_server.settings.transport_security)

    def test_protection_is_enabled(self):
        from door.mcp_app import mcp_server

        assert mcp_server.settings.transport_security.enable_dns_rebinding_protection is True, (
            "DNS-rebinding protection is disabled; any Host header is accepted "
            "(issue #4073, superseding #3254's blanket disable)."
        )

    @pytest.mark.parametrize(
        "host",
        [
            # The FQDN every in-tree caller uses.
            "context-mcp.agent-context.svc.cluster.local:5100",
            "context-mcp.agent-context.svc.cluster.local",
            # Search-path forms a same-namespace pod produces.
            "context-mcp:5100",
            "context-mcp",
            "context-mcp.agent-context:5100",
            "context-mcp.agent-context.svc:5100",
            # In-pod probes (scripts/validate.sh).
            "localhost:5100",
            "127.0.0.1:5100",
        ],
    )
    def test_real_callers_are_allowed(self, host):
        """Re-enabling must not 421 a legitimate in-cluster caller.

        This is the regression #3254 was reacting to: the SDK's localhost-only
        default rejected cluster DNS names and broke every agent worker.
        """
        assert self._middleware()._validate_host(host) is True, (
            f"Host {host!r} is rejected (421 Misdirected Request). This is a real "
            "in-cluster caller — see the allowed_hosts comment in mcp_app.py."
        )

    @pytest.mark.parametrize(
        "host", ["evil.attacker.com", "context-mcp.attacker.com", "attacker.com:5100", None, ""]
    )
    def test_foreign_hosts_are_rejected(self, host):
        assert self._middleware()._validate_host(host) is False, (
            f"Host {host!r} was accepted. A browser tricked into resolving that "
            "name to the Door's cluster IP could read responses cross-origin."
        )

    def test_absent_origin_is_allowed_but_browser_origin_is_not(self):
        """In-cluster HTTP clients send no Origin; browsers do.

        Pins that ``allowed_origins`` stays empty rather than ``["*"]`` — the SDK
        matches that string literally, so ``["*"]`` would read as permissive
        while rejecting exactly the same requests.
        """
        mw = self._middleware()
        assert mw._validate_origin(None) is True
        assert mw._validate_origin("https://evil.example.com") is False


class TestMiddlewareOrdering:
    """Auth must run before identity is stamped onto a trace."""

    async def test_auth_runs_before_span_enrichment(self):
        """Forged identity on a rejected request must not reach the span.

        Starlette builds the stack in reverse registration order, so
        ``authenticate_request`` must be registered AFTER
        ``enrich_span_with_identity`` to run before it. Swapping the two
        decorators still returns 401 but records the forged identity — this test
        is what catches that.
        """
        from door.server import app

        names = [m.kwargs.get("dispatch").__name__ for m in app.user_middleware if m.kwargs.get("dispatch")]
        assert "authenticate_request" in names, "authenticate_request middleware is not installed"
        assert "enrich_span_with_identity" in names
        assert names.index("authenticate_request") < names.index("enrich_span_with_identity"), (
            "authenticate_request must be OUTERMOST (last registered => first in "
            "app.user_middleware). As ordered, unauthenticated identity claims are "
            "stamped onto the OTel span before the request is rejected."
        )
