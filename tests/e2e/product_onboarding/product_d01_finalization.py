"""Finalize D01 only from the same attempt's retained recovery and cleanup."""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path

from tests.e2e.cli_uplift import ports, statestore

from . import live, product_automatic_identity
from .product_qualification_state import (
    RetainedQualification,
    held_evaluation,
    held_release,
    recovery_key,
)
from .product_rollback_artifacts import qualification

RECOVERY_STAGES = (
    "created",
    "quiesce_started",
    "quiesced",
    "baseline_restore_started",
    "baseline_verified",
    "candidate_restore_started",
    "candidate_verified",
)


def final_key(run_id):
    statestore.product_run_key(run_id)
    return f"{statestore.PREFIX}/product-runs/{run_id}/final-grades.json"


def finalize(store, run_id, source_revision):
    identity = product_automatic_identity.require_qualified(store, run_id, source_revision)
    preliminary = store._get(product_automatic_identity.preliminary_key(run_id))
    require = qualification.require
    require(isinstance(preliminary, dict) and preliminary.get("identity") == identity, "Final grading has no matching preliminary release")
    grades = deepcopy(preliminary.get("report"))
    require(
        isinstance(grades, dict)
        and grades.get("run_id") == run_id
        and grades.get("source_revision") == source_revision
        and grades.get("workflow_revision") == identity["workflow_revision"]
        and grades.get("input_issues") == []
        and bool(grades.get("checks"))
        and all(value is True for value in grades["checks"].values())
        and all(grades.get("cases", {}).get(case, {}).get("status") == "PASSED" for case in ("PR01-C01", "PR01-C02")),
        "Final grading requires passing preliminary API and CLI checks",
    )
    binding = held_evaluation(store, run_id)
    proof = held_release(store, run_id)
    require(
        isinstance(binding, dict)
        and binding.get("workflow_run_id") == run_id
        and binding.get("expected_revision") == source_revision
        and binding.get("product_cases") == "api_product_lifecycle"
        and isinstance(proof, dict)
        and proof.get("release_ref") == identity["release_ref"],
        "Recovery belongs to a different held attempt",
    )
    candidate = proof["release_artifact"]
    require(
        all(candidate.get(field) == value for field, value in identity.items() if field not in ("release_ref", "release_hash")),
        "Recovery differs from the signed automatic release",
    )
    stored = store._get(recovery_key(run_id))
    require(isinstance(stored, dict) and isinstance(stored.get("report"), dict), "Recovery has no retained qualification")
    baseline = stored["report"].get("baseline")
    state = RetainedQualification(store, candidate, baseline)
    report = state.load()
    require(
        report.get("candidate") == qualification.release_identity(candidate)
        and report.get("candidate_ref") == identity["release_ref"]
        and isinstance(report.get("baseline_ref"), str)
        and report["baseline_ref"].startswith("s3://")
        and "versionId=" in report["baseline_ref"]
        and all(baseline.get(field) == candidate.get(field) for field in ("account_id", "region", "resource_id", "migration_head")),
        "Recovery changed its signed artifacts, schema or target",
    )
    attempts = report.get("attempts", [])
    stages = [attempt.get("stage") for attempt in attempts]
    require(stages in (list(RECOVERY_STAGES), [*RECOVERY_STAGES, "cleaned"]), "Interrupted recovery cannot pass as completed rollback")
    owned = report.get("owned")
    document, manifest = store.load(binding["evaluation_id"])
    require(
        isinstance(owned, dict)
        and owned.get("run_id") == run_id
        and owned.get("source_revision") == source_revision
        and isinstance(document, dict)
        and document.get("product_run_id") == run_id
        and document.get("product_evidence", {}).get("held") == owned
        and attempts[0].get("receipt") == owned
        and attempts[1].get("receipt") == report["candidate"]
        and attempts[3].get("receipt") == baseline
        and attempts[5].get("receipt") == report["candidate"],
        "Recovery lost the original Product ownership or switch intent",
    )
    for index, artifact, admissions in ((2, report["candidate"], False), (4, baseline, False), (6, report["candidate"], True)):
        receipt = attempts[index].get("receipt")
        qualification.check_readback(owned, receipt, artifact, admissions=admissions)
        require(bool(receipt.get("pod_uids")), "Recovery lacks independently observed serving pods")
    require(attempts[6]["receipt"].get("replay_verified") is True, "Restored candidate did not replay the original idempotency key")
    receipt = document.get("product_cleanup_receipt")
    resources = manifest.get("resources") if isinstance(manifest, dict) else None
    fixtures = [entry for entry in resources if entry.get("kind") == "product_fixture"] if isinstance(resources, list) else []
    require(
        document.get("cleanup_ok") is True
        and isinstance(receipt, dict)
        and receipt.get("fixture_removed") is True
        and all(receipt.get(field) == owned.get(field) and owned.get(field) for field in ("run_id", "product_id", "project_id"))
        and isinstance(manifest, dict)
        and manifest.get("prefix") == binding["evaluation_id"]
        and len(fixtures) == 1
        and fixtures[0].get("id") == run_id
        and fixtures[0].get("prefix") == binding["evaluation_id"]
        and fixtures[0].get("status") == "deleted"
        and fixtures[0].get("account") == identity["account_id"]
        and fixtures[0].get("region") == identity["region"]
        and all(
            fixtures[0].get("detail", {}).get(field) == owned.get(field) for field in ("tenant_id", "actor_id", "source_revision", "idempotency_key")
        ),
        "Original owned Product cleanup is not durably verified",
    )
    cleaned = {"stage": "cleaned", "receipt": receipt}
    if stages == list(RECOVERY_STAGES):
        report["attempts"].append(cleaned)
    else:
        require(attempts[-1] == cleaned, "Retained cleanup changed its receipt")
    report["status"] = "passed"
    state.retain(report)
    grades["cases"]["PR01-C03"] = {"status": "PASSED", "evidence_ref": recovery_key(run_id)}
    grades["cases"]["PR01-C04"] = {"status": "PASSED", "evidence_ref": product_automatic_identity.identity_key(run_id)}
    grades["status"] = "pass"
    result = {"identity": identity, "report": grades}
    store._put(final_key(run_id), result)
    require(store._get(final_key(run_id)) == result, "Final release grades were not retained")
    return grades


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    cfg = live.resolved_config(args.expected_revision)
    aws = ports.default_ports(cfg)["aws"]
    store = statestore.default_store(cfg, aws)
    run_id = live.workflow_run_id()
    try:
        if store is None:
            raise ports.PortError("Final grading requires private durable state")
        report = finalize(store, run_id, args.expected_revision)
    except (ValueError, KeyError, TypeError, ports.PortError, statestore.StateStoreError) as error:
        report = {"status": "incomplete", "run_id": run_id, "failure": type(error).__name__}
        if store is not None:
            store._put(final_key(run_id), {"report": report})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
