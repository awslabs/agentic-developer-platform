"""Private tools release journal: attempted work is not runtime evidence."""

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path


STAGES = ("infra", "tool-lambdas", "interceptor", "facade", "targets", "verify")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SOURCE = re.compile(r"[0-9a-f]{40}\Z")



def bindings_sha256(bindings):
    """Bind state identity and private overlays without persisting their values."""
    if not isinstance(bindings, Mapping):
        raise ValueError("Coordinator: supply the approved private tools release bindings")
    try:
        variables = json.loads(bindings.get("ADP_TOOLS_TFVARS_JSON") or "{}")
    except (TypeError, ValueError):
        raise ValueError("Coordinator: repair the private tools Terraform variables") from None
    if not isinstance(variables, dict):
        raise ValueError("Coordinator: repair the private tools Terraform variables")
    target = {
        "state_bucket": bindings.get("ADP_TOOLS_STATE_BUCKET", ""),
        "state_key": f"tools/platform/{bindings.get('ADP_TOOLS_ENVIRONMENT', '')}.tfstate",
        "state_region": bindings.get("ADP_TOOLS_AWS_REGION", ""),
        "private_tfvars": variables,
        "cluster": bindings.get("ADP_TOOLS_EKS_CLUSTER_NAME", ""),
        "deployment_role": bindings.get("ADP_TOOLS_DEPLOY_ROLE_ARN", ""),
    }
    return hashlib.sha256(json.dumps(target, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_bindings(journal, bindings):
    if journal["bindings_sha256"] != bindings_sha256(bindings):
        raise ValueError("Coordinator: private tools state or configuration changed; reconcile before resuming or mutating")


def prerequisites(stages, bindings, *, plan=False):
    """Name the private binding owner before any stage or target read is attempted."""
    if any(stage not in STAGES for stage in stages):
        raise ValueError("Unsupported tools release stage")
    account = bindings.get("ADP_TOOLS_ACCOUNT_ID", "")
    region = bindings.get("ADP_TOOLS_AWS_REGION", "")
    environment = bindings.get("ADP_TOOLS_ENVIRONMENT", "")
    if not (re.fullmatch(r"[0-9]{12}", account)
            and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
            and environment in ("dev", "prod")):
        raise ValueError("Coordinator: select and confirm the tools account, region and environment")
    if not plan and not (SOURCE.fullmatch(bindings.get("ADP_SOURCE_REVISION", ""))
                         and bindings.get("ADP_TOOLS_DEPLOY_ROLE_ARN")):
        raise ValueError("Coordinator: supply a reviewed source and protected tools deployment role")
    if not plan and not all(bindings.get(name, "").isdigit() and int(bindings[name]) > 0
                            for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")):
        raise ValueError("Coordinator: run apply in the protected tools workflow with run and attempt IDs")
    if any(stage in ("infra", "targets") for stage in stages):
        if not bindings.get("ADP_TOOLS_STATE_BUCKET") or not bindings.get("ADP_TOOLS_TFVARS_JSON"):
            raise ValueError("Coordinator: supply the approved tools state bucket and Terraform variables")
        try:
            variables = json.loads(bindings["ADP_TOOLS_TFVARS_JSON"])
        except (ValueError, TypeError):
            raise ValueError("Coordinator: repair the private tools Terraform variables") from None
        if not isinstance(variables, dict) or variables.get("aws_account_id") != account:
            raise ValueError("Coordinator: bind private Terraform variables to the confirmed account")
    if any(stage in ("facade", "verify") for stage in stages) and not bindings.get("ADP_TOOLS_EKS_CLUSTER_NAME"):
        raise ValueError("Coordinator: bind the approved tools EKS cluster for facade or evaluation")
    for stage, variable, action in (
        ("tool-lambdas", "ADP_SHARED_TOOLS_RELEASE_RECEIPT_FILE",
         "run protected shared-agentcore-tools-deploy.yml with mode=apply and supply its runtime receipt"),
        ("verify", "ADP_TOOLS_EVAL_RECEIPT_FILE",
         "run protected SCN-8068-04 evaluation and supply its independent receipt"),
    ):
        if stage in stages and not plan and not Path(bindings.get(variable, "")).is_file():
            raise ValueError(f"Coordinator: {action}")


def save(path, journal):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=".tools-journal-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(journal, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Coordinator: restrict private tools journal to owner-only access")
    journal = json.loads(path.read_text())
    if not isinstance(journal, dict) or journal.get("schema_version") != 2 or not isinstance(journal.get("stages"), dict) or not re.fullmatch(r"[0-9a-f]{64}", journal.get("bindings_sha256", "")):
        raise ValueError("Coordinator: preserve and reconcile the legacy journal; it has no stage attempt evidence")
    return journal


def initialize(path, *, account, region, environment, source, bindings, resume=False):
    path = Path(path)
    if not (SOURCE.fullmatch(source) and re.fullmatch(r"[0-9]{12}", account)
            and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region) and environment in ("dev", "prod")):
        raise ValueError("Coordinator: confirm the tools release target and reviewed source")
    fingerprint = bindings_sha256(bindings)
    if not path.exists():
        if resume:
            raise ValueError("Coordinator: recover the saved private tools journal before resuming")
        journal = {"schema_version": 2, "account": account, "region": region,
                   "environment": environment, "source": source,
                   "bindings_sha256": fingerprint, "stages": {}}
        save(path, journal)
        return journal
    journal = load(path)
    require_bindings(journal, bindings)
    if (journal["account"], journal["region"], journal["environment"]) != (account, region, environment):
        raise ValueError("Tools deploy journal belongs to a different target")
    if journal["source"] != source:
        raise ValueError("Coordinator: preserve the prior journal and review newer main; never resume or overwrite another source")
    if not resume and journal["stages"]:
        raise ValueError("Coordinator: reconcile the saved release and use --resume; never implicitly repeat stages")
    return journal


def validate_receipt(receipt, journal, stage, complete_expected):
    attempt = journal["stages"][stage]
    expected = receipt.get("expected_runtime")
    observed = receipt.get("observed_runtime")
    return (receipt.get("stage") == stage
            and receipt.get("source_revision") == journal["source"]
            and receipt.get("account") == journal["account"]
            and receipt.get("region") == journal["region"]
            and receipt.get("environment") == journal["environment"]
            and receipt.get("bindings_sha256") == journal["bindings_sha256"]
            and type(receipt.get("run_id")) is int and receipt["run_id"] == attempt["run_id"]
            and type(receipt.get("run_attempt")) is int and receipt["run_attempt"] == attempt["run_attempt"]
            and receipt.get("mode") == "apply"
            and receipt.get("evidence_type") == "independent-runtime"
            and isinstance(expected, dict) and expected == complete_expected
            and observed == expected
            and all(isinstance(component, str) and isinstance(digest, str) and DIGEST.fullmatch(digest)
                    for component, digest in expected.items()))


def decision(path, stage, *, bindings, resume=False, receipts_dir=None):
    journal = load(path)
    require_bindings(journal, bindings)
    if stage not in STAGES:
        raise ValueError("Unsupported tools release stage")
    if stage not in journal["stages"]:
        return "run"
    if not resume:
        raise ValueError("Coordinator: reconcile the saved release and use --resume before any repeated stage")
    if not receipts_dir:
        raise ValueError(f"Coordinator: independently verify {stage} and supply its private stage receipt before resume")
    manifest_path = Path(receipts_dir) / "manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_mode & 0o077:
        raise ValueError("Coordinator: supply the private approved complete stage manifest for resume")
    manifest = json.loads(manifest_path.read_text())
    if not (isinstance(manifest, dict)
            and manifest.get("source_revision") == journal["source"]
            and all(manifest.get(field) == journal[field] for field in
                    ("account", "region", "environment"))
            and manifest.get("bindings_sha256") == journal["bindings_sha256"]
            and isinstance(manifest.get("stages"), dict)
            and isinstance(manifest["stages"].get(stage), dict) and manifest["stages"][stage]):
        raise ValueError("Coordinator: approved stage manifest does not match saved source or target")
    receipt_path = Path(receipts_dir) / f"{stage}.json"
    if not receipt_path.is_file():
        raise ValueError(f"Coordinator: independently verify {stage} and supply its private stage receipt before resume")
    if receipt_path.stat().st_mode & 0o077:
        raise ValueError(f"Coordinator: restrict the private {stage} receipt to owner-only access")
    receipt = json.loads(receipt_path.read_text())
    if not isinstance(receipt, dict) or not validate_receipt(receipt, journal, stage, manifest["stages"][stage]):
        raise ValueError(f"Coordinator: {stage} receipt is partial or does not match the saved attempt; verify runtime before resume")
    receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    previous_sha256 = journal["stages"][stage].get("receipt_sha256")
    previous_manifest = journal["stages"][stage].get("manifest_sha256")
    if (previous_sha256 and previous_sha256 != receipt_sha256) or (
            previous_manifest and previous_manifest != manifest_sha256):
        raise ValueError(f"Coordinator: {stage} receipt or manifest changed after verification; reconcile before resume")
    if journal["stages"][stage].get("status") != "receipt-verified":
        journal["stages"][stage].update(status="receipt-verified", receipt_sha256=receipt_sha256,
                                        manifest_sha256=manifest_sha256)
        save(path, journal)
    return "skip"


def start(path, stage, *, run_id, run_attempt, bindings):
    journal = load(path)
    require_bindings(journal, bindings)
    if stage not in STAGES or stage in journal["stages"]:
        raise ValueError("Coordinator: existing stage attempt needs independent reconciliation")
    if not (type(run_id) is int and run_id > 0 and type(run_attempt) is int and run_attempt > 0):
        raise ValueError("Coordinator: run stages only in a protected workflow with run and attempt IDs")
    journal["stages"][stage] = {"status": "started", "run_id": run_id, "run_attempt": run_attempt}
    save(path, journal)


def finish(path, stage, *, bindings):
    journal = load(path)
    require_bindings(journal, bindings)
    if journal["stages"].get(stage, {}).get("status") != "started":
        raise ValueError("Coordinator: stage has no saved started attempt")
    journal["stages"][stage]["status"] = "command-succeeded"
    save(path, journal)


def main():
    parser = argparse.ArgumentParser(description="Keep tools stage attempts separate from runtime evidence")
    parser.add_argument("action", choices=("prerequisites", "init", "decision", "start", "finish"))
    parser.add_argument("arguments", nargs="*")
    options = parser.parse_args()
    arguments = options.arguments
    if options.action == "prerequisites":
        prerequisites(arguments[1:], os.environ, plan=arguments[0] == "true")
    elif options.action == "init":
        initialize(arguments[0], account=arguments[1], region=arguments[2],
                   environment=arguments[3], source=arguments[4], bindings=os.environ, resume=arguments[5] == "true")
    elif options.action == "decision":
        print(decision(arguments[0], arguments[1], bindings=os.environ,
                       resume=arguments[2] == "true", receipts_dir=arguments[3] or None))
    elif options.action == "start":
        start(arguments[0], arguments[1], run_id=int(arguments[2]), run_attempt=int(arguments[3]), bindings=os.environ)
    else:
        finish(arguments[0], arguments[1], bindings=os.environ)


if __name__ == "__main__":
    try:
        main()
    except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
