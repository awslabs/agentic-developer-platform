"""Controlled report fixtures: these tests never claim actual provider acceptance."""

import copy
import json
import os
import subprocess
import sys

import pytest

from tests.e2e.tools import atlassian_flow_summary as summary


BINDING = {
    "source_revision": "a" * 40,
    "definition_revision": "b" * 40,
    "run_id": "314",
    "run_attempt": "2",
    "release_sha256": "c" * 64,
}
STORY_COUNTS = {
    8446: 3,
    8447: 4,
    8448: 4,
    8449: 5,
    8451: 3,
    8450: 4,
    8452: 3,
    8453: 4,
    8454: 6,
}
STAGE_ISSUES = {
    "identity": 8448,
    "reads-parity": 8449,
    "connections": 8451,
    "write-recovery": 8450,
    "clients": 8452,
    "release-automation": 8453,
    "harness": 8454,
}
TOOLS = (
    "search_jira",
    "get_jira_issue",
    "search_confluence",
    "get_confluence_page",
    "create_jira_issue",
    "update_jira_issue",
    "comment_jira_issue",
    "transition_jira_issue",
)
CASES = (
    "release-six-components",
    "adapter-four-routes",
    "core-health-through-outage",
    "role-site-owner-denials",
    "consent-two-users-sites",
    "identity-expiry-refresh",
    "disconnect-revoke",
    "catalogue-next-session",
    "claude-schema-loading",
    "codex-schema-loading",
    "legacy-all-verbs",
    "partial-routing-apply",
    "controller-restart-stale-lease",
    "group-rollback-redeploy",
    "unknown-write-no-replay",
    "report-recovery",
    "runner-loss-resume",
    "fixture-cleanup",
    "legacy-8076",
    "legacy-7266",
    "unrelated-runtimes",
)


@pytest.fixture
def reports():
    live = {
        "binding": copy.deepcopy(BINDING),
        "matrix_complete": True,
        "tool_cells": [
            {
                "cell_id": f"{client}:atlassian___{tool}",
                "status": "pass",
                "cleanup": "confirmed",
            }
            for client in ("claude", "codex")
            for tool in TOOLS
        ],
        "cases": [{"case_id": name, "status": "pass"} for name in CASES],
        "cleanup_status": "confirmed",
        "unresolved_operations": [],
        "final_release_state": "candidate_verified",
        "provider_live_attestation": True,
        "unresolved_prerequisites": [],
        "criteria": [
            {
                "criterion_id": f"{issue}.AC-{index:02}",
                "status": "pass",
                "evidence_verified": True,
            }
            for issue, count in STORY_COUNTS.items()
            for index in range(1, count + 1)
        ]
        + [
            {
                "criterion_id": f"8445.EC-{index:02}",
                "status": "pass",
                "evidence_verified": True,
            }
            for index in range(1, 7)
        ],
        "stages": [
            {"stage_id": stage, "status": "pass", "runtime_verified": True}
            for stage in ("identity-proof", "foundation", *STAGE_ISSUES)
        ],
    }
    stages = {
        stage: {
            "binding": copy.deepcopy(BINDING),
            "stage_id": stage,
            "criteria": [
                {
                    "criterion_id": f"{issue}.AC-{index:02}",
                    "phase_status": "pass",
                    "evidence_verified": True,
                }
                for index in range(1, STORY_COUNTS[issue] + 1)
            ],
            "runtime_verified": True,
            "independent_verification": "confirmed",
            "cleanup_status": "confirmed",
            "unresolved_operations": [],
            "verification_level": "controlled_integration",
            "fixture_source": "generated",
            "provider_live_attestation": False,
            "test_runtime_artifacts_match": True,
        }
        for stage, issue in STAGE_ISSUES.items()
    }
    return live, stages


def assemble(reports):
    return summary.assemble_summary(*reports, binding=BINDING)


