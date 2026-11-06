"""Observe the held Product and the signed Gateway image after a protected switch."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from src.orchestration.deployment_runtime_contract import ReleaseArtifact
from src.orchestration.deployment_runtime_reader import KubernetesRuntime
from tests.e2e.cli_uplift import cleanup, deployment_provenance, ports, statestore

from . import live, product_execution, product_recovery_readback, product_sessions
from .product_qualification_state import RetainedQualification, held_evaluation, held_release
from .product_rollback_artifacts import qualification, read_baseline


def record_stage(state, expected, stage, receipt):
    report = state.load()
    if not isinstance(report, dict) or not isinstance(report.get("owned"), dict):
        raise ports.PortError("Recovery stage has no original held Product")
    attempts = report.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ports.PortError("Recovery stage has no prior protected intent")
    previous = attempts[-1]
    if previous.get("stage") == stage and previous.get("receipt") == receipt:
        return report
    if previous.get("stage") != expected:
        raise ports.PortError("Recovery stage is out of order or belongs to another attempt")
    report["attempts"].append({"stage": stage, "receipt": receipt})
    state.retain(report)
    return state.load()


def prepare_restore(cfg, run_id, aws, store, *, baseline_reader=read_baseline):
    account = str(cfg["platform_account"])
    caller = aws.call("sts", "get_caller_identity")
    if caller.get("Account") != account or not re.fullmatch(
        rf"arn:aws:sts::{re.escape(account)}:assumed-role/adp-cli-uplift-eval-orchestrator/[A-Za-z0-9+=,.@_/-]+",
        str(caller.get("Arn", "")),
    ):
        raise ports.PortError("Candidate restoration requires the protected evaluator")
    binding = held_evaluation(store, run_id)
    if not isinstance(binding, dict) or binding != product_execution.recovery_binding(cfg, run_id, binding.get("evaluation_id")):
        raise ports.PortError("Candidate restoration has no original held run")
    proof = held_release(store, run_id)
    if not isinstance(proof, dict) or not isinstance(proof.get("target"), dict) or not isinstance(proof.get("release_artifact"), dict):
        raise ports.PortError("Candidate restoration has no signed release")
    candidate = proof["release_artifact"]
    target = proof["target"]
    baseline, baseline_ref = baseline_reader(
        aws, account=account, region=cfg["region"],
        repository=os.environ["GITHUB_REPOSITORY"], repository_id=target["repository_id"],
    )
    state = RetainedQualification(store, candidate, baseline)
    report = state.load()
    original, manifest = store.load(binding["evaluation_id"])
    owned = report.get("owned") if isinstance(report, dict) else None
    resources = manifest.get("resources") if isinstance(manifest, dict) else None
    fixtures = [entry for entry in resources if isinstance(entry, dict) and entry.get("kind") == "product_fixture"] if isinstance(resources, list) else []
    if (
        not isinstance(report, dict)
        or report.get("status") != "incomplete"
        or report.get("candidate") != qualification.release_identity(candidate)
        or report.get("baseline") != qualification.release_identity(baseline)
        or report.get("candidate_ref") != proof["release_ref"]
        or report.get("baseline_ref") != baseline_ref
        or candidate.get("workflow_revision") != os.environ.get("GITHUB_WORKFLOW_SHA")
        or target.get("account_id") != account
        or target.get("region") != cfg["region"]
        or target.get("resource_id") != candidate.get("resource_id")
        or target.get("resource_id") != baseline.get("resource_id")
        or not isinstance(owned, dict)
        or not isinstance(original, dict)
        or original.get("cleanup_ok") is not False
        or original.get("product_run_id") != run_id
        or not isinstance(original.get("product_evidence"), dict)
        or original["product_evidence"].get("held") != owned
        or not isinstance(manifest, dict)
        or manifest.get("prefix") != binding["evaluation_id"]
        or len(fixtures) != 1
        or fixtures[0].get("prefix") != binding["evaluation_id"]
        or fixtures[0].get("id") != run_id
        or fixtures[0].get("status") != "pending"
        or fixtures[0].get("account") != account
        or fixtures[0].get("region") != cfg["region"]
        or not isinstance(fixtures[0].get("detail"), dict)
        or any(fixtures[0]["detail"].get(key) != owned.get(key) for key in ("tenant_id", "actor_id", "source_revision", "idempotency_key"))
    ):
        raise ports.PortError("Candidate restoration differs from the held attempt")
    attempts = report.get("attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        raise ports.PortError("Candidate restoration has no quiescence intent")
    stage = attempts[-1].get("stage")
    if stage == "candidate_restore_started":
        if attempts[-1].get("receipt") != report["candidate"]:
            raise ports.PortError("Candidate restoration changed its intent")
        return report["candidate"]
    if stage not in ("quiesce_started", "quiesced", "baseline_restore_started", "baseline_verified"):
        raise ports.PortError("Candidate restoration is not at a compensable stage")
    record_stage(state, stage, "candidate_restore_started", report["candidate"])
    return report["candidate"]


def serving(cfg, aws, artifact, admissions):
    selected = deployment_provenance.binding(cfg)
    account = str(cfg["platform_account"])
    if (
        artifact["account_id"] != account
        or artifact["region"] != cfg["region"]
        or artifact["resource_id"] != selected["cluster"] + "/" + selected["namespace"]
        or type(admissions) is not bool
    ):
        raise ports.PortError("Product rollback observation has a different target")
    cluster = aws.call("eks", "describe_cluster", name=selected["cluster"])["cluster"]
    if cluster.get("name") != selected["cluster"] or cluster.get("status") != "ACTIVE" or cluster.get("arn") != selected["cluster_arn"]:
        raise ports.PortError("Product rollback cluster identity differs")
    signed = ReleaseArtifact.model_validate(artifact)
    expected = signed.model_copy(update={"product_profile_enabled": admissions})
    kubernetes = KubernetesRuntime(
        aws.session(),
        cluster,
        selected["namespace"],
        proof_invoke_url=cfg["internal_invoke_url"],
    )
    try:
        pod_uids, head = kubernetes.gateway(
            digest=signed.image_digest,
            source_revision=signed.source_revision,
            account=account,
            region=cfg["region"],
            product=expected,
        )
    finally:
        kubernetes.close()
    if head != signed.migration_head or not pod_uids:
        raise ports.PortError("Product rollback pods differ from the signed schema")
    return {"pod_uids": pod_uids, "migration_head": head}


def observe(
    cfg,
    run_id,
    aws,
    store,
    *,
    choice,
    admissions,
    baseline_reader=read_baseline,
    runtime_reader=serving,
    session_reader=product_sessions.restore,
    product_reader=product_recovery_readback.read_owned,
):
    account = str(cfg["platform_account"])
    caller = aws.call("sts", "get_caller_identity")
    if (
        caller.get("Account") != account
        or not re.fullmatch(
            rf"arn:aws:sts::{re.escape(account)}:assumed-role/adp-cli-uplift-eval-orchestrator/[A-Za-z0-9+=,.@_/-]+",
            str(caller.get("Arn", "")),
        )
        or choice not in ("candidate", "quiesced", "baseline")
        or admissions is not (choice == "candidate")
    ):
        raise ports.PortError("Product rollback observation has no protected evaluator")
    binding = held_evaluation(store, run_id)
    if not isinstance(binding, dict) or binding != product_execution.recovery_binding(cfg, run_id, binding.get("evaluation_id")):
        raise ports.PortError("Product rollback observation has no original held run")
    proof = held_release(store, run_id)
    if not isinstance(proof, dict):
        raise ports.PortError("Product rollback observation has no signed candidate")
    candidate = proof["release_artifact"]
    target = proof["target"]
    baseline, baseline_ref = baseline_reader(
        aws,
        account=account,
        region=cfg["region"],
        repository=os.environ["GITHUB_REPOSITORY"],
        repository_id=target["repository_id"],
    )
    state = RetainedQualification(store, candidate, baseline)
    report = state.load()
    if (
        not isinstance(report, dict)
        or report.get("status") != "incomplete"
        or candidate.get("product_profile_enabled") is not True
        or baseline.get("product_profile_enabled") is not True
        or candidate.get("product_onboarding_enabled") is not False
        or baseline.get("product_onboarding_enabled") is not False
        or report.get("candidate") != qualification.release_identity(candidate)
        or report.get("baseline") != qualification.release_identity(baseline)
        or target.get("account_id") != account
        or target.get("region") != cfg["region"]
        or target.get("resource_id") != candidate["resource_id"]
        or target.get("repository_id") != candidate.get("repository_id")
        or target.get("resource_id") != baseline["resource_id"]
        or candidate.get("migration_head") != baseline.get("migration_head")
        or candidate.get("workflow_revision") != os.environ.get("GITHUB_WORKFLOW_SHA")
        or report.get("candidate_ref") != proof["release_ref"]
        or report.get("baseline_ref") != baseline_ref
        or report.get("candidate") is None
        or report.get("baseline") is None
    ):
        raise ports.PortError("Product rollback observation differs from retained releases")
    expected_stage = {"quiesced": "quiesce_started", "baseline": "baseline_restore_started", "candidate": "candidate_restore_started"}[choice]
    if not report.get("attempts") or report["attempts"][-1].get("stage") != expected_stage:
        raise ports.PortError("Product rollback observation is not at the protected stage")
    original, manifest = store.load(binding["evaluation_id"])
    held = (original.get("product_evidence") or {}).get("held") if isinstance(original, dict) else None
    owned = report.get("owned")
    fixtures = [entry for entry in manifest.get("resources", []) if entry.get("kind") == "product_fixture"] if isinstance(manifest, dict) else []
    if (
        not isinstance(owned, dict)
        or owned != held
        or not isinstance(original, dict)
        or original.get("cleanup_ok") is not False
        or original.get("product_run_id") != run_id
        or not isinstance(manifest, dict)
        or manifest.get("prefix") != binding["evaluation_id"]
        or len(fixtures) != 1
        or fixtures[0].get("id") != run_id
        or fixtures[0].get("status") != "pending"
        or fixtures[0].get("prefix") != binding["evaluation_id"]
        or fixtures[0].get("account") != account
        or fixtures[0].get("region") != cfg["region"]
        or any(fixtures[0].get("detail", {}).get(key) != owned.get(key) for key in ("tenant_id", "actor_id", "source_revision", "idempotency_key"))
    ):
        raise ports.PortError("Product rollback observation has no durable original fixture")
    with tempfile.TemporaryDirectory(prefix="product-rollback-owner-") as directory:
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)
        owner = session_reader(
            {**cfg, "product_run_id": run_id},
            {"evaluation_id": binding["evaluation_id"], "document": original, "manifest": cleanup.Manifest(path, binding["evaluation_id"])},
            store,
            owner=True,
        )
    artifact = baseline if choice == "baseline" else candidate
    runtime = runtime_reader(cfg, aws, artifact, admissions)
    readback = product_reader(cfg, owned, owner["access_token"], admissions=admissions)
    receipt = {
        "source_revision": artifact["source_revision"],
        "workflow_revision": artifact["workflow_revision"],
        "image_digest": artifact["image_digest"],
        "migration_head": runtime["migration_head"],
        "pod_uids": runtime["pod_uids"],
        "product_profile_enabled": admissions,
        "product_onboarding_enabled": False,
        "account_id": artifact["account_id"],
        "region": artifact["region"],
        "resource_id": artifact["resource_id"],
        **readback,
    }
    qualification.check_readback(owned, receipt, qualification.release_identity(artifact), admissions=admissions)
    stage = {
        "quiesced": ("quiesce_started", "quiesced"),
        "baseline": ("baseline_restore_started", "baseline_verified"),
        "candidate": ("candidate_restore_started", "candidate_verified"),
    }[choice]
    record_stage(state, *stage, receipt)
    if choice == "quiesced":
        record_stage(state, "quiesced", "baseline_restore_started", report["baseline"])
    if choice == "baseline":
        record_stage(state, "baseline_verified", "candidate_restore_started", report["candidate"])
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--choice", required=True, choices=("quiesced", "baseline", "candidate", "restore-intent"))
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ports.PortError("Product recovery readback requires reviewed main")
    cfg = {
        **live.resolved_config(args.expected_revision),
        "product_cases": "api_product_lifecycle",
        "hold_for_rollback": True,
    }
    aws = ports.default_ports(cfg)["aws"]
    store = statestore.default_store(cfg, aws)
    if store is None:
        raise ports.PortError("Product recovery readback has no private state")
    if args.choice == "restore-intent":
        result = prepare_restore(cfg, live.workflow_run_id(), aws, store)
    else:
        result = observe(cfg, live.workflow_run_id(), aws, store, choice=args.choice, admissions=args.choice == "candidate")
    with os.fdopen(os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True)
    return result


if __name__ == "__main__":
    main()
