"""FO07 offline scenario selectors; deployed readbacks remain separate."""

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
L5_TESTS = ROOT / "modules/L5-services/tests"
GATEWAY_TESTS = ROOT / "modules/gateway/tests"

# The selected release files now include the full shared migration matrix, so a
# complete offline pass runs past five minutes on CI runners. Bound the nested
# run well above an observed pass instead of failing the release check on time.
OFFLINE_TIMEOUT_SECONDS = 1500


def run_offline(selectors):
    environment = {**os.environ, "AWS_CONFIG_FILE": "/dev/null",
                   "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
                   "AWS_EC2_METADATA_DISABLED": "true", "AWS_DEFAULT_REGION": "us-east-1",
                   "PYTHONPATH": os.pathsep.join((str(ROOT / "modules/L5-services/src"),
                                                   str(ROOT / "modules/gateway/src")))}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", "2",
                             "--dist", "loadfile", *map(str, selectors)],
                            cwd=ROOT, env=environment, capture_output=True, text=True,
                            timeout=OFFLINE_TIMEOUT_SECONDS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout
    print(result.stdout.splitlines()[-1])


def test_release():
    selectors = [
        L5_TESTS / "deployment/test_shared_release.py",
        L5_TESTS / "deployment/test_release_wiring.py",
        L5_TESTS / "deployment/test_worker_lifecycle.py",
        L5_TESTS / "deployment/test_repair_schedule.py",
        L5_TESTS / "test_execution_migrations.py",
        L5_TESTS / "finops/test_fo06.py::test_monthly_tick_freezes_closed_interval_and_identity",
        L5_TESTS / "finops/test_fo06.py::test_periodic_snapshot_repair",
        L5_TESTS / "finops/test_fo07_portability.py::test_scoped_route_uses_same_installed_wheel_for_both_projects",
        GATEWAY_TESTS / "migrations/test_156_l5_finops_inquiry.py",
        GATEWAY_TESTS / "migrations/test_161_merge_connector_finops.py",
    ]
    run_offline(selectors)


def test_fault_acceptance():
    selectors = [
        L5_TESTS / "finops/test_fo03.py::test_submit_recovery",
        L5_TESTS / "finops/test_fo03.py::test_session_recovery",
        L5_TESTS / "finops/test_fo03.py::test_terminal_recovery",
        L5_TESTS / "finops/test_fo04.py::test_collection_recovery",
        L5_TESTS / "finops/test_fo04.py::test_truthful_results",
        L5_TESTS / "finops/test_fo06.py::test_publication",
        L5_TESTS / "finops/test_fo07_recovery.py",
        GATEWAY_TESTS / "internal/test_finops_mcp.py::test_client_outcomes",
    ]
    run_offline(selectors)


def test_cleanup_reuse():
    selectors = [
        L5_TESTS / "finops/test_fo06.py::test_publication",
        L5_TESTS / "finops/test_fo06.py::test_cost_source_revocation",
        L5_TESTS / "finops/test_fo06.py::test_cost_citations_preserve_original_evidence",
        L5_TESTS / "finops/test_fo04.py::test_truthful_results",
        L5_TESTS / "finops/test_fo07_retention.py",
        L5_TESTS / "finops/test_retained_scope.py::test_retained_agent_scope_masks_cross_project_and_tenant_collisions",
        L5_TESTS / "integration/test_security_binding.py::test_finops_retirement_keeps_adopted_and_referenced_resources",
        L5_TESTS / "integration/test_security_binding.py::test_cleanup_requires_proof_and_confirmed_absence",
        GATEWAY_TESTS / "l5/test_finops_reuse.py::test_finops_result_source_rights_fail_closed_after_revocation",
    ]
    run_offline(selectors)
