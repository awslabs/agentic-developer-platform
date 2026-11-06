"""Offline readback fixtures use local Product/provider records and no AWS target."""

import base64
import ast
import copy
import hashlib
import importlib.util
import json
import sys
import types
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from l5_services.selector_receipts import SCENARIOS, SELECTOR_NODES, read_selector_receipts

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "security_release_readbacks", ROOT / "modules/L5-services/scripts/qualify-security-readbacks.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
SOURCE_CONTENT = b"fictional pinned source content"


def test_selected_receipt_nodes_match_the_live_runner_and_existing_tests():
    node = "modules/L5-services/tests/e2e/test_l5_security_acceptance.py"
    runner = ast.parse((ROOT / "modules/L5-services/tests/live/run_security_live.py").read_text())
    selectors = ast.literal_eval(next(
        assignment.value for assignment in runner.body if isinstance(assignment, ast.Assign)
        and isinstance(assignment.targets[0], ast.Name) and assignment.targets[0].id == "SELECTORS"))
    assert SELECTOR_NODES == {scenario: f"{node}::{name}" for scenario, name in selectors.items()}
    tests = ast.parse((ROOT / node).read_text())
    assert set(selectors.values()) <= {
        function.name for function in tests.body if isinstance(function, ast.FunctionDef)}


@pytest.mark.parametrize("fault", ["accepted", "accepted_long_running", "accepted_minute_summary", "accepted_hour_summary",
                                   "accepted_warning_summary", "malformed_duration", "missing", "foreign_source", "foreign_fixture",
                                   "wrong_attempt", "wrong_node", "missing_node", "stale", "altered_log",
                                   "public_receipt", "symlink", "rehashed_failure_log", "rehashed_skip_log",
                                   "out_of_order"])
