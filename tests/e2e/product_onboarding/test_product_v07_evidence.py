"""Synthetic packet fixtures cannot replace B06 deployed acceptance."""

import hashlib
import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.new_ui.product_release import private_capture_directory
from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.product_v07_evidence import (
    BOLT_ID, CASES, DECISION_PROCEDURE, build_packet, main, review_binding, write_packet,
)
from tests.e2e.product_onboarding.test_v07 import (
    captured_effects_fixture, guided_surface_fixture, recovery_selector_fixture,
)


def packet_fixture(directory):
    run_id = "product-42-1"
    cli_hashes = {"adp": "f" * 64, "adp-onboard.py": "e" * 64}
    _, guided = guided_surface_fixture(directory)
    for point, report in guided.items():
        product_id = "fixture-" + point
        cleanup = report["cleanup"]
        cleanup.update({"product_id": product_id, "project_id": "fixture-project",
                        "owned_absent": True, "cleanup_revision": 2})
        screenshot = directory / f"{run_id}-guidance_{point}.png"
        screenshot.write_bytes(b"synthetic screenshot")
        screenshot.chmod(0o600)
        browser = json.loads(Path(report["artifacts"]["browser"]).read_text())
        browser["screenshot"] = str(screenshot)
        Path(report["artifacts"]["browser"]).write_text(json.dumps(browser))
        cli = json.loads(Path(report["artifacts"]["installed_cli"]).read_text())
        for event in cli:
            event.update(installed_cli_sha256=cli_hashes["adp"], stdout='{"status":"ok"}', stderr="")
        Path(report["artifacts"]["installed_cli"]).write_text(json.dumps(cli))
        api = json.loads(Path(report["artifacts"]["api"]).read_text())
        api[0]["body"] = {"product_id": product_id, "project_id": "fixture-project"}
        Path(report["artifacts"]["api"]).write_text(json.dumps(api))
        Path(report["artifacts"]["cleanup"]).write_text(json.dumps([cleanup]))
        private_json(directory / f"{run_id}-guidance_{point}-report.json", report)

    effects = captured_effects_fixture()
    effects_ref = private_json(directory / f"{run_id}-guidance-effects.json", effects)
    usage_ref = private_json(directory / f"{run_id}-effects-usage.json", {"run_id": run_id, "turns": effects["turns"]})
    effects_cleanup = {"run_id": run_id, "product_id": "effects-product", "project_id": "effects-project",
                       "fixture_removed": True, "owned_absent": True, "shared_unchanged": True, "cleanup_revision": 2}
    effects_cleanup_ref = private_json(directory / f"{run_id}-effects-cleanup.json", effects_cleanup)
    _, resume, data = recovery_selector_fixture(directory)
    for event in data["installed_cli"]:
        event.update(installed_cli_sha256=cli_hashes["adp"], stdout='{"status":"ok"}', stderr="")
    Path(resume["artifacts"]["installed_cli"]).write_text(json.dumps(data["installed_cli"]))
    resume_ref = private_json(directory / f"{run_id}-guidance-resume-report.json", resume)

    identities = {"source_revision": "a" * 40, "requirements_revision": "b" * 40,
                  "api_workflow_revision": "c" * 40, "frontend_workflow_revision": "d" * 40,
                  "schema_head": "115_product_onboarding_sessions", "api_image_digest": "sha256:" + "e" * 64,
                  "frontend_release_sha256": "f" * 64, "frontend_release_ref": "evidence/frontend-release.json",
                  "cli_checksum": "a" * 64}
    attempts = {}
    for criterion, (scenario, selector, labels) in CASES.items():
        attempts[criterion] = []
        for label in labels:
            if criterion == "AC-01":
                report = guided[label]
                artifact = str(directory / f"{run_id}-guidance_{label}-report.json")
                readback = report["artifacts"]["api"]
            elif criterion == "AC-02":
                artifact, readback = effects_ref, usage_ref
            else:
                artifact, readback = resume_ref, resume["artifacts"]["api"]
            attempt = {"attempt_id": f"{run_id}/{criterion}/{label}", "run_id": run_id,
                       "workflow_run_id": 42, "workflow_attempt": 1, "case_id": label,
                       "scenario_id": scenario, "test_ref": selector, "result": "pass",
                       "requirements_revision": identities["requirements_revision"],
                       "source_revision": identities["source_revision"], "artifact_ref": artifact,
                       "readback_ref": readback, "expected": "Independent client assertions pass",
                       "actual": "Synthetic fixture assertions passed"}
            if criterion == "AC-03":
                attempt["source_attempt_id"] = next(item["attempt_id"] for item in resume["attempts"]
                                                    if item["stage"] == label)
            attempts[criterion].append(attempt)
    manifest_ref = private_json(directory / f"{run_id}-v07-attempts.json", {"run_id": run_id, "attempts": attempts})
    return {"run_id": run_id, "source_revision": identities["source_revision"],
            "requirements_revision": identities["requirements_revision"], "plan_revision": "b" * 40,
            "identities": identities, "deployment": {"receipt_ref": "receipts/release.json",
            "observation_ref": "evidence/runtime-observation.json",
            "observed": {**identities, "product_onboarding_enabled": True}},
            "cli_hashes": cli_hashes, "effects_fixture": {"tenant_id": "tenant", "actor_id": "owner",
            "product_id": "effects-product", "project_id": "effects-project", "usage_ref": usage_ref,
            "cleanup_ref": effects_cleanup_ref}, "attempt_manifest_ref": manifest_ref}


