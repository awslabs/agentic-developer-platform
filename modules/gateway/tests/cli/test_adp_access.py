"""Access CLI serialization and uncertain mutation handling."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

spec = importlib.util.spec_from_file_location("access_cli", Path(__file__).parents[2] / "cli" / "adp-access.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture(autouse=True)
def advisory(monkeypatch):
    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)


def request():
    return {
        "id": "req",
        "requester": "person",
        "target_tenant": "org",
        "status": "pending",
        "revision": "a" * 64,
        "proposed_role": "member",
        "requested_scope": "join_existing",
    }


def args(*extra):
    return cli.parser().parse_args(
        [
            "access-request",
            "approve",
            "--request",
            "req",
            "--expected-revision",
            "a" * 64,
            "--expected-role",
            "member",
            "--expected-scope",
            "join_existing",
            "--operation-id",
            str(uuid4()),
            "--reason",
            "reviewed",
            *extra,
        ]
    )


def test_preview_reads_exact_request_without_mutation():
    client = Mock()
    client.request.return_value = request()
    result = cli.execute(args("--dry-run", "--yes"), client)
    assert result["status"] == "preview"
    client.request.assert_called_once_with("GET", "/admin/access-requests/req/review")


def test_decision_serializes_reviewed_scope_and_operation():
    parsed = args("--yes")
    client = Mock()
    client.request.side_effect = [
        request(),
        {"request_id": "req", "operation_id": parsed.operation_id, "status": "approved", "tenant_id": "org", "granted_role": "member"},
    ]
    result = cli.execute(parsed, client)
    assert result["status"] == "ok"
    body = client.request.call_args.args[2]
    from src.admin.onboarding.cli_contract import Decision

    validated = Decision.model_validate(body)
    assert str(validated.operation_id) == parsed.operation_id
    assert validated.expected_role == "member"


@pytest.mark.parametrize("reply", [{}, {"status": "approved"}, {"request_id": "foreign"}])
def test_bad_ack_is_unknown(reply):
    client = Mock()
    client.request.side_effect = [request(), reply]
    with pytest.raises(cli.common.CliError) as exc:
        cli.execute(args("--yes"), client)
    assert exc.value.code == "unknown_mutation_outcome"


def test_foreign_review_never_mutates():
    client = Mock()
    client.request.return_value = {**request(), "id": "foreign"}
    with pytest.raises(cli.common.CliError):
        cli.execute(args("--yes"), client)
    assert client.request.call_count == 1


def test_real_shell_keeps_requested_tenant_distinct_from_selected_workspace(adp_script, adp_home):
    import json
    import os
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from .conftest import write_adp_session

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            calls.append(self.path)
            payload = json.dumps({"status": "no_membership", "tenant_id": "requested", "spend_eligibility": "not_evaluated"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        write_adp_session(adp_home)
        (adp_home / ".bedrock-gateway/config.json").write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{server.server_port}/api"}))
        result = subprocess.run(
            ["bash", str(adp_script), "access", "status", "--tenant", "requested", "--json"],
            env=dict(os.environ, HOME=str(adp_home)),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["detail"]["tenant_id"] == "requested"
        assert calls == ["/api/access/status?target_tenant=requested"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("remaining", [0, 1])
def test_revoke_reports_pending_when_new_tokens_remain(remaining):
    parsed = cli.parser().parse_args(
        ["session", "revoke-user", "--user", "user", "--org", "org", "--reason", "reviewed", "--expected-revision", "a" * 64, "--yes"]
    )
    before = {"user_id": "user", "org": "org", "revision": "a" * 64, "effect": "Gateway JWT only"}
    client = Mock()
    client.request.side_effect = [before, {**before, "tokens_revoked": 1, "active_gateway_tokens": remaining, "revocation_complete": remaining == 0}]
    result = cli.execute(parsed, client)
    assert result["status"] == ("pending" if remaining else "ok")


@pytest.mark.parametrize(
    "extra", [{}, {"active_gateway_tokens": 1, "revocation_complete": True}, {"active_gateway_tokens": "0", "revocation_complete": True}]
)
def test_revoke_refuses_malformed_completion_ack(extra):
    parsed = cli.parser().parse_args(
        ["session", "revoke-user", "--user", "user", "--org", "org", "--reason", "reviewed", "--expected-revision", "a" * 64, "--yes"]
    )
    before = {"user_id": "user", "org": "org", "revision": "a" * 64, "effect": "Gateway JWT only"}
    client = Mock()
    client.request.side_effect = [before, {**before, "tokens_revoked": 1, **extra}]
    with pytest.raises(cli.common.CliError) as exc:
        cli.execute(parsed, client)
    assert exc.value.code == "unknown_mutation_outcome"
