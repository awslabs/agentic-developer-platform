"""A reader-enabled successor retains exactly the original component ownership."""

import importlib.util
import json
from pathlib import Path

# The owner collector reads this same contract outside the Gateway package.
_spec = importlib.util.spec_from_file_location("bootstrap_component_predecessor_contract", Path(__file__).with_name("domain_bootstrap_components.py"))
original = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(original)

KIND = "bootstrap-reader-components-revoked-v1"
SOURCE_FLAG = "reader_management_components_recovery"
PARTIAL_SOURCE_FLAG = "partial_reader_components_recovery"


def validate_source(source):
    original.require(source is not None and source.get(SOURCE_FLAG) is True)
    original.validate_source({**source, original.SOURCE_FLAG: True})


def transition(row, prior, *, complete, grant_identity=None):
    """Check full current plan and prior owned components before/after cleanup."""
    require = original.require
    previous = {
        **prior,
        "plan_json": json.dumps(prior["retained_plan"]),
        "progress_json": json.dumps(prior["retained_progress"]),
        "revoked": True,
    }
    require(original.journal_identity(previous, grant_identity=grant_identity) == prior)
    before = prior["retained_progress"]
    progress = json.loads(row["progress_json"])
    partial = "component_inventory_complete" not in progress
    current = original.reader_journal_identity(
        row, complete=complete, grant_identity=grant_identity, inherited_components=before["components"] if partial else None
    )
    require(current["generation"] != prior["generation"] and current["claim"] != prior["claim"])
    require(all(current[k] == prior[k] for k in ("operation_id", "org_id", "workspace_id", "cluster_arn")))
    before, after = prior["retained_progress"], current["retained_progress"]
    require(before["components"] == current.get("inherited_components", after["components"]))
    require(before["workspace-namespace"]["identity"] == after["workspace-namespace"]["identity"])
    if not partial:
        require(before["management_observation"]["endpoint"] == after["management_observation"]["endpoint"])
        require(before["system_workload_baseline"]["original_allocation_id"] == after["system_workload_baseline"]["original_allocation_id"])
    return current


def validate(value, *, grant_identity=None, repeat_contract=None):
    if repeat_contract is None:
        from src.internal.domain_operation_store import harness

        repeat_contract = harness("settled_bootstrap_repeat")

    require = original.require
    require(value.get("evidence_kind") == KIND)
    require(not {"attempt_extension", "denied_grant", "retained_revocation", "precomponent_failures"}.intersection(value))
    repeat = repeat_contract
    closure = repeat.validate(value["settled_bootstrap"])
    prior_receipt = closure["predecessor"]
    previous = repeat.retained_journals(closure)
    revoked = value["revoked_bootstrap"]
    require(set(revoked) == {"journals", "current_generation", "predecessor"})
    require(
        revoked["predecessor"]
        == {
            "reauthorization_id": prior_receipt["reauthorization_id"],
            "source_fence": prior_receipt["source_fence"],
            "proof_sha256": original.digest(repeat.decoded(prior_receipt["proof"])),
        }
    )
    journals = revoked["journals"]
    require(1 <= len(previous) <= repeat.MAX_PREDECESSORS and len(journals) == len(previous) + 1)
    current = [j for j in journals if j["generation"] == revoked["current_generation"]]
    require(len(current) == 1 and [j for j in journals if j not in current] == previous)
    identity = current[0]
    require(all(identity[k] == value[k] for k in ("operation_id", "org_id", "workspace_id")))
    row = {
        **identity,
        "plan_json": json.dumps(identity["retained_plan"]),
        "progress_json": json.dumps(identity["retained_progress"]),
        "revoked": True,
    }
    require(transition(row, repeat.component_anchor(closure), complete=True, grant_identity=grant_identity) == identity)
    if "inherited_components" in identity:
        spec = importlib.util.spec_from_file_location("bootstrap_partial_source_contract", Path(__file__).with_name("domain_bootstrap_boundary.py"))
        source = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(source)
        qualified = source.qualified_source(value["source_image"])
        original.require(qualified is not None and qualified.get(PARTIAL_SOURCE_FLAG) is True)
    return identity