def test_v07_packet_versions_private_results_and_every_attempt(tmp_path):
    directory = private_capture_directory(tmp_path / "run")
    inputs = packet_fixture(directory)
    path, packet = write_packet(inputs, directory)
    assert packet == build_packet(inputs, directory)
    assert packet["status"] == "review_ready" and packet["issues"] == []
    assert packet["evidence_schema"] == "product-v07/v1" and packet["bolt_id"] == "B06"
    assert set(packet["cases"]) == set(CASES)
    assert all(len(packet["cases"][criterion]["attempts"]) == len(labels)
               for criterion, (_, _, labels) in CASES.items())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == packet
    content = {key: value for key, value in packet.items() if key not in ("packet_revision", "review")}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert packet["packet_revision"] == "sha256:" + digest and path.name == f"{inputs['run_id']}-v07-{digest}.json"


def test_v07_packet_binds_one_pending_bolt_review_to_exact_revisions(tmp_path):
    directory = private_capture_directory(tmp_path / "run")
    path, packet = write_packet(packet_fixture(directory), directory)
    expected = {"ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
                "procedure_ref": DECISION_PROCEDURE, "plan_revision": packet["plan_revision"],
                "packet_revision": packet["packet_revision"], "status": "pending"}
    assert review_binding(packet) == expected
    assert json.loads(path.read_text())["review"] == expected


@pytest.mark.parametrize("field,value", (
    ("status", "incomplete"), ("plan_revision", "0" * 40),
    ("packet_revision", "sha256:" + "0" * 64), ("review", None),
))
def test_v07_packet_rejects_tampered_review_binding(tmp_path, field, value):
    directory = private_capture_directory(tmp_path / "run")
    packet = build_packet(packet_fixture(directory), directory)
    packet[field] = value
    with pytest.raises(ValueError, match="decision|Incomplete"):
        review_binding(packet)


@pytest.mark.parametrize("field,value", (
    ("bolt_id", "B03"), ("procedure_ref", "other-review"),
    ("plan_revision", "0" * 40), ("packet_revision", "sha256:" + "0" * 64),
    ("status", "accepted"),
))
def test_v07_packet_rejects_changed_decision_reference(tmp_path, field, value):
    directory = private_capture_directory(tmp_path / "run")
    packet = build_packet(packet_fixture(directory), directory)
    packet["review"][field] = value
    with pytest.raises(ValueError, match="decision binding"):
        review_binding(packet)


