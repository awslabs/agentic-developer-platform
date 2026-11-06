"""Installed Product service CLI uses scoped Transform routes and independent result evidence."""

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

CLI = Path(__file__).resolve().parents[2] / "cli/adp-service.py"
spec = importlib.util.spec_from_file_location("transform_service_cli", CLI)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
SCOPE = ("--product", "example-product", "--project", "example-project", "--service", "transform", "--json")
BASE = "/v1/products/example-product/projects/example-project/services/transform"
RUN = {
    "run_id": "run-one",
    "revision": 2,
    "scope": {"tenant_id": "example-tenant", "product_id": "example-product", "project_id": "example-project", "relationship_revision": "5"},
    "state": "succeeded",
    "provider_state": "completed",
}
RESULT = {
    "service_id": "transform",
    "result_id": "result-one",
    "run_id": "run-one",
    "scope": RUN["scope"],
    "revision": 1,
    "verification_status": "succeeded",
    "scope_status": "passed",
    "tests_before": [{"status": "passed"}],
    "tests_after": [{"status": "passed"}],
    "limitations": [],
    "cost_status": "unknown",
}


class Client:
    def __init__(self):
        self.calls = []
        self.run = RUN.copy()
        self.result = RESULT.copy()

    def request(self, method, path, body=None, **options):
        self.calls.append((method, path, body, options))
        if path.endswith("/runs/run-one"):
            return self.run
        if path.endswith("/results?limit=100"):
            return {"items": [{"run_id": "run-one", "result_id": "result-one"}]}
        if path.endswith("/results/result-one"):
            return self.result
        if path.endswith("/runs") and method == "POST":
            return {**self.run, "state": "admitted", "provider_state": "not_started"}
        return {"items": []}


def call(client, *parts):
    return service.execute(service.parser().parse_args([*parts, *SCOPE]), client)


def test_transform_cli_scoped_admission_is_accepted_not_verified(tmp_path):
    client = Client()
    source = tmp_path / "run.json"
    input_body = {
        "service_id": "transform",
        "configuration_revision": 3,
        "binding_id": "binding-one",
        "capability": "refactor",
        "sources": [],
        "transform_input": {"goal": {"kind": "refactor", "summary": "Refactor"}},
    }
    source.write_text(json.dumps(input_body))
    receipt = call(client, "run", "--input", str(source), "--idempotency-key", "transform-run-12345678")
    assert receipt["status"] == "accepted"
    assert receipt["detail"]["state"] == "admitted"
    assert "not verified" in receipt["next_action"]
    assert client.calls == [("POST", BASE + "/runs", input_body, {"extra_headers": {"Idempotency-Key": "transform-run-12345678"}})]
    assert call(client, "results")["detail"] == {"items": []}
    assert client.calls[-1][1] == BASE + "/results?limit=25"
    input_body["service_id"] = "security"
    source.write_text(json.dumps(input_body))
    with pytest.raises(service.common.CliError, match="must match --service"):
        call(client, "run", "--input", str(source), "--idempotency-key", "transform-run-12345678")
    assert len([item for item in client.calls if item[0] == "POST"]) == 1


@pytest.mark.parametrize(
    ("state", "verification", "status", "error"),
    [
        ("succeeded", "succeeded", "ok", None),
        ("succeeded", "partial", "failed", "verification_failed"),
        ("succeeded", "unknown", "unknown", "verification_unknown"),
        ("outcome_unknown", "succeeded", "unknown", "run_outcome_unknown"),
        ("partial", "succeeded", "failed", "run_partial"),
    ],
)
def test_transform_wait_uses_independent_result_not_provider_prose(state, verification, status, error):
    client = Client()
    client.run = {**RUN, "state": state, "provider_state": "unknown" if state == "outcome_unknown" else "completed"}
    client.result = {
        **RESULT,
        "verification_status": verification,
        "tests_after": [{"status": "failed"}] if verification == "partial" else RESULT["tests_after"],
    }
    response = call(client, "wait", "--run-id", "run-one", "--wait-seconds", "1")
    assert response["status"] == status
    assert response["detail"] == client.run
    if error:
        assert response["error"]["code"] == error
    if state == "succeeded":
        assert response["verification"] == client.result
        assert [item[1] for item in client.calls] == [BASE + "/runs/run-one", BASE + "/results?limit=100", BASE + "/results/result-one"]
    else:
        assert [item[1] for item in client.calls] == [BASE + "/runs/run-one"]
    assert all(item[0] == "GET" for item in client.calls)


