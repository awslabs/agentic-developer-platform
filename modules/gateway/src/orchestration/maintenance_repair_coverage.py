"""Classify repair observations already included in a verified owner checkpoint."""

import re

from sqlalchemy import select

from .accepted_node import accepted_node_contract
from .maintenance_adoption_resolution import verified_adoption
from .models import OrchestrationAcceptedPlan
from .review_cycle import CycleBlockedError


async def assignment_adoption(session, assignment, node):
    """Read the same immutable checkpoint used by deployment authorization."""
    from src.agentauth.github_operations import OperationRefusedError

    # Ordinary task reads use their assigned plan, even when a newer plan is
    # present. Do not add a dependency on current-plan resolution to those reads.
    document = await session.scalar(
        select(OrchestrationAcceptedPlan.plan_document).where(
            OrchestrationAcceptedPlan.org_id == assignment.tenant_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.version == assignment.accepted_plan_version,
        )
    )
    if not any(
        (item.get("operational_release_binding") or {}).get("maintenance_adoption_id")
        for item in (document or {}).get("nodes", [])
        if isinstance(item, dict)
    ):
        return None
    contract = await accepted_node_contract(session, node)
    release = contract.node.operational_release_binding if contract else None
    if release is None or release.maintenance_adoption_id is None:
        return None
    if contract.plan_version != assignment.accepted_plan_version or release.repository != assignment.repository:
        raise OperationRefusedError("maintenance repair assignment changed")
    try:
        return await verified_adoption(session, node, contract)
    except CycleBlockedError as exc:
        raise OperationRefusedError("maintenance repair checkpoint is unverified") from exc


def covered_publication(adoption, *, repository, receipt_ref, pull_number, head):
    """Only call with `verified_adoption` output, never worker-supplied proof.

    A later status read can name the final reviewed squash head rather than an
    earlier published head. Both identities are independently covered by the
    checkpoint, but neither permits ignoring a different PR or a changed head.
    This classifies observations; it does not manufacture a repair merge.
    """
    if adoption is None:
        return None
    release = adoption["after_binding"]
    if (
        repository != release["repository"]
        or type(pull_number) is not int
        or pull_number < 1
        or receipt_ref != f"github/{repository}/pull/{pull_number}"
        or not isinstance(head, str)
        or re.fullmatch(r"[a-f0-9]{40}", head) is None
    ):
        return None
    publications = [
        item
        for item in adoption["repair_history"]
        if item["kind"] == "operations_repair_pr"
        and item["status"] == "succeeded"
        and item["receipt"] == receipt_ref
        and (item["detail"] or {}).get("pull_number") == pull_number
        and (item["detail"] or {}).get("head") == head
        and (item["detail"] or {}).get("repository", repository) == repository
    ]
    ids = sorted(item["id"] for item in publications)
    basis = "included_publication"
    # verified_adoption revalidates the exact publication IDs, repository,
    # tree equality and check identities in every retained squash receipt.
    for proof in [*adoption["proof"].get("squashed_repair_history", []), *adoption.get("inherited_squash_proofs", [])]:
        if proof["pull_number"] == pull_number and proof["reviewed_head"] == head:
            ids, basis = proof["publication_ids"], "reviewed_squash"
            break
    if not ids:
        return None
    return {
        "adoption_id": release["maintenance_adoption_id"],
        "checkpoint_cycle": adoption["checkpoint_cycle"],
        "source_revision": adoption["pins"]["source_revision"],
        "basis": basis,
        "publication_ids": ids,
        "next_action": (
            "This exact PR and head are already included in the verified maintenance checkpoint. "
            "Continue unfinished work, retaining the adopted source and any later verified repair merges; "
            "do not merge or republish this historical repair. "
            "New repair heads still require their own verified merge."
        ),
    }
