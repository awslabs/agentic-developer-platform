"""Provider credential handling over the REAL shared transport (Issue #5039).

These tests exist because the original U6 suites passed while two defects
shipped, and in both cases the reason was the test double:

**`gateway_unavailable` on a successful delete.** `adp_common.Api.request` ended
every call with `json.load(response)`. ADP's vault delete endpoint is declared
``status_code=204, response_model=None`` (`src/auth/vault_routes.py:203`) and
sends no body, so the parse raised and a SUCCESSFUL delete was reported as an
unreachable gateway. A `RecordingApi` returning `{}` cannot show this — the bug
lives in the transport that a stub replaces. So these tests speak HTTP: a real
`http.server` returns real status lines, including bodiless 204s, and the CLI
drives it through the real `adp_common.Api`.

**A truncated multi-line credential.** `read_provider_value` used
`sys.stdin.readline()`, so a multi-line JSON service-account key reached the
vault as just ``{`` and the command still reported ok. The existing provider
tests monkeypatch `read_provider_value`, which is exactly why this passed
review. So these tests feed real stdin and assert on what the SERVER received,
not on what the CLI said it did.

The rule both cases teach: assert over actual input and actual transport. A
double that stands in for the component under test proves nothing about it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CLI_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load("adp_superplane_transport", "adp-superplane.py")
# The extension does `import adp_common as common`, so THIS is the transport it
# actually uses. Loading a second copy would give CliError a different class
# identity and make `pytest.raises` miss.
common = cli.common

SERVICE_ACCOUNT_KEY = '{\n  "type": "service_account",\n  "project_id": "synthetic-example"\n}\n'
SINGLE_LINE_SECRET = "sk-synthetic-single-line-value"


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
        self.__class__.seen.append({"method": method, "path": path, "raw": raw.decode() or None, "body": json.loads(raw) if raw else None})

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
        _Handler.routes[(method, path)] = (status, payload)

    @property
    def seen(self) -> list[dict]:
        return _Handler.seen

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

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Each test models a fresh CLI process with its own deployment selection.
    monkeypatch.setattr(cli.common, "_deployment", cli.common._UNRESOLVED)
    # The session token is the ONLY credential path; stubbing its retrieval keeps
    # these tests offline without introducing a second credential store.
    monkeypatch.setattr(cli.common, "access_token", lambda: "synthetic-session-token")
    try:
        yield server
    finally:
        server.close()


def _api(gateway) -> object:
    """The real transport, pointed at the live gateway."""
    return common.Api()


# --- the transport itself ----------------------------------------------------


def test_a_bodiless_204_is_a_success_not_an_unreachable_gateway(gateway) -> None:
    """The core defect: ADP's vault delete answers 204 with no body.

    Before the fix this raised CliError(gateway_unavailable) even though the
    server had received the DELETE and acted on it.
    """
    gateway.route("DELETE", "/api/auth/credentials/cred-1", 204)

    assert _api(gateway).request("DELETE", "/auth/credentials/cred-1") == {}
    assert [(row["method"], row["path"]) for row in gateway.seen] == [("DELETE", "/api/auth/credentials/cred-1")]


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


# --- provider delete: two non-atomic steps -----------------------------------


def _delete_args(credential_id: str = "cred-1"):
    return cli.parser().parse_args(["provider", "delete", credential_id])


def test_both_deletes_succeed_when_the_vault_answers_204(gateway) -> None:
    """The reviewer's first reproduction: domain 200 + vault 204 reported failure."""
    gateway.route("DELETE", f"/api{cli.API_BASE}/providers/cred-1", 200, {"ok": True})
    gateway.route("DELETE", "/api/auth/credentials/cred-1", 204)

    result = cli.run(_delete_args(), _api(gateway))

    assert result["status"] == "ok"
    assert result["detail"]["domain_metadata"] == "deleted"
    assert result["detail"]["vault_credential"] == "deleted"
    assert [row["path"] for row in gateway.seen] == [
        f"/api{cli.API_BASE}/providers/cred-1",
        "/api/auth/credentials/cred-1",
    ]


