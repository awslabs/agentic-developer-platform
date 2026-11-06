"""Synthetic receipt checks; these cannot establish deployed CLI acceptance."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_completion_cli import scope_cli_evaluator
from tests.e2e.product_onboarding.product_v02_record import private_json


@pytest.fixture
def receipt(tmp_path):
    root = tmp_path / "test-results"
    root.mkdir(mode=0o700)
    environment = {
        "PRODUCT_B03_MODE": "true",
        "E2E_NEW_UI_ENABLED": "1",
        "PRODUCT_EXPECTED_REVISION": "a" * 40,
        "PRODUCT_BROWSER_WORKFLOW_REVISION": "b" * 40,
        "GITHUB_SHA": "c" * 40,
        "GITHUB_RUN_ID": "12",
        "GITHUB_RUN_ATTEMPT": "2",
    }
    manifest = "d" * 64
    binary = "e" * 64
    run_id = "product-12-2"
    private_json(root / "cleanup.json", {"verified": True})
    browser = {
        "run_id": run_id,
        "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
        "checkout_revision": environment["GITHUB_SHA"],
        "workflow_revision": environment["PRODUCT_BROWSER_WORKFLOW_REVISION"],
        "workflow_run_id": 12,
        "workflow_attempt": 2,
        "cleanup_ref": str(root / "cleanup.json"),
        "context": {
            "identities": {
                "requirements_revision": "f" * 40,
                "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
                "cli_checksum": manifest,
            },
            "deployment": {
                "observation_ref": private_json(
                    root / "runtime-observation.json", {"observed": True}
                ),
                "observed": {
                    "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
                    "cli_checksum": manifest,
                },
            },
        },
    }
    cli = {
        "run_id": run_id,
        "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
        "workflow_revision": environment["PRODUCT_BROWSER_WORKFLOW_REVISION"],
        "workflow_run_id": 12,
        "workflow_attempt": 2,
        "release_manifest_sha256": manifest,
        "installed_cli_sha256": binary,
        "cleanup": {
            "status": "verified",
            "original_projects_preserved": True,
            "reused_assets_preserved": True,
        },
        "cases": {},
    }
    for number in range(1, 4):
        criterion = f"AC-{number:02}"
        selector = f"case:PR04-C{number:02}"
        artifact = private_json(root / f"{criterion}-result.json", {"result": "pass"})
        readback = private_json(root / f"{criterion}-readback.json", [{"status": 200}])
        stdout = private_json(root / f"{criterion}-stdout.json", ["result"])
        record = {
            "run_id": run_id,
            "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
            "requirements_revision": "f" * 40,
            "workflow_run_id": 12,
            "workflow_attempt": 2,
            "attempt_id": f"{run_id}/{criterion}/cli",
            "surface": "cli",
            "test_ref": selector,
            "result": "pass",
            "installed_cli_sha256": binary,
            "exit_code": 0,
            "artifact_ref": artifact,
            "readback_ref": readback,
            "stdout_ref": stdout,
        }
        entry = {"attempts": [record]}
        if number == 3:
            entry["api_attempts"] = [
                {**record, "surface": "api", "attempt_id": f"{run_id}/{criterion}/api"}
            ]
        cli["cases"][criterion] = entry
    private_json(root / "product-v04-browser-results.json", browser)
    private_json(root / "product-v04-cli-results.json", cli)
    return tmp_path, root, environment, browser, cli


def test_installed_cli_receipts_are_owned_by_grouped_browser_attempt(receipt):
    attempt, root, environment, _, _ = receipt
    evaluate = scope_cli_evaluator(attempt, environment)
    for number in range(1, 4):
        selector = f"case:PR04-C{number:02}"
        result = evaluate(selector)
        assert result["status"] == "passed"
        assert result["selector"] == selector
        assert result["run_id"] == "product-12-2"
        assert result["report_path"] == str(root / "product-v04-cli-results.json")
    with pytest.raises(ValueError, match="reviewed inventory"):
        evaluate("case:other")


@pytest.mark.parametrize(
    "change",
    [
        "stale_browser",
        "wrong_release",
        "stale_cli",
        "missing_cleanup",
        "failed_attempt",
        "missing_api",
        "other_binary",
        "unowned_artifact",
        "world_readable_artifact",
    ],
)
def test_installed_cli_refuses_unverified_results(receipt, change):
    attempt, root, environment, browser, cli = receipt
    browser = deepcopy(browser)
    cli = deepcopy(cli)
    record = cli["cases"]["AC-03"]["attempts"][0]
    if change == "stale_browser":
        browser["workflow_attempt"] = 1
    elif change == "wrong_release":
        cli["release_manifest_sha256"] = "1" * 64
    elif change == "stale_cli":
        record["workflow_run_id"] = 9
    elif change == "missing_cleanup":
        cli["cleanup"]["status"] = "pending"
    elif change == "failed_attempt":
        record["result"] = "fail"
    elif change == "missing_api":
        cli["cases"]["AC-03"]["api_attempts"] = []
    elif change == "other_binary":
        record["installed_cli_sha256"] = "2" * 64
    elif change == "unowned_artifact":
        record["artifact_ref"] = "/tmp/other-run.json"
    elif change == "world_readable_artifact":
        (root / "AC-03-result.json").chmod(0o644)
    (root / "product-v04-browser-results.json").unlink()
    (root / "product-v04-cli-results.json").unlink()
    private_json(root / "product-v04-browser-results.json", browser)
    private_json(root / "product-v04-cli-results.json", cli)
    with pytest.raises(ValueError):
        scope_cli_evaluator(attempt, environment)("case:PR04-C03")


def test_installed_cli_preflight_refuses_missing_correlation_before_claiming_fixture(
    tmp_path,
):
    with pytest.raises(ValueError, match="pinned, authenticated"):
        scope_cli_evaluator(tmp_path, {"PRODUCT_B03_MODE": "true"})
    assert list(tmp_path.iterdir()) == []