def test_transform_wait_uses_distinct_exit_codes(monkeypatch, capsys):
    client = Client()
    monkeypatch.setattr(service.common, "Api", lambda: client)
    for state, expected in (("succeeded", 0), ("outcome_unknown", 8), ("failed", 6)):
        client.run = {**RUN, "state": state, "provider_state": "unknown" if state == "outcome_unknown" else "completed"}
        assert service.main(["wait", "--run-id", "run-one", "--wait-seconds", "1", *SCOPE]) == expected
        assert json.loads(capsys.readouterr().out)["detail"]["state"] == state


def test_transform_wait_finds_result_after_three_full_pages():
    class PaginatedClient(Client):
        def request(self, method, path, body=None, **kwargs):
            if path.startswith(BASE + "/results?"):
                self.calls.append((method, path, body, kwargs))
                offset = int(parse_qs(urlsplit(path).query).get("cursor", ["0"])[0])
                if offset < 300:
                    return {
                        "items": [{"run_id": f"older-{index}", "result_id": f"result-{index}"} for index in range(offset, offset + 100)],
                        "next_cursor": str(offset + 100),
                    }
                return {"items": [RESULT], "next_cursor": None}
            return super().request(method, path, body, **kwargs)

    client = PaginatedClient()
    response = call(client, "wait", "--run-id", "run-one", "--wait-seconds", "1")
    assert response["status"] == "ok" and response["verification"] == RESULT
    assert [item[1] for item in client.calls] == [
        BASE + "/runs/run-one",
        BASE + "/results?limit=100",
        *[BASE + f"/results?limit=100&cursor={offset}" for offset in (100, 200, 300)],
        BASE + "/results/result-one",
    ]


@pytest.mark.parametrize("target_on_last_page", [False, True])
def test_transform_result_paging_obeys_remaining_deadline(monkeypatch, target_on_last_page):
    elapsed = [0.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: elapsed[0])

    class SlowPages(Client):
        def request(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body, kwargs))
            elapsed[0] += 1
            return {"items": [RESULT] if target_on_last_page and elapsed[0] == 2 else [], "next_cursor": "next"}

    client = SlowPages()
    response = service.transform_verification(service.parser().parse_args(["wait", "--run-id", "run-one", "--wait-seconds", "2", *SCOPE]), client, 2)
    assert response is None
    assert [request[3]["timeout"] for request in client.calls] == [2, 1]
    assert all("/results?" in request[1] for request in client.calls)


def test_transform_wait_rejects_foreign_result_and_never_reports_passed():
    client = Client()
    client.result = {**RESULT, "scope": {**RUN["scope"], "tenant_id": "other-tenant"}}
    with pytest.raises(service.common.CliError, match="mismatched scoped result"):
        call(client, "wait", "--run-id", "run-one", "--wait-seconds", "1")
    assert all(item[0] == "GET" for item in client.calls)


def test_transform_wait_timeout_keeps_receipt_without_restarting(monkeypatch, capsys):
    client = Client()
    client.run = {**RUN, "state": "running"}
    monkeypatch.setattr(service.common, "Api", lambda: client)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    assert service.main(["wait", "--run-id", "run-one", "--wait-seconds", "1", *SCOPE]) == 7
    assert json.loads(capsys.readouterr().out)["status"] == "pending"
    assert all(item[0] == "GET" for item in client.calls)


def test_transform_scm_handoff_is_visibly_unavailable_and_has_no_transport_effect():
    client = Client()
    for action in (
        ("publication", "show", "--publication-id", "result-one"),
        ("publication", "retry", "--publication-id", "result-one", "--input", "not-read.json", "--idempotency-key", "publish-retry-key-001"),
    ):
        with pytest.raises(service.common.CliError, match="no separately authorized SCM handoff") as error:
            call(client, *action)
        assert error.value.code == "capability_unavailable" and error.value.exit_code == 5
    assert client.calls == []
