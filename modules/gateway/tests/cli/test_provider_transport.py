"""Provider credentials over the REAL transport and the REAL routes (#5039, #5637).

These tests exist because the original U6 suites passed while defects shipped, and
every time the reason was a stand-in for the thing under test.

**#5039 — the test double hid the transport.** `adp_common.Api.request` ended every
call with `json.load(response)`. ADP's vault delete is declared ``status_code=204,
response_model=None`` (`src/auth/vault_routes.py:249`) and sends no body, so the
parse raised and a SUCCESSFUL delete was reported as an unreachable gateway. A
`RecordingApi` returning `{}` cannot show that. And `read_provider_value` was
monkeypatched, so a `readline()` that truncated a multi-line service-account key
to ``{`` passed review. So these tests speak HTTP — a real `http.server` returns
real status lines, including bodiless 204s — feed real stdin, and assert on what
the SERVER received rather than on what the CLI said it did.

**#5637 — the test double also hid the ROUTES.** The suite this replaces routed
``/providers`` and ``/providers/{id}``. Those paths were invented by the helper:
they are absent from the gateway's allowlist
(`src/domain_proxy/superplane_routes.json`), so in production every provider verb
was 404'd at the proxy before the domain saw it — while these tests, which served
whatever path they were asked for, stayed green. A local server that answers
anything proves reachability of nothing.

Two rules follow, and both are now enforced here:

1. Routes come from the allowlist, not from the test. `_Gateway.route` refuses a
   path the gateway would not forward, so a reintroduced invented endpoint fails
   at the fixture instead of passing.
2. The two-store split is asserted by identifier, not by shape. The domain record
   carries only an `adp_credential_id` REFERENCE; the vault holds the value. They
   are different ids, and sending one to both endpoints is the defect that leaves
   a live secret behind while the command reports success.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"
ALLOWLIST = Path(__file__).parents[2] / "src/domain_proxy/superplane_routes.json"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CLI_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load("adp_superplane_transport", "adp-superplane.py")
CURRENT_RECOVERY_CONTEXT = cli.current_recovery_context
# The extension does `import adp_common as common`, so THIS is the transport it
# actually uses. Loading a second copy would give CliError a different class
# identity and make `pytest.raises` miss.
common = cli.common

SERVICE_ACCOUNT_KEY = '{\n  "type": "service_account",\n  "project_id": "synthetic-example"\n}\n'
SINGLE_LINE_SECRET = "sk-synthetic-single-line-value"

# Two DIFFERENT identifiers throughout, never interchangeable:
#   DOMAIN_RECORD — the domain's own row id, a UUID, taken by
#                   DELETE /vault/credentials/{credential_id}.
#   VAULT_REFERENCE — the opaque ADP vault handle that row POINTS AT, taken by
#                   DELETE /auth/credentials/{id}.
# A test that used one value for both would pass against the defect this suite
# exists to catch.
DOMAIN_RECORD = "11111111-2222-3333-4444-555555555555"
VAULT_REFERENCE = "66666666-7777-4888-8999-aaaaaaaaaaaa"
# A credential belonging to somebody else. Nothing in any test may delete it.
UNRELATED_VAULT_REFERENCE = "cred-vault-someone-else"

WORKSPACE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RECOVERY_CONTEXT = {
    "deployment_id": "deployment-1",
    "deployment": "test",
    "gateway": "https://gateway.example.test/api",
    "principal": "user-1",
    "tenant": "org-1",
}

# The gateway's own allowlist, as regexes, so the fake gateway forwards exactly
# what the real one would. Built from the shipped file rather than restated: a
# path removed there must stop being servable here too.
_ALLOWED = tuple((method, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$")) for method, path in json.loads(ALLOWLIST.read_text()))
# Routes the GATEWAY serves itself, outside the domain proxy: ADP's vault. These
# are not in the domain allowlist because they are not domain routes at all —
# which is the distinction the CLI has to get right.
_GATEWAY_OWNED = (
    ("GET", re.compile(r"^/auth/credentials$")),
    ("POST", re.compile(r"^/auth/credentials$")),
    ("PUT", re.compile(r"^/auth/credentials/[0-9a-f-]+$")),
    ("DELETE", re.compile(r"^/auth/credentials/[^/]+$")),
)


def forwardable(method: str, path: str) -> bool:
    """Would the real deployment answer this at all?

    `/api` is the gateway mount `adp_common.gateway_url()` appends; everything
    under `/api/superplane/v1` is matched against the domain allowlist exactly as
    `src/domain_proxy/superplane.py` matches it.
    """
    for prefix, table in (("/api/superplane/v1", _ALLOWED), ("/api", _GATEWAY_OWNED)):
        if path.startswith(prefix + "/") or path == prefix:
            native = path[len(prefix) :] or "/"
            return any(verb == method and pattern.fullmatch(native) for verb, pattern in table)
    return False


class _Handler(BaseHTTPRequestHandler):
    """Answers from a per-test routing table. Records every request it served."""

    routes: dict[tuple[str, str], tuple[int, object]] = {}
    seen: list[dict] = []

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass

    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        self.__class__.seen.append(
            {
                "method": method,
                "path": path,
                "query": self.path.split("?", 1)[1] if "?" in self.path else "",
                "raw": raw.decode() or None,
                "body": json.loads(raw) if raw else None,
            }
        )

        status, payload = self.__class__.routes.get((method, path), (404, {"detail": {"error": "not_found"}}))
        self.send_response(status)
        # 204 carries no body and no Content-Type — the shape that used to break.
        if status in (204, 205) or payload is None:
            self.end_headers()
            return
        encoded = json.dumps(payload).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
        self._serve("POST")

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
        self._serve("PUT")

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
        self._serve("DELETE")


class _Gateway:
    """A real HTTP gateway the CLI talks to over a socket."""

    def __init__(self) -> None:
        _Handler.routes, _Handler.seen = {}, []
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def route(self, method: str, path: str, status: int, payload: object = None) -> None:
        """Register a response — but only for a route a real deployment serves.

        This guard is the #5637 lesson made mechanical. The previous suite stubbed
        `/providers`, a path the gateway allowlist has never contained, so the
        tests proved the CLI could talk to a server that does not exist. Asserting
        here means an invented endpoint fails the test that stubs it.
        """
        assert forwardable(method, path), (
            f"{method} {path} is not forwardable: it is in neither the gateway's Superplane "
            f"allowlist ({ALLOWLIST.name}) nor ADP's own vault routes. Stubbing it would test a "
            "request the real deployment answers with 404."
        )
        _Handler.routes[(method, path)] = (status, payload)

    def unrouted(self, method: str, path: str, status: int, payload: object = None) -> None:
        """Register a response for a path the gateway does NOT forward.

        Used only to prove the CLI never calls one: if it did, the recorded
        requests would show it.
        """
        _Handler.routes[(method, path)] = (status, payload)

    @property
    def seen(self) -> list[dict]:
        return _Handler.seen

    def paths(self, method: str | None = None) -> list[str]:
        return [row["path"] for row in self.seen if method is None or row["method"] == method]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """A live gateway plus the sandboxed ADP config/session that reaches it."""
    server = _Gateway()
    config = tmp_path / ".bedrock-gateway"
    config.mkdir()
    (config / "config.json").write_text(json.dumps({"gateway_url": server.url}))

    for key in list(os.environ):
        if key.startswith("ADP_DEPLOYMENT") or key in {"ADP_HOME", "ADP_LEGACY_CONFIG_DIR", "BG_CONFIG_DIR"}:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Each test models a fresh CLI process with its own deployment selection.
    monkeypatch.setattr(cli.common, "_deployment", cli.common._UNRESOLVED)
    # The session token is the ONLY credential path; stubbing its retrieval keeps
    # these tests offline without introducing a second credential store.
    monkeypatch.setattr(cli.common, "access_token", lambda: "synthetic-session-token")
    monkeypatch.setattr(cli, "current_recovery_context", lambda api=None: dict(RECOVERY_CONTEXT))
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(VAULT_REFERENCE))
    try:
        yield server
    finally:
        server.close()


def _api(gateway) -> object:
    """The real transport, pointed at the live gateway."""
    return common.Api()


DOMAIN_LIST = f"/api{cli.API_BASE}{cli.DOMAIN_CREDENTIALS}"
DOMAIN_RECORD_PATH = f"{DOMAIN_LIST}/{DOMAIN_RECORD}"
VAULT_LIST = f"/api{cli.VAULT_CREDENTIALS}"
VAULT_RECORD_PATH = f"{VAULT_LIST}/{VAULT_REFERENCE}"


def _domain_response(**overrides) -> dict:
    return {
        "id": DOMAIN_RECORD,
        "name": "prod",
        "provider": "nebius",
        "credential_type": "api_key",
        "adp_credential_id": VAULT_REFERENCE,
        "status": "Active",
        **overrides,
    }


def _registered(gateway, **overrides) -> None:
    """The domain's view of one registered credential, as its real list response.

    Field names are `CredentialListResponse`/`CredentialResponse`
    (app/schemas/account.py): `credentials`, `id`, `adp_credential_id`.
    """
    gateway.route("GET", DOMAIN_LIST, 200, {"credentials": [_domain_response(**overrides)], "total": 1})


def _receipt(reference, name):
    return {
        "adp_credential_id": reference,
        "name": name,
        "provider": "nebius",
        "vault_credential_type": "api_key",
        "credential_type": "api_key",
        "phase": "domain_pending",
        "recovery_context": dict(RECOVERY_CONTEXT),
    }


def _vault_response(reference=VAULT_REFERENCE, *, label="prod"):
    return {
        "id": reference,
        "service": "nebius",
        "label": label,
        "credential_type": "api_key",
        "scope": "user",
    }


def _jwt(claims):
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_recovery_context_comes_from_the_selected_deployment_and_signed_session(monkeypatch) -> None:
    monkeypatch.setattr(common, "deployment_stamp", lambda: {"deployment_id": "deployment-1", "deployment": "test"})
    monkeypatch.setattr(common, "gateway_url", lambda: "https://gateway.example.test/api")
    monkeypatch.setattr(common, "access_token", lambda: _jwt({"sub": "user-1", "custom:org_id": "org-1"}))

    assert CURRENT_RECOVERY_CONTEXT() == RECOVERY_CONTEXT


def test_recovery_context_refuses_orgless_password_sessions(monkeypatch) -> None:
    monkeypatch.setattr(common, "deployment_stamp", lambda: {"deployment_id": "deployment-1", "deployment": "test"})
    monkeypatch.setattr(common, "gateway_url", lambda: "https://gateway.example.test/api")
    monkeypatch.setattr(common, "access_token", lambda: _jwt({"sub": "user-1"}))

    with pytest.raises(common.CliError) as raised:
        CURRENT_RECOVERY_CONTEXT()
    assert raised.value.code == "tenant_bound_token_required"


@pytest.mark.parametrize("action", ["add", "delete", "recover"])
def test_orgless_password_session_refuses_provider_mutation_and_keeps_receipts(gateway, monkeypatch, action) -> None:
    monkeypatch.setattr(cli, "current_recovery_context", CURRENT_RECOVERY_CONTEXT)
    monkeypatch.setattr(
        common,
        "deployment_stamp",
        lambda: {"deployment_id": "deployment-1", "deployment": "test"},
    )
    monkeypatch.setattr(common, "gateway_url", lambda: gateway.url)
    monkeypatch.setattr(common, "access_token", lambda: _jwt({"sub": "user-1"}))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    receipt = _receipt(VAULT_REFERENCE, "prod")
    cli.save_provider_recovery(receipt)
    args = _add_args() if action == "add" else _delete_args()
    if action == "recover":
        args = cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--yes"])
    with pytest.raises(common.CliError) as raised:
        cli.run(args, _api(gateway))
    assert raised.value.code == "tenant_bound_token_required"
    assert gateway.paths() == []
    assert cli.provider_recoveries()[VAULT_REFERENCE] == receipt


def _live_recovery_context(gateway, monkeypatch, token_source):
    monkeypatch.setattr(cli, "current_recovery_context", CURRENT_RECOVERY_CONTEXT)
    monkeypatch.setattr(common, "deployment_stamp", lambda: {"deployment_id": "deployment-1", "deployment": "test"})
    monkeypatch.setattr(common, "gateway_url", lambda: gateway.url)
    monkeypatch.setattr(common, "access_token", token_source)


@pytest.mark.parametrize("lazy", [False, True])
def test_create_receipt_uses_the_request_gateway_when_legacy_config_changes(gateway, monkeypatch, lazy):
    token = _jwt({"sub": "user-1", "custom:org_id": "org-1"})
    _live_recovery_context(gateway, monkeypatch, lambda: token)
    selected_gateway = [gateway.url]
    monkeypatch.setattr(common, "gateway_url", lambda: selected_gateway[0])
    capability_path = f"/api{cli.DOMAIN_CAPABILITIES}"
    create_path = f"/api{cli.API_BASE}/workspaces"
    gateway.route("GET", capability_path, 200, {"features": ["create-operation-id-v1"]})
    gateway.route("POST", create_path, 503, {"detail": {"error": "unavailable"}})
    requested_urls = []

    def transport():
        api = common.Api()
        original_open = api.opener.open

        def change_configuration_after_read(request, **kwargs):
            requested_urls.append(request.full_url)
            response = original_open(request, **kwargs)
            selected_gateway[0] = "https://different-gateway.example.test/api"
            return response

        api.opener.open = change_configuration_after_read
        return api

    monkeypatch.setattr(cli, "Api", transport)
    api = cli.LazyApi() if lazy else transport()
    with pytest.raises(common.CliError) as raised:
        cli.run(cli.parser().parse_args(["workspace", "create", "--name", "new-ws", "--yes"]), api)
    assert raised.value.code == "create_delivery_uncertain"
    assert requested_urls == [gateway.url + cli.DOMAIN_CAPABILITIES, gateway.url + cli.API_BASE + "/workspaces"]
    receipt = next(iter(cli.create_recoveries().values()))
    assert receipt["recovery_context"]["gateway"] == gateway.url
    assert token not in json.dumps(receipt)
    # A later command binds a new transport without reusing this command's pin.
    context = CURRENT_RECOVERY_CONTEXT(cli.SessionApi(cli.LazyApi()))
    assert context["gateway"] == selected_gateway[0]
    assert gateway.paths() == [capability_path, create_path]


def test_provider_delete_keeps_the_original_token_when_another_terminal_switches(gateway, monkeypatch):
    original_token = _jwt({"sub": "user-1", "custom:org_id": "org-1"})
    switched_token = _jwt({"sub": "user-1", "custom:org_id": "org-2"})
    selected = [original_token]
    reads = []
    _live_recovery_context(gateway, monkeypatch, lambda: reads.append(selected[0]) or selected[0])
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 204)
    gateway.route("DELETE", VAULT_RECORD_PATH, 204)
    api = _api(gateway)
    original_open = api.opener.open
    headers = []

    def switch_after_lookup(request, **kwargs):
        headers.append(request.get_header("Authorization"))
        response = original_open(request, **kwargs)
        selected[0] = switched_token
        return response

    api.opener.open = switch_after_lookup
    result = cli.run(_delete_args(), api)

    assert result["status"] == "ok"
    assert headers == ["Bearer " + original_token] * 3
    assert reads == [original_token]
    assert cli.provider_delete_recoveries() == {}
    # A separate command may use the newly selected session; the pin is local.
    cli.run(cli.parser().parse_args(["provider", "list"]), api)
    assert headers[-1] == "Bearer " + switched_token
    assert reads == [original_token, switched_token]


def test_lost_provider_put_and_receipt_use_the_same_token_before_any_request(gateway, monkeypatch):
    original_token = _jwt({"sub": "user-1", "custom:org_id": "org-1"})
    switched_token = _jwt({"sub": "user-2", "custom:org_id": "org-2"})
    reads = []

    def switch_after_token_read():
        token = original_token if not reads else switched_token
        reads.append(token)
        return token

    _live_recovery_context(gateway, monkeypatch, switch_after_token_read)
    gateway.route("PUT", VAULT_RECORD_PATH, 503, {"detail": {"error": "unavailable"}})
    gateway.route("GET", VAULT_LIST, 200, [])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    api = _api(gateway)
    original_open = api.opener.open
    headers = []

    def capture(request, **kwargs):
        headers.append(request.get_header("Authorization"))
        return original_open(request, **kwargs)

    api.opener.open = capture
    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), api)
    assert raised.value.code == "provider_vault_uncertain"
    assert headers == ["Bearer " + original_token] * 2
    assert reads == [original_token]
    receipt = cli.provider_recoveries()[VAULT_REFERENCE]
    assert receipt["recovery_context"]["principal"] == "user-1"
    assert receipt["recovery_context"]["tenant"] == "org-1"
    assert original_token not in json.dumps(receipt)

    with pytest.raises(common.CliError) as raised:
        cli.run(cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--yes"]), api)
    assert raised.value.code == "provider_recovery_context_mismatch"
    assert headers == ["Bearer " + original_token] * 2
    assert cli.provider_recoveries()[VAULT_REFERENCE] == receipt


def test_expired_pinned_token_does_not_switch_identity_to_finish_provider_cleanup(gateway, monkeypatch):
    original_token = _jwt({"sub": "user-1", "custom:org_id": "org-1"})
    switched_token = _jwt({"sub": "user-1", "custom:org_id": "org-2"})
    reads = []
    _live_recovery_context(gateway, monkeypatch, lambda: reads.append(True) or (original_token if len(reads) == 1 else switched_token))
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 204)
    gateway.route("DELETE", VAULT_RECORD_PATH, 401, {"detail": {"error": "expired_token"}})
    api = _api(gateway)
    original_open = api.opener.open
    headers = []

    def capture(request, **kwargs):
        headers.append(request.get_header("Authorization"))
        return original_open(request, **kwargs)

    api.opener.open = capture
    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), api)
    assert raised.value.code == "provider_delete_incomplete"
    assert len(reads) == 1
    assert headers == ["Bearer " + original_token] * 3
    assert cli.provider_delete_recoveries()[DOMAIN_RECORD]["recovery_context"]["tenant"] == "org-1"


# --- the transport itself ----------------------------------------------------


def test_a_bodiless_204_is_a_success_not_an_unreachable_gateway(gateway) -> None:
    """The #5039 defect: ADP's vault delete answers 204 with no body.

    Before the fix this raised CliError(gateway_unavailable) even though the
    server had received the DELETE and acted on it.
    """
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)

    assert _api(gateway).request("DELETE", f"{cli.VAULT_CREDENTIALS}/{VAULT_REFERENCE}") == {}
    assert [(row["method"], row["path"]) for row in gateway.seen] == [("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}")]


def test_an_empty_body_on_a_200_is_still_a_failure(gateway) -> None:
    """The fix must not become "empty body means success".

    A 200 promising JSON and delivering nothing is a broken response, not an
    empty success — otherwise a truncated reply would read as ok.
    """
    gateway.route("GET", f"/api{cli.API_BASE}/workspaces", 200, None)

    with pytest.raises(common.CliError) as raised:
        _api(gateway).request("GET", cli.API_BASE + "/workspaces")
    assert raised.value.code == "gateway_unavailable"


def test_malformed_json_is_still_a_failure(gateway) -> None:
    """Narrowly scoped: only 204/205 may be bodiless, and JSON must still parse."""
    api = _api(gateway)
    gateway.route("GET", f"/api{cli.API_BASE}/workspaces", 200, "not-a-json-object-{")
    # A JSON string parses, so force genuinely invalid bytes instead.
    original = api.opener.open

    class _Truncated(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    api.opener.open = lambda *a, **k: _Truncated(b'{"workspaces": [')
    try:
        with pytest.raises(common.CliError) as raised:
            api.request("GET", cli.API_BASE + "/workspaces")
    finally:
        api.opener.open = original
    assert raised.value.code == "gateway_unavailable"


def test_an_unreachable_gateway_is_still_reported(gateway) -> None:
    """A genuine network failure must stay distinguishable from an empty success."""
    api = _api(gateway)
    gateway.close()

    with pytest.raises(common.CliError) as raised:
        api.request("GET", cli.API_BASE + "/workspaces")
    assert raised.value.code == "gateway_unavailable"


# --- the routes exist: the #5637 defect, at the fixture ------------------------


def test_the_invented_provider_routes_are_not_forwardable() -> None:
    """`/providers` was never a route. It must fail the fixture's own guard.

    This is the check that makes the rest of the file trustworthy: if a later
    change reintroduces those paths, the test that stubs them cannot be written
    without tripping this.
    """
    assert not forwardable("GET", f"/api{cli.API_BASE}/providers")
    assert not forwardable("POST", f"/api{cli.API_BASE}/providers")
    assert not forwardable("DELETE", f"/api{cli.API_BASE}/providers/cred-1")
    # And the real ones are.
    assert forwardable("GET", DOMAIN_LIST)
    assert forwardable("POST", DOMAIN_LIST)
    assert forwardable("DELETE", DOMAIN_RECORD_PATH)


def test_provider_list_reads_the_domain_credential_registry(gateway) -> None:
    _registered(gateway)

    result = cli.run(cli.parser().parse_args(["provider", "list"]), _api(gateway))

    assert gateway.paths() == [DOMAIN_LIST]
    assert result["detail"]["providers"][0]["adp_credential_id"] == VAULT_REFERENCE
    assert result["detail"]["total"] == 1


# --- provider delete: two ids, two non-atomic steps ---------------------------


def _delete_args(credential_id: str = DOMAIN_RECORD):
    return cli.parser().parse_args(["provider", "delete", credential_id, "--yes"])


def test_delete_sends_the_record_id_to_the_domain_and_the_reference_to_the_vault(gateway) -> None:
    """The central #5637 defect: one value was sent to both endpoints.

    The domain row's id and the vault reference it points at are different
    identifiers. Using the record id against ADP's vault deletes nothing there,
    so the secret survived while the command reported success.
    """
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 200, {"id": DOMAIN_RECORD, "status": "Deleted"})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)

    result = cli.run(_delete_args(), _api(gateway))

    assert result["status"] == "ok"
    assert result["detail"]["domain_metadata"] == "deleted"
    assert result["detail"]["vault_credential"] == "deleted"
    # Read first, then delete each store with ITS OWN identifier, in that order.
    assert gateway.paths() == [DOMAIN_LIST, DOMAIN_RECORD_PATH, f"{VAULT_LIST}/{VAULT_REFERENCE}"]


def test_delete_accepts_the_vault_reference_as_well_as_the_record_id(gateway) -> None:
    """Either id a user has in hand resolves to the same pair of deletes.

    `provider add` prints the vault reference, so that is frequently the only id
    the operator kept.
    """
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 200, {"id": DOMAIN_RECORD})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)

    result = cli.run(_delete_args(VAULT_REFERENCE), _api(gateway))

    assert result["status"] == "ok"
    assert gateway.paths("DELETE") == [DOMAIN_RECORD_PATH, f"{VAULT_LIST}/{VAULT_REFERENCE}"]


def test_a_bodiless_domain_delete_still_reaches_the_vault(gateway) -> None:
    """#5039's orphaning path: a 204 from the domain used to abort the run.

    The parse raised, so the vault delete never ran and the secret was orphaned —
    with the CLI reporting an unreachable gateway.
    """
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 204)
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)

    result = cli.run(_delete_args(), _api(gateway))

    assert result["status"] == "ok"
    assert f"{VAULT_LIST}/{VAULT_REFERENCE}" in gateway.paths()


def test_a_race_on_the_domain_record_still_finishes_the_vault_delete(gateway) -> None:
    """Recoverability: the row was listed, then gone by the time we deleted it.

    Stopping at that 404 would leave the secret in the vault while the domain row
    is already absent — and `provider list` would no longer show it, so no CLI
    path would remain to remove it.
    """
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 404, {"detail": "Credential not found"})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)

    result = cli.run(_delete_args(), _api(gateway))

    assert result["status"] == "ok"
    assert result["detail"]["domain_metadata"] == "already_absent"
    assert result["detail"]["vault_credential"] == "deleted"
    assert f"{VAULT_LIST}/{VAULT_REFERENCE}" in gateway.paths()


def test_an_unknown_identifier_deletes_nothing_at_all(gateway) -> None:
    """The unrelated-deletion guard (AC-02).

    With no matching record there is no reference to act on, and guessing that the
    argument IS a vault reference is exactly how another party's credential gets
    deleted. So it must refuse, having sent no DELETE anywhere — including to the
    unrelated credential that a guess would have hit.
    """
    gateway.route("GET", DOMAIN_LIST, 200, {"credentials": [], "total": 0})
    gateway.route("DELETE", f"{VAULT_LIST}/{UNRELATED_VAULT_REFERENCE}", 204)

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(UNRELATED_VAULT_REFERENCE), _api(gateway))

    assert raised.value.code == "provider_not_found"
    assert gateway.paths("DELETE") == [], "nothing may be deleted when the credential cannot be attributed"


def test_an_ambiguous_identifier_deletes_nothing(gateway) -> None:
    """Two rows matching one name must not be resolved by picking either."""
    gateway.route(
        "GET",
        DOMAIN_LIST,
        200,
        {
            "credentials": [
                {"id": DOMAIN_RECORD, "adp_credential_id": VAULT_REFERENCE},
                {"id": "99999999-8888-7777-6666-555555555555", "adp_credential_id": VAULT_REFERENCE},
            ],
            "total": 2,
        },
    )

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(VAULT_REFERENCE), _api(gateway))

    assert raised.value.code == "provider_ambiguous"
    assert gateway.paths("DELETE") == []


def test_a_record_with_no_reference_is_non_mutating(gateway) -> None:
    """Malformed metadata cannot discard the only pointer to a live vault secret."""
    _registered(gateway, adp_credential_id=None)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 204)
    gateway.route("DELETE", f"{VAULT_LIST}/{UNRELATED_VAULT_REFERENCE}", 204)

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))

    assert raised.value.code == "malformed_response"
    assert gateway.paths("DELETE") == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"credentials": {}, "total": 0},
        {"credentials": [_domain_response(id="not-a-uuid")], "total": 1},
        {"credentials": [42], "total": 1},
        {"credentials": [_domain_response()], "total": 0},
    ],
)
def test_malformed_credential_lists_are_non_mutating(gateway, response) -> None:
    gateway.route("GET", DOMAIN_LIST, 200, response)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 204)
    gateway.route("DELETE", VAULT_RECORD_PATH, 204)

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))

    assert raised.value.code == "malformed_response"
    assert gateway.paths("DELETE") == []


def test_malformed_reconciliation_keeps_the_delete_receipt(gateway) -> None:
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 500, {"detail": "response lost"})

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))
    assert raised.value.code == "provider_delete_uncertain"

    gateway.route("GET", DOMAIN_LIST, 200, {"credentials": []})
    with pytest.raises(common.CliError) as retried:
        cli.run(_delete_args(), _api(gateway))

    assert retried.value.code == "malformed_response"
    assert cli.provider_delete_recoveries()[DOMAIN_RECORD]["adp_credential_id"] == VAULT_REFERENCE
    assert VAULT_RECORD_PATH not in gateway.paths("DELETE")


def test_malformed_registration_success_keeps_the_add_receipt(gateway, monkeypatch) -> None:
    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    gateway.route("POST", DOMAIN_LIST, 201, {"id": DOMAIN_RECORD})
    gateway.route("GET", DOMAIN_LIST, 200, {"credentials": []})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "provider_add_uncertain"
    assert cli.provider_recoveries()[VAULT_REFERENCE]["phase"] == "domain_pending"
    assert gateway.paths("DELETE") == []


def test_a_vault_failure_stays_visible_and_actionable(gateway) -> None:
    """A real vault failure must not be swallowed by the 204 tolerance."""
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 200, {"id": DOMAIN_RECORD})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 500, {"detail": {"error": "delete_failed"}})

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))

    assert raised.value.code == "provider_delete_incomplete"
    assert raised.value.exit_code == 5
    message = str(raised.value)
    # It must say the secret may survive, name the id to remove, and say how.
    assert "may still exist" in message
    assert VAULT_REFERENCE in message


def test_a_domain_5xx_is_uncertain_and_stops_before_the_vault(gateway) -> None:
    """A 5xx may follow commit, so it retains recovery state and leaves the vault intact."""
    _registered(gateway)
    gateway.route("DELETE", DOMAIN_RECORD_PATH, 500, {"detail": {"error": "delete_failed"}})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))

    assert raised.value.code == "provider_delete_uncertain"
    # The vault delete must not have run: the domain still references the secret.
    assert f"{VAULT_LIST}/{VAULT_REFERENCE}" not in gateway.paths()


def test_a_credential_still_bound_to_an_account_is_refused_by_the_domain(gateway) -> None:
    """The domain answers 409 while a credential is in use (routers/accounts.py).

    The CLI must surface that and leave the vault alone — the secret is still
    needed by whatever holds the binding.
    """
    _registered(gateway)
    gateway.route(
        "DELETE",
        DOMAIN_RECORD_PATH,
        409,
        {"detail": "credential is still in use; disable or replace its connections and detach cluster assignments first"},
    )
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))

    assert raised.value.code == "http_error"
    assert raised.value.status_code == 409
    assert f"{VAULT_LIST}/{VAULT_REFERENCE}" not in gateway.paths()


@pytest.mark.parametrize(
    "deployment_status,payload,expected",
    [
        (200, {"name": "dep-1", "status": "Deleted"}, "ok"),
        (200, {"name": "dep-1", "status": "Deleting"}, "pending"),
        (204, None, "pending"),
    ],
)
def test_the_other_delete_verbs_work_over_the_real_transport(gateway, deployment_status, payload, expected) -> None:
    """UUID deletes survive bodyless replies and report pending state honestly."""
    account_record = "33333333-4444-5555-6666-777777777777"
    deployment_record = "44444444-5555-6666-7777-888888888888"
    gateway.route("DELETE", f"/api{cli.API_BASE}/accounts/{account_record}", 204)
    target = f"/api{cli.API_BASE}/workspaces/{WORKSPACE}/deployments/{deployment_record}"
    gateway.route("DELETE", target, deployment_status, payload)

    account = cli.run(cli.parser().parse_args(["account", "delete", account_record, "--yes"]), _api(gateway))
    deployment = cli.run(
        cli.parser().parse_args(["deploy", "delete", "--id", deployment_record, "--workspace", WORKSPACE, "--yes"]),
        _api(gateway),
    )

    assert account["status"] == "ok"
    assert deployment["status"] == expected
    assert target in gateway.paths("DELETE")


def test_account_delete_resolves_the_cloud_number_to_the_record_uuid(gateway) -> None:
    """DELETE /accounts/{account_id} takes `uuid.UUID` — not the 12-digit number.

    Sending the cloud account number was a 422 (#5637). The number stays
    acceptable at the CLI, resolved through the list the route does serve.
    """
    account_record = "33333333-4444-5555-6666-777777777777"
    gateway.route(
        "GET",
        f"/api{cli.API_BASE}/accounts",
        200,
        {"accounts": [{"id": account_record, "account_id": "123456789012", "name": "prod"}], "total": 1},
    )
    gateway.route("DELETE", f"/api{cli.API_BASE}/accounts/{account_record}", 204)

    result = cli.run(cli.parser().parse_args(["account", "delete", "123456789012", "--yes"]), _api(gateway))

    assert result["status"] == "ok"
    assert gateway.paths("DELETE") == [f"/api{cli.API_BASE}/accounts/{account_record}"]


# --- provider add: the real stdin path and the real two-stage write -----------


def _add_args(credential_type: str = "api_key"):
    return cli.parser().parse_args(["provider", "add", "--name", "prod", "--provider", "nebius", "--type", credential_type, "--stdin", "--yes"])


def _vault_body(gateway) -> dict:
    bodies = [row["body"] for row in gateway.seen if row["path"] == VAULT_RECORD_PATH and row["method"] == "PUT"]
    assert bodies, "the vault was never called"
    return bodies[0]


def _domain_body(gateway) -> dict:
    bodies = [row["body"] for row in gateway.seen if row["path"] == DOMAIN_LIST and row["method"] == "POST"]
    assert bodies, "the domain registry was never called"
    return bodies[0]


def _both_writes_succeed(gateway, credential_type="api_key") -> None:
    response = _vault_response()
    response["credential_type"] = credential_type
    gateway.route("PUT", VAULT_RECORD_PATH, 201, response)
    gateway.route(
        "POST",
        DOMAIN_LIST,
        201,
        _domain_response(credential_type=cli.DOMAIN_CREDENTIAL_TYPES[credential_type]),
    )


def test_a_multiline_credential_reaches_the_vault_whole(gateway, monkeypatch) -> None:
    """#5039's truncation defect, asserted where it showed: the vault body.

    `readline()` sent "{" and the command still reported ok. Only the server's
    view of the request catches that — the CLI's own envelope looked fine.
    """
    _both_writes_succeed(gateway, "config_file")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SERVICE_ACCOUNT_KEY))

    result = cli.run(_add_args("config_file"), _api(gateway))

    assert _vault_body(gateway)["value"] == SERVICE_ACCOUNT_KEY.rstrip("\n")
    # And it must still be valid JSON on arrival — the point of config_file.
    assert json.loads(_vault_body(gateway)["value"])["project_id"] == "synthetic-example"
    assert result["detail"]["adp_credential_id"] == VAULT_REFERENCE
    assert result["detail"]["domain_record"] == DOMAIN_RECORD


def test_the_domain_registration_carries_the_reference_and_a_type_it_accepts(gateway, monkeypatch) -> None:
    """The second write's body is the domain's, not this helper's invention.

    `RegisterCredentialRequest` takes `name`, `provider`, `credential_type` and
    `adp_credential_id`; the helper's old `credential_id` was not a field at all.
    Its `credential_type` pattern is also narrower than ADP's vault enum, so
    `config_file` must be translated to `service_account` — untranslated it would
    be a 422 AFTER the secret was already stored.
    """
    _both_writes_succeed(gateway, "config_file")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SERVICE_ACCOUNT_KEY))

    cli.run(_add_args("config_file"), _api(gateway))

    body = _domain_body(gateway)
    assert body == {
        "name": "prod",
        "provider": "nebius",
        "credential_type": "service_account",
        "adp_credential_id": VAULT_REFERENCE,
    }
    # ADP's vault keeps its OWN vocabulary for the same credential.
    assert _vault_body(gateway)["credential_type"] == "config_file"


def test_a_single_line_secret_loses_its_trailing_newline_only(gateway, monkeypatch) -> None:
    """`echo secret | adp ... --stdin` must not store the newline."""
    _both_writes_succeed(gateway)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    cli.run(_add_args(), _api(gateway))

    assert _vault_body(gateway)["value"] == SINGLE_LINE_SECRET


def test_interior_and_leading_whitespace_are_preserved(gateway, monkeypatch) -> None:
    """An indented PEM-style block must arrive byte-identical apart from the tail."""
    pem = "-----BEGIN KEY-----\n  aGVsbG8=\n  d29ybGQ=\n-----END KEY-----"
    _both_writes_succeed(gateway, "config_file")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(pem + "\n"))

    cli.run(_add_args("config_file"), _api(gateway))

    assert _vault_body(gateway)["value"] == pem


def test_empty_stdin_stores_nothing(gateway, monkeypatch) -> None:
    _both_writes_succeed(gateway)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "usage_error"
    assert gateway.seen == [], "nothing may be sent when no value was supplied"


def test_whitespace_only_stdin_stores_nothing(gateway, monkeypatch) -> None:
    """Blank lines are not a credential."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n  \n\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "usage_error"
    assert gateway.seen == []


# --- AC-02: the second stage fails, and only what this run made is undone -----


def test_a_failed_registration_removes_only_the_credential_this_run_created(gateway, monkeypatch) -> None:
    """AC-02's core: compensate the owned write, and nothing else.

    The vault write succeeded, so an untracked secret exists that `provider list`
    will never show. The command must remove it — and it must do so by the id the
    vault returned in THIS process, never by searching for something with a
    matching label, because a retried add would then delete a credential another
    run or another person owns.
    """
    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    gateway.route("POST", DOMAIN_LIST, 422, {"detail": {"error": "validation_error"}})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)
    gateway.route("DELETE", f"{VAULT_LIST}/{UNRELATED_VAULT_REFERENCE}", 204)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "provider_add_rolled_back"
    assert raised.value.exit_code == 5
    # Exactly one delete, aimed at this run's own credential.
    assert gateway.paths("DELETE") == [f"{VAULT_LIST}/{VAULT_REFERENCE}"]
    # It must also say that nothing was left behind, so a retry is safe.
    assert "safe to retry" in str(raised.value)
    # No lookup was performed to decide what to delete — a list call is how a
    # label-matching cleanup would have found its victim.
    assert gateway.paths("GET") == []


