import json
import os
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.credential_integration import broker_ac01
from tests.e2e.tools.process_bounds import ProcessLimitError

SHA = "a" * 40
IMAGE = "example.invalid/adp-tools-facade@sha256:" + "b" * 64
GATEWAY_IMAGE = "example.invalid/adp-gateway@sha256:" + "c" * 64


def test_broker_ac01_two_isolated_owners_and_private_outputs():
    class Observer:
        calls = {"owner-a": 0, "owner-b": 0}

        def expected(self, fixture_id, attempt_id):
            return {"source": "independent-broker-fixture", "credential_id": fixture_id,
                    "sentinel": "private-" + fixture_id, "result": "safe-" + fixture_id,
                    "url": "https://fixture.example.invalid/" + fixture_id}

        def provider_calls(self, fixture_id, attempt_id):
            return self.calls[fixture_id]

        def generic_evidence(self, attempt_id):
            return {"receipt": "metadata-only"}

        def cleaned(self, attempt_id):
            return True

    observer = Observer()

    class Driver:
        isolated = True

        def discover(self, attempt_id):
            return [{"name": "credentials___" + operation, "inputSchema": {"properties": {"service": {"type": "string"}}}}
                    for operation in ("list", "proxy_request", "assume_role", "materialize")]

        def invoke(self, fixture_id, operation, arguments, attempt_id):
            if operation == "list":
                return [{"id": fixture_id, "service": "fixture", "label": "owned", "credential_type": "bearer"}]
            assert arguments["url"].endswith(fixture_id)
            observer.calls[fixture_id] += 1
            return {"status": 200, "body": "safe-" + fixture_id}

        def cleanup(self, attempt_id):
            pass

    assert broker_ac01(Driver(), observer)["status"] == "pass"
    assert observer.calls == {"owner-a": 1, "owner-b": 1}

    class LeakingDriver(Driver):
        def invoke(self, fixture_id, operation, arguments, attempt_id):
            response = super().invoke(fixture_id, operation, arguments, attempt_id)
            return {"status": 200, "body": "private-" + fixture_id} if operation == "proxy_request" else response

    assert broker_ac01(LeakingDriver(), Observer())["status"] == "fail"


