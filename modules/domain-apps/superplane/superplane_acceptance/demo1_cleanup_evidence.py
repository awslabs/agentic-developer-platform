"""Bind recorded cleanup grants and destroy-plan hashes to the original admission."""

from .demo1_cleanup_grants import observe_grants
from .demo1_evidence import EvidenceError, digest, identifier, instant
from .demo1_report import reference


def require(condition):
    if not condition:
        raise EvidenceError(
            "cleanup evidence: immutable preparation unavailable or mismatched"
        )


def observe_cleanup(reader, store, preparation, max_runtime_seconds, *, now, provider):
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
    scope = {
        "org_id": selected.org_id,
        "workspace_id": original.workspace_id,
        "request_id": selected.request_id,
        "plan_revision": selected.plan_revision,
        "account": selected.account,
        "region": selected.region,
        "authorized_at": selected.authorized_at.isoformat(),
        "observed_at": now.isoformat(),
        "preparation_request_id": saved.request_id,
        "preparation_revision": saved.revision,
        "preparation_plan_revision": saved.plan_revision,
        "retirement_request_id": saved.retirement_request_id,
        "source_operation_id": saved.source_operation_id,
        "original_allocation_id": saved.original_allocation_id,
        "approval_id": saved.approval_id,
    }
    runtime = reader.observe(max_runtime_seconds, cleanup_scope=scope)
    observed = runtime.get("cleanup")
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
    )
    require(
        runtime.get("status") == "OBSERVED"
        and runtime.get("release_ref") == reference(reader.target.release_id)
        and isinstance(observed, dict)
        and set(observed)
        == set(hashes)
        | {
            "status",
            "scope",
            "operation_id",
            "recorded_at",
            "producer_attempt_id",
            "producer_fence_token",
            "grant_count",
            "grants",
        }
        and observed["status"] == "OBSERVED"
        and observed["scope"] == scope
        and reference(identifier(observed["operation_id"], "cleanup operation"))
        == preparation.get("operation_ref")
        and selected.authorized_at
        <= instant(observed["recorded_at"], "cleanup artifact time")
        <= now
        and type(observed["producer_fence_token"]) is int
        and observed["producer_fence_token"] > 0
        and type(observed["grant_count"]) is int
        and 0 < observed["grant_count"] <= 100
        and store.load() == saved
    )
    identifier(observed["producer_attempt_id"], "cleanup producer attempt")
    for key in hashes:
        digest(observed[key], "cleanup artifact digest")
    require(
        isinstance(observed["grants"], list)
        and observed["grant_count"] == len(observed["grants"])
    )
    current = observe_grants(
        provider, selected, observed["grants"], observed["grant_set_sha256"]
    )
    require(store.load() == saved)
    return {
        "status": "OBSERVED",
        "scope": "immutable preparation and canonical recorded deletion plan, plus current EKS cleanup entry; Kubernetes grants, fence, provider inventory, plan bytes and cleanup unverified",
        "release_ref": runtime["release_ref"],
        "observed_at": now.isoformat(),
        "recorded_at": observed["recorded_at"],
        "operation_ref": preparation["operation_ref"],
        "producer_attempt_ref": reference(observed["producer_attempt_id"]),
        "producer_fence_token": observed["producer_fence_token"],
        "grant_count": observed["grant_count"],
        "current_eks_grants": current,
        **{
            key.removesuffix("_sha256").removesuffix("_id") + "_ref": reference(
                observed[key]
            )
            for key in hashes
        },
    }
