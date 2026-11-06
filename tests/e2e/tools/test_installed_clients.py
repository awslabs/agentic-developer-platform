import hashlib
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from tests.e2e.tools.installed_clients import installed_clients


SOURCE = Path(__file__).resolve().parents[3] / "modules/gateway/cli"
REVISION = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


@pytest.fixture
def served_cli():
    altered = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/tools":
                from tests.e2e.tools.client_matrix import OPERATIONS

                tools = [{"name": name, "inputSchema": {"type": "object", "properties": {},
                          "x-adp-schema-version": "1.0"}} for name in OPERATIONS if not name.startswith("adp___")]
                content = json.dumps({"tools": tools}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(content)
                return
            name = self.path.removeprefix("/api/cli/")
            if not name or "/" in name or not (SOURCE / name).is_file():
                self.send_error(404)
                return
            content = altered.get(name, (SOURCE / name).read_bytes())
            self.send_response(200)
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", altered
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_installer_checks_served_source_and_preserves_other_client_settings(served_cli):
    origin, _ = served_cli
    with installed_clients(origin, REVISION, initial_settings={
        ".claude.json": json.dumps({"model": "existing", "mcpServers": {"other": {"command": "other"}}}),
        ".codex/config.toml": 'model = "existing"\n[mcp_servers.other]\ncommand = "other"\n',
    }) as (installed, environ, hashes):
        home = Path(environ["HOME"])
        assert installed.is_file() and installed.parent == home / ".adp/bin"
        assert hashes["adp"] == hashlib.sha256((SOURCE / "adp").read_bytes()).hexdigest()
        assert hashes["adp-mcp.py"] == hashlib.sha256((SOURCE / "adp-mcp.py").read_bytes()).hexdigest()
        assert json.loads((home / ".bedrock-gateway/config.json").read_text())["gateway_url"] == origin + "/api"
        assert json.loads((home / ".claude.json").read_text())["mcpServers"]["other"] == {"command": "other"}
        assert '[mcp_servers.other]\ncommand = "other"' in (home / ".codex/config.toml").read_text()
        assert subprocess.run([str(installed), "version"], env=environ, capture_output=True).returncode == 0
    assert not home.exists()


def test_installed_cli_requires_private_host_for_full_inventory(served_cli):
    from tests.e2e.tools.client_matrix import OPERATIONS, cli_call
    from tests.e2e.tools.runner import MCPClient

    origin, _ = served_cli
    with installed_clients(origin, REVISION) as (installed, environ, _):
        environ.update(ADP_CREW_TOOLS_URL=origin, ADP_CREW_TOOLS_TOKEN="a" * 64,
                       ADP_CREW_RUN_ID=str(uuid4()))
        with MCPClient([str(installed), "mcp"], environ=environ) as client:
            listing = client.request("tools/list")
        names = [tool["name"] for tool in listing["tools"]]
        assert len(OPERATIONS) == 30
        assert len(names) == 28
        assert set(names) == set(OPERATIONS) - {"credentials___assume_role", "credentials___materialize"}
        with pytest.raises(ValueError, match="client discovery differs from the required public operation inventory"):
            cli_call(installed, environ, "adp___operations_list", {}, client_factory=MCPClient)


@pytest.mark.parametrize("binding_change", ("foreign", "missing"))
def test_installed_cli_invokes_real_mcp_process_for_owned_local_recovery(served_cli, binding_change):
    from tests.e2e.tools.client_matrix import recovery_result
    from tests.e2e.tools.runner import MCPClient

    origin, _ = served_cli
    with installed_clients(origin, REVISION) as (installed, environ, _):
        operation_ref = str(uuid4())
        execution_id = str(uuid4())
        run_id = str(uuid4())
        store = Path(environ["BG_CONFIG_DIR"]) / "tool-operations"
        store.mkdir(mode=0o700)
        record = store / (operation_ref + ".json")
        record.write_text(json.dumps({"request_id": operation_ref, "tool": "websearch___search",
                                      "execution_id": execution_id, "arguments": {"query": "isolated"},
                                      "binding": {"mode": "crew-proxy", "run_id": run_id}}))
        record.chmod(0o600)
        environ.update(ADP_CREW_TOOLS_URL=origin, ADP_CREW_TOOLS_TOKEN="a" * 64,
                       ADP_CREW_RUN_ID=run_id)
        owned = {"operation_ref": operation_ref, "receipt_id": "owned-receipt", "execution_id": execution_id}
        with MCPClient([str(installed), "mcp"], environ=environ) as client:
            assert client.version == "1.0.0"
            response = client.request("tools/call", {"name": "adp___operations_list", "arguments": {}})
        assert json.loads(response["content"][0]["text"]) == {"operations": [{
            "operation_ref": operation_ref, "request_id": operation_ref, "tool": "websearch___search",
            "execution_id": execution_id, "status": "pending"}]}
        result = recovery_result(response, "adp___operations_list", owned)
        assert result["operation_ref"] == operation_ref
        assert result["receipt_sha256"]
        assert json.loads(record.read_text())["request_id"] == operation_ref
        unowned_record = json.loads(record.read_text())
        if binding_change == "foreign":
            unowned_record["binding"]["run_id"] = str(uuid4())
        else:
            del unowned_record["binding"]
        record.write_text(json.dumps(unowned_record))
        with MCPClient([str(installed), "mcp"], environ=environ) as client:
            response = client.request("tools/call", {"name": "adp___operations_list", "arguments": {}})
        assert json.loads(response["content"][0]["text"]) == {"operations": []}
        with pytest.raises(ValueError, match="recovery list omitted the owned operation"):
            recovery_result(response, "adp___operations_list", owned)
        assert json.loads(record.read_text()) == unowned_record


@pytest.mark.parametrize("name", ("install.sh", "adp-mcp.py"))
def test_changed_served_or_installed_bytes_fail_closed(served_cli, name):
    origin, altered = served_cli
    altered[name] = b"#!/bin/sh\necho altered\n"
    with pytest.raises(ValueError, match="differ.*from pinned gateway source"):
        with installed_clients(origin, REVISION):
            pytest.fail("mismatched bytes must never be used")


def test_revision_and_gateway_must_be_explicit():
    with pytest.raises(ValueError, match="pinned gateway source revision"):
        with installed_clients("https://example.invalid", "main"):
            pass
    with pytest.raises(ValueError, match="gateway origin"):
        with installed_clients("https://user:secret@example.invalid", REVISION):
            pass


def test_unknown_operation_survives_client_exit_with_original_id(served_cli, tmp_path):
    from uuid import uuid4

    origin, _ = served_cli
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    recovery_id = str(uuid4())
    original_id = str(uuid4())
    with pytest.raises(TimeoutError):
        with installed_clients(origin, REVISION, recovery_root=root, recovery_id=recovery_id) as (_, environ, _):
            store = Path(environ["BG_CONFIG_DIR"]) / "tool-operations"
            store.mkdir(mode=0o700)
            (store / (original_id + ".json")).write_text(json.dumps({"request_id": original_id}))
            (Path(environ["BG_CONFIG_DIR"]) / "tokens.json").write_text("synthetic-private-token")
            raise TimeoutError("uncertain provider reply")
    saved = root / recovery_id / ".bedrock-gateway" / "tool-operations" / (original_id + ".json")
    assert json.loads(saved.read_text())["request_id"] == original_id
    assert not (root / recovery_id / ".bedrock-gateway" / "tokens.json").exists()


def test_no_operation_does_not_leave_recovery_home(served_cli, tmp_path):
    from uuid import uuid4

    origin, _ = served_cli
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    recovery_id = str(uuid4())
    with installed_clients(origin, REVISION, recovery_root=root, recovery_id=recovery_id):
        pass
    assert not (root / recovery_id).exists()


def test_recovery_root_must_be_private_before_any_install(served_cli, tmp_path):
    from uuid import uuid4

    origin, _ = served_cli
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(ValueError, match="owned private"):
        with installed_clients(origin, REVISION, recovery_root=root, recovery_id=str(uuid4())):
            pytest.fail("shared recovery root used")


def test_model_setup_uses_installed_adp_transport_not_standalone_keys(tmp_path, monkeypatch):
    from tests.e2e.tools import installed_clients as module

    home = tmp_path / "home"
    home.mkdir()
    config_dir = home / ".bedrock-gateway"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"gateway_url": "https://gateway.example.invalid/api"}))
    installed = home / ".adp/bin/adp"
    commands = []

    def configure(command, environ):
        commands.append(command)
        if command[1] == "claude":
            settings = home / ".claude/settings.json"
            settings.parent.mkdir(exist_ok=True)
            settings.write_text(json.dumps({"apiKeyHelper": str(installed) + " token",
                "env": {"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
                        "ANTHROPIC_BEDROCK_BASE_URL": "https://gateway.example.invalid/api"}}))
        else:
            settings = home / ".codex/config.toml"
            settings.parent.mkdir(exist_ok=True)
            settings.write_text('model_provider = "adp-gateway"\n[model_providers.adp-gateway]\n'
                                'base_url = "http://127.0.0.1:9191/openai/v1"\n'
                                'env_key = "ADP_GATEWAY_DUMMY"\n')

    monkeypatch.setattr(module, "run_client", configure)
    module.configure_model_transport(installed, {"HOME": str(home), "BG_CONFIG_DIR": str(config_dir)})
    assert commands == [[str(installed), "claude", "setup"], [str(installed), "codex", "setup"]]
    (home / ".claude/settings.json").write_text(json.dumps({"apiKeyHelper": "external token"}))
    monkeypatch.setattr(module, "run_client", lambda *_: None)
    with pytest.raises(ValueError, match="Claude model transport"):
        module.configure_model_transport(installed, {"HOME": str(home), "BG_CONFIG_DIR": str(config_dir)})