def test_v07_packet_cli_reports_only_status_and_preserves_incomplete_attempts(tmp_path, capsys):
    directory = private_capture_directory(tmp_path / "run")
    inputs = packet_fixture(directory)
    manifest_path = Path(inputs["attempt_manifest_ref"])
    manifest = json.loads(manifest_path.read_text())
    manifest["attempts"]["AC-01"][0]["result"] = "fail"
    manifest_path.write_text(json.dumps(manifest))
    input_path = private_json(directory / "private-inputs.json", inputs)
    assert main(["--inputs", input_path, "--directory", str(directory)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "incomplete"
    assert str(directory) not in json.dumps(output)
    packet_path = directory / f"{inputs['run_id']}-v07-{output['packet_revision'].removeprefix('sha256:')}.json"
    packet = json.loads(packet_path.read_text())
    assert packet["cases"]["AC-01"]["attempts"][0]["result"] == "fail"
    assert packet["review"]["status"] == "not_ready"
    with pytest.raises(ValueError, match="Incomplete"):
        review_binding(packet)


@pytest.mark.parametrize("fault", ("missing_guided", "missing_screenshot", "public_screenshot", "wrong_cli", "missing_cli_output",
                                     "missing_effects", "wrong_usage", "missing_effects_cleanup", "failed_cleanup",
                                     "wrong_revision", "wrong_schema", "wrong_deployment", "same_receipts",
                                     "missing_manifest", "missing_attempt", "failed_attempt", "stale_attempt",
                                     "crossed_readback", "missing_recovery", "wrong_source_attempt"))
def test_v07_packet_rejects_missing_failed_or_crossed_evidence(tmp_path, fault):
    directory = private_capture_directory(tmp_path / "run")
    inputs = packet_fixture(directory)
    if fault == "missing_guided":
        (directory / f"{inputs['run_id']}-guidance_idea-report.json").unlink()
    elif fault in ("missing_screenshot", "public_screenshot"):
        image = directory / f"{inputs['run_id']}-guidance_idea.png"
        image.unlink() if fault == "missing_screenshot" else image.chmod(0o644)
    elif fault == "wrong_cli":
        inputs["cli_hashes"]["adp"] = "0" * 64
    elif fault == "missing_cli_output":
        path = directory / f"{inputs['run_id']}-guidance_idea-cli.json"
        events = json.loads(path.read_text())
        del events[0]["stdout"]
        path.write_text(json.dumps(events))
    elif fault == "missing_effects":
        (directory / f"{inputs['run_id']}-guidance-effects.json").unlink()
    elif fault == "wrong_usage":
        Path(inputs["effects_fixture"]["usage_ref"]).write_text(json.dumps({"run_id": inputs["run_id"], "turns": {}}))
    elif fault == "missing_effects_cleanup":
        Path(inputs["effects_fixture"]["cleanup_ref"]).unlink()
    elif fault == "failed_cleanup":
        Path(inputs["effects_fixture"]["cleanup_ref"]).write_text(json.dumps({"run_id": inputs["run_id"], "fixture_removed": False}))
    elif fault == "wrong_revision":
        inputs["requirements_revision"] = "0" * 40
    elif fault == "wrong_schema":
        inputs["identities"]["schema_head"] = "not_observed"
    elif fault == "wrong_deployment":
        inputs["deployment"]["observed"]["product_onboarding_enabled"] = False
    elif fault == "same_receipts":
        inputs["deployment"]["observation_ref"] = inputs["deployment"]["receipt_ref"]
    elif fault == "missing_manifest":
        Path(inputs["attempt_manifest_ref"]).unlink()
    elif fault in ("missing_attempt", "failed_attempt", "stale_attempt", "crossed_readback", "wrong_source_attempt"):
        path = Path(inputs["attempt_manifest_ref"])
        manifest = json.loads(path.read_text())
        attempts = manifest["attempts"]["AC-03" if fault == "wrong_source_attempt" else "AC-01"]
        if fault == "missing_attempt":
            attempts.pop()
        elif fault == "failed_attempt":
            attempts[0]["result"] = "fail"
        elif fault == "stale_attempt":
            attempts[0]["source_revision"] = "0" * 40
        elif fault == "crossed_readback":
            attempts[0]["readback_ref"] = attempts[0]["artifact_ref"]
        else:
            attempts[0]["source_attempt_id"] = "other-stage"
        path.write_text(json.dumps(manifest))
    else:
        (directory / f"{inputs['run_id']}-guidance-resume-report.json").unlink()
    path, packet = write_packet(inputs, directory)
    assert packet["status"] == "incomplete" and packet["issues"]
    assert json.loads(path.read_text()) == packet
    if fault == "failed_attempt":
        assert packet["cases"]["AC-01"]["attempts"][0]["result"] == "fail"
        assert packet["cases"]["AC-01"]["status"] == "incomplete"
