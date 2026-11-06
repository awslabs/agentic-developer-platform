import json
import hashlib
from uuid import UUID

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.test_client_matrix import matrix_fixture
from tests.e2e.tools.test_interpreter_ac01 import Client, Observer
from tests.e2e.tools.test_runner import GATEWAY_IMAGE, IMAGE, SHA

EXECUTION_ID = "00000000-0000-4000-8000-000000000061"
POSITIVE = {"path": "/tmp/adp-8070-result.txt"}


class ProviderObserver(Observer):
    def __init__(self, *, wrong_readback=False):
        super().__init__()
        self.wrong_readback = wrong_readback

    def observe(self, *, execution_id, timeout):
        assert execution_id == EXECUTION_ID and timeout == 600
        return {"source": "provider-control-plane", "execution_id": execution_id,
                "status": "TERMINATED", "file_sha256": "0" * 64 if self.wrong_readback else
                hashlib.sha256(b"42\n").hexdigest(),
                "stdout": "42\n", "exitCode": 0, "session_count": 1,
                "session_ttl_seconds": 300, "cleanup_elapsed_seconds": 60,
                "provider_request_count": 5, "cost_usd": 0.2}


class InstalledClient(Client):
    def __init__(self, observer, name):
        super().__init__(observer)
        self.name = name
        self.version = "pinned-" + name

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def request(self, method, params=None):
        if method == "tools/list":
            names = ("code_interpreter___start", "code_interpreter___execute",
                     "code_interpreter___result", "code_interpreter___file",
                     "code_interpreter___close", "adp___artifact_read")
            return {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in names]}
        response = super().request(method, params)
        receipt = json.loads(response["content"][0]["text"])
        if "operation_ref" in receipt:
            receipt["operation_ref"] = str(UUID(int=len(self.calls) + 60))
        if "execution_id" in receipt:
            receipt["execution_id"] = EXECUTION_ID
        response["content"][0]["text"] = json.dumps(receipt)
        return response


def fixture(name):
    pricing = matrix_fixture(name)
    pricing.update({"arguments": POSITIVE, "setup": [], "max_seconds": 300,
                    "cleanup_seconds": 600, "session_ttl_seconds": 300,
                    "max_cost_usd": 5, "unit_cost_usd": 0.01, "expected_provider_calls": 7})
    if name != "cli":
        pricing["all_in_quote"]["max_model_requests"] = 7
    return {"positive": POSITIVE, "client_version": "pinned-" + name, "pricing": pricing}


def test_interpreter_ac04_local_matrix_proves_every_client_cell(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    observer = ProviderObserver()
    matrix = {name: fixture(name) for name in runner.CLIENTS}
    created = []

    def factory(name, selected):
        assert selected == matrix[name]
        created.append(name)
        return InstalledClient(observer, name)

    result = runner.evaluate("SCN-8070-04", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], provider_observer=observer,
                             pinned_client_factory=factory, client_fixture={"interpreter_matrix": matrix})
    assert result and created == list(runner.CLIENTS)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert report["live_attestation"] is False
    assert [(row["client"], row["status"]) for row in report["scenarios"]] == [
        (name, "pass") for name in runner.CLIENTS]
    assert all(row["cleanup"] == {"status": "confirmed", "provider_termination": "TERMINATED"}
               and row["observations"]["result"]["file_sha256"] ==
               row["observations"]["provider_observation"]["file_sha256"]
               for row in report["scenarios"])


def test_interpreter_ac04_blocks_unpriced_or_unobserved_cells_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    matrix = {name: fixture(name) for name in runner.CLIENTS}
    matrix["cli"]["pricing"]["authority_confirmed"] = False
    matrix["claude-code"]["pricing"]["session_ttl_seconds"] = 301
    calls = []
    observer = ProviderObserver()

    def factory(name, selected):
        calls.append(name)
        return InstalledClient(observer, name)

    assert not runner.evaluate("SCN-8070-04", tmp_path, "https://example.invalid", SHA,
                               GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], provider_observer=observer,
                               pinned_client_factory=factory, client_fixture={"interpreter_matrix": matrix})
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert calls == ["codex"]
    assert [row["status"] for row in report["scenarios"]] == ["not-run", "not-run", "pass"]
    assert all("fixture unavailable" in row["reason"] for row in report["scenarios"][:2])


def test_interpreter_ac04_run_wide_cost_reservation_prevents_second_session(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    observer = ProviderObserver()
    matrix = {name: fixture(name) for name in runner.CLIENTS}
    for selected in matrix.values():
        selected["pricing"]["unit_cost_usd"] = 2.5
        selected["pricing"]["max_cost_usd"] = 18
    launched = []

    def factory(name, selected):
        launched.append(name)
        return InstalledClient(observer, name)

    assert not runner.evaluate("SCN-8070-04", tmp_path, "https://example.invalid", SHA,
                               GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], provider_observer=observer,
                               pinned_client_factory=factory, client_fixture={"interpreter_matrix": matrix})
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert launched == ["cli"]
    assert [row["status"] for row in rows] == ["pass", "blocked", "blocked"]
    assert all(row["reason"] == "run-wide $20 cost bound exceeded" for row in rows[1:])


def test_interpreter_ac04_rejects_foreign_readback_after_owned_cleanup():
    observer = ProviderObserver(wrong_readback=True)
    with pytest.raises(ValueError, match="readback differs"):
        runner.interpreter_ac04("codex", fixture("codex"), InstalledClient(observer, "codex"), observer)
    assert observer.status == "TERMINATED"


def test_interpreter_ac04_requires_private_pricing_and_pinned_adapters(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    assert not runner.evaluate("SCN-8070-04", tmp_path, "https://example.invalid", SHA,
                               GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                               pinned_client_factory=lambda *_: pytest.fail("client launched without fixtures"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert len(report["scenarios"]) == 3 and not report["live_attestation"]
    assert all(row["status"] == "not-run" for row in report["scenarios"])
    assert runner.select_scenarios("SCN-8070-04")[0].action == "interpreter_ac04"


def test_protected_entrypoint_uses_only_designated_private_interpreter_bindings(monkeypatch):
    import sys
    from types import ModuleType

    name = "tests.e2e.tools.private_interpreter_sample"
    module = ModuleType(name)
    module.client_fixture = {"interpreter_matrix": {}}
    module.provider_observer = object()
    module.pinned_client_factory = lambda *_: None
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("ADP_TOOLS_INTERPRETER_FIXTURE_MODULE", name)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    captured = []
    monkeypatch.setattr(runner, "evaluate", lambda *args, **kwargs: captured.append(kwargs) or False)
    arguments = ["--output", "/tmp/interpreter-evidence", "--gateway-url", "https://example.invalid",
                 "--gateway-revision", SHA, "--gateway-image", GATEWAY_IMAGE,
                 "--facade-image", IMAGE]
    assert runner.main(["--scenario-filter", "SCN-8070-04", *arguments]) == 1
    assert (captured[-1]["client_fixture"], captured[-1]["provider_observer"],
            captured[-1]["pinned_client_factory"]) == (
                module.client_fixture, module.provider_observer, module.pinned_client_factory)
    assert runner.main(["--scenario-filter", "SCN-8070-01", *arguments]) == 1
    assert captured[-1]["client_fixture"] is captured[-1]["provider_observer"] is None
    monkeypatch.setenv("ADP_TOOLS_INTERPRETER_FIXTURE_MODULE", "other.module")
    assert runner.private_interpreter_bindings() == (None, None, None)
