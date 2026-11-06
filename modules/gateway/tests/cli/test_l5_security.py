"""L5 CLI uses the same scoped Product operations and revisions as the UI."""

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import override

import pytest

from tests.cli.conftest import MockGatewayHandler, MockGatewayServer

CLI = Path(__file__).parents[2] / "cli"
spec = importlib.util.spec_from_file_location("adp_service", CLI / "adp-service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
SCOPE = ["--product", "example-product", "--project", "example-project", "--json"]
BASE = "/v1/products/example-product/projects/example-project/services/security"


class FakeClient:
    def __init__(self):
        self.calls = []
        self.configuration = {"revision": 3, "hash": "opaque-revision", "settings": {"enabled": True}}
        self.configurations = {BASE: self.configuration}

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        scope = path.removesuffix("/configuration")
        if path.endswith("/configuration") and method == "PUT":
            self.configurations[scope] = {**self.configurations.get(scope, self.configuration), "revision": 4, "settings": body}
            if scope == BASE:
                self.configuration = self.configurations[scope]
        if path.endswith("/configuration"):
            return self.configurations.get(scope, {"revision": 1, "hash": "other-revision", "settings": {"enabled": False}})
        if path == BASE + "/configuration/options":
            return {"capabilities": ["threat_model", "code_review"]}
        if path == BASE + "/configuration/check":
            return {"ready": True, "revision": self.configuration["revision"]}
        if path == BASE + "/binding-operations" and method == "POST":
            return {"operation_id": "example-operation", "action": body["action"], "state": "accepted"}
        return {"items": []}


def run(client, *arguments):
    args = service.parser().parse_args([*arguments, *SCOPE])
    return service.execute(args, client)["detail"]


def test_configuration_transport_parity(tmp_path):
    client = FakeClient()
    assert run(client, "options")["capabilities"] == ["threat_model", "code_review"]
    assert run(client, "check")["revision"] == 3
    assert run(client, "config", "show") == client.configuration
    body = {"enabled": True, "monitoring": {"poll_interval_seconds": 600}, "triggers": [{"target": "example-target"}]}
    source = tmp_path / "settings.json"
    source.write_text(json.dumps(body))
    result = run(client, "config", "update", "--input", str(source), "--if-match", '"opaque-revision"', "--idempotency-key", "example-key-12345678")
    assert result["settings"] == body
    assert client.calls[-1] == (
        "PUT",
        BASE + "/configuration",
        body,
        {"extra_headers": {"If-Match": '"opaque-revision"', "Idempotency-Key": "example-key-12345678"}},
    )
    assert run(client, "config", "show")["settings"] == body
    assert run(client, "config", "history") == {"items": []}
    assert client.calls[-1][1] == BASE + "/configuration/versions"
    other = service.parser().parse_args(["config", "show", "--product", "other-product", "--project", "example-project", "--json"])
    assert service.execute(other, client)["detail"]["settings"] == {"enabled": False}
    assert run(client, "config", "show")["settings"] == body


def test_finops_cli_uses_shared_scoped_operations(tmp_path):
    client = FakeClient()
    scope = ["--product", "example-product", "--project", "example-project", "--service", "finops", "--json"]
    base = BASE.replace("/security", "/finops")

    def execute(*command):
        return service.execute(service.parser().parse_args([*command, *scope]), client)["detail"]

    execute("options")
    assert client.calls[-1][0:2] == ("GET", base + "/configuration/options")
    execute("config", "show")
    assert client.calls[-1][0:2] == ("GET", base + "/configuration")

    config = tmp_path / "finops-config.json"
    config.write_text(json.dumps({"enabled": True, "finops": {"agent_mode": "adopt"}}))
    execute("config", "update", "--input", str(config), "--if-none-match", "--idempotency-key", "example-key-12345678")
    assert client.calls[-1][0:3] == ("PUT", base + "/configuration", json.loads(config.read_text()))
    assert client.calls[-1][3]["extra_headers"] == {"If-None-Match": "*", "Idempotency-Key": "example-key-12345678"}

    binding = tmp_path / "finops-binding.json"
    binding.write_text(json.dumps({"action": "link", "configuration_revision": 1}))
    execute("binding", "link", "--input", str(binding), "--idempotency-key", "example-key-12345678")
    assert client.calls[-1][0:2] == ("POST", base + "/binding-operations")
    execute("binding", "status", "--operation-id", "example-operation")
    assert client.calls[-1][0:2] == ("GET", base + "/binding-operations/example-operation")

    inquiry = tmp_path / "finops-inquiry.json"
    inquiry.write_text(json.dumps({"service_id": "finops", "capability": "cost_inquiry"}))
    execute("run", "--input", str(inquiry), "--idempotency-key", "example-key-12345678")
    assert client.calls[-1][0:3] == ("POST", base + "/runs", json.loads(inquiry.read_text()))
    execute("status", "--run-id", "example-run")
    assert client.calls[-1][0:2] == ("GET", base + "/runs/example-run")
    execute("result", "show", "--result-id", "example-result")
    assert client.calls[-1][0:2] == ("GET", base + "/results/example-result")

    inquiry.write_text(json.dumps({"service_id": "devops", "capability": "cost_inquiry"}))
    with pytest.raises(service.common.CliError, match="must match --service"):
        execute("run", "--input", str(inquiry), "--idempotency-key", "example-key-12345678")
    assert client.calls[-1][1] == base + "/results/example-result"
    with pytest.raises(service.common.CliError, match="unavailable for FinOps"):
        execute("findings", "--result-id", "example-result")
    assert client.calls[-1][1] == base + "/results/example-result"


def test_cli_binding_actions_retain_input_and_conditions(tmp_path):
    client = FakeClient()
    source = tmp_path / "binding.json"
    source.write_text(json.dumps({"action": "link", "configuration_revision": 4, "native_resource_ref": {"record_id": "example-native"}}))
    result = run(client, "binding", "link", "--input", str(source), "--idempotency-key", "example-key-12345678")
    assert result["operation_id"] == "example-operation"
    assert client.calls[-1][1] == BASE + "/binding-operations"
    assert client.calls[-1][-1]["extra_headers"] == {"Idempotency-Key": "example-key-12345678"}
    source.write_text(json.dumps({"action": "unlink", "binding_id": "example-binding"}))
    run(client, "binding", "unlink", "--input", str(source), "--if-match", '"3"', "--idempotency-key", "example-key-12345678")
    assert client.calls[-1][-1]["extra_headers"]["If-Match"] == '"3"'
    assert run(client, "binding", "status", "--operation-id", "example-operation") == {"items": []}
    assert client.calls[-1][1] == BASE + "/binding-operations/example-operation"


def test_cli_rejects_bad_input_before_request(tmp_path):
    client = FakeClient()
    source = tmp_path / "bad.json"
    source.write_text("[]")
    with pytest.raises(service.common.CliError, match="JSON object"):
        run(client, "config", "update", "--input", str(source), "--if-none-match", "--idempotency-key", "example-key-12345678")
    assert not client.calls
    source.write_text(json.dumps({"action": "create"}))
    with pytest.raises(service.common.CliError, match="action must match"):
        run(client, "binding", "link", "--input", str(source), "--idempotency-key", "example-key-12345678")
    assert not client.calls


def test_denied_product_does_not_disclose_other_tenant_or_server_secrets(monkeypatch, capsys):
    denied = "/v1/products/other-product/projects/example-project/services/security/configuration"
    own_configuration = {"revision": 3, "hash": "own-revision", "settings": {"enabled": True}}
    with MockGatewayServer() as gateway:
        gateway.set_response("/api" + BASE + "/configuration", 200, own_configuration, {"ETag": '"own-opaque-token"'})
        gateway.set_response("/api" + denied, 403, {"detail": {"code": "access_denied", "message": "private-other-tenant-source-secret"}})
        monkeypatch.setattr(service.common, "gateway_url", lambda: gateway.url + "/api")
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        assert service.main(["config", "show", *SCOPE]) == 0
        assert json.loads(capsys.readouterr().out)["detail"] == own_configuration
        assert service.main(["config", "show", "--product", "other-product", "--project", "example-project", "--json"]) == 3
        output = capsys.readouterr()
        failure = json.loads(output.out)
        assert failure["status"] == "failed"
        assert failure["error"]["code"] == "access_denied"
        assert failure["detail"] == {}
        assert "private-other-tenant-source-secret" not in output.out + output.err
        assert "own-revision" not in output.out + output.err
        assert [request["path"] for request in gateway.requests] == ["/api" + BASE + "/configuration", "/api" + denied]
        assert all(request["headers"]["Authorization"] == "Bearer example-session" for request in gateway.requests)


class InstalledSecurityGateway(MockGatewayHandler):
    owners = {}
    configurations = {}
    requests = []
    runs = {}
    provider_runs = 0
    dropped = False

    def respond(self, status, payload, etag=None):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(data)

    def scoped_product(self):
        parts = self.path.removeprefix("/api/v1/products/").split("/")
        if len(parts) < 6 or parts[1:4:2] != ["projects", "services"] or parts[4] not in {"security", "finops"}:
            return None
        product = parts[0]
        if self.owners.get(product) != self.headers.get("Authorization"):
            self.respond(404, {"detail": {"code": "not_found"}})
            return None
        return product

    @override
    def do_GET(self):
        if self.path.startswith("/api/cli/"):
            filename = self.path.removeprefix("/api/cli/")
            if filename not in (CLI / "install.sh").read_text().split('CLI_FILES="', 1)[1].split('"', 1)[0].split():
                self.respond(404, {"detail": {"code": "not_found"}})
                return
            contents = (CLI / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(contents)))
            self.end_headers()
            self.wfile.write(contents)
            return
        if self.path == "/api/me/cli-capabilities":
            self.respond(
                200,
                {
                    "schema_version": "2026-09-21",
                    "gateway": {"state": "yes", "release": "example"},
                    "tenant": {"org_id": "example-tenant"},
                    "operations": [{"id": "service.mutate", "supported": "yes", "enabled": "yes", "permitted": "yes", "ready": "yes"}],
                },
            )
            return
        if self.path == "/api/workspaces":
            self.respond(404, {"detail": {"code": "not_found"}})
            return
        self.__class__.requests.append(("GET", self.path, self.headers.get("Authorization")))
        product = self.scoped_product()
        if product is None:
            return
        if self.path.endswith("/configuration"):
            current = self.configurations[product]
            self.respond(200, current, '"' + current["hash"] + '"')
        elif self.path.endswith("/configuration/options"):
            self.respond(200, {"capabilities": ["threat_model", "code_review"]})
        elif self.path.endswith("/runs/example-run"):
            receipt = next((item for (owner, _), item in self.runs.items() if owner == product), None)
            if receipt is None:
                self.respond(404, {"detail": {"code": "not_found"}})
            else:
                self.respond(200, receipt, '"' + str(receipt["revision"]) + '"')
        elif self.path.split("?", 1)[0].endswith("/results"):
            self.respond(200, {"items": []})
        else:
            self.respond(404, {"detail": {"code": "not_found"}})

    @override
    def do_PUT(self):
        self.__class__.requests.append(("PUT", self.path, self.headers.get("Authorization")))
        product = self.scoped_product()
        if product is None:
            return
        old = self.configurations[product]
        if self.headers.get("If-Match") != '"' + old["hash"] + '"':
            self.respond(412, {"detail": {"code": "stale_etag"}})
            return
        if self.headers.get("Idempotency-Key") != "example-key-12345678":
            self.respond(428, {"detail": {"code": "precondition_required"}})
            return
        settings = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        updated = {"revision": old["revision"] + 1, "hash": "revision-2", "settings": settings}
        self.configurations[product] = updated
        self.respond(200, updated, '"revision-2"')

    @override
    def do_POST(self):
        import socket

        self.__class__.requests.append(("POST", self.path, self.headers.get("Authorization")))
        product = self.scoped_product()
        if product is None:
            return
        if not self.path.endswith("/runs"):
            self.respond(404, {"detail": {"code": "not_found"}})
            return
        submitted = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if submitted["sources"][0]["tenant_id"] != "example-tenant":
            self.respond(403, {"detail": {"code": "access_denied", "message": "private-provider-secret"}})
            return
        key = (product, self.headers["Idempotency-Key"])
        if key not in self.runs:
            self.__class__.provider_runs += 1
            self.runs[key] = {
                "run_id": "example-run",
                "state": "admitted",
                "revision": 1,
                "deadline_at": "2026-10-27T21:00:00Z",
                "scope": {"tenant_id": "example-tenant", "product_id": product, "project_id": "example-project"},
                "monitoring": {"last_checked_at": None, "advisories": []},
            }
        if not self.dropped:
            self.__class__.dropped = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.respond(202, self.runs[key], '"1"')


