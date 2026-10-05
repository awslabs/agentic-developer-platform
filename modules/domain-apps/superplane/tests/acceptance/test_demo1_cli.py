"""Exercise the actual fixture CLI, not merely its in-process report helper."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]


def identifier(number: int) -> str:
    return str(UUID(int=number))


def write_private(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def run_cli(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "superplane_acceptance.demo1_cli", *arguments],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def fixture_documents():
    selected = {
        "version": "demo1-v1",
        "release_source": "a" * 40,
        "image_digest": "b" * 64,
        "schema_revision": "013",
        "connection_id": identifier(1),
        "role": "ExampleObserver",
        "account": "123456789012",
        "region": "us-east-1",
        "org_id": identifier(2),
        "requester_id": identifier(3),
        "approver_id": identifier(4),
        "workspace_name": "example-workspace",
        "mode": "managed",
        "cluster_placement": "dedicated",
        "request_id": identifier(5),
        "plan_revision": "c" * 64,
        "budget_usd": "10.00",
        "authorized_at": "2026-10-05T10:00:00+00:00",
        "deadline": "2026-10-05T12:00:00+00:00",
        "cleanup_owner": "example-operator",
        "recovery_checkpoint": "after-bootstrap",
        "survivors": ["example-shared-vpc"],
    }
    phase = {
        "workspace_id": identifier(6),
        "request_id": selected["request_id"],
        "phase_name": "creation",
        "admitted_at": "2026-10-05T10:55:00+00:00",
        "operation_id": identifier(7),
        "approval_id": identifier(8),
        "plan_revision": selected["plan_revision"],
        "artifact_digest": "d" * 64,
        "attempt_id": "attempt-1",
        "fence": "fence-1",
        "observed_at": "2026-10-05T11:00:00+00:00",
        "source": "fixture",
        "state": "observed",
        "approval": {
            "approval_id": identifier(8),
            "requester": selected["requester_id"],
            "approvers": [selected["approver_id"]],
            "result": "allowed-once",
            "decided_by": selected["approver_id"],
            "decided_at": "2026-10-05T10:45:00+00:00",
            "expires_at": "2026-10-05T11:00:00+00:00",
            "revoked": False,
        },
    }
    fixture = {
        "version": "demo1-fixture-v1",
        "phases": [phase],
        "owned_resources": ["example-owned-cluster"],
        "inventory": {
            "connection_id": selected["connection_id"],
            "role": selected["role"],
            "account": selected["account"],
            "region": selected["region"],
            "workspace_id": phase["workspace_id"],
            "status": "complete",
            "owned_present": [],
            "survivors_present": selected["survivors"],
            "cost_usd": "1.25",
            "observed_at": "2026-10-05T11:30:00+00:00",
        },
    }
    return selected, fixture


def test_fixture_command_publishes_private_sanitized_criterion_report(tmp_path):
    selected, fixture = fixture_documents()
    private_path, fixture_path, report_path = (
        tmp_path / name
        for name in (
            "private.json",
            "fixture.json",
            "report.json",
        )
    )
    write_private(private_path, selected)
    write_private(fixture_path, fixture)
    arguments = (
        "--mode",
        "fixture",
        "--private-input",
        str(private_path),
        "--fixture",
        str(fixture_path),
        "--report",
        str(report_path),
    )
    result = run_cli(*arguments)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "BLOCKED" in result.stdout and selected["account"] not in result.stdout
    assert report_path.stat().st_mode & 0o077 == 0
    record = json.loads(report_path.read_text(encoding="utf-8"))
    assert record["version"] == "demo1-cli-v1"
    assert record["contract_checkpoint"] == "2026-10-05.1"
    assert record["scenario"]["overall"] == "BLOCKED"
    assert {key: value["status"] for key, value in record["criteria"].items()} == {
        "AC-01": "BLOCKED",
        "AC-02": "BLOCKED",
        "AC-03": "NOT RUN",
        "AC-04": "NOT RUN",
    }
    assert selected["account"] not in json.dumps(record)
    assert selected["connection_id"] not in json.dumps(record)
    assert run_cli(*arguments).returncode == 2
    assert json.loads(report_path.read_text(encoding="utf-8")) == record
