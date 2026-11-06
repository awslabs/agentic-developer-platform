"""Well-Architected CLI uses shared Product-scoped requests and receipts."""

import importlib.util
import json
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "cli/adp-service.py"
spec = importlib.util.spec_from_file_location("well_architected_service_cli", CLI)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
SCOPE = ("--product", "example-product", "--project", "example-project", "--service", "well_architected", "--json")
BASE = "/v1/products/example-product/projects/example-project/services/well_architected"
PRODUCT_SCOPE = {"tenant_id": "example-tenant", "product_id": "example-product", "project_id": "example-project", "relationship_revision": "5"}
CONFIGURATION = {"scope": PRODUCT_SCOPE, "revision": 3, "hash": "config-hash", "settings": {"well_architected": {"pillars": ["SECURITY"]}}}
RUN = {"run_id": "run-one", "scope": PRODUCT_SCOPE, "revision": 1, "state": "admitted", "provider_state": "not_started"}
RESULT = {
    "result_id": "result-one",
    "run_id": "run-one",
    "scope": PRODUCT_SCOPE,
    "revision": 1,
    "service_id": "well_architected",
    "coverage": "partial",
    "limitations": ["source_only"],
}


class Client:
    def __init__(self):
        self.calls = []
        self.foreign = False

    def request(self, method, path, body=None, **options):
        self.calls.append((method, path, body, options))
        if path.endswith("/configuration/options"):
            return {"capabilities": ["architecture_review"]}
        if path.endswith("/configuration/check"):
            return {"ready": False, "gaps": ["binding_not_ready"]}
        if path.endswith("/configuration"):
            return CONFIGURATION
        if path.endswith("/binding-operations"):
            return {"operation_id": "setup-one", "state": "accepted", "action": body["action"]}
        if path.endswith("/bindings"):
            return {"items": [{"scope": PRODUCT_SCOPE, "binding_id": "binding-one"}]}
        if path.endswith("/runs") and method == "POST":
            return {**RUN, "scope": {**PRODUCT_SCOPE, "project_id": "foreign-project"} if self.foreign else PRODUCT_SCOPE}
        if path.endswith("/results/result-one/publication-status"):
            return {
                "result_id": "result-one",
                "revision": 1,
                "consumers": [
                    {
                        "destination": "product-context",
                        "state": "unavailable",
                        "projection_verified_on_ack": False,
                        "last_error": "consumer_schema_unadmitted",
                    }
                ],
            }
        if path.endswith("/runs/run-one"):
            return RUN
        if path.endswith("/results/result-one"):
            return RESULT
        if path.startswith(BASE + "/results?"):
            return {"items": [RESULT]}
        return {"items": []}


def call(client, *parts):
    return service.execute(service.parser().parse_args([*parts, *SCOPE]), client)


def test_scoped_configuration_setup_run_and_readback_keep_the_same_contract(tmp_path):
    client = Client()
    assert call(client, "options")["detail"]["capabilities"] == ["architecture_review"]
    assert call(client, "config", "show")["detail"] == CONFIGURATION
    assert call(client, "check")["detail"]["gaps"] == ["binding_not_ready"]
    choices = {
        "service_id": "well_architected",
        "capabilities": ["architecture_review"],
        "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]},
    }
    source = tmp_path / "choices.json"
    source.write_text(json.dumps(choices))
    assert (
        call(client, "config", "update", "--input", str(source), "--if-match", '"config-hash"', "--idempotency-key", "review-config-12345678")[
            "detail"
        ]
        == CONFIGURATION
    )
    assert client.calls[-1] == (
        "PUT",
        BASE + "/configuration",
        choices,
        {"extra_headers": {"Idempotency-Key": "review-config-12345678", "If-Match": '"config-hash"'}},
    )
    source.write_text(json.dumps({"action": "create", "configuration_revision": 3}))
    assert call(client, "binding", "create", "--input", str(source), "--idempotency-key", "review-setup-12345678")["detail"]["state"] == "accepted"
    assert client.calls[-1][1] == BASE + "/binding-operations"
    assert call(client, "binding", "list")["detail"]["items"][0]["scope"] == PRODUCT_SCOPE
    run_input = {
        "service_id": "well_architected",
        "configuration_revision": 3,
        "binding_id": "binding-one",
        "capability": "architecture_review",
        "sources": [],
    }
    source.write_text(json.dumps(run_input))
    receipt = call(client, "run", "--input", str(source), "--idempotency-key", "review-run-12345678")
    assert receipt["status"] == "accepted" and receipt["detail"] == RUN
    assert "not verified" in receipt["next_action"]
    assert client.calls[-1] == ("POST", BASE + "/runs", run_input, {"extra_headers": {"Idempotency-Key": "review-run-12345678"}})
    assert call(client, "status", "--run-id", "run-one")["detail"] == RUN
    assert call(client, "results")["detail"]["items"] == [RESULT]
    assert call(client, "result", "show", "--result-id", "result-one")["detail"] == RESULT