class CrossClientInstalledGateway(InstalledSecurityGateway):
    old_server = False
    capability_reads = 0
    write_conditions = []

    @override
    def do_GET(self):
        if self.path == "/api/me/cli-capabilities":
            self.__class__.capability_reads += 1
            if self.old_server:
                self.respond(404, {"detail": {"code": "not_found"}})
                return
        super().do_GET()

    @override
    def do_PUT(self):
        self.__class__.write_conditions.append(self.headers.get("If-Match"))
        super().do_PUT()


@pytest.fixture
def installed_cross_client_gateway(tmp_path):
    import base64
    import os
    import subprocess
    import threading
    from http.server import ThreadingHTTPServer

    from tests.cli.conftest import write_adp_session

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), CrossClientInstalledGateway)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    try:
        address = f"http://127.0.0.1:{gateway.server_port}/api"
        binary = tmp_path / "bin"
        home = tmp_path / "home"
        home.mkdir()
        environment = {
            **os.environ,
            "HOME": str(home),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        installed = subprocess.run(
            ["sh", str(CLI / "install.sh"), "--gateway-url", address, "--prefix", str(binary), "--no-path-edit"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert installed.returncode == 0, installed.stderr
        assert (binary / "adp-service.py").read_bytes() == (CLI / "adp-service.py").read_bytes()

        def session(directory, username, tenant):
            write_adp_session(directory, username=username)
            tokens_file = directory / ".bedrock-gateway" / "tokens.json"
            tokens = json.loads(tokens_file.read_text())
            segments = tokens["access_token"].split(".")
            claims = json.loads(base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)))
            claims["org_id"] = tenant
            segments[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            tokens["access_token"] = ".".join(segments)
            tokens_file.write_text(json.dumps(tokens))
            return "Bearer " + tokens["access_token"]

        authorization = session(home, "example-alice", "example-tenant")
        config = home / ".bedrock-gateway" / "config.json"
        config.write_text(json.dumps({**json.loads(config.read_text()), "gateway_url": address}))
        other_home = tmp_path / "other-home"
        other_home.mkdir()
        other_authorization = session(other_home, "example-bob", "other-tenant")
        CrossClientInstalledGateway.owners = {"example-product": authorization, "other-product": other_authorization}
        CrossClientInstalledGateway.configurations = {
            "example-product": {"revision": 1, "hash": "opaque-a7", "settings": {"enabled": False}},
            "other-product": {"revision": 7, "hash": "opaque-other", "settings": {"enabled": True}},
        }
        CrossClientInstalledGateway.requests = []
        CrossClientInstalledGateway.capability_reads = 0
        CrossClientInstalledGateway.write_conditions = []
        CrossClientInstalledGateway.old_server = False
        yield gateway, address, binary, environment, authorization, other_authorization
    finally:
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=2)


def test_cli_configuration_parity(tmp_path):
    import base64
    import os
    import subprocess
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen

    from tests.cli.conftest import write_adp_session

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), InstalledSecurityGateway)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        address = f"http://127.0.0.1:{gateway.server_port}/api"
        installer = CLI / "install.sh"
        binary = tmp_path / "bin"
        home = tmp_path / "home"
        home.mkdir()
        environment = {
            **os.environ,
            "HOME": str(home),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        installed = subprocess.run(
            ["sh", str(installer), "--gateway-url", address, "--prefix", str(binary), "--no-path-edit"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert installed.returncode == 0, installed.stderr
        assert (binary / "adp-service.py").read_bytes() == (CLI / "adp-service.py").read_bytes()
        write_adp_session(home)
        config = home / ".bedrock-gateway" / "config.json"
        config.write_text(json.dumps({**json.loads(config.read_text()), "gateway_url": address}))
        tokens_file = home / ".bedrock-gateway" / "tokens.json"
        tokens = json.loads(tokens_file.read_text())
        segments = tokens["access_token"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)))
        claims["org_id"] = "example-tenant"
        segments[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        tokens["access_token"] = ".".join(segments)
        tokens_file.write_text(json.dumps(tokens))
        authorization = "Bearer " + tokens["access_token"]
        InstalledSecurityGateway.owners = {"example-product": authorization, "other-product": "Bearer other-tenant"}
        InstalledSecurityGateway.configurations = {
            "example-product": {"revision": 1, "hash": "revision-1", "settings": {"enabled": False}},
            "other-product": {"revision": 7, "hash": "other-revision", "settings": {"enabled": True}},
        }
        InstalledSecurityGateway.requests = []
        other_home = tmp_path / "other-home"
        other_home.mkdir()
        write_adp_session(other_home, username="example-bob")
        other_token = json.loads((other_home / ".bedrock-gateway" / "tokens.json").read_text())["access_token"]
        InstalledSecurityGateway.owners["other-product"] = "Bearer " + other_token
        source = tmp_path / "settings.json"
        source.write_text(json.dumps({"enabled": True, "triggers": [{"target": "example-target"}], "monitoring": {"poll_interval_seconds": 600}}))

        def cli(*arguments):
            result = subprocess.run([str(binary / "adp"), "service", *arguments, *SCOPE], capture_output=True, text=True, env=environment, timeout=30)
            assert result.returncode == 0, result.stderr + result.stdout
            return json.loads(result.stdout)["detail"]

        assert cli("options")["capabilities"] == ["threat_model", "code_review"]
        assert cli("config", "show")["revision"] == 1
        updated = cli("config", "update", "--input", str(source), "--if-match", '"revision-1"', "--idempotency-key", "example-key-12345678")
        assert updated["revision"] == 2
        independent = Request(address + BASE + "/configuration", headers={"Authorization": authorization})
        with urlopen(independent, timeout=3) as response:
            assert response.headers["ETag"] == '"revision-2"'
            assert json.load(response) == updated
        assert cli("config", "show") == updated
        other_path = BASE.replace("example-product", "other-product") + "/configuration"
        denied = subprocess.run(
            [str(binary / "adp"), "service", "config", "show", "--product", "other-product", "--project", "example-project", "--json"],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        assert denied.returncode == 5
        assert json.loads(denied.stdout)["error"]["code"] == "not_found"
        assert "other-revision" not in denied.stdout + denied.stderr
        with urlopen(Request(address + other_path, headers={"Authorization": "Bearer " + other_token}), timeout=3) as response:
            assert json.load(response)["revision"] == 7
        assert InstalledSecurityGateway.configurations["other-product"]["revision"] == 7
        assert InstalledSecurityGateway.configurations["example-product"]["settings"] == json.loads(source.read_text())
        assert [method for method, _, _ in InstalledSecurityGateway.requests].count("PUT") == 1
        assert [(method, path) for method, path, bearer in InstalledSecurityGateway.requests if bearer != authorization] == [
            ("GET", "/api" + other_path)
        ]
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=2)


def test_installed_finops_cli_reads_scoped_configuration(installed_cross_client_gateway):
    import subprocess

    _gateway, _address, binary, environment, _authorization, _other_authorization = installed_cross_client_gateway
    response = subprocess.run(
        [
            str(binary / "adp"),
            "service",
            "config",
            "show",
            "--product",
            "example-product",
            "--project",
            "example-project",
            "--service",
            "finops",
            "--json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["etag"] == '"opaque-a7"'
    assert CrossClientInstalledGateway.requests[-1][:2] == (
        "GET",
        "/api/v1/products/example-product/projects/example-project/services/finops/configuration",
    )


def test_cross_client_and_compatibility(installed_cross_client_gateway, tmp_path):
    import subprocess
    from urllib.request import Request, urlopen

    _gateway, address, binary, environment, authorization, other_authorization = installed_cross_client_gateway
    configuration_path = address + BASE + "/configuration"

    def cli(*arguments, scope=SCOPE):
        response = subprocess.run([str(binary / "adp"), "service", *arguments, *scope], env=environment, capture_output=True, text=True, timeout=30)
        return response.returncode, json.loads(response.stdout), response.stderr

    code, original, stderr = cli("config", "show")
    assert code == 0, stderr
    assert original["detail"]["revision"] == 1
    assert original["etag"] == '"opaque-a7"'

    ui_settings = {"enabled": True, "triggers": [{"target": "example-target"}], "monitoring": {"poll_interval_seconds": 600}}
    ui_edit = Request(
        configuration_path,
        method="PUT",
        data=json.dumps(ui_settings).encode(),
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
            "If-Match": original["etag"],
            "Idempotency-Key": "example-key-12345678",
        },
    )
    with urlopen(ui_edit, timeout=3) as response:
        stored = json.load(response)
        ui_etag = response.headers["ETag"]
    assert stored["settings"] == ui_settings
    assert stored["revision"] == 2
    assert ui_etag == '"revision-2"'

    source = tmp_path / "cli-settings.json"
    source.write_text(json.dumps({"enabled": False}))

    def update(etag):
        return cli("config", "update", "--input", str(source), "--idempotency-key", "example-key-12345678", "--if-match", etag)

    code, stale, stderr = update(original["etag"])
    assert code == 4 and stale["error"]["code"] == "stale_revision"
    assert stale["detail"] == {} and "example-target" not in stderr + json.dumps(stale)
    assert CrossClientInstalledGateway.write_conditions == [original["etag"], original["etag"]]

    code, refreshed, stderr = cli("config", "show")
    assert code == 0, stderr
    assert refreshed["detail"] == stored
    assert refreshed["etag"] == ui_etag

    CrossClientInstalledGateway.old_server = True
    code, unsupported, stderr = update(refreshed["etag"])
    assert code == 5 and unsupported["error"]["code"] == "unsupported_operation"
    assert unsupported["detail"] == {}
    assert CrossClientInstalledGateway.write_conditions == [original["etag"], original["etag"]]
    assert CrossClientInstalledGateway.capability_reads == 2
    assert "example-target" not in stderr + json.dumps(unsupported)

    code, current, stderr = cli("config", "show")
    assert code == 0 and current["detail"] == stored and current["etag"] == ui_etag, stderr
    with urlopen(Request(configuration_path, headers={"Authorization": authorization}), timeout=3) as response:
        assert json.load(response) == current["detail"]
        assert response.headers["ETag"] == current["etag"]

    other_path = configuration_path.replace("example-product", "other-product")
    code, denied, stderr = cli("config", "show", scope=["--product", "other-product", "--project", "example-project", "--json"])
    assert code == 5 and denied["error"]["code"] == "not_found"
    assert "opaque-other" not in stderr + json.dumps(denied)
    with urlopen(Request(other_path, headers={"Authorization": other_authorization}), timeout=3) as response:
        assert json.load(response) == CrossClientInstalledGateway.configurations["other-product"]
    assert CrossClientInstalledGateway.configurations["other-product"]["revision"] == 7


def test_cli_stale_etag_refuses_overwrite_and_exposes_opaque_header(tmp_path, monkeypatch, capsys):
    import threading
    from http.server import HTTPServer
    from urllib.request import Request, urlopen

    gateway = HTTPServer(("127.0.0.1", 0), InstalledSecurityGateway)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    try:
        address = f"http://127.0.0.1:{gateway.server_port}/api"
        monkeypatch.setattr(service.common, "gateway_url", lambda: address)
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        InstalledSecurityGateway.owners = {"example-product": "Bearer example-session"}
        InstalledSecurityGateway.configurations = {"example-product": {"revision": 1, "hash": "opaque-a7_Z", "settings": {"enabled": False}}}
        InstalledSecurityGateway.requests = []
        source = tmp_path / "settings.json"
        source.write_text(json.dumps({"enabled": True, "monitoring": {"poll_interval_seconds": 300}}))

        assert service.main(["config", "show", *SCOPE]) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["detail"]["revision"] == 1
        assert first["etag"] == '"opaque-a7_Z"'
        assert first["etag"] != str(first["detail"]["revision"])

        edit = ["config", "update", "--input", str(source), "--if-match", first["etag"], "--idempotency-key", "example-key-12345678", *SCOPE]
        assert service.main(edit) == 0
        saved = json.loads(capsys.readouterr().out)
        assert saved["etag"] == '"revision-2"'
        assert saved["detail"]["revision"] == 2
        assert saved["detail"]["settings"] == json.loads(source.read_text())

        source.write_text(json.dumps({"enabled": False}))
        assert service.main(edit) == 4
        stale = json.loads(capsys.readouterr().out)
        assert stale["error"]["code"] == "stale_revision"
        assert stale["detail"] == {}
        assert "etag" not in stale
        with urlopen(Request(address + BASE + "/configuration", headers={"Authorization": "Bearer example-session"}), timeout=3) as response:
            assert response.headers["ETag"] == saved["etag"]
            assert json.load(response) == saved["detail"]
        assert [method for method, _, _ in InstalledSecurityGateway.requests].count("PUT") == 2
        assert InstalledSecurityGateway.configurations["example-product"] == saved["detail"]
    finally:
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=2)


def test_cross_client_configuration_revision_and_capability_contract(tmp_path, monkeypatch, capsys):
    import threading
    from http.server import HTTPServer
    from urllib.request import Request, urlopen

    class CrossClientGateway(InstalledSecurityGateway):
        supported = True
        capability_reads = 0

        @override
        def do_GET(self):
            if self.path == "/api/me/cli-capabilities":
                self.__class__.capability_reads += 1
                if not self.supported:
                    self.respond(
                        200,
                        {
                            "schema_version": "2026-09-21",
                            "gateway": {"state": "yes", "release": "example"},
                            "tenant": {"org_id": "example-tenant"},
                            "operations": [],
                        },
                    )
                    return
            super().do_GET()

    gateway = HTTPServer(("127.0.0.1", 0), CrossClientGateway)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    try:
        address = f"http://127.0.0.1:{gateway.server_port}/api"
        authorization = "Bearer example-session"
        monkeypatch.setattr(service.common, "gateway_url", lambda: address)
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        CrossClientGateway.owners = {"example-product": authorization}
        CrossClientGateway.configurations = {"example-product": {"revision": 1, "hash": "opaque-a7_Z", "settings": {"enabled": False}}}
        CrossClientGateway.requests = []
        configuration_path = address + BASE + "/configuration"

        assert service.main(["config", "show", *SCOPE]) == 0
        old_etag = json.loads(capsys.readouterr().out)["etag"]
        assert old_etag == '"opaque-a7_Z"'

        ui_settings = {"enabled": True, "triggers": [{"target": "example-target"}], "monitoring": {"poll_interval_seconds": 300}}
        ui_edit = Request(
            configuration_path,
            data=json.dumps(ui_settings).encode(),
            method="PUT",
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "If-Match": old_etag,
                "Idempotency-Key": "example-key-12345678",
            },
        )
        with urlopen(ui_edit, timeout=3) as response:
            saved = json.load(response)
            new_etag = response.headers["ETag"]
        assert saved["revision"] == 2
        assert saved["settings"] == ui_settings
        assert new_etag != old_etag

        cli_source = tmp_path / "cli-edit.json"
        cli_source.write_text(json.dumps({"enabled": False}))

        def cli_update(etag):
            return service.main(
                ["config", "update", "--input", str(cli_source), "--idempotency-key", "example-key-12345678", "--if-match", etag, *SCOPE]
            )

        assert cli_update(old_etag) == 4
        stale = json.loads(capsys.readouterr().out)
        assert stale["error"]["code"] == "stale_revision"
        assert stale["detail"] == {}

        assert service.main(["config", "show", *SCOPE]) == 0
        refreshed = json.loads(capsys.readouterr().out)
        assert refreshed["detail"] == saved
        assert refreshed["etag"] == new_etag

        CrossClientGateway.supported = False
        assert cli_update(new_etag) == 5
        unsupported = json.loads(capsys.readouterr().out)
        assert unsupported["error"]["code"] == "unsupported_operation"
        assert CrossClientGateway.capability_reads == 2
        assert [method for method, _, _ in CrossClientGateway.requests].count("PUT") == 2
        with urlopen(Request(configuration_path, headers={"Authorization": authorization}), timeout=3) as response:
            assert json.load(response) == saved
            assert response.headers["ETag"] == new_etag
    finally:
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=2)


