"""Offline checks for the operator handoff; these cannot establish live acceptance."""

from pathlib import Path

import pytest


RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "runner-registration-live.md"


@pytest.mark.parametrize("required", [
    "run-owned tenant-parent group",
    "10-minute deadline",
    "get-caller-identity",
    "approved versioned private S3 evidence",
    "runner_type=group_type",
    "run_untagged=false",
    "locked=true",
    "AWSPENDING",
    "AWSCURRENT",
    "old secret version ID",
    "AccessDenied",
    "final absence",
    "--create",
    "--rotate",
    "--revoke",
    "--reconcile",
    "--approved-root-sha256",
])
def test_live_runbook_covers_bounded_and_verifiable_actions(required):
    assert required.lower() in RUNBOOK.read_text().lower()


def test_live_runbook_requires_independent_manager_and_no_credential_shortcuts():
    instructions = RUNBOOK.read_text()
    assert "manager integration" in instructions
    assert "old-token rejection oracle" in instructions
    assert "the CLI's normal `--revoke` refuses an unfinished staging record" in instructions
    assert "not checked" in instructions
    assert all(unsafe not in instructions for unsafe in ("--insecure", "verify=False", "set -x"))


def test_live_runbook_contains_no_deployment_identifiers():
    from runpy import run_path

    scanner = Path(__file__).resolve().parents[5] / "scripts" / "check-public-docs.py"
    findings = run_path(str(scanner))["findings"]
    assert list(findings(RUNBOOK.read_text())) == []