def test_a_bodiless_domain_delete_still_reaches_the_vault(gateway) -> None:
    """The reviewer's second reproduction: the orphaning path.

    Domain DELETE answering 204 used to raise, so the vault delete never ran and
    the secret was orphaned — with the CLI reporting an unreachable gateway.
    """
    gateway.route("DELETE", f"/api{cli.API_BASE}/providers/cred-1", 204)
    gateway.route("DELETE", "/api/auth/credentials/cred-1", 204)

    result = cli.run(_delete_args(), _api(gateway))

    assert result["status"] == "ok"
    # The vault call is the one that must not be skipped.
    assert "/api/auth/credentials/cred-1" in [row["path"] for row in gateway.seen]


def test_a_retry_finishes_the_vault_delete_after_the_metadata_is_gone(gateway) -> None:
    """Recoverability: a 404 on the already-deleted metadata must not stop the run.

    This is the retry in the reviewer's scenario. Stopping at the 404 would leave
    the secret in the vault permanently, with no CLI path left to remove it.
    """
    gateway.route("DELETE", f"/api{cli.API_BASE}/providers/cred-1", 404, {"detail": {"error": "not_found"}})
    gateway.route("DELETE", "/api/auth/credentials/cred-1", 204)

    result = cli.run(_delete_args(), _api(gateway))

    assert result["status"] == "ok"
    assert result["detail"]["domain_metadata"] == "already_absent"
    assert result["detail"]["vault_credential"] == "deleted"
    assert "/api/auth/credentials/cred-1" in [row["path"] for row in gateway.seen]


def test_a_vault_failure_stays_visible_and_actionable(gateway) -> None:
    """A real vault failure must not be swallowed by the 204 tolerance."""
    gateway.route("DELETE", f"/api{cli.API_BASE}/providers/cred-1", 200, {"ok": True})
    gateway.route("DELETE", "/api/auth/credentials/cred-1", 500, {"detail": {"error": "delete_failed"}})

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))

    assert raised.value.code == "provider_delete_incomplete"
    assert raised.value.exit_code == 5
    message = str(raised.value)
    # It must say the secret may survive, and what to do next.
    assert "may still exist" in message
    assert "Retry" in message


def test_a_domain_failure_that_is_not_404_stops_before_the_vault(gateway) -> None:
    """Only an absent domain row is tolerated; a 500 is a real failure."""
    gateway.route("DELETE", f"/api{cli.API_BASE}/providers/cred-1", 500, {"detail": {"error": "delete_failed"}})
    gateway.route("DELETE", "/api/auth/credentials/cred-1", 204)

    with pytest.raises(common.CliError) as raised:
        cli.run(_delete_args(), _api(gateway))

    assert raised.value.code == "delete_failed"
    # The vault delete must not have run: the credential id is still referenced.
    assert "/api/auth/credentials/cred-1" not in [row["path"] for row in gateway.seen]


def test_the_other_delete_verbs_work_over_the_real_transport(gateway) -> None:
    """Every other DELETE verb meets the same bodiless-204 shape.

    `account delete` and `deploy delete` are the remaining ones. Before the fix
    each reported an unreachable gateway after a successful delete.
    """
    gateway.route("DELETE", f"/api{cli.API_BASE}/accounts/123456789012", 204)
    gateway.route("DELETE", f"/api{cli.API_BASE}/workspaces/ws-1/deployments/dep-1", 204)

    account = cli.run(cli.parser().parse_args(["account", "delete", "123456789012"]), _api(gateway))
    deployment = cli.run(cli.parser().parse_args(["deploy", "delete", "--name", "dep-1", "--workspace", "ws-1"]), _api(gateway))

    assert account["status"] == "ok"
    assert deployment["status"] == "ok"


# --- provider add: the real stdin path ---------------------------------------


def _add_args(credential_type: str = "api_key"):
    return cli.parser().parse_args(["provider", "add", "--name", "prod", "--provider", "nebius", "--type", credential_type, "--stdin"])


def _vault_body(gateway) -> dict:
    bodies = [row["body"] for row in gateway.seen if row["path"] == "/api/auth/credentials"]
    assert bodies, "the vault was never called"
    return bodies[0]


