"""A producer exports real checkout/account/provenance evidence without green defaults."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[2] / "scripts" / "repository-scan-receipt.py"
SPEC = importlib.util.spec_from_file_location("scan_export", SOURCE)
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


@pytest.fixture
def inputs(tmp_path):
    env = dict(
        CONTEXT_SOURCE="a" * 40,
        CONTEXT_REVISION="b" * 40,
        CONTEXT_WORKFLOW=".github/workflows/security-scan.yml",
        GITHUB_REPOSITORY="o/r",
        GITHUB_SHA="b" * 40,
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_WORKFLOW_REF="o/r/.github/workflows/security-scan.yml@refs/heads/main",
        SCAN_EXPECTED_ACCOUNT="123456789012",
        AWS_REGION="us-east-1",
        CONTEXT_CORRELATION="c" * 64,
        SCAN_INPUTS_JSON='{"expected_account_id":"123456789012","region":"us-east-1"}',
        CONTEXT_REPOSITORY_ID="123",
        GITHUB_RUN_ID="10",
        GITHUB_RUN_ATTEMPT="1",
    )
    provenance = tmp_path / "controller.json"
    provenance.write_text('{"source":"actual-build"}')
    observed = dict(
        source_revision="a" * 40,
        coverage_complete=False,
        cleanup_complete=True,
        images={"controller": dict(digest="sha256:" + "d" * 64, provenance_path="controller.json")},
    )
    return env, observed, tmp_path


def test_export_preserves_failed_coverage_and_hashes_the_actual_provenance_file(inputs):
    env, observed, root = inputs
    identity = export.context(env, lambda command: {"Account": "123456789012"}, "a" * 40)
    receipt = export.scan_receipt(identity, observed, root)
    assert receipt["coverage_complete"] is False and receipt["cleanup_complete"] is True
    assert receipt["images"]["controller"]["provenance_sha256"] == hashlib.sha256((root / "controller.json").read_bytes()).hexdigest()


@pytest.mark.parametrize("change", ["source", "account", "event", "workflow", "revision", "secret"])
def test_identity_mismatch_refuses_context(inputs, change):
    env, _, _ = inputs
    if change == "source":
        env["CONTEXT_SOURCE"] = "d" * 40
    elif change == "account":
        env["SCAN_EXPECTED_ACCOUNT"] = "999999999999"
    elif change == "event":
        env["GITHUB_EVENT_NAME"] = "schedule"
    elif change == "workflow":
        env["CONTEXT_WORKFLOW"] = ".github/workflows/other.yml"
    elif change == "revision":
        env["CONTEXT_REVISION"] = "d" * 40
    else:
        env["SCAN_INPUTS_JSON"] = '{"token":"do-not-publish"}'
    with pytest.raises(ValueError):
        export.context(env, lambda command: {"Account": "123456789012"}, "a" * 40)


@pytest.mark.parametrize("change", ["missing", "traversal", "digest", "boolean", "source"])
def test_missing_or_invalid_scanner_results_never_become_success(inputs, change):
    env, observed, root = inputs
    if change in {"missing", "traversal"}:
        observed["images"]["controller"]["provenance_path"] = "missing.json" if change == "missing" else "../outside.json"
    elif change == "digest":
        observed["images"]["controller"]["digest"] = "latest"
    elif change == "boolean":
        observed["coverage_complete"] = 1
    else:
        observed["source_revision"] = "d" * 40
    identity = export.context(env, lambda command: {"Account": "123456789012"}, "a" * 40)
    with pytest.raises(ValueError):
        export.scan_receipt(identity, observed, root)
