import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
MODULE = Path(__file__).resolve().parents[1]
SUITE = ROOT / "modules/insights/tests/devlake_poc/test_s02.py"
FIXTURE = Path(__file__).with_name("iam-renewal-scenario.json")
CHECKS = (
    "modules.insights.tests.devlake_poc.test_s02.MigrationReceiptTest.test_fresh_iam_and_full_tls_are_used_for_each_physical_readback",
    "modules.insights.tests.devlake_poc.test_s02.MigrationReceiptTest.test_new_database_connections_renew_tokens_after_fifteen_minutes",
    "modules.insights.tests.devlake_poc.test_s02.SchemaMigrationTest.test_migration_transaction_and_receipt_use_separate_fresh_tokens",
)


def select(name, fixture_path=FIXTURE):
    if name != "test_iam_renewal":
        raise ValueError("unsupported IAM renewal scenario selector")
    fixture = json.loads(Path(fixture_path).read_text())
    if (set(fixture) != {"scenario", "scope", "controlled_age_seconds", "physical_connections",
                         "receipt_reader_sha256", "migration_runner_sha256", "test_suite_sha256", "checks"} or
            fixture["scenario"] != name or fixture["scope"] != "offline-process-connections-only" or
            fixture["controlled_age_seconds"] != 16 * 60 or fixture["physical_connections"] != 2 or
            fixture["checks"] != list(CHECKS) or
            fixture["receipt_reader_sha256"] != hashlib.sha256((MODULE / "migration_receipt.py").read_bytes()).hexdigest() or
            fixture["migration_runner_sha256"] != hashlib.sha256((MODULE / "migrate.py").read_bytes()).hexdigest() or
            fixture["test_suite_sha256"] != hashlib.sha256(SUITE.read_bytes()).hexdigest()):
        raise ValueError("IAM renewal scenario fixture differs from pinned clients or checks")

    for check in CHECKS:
        subprocess.run([sys.executable, "-m", "unittest", "-q", check], cwd=ROOT, check=True)
    return {"scenario": name, "result": "offline-client-checks-passed",
            "worker_database_client": "not_implemented", "expired_static_token_server_rejection": "not_run",
            "live_aged_backend_connections": "not_run"}


def main():
    parser = argparse.ArgumentParser(description="Offline IAM database renewal scenario selector")
    parser.add_argument("--scenario", required=True, choices=["test_iam_renewal"])
    arguments = parser.parse_args()
    try:
        print(json.dumps(select(arguments.scenario), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"local IAM renewal scenario failed: {error}\n")


if __name__ == "__main__":
    main()
