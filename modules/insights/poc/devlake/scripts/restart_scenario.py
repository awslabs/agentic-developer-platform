import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
MODULE = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("restart-scenario.json")
CHECKS = (
    "modules.insights.tests.devlake_poc.test_s02_intent.OptionalIntentTest.test_collection_retry_keeps_one_protected_intent_and_retains_dependencies",
    "modules.insights.tests.devlake_poc.test_s02_intent.OptionalIntentTest.test_conflict_and_terminal_request_refuse_registration",
    "modules.insights.tests.devlake_poc.test_s02_intent.OptionalIntentTest.test_lost_save_response_requires_matching_consistent_readback",
    "modules.insights.tests.devlake_poc.test_s02_intent.OptionalIntentTest.test_unqualified_cleanup_blocks_both_installer_apply_paths_before_terraform",
    "modules.insights.tests.devlake_poc.test_s09_cleanup.ArtifactRecoveryTest.test_optional_intent_fences_abort_before_image_cleanup",
    "modules.insights.tests.devlake_poc.test_s09_cleanup.ArtifactRecoveryTest.test_optional_intent_fences_scheduled_expiry_before_image_cleanup",
    "modules.insights.tests.devlake_poc.test_s09_cleanup.ArtifactRecoveryTest.test_lost_delete_response_retries_exact_intent_without_second_delete",
    "modules.insights.tests.devlake_poc.test_s09_cleanup.ArtifactRecoveryTest.test_missing_held_image_and_forged_receipt_remain_incomplete",
)


def select(name, fixture_path=FIXTURE):
    if name != "test_restart":
        raise ValueError("unsupported restart scenario selector")
    fixture = json.loads(Path(fixture_path).read_text())
    if (set(fixture) != {"scenario", "scope", "intent_ledger_sha256", "artifact_finalizer_sha256", "checks"} or
            fixture["scenario"] != name or fixture["scope"] != "offline-intent-and-artifact-only" or
            fixture["checks"] != list(CHECKS) or
            fixture["intent_ledger_sha256"] != hashlib.sha256((MODULE / "intent_ledger.py").read_bytes()).hexdigest() or
            fixture["artifact_finalizer_sha256"] != hashlib.sha256((MODULE / "finalize_artifacts.py").read_bytes()).hexdigest()):
        raise ValueError("restart scenario fixture differs from pinned ledger or checks")

    for check in CHECKS:
        subprocess.run([sys.executable, "-m", "unittest", "-q", check], cwd=ROOT, check=True)
    return {"scenario": name, "result": "offline-intent-and-artifact-checks-passed",
            "optional_resource_cleanup": "not_implemented", "state_key_loss": "not_run",
            "live_restart": "not_run"}


def main():
    parser = argparse.ArgumentParser(description="Offline optional-intent restart scenario selector")
    parser.add_argument("--scenario", required=True, choices=["test_restart"])
    arguments = parser.parse_args()
    try:
        print(json.dumps(select(arguments.scenario), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"local restart scenario failed: {error}\n")


if __name__ == "__main__":
    main()
