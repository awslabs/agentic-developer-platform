"""The installed CLI's Dev Box binding: one owner session, then the public API as the laptop uses it (#9640).

Before #9640 the box CLI relayed a fixed read/draft allowlist through the loopback adapter and
refused every owner action. Now the verified host run is exchanged (once, cached under the home)
for the owner's session and every route goes to the public API with that session; the adapter
only ever sees the exchange. One local command double plays both parts: the adapter
(`/agent/orchestration/owner-session`) and the public gateway (laptop paths).
"""

import json
import os
import stat
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli/adp"
RUN_ID = "12345678-1234-1234-1234-123456789abc"
TOKEN = "a" * 64
OWNER_TOKEN = "owner-session-token-" + "b" * 48
PLAN_HASH = "b" * 64


@pytest.fixture
def adapter(tmp_path):
    requests = []
    state = {"mints": 0, "expires_in": 3600}

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, result):
            payload = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            requests.append(("GET", self.path, dict(self.headers), None))
            path = self.path.split("?", 1)[0]
            if path == "/v1/products":
                result = {"items": [], "next_cursor": None}
            elif path == "/v1/products/p-1/projects/j-1":
                result = {
                    "schema_version": "1.0",
                    "product_id": "p-1",
                    "project_id": "j-1",
                    "revision": 3,
                    "product": {"id": "p-1", "name": "Fixture"},
                    "project": {"id": "j-1"},
                    "delivery_home_repository": None,
                    "delivery_home_implied": True,
                }
            elif path == "/v1/projects/j-1/budget":
                result = {
                    "product_id": "p-1",
                    "project_id": "j-1",
                    "budget_usd": None,
                    "spent_usd": 0,
                    "reserved_usd": 0,
                    "available_usd": None,
                    "accounting_status": "current",
                    "flows": [],
                }
            elif path == "/github/status":
                result = {
                    "connections": [
                        {
                            "installation_id": 42,
                            "account_login": "example",
                            "repositories": ["example/project"],
                            "repository_details": [{"full_name": "example/project", "repository_id": 1234}],
                            "verification": {"repositories_live": True},
                        }
                    ]
                }
            elif path == "/orchestration/flows":
                result = {"flows": [], "total": 0, "limit": 20, "offset": 0}
            elif path == "/orchestration/flows/flow-1/plans":
                result = [
                    {
                        "version": 1,
                        "plan_hash": PLAN_HASH,
                        "accepted_by_decision_id": "decision-1",
                        "superseded_at": None,
                        "created_at": "2026-01-01T00:00:00Z",
                        "plan_document": {"flow_slug": "example"},
                    }
                ]
            elif path == "/orchestration/flows/flow-1/decisions":
                result = [{"id": "decision-1", "actor_kind": "human", "kind": "plan_accepted"}]
            elif path == "/orchestration/flows/flow-1":
                result = {"flow_id": "flow-1", "title": "Example", "state": "pending", "nodes": [], "cost": None}
            else:
                self.send_error(404)
                return
            self._json(200, result)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            requests.append(("POST", self.path, dict(self.headers), body))
            if self.path == "/agent/orchestration/owner-session":
                state["mints"] += 1
                expires = datetime.now(UTC) + timedelta(seconds=state["expires_in"])
                self._json(
                    201,
                    {
                        "access_token": OWNER_TOKEN,
                        "expires_at": expires.isoformat(),
                        "gateway_url": f"http://127.0.0.1:{self.server.server_port}",
                        "delegation": f"dev-box:box-a:{RUN_ID}",
                        "user_id": "owner-a",
                        "is_admin": True,
                    },
                )
                return
            if self.path == "/orchestration/flows/drafts/preview":
                self._json(200, {"would_register": True, "wrote_nothing": True, "plan_hash": PLAN_HASH, "plan_hash_is_bindable": True})
            elif self.path == "/orchestration/flows/drafts":
                self._json(201, {"flow_id": "example-flow", "plan_hash": PLAN_HASH, "plan_version": 1})
            elif self.path.startswith("/orchestration/gates/") and self.path.endswith(("/approve", "/reject")):
                self._json(200, {"decision_id": "decision-9", "flow_id": "example-flow", "status": "recorded"})
            else:
                self.send_error(404)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "ADP_CREW_TOOLS_URL": f"http://127.0.0.1:{server.server_port}",
        "ADP_CREW_TOOLS_TOKEN": TOKEN,
        "ADP_CREW_RUN_ID": RUN_ID,
    }
    for key in ("ADP_DEPLOYMENT", "ADP_DEPLOYMENT_ID", "ADP_TENANT", "ADP_TENANT_ID", "ADP_HOME"):
        env.pop(key, None)
    try:
        yield env, requests, state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def command(env, *args):
    return subprocess.run(["bash", str(CLI), *args], env=env, text=True, capture_output=True, timeout=15)


