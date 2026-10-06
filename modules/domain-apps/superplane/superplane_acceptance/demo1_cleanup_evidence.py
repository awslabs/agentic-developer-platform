"""Bind the authenticated historical preparation projection to its saved approval."""

from .demo1_evidence import EvidenceError, digest, identifier, instant
from .demo1_report import reference


def require(condition):
    if not condition:
        raise EvidenceError(
            "cleanup evidence: immutable preparation unavailable or mismatched"
        )


def observe_cleanup(reader, store, preparation, max_runtime_seconds, *, now, review):
    selected, original, saved = reader.selected, store.original, store.load()
    require(
        saved is not None
        and saved.submitted
        and selected.authorized_at <= now < selected.deadline
        and preparation.get("submission_observed") is True
        and preparation.get("state") == "succeeded"
        and preparation.get("request_ref") == reference(saved.request_id)
        and preparation.get("retirement_complete") is False
    )
    runtime = reader.observe(max_runtime_seconds)
    observed = review.get("cleanup_preparation")
    hashes = (
        "artifact_id",
        "grant_set_sha256",
        "fence_sha256",
        "inventory_sha256",
        "managed_workload_inventory_sha256",
        "destroy_sha256",
        "plan_file_sha256",
        "plan_json_sha256",
        "backend_sha256",
        "retirement_plan_sha256",
        "retirement_revision_sha256",
        "preparation_revision",
        "preparation_plan_revision",
    )
    bindings = {
        "version": 1,
        "status": "OBSERVED",
        "org_id": selected.org_id,
        "workspace_id": original.workspace_id,
        "source_operation_id": saved.source_operation_id,
        "retirement_request_id": saved.retirement_request_id,
        "preparation_request_id": saved.request_id,
        "preparation_revision": saved.revision,
        "preparation_plan_revision": saved.plan_revision,
        "preparation_approval_id": saved.approval_id,
    }
    require(
        runtime.get("status") == "OBSERVED"
        and runtime.get("release_ref") == reference(reader.target.release_id)
        and isinstance(observed, dict)
        and set(observed)
        == set(hashes)
        | set(bindings)
        | {
            "operation_id",
            "recorded_at",
            "producer_attempt_id",
            "producer_fence_token",
            "grant_count",
        }
        and all(observed.get(key) == value for key, value in bindings.items())
        and reference(identifier(observed["operation_id"], "cleanup operation"))
        == preparation.get("operation_ref")
        and selected.authorized_at
        <= instant(observed["recorded_at"], "cleanup artifact time")
        <= now
        and type(observed["producer_fence_token"]) is int
        and observed["producer_fence_token"] > 0
        and type(observed["grant_count"]) is int
        and 0 < observed["grant_count"] <= 128
        and observed["retirement_revision_sha256"] == review.get("revision")
        and observed["retirement_plan_sha256"]
        == review.get("approval_request", {}).get("parameters", {}).get("plan_revision")
        and store.load() == saved
    )
    identifier(observed["producer_attempt_id"], "cleanup producer attempt")
    for key in hashes:
        digest(observed[key], "cleanup artifact digest")
    return {
        "status": "OBSERVED",
        "scope": "authenticated historical preparation and canonical deletion request; current provider state and deletion unverified",
        "release_ref": runtime["release_ref"],
        "observed_at": now.isoformat(),
        "recorded_at": observed["recorded_at"],
        "operation_ref": preparation["operation_ref"],
        "producer_attempt_ref": reference(observed["producer_attempt_id"]),
        "producer_fence_token": observed["producer_fence_token"],
        "grant_count": observed["grant_count"],
        "current_eks_grants": {
            "status": "UNVERIFIED",
            "scope": "supplemental client observation; server execution gates remain required",
        },
        "current_kubernetes": {
            "status": "UNVERIFIED",
            "scope": "supplemental client observation; server execution gates remain required",
        },
        **{
            key.removesuffix("_sha256").removesuffix("_id") + "_ref": reference(
                observed[key]
            )
            for key in hashes
        },
    }
