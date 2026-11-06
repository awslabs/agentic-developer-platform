"""Compose WA05 AC-03 only from independently collected exact reuse facts."""

from datetime import UTC, datetime

from qualification_well_architected_denials import verify_baseline
from qualification_well_architected_observations import digest
from qualification_well_architected_reuse import canonical, timestamp, verify_task, verify_usage
from qualification_well_architected_revocation import retained_selection, verify_proof


REQUIRED = {
    "usage": "completed later canonical task and linked Context/Insights usage receipts",
    "denial_baseline": "successful owner result/query and pinned source artifact reads",
    "denial": "different authenticated reader denied the same result/query/source artifacts",
    "source_revocation": "original owner denied those surfaces after a proven Product source unlink",
}


def evaluate(case, observed, *, installed_verified):
    clients = observed.get("clients") or {}
    missing = [description for key, description in REQUIRED.items() if not clients.get(key)]
    if not installed_verified:
        missing.append("unchanged installed release artifacts for the selected native cases")
    progress = {"id": "WA05/AC-03", "run_id": case["run_id"], "result_revision": case["result_revision"],
                "outcome": "incomplete", "required_evidence": missing, "provider_effect_count": None}
    if missing:
        return progress
    result, usage = case["result"], clients["usage"]
    selected = {"scope": result["scope"], "run_id": case["run_id"], "result_id": result["result_id"],
                "result_revision": case["result_revision"], "sources": case["sources"]}
    actor = usage.get("actor") or {}
    if (usage.get("source") != "authenticated-product-api" or actor.get("account_type") != "human"
            or not actor.get("user_id") or actor.get("org_id") != selected["scope"]["tenant_id"]
            or usage.get("actor_id") != actor["user_id"] or usage.get("provider_effect_count") is not None
            or any(clients.get(key) != selected[key] for key in ("scope", "run_id", "result_id", "result_revision"))
            or clients.get("clients", {}).get("api") != canonical(result)
            or clients.get("publication", {}).get("result_id") != result["result_id"]
            or clients.get("publication", {}).get("revision") != result["revision"]
            or usage.get("origin", {}).get("run_id") != case["run_id"]
            or usage["origin"].get("input") != case["run_input"]):
        raise ValueError("reuse evidence differs from the selected authenticated result and origin")
    task = verify_task(usage["task"], selected, usage["task_id"], usage["origin"])
    verify_usage({**usage, "result": result}, result, actor, task, clients["publication"])
    baseline = clients["denial_baseline"]
    paths = verify_baseline(baseline, selected, result, case["source_manifest"])
    foreign, revoked = clients["denial"], clients["source_revocation"]
    if (baseline.get("reader") != actor or foreign.get("reader", {}).get("account_type") != "human"
            or not foreign.get("reader", {}).get("user_id") or not foreign["reader"].get("org_id")
            or foreign["reader"] == actor or foreign.get("kind") != "selected_reader"
            or foreign.get("source_revocation_observed") is not False
            or revoked.get("reader") != actor or revoked.get("source_revocation_observed") is not True
            or revoked.get("execution_authorized") is not False or revoked.get("provider_effect_count") is not None
            or any(revoked.get(key) != selected[key] for key in ("scope", "run_id", "result_id", "result_revision"))):
        raise ValueError("foreign and revoked denial must bind different and original authenticated readers")
    for denial in (foreign, revoked):
        if (denial.get("source") != "authenticated-product-api" or denial.get("status") != 404
                or denial.get("derived_content_bytes") != 0 or denial.get("baseline_digest") != canonical(baseline)
                or not set(paths) <= set(denial.get("denied_paths", []))):
            raise ValueError("denied reads omit the same successful result/query/artifact surfaces")
    recorded = observed.get("qualification_observations") or {}
    selection = {**selected, "run_revision": recorded.get("run_revision"), "project_kind": case["project_kind"],
                 "qualification_experiments": [{key: item["receipt"].get(key)
                    for key in ("experiment_id", "revision", "selection_digest")} for item in recorded.get("items", [])]}
    receipt, original_actor = retained_selection(selection, case, observed)
    if (original_actor != actor or receipt["selection"].get("run_admission_digest") != digest(usage["origin"].get("admission"))
            or revoked.get("proof_sha256") != digest(revoked.get("proof"))):
        raise ValueError("later task origin or source unlink differs from the original frozen observation")
    verify_proof(revoked["proof"], selection, receipt, actor, revoked["proof"]["operation_id"],
                 timestamp(revoked["baseline_at"]), datetime.now(UTC))
    if timestamp(task["created_at"]) > timestamp(revoked["baseline_at"]):
        raise ValueError("later task evidence must precede the selected source unlink baseline")
    progress.update(outcome="passed", required_evidence=[])
    return progress
