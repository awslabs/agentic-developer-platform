"""Publish only a bounded summary after independent private proof readback."""

import json
import os
import re
from pathlib import Path

from tests.e2e.tools.atlassian_phase import readback
from tests.e2e.tools.evidence_reader import require

SHA = re.compile(r"[0-9a-f]{40}\Z")
COUNT = re.compile(r"[1-9][0-9]*\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def stage_summary(
    *, repository, account, source, definition, run_id, attempt, uri, version,
    verify=readback,
):
    require(
        isinstance(repository, str) and REPOSITORY.fullmatch(repository) is not None
        and isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account) is not None
        and isinstance(source, str) and SHA.fullmatch(source) is not None
        and isinstance(definition, str) and SHA.fullmatch(definition) is not None
        and isinstance(run_id, str) and COUNT.fullmatch(run_id) is not None
        and isinstance(attempt, str) and COUNT.fullmatch(attempt) is not None
    )
    report = verify(
        uri, version, repository=repository, account=account, run_id=run_id,
        attempt=attempt, source=source, definition=definition, gateway=source,
    )
    require(
        isinstance(report, dict)
        and report.get("schema_version") == 1
        and report.get("issue_number") == 8446
        and report.get("generated_cleanup") == "verified"
        and report.get("cleanup_observation") == {
            "phase": "verified", "uncertain": False, "remaining_resources": []
        }
        and report.get("unresolved_operations") == []
        and report.get("phase") == "stage.identity-proof"
        and report.get("evidence_kind") == "generated-fixture"
        and report.get("controlled_status") == "candidate"
        and report.get("live_status") == "unproven"
        and report.get("source_revision") == source
        and report.get("definition_revision") == definition
        and report.get("gateway_revision") == source
        and report.get("run_id") == run_id
        and report.get("run_attempt") == attempt
        and isinstance(report.get("scenarios"), dict)
        and report["scenarios"] == {case: "pass" for case in ("SCN-01", "SCN-02", "SCN-03")}
        and all(isinstance(report.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", report[key])
                for key in ("contract_sha256", "readiness_sha256"))
    )
    return {
        "schema_version": 1,
        "phase": "stage.identity-proof",
        "source_revision": source,
        "gateway_revision": source,
        "definition_revision": definition,
        "run_id": run_id,
        "run_attempt": attempt,
        "criterion_rows": [
            {"issue_number": 8446, "criterion_id": criterion, "scenario_id": scenario,
             "controlled": "candidate", "live": "unproven"}
            for criterion, scenario in (
                ("AC-01", "SCN-01"), ("AC-02", "SCN-02"), ("AC-03", "SCN-03")
            )
        ],
        "runtime": {"verified": False, "release_receipt": "required-from-accepted-predecessor"},
        "cleanup": {"generated_verified": True, "remote_verified": False, "status": "generated_only"},
        "unresolved_operations": report["unresolved_operations"],
        "verification_level": "controlled_integration",
        "fixture_source": "generated",
        "test_source_matches_release": source == report["gateway_revision"],
        "independent_verification": {"private_phase_readback": True, "live_provider": False},
        "live_status": "unproven",
        "contract_sha256": report["contract_sha256"],
        "readiness_sha256": report["readiness_sha256"],
    }


def main():
    env = os.environ
    require(
        env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("PROOF_PHASE") == "stage.identity-proof"
        and env.get("SOURCE_REVISION") == env.get("GATEWAY_REVISION")
        and env.get("GITHUB_SHA") == env.get("DEFINITION_REVISION")
    )
    summary = stage_summary(
        repository=env["GITHUB_REPOSITORY"], account=env["EXPECTED_ACCOUNT"],
        source=env["SOURCE_REVISION"], definition=env["DEFINITION_REVISION"],
        run_id=env["GITHUB_RUN_ID"], attempt=env["GITHUB_RUN_ATTEMPT"],
        uri=env["CI_EVIDENCE_MANIFEST_URI"], version=env["CI_EVIDENCE_MANIFEST_VERSION"],
    )
    output = Path(env["RUNNER_TEMP"]) / "atlassian-stage-summary.json"
    require(not output.exists() and not output.is_symlink())
    output.write_text(json.dumps(summary, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