def test_contract_retains_every_stage_and_original_acceptance_clause(reports):
    before = copy.deepcopy(reports)
    result = assemble(reports)
    assert result["completion_ready"] is True
    assert len(result["criteria"]) == 42
    assert len(result["tool_cells"]) == 16 and len(result["cases"]) == 21
    assert set(result["stage_reports"]) == set(STAGE_ISSUES)
    assert len(summary.completion_contract()) == 81
    assert result["aggregation"]["failed_predicates"] == []
    assert summary.verify_summary(result, *reports, binding=BINDING) == result
    assert reports == before


@pytest.mark.parametrize(
    "change,failed",
    [
        (lambda live, stages: live["tool_cells"].pop(), "live.new-tools"),
        (
            lambda live, stages: live["tool_cells"].append(
                copy.deepcopy(live["tool_cells"][0])
            ),
            "live.new-tools",
        ),
        (
            lambda live, stages: live["tool_cells"][0].update(status="skipped"),
            "live.new-tools",
        ),
        (
            lambda live, stages: live["tool_cells"][0].update(cleanup="unknown"),
            "live.new-tools",
        ),
        (lambda live, stages: live["cases"].pop(), "live.coverage"),
        (
            lambda live, stages: live["cases"][0].update(status="not_run"),
            "live.coverage",
        ),
        (lambda live, stages: live["criteria"].pop(), "completion.criteria"),
        (
            lambda live, stages: live["criteria"][0].update(evidence_verified=1),
            "completion.criteria",
        ),
        (
            lambda live, stages: live["stages"][0].update(runtime_verified=1),
            "completion.stages",
        ),
        (lambda live, stages: live.update(matrix_complete=1), "live.complete"),
        (
            lambda live, stages: live.update(provider_live_attestation=1),
            "live.provider-attestation",
        ),
        (
            lambda live, stages: live.update(provider_live_attestation=False),
            "live.provider-attestation",
        ),
        (
            lambda live, stages: live.update(unresolved_operations=["uncertain-write"]),
            "live.unknowns",
        ),
        (
            lambda live, stages: live.update(unresolved_prerequisites=["consent"]),
            "completion.authority",
        ),
        (
            lambda live, stages: live.update(final_release_state="rollback_pending"),
            "live.final-state",
        ),
        (lambda live, stages: stages.pop("identity"), "identity.identity"),
        (lambda live, stages: stages["harness"]["criteria"].pop(), "harness.cases"),
        (
            lambda live, stages: stages["identity"].update(
                provider_live_attestation=True
            ),
            "identity.provider_live_attestation",
        ),
        (
            lambda live, stages: stages["identity"].update(provider_live_attestation=0),
            "identity.provider_live_attestation",
        ),
        (
            lambda live, stages: stages["clients"].update(
                test_runtime_artifacts_match=False
            ),
            "clients.test_runtime_artifacts_match",
        ),
        (
            lambda live, stages: live.update(completion_ready=False),
            "completion.producer-hold",
        ),
    ],
)
def test_missing_skipped_uncertain_or_promoted_report_cannot_complete(
    reports, change, failed
):
    change(*reports)
    result = assemble(reports)
    assert result["completion_ready"] is False
    assert failed in result["aggregation"]["failed_predicates"]
    with pytest.raises(ValueError, match="Acceptance is incomplete"):
        summary.verify_summary(result, *reports, binding=BINDING)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "d" * 40),
        ("definition_revision", "d" * 40),
        ("run_id", "315"),
        ("run_attempt", "1"),
        ("release_sha256", "d" * 64),
    ],
)
def test_release_and_attempt_bindings_cannot_be_mixed(reports, field, value):
    reports[1]["identity"]["binding"][field] = value
    result = assemble(reports)
    assert result["completion_ready"] is False
    assert result["aggregation"]["failed_predicates"] == ["binding.identity"]