def test_retained_operation_store_reopens_with_original_source_only(served_cli, tmp_path):
    from uuid import uuid4
    from tests.e2e.tools.installed_clients import resume_installed_clients

    origin, _ = served_cli
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    recovery_id = str(uuid4())
    original = str(uuid4())
    with installed_clients(origin, REVISION, recovery_root=root, recovery_id=recovery_id) as (_, environ, _):
        store = Path(environ["BG_CONFIG_DIR"]) / "tool-operations"
        store.mkdir(mode=0o700)
        record = store / (original + ".json")
        record.write_text(json.dumps({"request_id": original, "status": "unknown"}))
        record.chmod(0o600)
    with resume_installed_clients(origin, REVISION, recovery_root=root, recovery_id=recovery_id) as (adp, env, hashes):
        assert adp.exists() and hashes["adp"] == hashlib.sha256(adp.read_bytes()).hexdigest()
        assert (Path(env["BG_CONFIG_DIR"]) / "tool-operations" / (original + ".json")).exists()
    with pytest.raises(ValueError, match="different deployment"):
        with resume_installed_clients("https://elsewhere.example.invalid", REVISION,
                                      recovery_root=root, recovery_id=recovery_id):
            pytest.fail("wrong deployment rebound saved operations")


@pytest.mark.parametrize("interrupted", (False, True))
def test_detached_owned_proxy_stops_before_home_cleanup(served_cli, tmp_path, interrupted):
    import os
    import sys

    origin, _ = served_cli
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    recovery_id = str(uuid4())
    proxy = None
    try:
        try:
            with installed_clients(origin, REVISION, recovery_root=root, recovery_id=recovery_id) as (_, environ, _):
                proxy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                         start_new_session=True)
                assert os.getpgid(proxy.pid) == proxy.pid != os.getpgrp()
                birth = Path(f"/proc/{proxy.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
                config = Path(environ["BG_CONFIG_DIR"])
                record = config / "proxy.json"
                record.write_text(json.dumps({"pid": proxy.pid, "process_start": "proc:" + birth,
                    "proxy": "adp-gateway-proxy", "deployment_id": "default", "gateway_url": origin + "/api"}))
                record.chmod(0o600)
                store = config / "tool-operations"
                store.mkdir(mode=0o700)
                (store / "saved-operation.json").write_text("{}")
                if interrupted:
                    raise TimeoutError("owned evaluation stopped")
        except TimeoutError:
            assert interrupted
        assert proxy.wait(timeout=1) == -15
        assert (root / recovery_id / ".bedrock-gateway/tool-operations/saved-operation.json").exists()
    finally:
        if proxy is not None and proxy.poll() is None:
            proxy.kill()
            proxy.wait(timeout=2)


@pytest.mark.parametrize("mismatch", ("birth", "target", "deployment", "symlink"))
def test_proxy_cleanup_refuses_foreign_or_recycled_process(served_cli, mismatch):
    import sys
    from tests.e2e.tools.installed_clients import stop_owned_proxies

    origin, _ = served_cli
    with installed_clients(origin, REVISION) as (installed, environ, _):
        proxy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        record = Path(environ["BG_CONFIG_DIR"]) / "proxy.json"
        try:
            birth = Path(f"/proc/{proxy.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            value = {"pid": proxy.pid, "process_start": "proc:" + birth,
                     "proxy": "adp-gateway-proxy", "deployment_id": "default", "gateway_url": origin + "/api"}
            if mismatch == "birth":
                value["process_start"] = "proc:0"
            elif mismatch == "target":
                value["gateway_url"] = "https://foreign.invalid/api"
            elif mismatch == "deployment":
                value["deployment_id"] = "other"
            record.write_text(json.dumps(value))
            record.chmod(0o600)
            if mismatch == "symlink":
                target = record.with_name("foreign.json")
                record.rename(target)
                record.symlink_to(target)
            with pytest.raises(ValueError, match="proxy"):
                stop_owned_proxies(installed, environ, origin + "/api")
            assert proxy.poll() is None
        finally:
            record.unlink(missing_ok=True)
            proxy.kill()
            proxy.wait(timeout=2)


@pytest.mark.parametrize("persistent", (False, True))
def test_unknown_proxy_cleanup_retains_identity_without_operation(served_cli, tmp_path, monkeypatch, persistent):
    import shutil
    import sys
    from tests.e2e.tools import installed_clients as module

    origin, _ = served_cli
    options = {}
    if persistent:
        root = tmp_path / "recovery"
        root.mkdir(mode=0o700)
        options = {"recovery_root": root, "recovery_id": str(uuid4())}
    proxy = None
    home = None
    try:
        with pytest.raises(OSError, match="unavailable"):
            with installed_clients(origin, REVISION, **options) as (_, environ, _):
                home = Path(environ["HOME"])
                config = Path(environ["BG_CONFIG_DIR"])
                proxy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
                birth = Path(f"/proc/{proxy.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
                record = config / "proxy.json"
                record.write_text(json.dumps({"pid": proxy.pid, "process_start": "proc:" + birth,
                    "proxy": "adp-gateway-proxy", "deployment_id": "default", "gateway_url": origin + "/api"}))
                record.chmod(0o600)
                (config / "tokens.json").write_text("synthetic test session")
                monkeypatch.setattr(module, "_open_pidfd", lambda _: (_ for _ in ()).throw(OSError("pidfd unavailable")))
        assert proxy.poll() is None
        assert json.loads(record.read_text())["pid"] == proxy.pid
        assert json.loads((home / "proxy-cleanup-pending.json").read_text())["status"] == "unconfirmed"
        assert not (config / "tokens.json").exists()
    finally:
        if proxy is not None and proxy.poll() is None:
            proxy.kill()
            proxy.wait(timeout=2)
        if home is not None:
            shutil.rmtree(home)
