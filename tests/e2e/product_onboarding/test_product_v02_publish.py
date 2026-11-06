"""Synthetic producer fixtures only; these are not live Product acceptance."""

import json
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.e2e.product_onboarding.product_v02_evidence import CASES
from tests.e2e.product_onboarding.product_v02_publish import main, publish, review_status
from tests.e2e.product_onboarding.test_product_v02_evidence import SOURCE, WORKFLOW, sample_packet

SUCCESS = dict.fromkeys(("browser", "report", "bind", "upload"), "success")


def prepared(tmp_path):
    inputs = tmp_path / "inputs.json"
    junit = tmp_path / "junit.xml"
    inventory = tmp_path / "inventory.json"
    suite = tmp_path / "suite.json"
    output = tmp_path / "private-packet.json"
    packet = sample_packet()
    packet["report_artifact"] = str(output)

    def private(name):
        file = tmp_path / name
        file.write_bytes(b"Synthetic private test artifact")
        file.chmod(0o600)
        return str(file)

    ui, api = packet["surfaces"]["ui"], packet["surfaces"]["api"]
    ui["artifact_ref"] = private("ui-result.json")
    ui["report_ref"] = str(junit)
    api["artifact_ref"] = private("api-result.json")
    api["report_ref"] = private("api-report.json")
    api["readback_ref"] = private("api-readback.json")
    packet["cleanup"]["receipt_ref"] = private("cleanup.json")
    for width, viewport in ui["viewports"].items():
        viewport["screenshot_ref"] = private("viewport-" + width + ".png")
        viewport["trace_ref"] = private("viewport-" + width + ".zip")
    for name, case in packet["cases"].items():
        case["readback_ref"] = private("readback-" + name + ".json")
        api["case_readback_refs"][name] = case["readback_ref"]
        case["evidence_ref"] = private("browser-" + name + ".json")
        case["cleanup_ref"] = packet["cleanup"]["receipt_ref"]
        for index, attempt in enumerate(case["attempts"]):
            attempt["evidence_ref"] = private("attempt-" + name + "-" + str(index) + ".json")
    inputs.write_text(json.dumps(packet))
    selected = [identity[2] for identity in CASES.values()]
    inventory.write_text(json.dumps(selected))
    suite.write_text(json.dumps({
        "status": "pass", "suite": "new-ui", "source_revision": SOURCE,
        "expected": 3, "executed": 3, "passed": 3, "failed": 0, "skipped": 0, "missing": 0,
    }))
    root = ET.Element("testsuites")
    for index, case_id in enumerate(selected):
        case = ET.SubElement(root, "testcase", classname="test_product_profile", name=str(index))
        properties = ET.SubElement(case, "properties")
        ET.SubElement(properties, "property", name="adp_case_id", value=case_id)
    ET.ElementTree(root).write(junit)
    junit.chmod(0o600)
    return inputs, junit, inventory, suite, output


def run(paths, **kwargs):
    arguments = dict(
        release_revision=SOURCE, checkout_revision=SOURCE, workflow_revision=WORKFLOW,
        workflow_run_id=42, workflow_attempt=1,
    )
    arguments.update(kwargs)
    return publish(*paths, **arguments)


def test_packet_revision_binds_actual_suite_and_private_upload(tmp_path):
    paths = prepared(tmp_path)
    packet = run(paths)
    assert packet["status"] == "review_ready"
    assert packet["dispatch"]["selected_cases"] == sorted(CASES)
    assert packet["dispatch"]["workflow_run_id"] == 42
    assert packet["dispatch"]["workflow_attempt"] == 1
    assert stat.S_IMODE(paths[-1].stat().st_mode) == 0o600
    assert json.loads(paths[-1].read_text()) == packet
    assert review_status(packet, SUCCESS, "private-version") == "review_ready"
    assert review_status(packet, {**SUCCESS, "upload": "failure"}, "private-version") == "incomplete"
    assert review_status(packet, {**SUCCESS, "bind": "failure"}, "private-version") == "incomplete"
    assert review_status(packet, SUCCESS, "") == "incomplete"
    packet["dispatch"]["workflow_attempt"] = 2
    assert review_status(packet, SUCCESS, "private-version") == "incomplete"


def test_relative_output_canonicalization_does_not_follow_output_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "test-results"
    root.mkdir(mode=0o700)
    paths = prepared(root)
    outside = tmp_path / "outside.json"
    paths[-1].symlink_to(outside)
    with pytest.raises(FileExistsError):
        run((*paths[:-1], "test-results/private-packet.json"))
    assert not outside.exists()


def test_private_artifact_symlink_cannot_escape_packet_directory(tmp_path):
    root = tmp_path / "test-results"
    root.mkdir(mode=0o700)
    paths = prepared(root)
    outside = tmp_path / "outside.json"
    outside.write_text("Private synthetic evidence outside the selected directory")
    outside.chmod(0o600)
    artifact = root / "api-result.json"
    artifact.unlink()
    artifact.symlink_to(outside)
    packet = run(paths)
    assert packet["status"] == "incomplete"
    assert any("no retained private file" in issue for issue in packet["issues"])