def test_missing_configuration_etag_is_a_contract_failure(monkeypatch, capsys):
    with MockGatewayServer() as gateway:
        gateway.set_response("/api" + BASE + "/configuration", 200, {"revision": 1, "settings": {}})
        monkeypatch.setattr(service.common, "gateway_url", lambda: gateway.url + "/api")
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        assert service.main(["config", "show", *SCOPE]) == 5
        response = json.loads(capsys.readouterr().out)
        assert response["error"]["code"] == "invalid_response"
        assert response["detail"] == {}


@pytest.mark.parametrize(
    ("discovery", "error_code"),
    [
        ("no-route", "unsupported_operation"),
        ("missing-operation", "unsupported_operation"),
        ("old-schema", "unsupported_operation"),
        ("disabled", "feature_disabled"),
    ],
)
@pytest.mark.parametrize("command", ["config", "run"])
def test_unsupported_server_refuses_mutation_before_dispatch(tmp_path, monkeypatch, capsys, discovery, error_code, command):
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"enabled": True}))
    with MockGatewayServer() as gateway:
        if discovery != "no-route":
            operations = (
                []
                if discovery == "missing-operation"
                else [
                    {
                        "id": "service.mutate",
                        "supported": "yes",
                        "enabled": "no" if discovery == "disabled" else "yes",
                        "permitted": "yes",
                        "ready": "yes",
                    }
                ]
            )
            gateway.set_response(
                "/api/me/cli-capabilities",
                200,
                {
                    "schema_version": "old-version" if discovery == "old-schema" else "2026-09-21",
                    "gateway": {"state": "yes", "release": "example"},
                    "tenant": {"org_id": "example-tenant"},
                    "operations": operations,
                },
            )
        monkeypatch.setattr(service.common, "gateway_url", lambda: gateway.url + "/api")
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        arguments = ["config", "update", "--if-match", '"opaque-token"'] if command == "config" else ["run"]
        assert service.main([*arguments, "--input", str(source), "--idempotency-key", "example-request-0001", *SCOPE]) == 5
        response = json.loads(capsys.readouterr().out)
        assert response["error"]["code"] == error_code
        assert response["detail"] == {}
        assert [request["path"] for request in gateway.requests] == ["/api/me/cli-capabilities"]