def test_mismatched_service_or_foreign_result_is_never_emitted(tmp_path, monkeypatch, capsys):
    client = Client()
    source = tmp_path / "run.json"
    source.write_text(json.dumps({"service_id": "security"}))
    with pytest.raises(service.common.CliError, match="must match --service"):
        call(client, "run", "--input", str(source), "--idempotency-key", "review-run-12345678")
    assert client.calls == []
    client.foreign = True
    source.write_text(json.dumps({"service_id": "well_architected"}))
    monkeypatch.setattr(service.common, "Api", lambda: client)
    assert service.main(["run", "--input", str(source), "--idempotency-key", "review-run-12345678", *SCOPE]) == 5
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == "invalid_response" and output["detail"] == {}
    assert "foreign-project" not in json.dumps(output)
    assert client.calls[-1][1] == BASE + "/runs"


def test_wait_keeps_unknown_outcome_separate_from_native_cancellation(monkeypatch, capsys):
    class UnknownClient(Client):
        def request(self, method, path, body=None, **options):
            self.calls.append((method, path, body, options))
            return {**RUN, "state": "outcome_unknown", "provider_state": "unknown"}

    client = UnknownClient()
    monkeypatch.setattr(service.common, "Api", lambda: client)
    assert service.main(["wait", "--run-id", "run-one", "--wait-seconds", "1", *SCOPE]) == 8
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "unknown" and response["error"]["code"] == "run_outcome_unknown"
    assert "cancelled" not in json.dumps(response)
    assert [entry[1] for entry in client.calls] == [BASE + "/runs/run-one"]

    assert [entry[1] for entry in client.calls] == [BASE + "/runs/run-one"]


@pytest.mark.parametrize("command", ["result", "results"])
def test_foreign_project_result_is_rejected_without_json_disclosure(command, monkeypatch, capsys):
    fixture = json.loads((CLI.parents[1] / "frontend/src/__tests__/fixtures/well-architected-client-parity.json").read_text())
    scope = ["--product", fixture["scope"]["product_id"], "--project", fixture["scope"]["project_id"], "--service", "well_architected", "--json"]
    foreign = {**fixture["result"], "scope": {**fixture["result"]["scope"], "project_id": fixture["foreign_project_id"]}}

    class ForeignClient:
        def request(self, method, path, body=None, **options):
            assert method == "GET" and "/projects/project-one/services/well_architected/results" in path
            return foreign if command == "result" else {"items": [foreign]}

    monkeypatch.setattr(service.common, "Api", ForeignClient)
    args = ["result", "show", "--result-id", fixture["result"]["result_id"]] if command == "result" else ["results"]
    assert service.main([*args, *scope]) == 5
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == "invalid_response" and output["detail"] == {}
    assert fixture["foreign_project_id"] not in json.dumps(output)
    assert fixture["result"]["result_id"] not in json.dumps(output)