def test_a_committed_registration_with_a_lost_response_is_reconciled_without_deleting_the_vault(gateway, monkeypatch) -> None:
    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    gateway.route("POST", DOMAIN_LIST, 201, _domain_response())
    _registered(gateway)
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    api = _api(gateway)
    original = api.opener.open

    class Truncated(io.BytesIO):
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def lose_domain_response(request, **kwargs):
        response = original(request, **kwargs)
        if request.get_method() == "POST" and request.full_url.endswith(DOMAIN_LIST):
            with response:
                response.read()
            return Truncated(b'{"id":')
        return response

    api.opener.open = lose_domain_response
    result = cli.run(_add_args(), api)

    assert result["detail"]["reconciled"] is True
    assert gateway.paths("DELETE") == []
    assert gateway.paths("PUT") == [VAULT_RECORD_PATH]
    assert gateway.paths("POST") == [DOMAIN_LIST]
    assert cli.provider_recoveries() == {}


def test_a_malformed_first_vault_success_is_reconciled_by_its_exact_operation_id(gateway, monkeypatch) -> None:
    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    gateway.route(
        "GET",
        VAULT_LIST,
        200,
        [_vault_response()],
    )
    gateway.route("POST", DOMAIN_LIST, 201, _domain_response())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    api = _api(gateway)
    original = api.opener.open

    class Truncated(io.BytesIO):
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def lose_vault_response(request, **kwargs):
        response = original(request, **kwargs)
        if request.get_method() == "PUT" and request.full_url.endswith(VAULT_RECORD_PATH):
            with response:
                response.read()
            return Truncated(b'{"id":')
        return response

    api.opener.open = lose_vault_response
    result = cli.run(_add_args(), api)

    assert result["detail"]["adp_credential_id"] == VAULT_REFERENCE
    assert gateway.paths("PUT") == [VAULT_RECORD_PATH]
    assert gateway.paths("GET") == [VAULT_LIST]
    assert cli.provider_recoveries() == {}