def test_post_read_only_configuration_check_does_not_require_mutation_capability(monkeypatch, capsys):
    with MockGatewayServer() as gateway:
        gateway.set_response("/api" + BASE + "/configuration/check", 200, {"ready": True})
        monkeypatch.setattr(service.common, "gateway_url", lambda: gateway.url + "/api")
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        assert service.main(["check", *SCOPE]) == 0
        assert json.loads(capsys.readouterr().out)["detail"] == {"ready": True}
        assert [request["path"] for request in gateway.requests] == ["/api" + BASE + "/configuration/check"]


class ExecutionClient:
    def __init__(self):
        self.calls = []
        self.receipt = {"run_id": "example-run", "state": "admitted", "revision": 1, "monitoring": {"last_checked_at": None, "advisories": []}}
        self.status_reads = 0

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        if method == "POST" and path == BASE + "/runs":
            return self.receipt
        if method == "GET" and path == BASE + "/runs/example-run":
            self.status_reads += 1
            if self.status_reads > 1:
                self.receipt = {**self.receipt, "state": "succeeded", "revision": 2}
            return self.receipt
        if path.startswith(BASE + "/results"):
            return {"items": [{"result_id": "example-result", "run_id": "example-run"}]}
        if path.startswith(BASE + "/runs"):
            return {"items": [self.receipt]}
        raise AssertionError(f"Unexpected operation: {method} {path}")


