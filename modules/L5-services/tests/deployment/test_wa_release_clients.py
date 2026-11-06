"""Real HTTP client contracts and exact installed CLI selection fences."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected_clients as clients


@pytest.fixture
def selected():
    return {"gateway_url": "https://selected.example.invalid", "token_env": "OWNER_TOKEN",
            "scope": {"tenant_id": "tenant", "product_id": "product", "project_id": "project"},
            "run_id": "selected-run", "result_id": "selected-result", "result_revision": 2,
            "qualification_clients": {"mcp_token_env": "MCP_TOKEN", "design_task_id": "later-task",
                                      "denied_token_env": "DENIED_TOKEN"}}


def transport(selected, *, changed=None):
    observed = []
    result = {"result_id": selected["result_id"], "run_id": selected["run_id"],
              "revision": selected["result_revision"], "scope": selected["scope"], "recommendations": []}
    base = "/api/v1/products/product/projects/project/services/well_architected/results/selected-result"
    reference = {"tenant_id": "tenant", "project_id": "project", "source_connection_id": "l5-well-architected",
                 "record_type": "well_architected_result", "record_id": result["result_id"], "record_revision": "2"}
    citation = "well_architected_result:selected-result@2"
    peers = [{"destination": destination, "publication_id": destination + "-publication", "state": "published",
              "receipt_id": destination + "-receipt", "consumer_revision": "1", "projection_verified_on_ack": True}
             for destination in ("product-context", "engineering-insights")]
    scope = {**selected["scope"], "actor_id": "owner", "actions": ["read", "use"], "run_id": result["run_id"],
             "result_id": result["result_id"], "result_revision": 2, "result_digest": clients.canonical(result), "result_scope": result["scope"],
             "cleanup": {"receipt_digest": "a" * 64}, "publications": [{"destination": row["destination"],
                "publication_id": row["publication_id"], "revision": 1, "contribution_hash": "a" * 64, "receipt_digest": "b" * 64} for row in peers]}
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def connection(case):
        def respond(request):
            observed.append((case["token_env"], request.method, request.url.path, request.content))
            if request.url.path == "/api/auth/me":
                return httpx.Response(200, json={"user_id": "denied" if case["token_env"] == "DENIED_TOKEN" else "owner",
                    "org_id": "foreign" if changed == "actor-tenant" else "tenant", "account_type": "human"})
            if request.url.path == "/api/v1/tasks/later-task":
                return httpx.Response(200, json={"task_id": "later-task", "invocation_id": "changed-invocation" if changed == "late-task" and sum(path == "/api/v1/tasks/later-task" for _, _, path, _ in observed) > 1 else "other-invocation", "persona": "design",
                    "status": "running" if changed == "unfinished-task" else "completed", "version": 1,
                    "created_at": "2026-01-01T02:00:00Z", "product_id": "product", "project_id": "other" if changed == "task-project" else "project"})
            if request.url.path == base.rsplit("/results/", 1)[0] + "/runs/selected-run":
                return httpx.Response(200, json={"run_id": "selected-run", "trigger_id": "manual",
                    "admission": {"admitted_at": "2026-01-01T03:00:00Z" if changed == "early-task" else "2026-01-01T01:00:00Z"}})
            if request.url.path == base + "/reuse-scope":
                return httpx.Response(200, json={"scope": scope, "scope_digest": clients.canonical(scope)})
            if request.url.path == base + "/reuse-authorizations":
                return httpx.Response(200, json={"items": [{"authorization_id": "explicit-owner-consent", "revision": 1,
                    "scope_digest": "b" * 64 if changed == "consent-scope" else clients.canonical(scope), "expires_at": expiry,
                    "revoked_at": expiry if changed == "revoked-consent" or changed == "late-consent-revoke" and sum(path.endswith("/reuse-authorizations") for _, _, path, _ in observed) > 1 else None}]})
            if case["token_env"] == "DENIED_TOKEN":
                return httpx.Response(403 if changed == "deny-status" else 404,
                    json={"detail": {"code": "not_found"}, **({"private_count": 1} if changed == "deny-body" else {})})
            if request.url.path == "/api/v1/l5/mcp/tools/call":
                assert json.loads(request.content) == {"tool": "l5___get_result", "arguments": {
                    "product_id": "product", "project_id": "project", "service_id": "well_architected",
                    "result_id": "selected-result"}}
                return httpx.Response(200, json={"structuredContent": {"http_status": 200,
                    "data": {**result, **({"revision": 3} if changed == "mcp-revision" else {})}}})
            if request.url.path == base:
                return httpx.Response(200, json={**result, **({"run_id": "foreign"} if changed == "result" else {})})
            if request.url.path == base + "/publication-status":
                return httpx.Response(200, json={"result_id": "foreign" if changed == "publication" else result["result_id"],
                    "revision": 2, "consumers": peers})
            if request.url.path in {base + "/design-usage", base + "/design-usage/later-task"}:
                return httpx.Response(200, json={"result": {**result, **({"revision": 1} if changed == "usage" else {})},
                    "citation": citation,
                    "retrieval_receipt": {"task_id": "later-task", "actor_id": "foreign" if changed == "receipt-actor" else "owner",
                        "receipt_id": "retrieved", "contribution_id": "product-context-publication", "citation": citation, "result_ref": reference},
                    "usage_receipt": {"task_id": "foreign" if changed == "receipt-task" else "later-task", "actor_id": "owner",
                        "receipt_id": "used", "contribution_id": "engineering-insights-publication", "citation": citation,
                        "result_ref": reference, "retrieval_receipt_id": "foreign" if changed == "receipt-link" else "retrieved"}})
            pytest.fail("unselected request: " + str(request.url))

        return httpx.Client(base_url=case["gateway_url"], transport=httpx.MockTransport(respond))

    return connection, observed


@pytest.mark.parametrize("read_only", [True, False])
def test_actual_http_contract_retains_same_revision_and_emission_does_not_repeat_usage(selected, read_only):
    connection, requests = transport(selected)
    report = clients.collect(selected, connection, read_only=read_only)
    assert set(report["clients"]) == {"api", "mcp"}
    assert len(set(report["clients"].values())) == 1
    assert report["usage"]["task_id"] == "later-task"
    assert report["denial"]["status"] == 404 and report["denial"]["source_revocation_observed"] is False
    assert report["usage"]["actor_id"] == "owner" and report["usage"]["provider_effect_count"] is None
    usage = [item for item in requests if "/design-usage" in item[2]]
    assert len(usage) == 1 and usage[0][1] == ("GET" if read_only else "POST")
    assert not any(item[1] != "GET" and ("runs" in item[2] or "binding-operations" in item[2]) for item in requests)


@pytest.mark.parametrize("changed", ["result", "publication", "usage", "mcp-revision", "deny-status", "deny-body", "actor-tenant", "unfinished-task", "task-project", "early-task", "receipt-actor", "receipt-task", "receipt-link", "late-task"])
def test_foreign_changed_or_leaky_client_responses_never_count_as_parity(selected, changed):
    connection, _ = transport(selected, changed=changed)
    with pytest.raises(ValueError):
        clients.collect(selected, connection, read_only=True)


@pytest.fixture
def installed_cli(tmp_path):
    directory = tmp_path.resolve()
    home = directory / "selected-home"
    home.mkdir(mode=0o700)
    pins = {}
    for name in ("adp", "adp_deployments.py", "adp-service.py", "adp_common.py", "bg-cognito-auth.sh"):
        path = directory / name
        path.write_text("# selected installed client " + name)
        path.chmod(0o700 if name == "adp" else 0o600)
        pins[name] = sha256(path.read_bytes()).hexdigest()
    return {"executable": str(directory / "adp"), "files_sha256": pins,
            "adp_home": str(home), "deployment": "selected"}


@pytest.mark.parametrize("wrong_url", [False, True])
def test_cli_resolves_selected_deployment_before_reading_result(selected, installed_cli, monkeypatch, wrong_url):
    calls = []
    monkeypatch.setenv("ADP_GATEWAY_URL", "https://foreign.invalid")
    monkeypatch.setenv("BASH_ENV", "/foreign/script")

    def run(command, **options):
        calls.append(command)
        assert options["env"]["ADP_HOME"] == installed_cli["adp_home"]
        assert "ADP_GATEWAY_URL" not in options["env"] and "BASH_ENV" not in options["env"]
        if len(calls) == 1:
            value = {"deployment": "selected", "gateway_url": ("https://foreign.invalid" if wrong_url else selected["gateway_url"]) + "/api"}
        else:
            assert command[1:] == ["--deployment", "selected", "service", "result", "show", "--service", "well_architected",
                "--product", "product", "--project", "project", "--result-id", "selected-result", "--json"]
            value = {"status": "ok", "detail": {"result_id": "selected-result"}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

    if wrong_url:
        with pytest.raises(ValueError, match="another gateway"):
            clients.cli_result(selected, installed_cli, run=run)
        assert len(calls) == 1
    else:
        assert clients.cli_result(selected, installed_cli, run=run) == {"result_id": "selected-result"}
        assert len(calls) == 2


@pytest.mark.parametrize("change", ["pin", "writable", "symlink", "private_home", "unselected_helper"])
def test_untrusted_cli_selection_fails_before_any_execution(selected, installed_cli, change):
    path = Path(installed_cli["executable"]).with_name("adp_common.py")
    if change == "pin":
        path.write_text("changed helper")
    elif change == "writable":
        path.chmod(0o666)
    elif change == "symlink":
        target = path.with_name("target")
        path.rename(target)
        path.symlink_to(target)
    elif change == "unselected_helper":
        installed_cli["files_sha256"].pop("adp_common.py")
    else:
        Path(installed_cli["adp_home"]).chmod(0o755)
    with pytest.raises(ValueError):
        clients.cli_result(selected, installed_cli, run=lambda *args, **kwargs: pytest.fail("untrusted client must not execute"))


@pytest.mark.parametrize("changed", [None, "consent-scope", "revoked-consent", "late-consent-revoke"])
def test_retained_reuse_observes_existing_exact_owner_consent_without_issuing_it(selected, changed):
    selected["qualification_clients"]["retained_reuse"] = True
    connection, requests = transport(selected, changed=changed)
    if changed:
        with pytest.raises(ValueError):
            clients.collect(selected, connection, read_only=True)
    else:
        report = clients.collect(selected, connection, read_only=True)
        assert report["usage"]["retained_authorization"]["authorization"]["authorization_id"] == "explicit-owner-consent"
    assert all(method == "GET" for _, method, path, _ in requests if "reuse-" in path)