def test_failed_server_cleanup_retains_the_exact_vault_recovery_receipt(gateway, monkeypatch) -> None:
    gateway.route(
        "PUT",
        VAULT_RECORD_PATH,
        500,
        {"detail": {"error": "create_failed"}},
    )
    gateway.route("GET", VAULT_LIST, 200, [])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "provider_vault_uncertain"
    assert gateway.paths() == [VAULT_RECORD_PATH, VAULT_LIST]
    assert cli.provider_recoveries()[VAULT_REFERENCE]["phase"] == "vault_pending"


@pytest.mark.parametrize("retry_status", [403, 404, 405, 422])
def test_uncertain_vault_write_keeps_its_receipt_when_recovery_is_later_refused(gateway, monkeypatch, retry_status):
    gateway.route("PUT", VAULT_RECORD_PATH, 503, {"detail": {"error": "unavailable"}})
    gateway.route("GET", VAULT_LIST, 200, [])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))
    assert raised.value.code == "provider_vault_uncertain"
    receipt = cli.provider_recoveries()[VAULT_REFERENCE]
    gateway.route("PUT", VAULT_RECORD_PATH, retry_status, {"detail": {"error": "unavailable"}})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError):
        cli.run(
            cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--stdin", "--yes"]),
            _api(gateway),
        )

    assert cli.provider_recoveries()[VAULT_REFERENCE] == receipt
    assert gateway.paths("PUT") == [VAULT_RECORD_PATH] * 2
    assert gateway.paths("DELETE") == []
    assert gateway.paths("POST") == []