def test_run_submission_readback_results_and_bounded_wait(tmp_path, monkeypatch):
    client = ExecutionClient()
    run_input = {
        "configuration_revision": 1,
        "binding_id": "example-binding",
        "capability": "threat_model",
        "sources": [
            {
                "tenant_id": "example-tenant",
                "project_id": "example-project",
                "source_connection_id": "example-connection",
                "record_type": "document",
                "record_id": "example-document",
                "record_revision": "example-revision",
            }
        ],
    }
    source = tmp_path / "run.json"
    source.write_text(json.dumps(run_input, indent=2))
    admitted = run(client, "run", "--input", str(source), "--idempotency-key", "example-run-key-0001")
    assert admitted == client.receipt
    assert client.calls[0] == ("POST", BASE + "/runs", run_input, {"extra_headers": {"Idempotency-Key": "example-run-key-0001"}})
    assert run(client, "runs")["items"][0]["run_id"] == "example-run"
    assert run(client, "results")["items"][0]["run_id"] == "example-run"
    assert client.calls[2][1] == BASE + "/results?limit=25"
    assert run(client, "status", "--run-id", "example-run")["monitoring"] == admitted["monitoring"]
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    response = service.execute(
        service.parser().parse_args(["wait", "--run-id", "example-run", "--wait-seconds", "2", "--poll-seconds", "1", *SCOPE]), client
    )
    assert response["status"] == "ok"
    assert response["detail"]["run_id"] == admitted["run_id"]
    assert response["detail"]["state"] == "succeeded"
    assert sum(method == "POST" for method, _, _, _ in client.calls) == 1


@pytest.mark.parametrize(
    ("arguments", "method", "suffix"),
    [
        (("binding", "events", "--binding-id", "example-binding", "--service", "devops"), "GET", "/bindings/example-binding/events"),
        (
            ("triage", "history", "--result-id", "example-result", "--finding-id", "example-finding"),
            "GET",
            "/results/example-result/findings/example-finding/triage",
        ),
        (
            (
                "triage",
                "set",
                "--result-id",
                "example-result",
                "--finding-id",
                "example-finding",
                "--input",
                "INPUT",
                "--if-match",
                '"triage-revision"',
            ),
            "PUT",
            "/results/example-result/findings/example-finding/triage",
        ),
        (("compare", "--input", "INPUT"), "POST", "/comparisons"),
        (("publication", "show", "--publication-id", "example-publication"), "GET", "/publications/example-publication"),
        (
            ("publication", "retry", "--publication-id", "example-publication", "--input", "INPUT", "--idempotency-key", "example-retry-key-1234"),
            "POST",
            "/publications/example-publication/retry",
        ),
        (("artifact", "show", "--artifact-id", "example-artifact"), "GET", "/artifacts/example-artifact"),
    ],
)
def test_execution_matrix_forwards_scoped_operations(tmp_path, arguments, method, suffix):
    source = tmp_path / "body.json"
    source.write_text(json.dumps({"reason": "example retry"}))
    arguments = tuple(str(source) if argument == "INPUT" else argument for argument in arguments)
    client = FakeClient()
    args = service.parser().parse_args([*arguments, *SCOPE])
    service.execute(args, client)
    expected_service = "devops" if "devops" in arguments else "security"
    assert client.calls[-1][0:2] == (method, BASE.replace("security", expected_service) + suffix)
    if method != "GET":
        assert client.calls[-1][2] == {"reason": "example retry"}
    if "--if-match" in arguments:
        assert client.calls[-1][3]["extra_headers"]["If-Match"] == '"triage-revision"'


class LostReplyClient:
    def __init__(self):
        self.runs = {}
        self.attempts = 0
        self.provider_runs = 0
        self.calls = []

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        if method == "POST" and path == BASE + "/runs":
            self.attempts += 1
            key = kwargs["extra_headers"]["Idempotency-Key"]
            if key not in self.runs:
                self.provider_runs += 1
                self.runs[key] = {"run_id": "example-run", "state": "admitted", "revision": 1}
            if self.attempts == 1:
                raise service.common.CliError("Response lost; inspect receipt.", "unknown_mutation_outcome", 4)
            return self.runs[key]
        if method == "GET" and path == BASE + "/runs/example-run":
            return self.runs["example-run-key-0001"]
        raise AssertionError(f"Unexpected request {method} {path}")


def test_dropped_response_replay_uses_one_server_run_and_same_key(tmp_path):
    source = tmp_path / "run.json"
    source.write_text(json.dumps({"configuration_revision": 1, "binding_id": "example-binding"}))
    client = LostReplyClient()
    args = service.parser().parse_args(["run", "--input", str(source), "--idempotency-key", "example-run-key-0001", *SCOPE])
    with pytest.raises(service.common.CliError) as dropped:
        service.execute(args, client)
    assert dropped.value.code == "unknown_mutation_outcome"
    assert run(client, "status", "--run-id", "example-run")["state"] == "admitted"
    assert service.execute(args, client)["detail"]["run_id"] == "example-run"
    assert client.attempts == 2
    assert client.provider_runs == len(client.runs) == 1
    assert client.calls == [("POST", BASE + "/runs"), ("GET", BASE + "/runs/example-run"), ("POST", BASE + "/runs")]


class ArtifactGateway(MockGatewayHandler):
    content = b'{"assessment": "example-result"}'
    download_requests = []
    denied = False

    @override
    def do_GET(self):
        if not self.path.endswith("/content"):
            return super().do_GET()
        self.__class__.download_requests.append((self.path, self.headers.get("Authorization")))
        if self.denied:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("X-Adp-Content-Sha256", hashlib.sha256(self.content).hexdigest())
        self.send_header("Content-Length", str(len(self.content)))
        self.end_headers()
        self.wfile.write(self.content)


