"""The installed service CLI keeps DevOps requests within the selected project."""

import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from typing import override
from urllib.request import Request, urlopen

import pytest

from tests.cli.conftest import write_adp_session
from tests.cli.test_l5_security import CLI, InstalledSecurityGateway


class DevopsGateway(InstalledSecurityGateway):
    configurations = {}
    requests = []
    runs = {}
    assessments = 0
    publication_retries = 0
    drop_run_response = False
    artifact = b'{"sample":"devops-result"}'

    def scope(self):
        prefix = "/api/v1/products/"
        if not self.path.startswith(prefix):
            return None
        parts = self.path[len(prefix) :].split("/")
        if len(parts) < 6 or parts[1] != "projects" or parts[3:5] != ["services", "devops"]:
            return None
        product, project = parts[0], parts[2]
        if (product, project) not in self.configurations or self.headers.get("Authorization") != self.owners.get(product):
            self.respond(404, {"detail": {"code": "not_found"}})
            return None
        self.__class__.requests.append((self.command, self.path, self.headers.get("If-Match"), self.headers.get("Idempotency-Key")))
        return product, project, "/".join(parts[5:])

    @override
    def do_GET(self):
        if not self.path.startswith("/api/v1/products/"):
            return super().do_GET()
        scoped = self.scope()
        if scoped is None:
            return
        product, project, suffix = scoped
        configuration = self.configurations[product, project]
        if suffix == "configuration":
            self.respond(200, configuration, '"' + configuration["hash"] + '"')
        elif suffix == "configuration/options":
            self.respond(200, {"sources": [product + "-source"]})
        elif suffix == "bindings":
            self.respond(200, {"items": [{"binding_id": project + "-binding"}]})
        elif suffix == "bindings/" + project + "-binding":
            self.respond(200, {"binding_id": project + "-binding"}, '"binding-1"')
        elif suffix == "bindings/" + project + "-binding/prevention":
            self.respond(200, {"binding_id": project + "-binding", "action": "paused", "ready": True})
        elif suffix.startswith("runs?"):
            self.respond(200, {"items": [self.runs[product, project]]})
        elif suffix == "runs/" + project + "-run":
            run = self.runs[product, project]
            self.respond(200, run, '"' + str(run["revision"]) + '"')
        elif suffix.startswith("results?"):
            self.respond(200, {"items": [{"result_id": project + "-result", "run_id": project + "-run"}]})
        elif suffix == "results/" + project + "-result":
            self.respond(
                200,
                {
                    "result_id": project + "-result",
                    "run_id": project + "-run",
                    "origin": "adp_submitted",
                    "native_job_ref": {"job_id": project + "-native"},
                },
                '"1"',
            )
        elif suffix == "publications/" + project + "-publication":
            self.respond(200, {"publication_id": project + "-publication", "run_id": project + "-run"})
        elif suffix == "artifacts/" + project + "-artifact":
            prefix = f"/v1/products/{product}/projects/{project}/services/devops"
            self.respond(
                200,
                {
                    "artifact": {"sha256": hashlib.sha256(self.artifact).hexdigest()},
                    "download_path": prefix + "/artifacts/" + project + "-artifact/content",
                },
            )
        elif suffix == "artifacts/" + project + "-artifact/content":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.artifact)))
            self.send_header("X-Adp-Content-Sha256", hashlib.sha256(self.artifact).hexdigest())
            self.end_headers()
            self.wfile.write(self.artifact)
        else:
            self.respond(404, {"detail": {"code": "not_found"}})

    @override
    def do_PUT(self):
        scoped = self.scope()
        if scoped is None:
            return
        product, project, suffix = scoped
        current = self.configurations[product, project]
        if suffix != "configuration" or self.headers.get("If-Match") != '"' + current["hash"] + '"':
            self.respond(412, {"detail": {"code": "stale_revision"}})
            return
        settings = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        updated = {"revision": current["revision"] + 1, "hash": project + "-next", "settings": settings}
        self.configurations[product, project] = updated
        self.respond(200, updated, '"' + updated["hash"] + '"')

    @override
    def do_POST(self):
        scoped = self.scope()
        if scoped is None:
            return
        product, project, suffix = scoped
        if suffix == "configuration/check":
            self.respond(200, {"ready": True, "project_id": project})
        elif suffix == "configuration/refresh":
            current = self.configurations[product, project]
            self.respond(200, current, '"' + current["hash"] + '"')
        elif suffix == "binding-operations":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.respond(202, {"operation_id": project + "-operation", "action": body["action"]})
        elif suffix == "runs":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            references = body.get("devops_input", {}).get("resource_refs", [])
            if references and references[0].get("project_id") != project:
                self.respond(404, {"detail": {"code": "not_found", "message": "private-source-secret"}})
                return
            if body.get("service_id") != "devops":
                self.respond(422, {"detail": {"code": "validation_error"}})
                return
            run = {
                "run_id": project + "-run",
                "revision": 1,
                "state": "succeeded",
                "origin": "adp_submitted",
                "scope": {"product_id": product, "project_id": project},
                "native_job_ref": {"job_id": project + "-native"},
            }
            self.runs[product, project] = run
            self.__class__.assessments += 1
            if self.drop_run_response:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.respond(202, run, '"1"')
        elif suffix == "bindings/" + project + "-binding/prevention":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.respond(
                202, {"binding_id": project + "-binding", "action": body["action"], "configuration_revision": body["configuration_revision"]}
            )
        elif suffix == "publications/" + project + "-publication/retry":
            self.__class__.publication_retries += 1
            self.respond(202, {"publication_id": project + "-publication", "run_id": project + "-run"})
        else:
            self.respond(404, {"detail": {"code": "not_found"}})