@pytest.mark.parametrize("metadata_visible", [False, True])
def test_a_vault_conflict_retains_its_receipt_and_replays_only_the_same_operation(gateway, monkeypatch, metadata_visible) -> None:
    gateway.route(
        "PUT",
        VAULT_RECORD_PATH,
        409,
        {"detail": {"error": "operation_conflict"}},
    )
    gateway.route("GET", VAULT_LIST, 200, [_vault_response()] if metadata_visible else [])
    gateway.route("GET", DOMAIN_LIST, 200, {"credentials": [], "total": 0})
    gateway.route("POST", DOMAIN_LIST, 201, _domain_response())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "provider_vault_conflict"
    assert f"--recover {VAULT_REFERENCE} --stdin --yes" in str(raised.value)
    assert gateway.paths() == [VAULT_RECORD_PATH]
    assert cli.provider_recoveries()[VAULT_REFERENCE]["phase"] == "vault_conflict"

    def fresh_identity():
        raise AssertionError("recovery must not generate another operation UUID")

    monkeypatch.setattr(cli.uuid, "uuid4", fresh_identity)
    recover = ["provider", "add", "--recover", VAULT_REFERENCE, "--yes"]
    with pytest.raises(common.CliError) as raised:
        cli.run(cli.parser().parse_args(recover), _api(gateway))
    assert raised.value.code == "provider_secret_required"
    assert gateway.paths("POST") == []
    assert cli.provider_recoveries()[VAULT_REFERENCE]["phase"] == "vault_conflict"

    # Even visible metadata does not prove a rejected secret matched. A replay
    # must obtain a successful PUT before registering the reference.
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    with pytest.raises(common.CliError) as raised:
        cli.run(cli.parser().parse_args([*recover, "--stdin"]), _api(gateway))
    assert raised.value.code == "provider_vault_conflict"
    assert cli.provider_recoveries()[VAULT_REFERENCE]["phase"] == "vault_conflict"
    assert gateway.paths("POST") == []
    assert gateway.paths("DELETE") == []

    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    result = cli.run(cli.parser().parse_args([*recover, "--stdin"]), _api(gateway))

    assert result["detail"]["adp_credential_id"] == VAULT_REFERENCE
    assert gateway.paths("PUT") == [VAULT_RECORD_PATH] * 3
    assert gateway.paths("POST") == [DOMAIN_LIST]
    assert gateway.paths("DELETE") == []
    assert all(row["body"]["value"] == SINGLE_LINE_SECRET for row in gateway.seen if row["method"] == "PUT")
    assert cli.provider_recoveries() == {}