def test_artifact_download_checks_scoped_receipt_and_removes_failed_output(tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), ArtifactGateway)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{gateway.server_port}/api"
        metadata_path = "/api" + BASE + "/artifacts/example-artifact"
        checksum = hashlib.sha256(ArtifactGateway.content).hexdigest()
        ArtifactGateway.mock_responses = {
            metadata_path: {"status": 200, "body": {"artifact": {"sha256": checksum}, "download_path": BASE + "/artifacts/example-artifact/content"}}
        }
        ArtifactGateway.download_requests = []
        ArtifactGateway.denied = False
        monkeypatch.setattr(service.common, "gateway_url", lambda: url)
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        output = tmp_path / "artifact.json"
        args = service.parser().parse_args(["artifact", "download", "--artifact-id", "example-artifact", "--output", str(output), *SCOPE])
        observed = service.execute(args, service.common.Api())["detail"]
        assert output.read_bytes() == ArtifactGateway.content
        assert output.stat().st_mode & 0o777 == 0o600
        assert observed == {"path": str(output), "sha256": checksum, "bytes": len(ArtifactGateway.content)}
        assert ArtifactGateway.download_requests == [(metadata_path + "/content", "Bearer example-session")]
        ArtifactGateway.content = b"corrupted response"
        damaged = tmp_path / "damaged.json"
        args.output = str(damaged)
        with pytest.raises(service.common.CliError, match="checksum"):
            service.execute(args, service.common.Api())
        assert not damaged.exists()
    finally:
        ArtifactGateway.content = b'{"assessment": "example-result"}'
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize(
    ("arguments", "method", "suffix", "http_status", "code", "exit_code"),
    [
        (("run", "--input", "INPUT", "--idempotency-key", "example-run-key-0001"), "POST", "/runs", 403, "access_denied", 3),
        (("results",), "GET", "/results?limit=25", 403, "access_denied", 3),
        (("status", "--run-id", "missing-run"), "GET", "/runs/missing-run", 404, "not_found", 5),
        (("wait", "--run-id", "missing-run", "--wait-seconds", "1"), "GET", "/runs/missing-run", 404, "not_found", 5),
        (("result", "show", "--result-id", "missing-result"), "GET", "/results/missing-result", 404, "not_found", 5),
    ],
)
def test_execution_denied_or_missing_has_safe_json_and_exit(
    tmp_path,
    monkeypatch,
    capsys,
    arguments,
    method,
    suffix,
    http_status,
    code,
    exit_code,
):
    source = tmp_path / "run.json"
    source.write_text(
        json.dumps(
            {
                "configuration_revision": 1,
                "binding_id": "example-binding",
                "capability": "threat_model",
                "sources": [
                    {
                        "tenant_id": "example-tenant",
                        "project_id": "example-project",
                        "source_connection_id": "example-connection",
                        "record_type": "document",
                        "record_id": "example-document",
                        "record_revision": "example-revision",
                    }
                ],
            }
        )
    )
    command = [str(source) if value == "INPUT" else value for value in arguments]
    with MockGatewayServer() as gateway:
        if method == "POST":
            gateway.set_response(
                "/api/me/cli-capabilities",
                200,
                {
                    "schema_version": "2026-09-21",
                    "gateway": {"state": "yes", "release": "example"},
                    "tenant": {"org_id": "example-tenant"},
                    "operations": [{"id": "service.mutate", "supported": "yes", "enabled": "yes", "permitted": "yes", "ready": "yes"}],
                },
            )
        gateway.set_response(
            "/api" + BASE + suffix, http_status, {"detail": {"code": code, "message": "private-provider-secret and account context"}}
        )
        monkeypatch.setattr(service.common, "gateway_url", lambda: gateway.url + "/api")
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        assert service.main([*command, *SCOPE]) == exit_code
        output = capsys.readouterr()
        parsed = json.loads(output.out)
        assert parsed["status"] == "failed"
        assert parsed["error"]["code"] == code
        assert parsed["detail"] == {}
        assert "private-provider-secret" not in output.out + output.err
        assert "example-document" not in output.out + output.err
        assert [(request["method"], request["path"]) for request in gateway.requests] == [
            *([("GET", "/api/me/cli-capabilities")] if method == "POST" else []),
            (method, "/api" + BASE + suffix),
        ]


def test_wait_timeout_keeps_durable_receipt_and_same_key_replay(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    source = tmp_path / "run.json"
    source.write_text(json.dumps({"configuration_revision": 1, "binding_id": "example-binding"}))
    client = LostReplyClient()
    submit = service.parser().parse_args(["run", "--input", str(source), "--idempotency-key", "example-run-key-0001", *SCOPE])
    with pytest.raises(service.common.CliError) as dropped:
        service.execute(submit, client)
    assert dropped.value.code == "unknown_mutation_outcome"
    monkeypatch.setattr(service.common, "Api", lambda: client)
    timestamps = iter((100.0, 100.0, 100.1, 100.5, 101.0))
    sleeps = []
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: next(timestamps), sleep=sleeps.append))
    assert service.main(["wait", "--run-id", "example-run", "--wait-seconds", "1", *SCOPE]) == 7
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "pending"
    assert output["detail"] == client.runs["example-run-key-0001"]
    assert "do not submit again" in output["next_action"]
    assert sleeps == [pytest.approx(0.9)]
    assert client.calls == [("POST", BASE + "/runs"), ("GET", BASE + "/runs/example-run"), ("GET", BASE + "/runs/example-run")]
    assert service.execute(submit, client)["detail"] == output["detail"]
    assert client.provider_runs == len(client.runs) == 1
    assert client.attempts == 2


