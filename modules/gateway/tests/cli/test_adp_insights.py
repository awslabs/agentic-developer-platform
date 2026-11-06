"""Installed Insights reads retain source identities and existing CLI behavior."""

import base64
import importlib.util
import json
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import override
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.cli.conftest import MockGatewayHandler, write_adp_session

CLI = Path(__file__).parents[2] / "cli"
spec = importlib.util.spec_from_file_location("adp_insights_candidate", CLI / "adp-insights.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
TIME = "2026-10-19T12:00:00Z"


def result():
    work = {
        "tenant_id": "example-tenant",
        "project_id": "example-project",
        "source_connection_id": "example-source",
        "record_type": "work",
        "record_id": "example-work",
        "record_revision": "example-revision-1",
    }
    product = {**work, "record_type": "product", "record_id": "example-product"}
    initiative = {**work, "record_type": "initiative", "record_id": "example-initiative"}
    contribution = {**work, "record_type": "contribution", "record_id": "example-contribution"}
    cost = {**work, "record_type": "cost", "record_id": "example-cost"}
    relationship = {
        "relationship_id": "example-link",
        "relationship_revision": "example-revision-1",
        "mapping_version": "example-map-1",
        "from_ref": work,
        "to_ref": product,
    }
    return {
        "schema_version": "results/1.0",
        "view": "business_context",
        "project_id": "example-project",
        "product_id": "example-product",
        "as_of": TIME,
        "dataset_version": "example-dataset-1",
        "mapping_versions": ["example-map-1"],
        "coverage": "complete",
        "grouping": "historical",
        "work_count": 1,
        "cost_by_currency": {"USD": "10.00"},
        "allocated_by_currency": {},
        "unallocated_by_currency": {"USD": "10.00"},
        "contexts": [
            {
                "subject_ref": work,
                "grouping": "historical",
                "effective_at": TIME,
                "product_refs": [product],
                "portfolio_ids": ["example-commerce", "example-shared"],
                "initiative_refs": [initiative],
                "contribution_refs": [contribution],
                "relationship_versions": [relationship],
            },
            {
                "subject_ref": cost,
                "grouping": "historical",
                "effective_at": TIME,
                "product_refs": [product],
                "portfolio_ids": ["example-commerce", "example-shared"],
                "initiative_refs": [],
                "contribution_refs": [],
                "relationship_versions": [],
            },
        ],
        "associations": [
            {"subject_ref": work, "portfolio_ids": ["example-commerce", "example-shared"]},
            {"subject_ref": cost, "portfolio_ids": ["example-commerce", "example-shared"]},
        ],
        "allocation_versions": [],
        "next_cursor": None,
    }


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        return self.response


def test_context_show_reads_once_without_recalculating_cost():
    response = result()
    client = FakeClient(response)
    args = helper.parser().parse_args(
        [
            "context",
            "show",
            "--project",
            "example-project",
            "--product",
            "example-product",
            "--as-of",
            TIME,
            "--portfolio",
            "example-commerce",
            "--portfolio",
            "example-shared",
            "--json",
        ]
    )
    observed = helper.execute(args, client)
    assert observed["detail"] is response
    assert observed["detail"]["cost_by_currency"] == {"USD": "10.00"}
    assert len(client.calls) == 1 and client.calls[0][0] == "GET"
    query = parse_qs(urlsplit(client.calls[0][1]).query)
    assert query["portfolio_id"] == ["example-commerce", "example-shared"]
    assert query["as_of"] == [TIME]
    for bad in (dict(response, coverage="incomplete"), dict(response, dataset_version="latest", mapping_versions=[]), {}):
        try:
            helper.execute(args, FakeClient(bad))
        except helper.common.CliError as error:
            assert error.code == "invalid_response"
        else:
            raise AssertionError("an incomplete response was accepted")


class CandidateGateway(MockGatewayHandler):
    requests = []

    reply = None

    def log_message(self, *args):
        pass

    @override
    def do_GET(self):
        path = urlsplit(self.path)
        status = 200
        if path.path.startswith("/api/cli/"):
            name = path.path.removeprefix("/api/cli/")
            candidate = CLI / name
            if not candidate.is_file() or name != candidate.name:
                self.send_error(404)
                return
            data = candidate.read_bytes()
        elif path.path == "/api/engineering-insights/v1/projects/example-project/results":
            self.__class__.requests.append((path, self.headers.get("Authorization")))
            if self.__class__.reply is None:
                data = json.dumps(result()).encode()
            else:
                status, payload = self.__class__.reply
                data = json.dumps(payload).encode()
        else:
            self.send_error(404)
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if path.path.endswith(".json") or "/results" in path.path else "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.mark.parametrize(
    ("http_status", "payload", "expected_code", "expected_status", "expected_exit"),
    [
        (501, {"error": {"code": "unsupported_capability", "message": "example-source secret"}}, "unsupported_operation", "failed", 5),
        (404, {"detail": "Not Found"}, "unsupported_operation", "failed", 5),
        (404, {"error": {"code": "not_found", "message": "Result unavailable"}}, "not_found", "failed", 5),
        (409, {"error": {"code": "projection_pending", "message": "example-source secret"}, "work_count": 0}, "projection_pending", "pending", 4),
        (
            409,
            {"error": {"code": "projection_incomplete", "message": "example-source secret"}, "work_count": 0},
            "projection_incomplete",
            "unavailable",
            4,
        ),
    ],
)
def test_cli_read_errors_do_not_return_partial_or_hidden_results(
    monkeypatch, capsys, http_status, payload, expected_code, expected_status, expected_exit
):
    CandidateGateway.requests = []
    CandidateGateway.reply = (http_status, payload)
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), CandidateGateway)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    try:
        monkeypatch.setattr(helper.common, "gateway_url", lambda: f"http://127.0.0.1:{gateway.server_port}/api")
        monkeypatch.setattr(helper.common, "access_token", lambda: "example-session")
        code = helper.main(["context", "show", "--project", "example-project", "--product", "example-product", "--as-of", TIME, "--json"])
        observed = json.loads(capsys.readouterr().out)
        assert code == expected_exit
        assert observed["status"] == expected_status
        assert observed["error"]["code"] == expected_code
        assert observed["detail"] == {}
        assert "example-source" not in json.dumps(observed)
        assert "work_count" not in json.dumps(observed)
        assert len(CandidateGateway.requests) == 1
        assert CandidateGateway.requests[0][1] == "Bearer example-session"
    finally:
        CandidateGateway.reply = None
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=2)