@pytest.mark.parametrize("replay_status", [403, 404, 405, 503])
def test_a_failed_conflict_replay_keeps_the_existing_receipt_even_with_metadata(gateway, monkeypatch, replay_status) -> None:
    receipt = {**_receipt(VAULT_REFERENCE, "prod"), "phase": "vault_conflict"}
    cli.save_provider_recovery(receipt)
    gateway.route("GET", VAULT_LIST, 200, [_vault_response()])
    gateway.route("PUT", VAULT_RECORD_PATH, replay_status, {"detail": {"error": "unavailable"}})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError):
        cli.run(
            cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--stdin", "--yes"]),
            _api(gateway),
        )

    assert cli.provider_recoveries()[VAULT_REFERENCE] == receipt
    assert gateway.paths("PUT") == [VAULT_RECORD_PATH]
    assert gateway.paths("POST") == []
    assert gateway.paths("DELETE") == []


def test_a_lost_first_vault_response_retries_the_same_id_without_label_lookup(gateway, monkeypatch) -> None:
    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    gateway.route("GET", VAULT_LIST, 500, {"detail": {"error": "unavailable"}})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    api = _api(gateway)
    original = api.opener.open
    lost = True

    def lose_once(request, **kwargs):
        nonlocal lost
        response = original(request, **kwargs)
        if lost and request.get_method() == "PUT":
            lost = False
            with response:
                response.read()
            raise TimeoutError
        return response

    api.opener.open = lose_once
    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), api)
    assert raised.value.code == "provider_vault_uncertain"
    assert cli.provider_recoveries()[VAULT_REFERENCE]["phase"] == "vault_pending"

    gateway.route("GET", VAULT_LIST, 200, [])
    gateway.route("GET", DOMAIN_LIST, 200, {"credentials": [], "total": 0})
    gateway.route("POST", DOMAIN_LIST, 201, _domain_response())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
    result = cli.run(
        cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--stdin", "--yes"]),
        _api(gateway),
    )

    assert result["detail"]["adp_credential_id"] == VAULT_REFERENCE
    assert gateway.paths("PUT") == [VAULT_RECORD_PATH, VAULT_RECORD_PATH]
    assert cli.provider_recoveries() == {}


