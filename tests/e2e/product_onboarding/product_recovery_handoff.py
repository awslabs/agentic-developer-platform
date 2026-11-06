"""Qualify the original held Product and installed releases before a protected switch."""

from __future__ import annotations

import argparse
import os
import re

from tests.e2e.cli_uplift import ports, statestore

from . import live, product_execution, product_inventory, product_native
from .product_automatic_identity import require_qualified
from .product_qualification_state import (
    RetainedQualification,
    held_evaluation,
    held_release,
)
from .product_rollback_artifacts import qualify_baseline


def prepare(
    cfg,
    run_id,
    aws,
    store,
    *,
    inventory_reader=product_inventory.observe,
    native_reader=product_native.observe,
    qualify=qualify_baseline,
    accepted_reader=require_qualified,
):
    account = str(cfg["platform_account"])
    identity = aws.call("sts", "get_caller_identity")
    if identity.get("Account") != account or not re.fullmatch(
        rf"arn:aws:sts::{re.escape(account)}:assumed-role/adp-cli-uplift-eval-orchestrator/[A-Za-z0-9+=,.@_/-]+",
        str(identity.get("Arn", "")),
    ):
        raise ports.PortError(
            "Product recovery handoff requires the protected evaluator"
        )
    held = held_evaluation(store, run_id)
    if not isinstance(held, dict) or held != product_execution.recovery_binding(
        cfg, run_id, held.get("evaluation_id")
    ):
        raise ports.PortError(
            "Product recovery handoff differs from the original evaluation"
        )
    proof = held_release(store, run_id)
    inventory = inventory_reader(aws, cfg, cfg["expected_revision"])
    current = native_reader(cfg, inventory, aws)
    if (
        not isinstance(proof, dict)
        or not isinstance(current, dict)
        or current.get("release_ref") != proof.get("release_ref")
        or current.get("release_artifact") != proof.get("release_artifact")
        or current.get("target") != proof.get("target")
        or not isinstance(current.get("runtime_receipt"), dict)
        or not isinstance(proof.get("runtime_receipt"), dict)
        or any(
            not proof["runtime_receipt"].get(field)
            or current["runtime_receipt"].get(field) != proof["runtime_receipt"][field]
            for field in (
                "actual_revision",
                "image_digest",
                "migration_head",
                "cli_manifest_sha256",
                "pod_uids",
            )
        )
        or any(
            current["runtime_receipt"].get(field)
            is not proof["runtime_receipt"].get(field)
            for field in ("product_profile_enabled", "product_onboarding_enabled")
        )
    ):
        raise ports.PortError(
            "Product recovery handoff differs from the serving signed release"
        )
    accepted_reader(store, run_id, cfg["expected_revision"], current)
    candidate, baseline, baseline_ref = qualify(cfg, current, aws)
    state = RetainedQualification(store, candidate, baseline)
    report = state.load()
    if (
        not isinstance(report, dict)
        or report.get("candidate_ref") != proof["release_ref"]
        or report.get("baseline_ref") != baseline_ref
        or report.get("candidate", {}).get("source_revision")
        != cfg["expected_revision"]
        or report.get("status") != "incomplete"
    ):
        raise ports.PortError(
            "Product recovery handoff differs from the retained attempt"
        )
    state.begin_hold()
    return {
        "run_id": run_id,
        "source_revision": candidate["source_revision"],
        "workflow_revision": candidate["workflow_revision"],
        "candidate_image_digest": candidate["image_digest"],
        "baseline_source_revision": baseline["source_revision"],
        "baseline_image_digest": baseline["image_digest"],
        "resource_id": candidate["resource_id"],
        "account_id": candidate["account_id"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args(argv)
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        raise ports.PortError("Product handoff requires protected job outputs")
    cfg = {
        **live.resolved_config(args.expected_revision),
        "product_cases": "api_product_lifecycle",
        "hold_for_rollback": True,
    }
    aws = ports.default_ports(cfg)["aws"]
    store = statestore.default_store(cfg, aws)
    if store is None:
        raise ports.PortError("Product handoff has no private recovery state")
    result = prepare(cfg, live.workflow_run_id(), aws, store)
    with open(output, "a", encoding="utf-8") as handle:
        for key, value in result.items():
            handle.write(f"{key}={value}\n")
    return result


if __name__ == "__main__":
    main()