def test_publication_status_reads_scoped_revision_and_keeps_publication_debt_in_json(monkeypatch, capsys):
    client = Client()
    monkeypatch.setattr(service.common, "Api", lambda: client)
    assert service.main(["result", "publication", "--result-id", "result-one", *SCOPE]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "ok"
    assert response["detail"]["consumers"] == [
        {"destination": "product-context", "state": "unavailable", "projection_verified_on_ack": False, "last_error": "consumer_schema_unadmitted"}
    ]
    assert [item[1] for item in client.calls] == [BASE + "/results/result-one", BASE + "/results/result-one/publication-status"]


@pytest.mark.parametrize("foreign,stale", [(True, False), (False, True)])
def test_publication_status_does_not_emit_foreign_or_stale_receipts(foreign, stale, monkeypatch, capsys):
    class MismatchedClient(Client):
        def request(self, method, path, body=None, **options):
            result = super().request(method, path, body, **options)
            if path.endswith("/results/result-one") and foreign:
                return {**result, "scope": {**PRODUCT_SCOPE, "project_id": "foreign-project"}}
            if path.endswith("/publication-status") and stale:
                return {**result, "revision": 2, "consumers": [{"receipt_id": "stale-receipt"}]}
            return result

    client = MismatchedClient()
    monkeypatch.setattr(service.common, "Api", lambda: client)
    assert service.main(["result", "publication", "--result-id", "result-one", *SCOPE]) == 5
    response = json.loads(capsys.readouterr().out)
    assert response["error"]["code"] == "invalid_response" and response["detail"] == {}
    assert "foreign-project" not in json.dumps(response) and "stale-receipt" not in json.dumps(response)
    assert len(client.calls) == (1 if foreign else 2)


def test_wait_local_expiry_with_unknown_provider_is_not_reported_as_native_cancellation(monkeypatch, capsys):
    class ExpiredClient(Client):
        def request(self, method, path, body=None, **options):
            return {
                **RUN,
                "state": "expired",
                "provider_state": "unknown",
                "monitoring": {"state": "recovery_pending", "last_poll_error": {"code": "cancel_unsupported"}},
            }

    monkeypatch.setattr(service.common, "Api", ExpiredClient)
    assert service.main(["wait", "--run-id", "run-one", "--wait-seconds", "1", *SCOPE]) == 8
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "unknown" and response["error"]["code"] == "run_outcome_unknown"
    assert response["detail"]["monitoring"]["last_poll_error"]["code"] == "cancel_unsupported"


def test_shared_state_fixture_preserves_cli_json_exit_and_read_only_comparison(monkeypatch, capsys, tmp_path):
    fixture = json.loads((CLI.parents[1] / "frontend/src/__tests__/fixtures/well-architected-client-parity.json").read_text())
    states = fixture["states"]
    run = {**RUN, **states["run"]}
    result = {**RESULT, **states["result"], "revision": states["publication"]["revision"]}

    class StateClient(Client):
        def request(self, method, path, body=None, **options):
            if path == BASE + "/runs/run-one":
                self.calls.append((method, path, body, options))
                return run
            if path == BASE + "/results/result-one":
                self.calls.append((method, path, body, options))
                return result
            if path == BASE + "/results/result-one/publication-status":
                self.calls.append((method, path, body, options))
                return states["publication"]
            if path == BASE + "/comparisons":
                self.calls.append((method, path, body, options))
                return states["comparison"]
            return super().request(method, path, body, **options)

    client = StateClient()
    monkeypatch.setattr(service.common, "Api", lambda: client)

    def invoke(*arguments):
        code = service.main([*arguments, *SCOPE])
        return code, json.loads(capsys.readouterr().out)

    code, status = invoke("status", "--run-id", "run-one")
    assert code == 0 and status["detail"] == run
    code, waited = invoke("wait", "--run-id", "run-one", "--wait-seconds", "1")
    assert code == 8 and waited["status"] == "unknown" and waited["detail"] == run
    assert waited["error"]["code"] == "run_outcome_unknown"
    code, found = invoke("result", "show", "--result-id", "result-one")
    assert code == 0 and found["detail"] == result
    code, publication = invoke("result", "publication", "--result-id", "result-one")
    assert code == 0 and publication["detail"] == states["publication"]
    compare_input = tmp_path / "comparison.json"
    compare_input.write_text(json.dumps({key: states["comparison"][key] for key in ("baseline_result_id", "candidate_result_id")}))
    code, compared = invoke("compare", "--input", str(compare_input))
    assert code == 0 and compared["detail"] == states["comparison"]
    assert all(method == "GET" or method == "POST" and path.endswith("/comparisons") for method, path, _, _ in client.calls)
    assert client.calls[-1][2] == json.loads(compare_input.read_text())


def test_local_cancellation_and_supported_source_evidence_do_not_claim_native_or_deployed_success(monkeypatch, capsys):
    fixture = json.loads((CLI.parents[1] / "frontend/src/__tests__/fixtures/well-architected-client-parity.json").read_text())
    run = {**RUN, **fixture["states"]["run"], "state": "cancelled", "provider_state": "running"}
    result = {**RESULT, **fixture["states"]["result"], "recommendations": [{"comparison": {"status": "supported"}}]}

    class UnconfirmedClient(Client):
        def request(self, method, path, body=None, **options):
            self.calls.append((method, path))
            return run if path == BASE + "/runs/run-one" else result

    client = UnconfirmedClient()
    monkeypatch.setattr(service.common, "Api", lambda: client)
    assert service.main(["wait", "--run-id", "run-one", "--wait-seconds", "1", *SCOPE]) == 6
    waited = json.loads(capsys.readouterr().out)
    assert waited["status"] == "failed" and waited["error"]["code"] == "run_cancelled"
    assert waited["detail"]["provider_state"] == "running"
    assert waited["detail"]["monitoring"]["last_poll_error"]["code"] == "cancel_unsupported"
    assert "AWS cancellation confirmed" not in json.dumps(waited)
    assert service.main(["result", "show", "--result-id", "result-one", *SCOPE]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["detail"]["review_mode"] == "source_only"
    assert found["detail"]["recommendations"][0]["comparison"]["status"] == "supported"
    assert "deployment_verified" not in json.dumps(found)
    assert "verification" not in found and "verification" not in found["detail"]
    assert client.calls == [("GET", BASE + "/runs/run-one"), ("GET", BASE + "/results/result-one")]