def test_binding_is_selected_by_workflow_not_by_report(reports):
    reports[0].pop("binding")
    assert assemble(reports)["aggregation"]["failed_predicates"] == ["binding.live"]
    with pytest.raises(ValueError, match="workflow-selected"):
        summary.assemble_summary(*reports, binding={})


def test_summary_is_rebuilt_after_source_readback(reports):
    result = assemble(reports)
    reports[0]["cleanup_status"] = "unknown"
    with pytest.raises(ValueError, match="differs from the source reports"):
        summary.verify_summary(result, *reports, binding=BINDING)


def test_rewriting_summary_or_hidden_source_fields_changes_digest(reports):
    result = assemble(reports)
    result["stage_reports"]["identity"]["criteria"][0]["evidence_verified"] = 1
    with pytest.raises(ValueError, match="differs"):
        summary.verify_summary(result, *reports, binding=BINDING)
    result = assemble(reports)
    reports[1]["identity"]["observation_reference"] = "new-private-reference"
    with pytest.raises(ValueError, match="differs"):
        summary.verify_summary(result, *reports, binding=BINDING)


def test_empty_inputs_remain_explicitly_incomplete():
    result = summary.assemble_summary({}, {}, binding=BINDING)
    assert result["completion_ready"] is False
    assert "provider_live_attestation" not in result
    assert result["stage_reports"] == {}
    assert "live.provider-attestation" in result["aggregation"]["failed_predicates"]


def private_files(root, reports):
    root.mkdir(mode=0o700)
    entries = {
        "binding.json": BINDING,
        "live.json": reports[0],
        **{f"stage-{stage}.json": report for stage, report in reports[1].items()},
    }
    for name, value in entries.items():
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)


def command(root, action):
    return [
        sys.executable,
        "-m",
        "tests.e2e.tools.atlassian_flow_summary",
        action,
        "--inputs",
        str(root),
        "--binding",
        str(root / "binding.json"),
        "--summary",
        str(root / "atlassian-flow-summary.json"),
    ]


def test_executable_assembly_and_check_from_private_data(reports, tmp_path):
    root = tmp_path / "private"
    private_files(root, reports)
    before = subprocess.run(
        command(root, "assemble"), capture_output=True, text=True, timeout=10
    )
    assert before.returncode == 0, before.stdout + before.stderr
    output = root / "atlassian-flow-summary.json"
    assert output.stat().st_mode & 0o077 == 0
    checked = subprocess.run(
        command(root, "check"), capture_output=True, text=True, timeout=10
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    (root / "stage-identity.json").unlink()
    missing = subprocess.run(
        command(root, "check"), capture_output=True, text=True, timeout=10
    )
    assert missing.returncode == 2


def test_cli_retains_incomplete_summary_and_returns_failure(reports, tmp_path):
    root = tmp_path / "private"
    reports[0]["tool_cells"][0]["status"] = "skipped"
    private_files(root, reports)
    result = subprocess.run(
        command(root, "assemble"), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert (
        json.loads((root / "atlassian-flow-summary.json").read_bytes())[
            "completion_ready"
        ]
        is False
    )
    assert "live.new-tools" in result.stdout


@pytest.mark.parametrize(
    "body", [b'{"same":1,"same":2}', b'{"value":NaN}', b'{"value":Infinity}']
)
def test_reader_refuses_ambiguous_json(tmp_path, body):
    tmp_path.chmod(0o700)
    path = tmp_path / "report.json"
    path.write_bytes(body)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        summary._read_private(path)


def test_reader_refuses_symlink_and_world_readable_evidence(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "report.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        summary._read_private(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        summary._read_private(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError):
        summary._read_private(fifo)


def test_output_cannot_overwrite_an_earlier_attempt(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "summary.json"
    summary._write_private(path, {"attempt": "1"})
    with pytest.raises(ValueError):
        summary._write_private(path, {"attempt": "2"})
    assert json.loads(path.read_bytes()) == {"attempt": "1"}
