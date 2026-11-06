"""A distinct, once-only retry of an interrupted initial read-only plan."""

from .identity import OperationRefused

KIND = "interrupted-read-only-plan-v1"


def require_initial_step(record, step, receipts):
    if (
        receipts
        or record.action != "provision"
        or step.provider != "superplane-lifecycle"
        or step.step_id != "prepare-infrastructure"
        or step.operation_kind != "prepare-infrastructure"
    ):
        raise OperationRefused(
            "interrupted recovery requires the original initial plan"
        )


def valid_proof(proof):
    if not isinstance(proof, dict):
        return False
    owner = proof.get("owner_evidence", {})
    boundary = proof.get("preparation_boundary", {})
    if not isinstance(owner, dict) or not isinstance(boundary, dict):
        return False
    backend = boundary.get("backend_observation", {})
    if not isinstance(backend, dict):
        return False
    return (
        proof.get("evidence_kind") == KIND
        and proof.get("planning_started") is True
        and proof.get("apply_started") is False
        and proof.get("original_authority_expired") is True
        and proof.get("issued_sessions_expired") is True
        and bool(proof.get("owner_evidence_id"))
        and proof["owner_evidence_id"] == owner.get("record_id")
        and owner.get("evidence_kind") == KIND
        and owner.get("planning_started") is True
        and owner.get("apply_started") is False
        and boundary == owner.get("preparation_boundary")
        and boundary.get("kind") == KIND
        and boundary.get("read_only_recipe") is True
        and backend.get("versions_absent") is True
        and backend.get("delete_markers_absent") is True
        and backend.get("checksum_absent") is True
        and "attempt_extension" not in proof
        and "attempt_extension" not in owner
    )