@pytest.fixture
def installed_devops(tmp_path):
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), DevopsGateway)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
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
        token = tokens["access_token"]
        DevopsGateway.owners = {"sample-one": "Bearer " + token, "sample-two": "Bearer " + token}
        DevopsGateway.configurations = {
            (product, project): {"revision": 1, "hash": project + "-first", "settings": {"source": product + "-source"}}
            for product, project in (("sample-one", "project-one"), ("sample-two", "project-two"))
        }
        DevopsGateway.requests = []
        DevopsGateway.runs = {}
        DevopsGateway.assessments = 0
        DevopsGateway.publication_retries = 0
        DevopsGateway.drop_run_response = False
        yield address, binary, environment
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=2)


def test_cli_configuration(installed_devops, tmp_path):
    address, binary, environment = installed_devops
    manifest = json.loads((binary / "command-manifest.json").read_text())
    commands = {entry["command"]: entry for entry in manifest["commands"]}
    binding = commands["adp service binding create"]
    assert binding["request"]["paths"] == ["/v1/products/{product_id}/projects/{project_id}/services/{service_id}/binding-operations"]
    assert "tests/cli/test_devops_service.py" in binding["tests"]
    for product, project in (("sample-one", "project-one"), ("sample-two", "project-two")):
        scope = ["--service", "devops", "--product", product, "--project", project, "--json"]

        def cli(*arguments):
            result = subprocess.run([str(binary / "adp"), "service", *arguments, *scope], env=environment, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr + result.stdout
            return json.loads(result.stdout)

        assert cli("options")["detail"]["sources"] == [product + "-source"]
        initial = cli("config", "show")
        assert initial["etag"] == '"' + project + '-first"'
        assert cli("check")["detail"]["project_id"] == project
        source = tmp_path / (project + ".json")
        source.write_text(json.dumps({"source": product + "-updated"}))
        saved = cli("config", "update", "--input", str(source), "--if-match", initial["etag"], "--idempotency-key", "example-update-key-0001")
        assert saved["detail"]["revision"] == 2
        assert (
            cli("config", "refresh", "--input", str(source), "--if-match", saved["etag"], "--idempotency-key", "example-refresh-key-0001")["detail"]
            == saved["detail"]
        )
        assert cli("binding", "list")["detail"]["items"] == [{"binding_id": project + "-binding"}]
        assert cli("binding", "show", "--binding-id", project + "-binding")["etag"] == '"binding-1"'
        source.write_text(json.dumps({"action": "create", "source": product + "-source"}))
        receipt = cli("binding", "create", "--input", str(source), "--idempotency-key", "example-binding-key-0001")
        assert receipt["detail"] == {"operation_id": project + "-operation", "action": "create"}
        with urlopen(
            Request(
                address + f"/v1/products/{product}/projects/{project}/services/devops/configuration",
                headers={"Authorization": DevopsGateway.owners[product]},
            ),
            timeout=3,
        ) as response:
            assert json.load(response) == saved["detail"]
    assert (
        DevopsGateway.configurations["sample-one", "project-one"]["settings"] != DevopsGateway.configurations["sample-two", "project-two"]["settings"]
    )
    assert all("/services/devops/" in path for _, path, _, _ in DevopsGateway.requests)


def test_cli_configuration_rejects_foreign_scope_and_stale_revision(installed_devops, tmp_path):
    _, binary, environment = installed_devops
    source = tmp_path / "updated.json"
    source.write_text(json.dumps({"source": "should-not-replace"}))

    def cli(product, project, *arguments):
        result = subprocess.run(
            [str(binary / "adp"), "service", *arguments, "--product", product, "--project", project, "--service", "devops", "--json"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, json.loads(result.stdout), result.stderr

    first = DevopsGateway.configurations["sample-one", "project-one"].copy()
    second = DevopsGateway.configurations["sample-two", "project-two"].copy()
    code, stale, stderr = cli(
        "sample-one",
        "project-one",
        "config",
        "update",
        "--input",
        str(source),
        "--if-match",
        '"incorrect-revision"',
        "--idempotency-key",
        "example-stale-key-0001",
    )
    assert code == 4 and stale["error"]["code"] == "stale_revision", stderr
    code, denied, stderr = cli("sample-one", "project-two", "config", "show")
    assert code == 5 and denied["error"]["code"] == "not_found", stderr
    DevopsGateway.owners["sample-two"] = "Bearer different-user"
    code, denied, stderr = cli("sample-two", "project-two", "config", "show")
    assert code == 5 and denied["error"]["code"] == "not_found", stderr
    assert "sample-two-source" not in json.dumps(denied) + stderr
    assert DevopsGateway.configurations["sample-one", "project-one"] == first
    assert DevopsGateway.configurations["sample-two", "project-two"] == second
    assert [(method, etag) for method, _, etag, _ in DevopsGateway.requests if method == "PUT"] == [("PUT", '"incorrect-revision"')]


def test_cli_operations(installed_devops, tmp_path):
    address, binary, environment = installed_devops
    product, project = "sample-one", "project-one"
    scope = ["--product", product, "--project", project, "--service", "devops", "--json"]

    def cli(*arguments):
        result = subprocess.run([str(binary / "adp"), "service", *arguments, *scope], env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)

    source = tmp_path / "run.json"
    source.write_text(
        json.dumps(
            {
                "service_id": "devops",
                "binding_id": project + "-binding",
                "configuration_revision": 1,
                "capability": "investigation",
                "devops_input": {
                    "kind": "investigation",
                    "question": "What changed?",
                    "telemetry_refs": [],
                    "resource_refs": [
                        {
                            "tenant_id": "example-tenant",
                            "project_id": project,
                            "source_connection_id": "sample-connection",
                            "record_type": "resource",
                            "record_id": "sample-resource",
                            "record_revision": "revision-one",
                        }
                    ],
                },
            }
        )
    )
    run = cli("run", "--input", str(source), "--idempotency-key", "example-run-key-0001")["detail"]
    assert run["native_job_ref"] == {"job_id": project + "-native"}
    assert cli("runs")["detail"]["items"] == [run]
    assert cli("status", "--run-id", run["run_id"])["detail"] == run
    assert cli("wait", "--run-id", run["run_id"], "--wait-seconds", "1")["detail"] == run
    assert cli("results")["detail"]["items"][0]["run_id"] == run["run_id"]
    result = cli("result", "show", "--result-id", project + "-result")
    assert result["detail"]["native_job_ref"] == run["native_job_ref"]
    assert cli("prevention", "status", "--binding-id", project + "-binding")["detail"]["ready"] is True
    source.write_text(json.dumps({"action": "pause", "configuration_revision": 1}))
    prevention = cli(
        "prevention",
        "configure",
        "--binding-id",
        project + "-binding",
        "--input",
        str(source),
        "--if-match",
        '"binding-1"',
        "--idempotency-key",
        "example-prevention-0001",
    )
    assert prevention["detail"]["action"] == "pause"
    publication = cli("publication", "show", "--publication-id", project + "-publication")
    assert publication["detail"]["run_id"] == run["run_id"]
    artifact_id = project + "-artifact"
    access = cli("artifact", "show", "--artifact-id", artifact_id)
    assert access["detail"]["artifact"]["sha256"] == hashlib.sha256(DevopsGateway.artifact).hexdigest()
    destination = tmp_path / "downloaded.json"
    downloaded = cli("artifact", "download", "--artifact-id", artifact_id, "--output", str(destination))
    assert downloaded["detail"]["sha256"] == access["detail"]["artifact"]["sha256"]
    assert destination.read_bytes() == DevopsGateway.artifact
    assert destination.stat().st_mode & 0o777 == 0o600
    with urlopen(
        Request(
            address + f"/v1/products/{product}/projects/{project}/services/devops/runs/{run['run_id']}",
            headers={"Authorization": DevopsGateway.owners[product]},
        ),
        timeout=3,
    ) as response:
        assert json.load(response) == run
    assert len([path for method, path, _, _ in DevopsGateway.requests if method == "POST" and path.endswith("/runs")]) == 1
    assert ("POST", '"binding-1"', "example-prevention-0001") in [
        (method, etag, key) for method, path, etag, key in DevopsGateway.requests if path.endswith("/prevention")
    ]


def test_installed_operation_manifest_matches_shared_contract(installed_devops):
    _, binary, _ = installed_devops
    rows = [line.split("\t") for line in (CLI.parents[2] / "docs/l5-services/contracts/v1/operations.tsv").read_text().splitlines()[1:]]
    paths = {row[0]: row[2] for row in rows}
    manifest = json.loads((binary / "command-manifest.json").read_text())
    commands = {entry["command"]: entry for entry in manifest["commands"]}
    base = "/v1/products/{product_id}/projects/{project_id}/services/{service_id}"
    for name, operation in (
        ("run", "submitRun"),
        ("status", "getRun"),
        ("results", "listResults"),
        ("result show", "getResult"),
        ("prevention status", "getDevopsPrevention"),
        ("prevention configure", "configureDevopsPrevention"),
        ("publication show", "getPublication"),
        ("publication retry", "retryPublication"),
        ("artifact show", "getArtifactAccess"),
    ):
        assert commands["adp service " + name]["request"]["paths"] == [base + paths[operation]]
    assert "tests/cli/test_devops_service.py" in commands["adp service publication show"]["tests"]


def test_publication_retry_does_not_submit_assessment(installed_devops, tmp_path):
    address, binary, environment = installed_devops
    product, project = "sample-one", "project-one"
    scope = ["--product", product, "--project", project, "--service", "devops", "--json"]

    def cli(*arguments):
        result = subprocess.run([str(binary / "adp"), "service", *arguments, *scope], env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)["detail"]

    run_input = tmp_path / "run.json"
    run_input.write_text(
        json.dumps(
            {
                "service_id": "devops",
                "configuration_revision": 1,
                "binding_id": project + "-binding",
                "capability": "investigation",
                "devops_input": {
                    "kind": "investigation",
                    "question": "What changed?",
                    "resource_refs": [
                        {
                            "tenant_id": "example-tenant",
                            "project_id": project,
                            "source_connection_id": "sample-connection",
                            "record_type": "resource",
                            "record_id": "sample-resource",
                            "record_revision": "revision-one",
                        }
                    ],
                    "telemetry_refs": [],
                },
            }
        )
    )
    run = cli("run", "--input", str(run_input), "--idempotency-key", "example-run-key-0001")
    retry_input = tmp_path / "reason.json"
    retry_input.write_text(json.dumps({"reason": "Context publication was interrupted"}))
    for _ in range(2):
        retry = cli(
            "publication",
            "retry",
            "--publication-id",
            project + "-publication",
            "--input",
            str(retry_input),
            "--idempotency-key",
            "example-retry-key-0001",
        )
        assert retry == {"publication_id": project + "-publication", "run_id": run["run_id"]}
    with urlopen(
        Request(
            address + f"/v1/products/{product}/projects/{project}/services/devops/publications/" + project + "-publication",
            headers={"Authorization": DevopsGateway.owners[product]},
        ),
        timeout=3,
    ) as response:
        assert json.load(response) == retry
    assert DevopsGateway.assessments == 1
    assert DevopsGateway.publication_retries == 2
    assert [(path, key) for method, path, _, key in DevopsGateway.requests if method == "POST"] == [
        (f"/api/v1/products/{product}/projects/{project}/services/devops/runs", "example-run-key-0001"),
        *[(f"/api/v1/products/{product}/projects/{project}/services/devops/publications/" + project + "-publication/retry", "example-retry-key-0001")]
        * 2,
    ]


def test_cli_errors(installed_devops, tmp_path):
    _, binary, environment = installed_devops
    product, project = "sample-one", "project-one"
    scope = ["--product", product, "--project", project, "--service", "devops", "--json"]
    source = tmp_path / "invalid-run.json"
    source.write_text(json.dumps({"configuration_revision": 1, "binding_id": project + "-binding"}))
    invalid = subprocess.run(
        [str(binary / "adp"), "service", "run", "--input", str(source), "--idempotency-key", "example-run-key-0001", *scope],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["error"]["code"] == "usage_error"
    assert not any(path.endswith("/runs") for _, path, _, _ in DevopsGateway.requests)

    source.write_text(
        json.dumps(
            {
                "service_id": "devops",
                "binding_id": project + "-binding",
                "configuration_revision": 1,
                "capability": "investigation",
                "devops_input": {
                    "kind": "investigation",
                    "question": "What changed?",
                    "resource_refs": [
                        {
                            "tenant_id": "example-tenant",
                            "project_id": "project-two",
                            "source_connection_id": "private-source-secret",
                            "record_type": "resource",
                            "record_id": "private-other-resource",
                            "record_revision": "revision-one",
                        }
                    ],
                    "telemetry_refs": [],
                },
            }
        )
    )
    denied = subprocess.run(
        [str(binary / "adp"), "service", "run", "--input", str(source), "--idempotency-key", "example-denied-key-0001", *scope],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert denied.returncode == 5 and json.loads(denied.stdout)["error"]["code"] == "not_found"
    assert "private-source-secret" not in denied.stdout + denied.stderr
    assert DevopsGateway.assessments == 0

    run = {
        "run_id": project + "-run",
        "revision": 1,
        "state": "running",
        "origin": "adp_submitted",
        "scope": {"product_id": product, "project_id": project},
    }
    DevopsGateway.runs[product, project] = run
    previous_requests = len(DevopsGateway.requests)
    watch_path = f"/api/v1/products/{product}/projects/{project}/services/devops/runs/{run['run_id']}"
    process = subprocess.Popen(
        [sys.executable, str(binary / "adp-service.py"), "wait", "--run-id", run["run_id"], "--wait-seconds", "30", "--poll-seconds", "1", *scope],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        # Arrival at the server does not prove the CLI has read the receipt.
        # A second poll does: the first receipt was parsed before that poll began.
        while sum(method == "GET" and path == watch_path for method, path, _, _ in DevopsGateway.requests[previous_requests:]) < 2:
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        output, error = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    pending = json.loads(output)
    assert process.returncode == 7 and pending["status"] == "pending", error
    assert pending["detail"] == run
    assert "hosted work continues" in pending["next_action"]
    assert not any(method == "POST" for method, _, _, _ in DevopsGateway.requests[previous_requests:])
    assert DevopsGateway.runs[product, project] == run


@pytest.mark.parametrize("etag", ['"project-one-first"', ""])
def test_conflicting_preconditions_send_no_write(installed_devops, tmp_path, etag):
    _, binary, environment = installed_devops
    source = tmp_path / "settings.json"
    source.write_text(json.dumps({"source": "should-not-replace"}))
    original = DevopsGateway.configurations["sample-one", "project-one"].copy()
    result = subprocess.run(
        [
            str(binary / "adp"),
            "service",
            "config",
            "update",
            "--product",
            "sample-one",
            "--project",
            "project-one",
            "--service",
            "devops",
            "--input",
            str(source),
            "--if-match",
            etag,
            "--if-none-match",
            "--idempotency-key",
            "example-conflict-key-0001",
            "--json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = json.loads(result.stdout)
    assert result.returncode == 2 and output["status"] == "failed", result.stderr
    assert output["error"]["code"] == "usage_error" and output["detail"] == {}
    assert DevopsGateway.requests == []
    assert DevopsGateway.configurations["sample-one", "project-one"] == original


def test_foreign_run_source_and_missing_product_fail_closed(installed_devops, tmp_path):
    _, binary, environment = installed_devops
    source = tmp_path / "foreign-source.json"
    source.write_text(
        json.dumps(
            {
                "service_id": "devops",
                "binding_id": "project-one-binding",
                "configuration_revision": 1,
                "capability": "investigation",
                "devops_input": {
                    "kind": "investigation",
                    "question": "What changed?",
                    "resource_refs": [
                        {
                            "tenant_id": "example-tenant",
                            "project_id": "project-two",
                            "source_connection_id": "private-source-secret",
                            "record_type": "resource",
                            "record_id": "private-other-resource",
                            "record_revision": "revision-one",
                        }
                    ],
                    "telemetry_refs": [],
                },
            }
        )
    )
    command = [
        str(binary / "adp"),
        "service",
        "run",
        "--input",
        str(source),
        "--idempotency-key",
        "example-foreign-key-0001",
        "--service",
        "devops",
        "--json",
    ]
    missing = subprocess.run([*command, "--project", "project-one"], env=environment, capture_output=True, text=True, timeout=30)
    assert missing.returncode == 2 and json.loads(missing.stdout)["error"]["code"] == "usage_error"
    assert DevopsGateway.requests == []
    denied = subprocess.run(
        [*command, "--product", "sample-one", "--project", "project-one"], env=environment, capture_output=True, text=True, timeout=30
    )
    failure = json.loads(denied.stdout)
    assert denied.returncode == 5 and failure["error"]["code"] == "not_found", denied.stderr
    assert failure["detail"] == {}
    assert "private-source-secret" not in denied.stdout + denied.stderr
    assert "private-other-resource" not in denied.stdout + denied.stderr
    assert DevopsGateway.assessments == 0
    assert DevopsGateway.runs == {}
    assert DevopsGateway.requests[0][1] == "/api/v1/products/sample-one/projects/project-one/services/devops/runs"


def test_uncertain_submission_reads_original_run_without_retry(installed_devops, tmp_path):
    _, binary, environment = installed_devops
    project = "project-one"
    source = tmp_path / "run.json"
    source.write_text(
        json.dumps(
            {
                "service_id": "devops",
                "binding_id": project + "-binding",
                "configuration_revision": 1,
                "capability": "investigation",
                "devops_input": {
                    "kind": "investigation",
                    "question": "What changed?",
                    "resource_refs": [
                        {
                            "tenant_id": "example-tenant",
                            "project_id": project,
                            "source_connection_id": "sample-connection",
                            "record_type": "resource",
                            "record_id": "sample-resource",
                            "record_revision": "revision-one",
                        }
                    ],
                    "telemetry_refs": [],
                },
            }
        )
    )
    scope = ["--product", "sample-one", "--project", project, "--service", "devops", "--json"]
    DevopsGateway.drop_run_response = True
    submitted = subprocess.run(
        [str(binary / "adp"), "service", "run", "--input", str(source), "--idempotency-key", "example-uncertain-0001", *scope],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    failed = json.loads(submitted.stdout)
    assert submitted.returncode == 4 and failed["error"]["code"] == "unknown_mutation_outcome", submitted.stderr
    assert "do not retry blindly" in failed["error"]["message"]
    assert failed["detail"] == {} and DevopsGateway.assessments == 1
    checked = subprocess.run(
        [str(binary / "adp"), "service", "status", "--run-id", project + "-run", *scope], env=environment, capture_output=True, text=True, timeout=30
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["detail"] == DevopsGateway.runs["sample-one", project]
    assert DevopsGateway.assessments == 1
    assert [method for method, path, _, _ in DevopsGateway.requests if path.endswith("/runs")] == ["POST"]