def test_a_multiline_credential_reaches_the_vault_whole(gateway, monkeypatch) -> None:
    """The truncation defect, asserted where it actually showed: the vault body.

    `readline()` sent "{" and the command still reported ok. Only the server's
    view of the request catches that — the CLI's own envelope looked fine.
    """
    gateway.route("POST", "/api/auth/credentials", 201, {"id": "cred-9"})
    gateway.route("POST", f"/api{cli.API_BASE}/providers", 201, {"registered": True})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SERVICE_ACCOUNT_KEY))

    result = cli.run(_add_args("config_file"), _api(gateway))

    assert _vault_body(gateway)["value"] == SERVICE_ACCOUNT_KEY.rstrip("\n")
    # And it must still be valid JSON on arrival — the point of config_file.
    assert json.loads(_vault_body(gateway)["value"])["project_id"] == "synthetic-example"
    assert result["detail"]["credential_id"] == "cred-9"


def test_a_single_line_secret_loses_its_trailing_newline_only(gateway, monkeypatch) -> None:
    """`echo secret | adp ... --stdin` must not store the newline."""
    gateway.route("POST", "/api/auth/credentials", 201, {"id": "cred-9"})
    gateway.route("POST", f"/api{cli.API_BASE}/providers", 201, {"registered": True})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SINGLE_LINE_SECRET + "\n"))

    cli.run(_add_args(), _api(gateway))

    assert _vault_body(gateway)["value"] == SINGLE_LINE_SECRET


def test_interior_and_leading_whitespace_are_preserved(gateway, monkeypatch) -> None:
    """An indented PEM-style block must arrive byte-identical apart from the tail."""
    pem = "-----BEGIN KEY-----\n  aGVsbG8=\n  d29ybGQ=\n-----END KEY-----"
    gateway.route("POST", "/api/auth/credentials", 201, {"id": "cred-9"})
    gateway.route("POST", f"/api{cli.API_BASE}/providers", 201, {"registered": True})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(pem + "\n"))

    cli.run(_add_args("config_file"), _api(gateway))

    assert _vault_body(gateway)["value"] == pem


def test_empty_stdin_stores_nothing(gateway, monkeypatch) -> None:
    gateway.route("POST", "/api/auth/credentials", 201, {"id": "cred-9"})
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


# --- the protections that must survive the fix -------------------------------


def test_the_multiline_value_never_reaches_argv_output_or_the_url(gateway, monkeypatch, capsys) -> None:
    """Reading more of stdin must not widen the leak surface.

    The value may appear in exactly one place: the POST body to the vault.
    """
    gateway.route("POST", "/api/auth/credentials", 201, {"id": "cred-9"})
    gateway.route("POST", f"/api{cli.API_BASE}/providers", 201, {"registered": True})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SERVICE_ACCOUNT_KEY))

    result = cli.run(_add_args("config_file"), _api(gateway))
    captured = capsys.readouterr()
    secret = SERVICE_ACCOUNT_KEY.rstrip("\n")

    # Not in any URL the server saw.
    assert all(secret not in row["path"] for row in gateway.seen)
    # Not in the domain metadata request — only the returned id crosses over.
    domain = [row for row in gateway.seen if row["path"] == f"/api{cli.API_BASE}/providers"]
    assert domain and domain[0]["body"]["credential_id"] == "cred-9"
    assert all(secret not in (row["raw"] or "") for row in domain)
    # Not in the CLI's own output, on either stream, nor in the returned envelope.
    assert secret not in captured.out and secret not in captured.err
    assert secret not in json.dumps(result)


def test_no_local_copy_of_the_multiline_value_is_written(gateway, monkeypatch, tmp_path) -> None:
    """One login, one store: nothing under $HOME may contain the credential.

    Scans the whole home tree rather than one expected path, so a new file in a
    new location cannot slip past.
    """
    gateway.route("POST", "/api/auth/credentials", 201, {"id": "cred-9"})
    gateway.route("POST", f"/api{cli.API_BASE}/providers", 201, {"registered": True})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SERVICE_ACCOUNT_KEY))

    cli.run(_add_args("config_file"), _api(gateway))

    secret = SERVICE_ACCOUNT_KEY.rstrip("\n")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(errors="ignore"), f"credential leaked into {path}"
            assert "aGVsbG8=" not in path.read_text(errors="ignore")
    assert not (tmp_path / ".superplane").exists(), "the retired second credential store came back"