def test_native_reader_binds_all_selected_live_selector_receipts(tmp_path, fault):
    tmp_path.chmod(0o700)
    environment = {"GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "2",
                   "SOURCE_SHA": "a" * 40, "L5_FIXTURE_DIGEST": "b" * 64}
    observed = datetime.now(UTC) - (timedelta(minutes=90) if fault == "accepted_long_running" else timedelta())
    for scenario in SCENARIOS:
        name = f"security-456-2-{scenario}"
        log = tmp_path / (name + ".log")
        summaries = {
            "accepted_minute_summary": b"1 passed in 61.24s (0:01:01)\n",
            "accepted_hour_summary": b"1 passed in 3601.24s (1:00:01)\n",
            "accepted_warning_summary": b"1 passed, 2 warnings in 94.01s (0:01:34)\n",
            "malformed_duration": b"1 passed in 61.24s (0:61:01)\n",
        }
        log.write_bytes(summaries.get(fault, b"1 passed\n"))
        log.chmod(0o600)
        receipt = {"scenario": scenario, "run_id": "456", "run_attempt": "2",
                   "selector_node": SELECTOR_NODES[scenario],
                   "source_revision": environment["SOURCE_SHA"],
                   "fixture_sha256": environment["L5_FIXTURE_DIGEST"],
                   "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                   "result": "passed", "observed_at": observed.isoformat()}
        if scenario == "context":
            if fault == "foreign_source":
                receipt["source_revision"] = "f" * 40
            elif fault == "foreign_fixture":
                receipt["fixture_sha256"] = "f" * 64
            elif fault == "wrong_attempt":
                receipt["run_attempt"] = "3"
            elif fault == "stale":
                receipt["observed_at"] = (datetime.now(UTC) - timedelta(minutes=121)).isoformat()
            elif fault == "wrong_node":
                receipt["selector_node"] = SELECTOR_NODES["setup"]
            elif fault == "missing_node":
                receipt.pop("selector_node")
            elif fault == "out_of_order":
                receipt["observed_at"] = (observed - timedelta(seconds=1)).isoformat()
            elif fault in {"rehashed_failure_log", "rehashed_skip_log"}:
                log.write_bytes(b"1 passed, 1 failed\n" if fault == "rehashed_failure_log"
                                else b"1 passed, 1 skipped\n")
                receipt["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(receipt) + "\n")
        path.chmod(0o600)
        if scenario == "context":
            if fault == "missing":
                path.unlink()
            elif fault == "altered_log":
                log.write_bytes(b"0 passed\n")
            elif fault == "public_receipt":
                path.chmod(0o644)
            elif fault == "symlink":
                path.unlink()
                path.symlink_to(log)
    if not fault.startswith("accepted"):
        with pytest.raises((ValueError, OSError)):
            read_selector_receipts(tmp_path, environment, datetime.now(UTC))
    else:
        assert set(read_selector_receipts(tmp_path, environment, datetime.now(UTC))) == set(SCENARIOS)


@pytest.mark.parametrize("fault", ["accepted", "wrong_role", "wrong_region", "foreign_cluster",
                                     "wrong_image", "wrong_tag", "missing_pods", "wrong_receipt_source",
                                     "wrong_receipt_digest", "changed_ecr_digest", "invalid_wheel"])
def test_gateway_readback_requires_selected_identity_and_serving_image(fault):
    account = "123456789012"
    source = "a" * 40
    digest = "sha256:" + "b" * 64
    environment = {"AWS_REGION": "us-east-1", "L5_MUTATION_ROLE_ARN":
                   f"arn:aws:iam::{account}:role/fictional-deployment", "SOURCE_SHA": source,
                   "L5_ENVIRONMENT": "dev", "WHEEL_DIGEST": "a" * 64}
    principal = {"Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/fictional-deployment/run"}
    cluster = {"arn": f"arn:aws:eks:us-east-1:{account}:cluster/adp-dev-eks-cluster", "status": "ACTIVE"}
    image = {"imageDigest": digest, "imageTags": [source]}
    deployment = {"source_revision": source, "image_digest": digest}
    if fault == "wrong_role":
        principal["Arn"] = f"arn:aws:sts::{account}:assumed-role/other/run"
    elif fault == "wrong_region":
        environment["AWS_REGION"] = "us-west-2"
    elif fault == "foreign_cluster":
        cluster["arn"] = f"arn:aws:eks:us-east-1:{account}:cluster/other"
    elif fault == "wrong_image":
        image["imageDigest"] = "latest"
    elif fault == "wrong_tag":
        image["imageTags"] = ["latest"]
    elif fault == "wrong_receipt_source":
        deployment["source_revision"] = "f" * 40
    elif fault == "wrong_receipt_digest":
        deployment["image_digest"] = "latest"
    elif fault == "changed_ecr_digest":
        image["imageDigest"] = "sha256:" + "c" * 64
    elif fault == "invalid_wheel":
        environment["WHEEL_DIGEST"] = "latest"
    calls = []

    class FakeSession:
        region_name = "us-east-1"

        def client(self, service):
            if service == "sts":
                return SimpleNamespace(get_caller_identity=lambda: principal)
            if service == "eks":
                return SimpleNamespace(describe_cluster=lambda **kwargs: {"cluster": cluster})
            if service == "ecr":
                return SimpleNamespace(describe_images=lambda **kwargs: {"imageDetails": [image]})
            raise AssertionError(service)

    class FakeKubernetes:
        def __init__(self, session, description, namespace):
            assert session.region_name == "us-east-1" and namespace == "adp-gateway"
            assert description == cluster

        def gateway(self, **kwargs):
            calls.append(kwargs)
            return ([] if fault == "missing_pods" else ["pod-one"], "migration-head")

        def close(self):
            calls.append("closed")

    if fault == "accepted":
        result = reader.gateway_status(environment, session=FakeSession(), kubernetes=FakeKubernetes,
                                       deployment=deployment)
        assert result["revision"] == source and result["image_digest"] == digest
        assert result["wheel_sha256"] == "a" * 64
        assert result["pod_uids"] == ["pod-one"]
        assert calls == [{"digest": digest, "source_revision": source, "account": account,
                          "region": "us-east-1", "wheel_sha256": "a" * 64}, "closed"]
    else:
        with pytest.raises(ValueError):
            reader.gateway_status(environment, session=FakeSession(), kubernetes=FakeKubernetes,
                                  deployment=deployment)
        if fault == "missing_pods":
            assert calls[-1] == "closed"


def test_gateway_release_receipt_is_resolved_from_authenticated_history(monkeypatch):
    environment = {"L5_MUTATION_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment",
                   "L5_ENVIRONMENT": "dev", "AWS_REGION": "us-east-1", "GITHUB_REPOSITORY_ID": "123",
                   "GITHUB_REPOSITORY": "example/project", "GITHUB_RUN_ID": "456",
                   "GH_TOKEN": "fictional-private-token", "PROVIDER_SECRET": "fictional-provider-secret"}

    def invoke(command, **kwargs):
        assert command[-1] == "readback"
        assert kwargs["env"]["ADP_RECEIPT_ROLE"] == environment["L5_MUTATION_ROLE_ARN"]
        assert kwargs["env"]["ADP_RECEIPT_TARGET"] == "adp-gateway-deploy-dev"
        assert kwargs["env"]["GH_TOKEN"] == "fictional-private-token"
        assert "PROVIDER_SECRET" not in kwargs["env"]
        assert kwargs["capture_output"] and kwargs["check"] and kwargs["timeout"] == 120
        return SimpleNamespace(stdout=b'{"source_revision":"' + b"a" * 40 + b'","image_digest":"sha256:'
                               + b"b" * 64 + b'"}')

    monkeypatch.setattr(reader.subprocess, "run", invoke)
    assert reader.gateway_deployment(environment)["source_revision"] == "a" * 40
    with pytest.raises(ValueError, match="authenticated"):
        reader.gateway_deployment({**environment, "GH_TOKEN": ""})


def test_gateway_token_does_not_enter_product_or_provider_subprocesses(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fictional-private-token")
    with pytest.raises(FileNotFoundError):
        reader.main({"PRIVATE_EVIDENCE_DIR": str(tmp_path / "no-private-fixture")})
    assert "GH_TOKEN" not in reader.os.environ


@pytest.fixture
def setup_readback(tmp_path):
    from clients_setup import LiveSetupDriver

    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    sources = [{"tenant_id": "tenant-example", "project_id": "project-example",
                "record_type": "document", "record_id": "source-example", "record_revision": "revision-one"}]
    selected = {"scope": scope, "configuration_id": "config-example", "binding_id": "binding-example",
                "sources": sources, "binding_state": "ready", "readiness": [],
                "connection_ref": {"id": "credential-example"},
                "provider_identity": {"account_id": "123456789012"}}
    case = {"scope": scope, "selected": selected, "run_input": {"sources": sources, "binding_id": "binding-example"},
            "cli_receipt": "cli-example", "ui_receipt": "ui-example", "mcp_receipt": "mcp-example",
            "journal_file": str(tmp_path / "setup.jsonl"), "ui_region": "region-example"}
    for operation, receipt, effect in (("cli_update", "cli-example", "config-example"),
                                       ("ui_edit", "ui-example", "config-example"),
                                       ("mcp_submit", "mcp-example", "run-example")):
        for status in ("prepared", "acknowledged"):
            reader.AttemptJournal(case["journal_file"]).append({
                "operation": operation, "attempt_id": receipt, "work_id": receipt,
                "status": status, "effect_id": effect if status == "acknowledged" else None,
                "idempotency_key": receipt if status == "acknowledged" else None,
                "observed_at": datetime.now(UTC).isoformat()})
    records = {
        "configuration": {"scope": scope, "configuration_id": "config-example", "revision": 3,
                          "settings": {"region": "region-example"}},
        "binding": {"scope": scope, "binding_id": "binding-example", "configuration_id": "config-example",
                    "revision": 2, "connection_ref": selected["connection_ref"], "provider_identity":
                    selected["provider_identity"], "state": "ready", "readiness": []},
        "run": {"scope": scope, "run_id": "run-example", "revision": 1,
                "input": {"binding_id": "binding-example", "sources": sources},
                "admission": {"configuration_id": "config-example", "configuration_revision": 3}},
    }

    class FakeDriver:
        attempts = LiveSetupDriver.attempts

        def __init__(self):
            self.case = case
            self.journal = reader.AttemptJournal(case["journal_file"])

        def read_api(self, path):
            if path.endswith("/configuration"):
                return copy.deepcopy(records["configuration"])
            if path.endswith("/bindings/binding-example"):
                return copy.deepcopy(records["binding"])
            if path.endswith("/runs/run-example"):
                return copy.deepcopy(records["run"])
            raise AssertionError("foreign Product readback")

        def cli_observations(self, binding_id, run_id):
            return copy.deepcopy(records)

        def ui_observations(self, binding_id, run_id):
            return copy.deepcopy(records)

        def mcp_observations(self, binding_id, run_id):
            return copy.deepcopy(records)

    return case, records, FakeDriver()


@pytest.mark.parametrize("fault", ["accepted", "duplicate_attempt", "missing_attempt", "foreign_scope",
                                     "changed_source", "changed_ui", "wrong_receipt", "wrong_ui_region"])
def test_post_selector_setup_parity_uses_fresh_product_and_client_readbacks(setup_readback, fault):
    case, records, driver = setup_readback
    if fault == "duplicate_attempt":
        reader.AttemptJournal(case["journal_file"]).append({
            "operation": "mcp_submit", "attempt_id": "mcp-example", "work_id": "mcp-example",
            "status": "acknowledged", "effect_id": "other", "observed_at": datetime.now(UTC).isoformat()})
    elif fault == "missing_attempt":
        journal = Path(case["journal_file"])
        journal.write_text("".join(journal.read_text().splitlines(keepends=True)[:-1]))
    elif fault == "foreign_scope":
        records["configuration"]["scope"] = {**case["scope"], "product_id": "foreign"}
    elif fault == "changed_source":
        records["run"]["input"]["sources"] = [{**case["selected"]["sources"][0],
                                                     "record_revision": "foreign"}]
    elif fault == "changed_ui":
        driver.ui_observations = lambda binding_id, run_id: {
            **copy.deepcopy(records), "configuration": {**records["configuration"], "revision": 4}}
    elif fault == "wrong_receipt":
        case["ui_receipt"] = "other"
    elif fault == "wrong_ui_region":
        case["ui_region"] = "another-region"
    if fault == "accepted":
        result = reader.verify_setup_readback(case, 3, driver)
        assert result["run_id"] == "run-example" and result["configuration_revision"] == 3
    else:
        with pytest.raises((ValueError, AssertionError)):
            reader.verify_setup_readback(case, 3, driver)


@pytest.mark.parametrize("fault", ["accepted", "wrong_client", "missing_token", "mismatch"])
def test_setup_readback_uses_manual_evidence_and_read_only_clients(setup_readback, monkeypatch, fault):
    from clients_setup import LiveSetupDriver
    import security_manual_setup as manual

    case, records, sample = setup_readback
    case.update(api_url="https://api.example.invalid", ui_url="https://ui.example.invalid",
                api_token_env="SETUP_API_TOKEN", mcp_token_env="SETUP_MCP_TOKEN",
                ui_access_token_env="SETUP_UI_ACCESS", ui_id_token_env="SETUP_UI_ID")
    credentials = {case[field]: "fictional-token-" + field for field in (
        "api_token_env", "mcp_token_env", "ui_access_token_env", "ui_id_token_env")}
    if fault == "missing_token":
        credentials.pop(case["api_token_env"])
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail("independent readback entered browser or mutation code")

    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=forbidden))
    monkeypatch.setattr(LiveSetupDriver, "ui_observations", forbidden)
    monkeypatch.setattr(manual.ManualSetupDriver, "ui_edit", forbidden)
    monkeypatch.setattr(manual.ManualSetupDriver, "mcp_submit", forbidden)
    limits = {"max_attempts": 3, "deadline_seconds": 60}
    fixture = {"cases": [case], "limits": limits}
    monkeypatch.setattr(reader, "load_live_fixture", lambda path: fixture)
    # This is adapter/readback unit coverage. The actual protected import and
    # two-tenant SQL authorization are exercised by Gateway exchange tests.
    monkeypatch.setattr(manual, "verify_fixture", lambda *args: ({}, "a" * 64, datetime.now(UTC) + timedelta(minutes=5)))
    monkeypatch.setattr(manual, "private_events", lambda path: [])
    monkeypatch.setenv("PRIVATE_FIXTURE", "/offline/fixture.json")

    class FakeHttpClient:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == case["api_url"]
            assert kwargs["follow_redirects"] is False
            calls.append("client-open")

        def get(self, path, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: sample.read_api(path))

        def close(self):
            calls.append("client-close")

    def collect(self, selected):
        assert self.readback is True and selected == {"phase": "display", "run_id": "run-example"}
        calls.append("fresh-import")
        snapshot = copy.deepcopy(records)
        if fault == "wrong_client":
            snapshot["configuration"]["revision"] = 4
        return {"events": [{"document": {"observation": {
            "observation": "mismatch" if fault == "mismatch" else "displayed"}}}],
            "grant": {"snapshot": snapshot}}

    monkeypatch.setattr(reader.httpx, "Client", FakeHttpClient)
    monkeypatch.setattr(LiveSetupDriver, "cli_observations", lambda self, binding, run: copy.deepcopy(records))
    monkeypatch.setattr(LiveSetupDriver, "mcp_observations", lambda self, binding, run: copy.deepcopy(records))
    monkeypatch.setattr(manual.ManualSetupDriver, "selected", lambda self, phase, run_id: {"phase": phase, "run_id": run_id})
    monkeypatch.setattr(manual.ManualSetupDriver, "collect", collect)
    if fault == "accepted":
        assert reader.read_setup(case, limits, credentials)["run_id"] == "run-example"
        assert calls == ["client-open", "client-open", "fresh-import", "client-close", "client-close"]
    else:
        with pytest.raises((KeyError, ValueError, AssertionError)):
            reader.read_setup(case, limits, credentials)
        assert calls[-2:] == ["client-close", "client-close"] or fault == "missing_token"
    assert not any(key in reader.os.environ for key in credentials)


@pytest.mark.parametrize("fault", ["accepted", "missing_token", "verification_failed"])
def test_context_reader_assembles_read_only_driver_and_restores_credentials(monkeypatch, fault):
    from context_live import LiveContextDriver

    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"}
    selection = {"assessment_plan": {}}
    fields = ("publication_token_env", "context_token_env", "agent_read_token_env",
              "visibility_token_env", "audit_token_env", "foreign_product_token_env",
              "revoked_token_env")
    for index, field in enumerate(fields):
        selection[field] = "CONTEXT_TOKEN_" + str(index)
    case = {"scope": scope, "context_reuse": selection, "api_url": "https://api.example.invalid",
            "api_token_env": "API_TOKEN"}
    credentials = {name: "fictional-" + name for name in ("API_TOKEN", *[selection[field] for field in fields])}
    if fault == "missing_token":
        credentials.pop(selection["publication_token_env"])
    calls = []

    class FakeClient:
        def __init__(self, **options):
            assert options["follow_redirects"] is False
            assert options["headers"]["Authorization"] == "Bearer fictional-API_TOKEN"
            calls.append("client-open")

        def close(self):
            calls.append("client-close")

    def verify(selected_case, foreign, driver):
        assert isinstance(driver, LiveContextDriver)
        assert selected_case is case and foreign["scope"]["tenant_id"] != scope["tenant_id"]
        assert driver.assessment.read_api is driver.read_api
        assert not hasattr(driver, "journal")
        assert all(reader.os.environ[name] == value for name, value in credentials.items())
        calls.append("snapshot-read")
        if fault == "verification_failed":
            raise ValueError("independent Context read failed")
        return {"scope": scope}

    monkeypatch.setattr(reader.httpx, "Client", FakeClient)
    monkeypatch.setattr(reader, "api_reader", lambda client: object())
    monkeypatch.setattr(reader, "verify_context_snapshot", verify)
    monkeypatch.setenv("CONTEXT_TEST_ORIGINAL", "retained")
    if fault == "accepted":
        assert reader.read_context(case, {"scope": {**scope, "tenant_id": "tenant-two"}},
                                   {"deadline_seconds": 60}, credentials) == {"scope": scope}
        assert calls == ["client-open", "snapshot-read", "client-close"]
    else:
        with pytest.raises((KeyError, ValueError)):
            reader.read_context(case, {"scope": {**scope, "tenant_id": "tenant-two"}},
                                {"deadline_seconds": 60}, credentials)
        assert calls == ([] if fault == "missing_token" else ["client-open", "snapshot-read", "client-close"])
    assert reader.os.environ["CONTEXT_TEST_ORIGINAL"] == "retained"
    assert not any(name in reader.os.environ for name in credentials)


@pytest.fixture
def readbacks(tmp_path):
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    provider = {"label": "fictional-selected", "account_id": "123456789012",
                "role_arn": "arn:aws:iam::123456789012:role/fictional-selected", "region": "us-east-1"}
    case = {"scope": scope, "provider": provider, "assessment_journal_file": str(tmp_path / "assessments.jsonl"),
            "assessments": []}
    records = {}
    native = {}
    base = "/v1/products/product-example/projects/project-example/services/security"
    journal = reader.AttemptJournal(case["assessment_journal_file"])
    for capability, record_type in (("code_review", "repository_commit"), ("threat_model", "document")):
        run_id = capability + "-run"
        source = {**scope, "record_type": record_type, "record_id": capability + "-source",
                  "record_revision": capability + "-revision"}
        binding_id = capability + "-binding"
        ref = {"record_id": capability + "-job", "agent_space_id": capability + "-space"}
        if capability == "code_review":
            ref["record_revision"] = capability + "-review"
        plan = {"receipt": capability + "-receipt", "capability": capability, "binding_id": binding_id,
                "input": {"sources": [source]},
                "source_readback": [{"source_ref": source, "url": "https://example.invalid/" + source["record_revision"],
                                     "max_bytes": 1024, "token_env": "SOURCE_TOKEN"}],
                "review_file": str(tmp_path / (capability + "-rubric.json"))}
        case["assessments"].append(plan)
        journal.append({"attempt_id": plan["receipt"], "operation": "assessment_submit", "status": "prepared",
                        "work_id": plan["receipt"], "observed_at": datetime.now(UTC).isoformat()})
        journal.append({"attempt_id": plan["receipt"], "operation": "assessment_submit", "status": "acknowledged",
                        "work_id": plan["receipt"], "effect_id": run_id, "idempotency_key": plan["receipt"],
                        "observed_at": datetime.now(UTC).isoformat()})
        run = {"scope": scope, "run_id": run_id, "input": {"sources": [source], "capability": capability,
                "binding_id": binding_id}, "state": "succeeded", "provider_state": "completed",
                "native_job_ref": ref, "admission": {"provider_identity": {
                    key: provider[key] for key in ("account_id", "role_arn", "region")},
                    "native_space_ref": {"record_id": capability + "-space"},
                    "source_snapshots": [{"source_ref": source, "content": {"bytes": len(SOURCE_CONTENT),
                         "sha256": hashlib.sha256(SOURCE_CONTENT).hexdigest()}}]}}
        result_id = capability + "-result"
        rubric = {"run_id": run_id, "result_id": result_id, "native_job_ref": ref,
                  "rubric_revision": "independent-review", "review_sample": [],
                  "known_controls": ["fictional-reviewed-control"], "reviews": {}}
        Path(plan["review_file"]).write_text(json.dumps(rubric))
        Path(plan["review_file"]).chmod(0o600)
        result = {"scope": scope, "result_id": result_id, "run_id": run_id, "native_job_ref": ref,
                  "finding_count": 0, "coverage": "complete"}
        records[base + "/runs/" + run_id] = run
        records[base + "/bindings/" + binding_id] = {"scope": scope, "binding_id": binding_id,
            "state": "ready", "gaps": [], "provider_identity": run["admission"]["provider_identity"],
            "native_resource_ref": {"record_id": capability + "-space"}}
        records[base + "/results/" + result_id] = result
        records[base + "/results/" + result_id + "/findings?limit=100"] = {"items": []}
        native[run_id] = {"native_job_ref": ref, "status": "COMPLETED", "findings_coverage": "complete",
                          "findings": []}
    records[base + "/results?limit=100"] = {"items": [
        {"run_id": capability + "-run", "result_id": capability + "-result"}
        for capability in ("code_review", "threat_model")]}
    return case, records, native


@pytest.fixture
def trigger_readback(readbacks, tmp_path):
    case, records, native = readbacks
    plan = copy.deepcopy(case["assessments"][0])
    source = {**plan["input"]["sources"][0], "record_revision": "event-revision"}
    plan.update(event_id="event-example", receipt_url="https://example.invalid/events/event-example",
                token_env="EVENT_TOKEN", input={"capability": plan["capability"],
                                                  "binding_id": plan["binding_id"], "sources": [source]},
                source_readback=[{**plan["source_readback"][0], "source_ref": source}],
                review_file=str(tmp_path / "event-rubric.json"))
    case["trigger_event"] = plan
    base = "/v1/products/product-example/projects/project-example/services/security"
    run = copy.deepcopy(records[base + "/runs/code_review-run"])
    run.update(run_id="event-run", input={**run["input"], "sources": [source]},
               native_job_ref={**run["native_job_ref"], "record_id": "event-job"})
    run["admission"].update(event_id=plan["event_id"], source_snapshots=[{
        "source_ref": source, "content": {"bytes": len(SOURCE_CONTENT),
                                     "sha256": hashlib.sha256(SOURCE_CONTENT).hexdigest()}}])
    result = copy.deepcopy(records[base + "/results/code_review-result"])
    result.update(run_id="event-run", result_id="event-result", native_job_ref=run["native_job_ref"])
    records[base + "/runs/event-run"] = run
    records[base + "/results/event-result"] = result
    records[base + "/results/event-result/findings?limit=100"] = {"items": []}
    records[base + "/results?limit=100"]["items"].append({"run_id": "event-run", "result_id": "event-result"})
    native["event-run"] = {**native["code_review-run"], "native_job_ref": run["native_job_ref"]}
    Path(plan["review_file"]).write_text(json.dumps({
        "run_id": "event-run", "result_id": "event-result", "native_job_ref": run["native_job_ref"],
        "rubric_revision": "reviewed-event", "review_sample": [],
        "known_controls": ["fictional-reviewed-control"], "reviews": {}}))
    Path(plan["review_file"]).chmod(0o600)
    journal = reader.AttemptJournal(case["assessment_journal_file"])
    baseline_path = Path(case["assessment_journal_file"] + ".event-before.json")
    baseline_path.write_text(json.dumps({"event_id": "event-example", "scope": case["scope"],
        "run_id": "code_review-run", "space": "code_review-space",
        "jobs": [records[base + "/runs/code_review-run"]["native_job_ref"]],
        "observed_at": datetime.now(UTC).isoformat()}) + "\n")
    baseline_path.chmod(0o600)
    for attempt in range(2):
        for status in ("prepared", "acknowledged"):
            journal.append({"attempt_id": f"event-example-{attempt}", "operation": "event_delivery",
                            "status": status, "work_id": "event-example",
                            "effect_id": "event-run" if status == "acknowledged" else None,
                            "observed_at": str(datetime.now(UTC).timestamp())})
    return case, records, native


@pytest.mark.parametrize("fault", ["accepted", "foreign_receipt", "different_duplicate",
                                   "wrong_run", "wrong_source", "wrong_native", "missing_result",
                                   "truncated_journal", "foreign_rubric", "missing_baseline",
                                   "foreign_baseline", "duplicate_native_job", "missing_native_job",
                                   "missing_old_job", "foreign_submit", "wrong_submit_run",
                                   "late_submit", "swapped_submits", "foreign_provider_binding",
                                   "unready_event_binding"])
def test_trigger_readback_requires_duplicate_receipt_and_native_result(trigger_readback, fault):
    case, records, native = trigger_readback
    base = "/v1/products/product-example/projects/project-example/services/security"
    receipt = {"event_id": "event-example", "run_id": "event-run", "state": "admitted"}
    if fault == "foreign_receipt":
        receipt["run_id"] = "foreign-run"
    elif fault == "different_duplicate":
        entries = reader.AttemptJournal(case["assessment_journal_file"]).read()
        entries[-1]["effect_id"] = "second-run"
        Path(case["assessment_journal_file"]).write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    elif fault == "wrong_run":
        records[base + "/runs/event-run"]["admission"]["event_id"] = "foreign-event"
    elif fault == "wrong_source":
        records[base + "/runs/event-run"]["admission"]["source_snapshots"][0]["content"]["sha256"] = "0" * 64
    elif fault == "foreign_provider_binding":
        records[base + "/runs/event-run"]["admission"]["provider_identity"]["role_arn"] = "foreign-provider"
    elif fault == "unready_event_binding":
        records[base + "/bindings/code_review-binding"]["state"] = "unready"
    elif fault == "wrong_native":
        native["event-run"]["native_job_ref"] = {"record_id": "foreign-job"}
    elif fault == "missing_result":
        records[base + "/results?limit=100"]["items"].pop()
    elif fault == "truncated_journal":
        with Path(case["assessment_journal_file"]).open("a") as output:
            output.write("{")
    elif fault == "foreign_rubric":
        Path(case["trigger_event"]["review_file"]).write_text("{}")
    elif fault == "missing_baseline":
        Path(case["assessment_journal_file"] + ".event-before.json").unlink()
    elif fault == "foreign_baseline":
        baseline_path = Path(case["assessment_journal_file"] + ".event-before.json")
        baseline = json.loads(baseline_path.read_text())
        baseline["space"] = "another-native-space"
        baseline_path.write_text(json.dumps(baseline) + "\n")
    elif fault in {"foreign_submit", "wrong_submit_run", "late_submit", "swapped_submits"}:
        entries = reader.AttemptJournal(case["assessment_journal_file"]).read()
        if fault == "foreign_submit":
            entries[1]["attempt_id"] = "foreign-receipt"
        elif fault == "wrong_submit_run":
            entries[1]["effect_id"] = "threat_model-run"
        elif fault == "late_submit":
            entries[1]["observed_at"] = "2099-01-01T00:00:00+00:00"
        else:
            entries[:4] = entries[2:4] + entries[:2]
        Path(case["assessment_journal_file"]).write_text("".join(
            json.dumps(entry) + "\n" for entry in entries))

    def client_factory(**kwargs):
        assert kwargs == {"timeout": 20, "follow_redirects": False}

        def handle(request):
            assert str(request.url) == case["trigger_event"]["receipt_url"]
            assert request.headers["Authorization"] == "Bearer fictional-event-token"
            assert request.headers["Cache-Control"] == "no-cache"
            return httpx.Response(200, json=receipt)

        return httpx.Client(transport=httpx.MockTransport(handle))

    def observe():
        before = records[base + "/runs/code_review-run"]["native_job_ref"]
        event_job = records[base + "/runs/event-run"]["native_job_ref"]
        jobs = ([before] if fault == "missing_native_job" else [before, event_job])
        if fault == "missing_old_job":
            jobs = [event_job]
        if fault == "duplicate_native_job":
            jobs.append(event_job)
        return reader.read_trigger(case, {"EVENT_TOKEN": "fictional-event-token"},
            lambda path: records[path], lambda run, provider: native[run["run_id"]],
            lambda run: [{"source_ref": source, "content": SOURCE_CONTENT}
                         for source in run["input"]["sources"]], client_factory=client_factory,
            inventory=lambda run, provider: jobs)

    if fault == "accepted":
        result = observe()
        assert result["event_id"] == "event-example" and result["run_id"] == "event-run"
        assert result["binding_id"] == "code_review-binding"
        assert result["native_finding_ids"] == [item["native_id"] for item in native["event-run"]["findings"]]
        assert result["provider_identity"] == {field: case["provider"][field]
                                                for field in ("account_id", "role_arn", "region")}
        assert result["journal_sha256"] and result["source_sha256"] == [hashlib.sha256(SOURCE_CONTENT).hexdigest()]
        after_path = Path(case["assessment_journal_file"] + ".event-after.json")
        after = json.loads(after_path.read_text())
        assert datetime.fromisoformat(result["observed_at"]) >= datetime.fromisoformat(after["observed_at"])
        assert result["after_sha256"] == hashlib.sha256(after_path.read_bytes()).hexdigest()
    else:
        with pytest.raises((ValueError, AssertionError, KeyError, OSError, json.JSONDecodeError)):
            observe()


def test_post_selector_readback_requires_two_current_product_and_provider_jobs(readbacks):
    case, records, native = readbacks
    visited = []

    def read_api(path):
        visited.append(path)
        return records[path]

    observed = reader.assess_case(case, read_api, lambda run, provider: native[run["run_id"]],
                                  lambda run: [{"source_ref": source, "content": SOURCE_CONTENT}
                                               for source in run["input"]["sources"]])
    assert {item["capability"] for item in observed} == {"code_review", "threat_model"}
    assert all(item["sources"] for item in observed)
    assert all(item["native_finding_ids"] == [finding["native_id"] for finding in native[item["run_id"]]["findings"]] for item in observed)
    assert all(item["binding_id"] == item["capability"] + "-binding"
               and item["provider_identity"] == {field: case["provider"][field]
                                                 for field in ("account_id", "role_arn", "region")}
               for item in observed)
    assert all(item["rubric_sha256"] and item["source_sha256"] == item["snapshot_sha256"] == [
        hashlib.sha256(SOURCE_CONTENT).hexdigest()] for item in observed)
    assert len(visited) >= 10


@pytest.mark.parametrize("fault", ["changed_source_bytes", "changed_snapshot", "missing_source",
                                   "foreign_review", "missing_review", "public_review", "unreviewed_controls",
                                   "wrong_source_type", "missing_review_file"])
def test_post_selector_readback_rejects_unverified_sources_and_rubrics(readbacks, fault):
    case, records, native = readbacks
    base = "/v1/products/product-example/projects/project-example/services/security"
    plan = case["assessments"][0]
    content = SOURCE_CONTENT
    if fault == "changed_source_bytes":
        content = b"another source's bytes"
    elif fault == "changed_snapshot":
        records[base + "/runs/code_review-run"]["admission"]["source_snapshots"][0]["content"]["sha256"] = "0" * 64
    elif fault == "missing_source":
        content = None
    elif fault == "wrong_source_type":
        plan["input"]["sources"][0]["record_type"] = "document"
    else:
        review_path = Path(plan["review_file"])
        if fault == "missing_review_file":
            review_path.unlink()
        elif fault == "public_review":
            review_path.chmod(0o644)
        else:
            rubric = json.loads(review_path.read_text())
            if fault == "foreign_review":
                rubric["run_id"] = "foreign-run"
            elif fault == "missing_review":
                rubric["reviews"] = []
            else:
                rubric["known_controls"] = []
            review_path.write_text(json.dumps(rubric))
    with pytest.raises((ValueError, AssertionError, OSError)):
        reader.assess_case(case, lambda path: records[path], lambda run, provider: native[run["run_id"]],
                           lambda run: ([{"source_ref": source, "content": content}
                                         for source in run["input"]["sources"]] if content is not None else []))


@pytest.mark.parametrize("fault", ["accepted", "wrong_revision", "foreign_snapshot", "oversize",
                                   "missing", "bad_etag"])
def test_source_transport_uses_pinned_product_api_and_current_authorization(fault):
    source = {"tenant_id": "tenant-example", "project_id": "project-example",
              "source_connection_id": "source-example", "record_type": "document",
              "record_id": "architecture-example", "record_revision": "revision-example"}
    scope = {"tenant_id": source["tenant_id"], "product_id": "product-example",
             "project_id": source["project_id"]}
    inventory = json.dumps({"source_ref": source, "files": []}).encode()
    def artifact(content):
        return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    snapshot = {"snapshot_id": "snapshot-example", "source_ref": source,
                "content": artifact(SOURCE_CONTENT), "file_manifest": artifact(inventory)}
    run = {"run_id": "run-example", "scope": scope, "input": {"sources": [source]},
           "admission": {"source_snapshots": [snapshot]}}
    manifest = {"run_id": run["run_id"], "scope": scope, "sources": [source],
                "source_snapshot_ids": [snapshot["snapshot_id"]]}
    if fault == "wrong_revision":
        manifest["sources"] = [{**source, "record_revision": "another-revision"}]
    if fault == "foreign_snapshot":
        snapshot["source_ref"] = {**source, "tenant_id": "other"}
    if fault == "oversize":
        snapshot["content"]["bytes"] = len(SOURCE_CONTENT) - 1
    base = "/v1/products/product-example/projects/project-example/services/security/runs/run-example"
    calls = []

    def respond(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer fictional-product-token"
        if request.url.path == base + "/source-manifest":
            return httpx.Response(200, json=manifest)
        if request.url.path == base + "/source-snapshots/snapshot-example":
            return httpx.Response(200, json=snapshot)
        if request.url.path.endswith("/manifest"):
            return httpx.Response(200, content=inventory, headers={"ETag": f'"{artifact(inventory)["sha256"]}"'})
        if fault == "missing":
            return httpx.Response(404)
        return httpx.Response(200, content=SOURCE_CONTENT, headers={"ETag": (
            '"invalid"' if fault == "bad_etag" else f'"{artifact(SOURCE_CONTENT)["sha256"]}"')})

    with httpx.Client(transport=httpx.MockTransport(respond), base_url="https://example.invalid",
                      headers={"Authorization": "Bearer fictional-product-token"}) as client:
        if fault == "accepted":
            assert reader.product_sources(run, client) == [{"source_ref": source, "content": SOURCE_CONTENT}]
            assert calls == [base + "/source-manifest", base + "/source-snapshots/snapshot-example",
                             base + "/source-snapshots/snapshot-example/manifest",
                             base + "/source-snapshots/snapshot-example/content"]
        else:
            with pytest.raises((ValueError, httpx.HTTPStatusError)):
                reader.product_sources(run, client)


@pytest.mark.parametrize("fault", ["accepted", "prepared_only", "duplicate", "foreign_run",
                                   "foreign_journal_run",
                                   "foreign_owner", "owned_present", "linked_missing"])
def test_post_selector_cleanup_requires_native_owned_and_linked_readback(tmp_path, fault):
    journal_file = tmp_path / "cleanup.jsonl"
    selection = {"journal_file": str(journal_file), "owned_binding_id": "owned-binding", "owned": ["owned-resource"], "linked": ["linked-resource"]}
    case = {"recovery": selection, "api_url": "https://example.invalid", "api_token_env": "API_TOKEN",
            "scope": {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"},
            "selected": {"configuration_id": "configuration-example"}}
    journal = reader.AttemptJournal(journal_file)
    for status in ("prepared", "acknowledged") if fault != "prepared_only" else ("prepared",):
        journal.append({"attempt_id": "cleanup", "run_id": (
                            "other-run" if fault == "foreign_journal_run" else "selected-run"),
                        "operation": "cleanup", "status": status,
                        "work_id": "selected-run", "observed_at": datetime.now(UTC).isoformat()})
    if fault == "duplicate":
        journal.append({"attempt_id": "cleanup", "run_id": "selected-run",
                        "operation": "cleanup", "status": "acknowledged",
                        "work_id": "selected-run", "observed_at": datetime.now(UTC).isoformat()})
    packet = {"run_id": "selected-run", "ownership": {"owned-resource": "scenario_owned",
              "linked-resource": "customer_linked"}, "resources": {
                  "owned-resource": "absent", "linked-resource": "retained"}}
    if fault == "foreign_run":
        packet["run_id"] = "foreign-run"
    if fault == "foreign_owner":
        packet["ownership"]["owned-resource"] = "customer_linked"
    if fault == "owned_present":
        packet["resources"]["owned-resource"] = "present"
    if fault == "linked_missing":
        packet["resources"].pop("linked-resource")

    class NativeClient:
        def __init__(self, **kwargs):
            assert kwargs == {"base_url": case["api_url"], "timeout": 20, "follow_redirects": False,
                              "headers": {"Authorization": "Bearer fictional-token"}}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, headers):
            assert url in {"/v1/products/product-example/projects/project-example/services/security/runs/selected-run/"
                           "native-readback/spaces/owned-binding",
                           "/v1/products/product-example/projects/project-example/services/security/bindings/owned-binding"}
            self.path = url
            return self

        def raise_for_status(self):
            pass

        def json(self):
            if self.path.endswith("/bindings/owned-binding"):
                return {"binding_id": "owned-binding", "scope": case["scope"],
                        "configuration_id": "configuration-example", "ownership": "adp_created",
                        "native_resource_ref": {"record_id": "owned-resource"}, "revision": 2,
                        "state": "cleaned"}
            return packet

    if fault == "accepted":
        observed = reader.read_cleanup(case, {"API_TOKEN": "fictional-token"}, tmp_path,
                                       "456", "2", client_factory=NativeClient)
        assert observed["run_id"] == "selected-run"
        assert observed["owned_absent"] == observed["linked_retained"] == 1
        assert observed["readback"][0].startswith("private://security/native-cleanup/security-cleanup-456-2.json#sha256:")
        retained = tmp_path / "security-cleanup-456-2.json"
        assert json.loads(retained.read_text()) == packet
        assert retained.stat().st_mode & 0o077 == 0
        assert "owned-resource" not in json.dumps(observed)
        with pytest.raises(FileExistsError):
            reader.read_cleanup(case, {"API_TOKEN": "fictional-token"}, tmp_path,
                                "456", "2", client_factory=NativeClient)
    else:
        with pytest.raises((ValueError, AssertionError)):
            reader.read_cleanup(case, {"API_TOKEN": "fictional-token"}, tmp_path,
                                "456", "2", client_factory=NativeClient)


@pytest.mark.parametrize("fault", ["accepted", "allowed", "leaked", "shared_token", "foreign_scope",
                                   "missing_run", "unexpected_body", "oversized"])
def test_post_selector_scope_denials_reread_cleaned_run_without_provider_effect(fault):
    import httpx

    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    source = [{"record_revision": "selected-revision"}]
    case = {"scope": scope, "api_url": "https://example.invalid", "api_token_env": "API_TOKEN",
            "selected": {"sources": source, "binding_id": "binding-example"},
            "recovery": {"scope": scope, "sources": source, "binding_id": "binding-example",
                         "foreign_product_id": "other-product", "revoked_token_env": "REVOKED_TOKEN"}}
    foreign = {"scope": {**scope, "tenant_id": "other-tenant"}, "api_token_env": "FOREIGN_TOKEN"}
    cleanup = {"run_id": "selected-run"}
    credentials = {"API_TOKEN": "first-token", "FOREIGN_TOKEN": "other-token", "REVOKED_TOKEN": "revoked-token"}
    if fault == "shared_token":
        credentials["REVOKED_TOKEN"] = credentials["API_TOKEN"]
    elif fault == "foreign_scope":
        foreign["scope"]["tenant_id"] = scope["tenant_id"]
    elif fault == "missing_run":
        cleanup["run_id"] = ""
    calls = []

    def handle(request):
        assert request.method == "GET"
        calls.append((request.url.path, request.headers["Authorization"]))
        payload = {"detail": {"code": "access_denied"}}
        if fault == "leaked" and len(calls) == 2:
            payload["result_id"] = "private-result"
        elif fault == "unexpected_body" and len(calls) == 3:
            payload = {"error": "denied"}
        elif fault == "oversized" and len(calls) == 2:
            payload["padding"] = "x" * 9000
        return httpx.Response(200 if fault == "allowed" and len(calls) == 3 else 403, json=payload)

    transport = httpx.MockTransport(handle)

    def client_factory(**kwargs):
        assert kwargs == {"base_url": case["api_url"], "timeout": 20, "follow_redirects": False}
        return httpx.Client(transport=transport, **kwargs)

    if fault == "accepted":
        observed = reader.read_scope_denials(case, foreign, cleanup, credentials, client_factory=client_factory)
        assert observed["run_id"] == "selected-run" and observed["scope"] == scope
        assert {probe["case"] for probe in observed["probes"]} == {
            "foreign_product", "foreign_tenant", "revoked_access"}
        assert [authorization for _, authorization in calls] == [
            "Bearer first-token", "Bearer other-token", "Bearer revoked-token"]
        assert all("selected-run" in path for path, _ in calls)
        assert "other-product" in calls[0][0] and len(calls) == 3
        assert "first-token" not in json.dumps(observed)
    else:
        with pytest.raises((AssertionError, ValueError)):
            reader.read_scope_denials(case, foreign, cleanup, credentials, client_factory=client_factory)
        if fault in {"shared_token", "foreign_scope", "missing_run"}:
            assert not calls


@pytest.mark.parametrize("fault", ["missing_result", "wrong_source", "foreign_snapshot", "foreign_binding",
                                   "foreign_provider", "wrong_native", "uncertain_attempt"])
def test_post_selector_readback_refuses_incomplete_or_foreign_observations(readbacks, fault):
    case, records, native = copy.deepcopy(readbacks)
    base = "/v1/products/product-example/projects/project-example/services/security"
    if fault == "missing_result":
        records[base + "/results?limit=100"]["items"].pop()
    elif fault == "wrong_source":
        selected = records[base + "/runs/code_review-run"]["input"]["sources"]
        records[base + "/runs/code_review-run"]["input"]["sources"] = [{**selected[0], "record_revision": "foreign-revision"}]
    elif fault == "foreign_snapshot":
        records[base + "/runs/code_review-run"]["admission"]["source_snapshots"][0]["source_ref"] = {
            "record_revision": "foreign-revision"}
    elif fault == "uncertain_attempt":
        reader.AttemptJournal(case["assessment_journal_file"]).append({
            "attempt_id": "unknown", "operation": "assessment_submit", "status": "prepared",
            "work_id": "unknown", "observed_at": "2026-10-28T00:00:00+00:00"})
    elif fault == "foreign_binding":
        records[base + "/bindings/code_review-binding"]["scope"] = {"tenant_id": "foreign"}
    elif fault == "foreign_provider":
        records[base + "/bindings/code_review-binding"]["provider_identity"]["role_arn"] = "foreign-role"
    else:
        native["code_review-run"]["native_job_ref"] = {"record_id": "other"}
    with pytest.raises((ValueError, AssertionError)):
        reader.assess_case(case, lambda path: records[path], lambda run, provider: native[run["run_id"]],
                           lambda run: [{"source_ref": source, "content": SOURCE_CONTENT}
                                        for source in run["input"]["sources"]])


def test_native_reader_uses_the_selected_broker_connection():
    calls = []
    provider = {"label": "selected-provider", "account_id": "123456789012",
                "role_arn": "arn:aws:iam::123456789012:role/selected", "region": "us-east-1"}
    run = {"run_id": "run-example", "scope": {"product_id": "product-example",
            "project_id": "project-example"}, "admission": {"provider_identity": {
                key: provider[key] for key in ("account_id", "role_arn", "region")}}}
    def read_api(path):
        calls.append(path)
        return {"status": "COMPLETED", "native_job_ref": {"record_id": "one"}}

    assert reader.native_job(run, provider, read_api)["status"] == "COMPLETED"
    assert reader.native_inventory(run, provider, read_api)["status"] == "COMPLETED"
    assert calls == ["/v1/products/product-example/projects/project-example/services/security/"
                     "runs/run-example/native-readback/" + action for action in ("job", "inventory")]
    with pytest.raises(ValueError, match="identity"):
        reader.native_job(run, {**provider, "role_arn": "foreign"}, read_api)


@pytest.mark.parametrize("fault", ["accepted", "changed_code", "paused_mapping", "wrong_role",
                                     "missing_scope", "wrong_action", "public_receipt"])
def test_installed_readback_checks_digest_and_current_selected_role(tmp_path, monkeypatch, fault):
    digest = "a" * 64
    encoded = base64.b64encode(bytes.fromhex(digest)).decode()
    environment = {"ARCHIVE_DIGEST": digest, "L5_MUTATION_ROLE_ARN":
                   "arn:aws:iam::123456789012:role/fictional-selected", "L5_ENVIRONMENT": "dev",
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
    if fault == "wrong_role":
        environment["L5_MUTATION_ROLE_ARN"] = "not-a-selected-role"

    def invoke(command, *, capture_output, check, timeout):
        assert command[2:4] == ["status", "--environment"]
        assert command[command.index("--account") + 1] == "123456789012"
        assert command[command.index("--role-arn") + 1] == environment["L5_MUTATION_ROLE_ARN"]
        state = {"action": "status", "result": "passed", "scope": {"roles": {}, "queues": {}},
                 "observed": {"worker_sha256": encoded, "repair_sha256": encoded,
                              "mapping_state": "Enabled", "repair_state": "ENABLED"}}
        if fault == "changed_code":
            state["observed"]["worker_sha256"] = "other"
        if fault == "paused_mapping":
            state["observed"]["mapping_state"] = "Disabled"
        if fault == "missing_scope":
            state.pop("scope")
        if fault == "wrong_action":
            state["action"] = "rollout"
        Path(command[-1]).write_text(json.dumps(state))
        Path(command[-1]).chmod(0o644 if fault == "public_receipt" else 0o600)

    monkeypatch.setattr(reader.subprocess, "run", invoke)
    if fault == "accepted":
        installed = reader.installed_status(tmp_path, environment)
        assert installed["worker_sha256"] == encoded
        assert installed["scope_receipt"].startswith("private://security/installed/installed-123-1.json#sha256:")
    else:
        with pytest.raises(ValueError):
            reader.installed_status(tmp_path, environment)


@pytest.mark.parametrize("fault", ["accepted", "missing_product_token", "foreign_run", "changed_source",
                                     "duplicate_job", "wrong_receipt", "missing_restart", "missing_attempt",
                                     "changed_journal", "missing_snapshot", "foreign_snapshot", "late_snapshot",
                                     "no_progress", "missing_product", "missing_publication", "revision_changed"])
def test_post_selector_recovery_rereads_product_and_native_evidence(tmp_path, monkeypatch, fault):
    import httpx
    import recovery_live
    from evidence import AttemptJournal

    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    source = [{"record_revision": "pinned-revision"}]
    selected_input = {"sources": source, "binding_id": "binding-example", "capability": "threat_model"}
    reference = {"record_id": "job-example"}
    journal = AttemptJournal(tmp_path / "recovery.jsonl")
    for operation, work_id, attempt_id in (("fault_arm", "receipt-example", "fault_arm"),
                                           ("submit", "work-example", "attempt-example"),
                                           ("worker_restart", "receipt-example", "worker_restart"),
                                           ("cleanup", "run-example", "cleanup")):
        if fault == "missing_restart" and operation == "worker_restart":
            continue
        journal.append({"attempt_id": attempt_id, "run_id": "run-example", "operation": operation,
                        "status": "acknowledged" if operation != "submit" else "accepted", "work_id": work_id,
                        "observed_at": "2026-01-01T00:00:00Z"})
    case = {"scope": scope, "provider": {"label": "fictional"}, "max_attempts": 3,
            "api_url": "https://product.invalid", "api_token_env": "PRODUCT_TOKEN",
            "selected": {"sources": source, "binding_id": "binding-example"},
            "recovery": {"scope": scope, "sources": source, "binding_id": "binding-example",
                         "input": selected_input, "receipt": "receipt-example-01", "max_attempts": 3,
                         "exclusive_agent_space": True, "journal_file": str(journal.path)}}
    credentials = {} if fault == "missing_product_token" else {"PRODUCT_TOKEN": "product-token"}
    run = {"run_id": "run-example", "scope": scope, "input": selected_input,
           "admission": {"source_snapshots": ["pinned-snapshot"]},
           "state": "succeeded", "provider_state": "completed", "native_job_ref": reference}
    work = {"work_id": "work-example", "completed_at": "2026-01-01T00:02:00Z",
            "last_enqueued_at": "2026-01-01T00:01:00Z", "attempts": 2}
    attempt = {"attempt_id": "attempt-example", "operation": "submit", "status": "accepted",
               "first_work_id": "work-example", "started_at": "2026-01-01T00:00:00Z",
               "native_job_ref": reference}
    publication = {"state": "published", "attempts": 2, "publication_id": "publication-example",
                   "schema_version": "1.0", "result_id": "result-example", "result_version": 1,
                   "receipt": {"receipt_id": "native-receipt", "contribution_id": "publication-example",
                               "admitted_schema": "1.0", "consumer_revision": "revision-example"}}
    before = {**run, "state": "running", "provider_state": "submitted", "native_job_ref": None,
              "work": {**work, "completed_at": None, "attempts": 1,
                       "last_enqueued_at": work["last_enqueued_at"] if fault == "no_progress" else
                                           "2026-01-01T00:00:00Z"},
              "attempts": [{"attempt_id": "attempt-example", "operation": "submit", "native_job_ref": None}]}
    prior = {**publication, "state": "retry_wait", "attempts": 1, "receipt": None}
    snapshot = {"receipt": "receipt-example-01", "run": before, "publication": prior,
                "observed_at": "2026-01-01T00:00:00Z"}
    if fault == "foreign_snapshot":
        snapshot["run"]["run_id"] = "foreign-run"
    elif fault == "late_snapshot":
        snapshot["observed_at"] = "2027-01-01T00:00:00Z"
    snapshot_path = tmp_path / "recovery.jsonl.fault.json"
    if fault != "missing_snapshot":
        snapshot_path.write_text(json.dumps(snapshot))
        snapshot_path.chmod(0o600)
    if fault == "changed_source":
        run["input"] = {**selected_input, "sources": [{"record_revision": "other-revision"}]}
    elif fault == "wrong_receipt":
        publication["receipt"]["contribution_id"] = "another-publication"
    elif fault == "missing_attempt":
        attempt = None
    if fault == "missing_publication":
        publication = None
    monkeypatch.setattr(recovery_live, "product_sources", lambda observed, client: [
        {"source_ref": item, "content": b"pinned source"} for item in observed["input"]["sources"]])
    calls = []
    base = "/v1/products/product-example/projects/project-example/services/security"

    def handle(request):
        calls.append((request.url.path, request.headers["Authorization"]))
        if fault == "missing_product" and request.url.path.endswith("/runs/run-example"):
            return httpx.Response(404, json={"detail": {"code": "not_found"}})
        if request.url.path.endswith("/receipts/receipt-example-01"):
            return httpx.Response(200, json={"run_id": "foreign-run" if fault == "foreign_run" else "run-example"})
        if request.url.path.endswith("/recovery-state"):
            durable_run = {**run, "revision": 4} if fault == "revision_changed" else run
            return httpx.Response(200, json={"run": durable_run, "attempts": [attempt] if attempt else [],
                                             "work": [work], "publications": [publication] if publication else []})
        if request.url.path.endswith("/publications/publication-example"):
            return httpx.Response(200, json={"publication_id": "publication-example", "result_id": "result-example",
                                             "state": "published", "receipt_id": "native-receipt",
                                             "consumer_revision": "revision-example"})
        return httpx.Response(200, json=run)

    def client_factory(**kwargs):
        assert kwargs == {"base_url": case["api_url"], "timeout": 20, "follow_redirects": False,
                          "headers": {"Authorization": "Bearer product-token"}}
        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    def inventory(actual, provider):
        assert actual["run_id"] == "run-example" and provider is case["provider"]
        if fault == "changed_journal":
            journal.append({"attempt_id": "late", "operation": "submit", "status": "accepted",
                            "work_id": "work-example", "observed_at": "2026-01-01T00:00:00Z"})
        return [reference, reference] if fault == "duplicate_job" else [reference]

    if fault == "accepted":
        observed = reader.read_recovery(case, credentials, client_factory=client_factory, inventory=inventory)
        assert observed["run_id"] == "run-example" and observed["attempts"] == 1
        assert observed["journal_sha256"] == hashlib.sha256(journal.path.read_bytes()).hexdigest()
        assert [path for path, _ in calls] == [base + "/runs/receipts/receipt-example-01",
                                             base + "/runs/run-example", base + "/runs/run-example/recovery-state",
                                             base + "/runs/run-example/recovery-state",
                                             base + "/publications/publication-example"]
        assert {auth for _, auth in calls} == {"Bearer product-token"}
        assert observed["transition_sha256"] == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        assert "product-token" not in json.dumps(observed)
    else:
        with pytest.raises((ValueError, AssertionError, OSError, httpx.HTTPStatusError)):
            reader.read_recovery(case, credentials, client_factory=client_factory, inventory=inventory)
