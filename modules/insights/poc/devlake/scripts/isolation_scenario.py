import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
SCHEMA = Path(__file__).resolve().parents[1] / "schema-manifest.json"
FIXTURE = Path(__file__).with_name("isolation-scenario.json")
INFRA = Path(__file__).resolve().parents[1] / "infra"
CHECKS = (
    "modules.insights.tests.devlake_poc.test_s02.CellRolePolicyTest.test_only_registered_cell_tables_receive_scoped_dml_and_read_grants",
    "modules.insights.tests.devlake_poc.test_s02.CellRolePolicyTest.test_wrong_target_and_role_aliases_fail_before_sql",
    "modules.insights.tests.devlake_poc.test_s02.MigrationReceiptTest.test_verified_tls_rejects_foreign_ca_and_wrong_hostname_without_retry",
)


def select(name, fixture_path=FIXTURE):
    if name != "test_isolation":
        raise ValueError("unsupported isolation scenario selector")
    fixture = json.loads(Path(fixture_path).read_text())
    if (set(fixture) != {"scenario", "schema_manifest_sha256", "scope", "checks"} or
            fixture["scenario"] != name or fixture["scope"] != "offline-contract-only" or
            fixture["checks"] != list(CHECKS) or
            fixture["schema_manifest_sha256"] != hashlib.sha256(SCHEMA.read_bytes()).hexdigest()):
        raise ValueError("isolation scenario fixture differs from pinned schema or checks")
    if shutil.which("openssl") is None:
        raise ValueError("offline TLS check requires OpenSSL")

    for check in CHECKS:
        subprocess.run([sys.executable, "-m", "unittest", "-q", check], cwd=ROOT, check=True)
    subprocess.run(["terraform", f"-chdir={INFRA}", "init", "-backend=false", "-input=false", "-no-color"],
                   cwd=ROOT, check=True)
    subprocess.run(["terraform", f"-chdir={INFRA}", "test", "-no-color"], cwd=ROOT, check=True)
    return {"scenario": name, "result": "offline-contract-checks-passed",
            "sql_denials": "not_run", "storage_denials": "not_run", "libpq_tls_denials": "not_run"}


def main():
    parser = argparse.ArgumentParser(description="Offline scoped SQL/TLS isolation scenario selector")
    parser.add_argument("--scenario", required=True, choices=["test_isolation"])
    arguments = parser.parse_args()
    try:
        print(json.dumps(select(arguments.scenario), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"local isolation scenario failed: {error}\n")


if __name__ == "__main__":
    main()
