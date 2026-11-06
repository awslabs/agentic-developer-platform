"""Emit generic delivery v2 only after pilot and independently sourced cleanup pass."""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from source_reader import approved_reader
from verify_campaign import _load_document, _pilot_pins, native_pilot_artifact


CHECKS = ("preflight", "all-required-criteria", "independent-readback",
          "unattended-resume", "scope-and-identity")


def receipt(summary, manifest, plan, cleanup_raw, target, environment):
    """Bind the generic verifier to a qualified pilot and a different finalizer job."""
    pins = _pilot_pins(manifest)
    selected = {field: pins[field] for field in ("source_revision", "definition_revision",
                                                  "run_id", "run_attempt")}
    selected["provider_selection"] = json.loads(environment.get("ADP_DEVLAKE_PROVIDER_SELECTION", ""))
    native_pilot_artifact(summary, selected,
                          f"devlake-pilot-{pins['run_id']}-{pins['run_attempt']}", plan)
    if any(summary.get(field) != value for field, value in pins.items()):
        raise ValueError("pilot and launch manifest pins differ")
    expected = {"adp_source_revision": pins["source_revision"],
                "adp_definition_revision": pins["definition_revision"],
                "GITHUB_SHA": pins["source_revision"],
                "ADP_DEVLAKE_WORKFLOW_SHA": pins["definition_revision"],
                "GITHUB_RUN_ID": pins["run_id"], "GITHUB_RUN_ATTEMPT": pins["run_attempt"],
                "GITHUB_REPOSITORY": "aws-e/adp"}
    if any(environment.get(field) != value for field, value in expected.items()):
        raise ValueError("delivery workflow transport and pilot pins differ")
    digest = environment.get("adp_contract_digest", "")
    correlation = environment.get("adp_correlation", "")
    repository_id = environment.get("GITHUB_REPOSITORY_ID", "")
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
            not isinstance(correlation, str) or
            not 0 < len(correlation) <= 512 or not isinstance(repository_id, str) or
            not re.fullmatch(r"[1-9][0-9]*", repository_id)):
        raise ValueError("generic delivery transport is missing or invalid")
    if (not isinstance(target, dict) or set(target) != {
            "account_id", "region", "resource_kind", "resource_id", "component"} or
            target.get("resource_kind") != "engineering-poc" or
            target.get("component") != "devlake-poc" or
            not isinstance(target.get("account_id"), str)):
        raise ValueError("approved delivery target is missing or invalid")
    reference = manifest.get("delivery_cleanup_receipt")
    if (not isinstance(reference, dict) or not isinstance(reference.get("ref"), str) or
            not re.fullmatch(r"[1-9][0-9]*", reference["ref"]) or
            not isinstance(reference.get("sha256"), str) or not isinstance(cleanup_raw, bytes) or
            hashlib.sha256(cleanup_raw).hexdigest() != reference["sha256"]):
        raise ValueError("independent finalized cleanup receipt is missing or differs")
    try:
        cleanup = json.loads(cleanup_raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("independent finalized cleanup receipt is invalid") from error
    if not isinstance(cleanup, dict):
        raise ValueError("independent finalized cleanup receipt is invalid")
    observations = cleanup.get("observations", {})
    finalization = observations.get("cleanup", {}) if isinstance(observations, dict) else {}
    if (cleanup.get("scope") != "live-source-measurement" or
            cleanup.get("source_revision") != pins["source_revision"] or
            cleanup.get("phase") != "finalized" or
            not isinstance(cleanup.get("observed_at"), str) or
            not isinstance(finalization, dict) or
            finalization.get("scope") != "registered-artifacts-only" or
            not isinstance(finalization.get("states"), dict) or not finalization["states"] or
            any(state not in ("absent", "held") for state in finalization["states"].values()) or
            any(finalization.get(key) != target[key] for key in (
                "account_id", "region")) or finalization.get("pilot_id") != target["resource_id"]):
        raise ValueError("independent cleanup or selected delivery identity differs")
    try:
        observed_at = datetime.fromisoformat(cleanup["observed_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("independent cleanup timestamp is invalid") from error
    if observed_at.tzinfo is None:
        raise ValueError("independent cleanup timestamp needs a timezone")
    if not re.fullmatch(r"[0-9]{12}", target["account_id"]):
        raise ValueError("approved delivery account is invalid")
    summary_ref = f"github/actions/runs/{pins['run_id']}/attempts/{pins['run_attempt']}/devlake-pilot"
    cleanup_ref = f"github/actions/artifacts/{reference['ref']}"
    return {"schema_version": 2, "contract_digest": digest, "correlation": correlation,
            "repository_id": int(repository_id), "run_id": int(pins["run_id"]),
            "run_attempt": int(pins["run_attempt"]),
            "workflow_path": ".github/workflows/devlake-poc-qualify.yml",
            "workflow_revision": pins["definition_revision"],
            "source_revision": pins["source_revision"], "actual_revision": pins["source_revision"],
            "account_id": target["account_id"], "region": target["region"],
            "resource_kind": target["resource_kind"], "resource_id": target["resource_id"],
            "component": target["component"],
            "checks": [{"id": check, "outcome": "passed", "evidence_ref":
                        cleanup_ref if check == "scope-and-identity" else summary_ref}
                       for check in CHECKS],
            "cleanup": "passed", "produced_at": datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Emit separately verified generic pilot delivery receipt")
    for name in ("manifest", "plan", "summary", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = _load_document(args.manifest, "manifest")
        plan = _load_document(args.plan, "acceptance plan")
        summary = _load_document(args.summary, "pilot summary")
        target = json.loads(os.environ.get("ADP_DEVLAKE_DELIVERY_TARGET", ""))
        bindings = json.loads(os.environ.get("ADP_DEVLAKE_SOURCE_BINDINGS", ""))
        reference = manifest.get("delivery_cleanup_receipt")
        if not isinstance(reference, dict) or not isinstance(reference.get("ref"), str):
            raise ValueError("independent finalized cleanup receipt is missing")
        cleanup_raw = approved_reader(manifest, plan, bindings)(reference["ref"])
        result = receipt(summary, manifest, plan, cleanup_raw, target, os.environ)
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(result, output, sort_keys=True)
    except (ValueError, KeyError, TypeError, OSError):
        print("FAIL delivery: pilot, transport, target or independent cleanup is unavailable or invalid",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
