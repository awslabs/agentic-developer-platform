"""Synthetic V11 publisher reports; no deployed browser evidence is asserted."""

import json
import xml.etree.ElementTree as ET
from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.product_v11_evidence import CASES
from tests.e2e.product_onboarding.product_v11_publish import publish
from tests.e2e.product_onboarding.test_product_v11_evidence import sample_v11_packet


def prepared(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    directory = root / "screenshots"
    directory.mkdir(mode=0o700)
    sample = sample_v11_packet()
    identities = sample["identities"]
    context = {"identities": {key: value for key, value in identities.items()
                              if key not in ("browser_code_revision", "browser_workflow_revision")},
               "deployment": sample["deployment"], "plan_revision": sample["plan_revision"]}
    scope = {"tenant_id": "example-tenant", "product_id": "example-product", "project_id": "example-project"}
    run_id = sample["run_id"]

    def capture(name):
        path = directory / f"{run_id}-{name}.png"
        path.write_bytes(b"private browser capture")
        path.chmod(0o600)
        return str(path)

    for name, (criterion, scenario, _) in CASES.items():
        case_scope = {**scope, "product_id": scope["product_id"] + "-" + name,
                      "project_id": scope["project_id"] + "-" + name}
        report = {**case_scope, "scenario_id": scenario, "ac_id": criterion, "run_id": run_id,
                  "source_revision": identities["source_revision"],
                  "requirements_revision": identities["requirements_revision"],
                  "frontend_release_ref": identities["frontend_release_ref"],
                  "api_image_digest": identities["api_image_digest"], "schema_head": identities["schema_head"],
                  "observed": context["deployment"]["observed"], "evidence_context": context,
                  "browser_status": "pass", "cleanup": {**case_scope, "run_id": run_id,
                      "fixture_removed": True, "owned_absent": True, "shared_unchanged": True, "cleanup_revision": 1}}
        if name in ("v11_idea", "v11_existing_assets", "v11_running_product"):
            starting_point = name.removeprefix("v11_")
            report["starting_point"] = starting_point
            report["viewports"] = []
            report["api_reads"] = []
            for width, revision in ((390, 3), (1440, 4)):
                purpose = f"Saved at {width}px"
                report["viewports"].append({"width": width, "revision": revision, "purpose": purpose,
                    "keyboard": "pass", "offline_retry": "pass", "screenshot": capture(f"{name}-{width}")})
                report["api_reads"].append({"status": 200, "body": {**case_scope, "revision": revision,
                    "product": {"starting_point": starting_point, "purpose": purpose}}})
        elif name == "v11_resume":
            session = {**case_scope, "id": "example-session", "revision": 1,
                       "resume_path": "/products/example-product/onboarding-sessions/example-session",
                       "operation_ids": [], "steps": {"information": {"status": "complete"}}}
            before = {**case_scope, "revision": 1, "context_status": "unavailable",
                      "peer_availability": sample["cases"]["v11_resume"]["peer_availability"],
                      "progress": {"checks": [{"id": "information", "required": True}]},
                      "product": {"name": "Example", "purpose": "Before edit", "intended_users": ["Maintainers"],
                                  "starting_point": "idea"}}
            after = deepcopy(before)
            after["revision"] = 2
            after["product"]["purpose"] = "After browser edit"
            command = ["adp", "onboard", "start", "--product", case_scope["product_id"],
                       "--project", case_scope["project_id"], "--revision", "1", "--json"]
            report.update(viewports=[{"screenshot": capture("v11_resume")}],
                session_started=session, session_before=deepcopy(session), session_after=deepcopy(session),
                profile_before=before, profile_after=after,
                cli_events=[{"command": command, "exit_code": 0, "installed_cli_sha256": "c" * 64,
                             "stdout": json.dumps({"status": "ok", "detail": session}), "stderr": ""}],
                api_reads=[{"status": 200, "body": value} for value in (session, before, after)])
        else:
            initial = {**case_scope, "revision": 1, "product": {"name": "Private Example",
                "purpose": "Private purpose", "intended_users": ["Maintainers"], "starting_point": "idea"}}
            concurrent = deepcopy(initial)
            concurrent["revision"] = 2
            concurrent["product"]["purpose"] += " from another client"
            saved = deepcopy(initial)
            saved["revision"] = 3
            saved["product"]["purpose"] += " from V11 browser"
            report.update(baseline=initial, concurrent=concurrent, saved=saved,
                viewports=[{"width": width, "screenshot": capture(f"v11-release-{width}"),
                            "overflow": False, "keyboard": "pass"} for width in (390, 1440)],
                api_reads=[{"status": 200, "body": value} for value in (initial, concurrent, saved)] + [
                    {"status": 404, "body": {"detail": {"code": "not_found"}}},
                    {"status": 403, "body": {"detail": {"error": "tenant_membership_revoked"}}}])
        filename = name.replace("_", "-", 1) if name == "v11_release" else name
        private_json(directory / f"{run_id}-{filename}-report.json", report)
    inventory = root / "inventory.json"
    private_json(inventory, [selector for _, _, selector in CASES.values()])
    suite = root / "suite.json"
    private_json(suite, {"status": "pass", "suite": "new-ui", "source_revision": identities["source_revision"],
                         "expected": 5, "executed": 5, "passed": 5, "failed": 0, "skipped": 0, "missing": 0})
    junit = root / "junit.xml"
    suites = ET.Element("testsuites")
    for _, _, selector in CASES.values():
        case = ET.SubElement(suites, "testcase", name=selector)
        ET.SubElement(ET.SubElement(case, "properties"), "property", name="adp_case_id", value=selector)
    ET.ElementTree(suites).write(junit)
    junit.chmod(0o600)
    return directory, inventory, junit, suite, root / "product-v11-packet.json"


def run(paths, **changes):
    arguments = {"release_revision": "a" * 40, "checkout_revision": "a" * 40,
                 "workflow_revision": "b" * 40, "workflow_run_id": 42, "workflow_attempt": 1}
    arguments.update(changes)
    return publish(*paths, **arguments)


def test_v11_publisher_binds_five_real_report_shapes_and_private_artifacts(tmp_path):
    paths = prepared(tmp_path)
    packet = run(paths)
    assert packet["status"] == "contribution_ready" and packet["issues"] == []
    assert packet["combined_status"] == "pending_v10_and_human_decision"
    assert set(packet["cases"]) == set(CASES)
    assert all(packet["cases"][case]["attempts"][-1]["result"] == "pass" for case in CASES)
    assert len(json.loads((paths[-1].parent / "product-42-1-v11-cleanup.json").read_text())) == 5
    assert json.loads(paths[-1].read_text()) == packet
    from tests.e2e.product_onboarding.product_v11_evidence import review_mapping
    assert review_mapping(packet)["review"]["status"] == "not_ready"


@pytest.mark.parametrize("change", [
    lambda paths: (paths[0] / "product-42-1-v11_resume-report.json").unlink(),
    lambda paths: (paths[0] / "product-42-1-v11_idea-report.json").chmod(0o644),
    lambda paths: (paths[0] / "product-42-1-v11-release-report.json").write_text('{"browser_status":"pass"}'),
    lambda paths: paths[1].write_text(json.dumps([selector for _, _, selector in list(CASES.values())[:-1]])),
    lambda paths: paths[3].write_text(json.dumps({"status": "pass", "suite": "new-ui", "source_revision": "a" * 40,
                                           "expected": 5, "executed": 4, "passed": 4, "failed": 0, "skipped": 1, "missing": 0})),
])
def test_v11_publisher_rejects_missing_skipped_or_fabricated_evidence(tmp_path, change):
    paths = prepared(tmp_path)
    change(paths)
    packet = run(paths)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert packet["combined_status"] == "pending_v10_and_human_decision"


def test_v11_publisher_rejects_false_peer_success_scope_and_cleanup(tmp_path):
    paths = prepared(tmp_path)
    report_path = paths[0] / "product-42-1-v11_resume-report.json"
    report = json.loads(report_path.read_text())
    report["profile_after"]["peer_availability"]["l5"]["receipt"] = "invented"
    report_path.write_text(json.dumps(report))
    packet = run(paths)
    assert packet["status"] == "incomplete"
    assert any("v11_resume" in issue for issue in packet["issues"])


@pytest.mark.parametrize("name,change", [
    ("v11_idea", lambda report: report["cleanup"].update(shared_unchanged=False)),
    ("v11_release", lambda report: report["saved"].update(project_id="foreign-project")),
    ("v11_resume", lambda report: report["session_after"].update(operation_ids=["queued-peer"])),
    ("v11_existing_assets", lambda report: report["evidence_context"]["identities"].update(source_revision="f" * 40)),
    ("v11_running_product", lambda report: report["viewports"].pop()),
])
def test_v11_publisher_refuses_fabricated_case_observations(tmp_path, name, change):
    paths = prepared(tmp_path)
    filename = name.replace("_", "-", 1) if name == "v11_release" else name
    report_path = paths[0] / f"product-42-1-{filename}-report.json"
    report = json.loads(report_path.read_text())
    change(report)
    report_path.write_text(json.dumps(report))
    packet = run(paths)
    assert packet["status"] == "incomplete"
    assert any(name in issue for issue in packet["issues"])


def test_v11_publisher_refuses_skipped_junit_with_passing_suite(tmp_path):
    paths = prepared(tmp_path)
    document = ET.parse(paths[2])
    ET.SubElement(document.getroot().find("testcase"), "skipped")
    document.write(paths[2])
    packet = run(paths)
    assert packet["status"] == "incomplete"
    assert any("JUnit" in issue for issue in packet["issues"])


def test_v11_publisher_refuses_foreign_or_public_runner_artifacts(tmp_path):
    paths = prepared(tmp_path)
    paths[1].chmod(0o644)
    packet = run(paths)
    assert packet["status"] == "incomplete"
    assert any("private" in issue for issue in packet["issues"])


def test_v11_revision_exposure_requires_every_step_and_private_upload(tmp_path):
    from tests.e2e.product_onboarding.product_v11_publish import contribution_status

    packet = run(prepared(tmp_path))
    steps = dict.fromkeys(("browser", "report", "inventory", "bind", "cleanup", "upload"), "success")
    assert contribution_status(packet, steps, "private-version") == "contribution_ready"
    for name in steps:
        assert contribution_status(packet, {**steps, name: "failure"}, "private-version") == "incomplete"
    assert contribution_status(packet, steps, "") == "incomplete"
    changed = deepcopy(packet)
    changed["packet_revision"] = "sha256:" + "0" * 64
    assert contribution_status(changed, steps, "private-version") == "incomplete"


def test_v11_workflow_binds_publisher_before_upload_and_does_not_publish_b02():
    from pathlib import Path

    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/e2e-new-ui-playwright.yml").read_text()
    assert workflow.index("Verify exact V11 browser case inventory") < workflow.index("Bind V11 reports to the reviewed browser result")
    assert workflow.index("Bind V11 reports to the reviewed browser result") < workflow.index("Upload test results")
    assert workflow.index("Upload test results") < workflow.index("Expose V11 contribution only after private upload")
    assert "id: v11-bind\n        if: always() && inputs.product_v11" in workflow
    assert "python -m tests.e2e.product_onboarding.product_v11_publish \\" in workflow
    assert "id: bind-product\n        if: always() && inputs.product_source_revision != '' && !inputs.product_v11" in workflow
    assert "V11_MANIFEST_VERSION: ${{ steps.upload-results.outputs.manifest-version }}" in workflow
    assert "B09 still requires V10 evidence and one human decision" in workflow


def test_v11_publisher_rejects_duplicate_effects_across_cases(tmp_path):
    paths = prepared(tmp_path)
    original = json.loads((paths[0] / "product-42-1-v11_idea-report.json").read_text())
    path = paths[0] / "product-42-1-v11_existing_assets-report.json"
    duplicated = json.loads(path.read_text())
    for key in ("product_id", "project_id"):
        duplicated[key] = original[key]
        duplicated["cleanup"][key] = original[key]
        for read in duplicated["api_reads"]:
            read["body"][key] = original[key]
    path.write_text(json.dumps(duplicated))
    packet = run(paths)
    assert packet["status"] == "incomplete"
    assert any("reused an owned Product scope" in issue for issue in packet["issues"])
