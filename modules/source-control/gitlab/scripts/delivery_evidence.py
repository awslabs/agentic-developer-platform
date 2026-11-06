"""Adapt protected GitLab workflow step outcomes to the generic v2 delivery contract."""

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "gateway"))
from src.orchestration.delivery_contract import DeliveryContract, WorkflowDeliveryEvidence
from registration_delivery import LIVE_CHECKS, private_receipt, verify as verify_registration
from runner_delivery import LIVE_CHECKS as RUNNER_CHECKS, verify as verify_runners
from qualification_delivery import LIVE_CHECKS as QUALIFICATION_CHECKS, verify as verify_qualification

REQUIRED_STEPS = {
    "gitlab-infra-apply.yml": frozenset({"source", "identity", "config", "tools", "plan", "safety", "apply"}),
    "gitlab-runners-apply.yml": frozenset({"source", "inputs", "identity", "config", "apply"}),
    "gitlab-runners-qualify.yml": frozenset({"source", "inputs", "identity", "bundle", "final"}),
}


def verified_cleanup(env, checkout, account):
    path = Path(env["ADP_DELIVERY_CLEANUP_RECEIPT"])
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("private cleanup receipt required")
    receipt = json.loads(path.read_text())
    fixtures = Path(env["ADP_DELIVERY_FIXTURES"])
    info = fixtures.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("private cleanup fixtures required")
    scope = json.loads(fixtures.read_text())
    if (receipt.get("verdict") != "pass" or receipt.get("fresh_independent_api_readbacks") != "passed"
            or scope.get("source_revision") != checkout or scope.get("account_id") != account
            or scope.get("run_id") != env["ADP_QUALIFICATION_RUN_ID"]
            or receipt.get("run_id") != scope["run_id"]
            or receipt.get("workflow_run_id") != int(env["GITHUB_RUN_ID"])
            or receipt.get("bundle_sha256") != env["ADP_QUALIFICATION_BUNDLE_SHA256"]):
        raise ValueError("final live qualification and cleanup not verified")


def verified_identity(env, identity, checkout):
    contract = DeliveryContract.model_validate_json(env["ADP_DELIVERY_CONTRACT_JSON"])
    if contract.digest != env["ADP_CONTRACT_DIGEST"]:
        raise ValueError("operational delivery contract digest mismatch")
    if checkout != env["ADP_SOURCE_REVISION"]:
        raise ValueError("delivery source differs from the checked-out revision")
    if env["GITHUB_WORKFLOW_SHA"] != env["ADP_DEFINITION_REVISION"]:
        raise ValueError("delivery workflow differs from the approved definition")
    account = identity["Account"]
    if account != env["ACCOUNT_ID"] or account != env["ADP_DELIVERY_ACCOUNT_ID"]:
        raise ValueError("delivery account differs from the authenticated deployment")
    if (env["ADP_DELIVERY_RESOURCE_KIND"], env["ADP_DELIVERY_RESOURCE_ID"]) != (
        contract.resource_kind, contract.resource_id
    ):
        raise ValueError("delivery resource differs from the operational release")
    return contract, account