def test_an_uncertain_registration_retains_a_receipt_and_retries_the_same_reference(gateway, monkeypatch) -> None:
    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    gateway.route("POST", DOMAIN_LIST, 500, {"detail": {"error": "upstream_lost"}})
    gateway.route("GET", DOMAIN_LIST, 200, {"credentials": [], "total": 0})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "provider_add_uncertain"
    assert gateway.paths("DELETE") == []
    assert cli.provider_recoveries()[VAULT_REFERENCE]["name"] == "prod"

    _registered(gateway)
    gateway.route(
        "GET",
        VAULT_LIST,
        200,
        [_vault_response()],
    )
    result = cli.run(
        cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--yes"]),
        _api(gateway),
    )
    assert result["detail"]["reconciled"] is True
    assert cli.provider_recoveries() == {}


@pytest.mark.parametrize(
    "changed",
    [
        {"deployment_id": "deployment-2", "deployment": "other"},
        {"principal": "user-2", "tenant": "org-2"},
    ],
    ids=["deployment", "tenant-and-principal"],
)
def test_add_recovery_refuses_a_different_context_without_a_request(gateway, monkeypatch, changed) -> None:
    cli.save_provider_recovery(_receipt(VAULT_REFERENCE, "prod"))
    monkeypatch.setattr(cli, "current_recovery_context", lambda api=None: {**RECOVERY_CONTEXT, **changed})

    with pytest.raises(common.CliError) as raised:
        cli.run(
            cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--yes"]),
            _api(gateway),
        )

    assert raised.value.code == "provider_recovery_context_mismatch"
    assert gateway.paths() == []
    assert cli.provider_recoveries()[VAULT_REFERENCE]["name"] == "prod"


