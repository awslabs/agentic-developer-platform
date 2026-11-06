import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
MODULE = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("optional-mode-scenario.json")
PINNED_FILES = {
    "mode": MODULE / "mode.py",
    "collection_installer": MODULE / "deploy.py",
    "connector_installer": MODULE / "deploy_connectors.py",
    "intent_ledger": MODULE / "intent_ledger.py",
    "artifact_finalizer": MODULE / "finalize_artifacts.py",
    "ordinary_deploy": ROOT / "modules/insights/deploy.sh",
    "mode_tests": ROOT / "modules/insights/tests/devlake_poc/test_s02_modes.py",
    "intent_tests": ROOT / "modules/insights/tests/devlake_poc/test_s02_intent.py",
    "artifact_tests": ROOT / "modules/insights/tests/devlake_poc/test_s09_cleanup.py",
    "finalizer_fixture": ROOT / "modules/insights/tests/devlake_poc/test_s09_finalizer.py",
    "ordinary_tests": ROOT / "modules/insights/tests/test_deploy_script.py",
}
MODES = {"absent": ["false", "false"], "connector-only": ["true", "false"],
         "full": ["true", "true"]}
CHECKS = (
    "modules.insights.tests.devlake_poc.test_s02_modes.OptionalModeTest.test_absent_and_false_defaults_create_no_selection",
    "modules.insights.tests.devlake_poc.test_s02_modes.OptionalModeTest.test_connector_only_and_full_use_case_insensitive_canonical_flags",
    "modules.insights.tests.devlake_poc.test_s02_modes.OptionalModeTest.test_invalid_or_collection_only_flags_fail_before_credentials",
    "modules.insights.tests.devlake_poc.test_s02_modes.OptionalModeTest.test_invalid_environment_flags_reject_without_private_config",
    "modules.insights.tests.test_deploy_script.DeployScriptTest.test_ordinary_plans_and_upgrades_ignore_optional_flags_and_state",
    "modules.insights.tests.devlake_poc.test_s02_intent.OptionalIntentTest.test_collection_retry_keeps_one_protected_intent_and_retains_dependencies",
    "modules.insights.tests.devlake_poc.test_s02_intent.OptionalIntentTest.test_conflict_and_terminal_request_refuse_registration",
    "modules.insights.tests.devlake_poc.test_s02_intent.OptionalIntentTest.test_unqualified_cleanup_blocks_both_installer_apply_paths_before_terraform",
    "modules.insights.tests.devlake_poc.test_s09_cleanup.ArtifactRecoveryTest.test_optional_intent_fences_scheduled_expiry_before_image_cleanup",
    "modules.insights.tests.devlake_poc.test_s09_cleanup.ArtifactRecoveryTest.test_late_scheduler_cleanup_escalates_and_operator_acknowledges",
)


def select(name, fixture_path=FIXTURE):
    if name != "test_optional_deployment_modes":
        raise ValueError("unsupported optional-mode scenario selector")
    fixture = json.loads(Path(fixture_path).read_text())
    if (set(fixture) != {"scenario", "scope", "modes", "poll_seconds", "deadline_seconds", "pins", "checks"} or
            fixture["scenario"] != name or fixture["scope"] != "offline-selection-intent-and-artifact-only" or
            fixture["modes"] != MODES or fixture["poll_seconds"] != 3600 or
            fixture["deadline_seconds"] != 7200 or fixture["checks"] != list(CHECKS) or
            fixture["pins"] != {key: hashlib.sha256(path.read_bytes()).hexdigest()
                                for key, path in PINNED_FILES.items()}):
        raise ValueError("optional-mode scenario fixture differs from pinned modes or checks")

    for check in CHECKS:
        subprocess.run([sys.executable, "-m", "unittest", "-q", check], cwd=ROOT, check=True)
    return {"scenario": name, "result": "offline-mode-intent-and-artifact-checks-passed",
            "worker_drain": "not_implemented", "optional_resource_cleanup": "not_implemented",
            "retained_shared_data": "not_verified", "gateway_broker_admission": "not_integrated",
            "live_transitions": "not_run"}


def main():
    parser = argparse.ArgumentParser(description="Offline optional deployment-mode scenario selector")
    parser.add_argument("--scenario", required=True, choices=["test_optional_deployment_modes"])
    arguments = parser.parse_args()
    try:
        print(json.dumps(select(arguments.scenario), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"local optional-mode scenario failed: {error}\n")


if __name__ == "__main__":
    main()
