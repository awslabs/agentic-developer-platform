"""Controlled B03 publication checks; no deployed execution is inferred."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from tests.e2e.product_onboarding.product_v04_evidence import CASES, SURFACE_SELECTORS
from tests.e2e.product_onboarding.product_v04_publish import publish, review_status
from tests.e2e.product_onboarding.test_product_v04_evidence import sample_inputs
from tests.e2e.product_onboarding.product_v04_selection import selectors


def private(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(content))
    path.chmod(0o600)
    return str(path.resolve())


@pytest.fixture
def published(tmp_path):
    inputs = sample_inputs()
    run_id = "product-42-1"
    root = tmp_path.resolve()
    report = {
        "run_id": run_id,
        "source_revision": inputs["source_revision"],
        "checkout_revision": inputs["source_revision"],
        "workflow_run_id": 42,
        "workflow_attempt": 1,
        "workflow_revision": inputs["artifacts"]["browser_workflow_revision"],
        "context": {
            "plan_revision": inputs["plan_revision"],
            "identities": {
                "source_revision": inputs["source_revision"],
                "requirements_revision": inputs["requirements_revision"],
                "api_image_digest": inputs["artifacts"]["api_image_digest"],
                "api_workflow_revision": inputs["artifacts"]["api_workflow_revision"],
                "frontend_release_sha256": inputs["artifacts"][
                    "frontend_manifest_sha256"
                ],
                "schema_head": inputs["artifacts"]["schema_head"],
                "cli_checksum": "e" * 64,
            },
            "deployment": {
                "receipt_ref": "evidence/release-one",
                "observation_ref": private(root / "runtime.json", {"observed": True}),
                "observed": {
                    "source_revision": inputs["source_revision"],
                    "api_image_digest": inputs["artifacts"]["api_image_digest"],
                    "api_workflow_revision": inputs["artifacts"][
                        "api_workflow_revision"
                    ],
                    "frontend_release_sha256": inputs["artifacts"][
                        "frontend_manifest_sha256"
                    ],
                    "browser_workflow_revision": inputs["artifacts"][
                        "browser_workflow_revision"
                    ],
                    "schema_head": inputs["artifacts"]["schema_head"],
                },
            },
        },
        "cases": {},
        "cleanup_ref": private(
            root / "cleanup.json",
            {
                "run_id": run_id,
                "verified": True,
                "receipt": {
                    "owned_absent": True,
                    "fixture_removed": True,
                    "shared_unchanged": True,
                },
            },
        ),
    }
    cli = {
        "run_id": run_id,
        "source_revision": inputs["source_revision"],
        "workflow_run_id": 42,
        "workflow_attempt": 1,
        "release_manifest_sha256": "e" * 64,
        "installed_cli_sha256": inputs["artifacts"]["installed_cli_sha256"],
        "workflow_revision": inputs["artifacts"]["browser_workflow_revision"],
        "cleanup": {
            "status": "verified",
            "original_projects_preserved": True,
            "reused_assets_preserved": True,
        },
        "cases": {},
    }
    for criterion, (_, _, surfaces) in CASES.items():
        report["cases"][criterion] = {}
        cli["cases"][criterion] = {"attempts": [], "api_attempts": []}
        for surface in surfaces:
            if criterion == "AC-03" or surface == "cli":
                attempt = deepcopy(
                    next(
                        item
                        for item in inputs["cases"][criterion]["attempts"]
                        if item["surface"] == surface
                    )
                )
                attempt["run_id"] = run_id
                for key in ("artifact_ref", "readback_ref", "stdout_ref"):
                    if key in attempt:
                        attempt[key] = private(
                            root
                            / (criterion.lower() + "-" + surface + "-" + key + ".json"),
                            {"result": "synthetic"},
                        )
                cli["cases"][criterion][
                    "attempts" if surface == "cli" else "api_attempts"
                ].append(attempt)
            else:
                record = {
                    "selector": SURFACE_SELECTORS[criterion][surface],
                    "status": "pass",
                    "reads": 2,
                    "artifact": private(
                        root / (criterion.lower() + "-" + surface + "-result.json"),
                        {
                            "phases": {
                                "setup": "passed",
                                "call": "passed",
                                "teardown": "passed",
                            }
                        },
                    ),
                    "readback": private(
                        root / (criterion.lower() + "-" + surface + "-api.json"),
                        [{"status": 200}, {"status": 200}],
                    ),
                    "viewports": [],
                }
                if surface == "browser":
                    record["viewports"] = [
                        {
                            "screenshot": private(
                                root / (criterion.lower() + "-screenshot.json"),
                                {"capture": True},
                            ),
                            "trace": private(
                                root / (criterion.lower() + "-trace.json"),
                                {"trace": True},
                            ),
                        }
                    ]
                report["cases"][criterion][surface] = record
    junit = root / "junit.xml"
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite")
    for selected in selectors():
        case = ET.SubElement(suite, "testcase")
        properties = ET.SubElement(case, "properties")
        ET.SubElement(properties, "property", name="adp_case_id", value=selected)
    junit.write_bytes(ET.tostring(suites))
    junit.chmod(0o600)
    paths = {
        "browser": root / "browser.json",
        "cli": root / "cli.json",
        "junit": junit,
        "inventory": root / "inventory.json",
        "suite": root / "suite.json",
        "output": root / "packet.json",
    }
    private(paths["browser"], report)
    private(paths["cli"], cli)
    private(paths["inventory"], list(selectors()))
    private(
        paths["suite"],
        {
            "status": "pass",
            "suite": "new-ui",
            "source_revision": inputs["source_revision"],
            "expected": 4,
            "executed": 4,
            "passed": 4,
            "failed": 0,
            "skipped": 0,
            "missing": 0,
        },
    )
    kwargs = {
        "release_revision": inputs["source_revision"],
        "checkout_revision": inputs["source_revision"],
        "workflow_revision": inputs["artifacts"]["browser_workflow_revision"],
        "workflow_run_id": 42,
        "workflow_attempt": 1,
    }
    return paths, kwargs


def packet_for(paths, kwargs):
    return publish(
        *(
            str(paths[key])
            for key in ("browser", "cli", "junit", "inventory", "suite", "output")
        ),
        **kwargs,
    )


def test_all_required_surfaces_and_owned_cleanup_bind_to_one_private_packet(published):
    paths, kwargs = published
    packet = packet_for(paths, kwargs)
    assert packet["status"] == "review_ready", packet["issues"]
    assert set(packet["surfaces"]) == {"api", "browser", "cli"}
    assert all(result["status"] == "pass" for result in packet["surfaces"].values())
    assert (
        review_status(
            packet,
            {
                name: "success"
                for name in (
                    "browser",
                    "cleanup",
                    "report",
                    "inventory",
                    "bind",
                    "upload",
                )
            },
            "manifest-one",
        )
        == "review_ready"
    )
    assert paths["output"].stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("field", ["cli", "browser", "inventory", "suite", "junit"])
def test_missing_required_report_prevents_completion(published, field):
    paths, kwargs = published
    paths[field].unlink()
    packet = packet_for(paths, kwargs)
    assert packet["status"] == "incomplete"
    assert review_status(packet, {}, "manifest-one") == "incomplete"


@pytest.mark.parametrize(
    "case,surface,field,value",
    [
        ("AC-01", "browser", "status", "skip"),
        ("AC-02", "api", "status", "failed"),
        ("AC-01", "api", "reads", 0),
        ("AC-02", "browser", "selector", "wrong"),
    ],
)
def test_skipped_failed_or_unobserved_browser_case_never_publishes(
    published, case, surface, field, value
):
    paths, kwargs = published
    browser = json.loads(paths["browser"].read_text())
    browser["cases"][case][surface][field] = value
    paths["browser"].write_text(json.dumps(browser))
    packet = packet_for(paths, kwargs)
    assert packet["status"] == "incomplete"


@pytest.mark.parametrize(
    "change",
    [
        lambda cli: cli["cases"]["AC-01"]["attempts"].clear(),
        lambda cli: cli["cases"]["AC-02"]["attempts"][0].update(result="interrupted"),
        lambda cli: cli["cases"]["AC-03"]["api_attempts"][0].update(result="fail"),
        lambda cli: cli["cleanup"].update(status="incomplete"),
        lambda cli: cli.update(release_manifest_sha256="f" * 64),
        lambda cli: cli.update(source_revision="d" * 40),
    ],
)
def test_cli_history_cleanup_or_release_mismatch_never_publishes(published, change):
    paths, kwargs = published
    cli = json.loads(paths["cli"].read_text())
    change(cli)
    paths["cli"].write_text(json.dumps(cli))
    packet = packet_for(paths, kwargs)
    assert packet["status"] == "incomplete"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report["cases"]["AC-01"]["api"].update(reads=3),
        lambda report: report["cases"]["AC-02"]["browser"].update(status="interrupted"),
        lambda report: report["cases"]["AC-01"]["browser"]["viewports"].clear(),
        lambda report: report.update(workflow_attempt=2),
        lambda report: report["cases"]["AC-01"].update(unrelated={"status": "pass"}),
    ],
)
def test_missing_readback_capture_or_wrong_workflow_attempt_is_incomplete(
    published, mutation
):
    paths, kwargs = published
    report = json.loads(paths["browser"].read_text())
    mutation(report)
    paths["browser"].write_text(json.dumps(report))
    assert packet_for(paths, kwargs)["status"] == "incomplete"


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_forged_pass_does_not_override_interrupted_execution_phase(published, phase):
    paths, kwargs = published
    report = json.loads(paths["browser"].read_text())
    result_path = Path(report["cases"]["AC-02"]["api"]["artifact"])
    result = json.loads(result_path.read_text())
    result["phases"][phase] = "failed"
    result_path.write_text(json.dumps(result))
    assert packet_for(paths, kwargs)["status"] == "incomplete"


def test_private_cli_attempt_cannot_reuse_external_or_readback_artifact(
    published, tmp_path
):
    paths, kwargs = published
    cli = json.loads(paths["cli"].read_text())
    attempt = cli["cases"]["AC-01"]["attempts"][0]
    attempt["readback_ref"] = str(tmp_path.parent / "unowned.json")
    paths["cli"].write_text(json.dumps(cli))
    assert packet_for(paths, kwargs)["status"] == "incomplete"


@pytest.mark.parametrize(
    "bad_step", ["browser", "cleanup", "report", "inventory", "bind", "upload"]
)
def test_missing_required_workflow_step_prevents_packet_publication(
    published, bad_step
):
    paths, kwargs = published
    packet = packet_for(paths, kwargs)
    steps = {
        name: "success"
        for name in ("browser", "cleanup", "report", "inventory", "bind", "upload")
    }
    steps[bad_step] = "skipped"
    assert review_status(packet, steps, "private-manifest") == "incomplete"
    steps[bad_step] = "success"
    assert review_status(packet, steps, "null") == "incomplete"
    packet["cases"]["AC-01"]["actual"] = "changed after publication"
    assert review_status(packet, steps, "private-manifest") == "incomplete"


def test_uncleaned_scope_or_tampered_packet_cannot_be_review_ready(published):
    paths, kwargs = published
    cleanup = json.loads((paths["output"].parent / "cleanup.json").read_text())
    cleanup["verified"] = False
    (paths["output"].parent / "cleanup.json").write_text(json.dumps(cleanup))
    assert packet_for(paths, kwargs)["status"] == "incomplete"
    assert (
        review_status(
            {"status": "review_ready", "packet_revision": "sha256:" + "a" * 64},
            {},
            "manifest",
        )
        == "incomplete"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda cli: cli.update(workflow_run_id=43),
        lambda cli: cli.update(workflow_attempt=2),
        lambda cli: cli.update(workflow_revision="f" * 40),
    ],
)
def test_installed_cli_report_must_match_selected_browser_workflow(published, change):
    paths, kwargs = published
    cli = json.loads(paths["cli"].read_text())
    change(cli)
    paths["cli"].write_text(json.dumps(cli))
    assert packet_for(paths, kwargs)["status"] == "incomplete"


@pytest.mark.parametrize("name", ["cli", "browser"])
def test_non_object_surface_reports_cannot_be_published(published, name):
    paths, kwargs = published
    paths[name].write_text(json.dumps(["pass"]))
    assert packet_for(paths, kwargs)["status"] == "incomplete"


def test_non_object_cleanup_receipt_cannot_be_published(published):
    paths, kwargs = published
    report = json.loads(paths["browser"].read_text())
    Path(report["cleanup_ref"]).write_text(json.dumps(["verified"]))
    assert packet_for(paths, kwargs)["status"] == "incomplete"