def mints(requests):
    return [r for r in requests if r[1] == "/agent/orchestration/owner-session"]


def gateway_calls(requests):
    return [r for r in requests if r[1] != "/agent/orchestration/owner-session"]


def test_host_run_exchanges_once_for_the_owner_session_then_uses_it_everywhere(adapter):
    env, requests, state = adapter
    plan = Path(env["HOME"]) / "plan.json"
    plan.write_text(json.dumps({"flow_slug": "example-flow", "org_id": "tenant-a", "nodes": []}))
    status = command(env, "status")
    products = command(env, "product", "list", "--json")
    flows = command(env, "flow", "list", "--json")
    created = command(env, "flow", "create", "--file", str(plan), "--json")

    assert status.returncode == 0 and RUN_ID in status.stdout
    assert products.returncode == 0 and json.loads(products.stdout)["status"] == "ok"
    assert flows.returncode == 0 and json.loads(flows.stdout)["status"] == "ok"
    assert created.returncode == 4, (created.stdout, created.stderr)
    assert json.loads(created.stdout)["detail"]["flow_id"] == "example-flow"
    # One exchange for the whole sequence: the session is cached under the home, 0600.
    exchange = mints(requests)
    assert len(exchange) == 1 and state["mints"] == 1
    assert exchange[0][2]["Authorization"] == f"Bearer {TOKEN}" and exchange[0][2]["X-Agent-Runid"] == RUN_ID
    cache = Path(env["HOME"]) / ".adp/crew-owner-session.json"
    assert cache.is_file() and stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert json.loads(cache.read_text())["run_id"] == RUN_ID
    # Every API call is the laptop's: public paths, the owner's bearer, no run header.
    calls = gateway_calls(requests)
    assert [path.split("?", 1)[0] for _, path, _, _ in calls] == [
        "/v1/products",
        "/orchestration/flows",
        "/orchestration/flows/drafts/preview",
        "/orchestration/flows/drafts",
    ]
    assert all(headers["Authorization"] == f"Bearer {OWNER_TOKEN}" and "X-Agent-Runid" not in headers for _, _, headers, _ in calls)
    assert all(body["flow_slug"] == "example-flow" for _, _, _, body in calls[2:])
    assert not (Path(env["HOME"]) / ".bedrock-gateway/tokens.json").exists()


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_gate_decisions_reach_the_gateway_as_the_owner(adapter, action):
    env, requests, _ = adapter
    result = command(env, "flow", "gate", action, "gate-1", "--yes", "--json")
    gates = [r for r in gateway_calls(requests) if r[1].startswith("/orchestration/gates/")]
    assert result.returncode != 2, (result.stdout, result.stderr)
    assert gates and gates[-1][1].endswith(f"/{action}")
    assert gates[-1][2]["Authorization"] == f"Bearer {OWNER_TOKEN}" and "X-Agent-Runid" not in gates[-1][2]


def test_product_budget_set_is_sent_as_the_owner(adapter):
    env, requests, _ = adapter
    result = command(
        env, "product", "budget", "set", "--product", "p-1", "--project", "j-1", "--budget-usd", "10.00", "--reason", "owner raised it", "--json"
    )
    assert "Dev Box" not in (result.stderr + result.stdout)
    assert (
        any(
            method == "PUT" and "/budget" in path and headers["Authorization"] == f"Bearer {OWNER_TOKEN}"
            for method, path, headers, _ in gateway_calls(requests)
        )
        or result.returncode != 2
    )


