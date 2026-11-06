"""Controlled producer ports: these tests do not establish live acceptance."""

import json
import stat
from types import SimpleNamespace

import pytest

from tests.e2e.new_ui.conftest import pytest_runtest_makereport, pytest_sessionfinish
from tests.e2e.product_onboarding.product_v02_evidence import CASES, build_packet
from tests.e2e.product_onboarding.product_v02_record import BrowserEvidence, CaseEvidence
from tests.e2e.product_onboarding.test_product_v02_evidence import SOURCE, WORKFLOW, sample_packet
from tests.e2e.product_onboarding.test_product_v02_publish import prepared, run


def collector(tmp_path):
    return BrowserEvidence(tmp_path, {
        "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": SOURCE,
        "PRODUCT_EXPECTED_REVISION": SOURCE, "PRODUCT_BROWSER_WORKFLOW_REVISION": WORKFLOW,
    })


def phase(evidence, case, when, outcome):
    report = SimpleNamespace(when=when, outcome=outcome, failed=outcome == "failed")
    item = SimpleNamespace(config=SimpleNamespace(product_evidence=evidence), nodeid=CASES[case][2],
                           path="tests/e2e/new_ui/test_product_profile.py", funcargs={})
    hook = pytest_runtest_makereport(item, None)
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))


@pytest.mark.parametrize("workflow_paths", [False, True])
def test_actual_report_hooks_produce_inputs_consumed_by_packet_gate(tmp_path, monkeypatch, workflow_paths):
    if workflow_paths:
        monkeypatch.chdir(tmp_path)
        tmp_path = tmp_path / "test-results"
        tmp_path.mkdir(mode=0o700)
    paths = prepared(tmp_path)
    context = json.loads(paths[0].read_text())
    evidence = collector(tmp_path)
    evidence.bind({
        "run_id": "product-42-1", "source_revision": SOURCE,
        "evidence_context": {key: context[key] for key in ("plan_revision", "identities", "deployment")}
        | {"cli": context["surfaces"]["cli"]},
    })
    for name in CASES:
        record = evidence.records[name]
        body = {"product_id": "owned-product", "project_id": "owned-project", "revision": 1}
        response = SimpleNamespace(status=200, body=lambda: json.dumps(body).encode())
        request = SimpleNamespace(get=lambda *args, **kwargs: response)
        assert record.request(request).get("https://workspace.example/api/v1/products", headers={
            "Authorization": "Bearer synthetic-secret-not-for-evidence",
        }) is response
        record.cleanup.append({key: context["cleanup"][key] for key in (
            "run_id", "product_id", "project_id", "fixture_removed", "owned_absent", "shared_unchanged",
        )})
        for when in ("setup", "call", "teardown"):
            phase(evidence, name, when, "passed")
    evidence.records["live_browser_evidence"].viewports = [
        {"width": int(width), "screenshot": value["screenshot_ref"], "trace": value["trace_ref"],
         "overflow": False, "keyboard": "pass"}
        for width, value in context["surfaces"]["ui"]["viewports"].items()
    ]
    pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace(product_evidence=evidence)), 0)
    input_path = tmp_path / "product-v02-inputs.json"
    arguments = (input_path, *paths[1:4], tmp_path / "product-v02-packet.json")
    if workflow_paths:
        for source, name in zip(paths[1:4], ("e2e-new-ui-junit.xml", "new-ui-inventory.json", "new-ui-result.json")):
            source.rename(tmp_path / name)
        arguments = tuple("test-results/" + name for name in (
            "product-v02-inputs.json", "e2e-new-ui-junit.xml", "new-ui-inventory.json",
            "new-ui-result.json", "product-v02-packet.json",
        ))
    packet = run(arguments)
    assert packet["status"] == "review_ready", packet["issues"]
    assert set(packet["cases"]) == set(CASES)
    for path in tmp_path.glob("product-v02-*.json"):
        assert "synthetic-secret-not-for-evidence" not in path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("when,outcome,status", [
    ("setup", "failed", "fail"), ("call", "failed", "fail"),
    ("teardown", "failed", "fail"), ("call", "skipped", "skip"),
])
def test_nonpassing_lifecycle_cannot_produce_passing_inputs(tmp_path, when, outcome, status):
    evidence = collector(tmp_path)
    name = "live_browser_create"
    for step in ("setup", "call", "teardown"):
        phase(evidence, name, step, outcome if step == when else "passed")
    inputs = evidence.write()
    assert inputs["cases"][name]["status"] == status
    assert inputs["cases"]["live_browser_evidence"]["status"] == "not_run"
    assert build_packet(inputs)["status"] == "incomplete"


def test_interruption_and_missing_cleanup_are_not_success(tmp_path):
    evidence = collector(tmp_path)
    phase(evidence, "live_browser_create", "setup", "passed")
    inputs = evidence.write()
    assert inputs["cases"]["live_browser_create"]["status"] == "interrupted"
    assert inputs["cleanup"]["status"] == "incomplete"
    assert build_packet(inputs)["status"] == "incomplete"
    with pytest.raises(FileExistsError):
        evidence.write()


@pytest.mark.parametrize("field,value", [("run_id", "product-43-1"), ("source_revision", "b" * 40)])
def test_foreign_fixture_cannot_bind_evidence(tmp_path, field, value):
    evidence = collector(tmp_path)
    fixture = {"run_id": "product-42-1", "source_revision": SOURCE, field: value}
    with pytest.raises(ValueError, match="differs"):
        evidence.bind(fixture)


def test_changed_deployment_context_is_refused(tmp_path):
    evidence = collector(tmp_path)
    fixture = {"run_id": "product-42-1", "source_revision": SOURCE,
               "evidence_context": {"identities": sample_packet()["identities"]}}
    evidence.bind(fixture)
    fixture["evidence_context"]["identities"]["schema_head"] = "another-head"
    with pytest.raises(ValueError, match="changed"):
        evidence.bind(fixture)


def test_binary_assets_do_not_become_api_evidence():
    record = CaseEvidence()
    response = SimpleNamespace(status=200, body=lambda: b"javascript bytes")
    request = record.request(SimpleNamespace(get=lambda *args, **kwargs: response))
    assert request.get("https://workspace.example/assets/main.js") is response
    assert record.reads == []


def test_oversized_readback_is_refused():
    record = CaseEvidence()
    response = SimpleNamespace(status=200, body=lambda: b"x" * (2 * 1024 * 1024 + 1))
    request = record.request(SimpleNamespace(get=lambda *args, **kwargs: response))
    with pytest.raises(ValueError, match="bound"):
        request.get("https://workspace.example/api/v1/products")
