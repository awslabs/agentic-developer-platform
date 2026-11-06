"""The CLI MCP bridge speaks JSON-RPC on stdout and HTTP to the gateway."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from tests.e2e.tools.atlassian_integration import client_schema_loading


SHIM = Path(__file__).resolve().parents[2] / "modules/gateway/cli/adp-mcp.py"


class Gateway(BaseHTTPRequestHandler):
    requests = []
    catalogue = [{"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}]
    enforce_catalogue = False
    receipts = {}
    provider_calls = 0
    drop_response = False
    operation_directory = None
    persisted_before_call = []
    close_state = "closing"
    cleanup_status = "unknown"

    def log_message(self, *args):
        pass

    def respond(self):
        length = int(self.headers.get("Content-Length", "0"))
        if getattr(self, "failure_status", None):
            if self.failure_status == 302:
                if self.path == "/redirect":
                    self.send_error(404)
                    return
                self.send_response(302)
                self.send_header("Location", "/redirect")
                self.end_headers()
                return
            self.send_error(self.failure_status)
            return
        self.requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(length)) if length else None))
        if self.headers.get("Authorization") == "Bearer foreign-token":
            self.send_error(404)
            return
        if self.path == "/api/agent/tools" or self.path == "/tools":
            body = {"tools": self.catalogue}
        elif self.path == "/api/agent/tools/call" or self.path == "/tools/call":
            call = self.requests[-1][2]
            if self.enforce_catalogue and call["tool"] not in {tool["name"] for tool in self.catalogue}:
                self.send_error(403)
                return
            request_id = call["request_id"]
            if self.operation_directory:
                stored = self.operation_directory / (request_id + ".json")
                self.persisted_before_call.append(json.loads(stored.read_text()))
            existing = self.receipts.get(request_id)
            if existing and existing["arguments"] != call["arguments"]:
                self.send_error(409)
                return
            if existing:
                body = existing["receipt"]
            else:
                type(self).provider_calls += 1
                body = {"status": "confirmed", "result_status": "success", "result": {"items": ["ok"]},
                        "request_id": request_id, "execution_id": str(uuid4())}
                self.receipts[request_id] = {"arguments": call["arguments"], "receipt": body}
                if self.drop_response:
                    type(self).drop_response = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
        elif self.path in {"/api/agent/tools/operations/recover", "/tools/operations/recover"}:
            call = self.requests[-1][2]
            existing = self.receipts.get(call["request_id"])
            if not existing or existing["arguments"] != call["arguments"]:
                self.send_error(404)
                return
            body = existing["receipt"]
        elif self.path.startswith("/api/agent/tools/executions/") and "/close?" in self.path:
            body = {"execution_id": urlsplit(self.path).path.split("/")[-2],
                    "state": self.close_state, "cleanup_status": self.cleanup_status}
        elif self.path.startswith("/api/agent/tools/executions/"):
            parts = urlsplit(self.path).path.split("/")
            request_id = parts[-3] if parts[-1] == "result" else parts[-1]
            stored = self.receipts.get(request_id)
            if stored is None:
                self.send_error(404)
                return
            if stored["receipt"]["execution_id"] != parts[-5 if parts[-1] == "result" else -3]:
                self.send_error(404)
                return
            body = stored["receipt"]
            if parts[-1] == "result":
                if body["status"] != "confirmed":
                    self.send_error(404)
                    return
                from urllib.parse import parse_qs
                query = parse_qs(urlsplit(self.path).query)
                offset = int(query["offset"][0])
                length = int(query["length"][0])
                data = json.dumps(body["result"]).encode()
                chunk = data[offset:offset + length]
                body = {"artifact_id": "result", "offset": offset, "length": len(chunk),
                        "total_length": len(data), "content_type": "application/json",
                        "sha256": hashlib.sha256(data).hexdigest(), "data_base64": base64.b64encode(chunk).decode()}
        elif self.path == "/api/auth/cli/revoke":
            body = {"revoked": True}
        else:
            self.send_error(404)
            return
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = respond
    do_POST = respond


def packet(method, identifier, params=None):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if identifier is not None:
        message["id"] = identifier
    return message


def fake_token(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"test.{payload}.signature"


@pytest.fixture
def client(tmp_path):
    Gateway.requests = []
    Gateway.catalogue = [{"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}]
    Gateway.enforce_catalogue = False
    Gateway.receipts = {}
    Gateway.provider_calls = 0
    Gateway.drop_response = False
    Gateway.persisted_before_call = []
    Gateway.operation_directory = None
    Gateway.close_state = "closing"
    Gateway.cleanup_status = "unknown"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cli_dir = tmp_path / "bin"
    cli_dir.mkdir()
    shutil.copy(SHIM, cli_dir / "adp-mcp.py")
    shutil.copy(SHIM.parent / "adp_common.py", cli_dir / "adp_common.py")
    shutil.copy(SHIM.parent / "adp_native_test.py", cli_dir / "adp_native_test.py")
    shutil.copy(SHIM.parent / "adp_native_credential_handoff.py", cli_dir / "adp_native_credential_handoff.py")
    adp = cli_dir / "adp"
    adp.write_text("#!/bin/sh\nprintf 'example-token\\n'\n")
    adp.chmod(0o700)
    config_dir = tmp_path / "auth"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{server.server_port}"}))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ADP_", "BG_", "SIGV4_"))}
    env.update(BG_CONFIG_DIR=str(config_dir), HOME=str(tmp_path))

    def invoke(*packets):
        process = subprocess.run([sys.executable, str(cli_dir / "adp-mcp.py")], input="".join(json.dumps(p) + "\n" for p in packets),
                                 capture_output=True, text=True, env=env, check=True)
        return [json.loads(line) for line in process.stdout.splitlines()]

    yield invoke, env, server
    server.shutdown()
    thread.join()
    server.server_close()


def test_initialize_and_list_via_existing_token(client):
    invoke, _, _ = client
    messages = invoke(packet("initialize", 1), packet("notifications/initialized", None), packet("tools/list", 2))
    assert len(messages) == 2
    assert messages[0]["result"]["serverInfo"]["name"] == "adp-tools"
    assert messages[1]["result"]["tools"][0]["name"] == "websearch___search"
    assert [(path, body) for path, _, body in Gateway.requests] == [
        ("/api/v1/l5/mcp/tools/list", {}), ("/api/agent/tools", None)]
    assert all(headers["Authorization"] == "Bearer example-token" for _, headers, _ in Gateway.requests)


def test_next_mcp_session_sees_added_and_removed_tools_without_cli_update(client):
    invoke, _, _ = client
    Gateway.enforce_catalogue = True
    before = invoke(packet("tools/list", 1))[0]["result"]["tools"]
    assert "websearch___search" in [tool["name"] for tool in before]
    Gateway.catalogue = [{"name": "jira___get_issue", "description": "Read Jira",
                          "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}}}]
    refreshed = invoke(packet("tools/list", 2), packet("tools/call", 3, {
        "name": "jira___get_issue", "arguments": {"key": "DEMO-1"}}))
    names = [tool["name"] for tool in refreshed[0]["result"]["tools"]]
    assert names[0] == "jira___get_issue" and "websearch___search" not in names
    assert refreshed[1]["result"]["isError"] is False
    assert Gateway.requests[-1][2]["tool"] == "jira___get_issue"
    removed = invoke(packet("tools/call", 4, {
        "name": "websearch___search", "arguments": {"query": "stale client"}}))[0]
    assert removed["result"]["isError"] is True
    assert Gateway.provider_calls == 1


def test_large_eager_catalogue_keeps_every_named_tool_callable(client):
    invoke, _, _ = client
    Gateway.enforce_catalogue = True
    Gateway.catalogue = [{"name": "jira___get_issue" if index == 0 else f"jira___read_{index:03d}",
                          "description": "A permitted read " + "x" * 950,
                          "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}}}
                         for index in range(80)]
    replies = invoke(packet("tools/list", 1), packet("tools/call", 2, {
        "name": "jira___get_issue", "arguments": {"key": "DEMO-1"}}))
    definitions = replies[0]["result"]["tools"]
    names = {tool["name"] for tool in definitions}
    descriptor_bytes = len(json.dumps(definitions, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    assert {tool["name"] for tool in Gateway.catalogue} <= names
    assert len(Gateway.catalogue) == 80 and 80_000 < descriptor_bytes <= 128 * 1024
    assert replies[1]["result"]["isError"] is False
    assert Gateway.requests[-1][2]["tool"] == "jira___get_issue"


@pytest.mark.parametrize("native_client", ("claude-code", "codex"))
def test_client_schema_loading_reports_fixture_bytes_not_model_context(client, native_client):
    invoke, _, _ = client
    Gateway.enforce_catalogue = True
    Gateway.catalogue = [{"name": "jira___get_issue" if index == 0 else f"jira___read_{index:03d}",
                          "description": "A permitted read " + "x" * 950,
                          "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}}}
                         for index in range(80)]
    replies = invoke(packet("tools/list", 1), packet("tools/call", 2, {
        "name": "jira___get_issue", "arguments": {"key": "DEMO-1"}}))
    report = client_schema_loading(native_client, replies[0]["result"]["tools"],
                                   "jira___get_issue", replies[1]["result"])
    assert report["client"] == native_client
    assert report["scenario"] == "SCN-03"
    assert report["status"] == "pending-installed-client-proof"
    assert report["named_call"] == {"tool": "jira___get_issue", "status": "fixture-pass"}
    assert report["mcp_descriptors"]["count"] >= 80
    assert 80_000 < report["mcp_descriptors"]["utf8_bytes"] <= 128 * 1024
    assert all(value is None for value in report["model_facing"].values())
    assert report["installed_client"]["native_schema_deferral"] == "unverified"
    assert Gateway.requests[-1][2]["tool"] == "jira___get_issue"


def test_oversized_eager_catalogue_refuses_without_truncation(client):
    invoke, _, _ = client
    Gateway.catalogue = [{"name": "jira___get_issue", "description": "",
                          "inputSchema": {"type": "object"}}]
    baseline = invoke(packet("tools/list", 1))[0]["result"]["tools"]
    baseline_bytes = len(json.dumps(baseline, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    Gateway.catalogue[0]["description"] = "x" * (128 * 1024 - baseline_bytes)
    at_limit = invoke(packet("tools/list", 2))[0]
    assert len(json.dumps(at_limit["result"]["tools"], ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8")) == 128 * 1024
    Gateway.catalogue[0]["description"] += "x"
    oversized = invoke(packet("tools/list", 3))[0]
    assert "result" not in oversized
    assert "131073 UTF-8 bytes" in oversized["error"]["message"]
    assert "native schema deferral is unverified" in oversized["error"]["message"]
    assert "no tools were silently omitted" in oversized["error"]["message"]


def test_reconnected_entries_keep_original_profile_and_gateway(client, tmp_path):
    _, _, alpha_server = client
    home, run = setup_cli(tmp_path)

    class BetaGateway(Gateway):
        requests = []
        receipts = {}
        provider_calls = 0
        catalogue = [{"name": "confluence___get_page", "inputSchema": {"type": "object"}}]
        enforce_catalogue = True

    beta_server = ThreadingHTTPServer(("127.0.0.1", 0), BetaGateway)
    beta_thread = Thread(target=beta_server.serve_forever, daemon=True)
    beta_thread.start()
    Gateway.catalogue = [{"name": "jira___get_issue", "inputSchema": {"type": "object"}}]
    Gateway.enforce_catalogue = True
    try:
        import tomllib

        entries, tokens = {}, {}
        for name, server, token in (("alpha", alpha_server, fake_token(sub="alpha-user", tenant_id="alpha")),
                                    ("beta", beta_server, fake_token(sub="beta-user", tenant_id="beta"))):
            url = f"http://127.0.0.1:{server.server_port}"
            assert run("deployment", "add", name, "--url", url).returncode == 0
            registry = json.loads((home / ".adp/deployments.json").read_text())
            store = home / ".adp/deployments" / registry["deployments"][name]["id"]
            store.mkdir(exist_ok=True, parents=True)
            (store / "config.json").write_text(json.dumps({"gateway_url": url + "/api"}))
            (store / "tokens.json").write_text(json.dumps({"access_token": token,
                                                           "refresh_token": "fixture", "expires_at": int(time.time()) + 3600}))
            tokens[name] = token
            result = run("--deployment", name, "mcp", "configure", "--client", "codex")
            assert result.returncode == 0, result.stderr
            entries[name] = tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"]["adp-tools"]
            assert entries[name]["env"]["BG_CONFIG_DIR"] == str(store)
        assert entries["alpha"]["env"]["ADP_DEPLOYMENT_ID"] != entries["beta"]["env"]["ADP_DEPLOYMENT_ID"]
        for name, tool in (("alpha", "jira___get_issue"), ("beta", "confluence___get_page")):
            entry = entries[name]
            messages = (packet("tools/list", 1), packet("tools/call", 2, {"name": tool, "arguments": {}}))
            env = {"HOME": str(home), "PATH": os.environ["PATH"], "ADP_DEPLOYMENT": "beta" if name == "alpha" else "alpha",
                   **entry["env"]}
            result = subprocess.run([entry["command"], *entry["args"]], env=env,
                                    input="".join(json.dumps(message) + "\n" for message in messages),
                                    text=True, capture_output=True)
            assert result.returncode == 0, result.stderr
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            assert replies[0]["result"]["tools"][0]["name"] == tool
            assert replies[1]["result"]["isError"] is False
            receipt = (Gateway if name == "alpha" else BetaGateway).requests
            assert receipt[-1][2]["tool"] == tool
            assert receipt[-1][1]["Authorization"] == "Bearer " + tokens[name]
        assert Gateway.provider_calls == 1 and BetaGateway.provider_calls == 1
    finally:
        beta_server.shutdown()
        beta_thread.join()
        beta_server.server_close()


@pytest.fixture
def automatic_refresh_fixture(client, tmp_path):
    _, _, alpha_server = client
    home, run = setup_cli(tmp_path)

    class BetaGateway(Gateway):
        requests = []
        receipts = {}
        provider_calls = 0
        failure_status = None
        catalogue = [{"name": "confluence___get_page", "inputSchema": {
            "type": "object", "x-adp-schema-version": "beta-1"}}]
        enforce_catalogue = True

    beta_server = ThreadingHTTPServer(("127.0.0.1", 0), BetaGateway)
    beta_thread = Thread(target=beta_server.serve_forever, daemon=True)
    beta_thread.start()
    Gateway.enforce_catalogue = True
    Gateway.catalogue = [{"name": "jira___get_issue", "inputSchema": {
        "type": "object", "x-adp-schema-version": "alpha-1"}}]
    claude = home / ".claude.json"
    claude.write_text(json.dumps({"userOwnedSetting": "keep", "mcpServers": {"other": {"command": "sentinel"}}}))
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('model = "keep"\n[mcp_servers.other]\ncommand = "sentinel"\n')
    stores, tokens = {}, {}
    for name, server in (("alpha", alpha_server), ("beta", beta_server)):
        url = f"http://127.0.0.1:{server.server_port}"
        result = run("deployment", "add", name, "--url", url)
        assert result.returncode == 0, result.stderr
        registry = json.loads((home / ".adp/deployments.json").read_text())
        store = home / ".adp/deployments" / registry["deployments"][name]["id"]
        store.mkdir(exist_ok=True, parents=True)
        (store / "config.json").write_text(json.dumps({"gateway_url": url + "/api", "refresh_via": "gateway"}))
        token = fake_token(sub=f"{name}-member", tenant_id=f"{name}-tenant")
        (store / "tokens.json").write_text(json.dumps({"access_token": token, "refresh_token": "fixture",
                                                       "expires_at": 4102444800}))
        stores[name], tokens[name] = store, token
    try:
        yield {"home": home, "run": run, "stores": stores, "tokens": tokens,
               "claude": claude, "codex": codex, "alpha": Gateway, "beta": BetaGateway}
    finally:
        beta_server.shutdown()
        beta_thread.join()
        beta_server.server_close()


def test_automatic_client_refresh(automatic_refresh_fixture):
    import tomllib

    fixture = automatic_refresh_fixture
    home, run = fixture["home"], fixture["run"]

    def configure(profile):
        entries = {}
        for client_name in ("codex", "claude-code"):
            result = run("--deployment", profile, "mcp", "configure", "--client", client_name)
            assert result.returncode == 0, result.stderr
            if client_name == "codex":
                entry = tomllib.loads(fixture["codex"].read_text())["mcp_servers"]["adp-tools"]
            else:
                entry = json.loads(fixture["claude"].read_text())["mcpServers"]["adp-tools"]
            assert entry["env"]["BG_CONFIG_DIR"] == str(fixture["stores"][profile])
            entries[client_name] = entry
        return entries

    def reconnect(entry, selected, *messages):
        env = {"HOME": str(home), "PATH": os.environ["PATH"], "ADP_DEPLOYMENT": selected, **entry["env"]}
        result = subprocess.run([entry["command"], *entry["args"]], env=env,
                                input="".join(json.dumps(message) + "\n" for message in messages),
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return [json.loads(line) for line in result.stdout.splitlines()]

    alpha_entries = configure("alpha")
    codex_before = tomllib.loads(fixture["codex"].read_text())
    claude_before = json.loads(fixture["claude"].read_text())
    for entry in alpha_entries.values():
        original = reconnect(entry, "beta", packet("tools/list", 1))[0]["result"]["tools"]
        assert original[0]["name"] == "jira___get_issue"
        assert original[0]["inputSchema"]["x-adp-schema-version"] == "alpha-1"

    fixture["alpha"].catalogue = [{"name": "jira___get_project", "inputSchema": {
        "type": "object", "x-adp-schema-version": "alpha-2"}}]
    beta_entries = configure("beta")
    codex_after = tomllib.loads(fixture["codex"].read_text())
    claude_after = json.loads(fixture["claude"].read_text())
    for config in (codex_before, codex_after):
        assert config["model"] == "keep" and config["mcp_servers"]["other"] == {"command": "sentinel"}
    for config in (claude_before, claude_after):
        assert config["userOwnedSetting"] == "keep" and config["mcpServers"]["other"] == {"command": "sentinel"}
    assert codex_before["mcp_servers"]["adp-tools"] != codex_after["mcp_servers"]["adp-tools"]
    assert claude_before["mcpServers"]["adp-tools"] != claude_after["mcpServers"]["adp-tools"]

    for entries, profile, other_profile, tool, revision in (
        (alpha_entries, "alpha", "beta", "jira___get_project", "alpha-2"),
        (beta_entries, "beta", "alpha", "confluence___get_page", "beta-1"),
    ):
        for entry in entries.values():
            refreshed = reconnect(entry, other_profile, packet("tools/list", 1), packet("tools/call", 2, {
                "name": tool, "arguments": {}}))
            assert refreshed[0]["result"]["tools"][0]["name"] == tool
            assert refreshed[0]["result"]["tools"][0]["inputSchema"]["x-adp-schema-version"] == revision
            assert refreshed[1]["result"]["isError"] is False
        calls = [request for request in fixture[profile].requests if request[0] == "/api/agent/tools/call"]
        assert len(calls) == 2
        assert all(request[2]["tool"] == tool and request[1]["Authorization"] ==
                   "Bearer " + fixture["tokens"][profile] for request in calls)

    alpha_tokens = fixture["stores"]["alpha"] / "tokens.json"
    alpha_tokens.write_text(json.dumps({"access_token": fixture["tokens"]["alpha"],
                                       "refresh_token": "expired-fixture", "expires_at": 1}))
    alpha_tool_count = len([request for request in fixture["alpha"].requests if request[0] == "/api/agent/tools"])
    expired = reconnect(alpha_entries["codex"], "beta", packet("tools/list", 3))[0]
    assert "run adp login" in expired["error"]["message"].lower()
    assert len([request for request in fixture["alpha"].requests if request[0] == "/api/agent/tools"]) == alpha_tool_count
    beta_after_expiry = reconnect(beta_entries["claude-code"], "alpha", packet("tools/list", 4))[0]
    assert beta_after_expiry["result"]["tools"][0]["name"] == "confluence___get_page"
    rotated = fake_token(sub="alpha-member", tenant_id="alpha-tenant", revision="2")
    alpha_tokens.write_text(json.dumps({"access_token": rotated, "refresh_token": "fixture",
                                       "expires_at": 4102444800}))
    refreshed = reconnect(alpha_entries["claude-code"], "beta", packet("tools/list", 5))[0]
    assert refreshed["result"]["tools"][0]["name"] == "jira___get_project"
    assert fixture["alpha"].requests[-1][1]["Authorization"] == "Bearer " + rotated

    fixture["beta"].failure_status = 503
    try:
        outage = reconnect(beta_entries["codex"], "alpha", packet("tools/list", 6))[0]
        assert "result" not in outage and "503" in outage["error"]["message"]
    finally:
        fixture["beta"].failure_status = None
    assert len([request for request in fixture["beta"].requests
                if request[0] == "/api/agent/tools/call"]) == 2

    config = json.loads(fixture["claude"].read_text())
    config["mcpServers"]["adp-tools"] = {"command": "operator-owned"}
    fixture["claude"].write_text(json.dumps(config))
    original = fixture["claude"].read_bytes()
    collision = run("--deployment", "alpha", "mcp", "configure", "--client", "claude-code")
    assert collision.returncode != 0 and "modified" in collision.stderr
    assert fixture["claude"].read_bytes() == original


def test_pinned_entry_refuses_rebound_deployment_without_gateway_request(client, tmp_path):
    _, _, server = client
    home, run = setup_cli(tmp_path)
    url = f"http://127.0.0.1:{server.server_port}"
    assert run("deployment", "add", "alpha", "--url", url).returncode == 0
    registry_path = home / ".adp/deployments.json"
    registry = json.loads(registry_path.read_text())
    store = home / ".adp/deployments" / registry["deployments"]["alpha"]["id"]
    store.mkdir(exist_ok=True, parents=True)
    (store / "config.json").write_text(json.dumps({"gateway_url": url + "/api"}))
    (store / "tokens.json").write_text(json.dumps({"access_token": fake_token(sub="alpha-user"),
                                                   "expires_at": int(time.time()) + 3600}))
    assert run("--deployment", "alpha", "mcp", "configure", "--client", "claude-code").returncode == 0
    entry = json.loads((home / ".claude.json").read_text())["mcpServers"]["adp-tools"]
    registry["deployments"]["alpha"]["gateway_url"] = "http://127.0.0.1:1/api"
    registry_path.write_text(json.dumps(registry))
    result = subprocess.run([entry["command"], *entry["args"]], env={"HOME": str(home), "PATH": os.environ["PATH"],
                             **entry["env"]}, input=json.dumps(packet("tools/list", 1)) + "\n",
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "another gateway" in result.stderr.lower() or "changed" in result.stderr.lower()
    assert not Gateway.requests


def test_expired_entry_reports_login_instead_of_calling_tools(client, tmp_path):
    _, _, server = client
    home, run = setup_cli(tmp_path)
    auth = home / ".bedrock-gateway"
    (auth / "config.json").write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{server.server_port}/api",
                                                  "refresh_via": "gateway"}))
    (auth / "tokens.json").write_text(json.dumps({"access_token": fake_token(sub="expired-user"),
                                                  "refresh_token": "expired-fixture",
                                                  "expires_at": int(time.time()) - 3600}))
    assert run("mcp", "configure", "--client", "claude-code").returncode == 0
    entry = json.loads((home / ".claude.json").read_text())["mcpServers"]["adp-tools"]
    result = subprocess.run([entry["command"], *entry["args"]], env={"HOME": str(home), "PATH": os.environ["PATH"],
                             **entry["env"]}, input=json.dumps(packet("tools/list", 1)) + "\n",
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert "run adp login" in response["error"]["message"].lower()
    assert not any(path == "/api/agent/tools" for path, _, _ in Gateway.requests)


def test_outage_does_not_serve_stale_catalogue_on_reconnect(client):
    invoke, _, _ = client
    assert invoke(packet("tools/list", 1))[0]["result"]["tools"]
    Gateway.failure_status = 503
    try:
        failed = invoke(packet("tools/list", 2))[0]
        assert "result" not in failed
        assert "503" in failed["error"]["message"]
    finally:
        Gateway.failure_status = None


@pytest.mark.parametrize("client_name", ["claude", "codex"])
def test_managed_entry_calls_named_tool_without_aws(client, tmp_path, client_name):
    _, env, _ = client
    config_dir = Path(env["BG_CONFIG_DIR"])
    (config_dir / "tokens.json").write_text(json.dumps({
        "access_token": fake_token(sub="member-1"), "refresh_token": "fixture",
        "expires_at": int(time.time()) + 3600,
    }))
    installed = tmp_path / "bin/adp"
    for name in ("adp", "bg-cognito-auth.sh", "adp_deployments.py", "bg-gateway-proxy.py", "adp_mcp_config.py"):
        shutil.copy(SHIM.parent / name, installed.parent / name)
        (installed.parent / name).chmod(0o700)
    for _ in range(2):
        setup = subprocess.run(["bash", str(installed), client_name, "setup"],
                               env=env, text=True, capture_output=True)
        assert setup.returncode == 0, setup.stderr
    if client_name == "codex":
        config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
        entry = config["mcp_servers"]["adp-tools"]
        assert list(config["mcp_servers"]) == ["adp-tools"]
    else:
        config = json.loads((tmp_path / ".claude.json").read_text())
        entry = config["mcpServers"]["adp-tools"]
        assert list(config["mcpServers"]) == ["adp-tools"]
    assert entry["command"] == str(installed)
    assert entry["env"]["BG_CONFIG_DIR"] == str(config_dir)
    assert entry["args"][-1] == "mcp"
    session = subprocess.run([entry["command"], *entry["args"]], env={**env, **entry["env"]},
                             input="".join(json.dumps(message) + "\n" for message in (
                                 packet("tools/list", 1),
                                 packet("tools/call", 2, {"name": "websearch___search", "arguments": {"query": "hello"}}),
                             )), text=True, capture_output=True)
    assert session.returncode == 0, session.stderr
    replies = [json.loads(line) for line in session.stdout.splitlines()]
    assert replies[0]["result"]["tools"][0]["name"] == "websearch___search"
    assert replies[1]["result"]["isError"] is False
    assert Gateway.provider_calls == 1


def test_cli_discovery_uses_real_stdio_without_model_setup(client, tmp_path, monkeypatch):
    from contextlib import contextmanager

    from tests.e2e.tools import client_matrix, runner

    _, env, _ = client
    installed = tmp_path / "bin/adp"
    installed.write_text("#!/bin/sh\nif [ \"$1\" = mcp ]; then exec " + shlex.quote(sys.executable)
                         + " " + shlex.quote(str(installed.parent / "adp-mcp.py"))
                         + "; fi\nprintf 'example-token\\n'\n")
    original = Gateway.respond

    def respond(gateway):
        if gateway.path != "/api/agent/tools":
            return original(gateway)
        Gateway.requests.append((gateway.path, dict(gateway.headers), None))
        listing = {"tools": [{"name": name, "inputSchema": {"type": "object",
                            "x-adp-schema-version": "1.0"}} for name in client_matrix.OPERATIONS
                             if not name.startswith("adp___")]}
        data = json.dumps(listing).encode()
        gateway.send_response(200)
        gateway.send_header("Content-Length", str(len(data)))
        gateway.end_headers()
        gateway.wfile.write(data)

    @contextmanager
    def installation(*args):
        yield installed, env, {"adp": "a" * 64, "adp-mcp.py": "b" * 64}

    monkeypatch.setattr(Gateway, "do_GET", respond)
    monkeypatch.setattr(runner, "installed_clients", installation)
    # Local protocol fixture only: no genuine session import or provider call.
    monkeypatch.setattr(runner, "import_session", lambda *args: None)
    monkeypatch.setattr(runner, "configure_model_transport",
                        lambda *args: pytest.fail("stdio discovery attempted native model setup"))
    # The real stdio bridge filters private delivery names from the facade.
    # With no native host, that partial inventory cannot qualify all-tool access.
    with pytest.raises(ValueError, match="required public operation inventory"):
        runner.harness_ac01("cli", gateway_url="https://example.invalid",
                           gateway_revision="a" * 40, refresh_file="offline-only")
    assert [request[0] for request in Gateway.requests] == ["/api/v1/l5/mcp/tools/list", "/api/agent/tools"]
    assert Gateway.provider_calls == 0


def test_call_forwards_arguments_and_reports_receipt(client):
    invoke, _, _ = client
    result = invoke(packet("tools/call", "rpc-id", {"name": "websearch___search", "arguments": {"query": "hello"}}))[0]
    assert result["id"] == "rpc-id"
    assert result["result"]["isError"] is False
    body = Gateway.requests[0][2]
    assert body["arguments"] == {"query": "hello"}
    assert body["request_id"] != "rpc-id"
    assert json.loads(result["result"]["content"][0]["text"])["request_id"] == body["request_id"]


def test_no_token_reports_login_without_writing_stdout_diagnostics(client, tmp_path):
    invoke, env, _ = client
    (tmp_path / "bin/adp").write_text("#!/bin/sh\nexit 1\n")
    result = invoke(packet("tools/list", 1))[0]
    assert "run adp login" in result["error"]["message"]
    assert not Gateway.requests


def test_invalid_method_does_not_call_gateway(client):
    invoke, _, _ = client
    result = invoke(packet("resources/list", 1))[0]
    assert result["error"]["code"] == -32601
    assert not Gateway.requests


def test_pod_proxy_does_not_request_laptop_token(client, tmp_path):
    invoke, env, server = client
    (tmp_path / "bin/adp").write_text("#!/bin/sh\nexit 1\n")
    env["SIGV4_PROXY_TARGET"] = "https://gateway.example.invalid/agent"
    env["SIGV4_PROXY_PORT"] = str(server.server_port)
    result = invoke(packet("tools/list", 1))[0]
    assert result["result"]["tools"][0]["name"] == "websearch___search"
    assert Gateway.requests[0][0] == "/tools"
    assert "Authorization" not in Gateway.requests[0][1]


def test_gateway_denial_is_not_reported_as_success(client):
    invoke, _, _ = client
    Gateway.failure_status = 403
    try:
        result = invoke(packet("tools/list", 1))[0]
        assert result["error"]["message"] == "Gateway rejected tool request (HTTP 403)"
    finally:
        Gateway.failure_status = None


def setup_cli(tmp_path):
    cli_dir = SHIM.parent
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in ("adp", "adp_deployments.py", "bg-gateway-proxy.py", "bg-cognito-auth.sh", "adp-mcp.py", "adp_mcp_config.py", "adp_common.py",
                 "adp_native_test.py", "adp_native_credential_handoff.py"):
        shutil.copy(cli_dir / name, installed / name)
        (installed / name).chmod(0o700)
    auth = home / ".bedrock-gateway"
    auth.mkdir()
    (auth / "config.json").write_text(json.dumps({"gateway_url": "https://gateway.example.invalid"}))
    (auth / "tokens.json").write_text(json.dumps({"access_token": "fixture", "expires_at": 4102444800}))
    env = {"HOME": str(home), "ADP_HOME": str(home / ".adp"), "PATH": os.environ["PATH"]}

    def run(*args):
        return subprocess.run(["bash", str(installed / "adp"), *args], env=env, capture_output=True, text=True)

    return home, run


def expected_entry(tmp_path):
    return {"command": str(tmp_path / "installed/adp"),
            "args": ["--deployment", "default", "mcp"],
            "env": {"BG_CONFIG_DIR": str(tmp_path / "home/.bedrock-gateway"),
                    "ADP_DEPLOYMENT_ID": "default", "ADP_DEPLOYMENT_URL": "https://gateway.example.invalid/api"}}


def test_standalone_registration_merges_both_clients(tmp_path):
    import tomllib

    home, run = setup_cli(tmp_path)
    claude = home / ".claude.json"
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text('{"env": {"KEEP": "value"}}')
    claude.write_text(json.dumps({"existing": 7, "mcpServers": {"other": {"command": "other"}}}))
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('model = "original"\n[mcp_servers.other]\ncommand = "other"\n')
    assert run("mcp", "configure", "--client", "claude-code").returncode == 0
    assert run("mcp", "configure", "--client", "codex").returncode == 0
    claude_content = claude.read_text()
    codex_content = codex.read_text()
    assert json.loads(claude_content) == {"existing": 7, "mcpServers": {
        "other": {"command": "other"}, "adp-tools": expected_entry(tmp_path)}}
    assert tomllib.loads(codex_content)["mcp_servers"]["adp-tools"] == expected_entry(tmp_path)
    assert 'model = "original"' in codex_content
    assert run("mcp", "configure", "--client", "claude-code").returncode == 0
    assert run("mcp", "configure", "--client", "codex").returncode == 0
    assert claude_content == claude.read_text()
    assert codex_content == codex.read_text()
    assert settings.read_text() == '{"env": {"KEEP": "value"}}'


def test_setup_keeps_other_model_settings_and_registers_mcp(tmp_path):
    import tomllib

    home, run = setup_cli(tmp_path)
    claude = home / ".claude/settings.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps({"existing": 7, "env": {"OWNED_BY_USER": "keep"}}))
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('model = "original"\n[model_providers.other]\nname = "other"\n')
    for client_name in ("claude", "codex"):
        result = run(client_name, "setup")
        assert result.returncode == 0, result.stderr
    claude_settings = json.loads(claude.read_text())
    assert claude_settings["existing"] == 7
    assert claude_settings["env"]["OWNED_BY_USER"] == "keep"
    assert "mcpServers" not in claude_settings
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["adp-tools"] == expected_entry(tmp_path)
    codex_settings = tomllib.loads(codex.read_text())
    assert codex_settings["model_providers"]["other"]["name"] == "other"
    assert codex_settings["mcp_servers"]["adp-tools"] == expected_entry(tmp_path)
    prior = (claude.read_text(), codex.read_text())
    for client_name in ("claude", "codex"):
        result = run(client_name, "setup")
        assert result.returncode == 0, result.stderr
    assert prior == (claude.read_text(), codex.read_text())


def test_configure_rejects_invalid_client_without_mutating_settings(tmp_path):
    home, run = setup_cli(tmp_path)
    result = run("mcp", "configure", "--client", "unknown")
    assert result.returncode != 0
    assert not (home / ".claude").exists()
    assert not (home / ".codex").exists()


def test_standalone_registration_preserves_invalid_settings(tmp_path):
    home, run = setup_cli(tmp_path)
    claude = home / ".claude.json"
    claude.write_text('{"mcpServers": ["not an object"]}')
    result = run("mcp", "configure", "--client", "claude-code")
    assert result.returncode != 0
    assert claude.read_text() == '{"mcpServers": ["not an object"]}'


def test_mid_session_uses_fresh_token_for_each_request(client, tmp_path):
    invoke, _, _ = client
    (tmp_path / "bin/adp").write_text(
        '#!/bin/sh\ncount=$(cat "$HOME/token-count" 2>/dev/null || printf 0)\n'
        'count=$((count + 1))\nprintf "%s" "$count" > "$HOME/token-count"\n'
        'if [ "$count" -eq 1 ]; then printf "old-token\\n"; else printf "refreshed-token\\n"; fi\n'
    )
    messages = invoke(packet("tools/list", 1), packet("tools/call", 2, {"name": "websearch___search", "arguments": {"query": "example"}}))
    assert all("result" in message for message in messages)
    assert [request[1]["Authorization"] for request in Gateway.requests] == [
        "Bearer old-token", "Bearer refreshed-token", "Bearer refreshed-token"]


def test_dead_refresh_token_returns_login_error_without_replaying_call(client, tmp_path):
    invoke, _, _ = client
    (tmp_path / "bin/adp").write_text(
        '#!/bin/sh\ncount=$(cat "$HOME/token-count" 2>/dev/null || printf 0)\n'
        'if [ "$count" -ge 2 ]; then exit 1; fi\n'
        'count=$((count + 1))\nprintf "%s" "$count" > "$HOME/token-count"\n'
        'printf "first-token\\n"\n'
    )
    result = invoke(packet("tools/list", 1), packet("tools/call", 2, {"name": "websearch___search", "arguments": {"query": "example"}}))
    assert result[0]["result"]["tools"]
    assert result[1]["result"]["isError"]
    error_text = json.loads(result[1]["result"]["content"][0]["text"])
    assert "run adp login" in error_text["message"]
    assert "401" not in error_text["message"]
    assert error_text["operation_ref"]
    assert [request[0] for request in Gateway.requests] == ["/api/v1/l5/mcp/tools/list", "/api/agent/tools"]


def test_expired_gateway_bearer_reports_login(client):
    invoke, _, _ = client
    Gateway.failure_status = 401
    try:
        result = invoke(packet("tools/list", 1))[0]
        assert "run adp login" in result["error"]["message"]
        assert "401" not in result["error"]["message"]
    finally:
        Gateway.failure_status = None


def test_gateway_redirect_is_not_followed_with_bearer(client):
    invoke, _, _ = client
    Gateway.failure_status = 302
    try:
        result = invoke(packet("tools/list", 1))[0]
        assert result["error"]["message"] == "Gateway rejected tool request (HTTP 302)"
    finally:
        Gateway.failure_status = None


def test_fresh_local_install_ships_shim_without_extra_packages(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "installed"
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    cli = SHIM.parent
    process = subprocess.run(
        ["bash", str(cli / "install.sh"), "--prefix", str(installed),
         "--gateway-url", "https://gateway.example.invalid", "--no-path-edit"],
        env=env, capture_output=True, text=True,
    )
    assert process.returncode == 0, process.stderr
    assert (installed / "adp-mcp.py").read_bytes() == SHIM.read_bytes()
    assert (installed / "adp_mcp_config.py").read_bytes() == (cli / "adp_mcp_config.py").read_bytes()
    assert (installed / "command-manifest.json").exists()
    assert json.loads((home / ".bedrock-gateway/config.json").read_text())["gateway_url"] == \
        "https://gateway.example.invalid/api"
    help_result = subprocess.run([str(installed / "adp"), "mcp", "--help"], env=env,
                                 capture_output=True, text=True)
    assert help_result.returncode == 0, help_result.stderr
    assert "bundled ADP stdio MCP server" in help_result.stdout


def test_normalized_installer_gateway_is_not_double_prefixed(client):
    invoke, env, server = client
    config = Path(env["BG_CONFIG_DIR"]) / "config.json"
    config.write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{server.server_port}/api"}))
    result = invoke(packet("tools/list", 1))[0]
    assert result["result"]["tools"]
    assert [request[0] for request in Gateway.requests] == ["/api/v1/l5/mcp/tools/list", "/api/agent/tools"]


def test_tools_logout_revokes_then_removes_only_own_entries(client, tmp_path):
    import tomllib

    _, _, server = client
    home, run = setup_cli(tmp_path)
    auth = home / ".bedrock-gateway"
    (auth / "config.json").write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{server.server_port}/api"}))
    (auth / "tokens.json").write_text(json.dumps({"refresh_token": "temporary-fake-refresh-token"}))
    claude = home / ".claude.json"
    claude.write_text(json.dumps({"existing": 7, "mcpServers": {"other": {"command": "other"}}}))
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('[mcp_servers.other]\ncommand = "other"\n')
    assert run("mcp", "configure", "--client", "claude-code").returncode == 0
    assert run("mcp", "configure", "--client", "codex").returncode == 0
    result = run("tools", "logout")
    assert result.returncode == 0, result.stderr
    assert not (auth / "tokens.json").exists()
    assert json.loads(claude.read_text())["mcpServers"] == {"other": {"command": "other"}}
    assert tomllib.loads(codex.read_text())["mcp_servers"] == {"other": {"command": "other"}}
    assert Gateway.requests[-1][0] == "/api/auth/cli/revoke"
    assert Gateway.requests[-1][2] == {"refresh_token": "temporary-fake-refresh-token"}
    assert "temporary-fake-refresh-token" not in result.stdout + result.stderr
    assert run("tools", "logout").returncode == 0


def test_tools_logout_preserves_session_when_revocation_unavailable(client, tmp_path):
    _, _, server = client
    home, run = setup_cli(tmp_path)
    auth = home / ".bedrock-gateway"
    (auth / "config.json").write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{server.server_port}/api"}))
    (auth / "tokens.json").write_text(json.dumps({"refresh_token": "temporary-fake-refresh-token"}))
    assert run("mcp", "configure", "--client", "claude-code").returncode == 0
    Gateway.failure_status = 503
    try:
        result = run("tools", "logout")
        assert result.returncode != 0
        assert "retry adp tools logout" in result.stderr
        assert (auth / "tokens.json").exists()
        assert "adp-tools" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    finally:
        Gateway.failure_status = None


def test_tools_status_reports_existing_token_expiry(tmp_path):
    home, run = setup_cli(tmp_path)
    response = run("tools", "status")
    assert response.returncode == 0, response.stderr
    assert "Access token: valid" in response.stdout
    assert (home / ".bedrock-gateway/tokens.json").exists()


def test_registration_refuses_unowned_codex_subtables(tmp_path):
    home, run = setup_cli(tmp_path)
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('[mcp_servers.adp-tools]\ncommand = "old"\n'
                     '[mcp_servers.adp-tools.env]\nOLD_TOKEN = "do-not-keep"\n'
                     '[mcp_servers.other]\ncommand = "other"\n')
    original = codex.read_bytes()
    response = run("mcp", "configure", "--client", "codex")
    assert response.returncode != 0
    assert "unowned" in response.stderr
    assert codex.read_bytes() == original


@pytest.mark.parametrize("client_name", ["codex", "claude"])
def test_setup_refuses_unowned_entry_without_touching_other_settings(tmp_path, client_name):
    home, run = setup_cli(tmp_path)
    if client_name == "codex":
        config = home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('model = "keep"\n[mcp_servers.adp-tools]\ncommand = "my-tool"\n')
        other = config
    else:
        config = home / ".claude.json"
        config.write_text(json.dumps({"mcpServers": {"adp-tools": {"command": "my-tool"}}}))
        other = home / ".claude/settings.json"
    original = config.read_bytes()
    result = run(client_name, "setup")
    assert result.returncode != 0 and "unowned" in result.stderr
    assert config.read_bytes() == original
    if client_name == "claude":
        assert not other.exists()


def test_invalid_codex_toml_does_not_get_replaced(tmp_path):
    home, run = setup_cli(tmp_path)
    config = home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('model = [broken\n')
    assert run("codex", "setup").returncode != 0
    assert config.read_text() == 'model = [broken\n'


def test_modified_managed_entry_is_not_silently_restored(tmp_path):
    home, run = setup_cli(tmp_path)
    config = home / ".claude.json"
    assert run("mcp", "configure", "--client", "claude-code").returncode == 0
    modified = json.loads(config.read_text())
    modified["mcpServers"]["adp-tools"]["command"] = "user-replacement"
    config.write_text(json.dumps(modified))
    before = config.read_bytes()
    response = run("mcp", "configure", "--client", "claude-code")
    assert response.returncode != 0 and "modified" in response.stderr
    assert config.read_bytes() == before


def test_managed_updates_preserve_other_servers_and_keep_a_private_backup(tmp_path):
    home, run = setup_cli(tmp_path)
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('model = "keep"\n[mcp_servers.other]\ncommand = "my-tool"\n')
    assert run("mcp", "configure", "--client", "codex").returncode == 0
    before = codex.read_bytes()
    (home / ".bedrock-gateway/config.json").write_text(json.dumps({"gateway_url": "https://different.example.invalid"}))
    assert run("mcp", "configure", "--client", "codex").returncode == 0
    config = tomllib.loads(codex.read_text())
    assert config["model"] == "keep" and config["mcp_servers"]["other"]["command"] == "my-tool"
    assert config["mcp_servers"]["adp-tools"]["env"]["ADP_DEPLOYMENT_URL"] == "https://different.example.invalid/api"
    backups = list((home / ".adp/mcp-config").glob("codex.*.backup"))
    assert any(backup.read_bytes() == before for backup in backups)
    assert all(backup.stat().st_mode & 0o777 == 0o600 for backup in backups)
    assert codex.stat().st_mode & 0o777 == 0o600


def test_concurrent_registration_and_interrupted_provenance_recovery(tmp_path):
    home, run = setup_cli(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run("mcp", "configure", "--client", "codex"), range(4)))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    codex = home / ".codex/config.toml"
    assert codex.read_text().count("[mcp_servers.adp-tools]") == 1
    state_path = home / ".adp/mcp-config/managed.json"
    state = json.loads(state_path.read_text())
    previous = state["clients"].pop("codex")
    state["pending"]["codex"] = {"path": previous["path"], "previous": None, "digest": previous["digest"]}
    state_path.write_text(json.dumps(state))
    assert run("mcp", "configure", "--client", "codex").returncode == 0
    recovered = json.loads(state_path.read_text())
    assert recovered["pending"] == {} and recovered["clients"]["codex"] == previous
    assert tomllib.loads(codex.read_text())["mcp_servers"]["adp-tools"] == expected_entry(tmp_path)


def test_generated_operation_is_private_and_saved_before_gateway_dispatch(client):
    invoke, env, _ = client
    directory = Path(env["BG_CONFIG_DIR"]) / "tool-operations"
    Gateway.operation_directory = directory
    result = invoke(packet("tools/call", "rpc-id", {"name": "websearch___search", "arguments": {"query": "local"}}))[0]
    body = Gateway.requests[0][2]
    request_id = body["request_id"]
    assert request_id != "rpc-id"
    saved = Gateway.persisted_before_call[0]
    assert saved["request_id"] == request_id
    assert saved["tool"] == "websearch___search" and saved["arguments"] == {"query": "local"}
    assert saved["status"] == "pending" and saved["caller_execution_id"] is None
    assert saved["binding"]["mode"] == "bearer"
    assert "example-token" not in json.dumps(saved)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / f"{request_id}.json").stat().st_mode) == 0o600
    receipt = json.loads(result["result"]["content"][0]["text"])
    assert receipt["operation_ref"] == request_id
    assert receipt["execution_id"] == Gateway.receipts[request_id]["receipt"]["execution_id"]


def test_lost_reply_restarts_without_automatic_replay(client):
    invoke, env, _ = client
    Gateway.drop_response = True
    first = invoke(packet("tools/call", 7, {"name": "websearch___search", "arguments": {"query": "once"}}))[0]
    uncertain = json.loads(first["result"]["content"][0]["text"])
    assert uncertain["status"] == "unknown"
    assert first["result"]["isError"]
    request_id = uncertain["operation_ref"]
    assert Gateway.provider_calls == 1
    assert len(Gateway.requests) == 1

    recovered = invoke(packet("tools/call", 8, {"name": "adp___operations_list"}))[0]
    operations = json.loads(recovered["result"]["content"][0]["text"])["operations"]
    assert operations == [{"operation_ref": request_id, "request_id": request_id, "tool": "websearch___search",
                           "execution_id": None, "status": "unknown"}]
    assert len(Gateway.requests) == 1
    retry = invoke(packet("tools/call", 9, {"name": "adp___operation_retry",
                                                  "arguments": {"request_id": request_id}}))[0]
    receipt = json.loads(retry["result"]["content"][0]["text"])
    assert receipt["status"] == "confirmed"
    assert receipt["operation_ref"] == request_id
    assert Gateway.provider_calls == 1
    assert [entry[2]["request_id"] for entry in Gateway.requests] == [request_id, request_id]
    assert Gateway.requests[-1][0] == "/api/agent/tools/operations/recover"
    saved = json.loads((Path(env["BG_CONFIG_DIR"]) / "tool-operations" / f"{request_id}.json").read_text())
    assert saved["status"] == "confirmed"
    assert saved["execution_id"] == receipt["execution_id"]


def test_changed_input_with_original_request_id_conflicts_without_dispatch(client):
    invoke, _, _ = client
    first = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "original"}}))[0]
    assert not first["result"]["isError"]
    request_id = json.loads(first["result"]["content"][0]["text"])["operation_ref"]
    changed = packet("tools/call", 2, {"name": "adp___operation_retry", "arguments": {
        "operation_ref": request_id, "tool": "websearch___search", "arguments": {"query": "changed"}}})
    refusal = json.loads(invoke(changed)[0]["result"]["content"][0]["text"])
    assert refusal["status"] == "rejected"
    assert refusal["operation_ref"] == request_id
    assert Gateway.provider_calls == 1
    assert len(Gateway.requests) == 1


def test_explicit_same_payload_retry_keeps_original_execution(client):
    invoke, _, _ = client
    first = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "original"}}))[0]
    receipt = json.loads(first["result"]["content"][0]["text"])
    request_id = receipt["operation_ref"]
    retry = invoke(packet("tools/call", 2, {"name": "adp___operation_retry", "arguments": {
        "operation_ref": request_id, "tool": "websearch___search", "arguments": {"query": "original"}}}))[0]
    receipts = [receipt, json.loads(retry["result"]["content"][0]["text"])]
    assert receipts[0]["request_id"] == receipts[1]["request_id"] == request_id
    assert receipts[0]["execution_id"] == receipts[1]["execution_id"]
    assert Gateway.provider_calls == 1
    assert Gateway.requests[0][2]["arguments"] == {"query": "original"}
    assert Gateway.requests[1][0].startswith("/api/agent/tools/executions/")
    assert Gateway.requests[1][2] is None


def test_operation_list_excludes_saved_tool_inputs(client):
    invoke, _, _ = client
    invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "private query"}}))
    listed = invoke(packet("tools/call", 2, {"name": "adp___operations_list"}))[0]
    text = listed["result"]["content"][0]["text"]
    assert "private query" not in text
    assert json.loads(text)["operations"][0]["request_id"]


def test_offline_cli_recovers_reference_even_without_gateway(client, tmp_path):
    invoke, env, _ = client
    call = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "example"}}))[0]
    request_id = json.loads(call["result"]["content"][0]["text"])["operation_ref"]
    (tmp_path / "bin/adp").write_text("#!/bin/sh\nexit 1\n")
    result = subprocess.run([sys.executable, str(tmp_path / "bin/adp-mcp.py"), "operations"],
                            env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["operations"][0]["request_id"] == request_id


def test_installed_wrapper_lists_saved_reference_without_live_login(tmp_path):
    home, run = setup_cli(tmp_path)
    operations = home / ".bedrock-gateway/tool-operations"
    operations.mkdir(mode=0o700)
    request_id = str(uuid4())
    record = operations / f"{request_id}.json"
    record.write_text(json.dumps({"request_id": request_id, "tool": "websearch___search",
                                  "arguments": {"query": "keep private"}, "status": "unknown"}))
    record.chmod(0o600)
    (home / ".bedrock-gateway/tokens.json").unlink()
    response = run("mcp", "operations")
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["operations"][0]["request_id"] == request_id
    assert "keep private" not in response.stdout


def test_owned_status_artifact_and_idempotent_close_after_token_refresh(client, tmp_path):
    invoke, _, _ = client
    token_command = tmp_path / "bin/adp"
    original = fake_token(sub="person-one", iss="https://login.example.invalid", iat=1)
    refreshed = fake_token(sub="person-one", iss="https://login.example.invalid", iat=2)
    token_command.write_text('#!/bin/sh\ncount=$(cat "$HOME/token-count" 2>/dev/null || printf 0)\n'
                             'count=$((count + 1))\nprintf "%s" "$count" > "$HOME/token-count"\n'
                             f'if [ "$count" -eq 1 ]; then echo {original}; else echo {refreshed}; fi\n')
    initial = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "example"}}))[0]["result"]
    request_id = json.loads(initial["content"][0]["text"])["operation_ref"]
    assert not initial["isError"]
    calls = [packet("tools/call", index, {"name": tool, "arguments": {"request_id": request_id}})
             for index, tool in enumerate(("adp___operation_status", "adp___artifact_read",
                                           "adp___execution_close", "adp___execution_close"), 2)]
    status, artifact, closing, again = [item["result"] for item in invoke(*calls)]
    assert not status["isError"]
    assert json.loads(status["content"][0]["text"])["status"] == "confirmed"
    chunk = json.loads(artifact["content"][0]["text"])
    data = base64.b64decode(chunk["data_base64"])
    assert json.loads(data) == {"items": ["ok"]}
    assert hashlib.sha256(data).hexdigest() == chunk["sha256"]
    assert chunk["operation_ref"] == request_id
    assert all(item["isError"] for item in (closing, again))
    assert json.loads(again["content"][0]["text"])["cleanup_status"] == "unknown"
    Gateway.close_state, Gateway.cleanup_status = "closed", "confirmed"
    done = invoke(calls[-1])[0]["result"]
    assert not done["isError"]
    assert json.loads(done["content"][0]["text"])["state"] == "closed"
    assert Gateway.provider_calls == 1
    assert all(entry[1]["Authorization"] == "Bearer " + refreshed for entry in Gateway.requests[1:])


def test_foreign_owner_denied_and_unknown_or_failed_outcome_is_not_success(client, tmp_path):
    invoke, _, _ = client
    original = fake_token(sub="person-one", iss="https://login.example.invalid", iat=1)
    (tmp_path / "bin/adp").write_text(f'#!/bin/sh\necho {original}\n')
    first = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "example"}}))[0]["result"]
    request_id = json.loads(first["content"][0]["text"])["operation_ref"]
    status = packet("tools/call", 2, {"name": "adp___operation_status", "arguments": {"request_id": request_id}})
    artifact = packet("tools/call", 3, {"name": "adp___artifact_read", "arguments": {"request_id": request_id}})
    close = packet("tools/call", 4, {"name": "adp___execution_close", "arguments": {"request_id": request_id}})
    (tmp_path / "bin/adp").write_text('#!/bin/sh\necho foreign-token\n')
    for result in invoke(status, artifact, close):
        assert result["result"]["isError"]
        assert json.loads(result["result"]["content"][0]["text"])["status"] == "rejected"
    assert Gateway.provider_calls == 1
    refreshed = fake_token(sub="person-one", iss="https://login.example.invalid", iat=2)
    (tmp_path / "bin/adp").write_text(f'#!/bin/sh\necho {refreshed}\n')
    Gateway.receipts[request_id]["receipt"].update(status="unknown")
    unknown_status, missing_artifact = invoke(status, artifact)
    assert unknown_status["result"]["isError"]
    assert json.loads(unknown_status["result"]["content"][0]["text"])["status"] == "unknown"
    assert missing_artifact["result"]["isError"]
    Gateway.receipts[request_id]["receipt"].update(status="confirmed", result_status="failed")
    failed = invoke(status)[0]["result"]
    assert failed["isError"]
    assert json.loads(failed["content"][0]["text"])["result_status"] == "failed"



def test_chunked_result_has_full_digest_and_rejects_invalid_range(client):
    invoke, _, _ = client
    first = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "example"}}))[0]["result"]
    request_id = json.loads(first["content"][0]["text"])["operation_ref"]
    Gateway.receipts[request_id]["receipt"]["result"] = {"payload": "x" * 8200}
    content = bytearray()
    digest = None
    for offset in (0, 8192):
        response = invoke(packet("tools/call", 2, {"name": "adp___artifact_read", "arguments": {
            "request_id": request_id, "offset": offset}}))[0]["result"]
        assert not response.get("isError", False)
        chunk = json.loads(response["content"][0]["text"])
        assert chunk["offset"] == offset
        assert chunk["length"] <= 8192
        content.extend(base64.b64decode(chunk["data_base64"]))
        digest = chunk["sha256"]
    assert hashlib.sha256(content).hexdigest() == digest
    assert json.loads(content)["payload"] == "x" * 8200
    prior = len(Gateway.requests)
    invalid = invoke(packet("tools/call", 3, {"name": "adp___artifact_read", "arguments": {
        "request_id": request_id, "length": 8193}}))[0]
    assert "range" in invalid["error"]["message"]
    assert len(Gateway.requests) == prior


def test_read_only_recovery_with_lost_execution_id_never_dispatches(client):
    invoke, _, _ = client
    Gateway.drop_response = True
    initial = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "example"}}))[0]["result"]
    request_id = json.loads(initial["content"][0]["text"])["operation_ref"]
    for name in ("adp___operation_status", "adp___artifact_read", "adp___execution_close"):
        result = invoke(packet("tools/call", 2, {"name": name, "arguments": {"request_id": request_id}}))[0]["result"]
        assert result["isError"]
        assert json.loads(result["content"][0]["text"])["status"] == "unknown"
    assert len(Gateway.requests) == Gateway.provider_calls == 1


def test_installed_mcp_proxy_needs_no_laptop_configuration(client, tmp_path):
    _, _, server = client
    home = tmp_path / "pod-home"
    home.mkdir()
    installed = tmp_path / "pod-bin"
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    install = subprocess.run(
        ["bash", str(SHIM.parent / "install.sh"), "--prefix", str(installed),
         "--gateway-url", "https://gateway.example.invalid", "--no-path-edit"],
        env=env, capture_output=True, text=True,
    )
    assert install.returncode == 0, install.stderr
    shutil.rmtree(home / ".bedrock-gateway")
    env.update(SIGV4_PROXY_TARGET="https://gateway.example.invalid/agent", SIGV4_PROXY_PORT=str(server.server_port))
    result = subprocess.run(
        [str(installed / "adp"), "mcp"], env=env, capture_output=True, text=True,
        input="\n".join(json.dumps(message) for message in (packet("initialize", 1), packet("tools/list", 2))) + "\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert messages[0]["result"]["serverInfo"]["name"] == "adp-tools"
    assert messages[1]["result"]["tools"][0]["name"] == "websearch___search"
    assert Gateway.requests[0][0] == "/tools"
    assert "Authorization" not in Gateway.requests[0][1]
    assert not (home / ".bedrock-gateway/tokens.json").exists()
    env.pop("SIGV4_PROXY_TARGET")
    refused = subprocess.run([str(installed / "adp"), "mcp"], env=env, capture_output=True, text=True, input="")
    assert refused.returncode != 0
    assert refused.stdout == ""
    assert "adp login" in refused.stderr


def test_identical_call_after_restart_reads_learned_execution(client):
    invoke, _, _ = client
    params = {"name": "websearch___search", "arguments": {"query": "once"}}
    first = invoke(packet("tools/call", 1, params))[0]["result"]
    reference = json.loads(first["content"][0]["text"])["operation_ref"]
    second = invoke(packet("tools/call", 2, {"name": "adp___operation_retry", "arguments": {
        "operation_ref": reference, "tool": "websearch___search", "arguments": params["arguments"]}}))[0]["result"]
    assert not first["isError"] and not second["isError"]
    assert json.loads(first["content"][0]["text"]) == json.loads(second["content"][0]["text"])
    assert Gateway.provider_calls == 1
    assert Gateway.requests[-1][0].startswith("/api/agent/tools/executions/")
    assert Gateway.requests[-1][2] is None
    conflicting = invoke(packet("tools/call", 3, {**params, "execution_id": str(uuid4())}))[0]
    assert "error" in conflicting
    assert len(Gateway.requests) == 2


@pytest.mark.parametrize("changed", ["invocation_id", "attempt", "tenant_id"])
def test_proxy_lost_reply_retry_keeps_original_run(client, tmp_path, changed):
    invoke, env, server = client
    claims = {"invocation_id": "run-one", "attempt": 1, "tenant_id": "tenant-one", "credential_epoch": 1}
    credential = tmp_path / "run-credential"
    credential.write_text(fake_token(**claims).replace("test.", "adpr1.", 1))
    env.update(SIGV4_PROXY_TARGET="https://gateway.example.invalid/agent", SIGV4_PROXY_PORT=str(server.server_port),
               ADP_RUN_CREDENTIAL_FILE=str(credential))
    Gateway.drop_response = True
    first = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "example"}}))[0]["result"]
    receipt = json.loads(first["content"][0]["text"])
    assert receipt["status"] == "unknown" and Gateway.provider_calls == 1
    retry = packet("tools/call", 2, {"name": "adp___operation_retry", "arguments": {"request_id": receipt["operation_ref"]}})
    credential.write_text(fake_token(**{**claims, changed: 2 if changed == "attempt" else "other"}))
    refused = invoke(retry)[0]["result"]
    assert refused["isError"] and json.loads(refused["content"][0]["text"])["status"] == "rejected"
    assert len(Gateway.requests) == Gateway.provider_calls == 1
    credential.write_text(fake_token(**{**claims, "credential_epoch": 2}))
    recovered = invoke(retry)[0]["result"]
    assert not recovered["isError"] and Gateway.provider_calls == 1
    assert len(Gateway.requests) == 2


@pytest.mark.parametrize("lost_reply", [True, False])
def test_record_without_saved_binding_never_resubmits(client, lost_reply):
    invoke, env, _ = client
    Gateway.drop_response = lost_reply
    first = invoke(packet("tools/call", 1, {"name": "websearch___search", "arguments": {"query": "example"}}))[0]["result"]
    request_id = json.loads(first["content"][0]["text"])["operation_ref"]
    path = Path(env["BG_CONFIG_DIR"]) / "tool-operations" / f"{request_id}.json"
    record = json.loads(path.read_text())
    del record["binding"]
    path.write_text(json.dumps(record))
    result = invoke(packet("tools/call", 2, {"name": "adp___operation_retry", "arguments": {"request_id": request_id}}))[0]["result"]
    assert result["isError"] and json.loads(result["content"][0]["text"])["status"] == "rejected"
    assert len(Gateway.requests) == 1
    assert Gateway.provider_calls == 1