@pytest.mark.parametrize(("wait_seconds", "poll_seconds"), [(0, 1), (3601, 1), (1, 0), (1, 61)])
def test_wait_rejects_unbounded_or_invalid_intervals_before_read(monkeypatch, capsys, wait_seconds, poll_seconds):
    client = LostReplyClient()
    monkeypatch.setattr(service.common, "Api", lambda: client)
    assert service.main(["wait", "--run-id", "example-run", "--wait-seconds", str(wait_seconds), "--poll-seconds", str(poll_seconds), *SCOPE]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "usage_error"
    assert client.calls == []


def test_wait_failed_terminal_returns_receipt_and_exit_six(monkeypatch, capsys):
    client = LostReplyClient()
    client.runs["example-run-key-0001"] = {"run_id": "example-run", "state": "failed", "revision": 4}
    monkeypatch.setattr(service.common, "Api", lambda: client)
    assert service.main(["wait", "--run-id", "example-run", "--wait-seconds", "1", *SCOPE]) == 6
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed"
    assert output["error"]["code"] == "run_failed"
    assert output["detail"] == client.runs["example-run-key-0001"]
    assert client.calls == [("GET", BASE + "/runs/example-run")]


def test_cli_execution_and_errors(tmp_path):
    import base64
    import os
    import subprocess
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from tests.cli.conftest import write_adp_session

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), InstalledSecurityGateway)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    try:
        address = f"http://127.0.0.1:{gateway.server_port}/api"
        binary = tmp_path / "bin"
        home = tmp_path / "home"
        home.mkdir()
        environment = {
            **os.environ,
            "HOME": str(home),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        installed = subprocess.run(
            ["sh", str(CLI / "install.sh"), "--gateway-url", address, "--prefix", str(binary), "--no-path-edit"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert installed.returncode == 0, installed.stderr
        assert (binary / "adp-service.py").read_bytes() == (CLI / "adp-service.py").read_bytes()
        write_adp_session(home)
        config = home / ".bedrock-gateway" / "config.json"
        config.write_text(json.dumps({**json.loads(config.read_text()), "gateway_url": address}))
        tokens_file = home / ".bedrock-gateway" / "tokens.json"
        tokens = json.loads(tokens_file.read_text())
        segments = tokens["access_token"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)))
        claims["org_id"] = "example-tenant"
        segments[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        tokens["access_token"] = ".".join(segments)
        tokens_file.write_text(json.dumps(tokens))
        authorization = "Bearer " + tokens["access_token"]
        other_home = tmp_path / "other-home"
        other_home.mkdir()
        write_adp_session(other_home, username="example-bob")
        other_token = json.loads((other_home / ".bedrock-gateway" / "tokens.json").read_text())["access_token"]
        InstalledSecurityGateway.owners = {"example-product": authorization, "other-product": "Bearer " + other_token}
        InstalledSecurityGateway.runs = {}
        InstalledSecurityGateway.provider_runs = 0
        InstalledSecurityGateway.dropped = False
        InstalledSecurityGateway.requests = []
        source = tmp_path / "run.json"
        run_input = {
            "configuration_revision": 1,
            "binding_id": "example-binding",
            "capability": "threat_model",
            "sources": [
                {
                    "tenant_id": "example-tenant",
                    "project_id": "example-project",
                    "source_connection_id": "example-source",
                    "record_type": "document",
                    "record_id": "example-document",
                    "record_revision": "example-revision",
                }
            ],
        }
        source.write_text(json.dumps(run_input))

        def cli(*arguments):
            result = subprocess.run([str(binary / "adp"), "service", *arguments, *SCOPE], env=environment, capture_output=True, text=True, timeout=30)
            return result.returncode, json.loads(result.stdout), result.stderr

        submit = ("run", "--input", str(source), "--idempotency-key", "example-run-key-0001")
        exit_code, first, stderr = cli(*submit)
        assert exit_code == 4 and first["error"]["code"] == "unknown_mutation_outcome"
        assert first["detail"] == {}
        assert "example-document" not in stderr + json.dumps(first)
        independent = Request(address + BASE + "/runs/example-run", headers={"Authorization": authorization})
        with urlopen(independent, timeout=3) as response:
            stored = json.load(response)
            assert response.headers["ETag"] == '"1"'
        assert stored["run_id"] == "example-run"
        assert stored["scope"]["tenant_id"] == "example-tenant"
        assert stored["monitoring"]["advisories"] == []
        exit_code, observed, _ = cli("status", "--run-id", "example-run")
        assert exit_code == 0 and observed["detail"] == stored
        exit_code, replay, _ = cli(*submit)
        assert exit_code == 0 and replay["detail"] == stored
        exit_code, pending, _ = cli("wait", "--run-id", "example-run", "--wait-seconds", "1")
        assert exit_code == 7 and pending["detail"] == stored
        exit_code, results, _ = cli("results")
        assert exit_code == 0 and results["detail"] == {"items": []}
        exit_code, missing, _ = cli("status", "--run-id", "missing-run")
        assert exit_code == 5 and missing["error"]["code"] == "not_found"
        assert missing["detail"] == {}
        assert InstalledSecurityGateway.provider_runs == len(InstalledSecurityGateway.runs) == 1
        assert [(method, path) for method, path, _ in InstalledSecurityGateway.requests if method == "POST"] == [
            ("POST", "/api" + BASE + "/runs")
        ] * 2
        run_input["sources"][0]["tenant_id"] = "other-tenant"
        source.write_text(json.dumps(run_input))
        exit_code, denied, stderr = cli("run", "--input", str(source), "--idempotency-key", "example-denied-key-0001")
        assert exit_code == 3 and denied["error"]["code"] == "access_denied"
        assert denied["detail"] == {}
        assert "private-provider-secret" not in stderr + json.dumps(denied)
        assert InstalledSecurityGateway.provider_runs == 1
        with pytest.raises(HTTPError) as inaccessible:
            urlopen(Request(address + BASE + "/runs/example-run", headers={"Authorization": "Bearer " + other_token}), timeout=3)
        assert inaccessible.value.code == 404
        assert ("other-product", "example-run-key-0001") not in InstalledSecurityGateway.runs
    finally:
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize(
    ("arguments", "suffix", "status", "code", "exit_code"),
    [
        (("config", "show"), "/configuration", 404, "not_found", 5),
        (("config", "update"), "/configuration", 412, "stale_revision", 4),
        (("config", "update"), "/configuration", 428, "precondition_required", 5),
        (("config", "update"), "/configuration", 409, "idempotency_conflict", 4),
        (("run",), "/runs", 422, "validation_error", 2),
    ],
)
def test_finops_service_json_preserves_scoped_server_status(
    tmp_path,
    monkeypatch,
    capsys,
    arguments,
    suffix,
    status,
    code,
    exit_code,
):
    document = tmp_path / "finops-input.json"
    document.write_text(json.dumps({"service_id": "finops", "finops": {"agent_mode": "create"}}))
    arguments = (*arguments, "--input", str(document)) if arguments[-1] in {"update", "run"} else arguments
    if arguments[0] == "config" and arguments[1] == "update":
        arguments += ("--if-match", '"revision-example"')
    if arguments[0] != "config" or arguments[1] != "show":
        arguments += ("--idempotency-key", "finops-key-example-0001")

    def respond_put(self):
        self.do_POST()
        self.__class__.request_log[-1]["method"] = "PUT"

    monkeypatch.setattr(MockGatewayHandler, "do_PUT", respond_put, raising=False)
    with MockGatewayServer() as gateway:
        gateway.set_response(
            "/api" + BASE.replace("/security", "/finops") + suffix,
            status,
            {"detail": {"code": code, "message": "private-provider-secret"}},
        )
        monkeypatch.setattr(service.common, "ensure_can_mutate", lambda *args, **kwargs: None)
        monkeypatch.setattr(service.common, "gateway_url", lambda: gateway.url + "/api")
        monkeypatch.setattr(service.common, "access_token", lambda: "example-session")
        scoped = ["--product", "example-product", "--project", "example-project", "--service", "finops", "--json"]
        assert service.main([*arguments, *scoped]) == exit_code
        output = capsys.readouterr()
        envelope = json.loads(output.out)
        assert envelope["error"]["http_status"] == status
        assert envelope["error"]["code"] == code
        assert "private-provider-secret" not in output.out + output.err
        assert len(gateway.requests) == 1
        assert gateway.requests[0]["method"] == (
            "PUT" if arguments[0] == "config" and arguments[1] == "update" else "POST" if arguments[0] == "run" else "GET"
        )
        assert gateway.requests[0]["path"] == "/api" + BASE.replace("/security", "/finops") + suffix


@pytest.mark.parametrize(
    ("state", "exit_code", "envelope_status"),
    (
        ("succeeded", 0, "ok"),
        ("partial", 6, "failed"),
        ("blocked", 6, "failed"),
        ("outcome_unknown", 7, "pending"),
    ),
)
def test_installed_finops_wait_and_read_only_recovery(installed_cross_client_gateway, monkeypatch, state, exit_code, envelope_status):
    import subprocess

    _gateway, _address, binary, environment, _authorization, _other_authorization = installed_cross_client_gateway
    receipt = {
        "run_id": "example-run",
        "revision": 3,
        "state": state,
        "provider_state": "outcome_unknown",
        "scope": {"tenant_id": "example-tenant", "product_id": "example-product", "project_id": "example-project"},
    }
    monkeypatch.setattr(CrossClientInstalledGateway, "runs", {("example-product", "receipt-key"): receipt})
    monkeypatch.setattr(CrossClientInstalledGateway, "provider_runs", 0)
    scope = ("--product", "example-product", "--project", "example-project", "--service", "finops", "--json")

    def command(*arguments):
        completed = subprocess.run([str(binary / "adp"), "service", *arguments, *scope], env=environment, capture_output=True, text=True, timeout=15)
        assert completed.stderr == ""
        return completed.returncode, json.loads(completed.stdout)

    code, observed = command("wait", "--run-id", "example-run", "--wait-seconds", "1", "--poll-seconds", "1")
    assert code == exit_code and observed["status"] == envelope_status
    assert observed["detail"] == receipt
    if state == "outcome_unknown":
        assert "do not submit again" in observed["next_action"]
    code, status = command("status", "--run-id", "example-run")
    assert code == 0 and status["detail"] == receipt
    code, results = command("results")
    assert code == 0 and results["detail"] == {"items": []}
    assert CrossClientInstalledGateway.provider_runs == 0
    assert all(method == "GET" for method, _, _ in CrossClientInstalledGateway.requests)


@pytest.mark.parametrize(
    "bad_receipt",
    (
        {"run_id": "example-run", "state": "succeeded", "scope": {"product_id": "other-product", "project_id": "example-project"}},
        {"run_id": "example-run", "state": "unknown", "scope": {"product_id": "example-product", "project_id": "example-project"}},
    ),
)
def test_finops_wait_rejects_invalid_receipt_without_revealing_it(monkeypatch, capsys, bad_receipt):
    class Client:
        calls = []

        def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return bad_receipt

    client = Client()
    monkeypatch.setattr(service.common, "Api", lambda: client)
    assert (
        service.main(
            [
                "wait",
                "--run-id",
                "example-run",
                "--wait-seconds",
                "1",
                "--product",
                "example-product",
                "--project",
                "example-project",
                "--service",
                "finops",
                "--json",
            ]
        )
        == 5
    )
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == "invalid_response" and output["detail"] == {}
    assert "other-product" not in json.dumps(output)
    assert len(client.calls) == 1 and client.calls[0][0] == "GET"
    assert 0 < client.calls[0][2]["timeout"] <= 1


@pytest.mark.parametrize(
    ("native_status", "run_state", "wait_exit"),
    (
        ("answered", "partial", 6),
        ("refused", "partial", 6),
        ("outcome_unknown", "outcome_unknown", 7),
    ),
)
def test_installed_finops_result_keeps_native_and_billing_outcomes_separate(
    installed_cross_client_gateway,
    monkeypatch,
    native_status,
    run_state,
    wait_exit,
):
    import subprocess

    _gateway, _address, binary, environment, _authorization, _other_authorization = installed_cross_client_gateway
    scope = {
        "tenant_id": "example-tenant",
        "product_id": "example-product",
        "project_id": "example-project",
        "relationship_revision": "1",
        "owner_id": "owner-example",
        "visibility": "only_me",
    }
    reference = {"artifact_id": "evidence-example", "version": "1", "sha256": "a" * 64}
    query = {
        "start": "2026-01-01",
        "end": "2026-02-01",
        "metric": "UnblendedCost",
        "granularity": "MONTHLY",
        "group_by": ["SERVICE"],
        "currency": "USD",
    }
    native_amount = "-10.0000" if native_status == "answered" else None
    native_measure = {
        "amount": native_amount,
        "currency": "USD" if native_amount else None,
        "estimated": None,
        "source_refreshed_at": None,
        "freshness_reason": "partial_native",
        "coverage": "partial",
    }
    billing_measure = {
        "amount": "-12.3400",
        "currency": "USD",
        "estimated": False,
        "source_refreshed_at": None,
        "freshness_reason": "partial_billing",
        "coverage": "partial",
    }
    result = {
        "result_id": "result-example",
        "revision": 2,
        "scope": scope,
        "kind": "finops_cost_inquiry",
        "run_id": "example-run",
        "query": query,
        "scope_digest": "b" * 64,
        "binding_generation": 1,
        "attribution": "partial",
        "attribution_evidence": [reference],
        "native": {
            "status": native_status,
            "conversation_id": None,
            "turn_id": None,
            "answer_artifact": reference if native_amount else None,
            "observed_at": None,
            "measure": native_measure,
        },
        "oracle": {
            "collection_id": "collection-example",
            "query_hash": "c" * 64,
            "observed_at": "2026-02-02T00:00:00Z",
            "measure": billing_measure,
            "pages": [reference],
        },
        "comparison": {"state": "incomparable", "delta": None, "display_quantum": None, "reason": "partial_billing"},
        "billing_scope": {"account_ids": ["123456789012"], "view_ref": "view-example", "snapshot_artifact": reference},
    }
    run = {
        "run_id": "example-run",
        "revision": 2,
        "scope": scope,
        "state": run_state,
        "provider_state": "unknown" if native_status == "outcome_unknown" else "completed",
    }
    monkeypatch.setattr(CrossClientInstalledGateway, "runs", {("example-product", "receipt-key"): run})
    monkeypatch.setattr(CrossClientInstalledGateway, "provider_runs", 0)
    original = CrossClientInstalledGateway.do_GET

    def serve_result(self):
        if self.path.endswith("/services/finops/results/result-example"):
            self.__class__.requests.append(("GET", self.path, self.headers.get("Authorization")))
            if self.scoped_product() is not None:
                self.respond(200, result, '"2"')
        else:
            original(self)

    monkeypatch.setattr(CrossClientInstalledGateway, "do_GET", serve_result)
    args = ("--product", "example-product", "--project", "example-project", "--service", "finops", "--json")

    def command(*arguments):
        completed = subprocess.run([str(binary / "adp"), "service", *arguments, *args], env=environment, capture_output=True, text=True, timeout=15)
        assert completed.stderr == ""
        return completed.returncode, json.loads(completed.stdout)

    code, observed = command("result", "show", "--result-id", "result-example")
    assert code == 0 and observed["status"] == "ok" and observed["etag"] == '"2"'
    assert observed["detail"] == result
    assert observed["detail"]["native"]["measure"]["amount"] == native_amount
    assert observed["detail"]["oracle"]["measure"]["amount"] == "-12.3400"
    assert observed["detail"]["comparison"]["delta"] is None
    code, waited = command("wait", "--run-id", "example-run", "--wait-seconds", "1")
    assert code == wait_exit and waited["detail"]["state"] == run_state
    code, recovered = command("status", "--run-id", "example-run")
    assert code == 0 and recovered["detail"] == run
    assert CrossClientInstalledGateway.provider_runs == 0
    assert all(method == "GET" for method, _, _ in CrossClientInstalledGateway.requests)


def test_installed_product_cli_offers_transform_without_provider_setup(installed_cross_client_gateway):
    import subprocess

    _gateway, _address, binary, environment, _authorization, _other_authorization = installed_cross_client_gateway
    for arguments in (("run", "--help"), ("wait", "--help"), ("result", "show", "--help")):
        completed = subprocess.run(
            [str(binary / "adp"), "service", *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode == 0, completed.stderr
        assert "transform" in completed.stdout


def test_explicit_effect_challenge_reaches_installed_canonical_cli(tmp_path):
    client = FakeClient()
    challenge = "00000000-0000-4000-8000-000000000001"
    run(client, "check", "--observation-effect", challenge)
    assert client.calls[-1][3]["extra_headers"] == {"X-ADP-Observation-Effect": challenge}
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"enabled": False}))
    run(
        client,
        "config",
        "update",
        "--input",
        str(settings),
        "--idempotency-key",
        "selected-key-0001",
        "--if-match",
        '"etag"',
        "--observation-effect",
        challenge,
    )
    assert client.calls[-1][3]["extra_headers"] == {
        "X-ADP-Observation-Effect": challenge,
        "If-Match": '"etag"',
        "Idempotency-Key": "selected-key-0001",
    }


def test_manual_evidence_registration_and_receipt_use_local_authenticated_routes():
    client = FakeClient()
    run(client, "evidence-register", "--request-id", "request-1")
    assert client.calls[-1][:3] == ("POST", BASE + "/release-evidence/requests/request-1/credentials", {"slot": "cli"})
    run(client, "evidence-receipt", "--receipt-id", "original_edit_receipt_1")
    assert client.calls[-1][:3] == ("GET", BASE + "/release-evidence/edits/original_edit_receipt_1", None)
    with pytest.raises(service.common.CliError, match="requires --service security"):
        run(client, "evidence-register", "--request-id", "request-1", "--service", "devops")
