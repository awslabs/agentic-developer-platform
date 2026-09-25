"""Tenant precedence, process pinning and refreshed bearer isolation."""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
spec = importlib.util.spec_from_file_location("tenant_cli", CLI / "adp-tenant.py")
tenant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tenant)


def token(sub="human", org="home", issuer="pool"):
    data = base64.urlsafe_b64encode(json.dumps({"sub": sub, "org_id": org, "iss": issuer}).encode()).decode().rstrip("=")
    return "e30." + data + ".sig"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tenant.common, "gateway_url", lambda: "https://gateway.example/api")
    monkeypatch.setattr(tenant.common, "config_path", lambda: tmp_path / "config.json")
    mock = Mock()

    def request(method, path, body=None, **kwargs):
        if path == "/workspaces":
            return {"items": [{"org_id": "home", "name": "Home"}, {"org_id": "work", "name": "Work"}]}
        assert path == "/workspaces/context"
        return {"tenant_id": body["org_id"], "identity": "human", "context_token": "signed.lease.token", "membership_id": "member-" + body["org_id"]}

    mock.request.side_effect = request
    return mock


def test_multiple_memberships_require_selection(client):
    with pytest.raises(tenant.common.CliError, match="Select one"):
        tenant.resolve(token(), client)
    assert client.request.call_count == 1


def test_flag_environment_saved_precedence(client, monkeypatch):
    tenant.common.write_json(tenant.default_path(token()), {"tenant_id": "home"})
    monkeypatch.setenv("ADP_TENANT", "work")
    assert tenant.resolve(token(), client)["ADP_TENANT_ID"] == "work"
    assert tenant.resolve(token(), client, "Home")["ADP_TENANT_ID"] == "home"
    monkeypatch.delenv("ADP_TENANT")
    assert tenant.resolve(token(), client)["ADP_TENANT_SOURCE"] == "saved_default"


def test_pin_survives_default_change_and_refresh(client, monkeypatch):
    pin = tenant.resolve(token(), client, "work")
    for key, value in pin.items():
        monkeypatch.setenv(key, value)
    tenant.common.write_json(tenant.default_path(token()), {"tenant_id": "home"})
    assert tenant.resolve(token(org="home"), client)["ADP_TENANT_ID"] == "work"
    wrapped = tenant.wrap(token(org="home"), client)
    assert wrapped.endswith("~" + token(org="home"))
    assert client.request.call_args.args[2] == {"org_id": "work", "expected_membership": "member-work"}


def test_account_switch_does_not_reuse_pin_or_saved_default(client, monkeypatch):
    assert tenant.default_path(token("human")) != tenant.default_path(token("another"))
    monkeypatch.setenv("ADP_TENANT_ID", "work")
    monkeypatch.setenv("ADP_TENANT_SUB", "human")
    with pytest.raises(tenant.common.CliError, match="Login changed"):
        tenant.resolve(token("another"), client)
    with pytest.raises(tenant.common.CliError, match="Login changed"):
        tenant.wrap(token("another"), client)


@pytest.mark.parametrize("selection", ["foreign", "duplicate"])
def test_unknown_ambiguous_names_never_exchange(client, selection):
    client.request.side_effect = None
    client.request.return_value = {"items": [{"org_id": "a", "name": "duplicate"}, {"org_id": "b", "name": "duplicate"}]}
    with pytest.raises(tenant.common.CliError):
        tenant.resolve(token(), client, selection)
    assert client.request.call_count == 1


def test_legacy_only_without_explicit_selector(client):
    client.request.side_effect = tenant.common.CliError("not found", status_code=404)
    assert tenant.resolve(token(), client)["ADP_TENANT_MODE"] == "legacy"
    with pytest.raises(tenant.common.CliError):
        tenant.resolve(token(), client, "work")