@pytest.mark.parametrize("change,reason", [
    (lambda paths: paths[0].unlink(), "private B02 inputs"),
    (lambda paths: paths[1].unlink(), "browser JUnit"),
    (lambda paths: paths[2].unlink(), "browser inventory"),
    (lambda paths: paths[3].unlink(), "browser suite report"),
    (lambda paths: paths[2].write_text("{}"), "browser inventory"),
    (lambda paths: paths[2].write_text(json.dumps(["wrong"])), "browser inventory"),
    (lambda paths: paths[2].write_text(json.dumps([["wrong"]])), "browser inventory"),
    (lambda paths: paths[3].write_text(json.dumps({"status": "pass"})), "browser suite"),
    (lambda paths: paths[3].write_text(json.dumps({"status": "pass", "suite": "new-ui", "source_revision": SOURCE,
                                                   "expected": 3, "executed": 2, "passed": 2, "skipped": 1,
                                                   "failed": 0, "missing": 0})), "browser suite"),
    (lambda paths: paths[1].write_text("<testsuites />"), "browser JUnit"),
    (lambda paths: paths[1].write_text("malformed XML"), "browser JUnit"),
])
def test_missing_or_nonpassing_producer_refuses_review(tmp_path, change, reason):
    paths = prepared(tmp_path)
    change(paths)
    packet = run(paths)
    assert packet["status"] == "incomplete"
    assert any(reason in issue for issue in packet["issues"]), packet["issues"]
    assert review_status(packet, SUCCESS, "private-version") == "incomplete"
    assert json.loads(paths[-1].read_text()) == packet


def test_skipped_product_junit_does_not_pass_even_with_green_report(tmp_path):
    paths = prepared(tmp_path)
    tree = ET.parse(paths[1])
    ET.SubElement(tree.getroot().find(".//testcase"), "skipped")
    tree.write(paths[1])
    assert run(paths)["status"] == "incomplete"


def test_wrong_source_workflow_run_and_missing_cleanup_refused(tmp_path):
    paths = prepared(tmp_path)
    packet = run(paths, release_revision="f" * 40, checkout_revision="f" * 40,
                 workflow_revision="f" * 40, workflow_run_id=10, workflow_attempt=2)
    assert packet["status"] == "incomplete"
    assert any("dispatch" in issue for issue in packet["issues"])
    assert any("ui report differs" in issue for issue in packet["issues"])


def test_missing_owned_cleanup_refuses_even_when_browser_report_passes(tmp_path):
    paths = prepared(tmp_path)
    inputs = json.loads(paths[0].read_text())
    inputs["cleanup"]["owned_absent"] = False
    paths[0].write_text(json.dumps(inputs))
    assert run(paths)["status"] == "incomplete"


def test_cli_exits_nonzero_for_incomplete_inputs(tmp_path):
    paths = prepared(tmp_path)
    paths[0].unlink()
    arguments = ["--inputs", str(paths[0]), "--junit", str(paths[1]),
                 "--inventory", str(paths[2]), "--suite", str(paths[3]),
                 "--output", str(paths[4]), "--release-revision", SOURCE,
                 "--checkout-revision", SOURCE, "--workflow-revision", WORKFLOW,
                 "--workflow-run-id", "42", "--workflow-attempt", "1"]
    assert main(arguments) == 1
    assert json.loads(paths[4].read_text())["status"] == "incomplete"


def test_workflow_only_shows_revision_after_private_upload():
    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/e2e-new-ui-playwright.yml").read_text()
    assert workflow.index("name: Bind Product B02 evidence") < workflow.index("name: Upload test results")
    assert workflow.index("name: Upload test results") < workflow.index("name: Expose the reviewed B02 packet revision")
    assert "steps.upload-results.outputs.manifest-version" in workflow
    assert "steps.browser-run.outcome" in workflow
    assert "steps.bind-product.outcome" in workflow
    assert "if: always() && inputs.product_source_revision != ''" in workflow
    assert "review_status(packet," in workflow
    assert "human acceptance is separate" in workflow


@pytest.mark.parametrize("reference", [
    lambda packet: packet["surfaces"]["ui"]["viewports"]["390"]["screenshot_ref"],
    lambda packet: packet["surfaces"]["ui"]["viewports"]["1440"]["trace_ref"],
    lambda packet: packet["cases"]["live_browser_create"]["readback_ref"],
    lambda packet: packet["cases"]["live_browser_create"]["attempts"][0]["evidence_ref"],
    lambda packet: packet["cleanup"]["receipt_ref"],
])
def test_missing_private_artifact_refuses_review(tmp_path, reference):
    paths = prepared(tmp_path)
    packet = json.loads(paths[0].read_text())
    Path(reference(packet)).unlink()
    published = run(paths)
    assert published["status"] == "incomplete"
    assert any("no retained private file" in issue for issue in published["issues"])


def test_unprotected_or_external_artifact_refuses_review(tmp_path):
    paths = prepared(tmp_path)
    packet = json.loads(paths[0].read_text())
    file = Path(packet["cleanup"]["receipt_ref"])
    file.chmod(0o644)
    assert run(paths)["status"] == "incomplete"


def test_external_artifact_refuses_review(tmp_path):
    paths = prepared(tmp_path)
    external = tmp_path.parent / (tmp_path.name + "-outside.png")
    external.write_bytes(b"outside")
    external.chmod(0o600)
    packet = json.loads(paths[0].read_text())
    packet["surfaces"]["ui"]["viewports"]["390"]["screenshot_ref"] = str(external)
    paths[0].write_text(json.dumps(packet))
    assert run(paths)["status"] == "incomplete"


def test_corrupt_input_or_review_note_cannot_present_a_revision(tmp_path):
    paths = prepared(tmp_path)
    paths[0].write_text('{"status": NaN}')
    assert run(paths)["status"] == "incomplete"
    packet = json.loads(paths[-1].read_text())
    packet.update(status="review_ready", packet_revision="sha256:" + "a" * 64, invalid=float("nan"))
    assert review_status(packet, SUCCESS, "version") == "incomplete"
