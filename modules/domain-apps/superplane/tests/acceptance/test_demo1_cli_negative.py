"""CLI refusal cases exercise the entry point, not only its parsers."""

import json
import os

import pytest
from test_demo1_c1 import preview_body
from test_demo1_cli import fixture_documents, identifier, run_cli, write_private


def invoke(tmp_path, selected, fixture):
    private_path = tmp_path / "private.json"
    fixture_path = tmp_path / "fixture.json"
    report_path = tmp_path / "report.json"
    write_private(private_path, selected)
    write_private(fixture_path, fixture)
    result = run_cli(
        "--mode",
        "fixture",
        "--private-input",
        str(private_path),
        "--fixture",
        str(fixture_path),
        "--report",
        str(report_path),
    )
    return result, report_path


def test_live_mode_refuses_before_reading_inputs_or_writing_evidence(tmp_path):
    report_path = tmp_path / "report.json"
    result = run_cli(
        "--mode",
        "live",
        "--private-input",
        str(tmp_path / "absent-input"),
        "--fixture",
        str(tmp_path / "absent-fixture"),
        "--report",
        str(report_path),
    )
    assert result.returncode == 2
    assert "BLOCKED" in result.stdout
    assert "absent-input" not in result.stdout + result.stderr
    assert not report_path.exists()


@pytest.mark.parametrize("status", [403, 503])
def test_preview_denial_or_unavailability_reports_blocked_without_body_echo(
    tmp_path, status
):
    selected, fixture = fixture_documents()
    fixture["retirement"] = {
        "status_code": status,
        "operation_id": identifier(9),
        "body": {"private_response": "example-secret-should-not-appear"},
    }
    result, report_path = invoke(tmp_path, selected, fixture)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["scenario"]["checks"]["removal"]["status"] == "BLOCKED"
    assert report["scenario"]["retirement_preview"]["cost"] == "UNKNOWN"
    assert report["criteria"]["AC-02"]["status"] == "BLOCKED"
    assert "example-secret-should-not-appear" not in str(report)


@pytest.mark.parametrize(
    "change",
    [
        {"admission_available": True, "blocked_reason": None},
        {"account_id": "000000000001"},
        {"approval_request": {"approval_id": identifier(31)}},
    ],
)
def test_unreviewed_admission_or_foreign_preview_fails_without_private_data(
    tmp_path,
    change,
):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    body = preview_body(selected, phase, identifier(9))
    body.update(change)
    fixture["retirement"] = {
        "status_code": 200,
        "operation_id": identifier(9),
        "body": body,
    }
    result, report_path = invoke(tmp_path, selected, fixture)
    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["scenario"]["checks"]["removal"]["status"] == "FAIL"
    assert report["criteria"]["AC-01"]["status"] == "FAIL"
    assert selected["account"] not in result.stdout + result.stderr + str(report)
    assert body["lifecycle_artifact_id"] not in str(report)


@pytest.mark.parametrize("reused_identity", ["request", "operation"])
def test_retirement_cannot_reuse_creation_identity(tmp_path, reused_identity):
    selected, fixture = fixture_documents()
    fixture["retirement"] = {
        "status_code": 503,
        "operation_id": selected["request_id"]
        if reused_identity == "request"
        else fixture["phases"][0]["operation_id"],
        "body": None,
    }
    result, report_path = invoke(tmp_path, selected, fixture)
    assert result.returncode == 1
    assert (
        json.loads(report_path.read_text())["scenario"]["checks"]["removal"]["status"]
        == "FAIL"
    )


@pytest.mark.parametrize("bad_input", ["world-readable", "symlink", "duplicate-json"])
def test_private_input_refusal_does_not_publish_report(tmp_path, bad_input):
    selected, fixture = fixture_documents()
    private_path = tmp_path / "private.json"
    fixture_path = tmp_path / "fixture.json"
    report_path = tmp_path / "report.json"
    write_private(private_path, selected)
    write_private(fixture_path, fixture)
    if bad_input == "world-readable":
        private_path.chmod(0o644)
    elif bad_input == "symlink":
        alias = tmp_path / "link.json"
        alias.symlink_to(private_path)
        private_path = alias
    else:
        private_path.write_text('{"version":"demo1-v1","version":"demo1-v1"}')
        private_path.chmod(0o600)
    result = run_cli(
        "--mode",
        "fixture",
        "--private-input",
        str(private_path),
        "--fixture",
        str(fixture_path),
        "--report",
        str(report_path),
    )
    assert result.returncode == 2
    assert "BLOCKED" in result.stdout
    assert selected["account"] not in result.stdout + result.stderr
    assert not report_path.exists()


@pytest.mark.parametrize("pipe_input", ["private", "fixture"])
def test_named_pipe_input_is_refused_without_waiting_for_a_writer(tmp_path, pipe_input):
    selected, fixture = fixture_documents()
    private_path = tmp_path / "private.json"
    fixture_path = tmp_path / "fixture.json"
    report_path = tmp_path / "report.json"
    write_private(private_path, selected)
    write_private(fixture_path, fixture)
    pipe_path = private_path if pipe_input == "private" else fixture_path
    pipe_path.unlink()
    os.mkfifo(pipe_path, mode=0o600)

    result = run_cli(
        "--mode",
        "fixture",
        "--private-input",
        str(private_path),
        "--fixture",
        str(fixture_path),
        "--report",
        str(report_path),
    )

    assert result.returncode == 2
    assert "private regular file" in result.stdout
    assert "BLOCKED" in result.stdout
    assert str(pipe_path) not in result.stdout
    assert selected["account"] not in result.stdout
    assert result.stderr == ""
    assert not report_path.exists()


def test_missing_release_digest_refused_before_fixture_becomes_evidence(tmp_path):
    selected, fixture = fixture_documents()
    selected["release_source"] = "unapproved-source"
    result, report_path = invoke(tmp_path, selected, fixture)
    assert result.returncode == 2
    assert "BLOCKED" in result.stdout
    assert "unapproved-source" not in result.stdout + result.stderr
    assert not report_path.exists()


def test_cross_operation_approval_replay_is_fail_not_blocked(tmp_path):
    selected, fixture = fixture_documents()
    original = fixture["phases"][0]
    fixture["phases"].append(
        {
            **original,
            "phase_name": "bootstrap",
            "operation_id": identifier(9),
            "artifact_digest": "e" * 64,
            "attempt_id": "attempt-2",
            "fence": "fence-2",
            "observed_at": "2026-10-05T11:01:00+00:00",
        }
    )
    result, report_path = invoke(tmp_path, selected, fixture)
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["scenario"]["checks"]["creation"]["status"] == "FAIL"