def test_broker_selector_requires_private_fixture(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-8073-01")
    assert not success
    assert report["scenarios"][0]["status"] == "blocked"
    assert report["scenarios"][0]["evidence_scope"] == "repository-integration"


class FixtureClient:
    version = "1.0.0"

    def __init__(self, command):
        self.command = command

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request(self, method):
        assert method == "tools/list"
        return {"tools": [{"name": "websearch___search"}]}


def evaluate(tmp_path, monkeypatch, *, cases=runner.SCENARIOS, pattern="SCN-TLS-0017"):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    success = runner.evaluate(pattern, tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              scenarios=cases, client_factory=FixtureClient, command=["adp", "mcp"])
    return success, json.loads((tmp_path / "evidence.json").read_text())


def test_filter_publishes_per_scenario_tags_and_junit(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch)
    assert not success
    assert report["live_attestation"] is False
    assert report["evidence_schema"] == "workflow-evaluation/v1"
    assert report["deployed_revisions"] == {"gateway": SHA, "gateway_image": GATEWAY_IMAGE,
                                            "facade_image": IMAGE}
    assert report["scenarios"][0]["tags"] == ["@scenario SCN-TLS-0017", "@story TLS-C06"]
    assert report["scenarios"][0]["status"] == "blocked"
    assert report["scenarios"][0]["observations"]["tools_list_sha256"]
    suite = ElementTree.parse(tmp_path / "junit.xml").getroot()
    assert suite.attrib["tests"] == "1" and suite.attrib["failures"] == "1"


def test_missing_filter_rejected(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-TLS-9999")
    assert not success and report["error"] == "ValueError"
    suite = ElementTree.parse(tmp_path / "junit.xml").getroot()
    assert suite.attrib["failures"] == "1"


def test_skipped_scenario_remains_visible_and_fails(tmp_path, monkeypatch):
    cases = (runner.Scenario("SCN-TLS-0099", ("TLS-C06",), "missing_fixture"),)
    success, report = evaluate(tmp_path, monkeypatch, cases=cases, pattern="SCN-TLS-0099")
    assert success is False
    assert report["scenarios"][0]["status"] == "not-run"
    assert ElementTree.parse(tmp_path / "junit.xml").getroot().attrib["failures"] == "1"


def test_unverifiable_gateway_publishes_failure_not_a_false_pass(tmp_path, monkeypatch):
    def unavailable(_):
        raise ValueError("revision unverified")

    monkeypatch.setattr(runner, "verify_gateway", unavailable)
    assert not runner.evaluate("SCN-TLS-00*", tmp_path, "https://example.invalid", SHA,
                               GATEWAY_IMAGE, IMAGE, client_factory=FixtureClient, command=["adp", "mcp"])
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert report["error"] == "ValueError" and report["scenarios"][0]["status"] == "not-run"


def test_incomplete_image_binding_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="verified gateway"):
        runner.evaluate("SCN-TLS-00*", tmp_path, "https://example.invalid", SHA,
                        "latest", IMAGE, client_factory=FixtureClient, command=["adp", "mcp"])


def client_release_window():
    from tests.e2e.tools.test_release_window import fixture

    release = fixture()
    digest = GATEWAY_IMAGE.rsplit("@", 1)[-1]
    gateway = release["components"]["gateway"]
    gateway["expected_runtime"]["image"] = digest
    for field in ("component_digests", "runtime_digests"):
        gateway["receipt"][field]["image"] = digest
    for phase in ("before", "after"):
        release["readbacks"][phase]["gateway"]["image"] = digest
    return release


def test_client_install_missing_release_receipts_cannot_launch_or_attest(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setattr(runner, "harness_ac01", lambda *args, **kwargs: pytest.fail("unverified client launch"))
    passed = runner.evaluate("SCN-8067-01", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=[], live=True,
                             client_refresh_file="/protected/fixture")
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not passed and report["live_attestation"] is False
    assert len(report["scenarios"]) == 3
    assert {row["status"] for row in report["scenarios"]} == {"blocked"}
    assert all("release receipts" in row["reason"] for row in report["scenarios"])


def test_client_runtime_window_rejects_wrong_image_before_launch():
    release = client_release_window()
    gateway = release["components"]["gateway"]
    gateway["expected_runtime"]["image"] = "sha256:" + "f" * 64
    gateway["receipt"]["component_digests"]["image"] = "sha256:" + "f" * 64
    gateway["receipt"]["runtime_digests"]["image"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="pinned image"):
        with runner.client_runtime_window(release, lambda phase: pytest.fail("unverified runtime read"),
                                          source=SHA, account=release["account"],
                                          gateway_image=GATEWAY_IMAGE, facade_image=IMAGE):
            pytest.fail("wrong image served a client")


@pytest.mark.parametrize("phase", ("before", "after"))
def test_client_install_changed_runtime_digest_cannot_attest(tmp_path, monkeypatch, phase):
    release = client_release_window()
    release["readbacks"][phase]["gateway"]["image"] = "sha256:" + "f" * 64
    calls = []
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)

    def installed(*args, **kwargs):
        calls.append(kwargs["gateway_revision"])
        return {"source": "synthetic-installed-client", "client_version": "1.0.0"}

    monkeypatch.setattr(runner, "harness_ac01", installed)
    passed = runner.evaluate("SCN-8067-01", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=[], live=True, client_refresh_file="/protected/fixture",
                             release_manifest=release, release_observer=lambda phase: release["readbacks"][phase],
                             release_source_revision=SHA, release_account=release["account"])
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not passed and report["live_attestation"] is False
    assert len(calls) == (0 if phase == "before" else 3)
    assert {row["status"] for row in report["scenarios"]} == {"fail"}
    assert all(row["reason"] == "ValueError" for row in report["scenarios"])


def test_client_runtime_receipts_bind_both_sides_of_invocation():
    release = client_release_window()
    events = []

    def readback(phase):
        events.append(phase)
        return release["readbacks"][phase]

    with runner.client_runtime_window(release, readback, source=SHA, account=release["account"],
                                      gateway_image=GATEWAY_IMAGE, facade_image=IMAGE) as window:
        events.append("client")
        assert window["receipts"]["gateway"]["run_attempt"] == 2
        assert window["source_revision"] == SHA
    assert events == ["before", "client", "after"]


def test_workflow_requires_protected_identity_and_verified_images():
    from pathlib import Path

    import yaml

    workflow = yaml.load(
        (Path(__file__).resolve().parents[3] / ".github/workflows/tools-live-evaluation.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert list(workflow["on"]) == ["workflow_dispatch"]
    job = workflow["jobs"]["evaluate"]
    assert job["runs-on"] == "arc-runner-org"
    assert job["environment"] == "adp-tools-deploy-dev"
    steps = job["steps"]
    assert any(step.get("uses") == "./.github/actions/trusted-deployment" for step in steps)
    source = "\n".join(step.get("run", "") for step in steps)
    assert 'imageTag=$revision' in source and '"$digest" == "$expected"' in source
    assert 'secrets.TLS_EVAL_REFRESH_TOKEN' in str(steps)
    assert "bg-cognito-auth.sh import" in source and "tests.e2e.tools.runner" in source
    upload = next(step for step in steps if step.get("uses") == "./.github/actions/upload-ci-evidence-s3")
    assert upload["if"] == "always()"
    assert upload["with"] == {
        "name": "tools-live-evaluation-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "${{ runner.temp }}/tools-eval-evidence/",
        "if-no-files-found": "error",
        "retention-days": "14",
    }
    preserve = next(step for step in steps if "CI_EVIDENCE_RUNNER_ROLE_ARN=" in step.get("run", ""))
    assume = next(step for step in steps if step.get("uses") == "./.github/actions/trusted-deployment")
    assert steps.index(preserve) < steps.index(assume) < steps.index(upload)
    assert 'test -n "$AWS_ROLE_ARN"' in preserve["run"]
    assert 'test -f "$AWS_WEB_IDENTITY_TOKEN_FILE"' in preserve["run"]
    assert 'CI_EVIDENCE_RUNNER_TOKEN_FILE=%s' in preserve["run"]


def test_lint_accepts_registered_tags():
    runner.validate_scenarios(runner.SCENARIOS)
    assert runner.main(["--lint"]) == 0


def test_search_ac01_qualified_selector_checks_citations_receipt_and_provider_calls(tmp_path, monkeypatch):
    class SearchObserver:
        calls = 0

        def provider_calls(self):
            return self.calls

    class SearchClient:
        version = "fixture-cli"

        def __init__(self, command):
            self.observer = observer

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, params=None):
            if method == "tools/list":
                return {"tools": [{"name": "websearch___search", "inputSchema": {
                    "required": ["query"], "properties": {"query": {}},
                    "additionalProperties": False}}]}
            assert method == "tools/call" and params == {"name": "websearch___search",
                "arguments": {"query": "example"}}
            self.observer.calls += 1
            return {"content": [{"type": "text", "text": json.dumps({
                "status": "confirmed", "receipt_id": "receipt-one", "result": {"result": {
                    "result": {"result": {"status": "completed", "query_count": 1,
                        "results": [{"url": "https://example.com", "text": "sample"}],
                        "citations": ["https://example.com"], "estimated_search_usd": 0.007,
                        "pricing_source": "https://aws.amazon.com/bedrock/agentcore/pricing/"}}}}})}]}

    observer = SearchObserver()
    fixture = {"query": "example", "max_results": 1, "expected_urls": ["https://example.com"],
               "unit_cost_usd": 0.007,
               "pricing_source": "https://aws.amazon.com/bedrock/agentcore/pricing/"}
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    assert runner.evaluate("SCN-8069-01", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                           command=["adp", "mcp"], client_factory=SearchClient,
                           integration_observer=observer, contract_fixture=fixture)
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["tags"] == ["@scenario SCN-8069-01", "@story 8069", "@acceptance AC-01"]
    assert row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["observations"]["observed_provider_calls"] == 1
    with pytest.raises(ValueError, match="search results, citations"):
        runner.search_ac01(SearchClient(["adp", "mcp"]),
                           {**fixture, "expected_urls": ["https://different.example.com"]}, observer)


def test_search_ac02_qualified_selector_rejects_unauthorized_and_duplicate_dispatch(tmp_path, monkeypatch):
    class Observer:
        calls = 0

        def provider_calls(self):
            return self.calls

    class Driver:
        def __init__(self, observer):
            self.observer = observer
            self.original = {"status": 202, "request_id": "00000000-0000-4000-8000-000000000004",
                             "retry_after_seconds": 12}

        def denied(self):
            return {"status": 403}

        def malformed(self):
            return {"status": 422}

        def throttled(self):
            self.observer.calls += 1
            return self.original

        def retry(self, request_id):
            assert request_id == self.original["request_id"]
            return self.original

        def changed_payload(self, request_id):
            assert request_id == self.original["request_id"]
            return {"status": 409}

    observer = Observer()
    driver = Driver(observer)
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    assert runner.evaluate("SCN-8069-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                           command=["adp", "mcp"], client_factory=FixtureClient,
                           integration_observer=observer,
                           integration_driver_factory=lambda case, client, tool: driver)
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "pass" and row["tags"] == [
        "@scenario SCN-8069-02", "@story 8069", "@acceptance AC-02"]
    assert row["observations"]["observed_provider_calls"] == observer.calls == 1
    observer.calls = 0
    driver.denied = lambda: (setattr(observer, "calls", 1) or {"status": 403})
    with pytest.raises(ValueError, match="unauthorized or malformed"):
        runner.search_ac02(driver, observer)


def test_search_ac04_three_client_matrix_checks_receipts_errors_costs_and_cleanup(tmp_path, monkeypatch):
    from tests.e2e.tools.test_client_matrix import matrix_fixture

    reference = "00000000-0000-4000-8000-000000000004"
    execution = "00000000-0000-4000-8000-000000000005"
    positive = {"query": "test", "max_results": 1, "expected_urls": ["https://example.com"],
                "unit_cost_usd": 0.007, "pricing_source": "protected price sheet"}

    class Observer:
        def __init__(self):
            self.counts = {name: 0 for name in runner.CLIENTS}

        def provider_calls(self):
            return sum(self.counts.values())

        def observe(self, **kwargs):
            name = kwargs["client"]
            assert kwargs["operation_ref"] == reference and kwargs["execution_id"] == execution
            assert self.counts[name] == 2
            return {"source": "provider-control-plane", "operation_ref": reference,
                    "execution_id": execution, "receipt_id": "receipt-" + name,
                    "operation": "websearch___search", "assertion": {
                        "source": "provider-control-plane", "operation": "websearch___search", "passed": True},
                    "provider_request_count": 2, "cleanup_status": "confirmed",
                    "cleanup_observed_at": "2026-01-01T00:00:00Z", "session_count": 0,
                    "session_ttl_seconds": 0, "cleanup_elapsed_seconds": 10, "cost_usd": 0.014}

    class Adapter:
        def __init__(self, name, observer):
            self.name, self.observer = name, observer
            self.version = "fixture-" + name
            self.calls = []
            self.throttle = {"status": 202, "request_id": "00000000-0000-4000-8000-000000000006",
                             "retry_after_seconds": 12}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, params=None):
            if method == "tools/list":
                return {"tools": [{"name": "websearch___search", "inputSchema": {
                    "properties": {"query": {"type": "string"}}, "required": ["query"],
                    "additionalProperties": False}}]}
            assert (method, params) == ("tools/call", {"name": "websearch___search",
                                                      "arguments": {"query": "test"}})
            self.observer.counts[self.name] += 1
            result = {"status": "completed", "query_count": 1, "results": [{"url": "https://example.com"}],
                      "citations": ["https://example.com"], "estimated_search_usd": 0.007,
                      "pricing_source": "protected price sheet"}
            receipt = {"status": "confirmed", "operation_ref": reference, "execution_id": execution,
                       "receipt_id": "receipt-" + self.name,
                       "result": {"result": {"result": {"result": result}}}}
            return {"content": [{"type": "text", "text": json.dumps(receipt)}]}

        def denied(self):
            self.calls.append("denied")
            return {"status": 403}

        def malformed(self):
            self.calls.append("malformed")
            return {"status": 422}

        def throttled(self):
            self.calls.append("throttled")
            self.observer.counts[self.name] += 1
            return self.throttle

        def retry(self, request_id):
            self.calls.append("retry")
            assert request_id == self.throttle["request_id"]
            return self.throttle

        def changed_payload(self, request_id):
            self.calls.append("changed_payload")
            assert request_id == self.throttle["request_id"]
            return {"status": 409}

        def close_original(self, operation_ref):
            self.calls.append("close_original")
            assert operation_ref == reference
            return {"cleanup_status": "confirmed"}

    observer = Observer()
    adapters = {}
    fixtures = {}
    for name in runner.CLIENTS:
        pricing = matrix_fixture(name)
        pricing.update(unit_cost_usd=0.007, expected_provider_calls=2)
        if name != "cli":
            pricing["all_in_quote"]["max_model_requests"] = 7
        fixtures[name] = {"positive": positive, "pricing": pricing, "client_version": "fixture-" + name}

    def factory(name, fixture):
        adapters[name] = Adapter(name, observer)
        return adapters[name]

    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    assert runner.evaluate("SCN-8069-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                           command=["adp", "mcp"], client_factory=FixtureClient,
                           client_fixture={"search_matrix": fixtures}, pinned_client_factory=factory,
                           provider_observer=observer)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert [row["cell_id"] for row in rows] == [name + ":search" for name in runner.CLIENTS]
    assert all(row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
               and row["client_version"] == "fixture-" + row["client"]
               and row["observations"]["positive"]["receipt_id"] == "receipt-" + row["client"]
               and row["observations"]["positive"]["query_count"] == 1
               and row["observations"]["negative"]["changed_payload"] == 409
               and row["observations"]["cleanup"]["status"] == "confirmed"
               and row["observations"]["provider_observation"]["cost_usd"] == 0.014
               and row["observations"]["pricing_source_revision"] == "a" * 40 for row in rows)
    assert all(adapter.calls == ["denied", "malformed", "throttled", "retry", "changed_payload", "close_original"]
               for adapter in adapters.values())
    assert observer.provider_calls() == 6
    fixtures["codex"]["positive"] = {**positive, "query": "different"}
    assert not runner.evaluate("SCN-8069-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                               command=["adp", "mcp"], client_factory=FixtureClient,
                               client_fixture={"search_matrix": fixtures}, pinned_client_factory=factory,
                               provider_observer=observer)
    assert observer.provider_calls() == 6
    fixtures["codex"]["positive"] = positive
    monkeypatch.setattr(Adapter, "changed_payload", lambda self, request_id: {"status": 200})
    assert not runner.evaluate("SCN-8069-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                               command=["adp", "mcp"], client_factory=FixtureClient,
                               client_fixture={"search_matrix": fixtures}, pinned_client_factory=factory,
                               provider_observer=observer)
    assert observer.provider_calls() == 12
    assert all(adapter.calls[-1] == "close_original" for adapter in adapters.values())


def test_search_ac04_live_mode_never_launches_without_paid_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    success = runner.evaluate("SCN-8069-04", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                              client_factory=lambda _: pytest.fail("unadmitted MCP client launch"),
                              pinned_client_factory=lambda *_: pytest.fail("unadmitted client launch"),
                              provider_observer=object(), live=True)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and len(rows) == 3
    assert all(row["status"] == "blocked" and row["cleanup"]["status"] == "not-run"
               and row["evidence_scope"] == "deployed-target" for row in rows)


def test_search_ac04_rejects_unpriced_matrix_before_client_launch(tmp_path, monkeypatch):
    from tests.e2e.tools.test_client_matrix import matrix_fixture

    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    pricing = matrix_fixture()
    pricing.update(unit_cost_usd=0.007, expected_provider_calls=2, authority_confirmed=False)
    positive = {"query": "test", "max_results": 1, "expected_urls": ["https://example.com"],
                "unit_cost_usd": 0.007, "pricing_source": "protected price sheet"}
    fixtures = {name: {"positive": positive, "pricing": pricing, "client_version": "fixture-" + name}
                for name in runner.CLIENTS}
    assert not runner.evaluate("SCN-8069-04", tmp_path, "https://example.invalid", SHA,
                               GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=FixtureClient,
                               client_fixture={"search_matrix": fixtures}, provider_observer=object(),
                               pinned_client_factory=lambda *_: pytest.fail("unpriced client launch"))
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert all(row["status"] != "pass" for row in rows)


def test_lint_rejects_missing_story_even_when_filter_excludes_it(tmp_path, monkeypatch, capsys):
    missing = runner.Scenario("SCN-TLS-0099", (), "list_tools")
    cases = runner.SCENARIOS + (missing,)
    with pytest.raises(ValueError, match="story tag is required"):
        runner.validate_scenarios(cases)
    success, report = evaluate(tmp_path, monkeypatch, cases=cases, pattern="SCN-TLS-0017")
    assert not success and report["error"] == "ValueError"
    assert not report["scenarios"]
    monkeypatch.setattr(runner, "SCENARIOS", cases)
    assert runner.main(["--lint"]) == 2
    assert "story tag is required" in capsys.readouterr().err


def test_lint_rejects_duplicate_ids_and_invalid_story_values():
    for cases in ((runner.SCENARIOS[0], runner.SCENARIOS[0]),
                  (runner.Scenario("SCN-TLS-0088", ("random",), "list_tools"),),
                  (runner.Scenario("SCN-TLS-888", ("TLS-S05",), "list_tools"),)):
        with pytest.raises(ValueError):
            runner.validate_scenarios(cases)


def test_pr_ci_runs_tag_lint_before_unit_fixtures():
    from pathlib import Path

    import yaml

    workflow = yaml.load((Path(__file__).resolve().parents[3] / ".github/workflows/tools-ci.yml").read_text(),
                         Loader=yaml.BaseLoader)
    paths = workflow["on"]["pull_request"]["paths"]
    assert "tests/e2e/tools/**" in paths
    assert ".github/workflows/tools-live-evaluation.yml" in paths
    source = "\n".join(step.get("run", "") for step in workflow["jobs"]["verify"]["steps"])
    assert "python -m tests.e2e.tools.runner --lint" in source
    assert "python -m pytest -q tests/e2e/tools" in source
    assert ("python -m pip install -e 'modules/gateway[dev]' -e modules/tools -e modules/tools/facade -e modules/tools/adapters "
            "moto==5.0.27 'aiosqlite>=0.20.0'") in source


def test_default_filter_includes_lifecycle_faults_without_false_green(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-TLS-00*")
    assert not success and report["live_attestation"] is False
    assert {row["scenario_id"] for row in report["scenarios"]} >= {
        f"SCN-TLS-{number:04}" for number in range(50, 60)
    }
    for row in report["scenarios"]:
        if row["scenario_id"] != "SCN-TLS-0017":
            assert row["status"] == "not-run"
            if row["scenario_id"] in {f"SCN-TLS-{number:04}" for number in range(50, 60)}:
                assert row["cleanup"]["status"] == "not-run"
                assert row["fixture"]["scope"] == "run-owned-isolated"


def test_runner_invokes_isolated_fault_driver_without_accepting_simulated_outcome(tmp_path, monkeypatch):
    from tests.e2e.tools.test_scenarios import IsolatedDriver

    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    case = next(case for case in runner.SCENARIOS if case.identifier == "SCN-TLS-0051")
    driver = IsolatedDriver()
    success = runner.evaluate(case.identifier, tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], scenarios=(case,),
                              client_factory=FixtureClient, fault_driver_factory=lambda *_: driver)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not success and not report["live_attestation"]
    row = report["scenarios"][0]
    assert row["status"] == "blocked" and row["execution_id"] and row["request_ids"]
    assert row["operation_ids"] and row["provider_request_observations"]
    assert row["artifact_sha256"] and row["cleanup"]["status"] == "confirmed"
    assert driver.calls[-1][0] == "stop"


def test_full_and_l5_include_prior_gates_and_every_lifecycle_id():
    expected = set(runner.BASE_STORIES)
    expected |= {f"{number:04}" for number in range(50, 60)}
    for selection in ("full", "l5", "SCN-TLS-00*"):
        cases = runner.select_scenarios(selection)
        identifiers = {case.identifier for case in cases}
        assert {identifier.removeprefix("SCN-TLS-") for identifier in identifiers if identifier.startswith("SCN-TLS-")} == expected
        assert identifiers >= {f"SCN-TLS-{number:04}" for number in range(50, 60)}
        assert all(identifier.startswith("SCN-TLS-") for identifier in identifiers)


def test_repair_registry_keeps_both_story_families_and_local_acceptance_mapping(tmp_path, monkeypatch):
    expected_actions = {
        8064: ("contract_ac01", "contract_ac02", "contract_ac03", "contract_ac04"),
        8065: ("access_ac01", "access_ac02", "access_ac03", "access_live"),
        8066: ("cleanup_ac01", "cleanup_ac02", "cleanup_ac03", "cleanup_ac04"),
        8067: ("client_install", "client_matrix", "client_faults", "client_evidence"),
        8068: ("release_ac01", "release_ac02", "release_ac03", "release_ac04"),
        8069: ("search_ac01", "search_ac02", "search_ac03", "search_ac04"),
        8070: ("interpreter_ac01", "interpreter_ac02", "interpreter_ac03", "interpreter_ac04"),
    }
    expected = {f"SCN-{issue}-{number:02}": (issue, number, action)
                for issue, actions in expected_actions.items()
                for number, action in enumerate(actions, 1)}
    expected["SCN-8071-01"] = (8071, 1, "browser_ac01")
    expected["SCN-8071-02"] = (8071, 2, "browser_ac02")
    expected["SCN-8071-03"] = (8071, 3, "browser_ac03")
    expected["SCN-8071-04"] = (8071, 4, "browser_ac04")
    expected["SCN-8072-01"] = (8072, 1, "knowledge_ac01")
    expected["SCN-8072-02"] = (8072, 2, "knowledge_ac02")
    expected["SCN-8072-03"] = (8072, 3, "knowledge_ac03")
    expected["SCN-8072-04"] = (8072, 4, "knowledge_ac04")
    expected["SCN-8073-01"] = (8073, 1, "broker_ac01")
    expected["SCN-8073-02"] = (8073, 2, "broker_ac02")
    expected["SCN-8073-03"] = (8073, 3, "broker_ac03")
    expected["SCN-8073-04"] = (8073, 4, "broker_ac04")
    for number in range(1, 5):
        expected[f"SCN-8078-{number:02}"] = (8078, number, f"governance_ac{number:02}")
    expected["SCN-8074-01"] = (8074, 1, "oauth_ac01")
    expected["SCN-8074-02"] = (8074, 2, "oauth_ac02")
    expected["SCN-8074-03"] = (8074, 3, "oauth_ac03")
    expected["SCN-8074-04"] = (8074, 4, "oauth_ac04")
    expected["SCN-8075-01"] = (8075, 1, "github_ac01")
    expected["SCN-8075-02"] = (8075, 2, "github_ac02")
    expected["SCN-8075-03"] = (8075, 3, "github_ac03")
    expected["SCN-8075-04"] = (8075, 4, "github_ac04")
    for number in range(1, 5):
        expected[f"SCN-8076-{number:02}"] = (8076, number, f"jira_ac{number:02}")
    expected["SCN-8077-01"] = (8077, 1, "slack_ac01")
    expected["SCN-8077-02"] = (8077, 2, "slack_ac02")
    expected["SCN-8077-03"] = (8077, 3, "slack_ac03")
    expected["SCN-8077-04"] = (8077, 4, "slack_ac04")
    expected["SCN-8454-01"] = (8454, 1, "atlassian_setup")
    expected["SCN-8454-02"] = (8454, 2, "atlassian_client_matrix")
    expected["SCN-8454-03"] = (8454, 3, "atlassian_authority")
    expected["SCN-8454-04"] = (8454, 4, "atlassian_recovery")
    expected["SCN-8454-05"] = (8454, 5, "atlassian_regression")
    expected["SCN-8454-06"] = (8454, 6, "atlassian_handoff")
    cases = runner.select_scenarios("repair")
    assert {case.identifier: case.action for case in cases} == {
        identifier: mapping[2] for identifier, mapping in expected.items()
    }
    for issue in expected_actions:
        assert {case.identifier for case in runner.select_scenarios(f"SCN-{issue}-*")} == {
            identifier for identifier, mapping in expected.items() if mapping[0] == issue
        }

    success, report = evaluate(tmp_path, monkeypatch, pattern="repair")
    assert not success and report["live_attestation"] is False
    assert set(report["selected_scenario_ids"]) == set(expected)
    rows = report["scenarios"]
    assert len(rows) == 198 + len(runner.GOVERNANCE_FAMILIES) + 2 + 4 * len(runner.CLIENTS) + 21
    assert {row["scenario_id"] for row in rows} == set(expected)
    for row in rows:
        issue, number, _ = expected[row["scenario_id"]]
        assert row["issue_number"] == issue
        assert row["local_scenario_id"] == f"SCN-{number:02}"
        assert row["tags"] == [f"@scenario {row['scenario_id']}", f"@story {issue}",
                                f"@acceptance AC-{number:02}"]
        assert row["status"] == ("blocked" if issue in {8068, 8073, 8074, 8075, 8076, 8077, 8078, 8454} else "not-run") and row["reason"]
        if issue in {8068, 8077, 8078}:
            assert row["evidence_scope"] == "repository-integration"
    assert {row["client"] for row in rows if row["scenario_id"] == "SCN-8064-04"} == set(runner.CLIENTS)
    suite = ElementTree.parse(tmp_path / "junit.xml").getroot()
    names = [entry.attrib["name"] for entry in suite.findall("testcase")]
    assert len(set(names)) == len(rows)
    assert suite.attrib["failures"] == str(len(rows))


@pytest.mark.parametrize("local_id", ("SCN-01", "SCN-02", "SCN-03", "SCN-04"))
def test_ambiguous_local_selector_is_rejected_with_qualified_guidance(tmp_path, monkeypatch, local_id):
    with pytest.raises(ValueError, match="story-qualified selector"):
        runner.select_scenarios(local_id)
    success, report = evaluate(tmp_path, monkeypatch, pattern=local_id)
    assert not success and not report["scenarios"]
    assert "ambiguous local scenario ID" in report["selection_error"]


def test_live_repair_selection_retains_every_id_and_requires_separate_bounded_access_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: pytest.fail("must refuse before network use"))
    success = runner.evaluate("repair", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], live=True,
                              client_factory=lambda *_: pytest.fail("must refuse before client start"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not success and not report["scenarios"]
    assert {"SCN-8075-01", "SCN-8075-02", "SCN-8075-03", "SCN-8075-04"} <= set(report["selected_scenario_ids"])
    assert set(report["selected_scenario_ids"]) - {"SCN-8075-01", "SCN-8075-02", "SCN-8075-03", "SCN-8075-04"} == {
        f"SCN-{issue}-{number:02}" for issue in (8064, 8065, 8066, 8067) for number in range(1, 5)
    } | {"SCN-8068-01", "SCN-8068-02", "SCN-8068-03", "SCN-8068-04",
         "SCN-8069-01", "SCN-8069-02", "SCN-8069-03", "SCN-8069-04",
         "SCN-8070-01", "SCN-8070-02", "SCN-8070-03", "SCN-8070-04",
         "SCN-8071-01", "SCN-8071-02", "SCN-8071-03", "SCN-8071-04", "SCN-8072-01", "SCN-8072-02", "SCN-8072-03", "SCN-8072-04", "SCN-8073-01", "SCN-8073-02", "SCN-8073-03", "SCN-8073-04",
         "SCN-8074-01", "SCN-8074-02", "SCN-8074-03", "SCN-8074-04",
         "SCN-8076-01", "SCN-8076-02", "SCN-8076-03", "SCN-8076-04",
         "SCN-8077-01", "SCN-8077-02", "SCN-8077-03", "SCN-8077-04",
         "SCN-8078-01", "SCN-8078-02", "SCN-8078-03", "SCN-8078-04", "SCN-8454-01", "SCN-8454-02", "SCN-8454-03", "SCN-8454-04", "SCN-8454-05", "SCN-8454-06"}
    assert "SCN-8065-04 must run alone" in report["selection_error"]
    assert "qualification must enumerate every repair case" in report["selection_error"]


@pytest.mark.parametrize("number", (1, 2, 3))
def test_local_access_integration_cannot_pass_as_live_acceptance(tmp_path, monkeypatch, number):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate(f"SCN-8065-{number:02}", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], live=True,
                              client_factory=FixtureClient,
                              integration_driver_factory=lambda *_: pytest.fail("local driver must not run as live"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    row, = report["scenarios"]
    assert not success and row["status"] == "blocked"
    assert row["evidence_scope"] == "repository-integration"
    assert "cannot establish deployed acceptance" in row["reason"]


def test_real_clients_and_workloads_have_separate_nonpassing_cells(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="l5")
    assert not success
    core = [row for row in report["scenarios"] if row["scenario_id"] == "SCN-TLS-0050"]
    assert {(row["client"], row["tool"]) for row in core} == {
        (client, tool) for client in ("cli", "claude-code", "codex")
        for tool in ("search", "interpreter", "browser")
    }
    for identifier in ("SCN-TLS-0058", "SCN-TLS-0059"):
        workload = [row for row in report["scenarios"] if row["scenario_id"] == identifier]
        assert {(row["client"], row["tool"]) for row in workload} == {
            (client, tool) for client in ("crew-pod", "engine-worker") for tool in ("interpreter", "browser")
        }
    assert all(row["status"] != "pass" for row in core)
    assert all(row["client_version"] is None for row in core if row["client"] != "cli")
    suite = ElementTree.parse(tmp_path / "junit.xml").getroot()
    names = [entry.attrib["name"] for entry in suite.findall("testcase")]
    assert len(names) == len(set(names)) == len(report["scenarios"])
    assert suite.attrib["failures"] == str(len(report["scenarios"]))


def test_each_cli_tool_cell_gets_a_separate_bounded_0050_execution(tmp_path, monkeypatch):
    from tests.e2e.tools.test_scenarios import IsolatedDriver

    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    drivers = {}

    def driver_for_cell(case, client_name, tool):
        assert case.identifier == "SCN-TLS-0050" and client_name == "cli"
        drivers[tool] = IsolatedDriver()
        return drivers[tool]

    success = runner.evaluate("SCN-TLS-0050", tmp_path, "https://example.invalid", SHA,
                              GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"],
                              client_factory=FixtureClient, fault_driver_factory=driver_for_cell)
    assert not success
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert len(drivers) == 3
    for row in rows:
        if row["client"] == "cli":
            assert row["fixture"]["fault_actions"] == [row["tool"], "confirm_stop"]
            assert row["fixture"]["max_provider_calls"] == 1
            assert row["status"] == "blocked"
        else:
            assert row["status"] == "not-run"
    assert len({row["request_ids"][0] for row in rows if row["client"] == "cli"}) == 3


class ContractClient:
    version = "test-client"

    def __init__(self, command):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request(self, method, params=None):
        if method == "tools/list":
            fields = {"websearch___search": ["query"], "code_interpreter___start": [],
                      "code_interpreter___execute": ["session_id", "code", "language", "execution_ref"],
                      "code_interpreter___close": ["session_id", "execution_ref"]}
            return {"tools": [{"name": name, "inputSchema": {"x-adp-schema-version": "1.0",
                "properties": dict.fromkeys(required, {"type": "string"}), "required": required}}
                for name, required in fields.items()]}
        assert method == "tools/call" and set(params) == {"name", "arguments"}
        self.calls.append(params)
        index = len(self.calls)
        if index > 2:
            assert params["arguments"].get("execution_ref") == "00000000-0000-4000-8000-000000000002"
        receipt = {"status": "confirmed", "operation_ref": f"00000000-0000-4000-8000-{index:012d}",
                   "execution_id": "00000000-0000-4000-8000-000000000011", "receipt_id": f"receipt-{index}"}
        if index == 2:
            receipt["result"] = {"result": {"result": {"session_id": "session-owned"}}}
        return {"content": [{"type": "text", "text": json.dumps(receipt)}], "isError": False}


@pytest.mark.parametrize("live", (False, True))
def test_contract_ac01_valid_calls_are_schema_supported_but_not_live_evidence(tmp_path, monkeypatch, live):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    created = []

    def client_factory(command):
        client = ContractClient(command)
        created.append(client)
        return client

    fixture = {"query": "isolated search", "code": "print(1)", "language": "python"}
    success = runner.evaluate("SCN-8064-01", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=client_factory, contract_fixture=fixture, live=live)
    assert not success
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["tags"] == ["@scenario SCN-8064-01", "@story 8064", "@acceptance AC-01"]
    assert row["status"] == "blocked" and row["observations"]["cleanup"]["status"] == "not-verified"
    assert row["observations"]["close_receipt_status"] == "confirmed"
    assert len(row["observations"]["request_ids"]) == len(created[0].calls) == 4
    assert [call["name"] for call in created[0].calls] == ["websearch___search", "code_interpreter___start",
                                                             "code_interpreter___execute", "code_interpreter___close"]
    assert created[0].calls[2]["arguments"] == {"execution_ref": "00000000-0000-4000-8000-000000000002",
                                                  "session_id": "session-owned", "code": "print(1)", "language": "python"}


def test_contract_ac01_requires_fixture_and_does_not_claim_execution(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-8064-01")
    assert not success
    assert report["scenarios"][0]["status"] == "not-run"
    assert report["scenarios"][0]["reason"] == "isolated search and interpreter fixture unavailable"


class RejectingClient:
    version = "test-client"

    def __init__(self, command):
        self.calls = []
        self.provider_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request_raw(self, method, params):
        assert method == "tools/call"
        self.calls.append(params)
        messages = ["Tool name and object arguments required", "Server-owned authority arguments are not accepted",
                    "Unknown operation reference", "Arguments do not match the advertised client schema"]
        return {"jsonrpc": "2.0", "id": len(self.calls), "error": {"code": -32603,
                "message": messages[len(self.calls) - 1]}}


@pytest.mark.parametrize("live", (False, True))
def test_contract_ac02_rejects_injected_and_malformed_calls_without_claiming_dispatch(tmp_path, monkeypatch, live):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    created = []

    def client_factory(command):
        client = RejectingClient(command)
        created.append(client)
        return client

    fixture = {"foreign_ref": "00000000-0000-4000-8000-000000000098", "foreign_session": "other-owner"}
    success = runner.evaluate("SCN-8064-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=client_factory, contract_fixture=fixture, live=live)
    assert not success
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["tags"] == ["@scenario SCN-8064-02", "@story 8064", "@acceptance AC-02"]
    assert row["status"] == "blocked" and row["observations"] == {"rejected_calls": 4,
                                                               "provider_dispatch": "not-verified"}
    assert len(created[0].calls) == 4 and created[0].provider_calls == 0
    assert set(created[0].calls[0]) == {"name", "arguments", "request_id"}
    assert created[0].calls[2]["arguments"]["execution_ref"] == fixture["foreign_ref"]


def test_contract_ac02_does_not_accept_unknown_as_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)

    class UncertainClient(RejectingClient):
        def request_raw(self, method, params):
            packet = super().request_raw(method, params)
            packet["result"] = {"status": "unknown"}
            return packet

    fixture = {"foreign_ref": "00000000-0000-4000-8000-000000000098", "foreign_session": "other-owner"}
    success = runner.evaluate("SCN-8064-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=UncertainClient, contract_fixture=fixture)
    assert not success
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "fail" and row["reason"] == "ValueError"


def test_contract_ac02_real_stdio_rejects_before_gateway_request(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    bridge = Path(__file__).resolve().parents[3] / "modules/gateway/cli/adp-mcp.py"

    def client_factory(command):
        return runner.MCPClient([sys.executable, str(bridge)],
                                environ={**os.environ, "BG_CONFIG_DIR": str(tmp_path)})

    fixture = {"foreign_ref": "00000000-0000-4000-8000-000000000098", "foreign_session": "other-owner"}
    success = runner.evaluate("SCN-8064-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=client_factory, contract_fixture=fixture)
    assert not success
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "blocked" and row["observations"]["rejected_calls"] == 4
    assert row["observations"]["provider_dispatch"] == "not-verified"


class LostReplyDriver:
    def __init__(self):
        self.armed = False
        self.count = 0
        self.repeated = False

    def arm_drop(self):
        self.armed = True

    def provider_calls(self):
        return self.count


class RecoveryClient:
    version = "test-client"
    reference = "00000000-0000-4000-8000-000000000071"

    def __init__(self, command, driver):
        self.driver = driver
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request(self, method, params):
        assert method == "tools/call" and set(params) == {"name", "arguments"}
        self.calls.append(params)
        if params["name"] == "websearch___search":
            assert self.driver.armed and params["arguments"] == {"query": "original"}
            self.driver.count += 1
            raise TimeoutError("reply dropped after dispatch")
        if params["name"] == "adp___operations_list":
            receipt = {"operations": [{"operation_ref": self.reference, "tool": "websearch___search",
                                       "status": "unknown"}]}
        elif params["name"] == "adp___operation_retry":
            assert params["arguments"]["operation_ref"] == self.reference
            query = params["arguments"]["arguments"]["query"]
            assert query in {"original", "changed"}
            if self.driver.repeated and query == "original":
                self.driver.count += 1
            receipt = {"status": "confirmed" if query == "original" else "rejected",
                       "operation_ref": self.reference}
        else:
            raise AssertionError("unexpected MCP call")
        return {"content": [{"type": "text", "text": json.dumps(receipt)}]}


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("live", (False, True))
def test_contract_ac03_reconnect_preserves_payload_and_detects_repeat(tmp_path, monkeypatch, repeat, live):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    driver = LostReplyDriver()
    driver.repeated = repeat
    clients = []

    def client_factory(command):
        client = RecoveryClient(command, driver)
        clients.append(client)
        return client

    def driver_factory(case, client_name, tool):
        assert (case.identifier, client_name, tool) == ("SCN-8064-03", "cli", "contract")
        return driver

    fixture = {"query": "original", "changed_query": "changed"}
    success = runner.evaluate("SCN-8064-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=client_factory,
                              fault_driver_factory=driver_factory, contract_fixture=fixture, live=live)
    assert not success
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["tags"] == ["@scenario SCN-8064-03", "@story 8064", "@acceptance AC-03"]
    assert len(clients) == 2 and len(clients[0].calls) == 1 and len(clients[1].calls) == 3
    assert clients[1].calls[1]["arguments"]["arguments"] == {"query": "changed"}
    assert clients[1].calls[2]["arguments"]["arguments"] == {"query": "original"}
    if repeat:
        assert row["status"] == "fail" and row["reason"] == "ValueError" and driver.count == 2
    else:
        assert row["status"] == "blocked" and driver.count == 1
        assert row["observations"]["request_id"] == RecoveryClient.reference
        assert row["observations"]["changed_payload"] == "rejected"
        assert row["observations"]["provider_dispatch"] == "not-independently-verified"


def test_contract_ac03_missing_fault_driver_cannot_dispatch(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-8064-03")
    assert not success and report["scenarios"][0]["status"] == "not-run"
    assert report["scenarios"][0]["reason"] == "isolated lost-reply fixture or fault driver unavailable"
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    success = runner.evaluate("SCN-8064-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=FixtureClient,
                              fault_driver_factory=lambda *args: None,
                              contract_fixture={"query": "original", "changed_query": "changed"})
    assert not success
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "not-run" and row["reason"] == "isolated lost-reply fault driver unavailable"


def priced_client_fixtures():
    return {name: {"query": "isolated search", "code": "print(1)", "language": "python",
                   "client_version": "test-client", "max_seconds": 300, "session_ttl_seconds": 300,
                   "cleanup_seconds": 600, "max_cost_usd": 5, "unit_cost_usd": 0.1,
                   "pricing_source": "private-authorized-price", "authority_confirmed": True}
            for name in ("cli", "claude-code", "codex")}


@pytest.mark.parametrize("field,value", [("authority_confirmed", False), ("pricing_source", ""),
                                          ("unit_cost_usd", None), ("session_ttl_seconds", 301),
                                          ("cleanup_seconds", 601), ("max_cost_usd", 21)])
def test_contract_ac04_missing_authority_pricing_or_bounds_blocks_fixture(field, value):
    fixture = priced_client_fixtures()["cli"]
    fixture[field] = value
    with pytest.raises(runner.FixtureUnavailable):
        runner.validate_contract_ac04_fixture(fixture)


def test_contract_ac04_requires_pinned_adapters_without_claiming_live_success(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-8064-04")
    assert not success
    assert [(row["client"], row["status"], row["client_version"]) for row in report["scenarios"]] == [
        (name, "not-run", None) for name in ("cli", "claude-code", "codex")]
    assert all(row["reason"] == "priced authorized per-client fixture unavailable" for row in report["scenarios"])


def test_contract_ac04_isolated_cells_remain_blocked_without_provider_readback(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    created = []

    def pinned_factory(client_name, fixture):
        client = ContractClient([client_name])
        created.append((client_name, client))
        return client

    success = runner.evaluate("SCN-8064-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=FixtureClient,
                              contract_fixture=priced_client_fixtures(), pinned_client_factory=pinned_factory)
    assert not success
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert [(row["client"], row["cell_id"], row["client_version"]) for row in rows] == [
        (name, name + ":contract", "test-client") for name in ("cli", "claude-code", "codex")]
    assert all(row["status"] == "blocked" and row["tags"] == ["@scenario SCN-8064-04", "@story 8064", "@acceptance AC-04"]
               for row in rows)
    assert all(row["observations"]["cleanup"]["status"] == "not-verified" and
               row["observations"]["receipt_ids"] == [f"receipt-{index}" for index in range(1, 5)]
               for row in rows)
    assert [len(client.calls) for _, client in created] == [4, 4, 4]
    assert all(row["fixture_bounds"] == {"max_seconds": 300, "session_ttl_seconds": 300,
                                         "cleanup_seconds": 600, "max_cost_usd": 5} for row in rows)


def test_contract_ac04_live_mode_cannot_replace_independent_provider_observer(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    success = runner.evaluate("SCN-8064-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=FixtureClient, live=True,
                              contract_fixture=priced_client_fixtures(),
                              pinned_client_factory=lambda *_: pytest.fail("observer required before paid calls"))
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert not success and len(rows) == 3
    assert all(row["status"] == "not-run" and
               row["reason"] == "pinned client adapter or independent provider observer unavailable"
               for row in rows)


def test_contract_ac04_missing_authority_and_run_wide_cost_prevent_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    created = []

    def pinned_factory(client_name, fixture):
        created.append(client_name)
        return ContractClient([client_name])

    fixtures = priced_client_fixtures()
    fixtures["cli"]["authority_confirmed"] = False
    fixtures["claude-code"]["max_cost_usd"] = 20
    fixtures["codex"]["max_cost_usd"] = 5
    success = runner.evaluate("SCN-8064-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                              command=["adp", "mcp"], client_factory=FixtureClient,
                              contract_fixture=fixtures, pinned_client_factory=pinned_factory)
    assert not success and created == ["claude-code"]
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert [row["status"] for row in rows] == ["not-run", "blocked", "not-run"]
    assert rows[0]["reason"] == "priced authorized per-client fixture unavailable"
    assert rows[2]["reason"] == "run-wide $20 cost bound exceeded"


def test_contract_ac04_synthetic_oracle_requires_bounded_cost_and_independent_cleanup():
    fixture = priced_client_fixtures()["cli"]
    fixture["client_version"] = "uninstalled-version"
    client = ContractClient(["test"])
    with pytest.raises(ValueError, match="pinned client version"):
        runner.contract_ac04(client, fixture, None, live=False)
    assert client.calls == []
    fixture["client_version"] = "test-client"

    class Observer:
        def __init__(self, cost=1, cleanup="confirmed", ttl=300, cleanup_elapsed=30):
            self.cost = cost
            self.cleanup = cleanup
            self.ttl = ttl
            self.cleanup_elapsed = cleanup_elapsed
            self.timeout = None

        def observe(self, *, execution_id, request_ids, timeout):
            self.timeout = timeout
            return {"source": "provider-control-plane", "execution_id": execution_id,
                    "request_ids": list(request_ids), "receipt_ids": [f"receipt-{index}" for index in range(1, 5)],
                    "provider_request_count": 4, "cost_usd": self.cost,
                    "session_count": 1, "session_ttl_seconds": self.ttl,
                    "cleanup_elapsed_seconds": self.cleanup_elapsed,
                    "cleanup_status": self.cleanup, "termination": "stopped",
                    "termination_observed_at": "synthetic-observation"}

    observer = Observer()
    status, _, evidence = runner.contract_ac04(ContractClient(["test"]), fixture, observer, live=True)
    assert status == "pass" and evidence["cleanup"]["status"] == "confirmed"
    assert observer.timeout == 600
    assert runner.contract_ac04(ContractClient(["test"]), fixture, Observer(cost=6), live=True)[0] == "fail"
    assert runner.contract_ac04(ContractClient(["test"]), fixture, Observer(cleanup="pending"), live=True)[0] == "blocked"
    assert runner.contract_ac04(ContractClient(["test"]), fixture, Observer(ttl=301), live=True)[0] == "fail"
    assert runner.contract_ac04(ContractClient(["test"]), fixture, Observer(cleanup_elapsed=601), live=True)[0] == "fail"


def test_installed_client_matrix_enumerates_every_operation_without_fixtures(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    assert not runner.evaluate("SCN-8067-02", tmp_path, "https://example.invalid", SHA,
                               GATEWAY_IMAGE, IMAGE, command=["checkout", "mcp"],
                               client_factory=lambda _: pytest.fail("checkout shim must not launch"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert len(report["scenarios"]) == 90
    assert {row["status"] for row in report["scenarios"]} == {"not-run"}
    assert {row["tool"] for row in report["scenarios"]} == set(runner.OPERATIONS)
    assert {row["client"] for row in report["scenarios"]} == set(runner.CLIENTS)


def test_one_matrix_cell_never_counts_as_complete_live_qualification(tmp_path, monkeypatch):
    from tests.e2e.tools.test_client_matrix import matrix_fixture

    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    calls = []

    def execute(client, operation, fixture, **binding):
        calls.append((client, operation, binding["gateway_revision"]))
        return {"client_version": "1.0.0", "source": "installed-cli-mcp", "cleanup": {"status": "confirmed"}}

    monkeypatch.setattr(runner, "harness_ac02", execute)
    observer = object()
    result = runner.evaluate("SCN-8067-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE,
                             IMAGE, command=["checkout", "mcp"], live=True, client_fixture={"operations": {
                                 "websearch___search": matrix_fixture()}}, provider_observer=observer,
                             client_refresh_file="/protected/ref", cell_filter="cli:websearch___search",
                             recovery_root=tmp_path,
                             client_factory=lambda _: pytest.fail("checkout shim must not launch"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert result is False and report["live_attestation"] is False and report["matrix_complete"] is False
    assert len(report["scenarios"]) == 1 and report["scenarios"][0]["status"] == "blocked"
    assert "#8078" in report["scenarios"][0]["reason"] and calls == []


def test_matrix_rejects_unpriced_fixture_before_any_client_launch(tmp_path, monkeypatch):
    from tests.e2e.tools.test_client_matrix import matrix_fixture

    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    monkeypatch.setattr(runner, "harness_ac02", lambda *a, **k: pytest.fail("fixture must be rejected"))
    fixture = {**matrix_fixture(), "max_cost_usd": 21}
    result = runner.evaluate("SCN-8067-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE,
                             IMAGE, command=[], live=False, client_fixture={"operations": {
                                 "websearch___search": fixture}}, provider_observer=object(),
                             client_refresh_file="/protected/ref", cell_filter="cli:websearch___search",
                             recovery_root=tmp_path,
                             client_factory=lambda _: pytest.fail("checkout shim must not launch"))
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert result is False and row["status"] == "fail" and row["reason"] == "ValueError"


def test_offline_harness_ac02_uses_only_injected_fake_backend_and_readback(monkeypatch):
    from contextlib import contextmanager
    from tests.e2e.tools.test_client_matrix import LISTING, OPERATION, RECEIPT, matrix_fixture, provider_evidence

    calls = []

    @contextmanager
    def installation(url, revision, **kwargs):
        assert url == "https://example.invalid" and revision == SHA
        assert kwargs["recovery_root"] == "/protected/recovery"
        yield Path("/isolated/.adp/bin/adp"), {"HOME": "/isolated"}, {"adp": "a" * 64,
                                                           "adp-mcp.py": "b" * 64}

    class Client:
        version = "1.0.0"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, params=None):
            calls.append((method, params))
            if method == "tools/list":
                return LISTING
            return {"content": [{"type": "text", "text": json.dumps(RECEIPT)}]}

    class Observer:
        def observe(self, **binding):
            calls.append(("provider", binding))
            return provider_evidence()

    monkeypatch.setattr(runner, "import_session", lambda *args: calls.append(("import", args[2])))
    monkeypatch.setattr(runner, "configure_model_transport", lambda *args: pytest.fail("stdio cannot configure models"))
    observation = runner._offline_harness_ac02("cli", OPERATION, matrix_fixture("cli"), gateway_url="https://example.invalid",
                                      gateway_revision=SHA, refresh_file="/protected/ref", observer=Observer(),
                                      recovery_root="/protected/recovery", recovery_id="00000000-0000-4000-8000-000000000001",
                                      client_factory=lambda command, *, environ: Client(), offline_installer=installation)
    assert [call[0] for call in calls] == ["import", "tools/list", "tools/call", "provider"]
    assert calls[2][1]["name"] == OPERATION and observation["installed_mcp_sha256"] == "b" * 64
    assert observation["source"] == "offline-synthetic-backend"
    assert observation["estimated_all_in_usd"] and "reserved_all_in_usd" not in observation
    assert observation["provider_observation"]["source"] == "provider-control-plane"
    assert observation["cleanup"] == observation["provider_observation"]["cleanup"]

    class Unconfirmed(Observer):
        def observe(self, **binding):
            return {**super().observe(**binding), "cleanup_status": "pending"}

    with pytest.raises(ValueError, match="cleanup"):
        runner._offline_harness_ac02("cli", OPERATION, matrix_fixture("cli"), gateway_url="https://example.invalid",
                            gateway_revision=SHA, refresh_file="/protected/ref", observer=Unconfirmed(),
                            recovery_root="/protected/recovery", recovery_id="00000000-0000-4000-8000-000000000001",
                            client_factory=lambda command, *, environ: Client(), offline_installer=installation)
    with pytest.raises(runner.FixtureUnavailable):
        runner._offline_harness_ac02("cli", OPERATION, matrix_fixture("cli"), gateway_url="https://example.invalid",
                           gateway_revision=SHA, refresh_file="/protected/ref", observer=None,
                           recovery_root="/protected/recovery", recovery_id="00000000-0000-4000-8000-000000000001",
                           client_factory=lambda command, *, environ: Client(), offline_installer=installation)


@pytest.mark.parametrize("client_name", ("claude-code", "codex"))
def test_native_harness_keeps_model_setup_before_fixture_work(monkeypatch, client_name):
    from contextlib import contextmanager

    from tests.e2e.tools.test_client_matrix import OPERATION, matrix_fixture

    @contextmanager
    def installation(*args, **kwargs):
        yield Path("/isolated/adp"), {"HOME": "/isolated"}, {}

    def setup(*args):
        raise RuntimeError("native model setup required")

    monkeypatch.setattr(runner, "import_session", lambda *args: None)
    monkeypatch.setattr(runner, "configure_model_transport", setup)
    monkeypatch.setattr(runner, "prepare_owned", lambda *args, **kwargs: pytest.fail("setup not validated"))
    with pytest.raises(RuntimeError, match="native model setup required"):
        runner._offline_harness_ac02(client_name, OPERATION, matrix_fixture(client_name),
                                    gateway_url="https://example.invalid", gateway_revision=SHA,
                                    refresh_file="offline-only", observer=object(), recovery_root="/private",
                                    recovery_id="00000000-0000-4000-8000-000000000001",
                                    offline_installer=installation, client_factory=None)


def test_paid_installed_client_helper_refuses_ci_before_loading_any_fixture(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(runner, "installed_clients", lambda *_: pytest.fail("paid CI operation attempted"))
    with pytest.raises(runner.FixtureUnavailable, match="forbidden in CI"):
        runner.harness_ac02("cli", "websearch___search", {}, gateway_url="https://example.invalid",
                            gateway_revision=SHA, refresh_file="/protected/ref", observer=object(),
                            recovery_root="/protected/recovery", recovery_id="00000000-0000-4000-8000-000000000001")


@pytest.mark.parametrize("fault", (TimeoutError("reply lost after dispatch"),
                                   ProcessLimitError("client process deadline exceeded")))
def test_unknown_matrix_outcome_keeps_original_reference_and_blocks(tmp_path, fault):
    from uuid import uuid4

    original = str(uuid4())
    recovery_id = str(uuid4())
    store = tmp_path / recovery_id / ".bedrock-gateway" / "tool-operations"
    store.mkdir(parents=True)
    (store / (original + ".json")).write_text(json.dumps({"request_id": original}))
    row = {"recovery_id": recovery_id}
    runner.block_uncertain_matrix(row, tmp_path, fault)
    assert row["status"] == "blocked" and row["reason"] == type(fault).__name__
    assert row["request_ids"] == [original] and row["cleanup"]["status"] == "unconfirmed"
    assert (store / (original + ".json")).exists()


def test_unconfirmed_cleanup_cannot_accept_a_matrix_cell(tmp_path, monkeypatch):
    from tests.e2e.tools.test_client_matrix import matrix_fixture

    with pytest.raises(ValueError, match="cleanup"):
        runner.confirmed_cleanup({"cleanup": {"status": "pending"},
                                  "provider_observation": {"cleanup": {"status": "pending"}}})
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setattr(runner, "harness_ac02", lambda *args, **kwargs: pytest.fail("paid call attempted"))
    passed = runner.evaluate("SCN-8067-02", tmp_path / "results", "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=[], live=False,
                             client_fixture={"operations": {"websearch___search": matrix_fixture("cli")}},
                             provider_observer=object(), client_refresh_file="/protected/ref",
                             recovery_root=tmp_path, cell_filter="cli:websearch___search")
    report = json.loads((tmp_path / "results/evidence.json").read_text())
    row = report["scenarios"][0]
    assert not passed and not report["live_attestation"] and row["status"] == "blocked"
    assert "#8078" in row["reason"] and row["cleanup"]["status"] == "unconfirmed"
    assert "recovery_id" not in row and "request_ids" not in row


def test_recovery_restarts_original_installed_mcp_without_new_provider_dispatch(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from uuid import uuid4

    original = str(uuid4())
    recovery_id = str(uuid4())
    home = tmp_path / recovery_id
    store = home / ".bedrock-gateway/tool-operations"
    store.mkdir(parents=True)
    record = store / (original + ".json")
    record.write_text(json.dumps({"request_id": original, "tool": "websearch___search",
                                  "arguments": {"query": "fixture"}}))
    record.chmod(0o600)
    calls = []

    @contextmanager
    def resume(*args, **kwargs):
        calls.append(("resume", kwargs["recovery_id"]))
        yield Path("/retained/.adp/bin/adp"), {"BG_CONFIG_DIR": str(store.parent)}, {}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, params):
            calls.append((method, params))
            return {"content": [{"type": "text", "text": json.dumps({"status": "confirmed",
                "receipt_id": "receipt-1", "operation_ref": original})}]}

    monkeypatch.setattr(runner, "resume_installed_clients", resume)
    monkeypatch.setattr(runner, "import_session", lambda *args: calls.append(("import", args[2])))
    result = runner.recover_original_operation(tmp_path, recovery_id, original,
                                               gateway_url="https://example.invalid", gateway_revision=SHA,
                                               refresh_file="/protected/ref",
                                               client_factory=lambda command, *, environ: Client())
    assert [item[0] for item in calls] == ["resume", "import", "tools/call"]
    assert calls[-1][1] == {"name": "adp___operation_status", "arguments": {"operation_ref": original}}
    assert result["operation_ref"] == original and result["cleanup"]["status"] == "unconfirmed"


def test_installed_client_evidence_is_versioned_redacted_and_checksummed(tmp_path):
    import hashlib

    secret = "PRIVATE_TEST_SENTINEL"
    report = {"evidence_schema": "workflow-evaluation/v1", "schema_version": 1,
              "harness_revision": SHA, "github_run_id": "8100", "github_run_attempt": "2",
              "deployed_revisions": {"gateway": SHA, "gateway_image": GATEWAY_IMAGE,
                                     "facade_image": IMAGE}, "scenarios": []}
    for client in runner.CLIENTS:
        report["scenarios"].append({
            "scenario_id": "SCN-8067-02", "issue_number": 8067, "local_scenario_id": "SCN-02",
            "tags": ["@scenario SCN-8067-02", "@story 8067", "@acceptance AC-02"],
            "client": client, "cell_id": client + ":websearch___search", "tool": "websearch___search",
            "status": "blocked", "reason": "Bearer " + secret,
            "client_version": "1.0.0", "evidence_scope": "repository-integration",
            "observations": {"source": client + "-native-mcp" if client != "cli" else "installed-cli-mcp",
                             "client_version": "1.0.0", "discovery_sha256": "a" * 64,
                             "input_tokens": 17, "output_tokens": 23,
                             "provider_observation": {"source": "provider-control-plane",
                                                      "provider_request_count": 1,
                                                      "assertion": {"passed": True, "operation": "websearch___search"},
                                                      "cleanup": {"status": "confirmed", "observed_at": "now"},
                                                      "refresh_token": secret},
                             "raw_tool_result": secret, "authorization": "Bearer " + secret},
        })
    runner.write_results(tmp_path, report)
    published = json.loads((tmp_path / "evidence.json").read_text())
    assert published["evidence_schema"] == "workflow-evaluation/v1" and published["schema_version"] == 1
    assert secret not in (tmp_path / "evidence.json").read_text()
    assert secret not in (tmp_path / "junit.xml").read_text()
    assert len(published["scenarios"]) == 3
    for row in published["scenarios"]:
        evidence = row["client_evidence"]
        assert evidence["schema"] == "installed-client-evidence/v1"
        assert evidence["scenario_id"] == "SCN-8067-02" and evidence["acceptance_ids"] == ["AC-02"]
        assert evidence["client"] == row["client"] and evidence["cell_id"] == row["cell_id"]
        assert evidence["run"] == {"id": "8100", "attempt": "2"}
        assert evidence["source"]["harness_revision"] == SHA
        assert evidence["source"]["invocation"] == row["observations"]["source"]
        assert evidence["provider_source"] == row["observations"]["provider_observation"]["source"]
        assert evidence["target"] == report["deployed_revisions"]
        assert row["observations"]["provider_observation"]["provider_request_count"] == 1
        assert row["observations"]["input_tokens"] == 17
        assert row["observations"]["output_tokens"] == 23
        assert "raw_tool_result" not in row["observations"]
        assert "refresh_token" not in row["observations"]["provider_observation"]
        checksum = row.pop("evidence_sha256")
        assert checksum == hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                                   allow_nan=False).encode()).hexdigest()


def test_client_evidence_rejects_token_text_even_under_usage_keys():
    assert runner.client_evidence_value({"input_tokens": 17, "output_tokens": 23,
                                         "refresh_token": "private-token",
                                         "provider_observation": {"input_tokens": "Bearer test-token",
                                                                  "output_tokens": -1}}) == {
        "input_tokens": 17, "output_tokens": 23, "provider_observation": {}}


def test_non_ci_matrix_cannot_dispatch_without_authoritative_admission(tmp_path, monkeypatch):
    from tests.e2e.tools.test_client_matrix import OPERATION, matrix_fixture

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(runner, "installed_clients", lambda *args, **kwargs: pytest.fail("installer started"))
    monkeypatch.setattr(runner, "prepare_owned", lambda *args, **kwargs: pytest.fail("setup dispatched"))
    with pytest.raises(runner.FixtureUnavailable, match="#8078"):
        runner.harness_ac02("cli", OPERATION, matrix_fixture(), gateway_url="https://example.invalid",
                            gateway_revision=SHA, refresh_file="/protected/ref", observer=object(),
                            recovery_root=tmp_path, recovery_id="00000000-0000-4000-8000-000000000001")
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    result = runner.evaluate("SCN-8067-02", tmp_path / "matrix", "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=[], live=False,
                             client_fixture={"operations": {OPERATION: matrix_fixture("cli")}},
                             provider_observer=object(), client_refresh_file="/protected/ref",
                             recovery_root=tmp_path, cell_filter="cli:" + OPERATION)
    row = json.loads((tmp_path / "matrix/evidence.json").read_text())["scenarios"][0]
    assert not result and row["status"] == "blocked" and "#8078" in row["reason"]
    assert "recovery_id" not in row and row["cleanup"]["status"] == "unconfirmed"


def test_bounded_fault_selector_executes_all_four_rejections_without_client_launch(tmp_path, monkeypatch):
    release = client_release_window()
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    monkeypatch.setattr(runner, "installed_clients", lambda *args, **kwargs: pytest.fail("client launched"))
    monkeypatch.setattr(runner, "harness_ac02", lambda *args, **kwargs: pytest.fail("tool dispatched"))
    result = runner.evaluate("SCN-8067-03", tmp_path, "https://example.invalid", SHA,
                             GATEWAY_IMAGE, IMAGE, command=[], live=True,
                             release_manifest=release, release_observer=lambda phase: release["readbacks"][phase],
                             release_source_revision=SHA, release_account=release["account"])
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not result and report["live_attestation"] is False and len(report["scenarios"]) == 3
    for row in report["scenarios"]:
        assert row["scenario_id"] == "SCN-8067-03" and row["status"] == "blocked"
        assert row["evidence_scope"] == "repository-integration"
        assert row["cleanup"]["status"] == "unconfirmed"
        assert row["observations"] == {"source": "isolated-bounded-faults", "faults": {
            "missing_fixture": "rejected", "changed_digest": "rejected",
            "timeout": "rejected", "unconfirmed_cleanup": "rejected"}}
        assert row["client_evidence"]["source"]["invocation"] == "isolated-bounded-faults"
        assert row["client_evidence"]["provider_source"] is None
    assert {row["client"] for row in report["scenarios"]} == set(runner.CLIENTS)


def test_fault_selector_missing_or_changed_release_binding_cannot_attest(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    incomplete = runner.evaluate("SCN-8067-03", tmp_path / "missing", "https://example.invalid",
                                 SHA, GATEWAY_IMAGE, IMAGE, command=[], live=True)
    rows = json.loads((tmp_path / "missing/evidence.json").read_text())["scenarios"]
    assert not incomplete and all(row["status"] == "not-run" for row in rows)
    release = client_release_window()
    release["readbacks"]["before"]["gateway"]["image"] = "sha256:" + "f" * 64
    changed = runner.evaluate("SCN-8067-03", tmp_path / "changed", "https://example.invalid",
                              SHA, GATEWAY_IMAGE, IMAGE, command=[], live=True,
                              release_manifest=release,
                              release_observer=lambda phase: release["readbacks"][phase],
                              release_source_revision=SHA, release_account=release["account"])
    rows = json.loads((tmp_path / "changed/evidence.json").read_text())["scenarios"]
    assert not changed and all(row["status"] == "fail" and row["reason"] == "ValueError" for row in rows)


def test_public_operation_exception_does_not_weaken_generic_secret_text_redaction():
    operation = "credentials___list"
    assert runner.client_evidence_value(operation) == "[redacted]"
    assert runner.client_evidence_value({"operation": operation, "source": operation,
                                         "provider_observation": {"operation": operation}}) == {
        "operation": "[redacted]", "source": "[redacted]", "provider_observation": {"operation": "[redacted]"}}


def test_broker_ac02_isolated_role_file_and_provider_oracle(tmp_path, monkeypatch):
    from tests.e2e.tools.credential_integration import broker_ac02

    class Observer:
        def __init__(self, defect=None):
            self.defect = defect
            self.calls = {f"{owner}-{operation}": 0 for owner in ("owner-a", "owner-b")
                          for operation in ("assume_role", "materialize")}
            self.was_cleaned = False

        def clock(self, attempt_id):
            return 2_000_000_000

        def expected(self, fixture_id, attempt_id):
            operation = fixture_id.split("-", 2)[-1]
            return {"source": "independent-broker-fixture", "owner_id": fixture_id[:7],
                    "credential_id": fixture_id if self.defect != "shared_credential" else "shared",
                    "receipt_id": "receipt-" + fixture_id, "service": "aws" if operation == "assume_role" else "ssh",
                    "label": "owned", "sentinel": "private-" + fixture_id}

        def provider_calls(self, fixture_id, attempt_id):
            return self.calls[fixture_id]

        def issued(self, fixture_id, attempt_id):
            operation = fixture_id.split("-", 2)[-1]
            duration = 900 if operation == "assume_role" else 300
            if self.defect == "overlong" and operation == "assume_role":
                duration += 1
            return {"source": "independent-broker-fixture", "owner_id": fixture_id[:7],
                    "credential_id": fixture_id, "receipt_id": "receipt-" + fixture_id,
                    "issued_at": 2_000_000_000, "expires_at": 2_000_000_000 + duration,
                    "recipient_bound": self.defect != "unbound", "volatile_sink": True}

        def generic_evidence(self, attempt_id):
            return {"receipt": "metadata-only" if self.defect != "leak" else "private-owner-a-materialize"}

        def cleaned(self, attempt_id):
            return self.was_cleaned and self.defect != "cleanup_unknown"

    class Driver:
        isolated = True

        def __init__(self, observer):
            self.observer = observer

        def invoke(self, fixture_id, operation, arguments, attempt_id):
            assert operation in fixture_id and not {"user_id", "agent_id", "task_id"} & arguments.keys()
            assert arguments["use_seconds"] == (900 if operation == "assume_role" else 300)
            self.observer.calls[fixture_id] += 2 if self.observer.defect == "duplicate" else 1
            return {"receipt_id": "receipt-" + fixture_id, "status": "delivered"}

        def cleanup(self, attempt_id):
            self.observer.was_cleaned = True

    observer = Observer()
    result = broker_ac02(Driver(observer), observer)
    assert result["status"] == "pass" and result["cleanup"]["status"] == "confirmed"
    assert observer.calls == {fixture_id: 1 for fixture_id in observer.calls}
    isolated_observer = Observer()
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    runner.evaluate(
        "SCN-8073-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: Driver(isolated_observer), integration_observer=isolated_observer,
    )
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["scenario_id"] == "SCN-8073-02" and row["status"] == "pass"
    assert row["evidence_scope"] == "repository-integration"
    assert row["cleanup"] == {"status": "confirmed"}
    for defect in ("overlong", "shared_credential", "unbound", "leak", "duplicate"):
        broken = Observer(defect)
        assert broker_ac02(Driver(broken), broken)["status"] == "fail"
    unknown = Observer("cleanup_unknown")
    assert broker_ac02(Driver(unknown), unknown)["status"] == "blocked"


def test_broker_ac02_selector_requires_private_fixture(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-8073-02")
    assert not success
    assert report["selected_scenario_ids"] == ["SCN-8073-02"]
    assert report["scenarios"][0]["status"] == "blocked"
    assert report["scenarios"][0]["evidence_scope"] == "repository-integration"


def test_broker_ac03_isolated_denial_oracle_and_provider_counters(tmp_path, monkeypatch):
    from tests.e2e.tools.credential_integration import broker_ac03

    class Observer:
        def __init__(self, defect=None):
            self.defect = defect
            self.calls = {owner: 0 for owner in ("owner-a", "owner-b")}
            self.clock_reads = 0
            self.was_cleaned = False

        def clock(self, attempt_id):
            self.clock_reads += 1
            return 2_000_000_000 + (self.defect == "clock_changed" and self.clock_reads > 1)

        def expected(self, fixture_id, attempt_id):
            return {"source": "independent-broker-fixture", "owner_id": fixture_id,
                    "credential_id": "credential-" + fixture_id, "service": "service-" + fixture_id,
                    "label": "owned", "sentinel": "sentinel-" + fixture_id}

        def provider_calls(self, fixture_id, attempt_id):
            return self.calls[fixture_id]

        def generic_evidence(self, attempt_id):
            return {"receipt": "sentinel-owner-a" if self.defect == "leak" else "metadata-only"}

        def cleaned(self, attempt_id):
            return self.was_cleaned and self.defect != "cleanup_unknown"

    class Driver:
        isolated = True

        def __init__(self, observer):
            self.observer = observer

        def invoke(self, fixture_id, operation, arguments, attempt_id):
            if self.observer.defect == "provider_called":
                self.observer.calls[fixture_id] += 1
            if {"user_id", "agent_id", "task_id"} & arguments.keys():
                return {"status": "denied", "error": "unexpected" if self.observer.defect == "forgery_accepted" else "identity_binding_failed"}
            if operation == "list":
                return ["credential-owner-b"] if self.observer.defect == "foreign_visible" else []
            return {"status": "denied", "error": "unexpected" if self.observer.defect == "foreign_accepted" else "credential_not_found"}

        def cleanup(self, attempt_id):
            self.observer.was_cleaned = True

    observer = Observer()
    result = broker_ac03(Driver(observer), observer)
    assert result["status"] == "pass" and result["cleanup"]["status"] == "confirmed"
    assert result["observations"]["denied_attempts"] == 32
    assert observer.calls == {owner: 0 for owner in observer.calls}

    isolated_observer = Observer()
    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    runner.evaluate(
        "SCN-8073-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda *_: Driver(isolated_observer), integration_observer=isolated_observer,
    )
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["scenario_id"] == "SCN-8073-03" and row["status"] == "pass"
    assert row["evidence_scope"] == "repository-integration"
    assert row["cleanup"] == {"status": "confirmed"}

    for defect in ("provider_called", "forgery_accepted", "foreign_visible", "foreign_accepted", "leak", "clock_changed"):
        broken = Observer(defect)
        assert broker_ac03(Driver(broken), broken)["status"] == "fail"
    unknown = Observer("cleanup_unknown")
    assert broker_ac03(Driver(unknown), unknown)["status"] == "blocked"


def test_broker_ac03_selector_requires_private_fixture(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-8073-03")
    assert not success
    assert report["selected_scenario_ids"] == ["SCN-8073-03"]
    assert report["scenarios"][0]["status"] == "blocked"
    assert report["scenarios"][0]["evidence_scope"] == "repository-integration"


def test_broker_ac04_client_matrix_revocation_and_independent_oracles(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from tests.e2e.tools.credential_integration import OPERATIONS as BROKER_OPERATIONS
    from tests.e2e.tools.credential_integration import broker_ac04
    from tests.e2e.tools.test_client_matrix import matrix_fixture

    class Observer:
        def __init__(self, defect=None):
            self.defect = defect
            self.calls = {f"{client}-{operation}": 0 for client in runner.CLIENTS for operation in BROKER_OPERATIONS}
            self.cleaned_attempts = set()

        def pricing(self, client_name, attempt_id):
            quote = matrix_fixture()["all_in_quote"]
            quote["max_model_requests"] = 8
            if client_name == "cli":
                quote.update(max_model_requests=0, max_input_tokens=0, max_output_tokens=0,
                             input_token_usd="0", output_token_usd="0")
            changes = {
                "overhead": {"startup_usd": "4"}, "retries": {"max_retries": 1},
                "stale_quote": {"effective_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
                "unpriced_model": {"input_token_usd": "0"},
                "underpriced_model": {"max_model_requests": 7},
                "stdio_model": {"input_token_usd": "0.01"},
            }
            quote.update(changes.get(self.defect, {}))
            return {"source": "independent-test-pricing", "max_sessions": 1, "ttl_seconds": 300,
                    "cleanup_seconds": 600, "per_call_usd": "0" if self.defect == "unpriced" else "1",
                    "max_cell_usd": "6", "all_in_quote": None if self.defect == "no_quote" else quote}

        def client_version(self, client_name, attempt_id):
            return {"source": "independent-client-install", "name": client_name,
                    "version": "not-pinned" if self.defect == "version" else "fixture-v1"}

        def expected(self, fixture_id, attempt_id):
            client_name, operation = fixture_id.rsplit("-", 1)
            return {"source": "independent-broker-fixture", "client": client_name, "operation": operation,
                    "credential_id": "shared" if self.defect == "shared_credential" else fixture_id,
                    "service": "fixture", "label": fixture_id,
                    "sentinel": "PRIVATE_" + fixture_id, "receipt_id": "receipt-" + fixture_id,
                    "result": "harmless-response", "issued_at": 2000000000,
                    "expires_at": 2000000900 if operation == "assume_role" else 2000000300,
                    "use_seconds": 900 if operation == "assume_role" else 300}

        def provider_calls(self, fixture_id, attempt_id):
            return self.calls[fixture_id]

        def generic_evidence(self, attempt_id):
            return {"canary": "PRIVATE_cli-materialize"} if self.defect == "leak" else {"receipt": "metadata-only"}

        def session(self, attempt_id):
            return {"source": "independent-broker-fixture", "count": 2 if self.defect == "second_session" else 1,
                    "ttl_seconds": 300, "cleanup_seconds": 600}

        def expired(self, attempt_id, fixture_id=None, receipt_id=None):
            return self.defect != "unexpired"

        def cleaned(self, attempt_id):
            return attempt_id in self.cleaned_attempts and self.defect != "cleanup_unknown"

    class Driver:
        isolated = True
        authorized = True
        version = "fixture-v1"

        def __init__(self, observer, client_name):
            self.observer = observer
            self.client_name = client_name
            self.revoked = set()

        def discover(self, attempt_id):
            return [{"name": "credentials___" + operation, "inputSchema": {"properties": {}}}
                    for operation in BROKER_OPERATIONS]

        def invoke(self, operation, arguments, attempt_id):
            item = self.observer.expected(fixture_id=self.client_name + "-" + operation, attempt_id=attempt_id)
            if item["credential_id"] in self.revoked:
                if self.observer.defect == "provider_after_revoke":
                    self.observer.calls[item["credential_id"]] += 1
                return ({"status": "completed"} if self.observer.defect == "accepted_after_revoke" else
                        {"status": "denied", "error": "consent_revoked"})
            if operation != "list":
                self.observer.calls[item["credential_id"]] += 1
            if operation == "list":
                return [{"id": item["credential_id"], "service": item["service"], "label": item["label"]}]
            if operation == "proxy_request":
                return {"status": 200, "body": item["result"]}
            return {"status": "completed", "delivery": "private", "receipt_id": item["receipt_id"],
                    "expires_at": item["expires_at"]}

        def revoke(self, operation, arguments, attempt_id):
            self.revoked.add(arguments["credential_id"])
            return {"status": "revoked"}

        def cleanup(self, attempt_id):
            self.observer.cleaned_attempts.add(attempt_id)

    observer = Observer()
    for client_name in runner.CLIENTS:
        result = broker_ac04(Driver(observer, client_name), observer, client_name)
        assert result["status"] == "pass" and result["cleanup"]["status"] == "confirmed"
        assert result["observations"]["operations"] == list(BROKER_OPERATIONS)
        assert result["observations"]["pricing_source_revision"] == "a" * 40
        assert float(result["observations"]["estimated_all_in_usd"]) > 3
        assert "reserved_all_in_usd" not in result["observations"]
    assert all(value == (0 if name.endswith("-list") else 1) for name, value in observer.calls.items())

    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    integration_observer = Observer()
    runner.evaluate(
        "SCN-8073-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=FixtureClient,
        integration_driver_factory=lambda case, client_name, tool: Driver(integration_observer, client_name),
        integration_observer=integration_observer,
    )
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert {row["client"] for row in rows} == set(runner.CLIENTS)
    assert len(rows) == 3 and all(row["scenario_id"] == "SCN-8073-04" and row["status"] == "pass" and
                                  row["evidence_scope"] == "repository-integration" and
                                  row["cleanup"] == {"status": "confirmed"} for row in rows)

    for defect in ("version", "shared_credential", "leak", "second_session", "unexpired", "provider_after_revoke", "accepted_after_revoke"):
        broken = Observer(defect)
        assert broker_ac04(Driver(broken, "cli"), broken, "cli")["status"] == "fail"
    unknown = Observer("cleanup_unknown")
    assert broker_ac04(Driver(unknown, "cli"), unknown, "cli")["status"] == "blocked"
    unpriced = Observer("unpriced")
    with pytest.raises(runner.FixtureUnavailable, match="ceiling"):
        broker_ac04(Driver(unpriced, "cli"), unpriced, "cli")
    for client_name in runner.CLIENTS:
        defects = ("no_quote", "overhead", "retries", "stale_quote")
        defects += ("stdio_model",) if client_name == "cli" else ("unpriced_model", "underpriced_model")
        for defect in defects:
            broken = Observer(defect)
            with pytest.raises(runner.FixtureUnavailable, match="all-in broker pricing"):
                broker_ac04(Driver(broken, client_name), broken, client_name)
            assert not any(broken.calls.values()), (client_name, defect)


def test_broker_ac04_missing_protected_fixture_blocks_without_provider_calls(tmp_path, monkeypatch):
    success, report = evaluate(tmp_path, monkeypatch, pattern="SCN-8073-04")
    assert not success and report["selected_scenario_ids"] == ["SCN-8073-04"]
    assert len(report["scenarios"]) == len(runner.CLIENTS)
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "repository-integration" and
               row["cleanup"]["status"] == "not-run" for row in report["scenarios"])

    monkeypatch.setattr(runner, "verify_gateway", lambda _: None)
    runner.evaluate("SCN-8073-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                    command=["adp", "mcp"], client_factory=FixtureClient, live=True)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert len(rows) == len(runner.CLIENTS)
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "deployed-target" and
               "protected broker deployment" in row["reason"] for row in rows)


def test_tools_ci_installs_gateway_dev_extra_for_proof_guards():
    import yaml

    workflow = yaml.load(
        (Path(__file__).resolve().parents[3] / ".github/workflows/tools-ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    dependencies = next(step["run"] for step in workflow["jobs"]["verify"]["steps"]
                        if step.get("name") == "Install workflow test dependencies")
    assert "-e 'modules/gateway[dev]'" in dependencies
    assert "test_identity_proof_binding.py" in str(workflow["jobs"]["verify"]["steps"])