def test_installed_cli_uses_existing_login_and_keeps_product_help(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    binary = tmp_path / "bin"
    installer = tmp_path / "install.sh"
    installer.write_bytes((CLI / "install.sh").read_bytes())
    CandidateGateway.requests = []
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), CandidateGateway)
    CandidateGateway.reply = None
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{gateway.server_port}/api"
        environment = {
            **os.environ,
            "HOME": str(home),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        installed = subprocess.run(
            ["sh", str(installer), "--gateway-url", url, "--prefix", str(binary), "--no-path-edit"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert installed.returncode == 0, installed.stderr
        assert (binary / "adp-insights.py").read_bytes() == (CLI / "adp-insights.py").read_bytes()
        write_adp_session(home)
        tokens_file = home / ".bedrock-gateway" / "tokens.json"
        tokens = json.loads(tokens_file.read_text())
        segments = tokens["access_token"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)))
        claims["org_id"] = "example-tenant"
        segments[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        tokens["access_token"] = ".".join(segments)
        tokens_file.write_text(json.dumps(tokens))
        config = home / ".bedrock-gateway" / "config.json"
        stored = json.loads(config.read_text())
        stored["gateway_url"] = url
        config.write_text(json.dumps(stored))
        command = subprocess.run(
            [
                str(binary / "adp"),
                "insights",
                "context",
                "show",
                "--project",
                "example-project",
                "--product",
                "example-product",
                "--as-of",
                TIME,
                "--json",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert command.returncode == 0, command.stderr
        observed = json.loads(command.stdout)
        assert observed["detail"]["associations"][0]["subject_ref"]["record_id"] == "example-work"
        assert observed["detail"]["cost_by_currency"] == {"USD": "10.00"}
        assert observed["detail"]["contexts"][0]["initiative_refs"][0]["record_id"] == "example-initiative"
        assert observed["detail"]["contexts"][0]["contribution_refs"][0]["record_id"] == "example-contribution"
        assert observed["detail"]["contexts"][0]["relationship_versions"][0]["mapping_version"] == "example-map-1"
        assert {item["subject_ref"]["record_id"] for item in observed["detail"]["associations"]} == {"example-work", "example-cost"}
        assert len(CandidateGateway.requests) == 1
        assert CandidateGateway.requests[0][1].startswith("Bearer ")
        existing = subprocess.run([str(binary / "adp"), "product", "--help"], env=environment, capture_output=True, text=True, timeout=30)
        assert existing.returncode == 0, existing.stderr
        assert "adp product" in existing.stdout
    finally:
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=2)