def test_delete_recovery_refuses_a_different_tenant_without_a_request(gateway, monkeypatch) -> None:
    receipt = {
        "domain_record": DOMAIN_RECORD,
        "adp_credential_id": VAULT_REFERENCE,
        "requested_as": DOMAIN_RECORD,
        "recovery_context": dict(RECOVERY_CONTEXT),
    }
    cli.save_provider_delete_recovery(receipt)
    monkeypatch.setattr(cli, "current_recovery_context", lambda api=None: {**RECOVERY_CONTEXT, "tenant": "org-2"})

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))

    assert raised.value.code == "provider_recovery_context_mismatch"
    assert gateway.paths() == []
    assert cli.provider_delete_recoveries()[DOMAIN_RECORD] == receipt


@pytest.mark.parametrize("identifier", [DOMAIN_RECORD, VAULT_REFERENCE], ids=["domain-record", "vault-reference"])
def test_delete_recovery_refuses_a_re_registered_vault_reference(gateway, identifier) -> None:
    receipt = {
        "domain_record": DOMAIN_RECORD,
        "adp_credential_id": VAULT_REFERENCE,
        "requested_as": identifier,
        "recovery_context": dict(RECOVERY_CONTEXT),
    }
    replacement_record = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    cli.save_provider_delete_recovery(receipt)
    gateway.route(
        "GET",
        DOMAIN_LIST,
        200,
        {
            "credentials": [
                _domain_response(
                    id=replacement_record,
                    adp_credential_id=VAULT_REFERENCE,
                )
            ],
            "total": 1,
        },
    )

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(identifier), _api(gateway))

    assert raised.value.code == "provider_recovery_conflict"
    assert gateway.paths() == [DOMAIN_LIST]
    assert cli.provider_delete_recoveries()[DOMAIN_RECORD] == receipt


def test_unbound_legacy_recovery_is_retained_without_a_request(gateway) -> None:
    receipt = _receipt(VAULT_REFERENCE, "prod")
    receipt.pop("recovery_context")
    cli.save_provider_recovery(receipt)

    with pytest.raises(common.CliError) as raised:
        cli.run(
            cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--yes"]),
            _api(gateway),
        )

    assert raised.value.code == "provider_recovery_context_missing"
    assert gateway.paths() == []
    assert cli.provider_recoveries()[VAULT_REFERENCE] == receipt


def test_recovery_refuses_an_exact_id_with_different_metadata(gateway) -> None:
    cli.save_provider_recovery(_receipt(VAULT_REFERENCE, "prod"))
    gateway.route("GET", VAULT_LIST, 200, [_vault_response(label="somebody-elses-label")])

    with pytest.raises(common.CliError) as raised:
        cli.run(
            cli.parser().parse_args(["provider", "add", "--recover", VAULT_REFERENCE, "--yes"]),
            _api(gateway),
        )

    assert raised.value.code == "provider_recovery_conflict"
    assert gateway.paths() == [VAULT_LIST]
    assert cli.provider_recoveries()[VAULT_REFERENCE]["name"] == "prod"


def test_a_lost_domain_delete_response_never_blindly_deletes_the_vault_and_retry_finishes(gateway) -> None:
    class LostDeleteApi:
        def __init__(self):
            self.record_present = True
            self.deleted = []

        def request(self, method, path, body=None, **kwargs):
            if method == "GET":
                records = [_domain_response()] if self.record_present else []
                return {"credentials": records, "total": len(records)}
            if method == "DELETE" and path.endswith(DOMAIN_RECORD):
                self.record_present = False
                raise common.CliError("response lost", "gateway_unavailable")
            if method == "DELETE":
                self.deleted.append(path)
                return {}
            raise AssertionError((method, path))

    api = LostDeleteApi()
    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), api)
    assert raised.value.code == "provider_delete_uncertain"
    assert api.deleted == []

    result = cli.run(_delete_args(), api)
    assert result["detail"]["domain_metadata"] == "already_absent"
    assert api.deleted == [f"{cli.VAULT_CREDENTIALS}/{VAULT_REFERENCE}"]