def test_tenant_state_and_proxy_namespaces_pin_but_share_refresh_store(monkeypatch):
    import adp_deployments as deployments

    record = {"id": "stable", "gateway_url": "https://gateway.example", "legacy": False}
    monkeypatch.setenv("ADP_TENANT_SUB", "human")
    monkeypatch.setenv("ADP_TENANT_ID", "home")
    home = deployments.Deployment("alias", record, "flag")
    monkeypatch.setenv("ADP_TENANT_ID", "work")
    work = deployments.Deployment("same", record, "flag")
    assert home.state_dir != work.state_dir
    assert home.runtime_dir != work.runtime_dir
    assert home.config_dir == work.config_dir
    assert home.runtime_dir != deployments.Deployment("alias", record, "flag").runtime_dir


def test_resume_rejects_changed_tenant_before_remote_io(monkeypatch):
    monkeypatch.setenv("ADP_TENANT_ID", "work")
    monkeypatch.setenv("ADP_TENANT_SUB", "human")
    with pytest.raises(tenant.common.CliError, match="another tenant"):
        tenant.common.check_handoff_deployment({"tenant_id": "home", "tenant_identity": "human"})


def test_composite_cache_scope_uses_signed_tenant_not_old_cognito_default():
    lease = base64.urlsafe_b64encode(json.dumps({"tenant": "work"}).encode()).decode().rstrip("=")
    wrapped = "adpctx1~e30." + lease + ".sig~" + token(org="home")
    scope = tenant.common.authenticated_scope(wrapped)
    assert scope["tenant"] == "work"
    assert scope["identity"] == tenant.common.authenticated_scope(token())["identity"]


def test_real_shell_global_selector_and_token_helper_transport(tmp_path):
    import subprocess
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []
    requested_tenants = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, data):
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler protocol
            calls.append((self.path, self.headers.get("Authorization")))
            if self.path.endswith("/workspaces"):
                self.respond({"items": [{"org_id": "home", "name": "Home"}, {"org_id": "work", "name": "Work"}]})
            else:
                self.respond({"run_id": "run", "available": True, "state": "running"})

        def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler protocol
            calls.append((self.path, self.headers.get("Authorization")))
            assert self.path.endswith("/workspaces/context")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requested_tenants.append(body["org_id"])
            self.respond({"tenant_id": body["org_id"], "identity": "human", "membership_id": "membership", "context_token": "signed.lease.value"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        directory = tmp_path / ".bedrock-gateway"
        directory.mkdir(mode=0o700)
        (directory / "config.json").write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{server.server_port}/api"}))
        (directory / "tokens.json").write_text(
            json.dumps({"access_token": token(), "id_token": token(), "refresh_token": "fixture", "expires_at": int(time.time()) + 3600})
        )
        os.chmod(directory / "tokens.json", 0o600)
        result = subprocess.run(
            ["bash", str(CLI / "adp"), "--tenant", "work", "tenant", "current", "--json"], text=True, capture_output=True, timeout=30
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert json.loads(result.stdout)["detail"]["selection_source"] == "flag"
        result = subprocess.run(
            ["bash", str(CLI / "adp"), "--tenant", "work", "agent", "state", "--run", "run", "--json"], text=True, capture_output=True, timeout=30
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert calls[-1][1] == "Bearer adpctx1~signed.lease.value~" + token()
        assert all(path != "/api/workspaces/select" for path, _ in calls)
        # A saved Claude helper runs later, outside the launcher's environment.
        # A different terminal's tenant default cannot retarget that setup.
        configured = subprocess.run(
            ["bash", str(CLI / "adp"), "--tenant", "work", "claude", "setup"],
            text=True, capture_output=True, timeout=30,
        )
        assert configured.returncode == 0, configured.stderr + configured.stdout
        helper = json.loads((tmp_path / ".claude/settings.json").read_text())["apiKeyHelper"]
        import shlex
        words = shlex.split(helper)
        # The installed bundle is executable; source fixtures invoke bash.
        words.insert(words.index(str(CLI / "adp")), "bash")
        resumed = subprocess.run(words, env={**os.environ, "ADP_TENANT": "home"},
                                 text=True, capture_output=True, timeout=30)
        assert resumed.returncode == 0, resumed.stderr + resumed.stdout
        assert requested_tenants[-1] == "work"
        assert resumed.stdout == "adpctx1~signed.lease.value~" + token()

    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