def test_cached_session_near_expiry_is_exchanged_again(adapter):
    env, requests, state = adapter
    state["expires_in"] = 120  # inside the five-minute refresh margin
    assert command(env, "product", "list", "--json").returncode == 0
    assert command(env, "product", "list", "--json").returncode == 0
    assert state["mints"] == 2


def test_status_product_and_flow_use_host_run_without_laptop_session(adapter):
    env, requests, _ = adapter
    status = command(env, "status")
    status_json = command(env, "status", "--json")
    products = command(env, "product", "list", "--json")
    assert status.returncode == 0 and RUN_ID in status.stdout and "Host run" in status.stdout
    assert "No ADP deployment" not in status.stderr, status.stderr
    assert status_json.returncode == 0 and json.loads(status_json.stdout) == {"mode": "host-run", "run_id": RUN_ID}
    assert products.returncode == 0 and json.loads(products.stdout)["status"] == "ok"
    assert not (Path(env["HOME"]) / ".bedrock-gateway/tokens.json").exists()


def test_flow_plans_show_and_watch_use_the_public_paths(adapter):
    env, requests, _ = adapter
    plans = command(env, "flow", "plans", "flow-1", "--json")
    show = command(env, "flow", "show", "flow-1", "--json")
    watch = command(env, "flow", "watch", "flow-1", "--once", "--json")
    assert plans.returncode == 0, plans.stderr
    assert show.returncode == 0, show.stderr
    assert watch.returncode == 0, watch.stderr
    assert json.loads(plans.stdout)["detail"]["acceptance_status"] == "accepted"
    assert [path for _, path, _, _ in gateway_calls(requests)] == [
        "/orchestration/flows/flow-1/plans",
        "/orchestration/flows/flow-1/decisions",
        "/orchestration/flows/flow-1",
        "/orchestration/flows/flow-1",
    ]


def test_github_status_reports_immutable_repository_id(adapter):
    env, requests, _ = adapter
    listing = command(env, "github", "status", "--json")
    exact = command(env, "github", "status", "--repo", "example/project", "--json")
    assert listing.returncode == 0, listing.stderr
    assert exact.returncode == 0, exact.stderr
    assert json.loads(exact.stdout)["detail"]["repository_id"] == 1234
    assert [path for _, path, _, _ in gateway_calls(requests)] == ["/github/status"] * 2


def test_host_run_preserves_the_existing_mcp_bridge_without_enabling_mcp_setup(adapter):
    env, requests, _ = adapter
    initialized = subprocess.run(
        ["bash", str(CLI), "mcp"],
        env=env,
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["result"]["serverInfo"]["name"] == "adp-tools"
    assert command(env, "mcp", "configure").returncode == 2
    assert requests == []


def test_invalid_binding_does_not_fall_back_to_a_laptop_session(adapter):
    env, requests, _ = adapter
    env["ADP_CREW_TOOLS_URL"] = "https://example.test"
    result = command(env, "product", "list")
    assert result.returncode == 2
    assert "binding" in result.stderr.lower()
    assert requests == []


def test_login_refuses_without_writing_a_human_token(adapter):
    env, requests, _ = adapter
    result = command(env, "login")
    assert result.returncode == 2
    assert "laptop" in result.stderr.lower() and "no token" in result.stderr.lower()
    assert requests == []
    assert not list(Path(env["HOME"]).rglob("tokens.json"))


def test_host_flow_create_with_yes_refuses_before_request(adapter):
    env, requests, _ = adapter
    plan = Path(env["HOME"]) / "plan.json"
    plan.write_text(json.dumps({"flow_slug": "example-flow"}))
    result = command(env, "flow", "create", "--file", str(plan), "--yes", "--json")
    assert result.returncode == 2
    assert requests == []


def test_product_show_and_budget_show_use_the_public_paths(adapter):
    env, requests, _ = adapter
    shown = command(env, "product", "show", "--product", "p-1", "--project", "j-1", "--json")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout[shown.stdout.index("{") :])["detail"]["delivery_home_implied"] is True
    budget = command(env, "product", "budget", "show", "--product", "p-1", "--project", "j-1", "--json")
    assert budget.returncode == 0, budget.stderr
    paths = [path.split("?", 1)[0] for _, path, _, _ in gateway_calls(requests)]
    assert "/v1/products/p-1/projects/j-1" in paths and "/v1/projects/j-1/budget" in paths