def test_a_failed_compensation_leaves_a_receipt_naming_the_exact_id(gateway, monkeypatch) -> None:
    """AC-02's recovery receipt: the worst outcome is a silent "ok" over a leak.

    Registration failed AND the cleanup failed, so a secret really is stranded.
    The command must exit non-zero and name the id to remove — without echoing
    the value.
    """
    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    gateway.route("POST", DOMAIN_LIST, 422, {"detail": {"error": "register_failed"}})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 500, {"detail": {"error": "delete_failed"}})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    message = str(raised.value)
    assert raised.value.code == "provider_add_incomplete"
    assert raised.value.exit_code == 5
    assert VAULT_REFERENCE in message, "the receipt must name the id an operator has to remove"
    assert SINGLE_LINE_SECRET not in message, "a receipt must never carry the value"


def test_a_retry_after_a_rollback_touches_only_its_own_new_credential(gateway, monkeypatch) -> None:
    """AC-02's retry clause: retries cannot orphan or delete unrelated credentials.

    The second attempt gets a different vault id, because the first attempt's was
    deleted. If cleanup were keyed on anything but that returned id — the label,
    the provider, the newest row — this is where it would reach across runs.
    """
    second_reference = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    gateway.route("POST", DOMAIN_LIST, 422, {"detail": {"error": "validation_error"}})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)
    gateway.route("DELETE", f"{VAULT_LIST}/{second_reference}", 204)
    gateway.route("DELETE", f"{VAULT_LIST}/{UNRELATED_VAULT_REFERENCE}", 204)

    references = iter((VAULT_REFERENCE, second_reference))
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(next(references)))
    for reference in (VAULT_REFERENCE, second_reference):
        gateway.route("PUT", f"{VAULT_LIST}/{reference}", 201, _vault_response(reference))
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))
        with pytest.raises(common.CliError):
            cli.run(_add_args(), _api(gateway))

    assert gateway.paths("DELETE") == [f"{VAULT_LIST}/{VAULT_REFERENCE}", f"{VAULT_LIST}/{second_reference}"]


def test_a_vault_write_that_returns_no_id_registers_nothing(gateway, monkeypatch) -> None:
    """Without an id there is no reference to register and none to clean up.

    Registering with an empty reference would create a domain row pointing at
    nothing, which `provider delete` could then never complete.
    """
    gateway.route("PUT", VAULT_RECORD_PATH, 201, {"status": "created"})
    gateway.route("POST", DOMAIN_LIST, 201, {"id": DOMAIN_RECORD})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "provider_vault_uncertain"
    assert gateway.paths("PUT") == [VAULT_RECORD_PATH]
    assert gateway.paths("POST") == []
    assert cli.provider_recoveries()[VAULT_REFERENCE]["phase"] == "vault_pending"


def test_an_old_vault_without_idempotent_put_fails_before_any_secret_write(gateway, monkeypatch) -> None:
    gateway.route("PUT", VAULT_RECORD_PATH, 405, {"detail": "Method Not Allowed"})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args(), _api(gateway))

    assert raised.value.code == "vault_idempotency_unavailable"
    assert raised.value.exit_code == 4
    assert gateway.paths() == [VAULT_RECORD_PATH]
    assert cli.provider_recoveries() == {}


def test_concurrent_recovery_updates_preserve_every_receipt_and_workspace(gateway) -> None:
    second = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    third = "12345678-1234-4123-8123-123456789abc"
    delete_receipt = {"domain_record": DOMAIN_RECORD, "adp_credential_id": VAULT_REFERENCE}

    def together(*calls):
        barrier = threading.Barrier(len(calls))

        def invoke(call):
            barrier.wait()
            call()

        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            list(pool.map(invoke, calls))

    together(
        lambda: cli.save_provider_recovery(_receipt(VAULT_REFERENCE, "first")),
        lambda: cli.save_provider_recovery(_receipt(second, "second")),
    )
    assert set(cli.provider_recoveries()) == {VAULT_REFERENCE, second}

    together(
        lambda: cli.save_provider_delete_recovery(delete_receipt),
        lambda: cli.save_provider_recovery(_receipt(third, "third")),
    )
    assert DOMAIN_RECORD in cli.provider_delete_recoveries()
    assert third in cli.provider_recoveries()

    together(
        lambda: cli.clear_provider_recovery(VAULT_REFERENCE),
        lambda: cli.save_provider_recovery(_receipt("fedcba98-7654-4321-8765-abcdefabcdef", "fourth")),
        lambda: cli.update_state(lambda state: state.update(workspace="concurrent-workspace")),
    )
    state = cli.common.read_state(cli.STATE)
    assert VAULT_REFERENCE not in cli.provider_recoveries()
    assert state["workspace"] == "concurrent-workspace"
    assert DOMAIN_RECORD in cli.provider_delete_recoveries()


# --- the protections that must survive the fix -------------------------------


def test_the_multiline_value_never_reaches_argv_output_or_the_url(gateway, monkeypatch, capsys) -> None:
    """Reading more of stdin must not widen the leak surface.

    The value may appear in exactly one place: the idempotent PUT body to ADP's vault.
    """
    _both_writes_succeed(gateway, "config_file")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SERVICE_ACCOUNT_KEY))

    result = cli.run(_add_args("config_file"), _api(gateway))
    captured = capsys.readouterr()
    secret = SERVICE_ACCOUNT_KEY.rstrip("\n")

    # Not in any URL the server saw, query strings included.
    assert all(secret not in row["path"] + row["query"] for row in gateway.seen)
    # Not in the domain registration — only the reference crosses over.
    domain = [row for row in gateway.seen if row["path"] == DOMAIN_LIST and row["method"] == "POST"]
    assert domain and domain[0]["body"]["adp_credential_id"] == VAULT_REFERENCE
    assert all(secret not in (row["raw"] or "") for row in domain)
    # Not in the CLI's own output, on either stream, nor in the returned envelope.
    assert secret not in captured.out and secret not in captured.err
    assert secret not in json.dumps(result)


def test_no_local_copy_of_the_multiline_value_is_written(gateway, monkeypatch, tmp_path) -> None:
    """One login, one store: nothing under $HOME may contain the credential.

    Scans the whole home tree rather than one expected path, so a new file in a
    new location cannot slip past.
    """
    _both_writes_succeed(gateway, "config_file")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SERVICE_ACCOUNT_KEY))

    cli.run(_add_args("config_file"), _api(gateway))

    secret = SERVICE_ACCOUNT_KEY.rstrip("\n")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(errors="ignore"), f"credential leaked into {path}"
            assert "aGVsbG8=" not in path.read_text(errors="ignore")
    assert not (tmp_path / ".superplane").exists(), "the retired second credential store came back"


def test_a_failed_registration_leaks_nothing_on_the_way_out(gateway, monkeypatch, capsys) -> None:
    """The compensation path prints a receipt; it must not print the secret."""
    gateway.route("PUT", VAULT_RECORD_PATH, 201, _vault_response())
    gateway.route("POST", DOMAIN_LIST, 422, {"detail": {"error": "validation_error"}})
    gateway.route("DELETE", f"{VAULT_LIST}/{VAULT_REFERENCE}", 204)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SERVICE_ACCOUNT_KEY))

    with pytest.raises(common.CliError) as raised:
        cli.run(_add_args("config_file"), _api(gateway))
    captured = capsys.readouterr()

    secret = SERVICE_ACCOUNT_KEY.rstrip("\n")
    assert secret not in str(raised.value)
    assert secret not in captured.out and secret not in captured.err