def produce(env, identity, checkout):
    contract, account = verified_identity(env, identity, checkout)
    workflow = env["ADP_DELIVERY_WORKFLOW"]
    steps = REQUIRED_STEPS.get(workflow)
    required = set(contract.required_checks)
    if steps is None or required not in (steps, LIVE_CHECKS, RUNNER_CHECKS, QUALIFICATION_CHECKS):
        raise ValueError("operational contract omits required protected workflow checks")
    if LIVE_CHECKS & required and workflow != "gitlab-infra-apply.yml":
        raise ValueError("registration checks require the reviewed GitLab infrastructure workflow")
    if RUNNER_CHECKS & required and workflow != "gitlab-runners-apply.yml":
        raise ValueError("runner checks require the reviewed runner apply workflow")
    if QUALIFICATION_CHECKS & required and workflow != "gitlab-runners-qualify.yml":
        raise ValueError("qualification checks require the reviewed qualification workflow")
    results = json.loads(env["ADP_DELIVERY_STEP_OUTCOMES"])
    if not isinstance(results, dict) or set(results) != steps:
        raise ValueError("required operational checks do not match workflow steps")
    if any(outcome != "success" for outcome in results.values()):
        raise ValueError("required operational check did not succeed")
    live_refs = {}
    if LIVE_CHECKS & required:
        live_refs = verify_registration(private_receipt(env["ADP_REGISTRATION_RECEIPT"]), env, account, checkout)
    if RUNNER_CHECKS & required:
        live_refs = verify_runners(private_receipt(env["ADP_RUNNER_RECEIPT"]), env, account, checkout)
    if QUALIFICATION_CHECKS & required:
        live_refs = verify_qualification(private_receipt(env["ADP_DELIVERY_CLEANUP_RECEIPT"]), env, account, checkout)
    run_url = f"https://github.com/{env['GITHUB_REPOSITORY']}/actions/runs/{int(env['GITHUB_RUN_ID'])}/attempts/{int(env['GITHUB_RUN_ATTEMPT'])}"
    payload = {
        "schema_version": 2,
        "contract_digest": contract.digest,
        "correlation": env["ADP_CORRELATION"],
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "workflow_path": f".github/workflows/{workflow}",
        "workflow_revision": env["GITHUB_WORKFLOW_SHA"],
        "source_revision": checkout,
        "actual_revision": checkout,
        "account_id": account,
        "region": env["AWS_REGION"],
        "resource_kind": contract.resource_kind,
        "resource_id": contract.resource_id,
        "component": env["ADP_DELIVERY_COMPONENT"],
        "checks": [{"id": check, "outcome": "passed", "evidence_ref": live_refs.get(check, f"{run_url}#{check}")}
                   for check in contract.required_checks],
        "cleanup": "not_required",
        "produced_at": datetime.now(UTC).isoformat(),
    }
    if contract.cleanup_required:
        if workflow != "gitlab-runners-qualify.yml":
            raise ValueError("cleanup-required delivery needs verified cleanup receipts")
        verified_cleanup(env, checkout, account)
        payload["cleanup"] = "passed"
    elif workflow == "gitlab-runners-qualify.yml":
        raise ValueError("qualification contract must require cleanup")
    return WorkflowDeliveryEvidence.model_validate(payload)


def produce_context(env, identity, checkout):
    transport_inputs = {"adp_correlation", "adp_source_revision", "adp_definition_revision"}

    contract, account = verified_identity(env, identity, checkout)
    workflow = env["ADP_DELIVERY_WORKFLOW"]
    if workflow not in REQUIRED_STEPS:
        raise ValueError("unapproved GitLab context workflow")
    inputs = json.loads(env["ADP_DELIVERY_INPUTS_JSON"])
    if (not isinstance(inputs, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in inputs.items())
            or any(inputs.get(k) != v for k, v in contract.workflow_inputs.items())
            or inputs.get("adp_source_revision") != checkout
            or inputs.get("adp_definition_revision") != env["GITHUB_WORKFLOW_SHA"]
            or inputs.get("adp_contract_digest") != contract.digest
            or inputs.get("adp_correlation") != env["ADP_CORRELATION"]):
        raise ValueError("context inputs differ from the accepted dispatch")
    return {
        "schema_version": 1, "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]), "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "workflow_path": f".github/workflows/{workflow}", "workflow_revision": env["GITHUB_WORKFLOW_SHA"],
        "source_revision": checkout, "account_id": account, "region": env["AWS_REGION"],
        "resource_kind": contract.resource_kind, "resource_id": contract.resource_id,
        "inputs": {k: v for k, v in inputs.items() if k not in transport_inputs}, "correlation": env["ADP_CORRELATION"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", action="store_true")
    args = parser.parse_args()
    identity = json.loads(subprocess.check_output(["aws", "sts", "get-caller-identity", "--output", "json"], timeout=30))
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=10).strip()
    evidence = (produce_context if args.context else produce)(os.environ, identity, checkout)
    name = "deployment-context.json" if args.context else "delivery.json"
    target = Path(os.environ["RUNNER_TEMP"]) / "gitlab-delivery" / name
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence) if args.context else evidence.model_dump_json())


if __name__ == "__main__":
    main()
