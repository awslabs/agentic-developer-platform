"""Explicit physical-provider census for maintained lifecycle wrapper operations."""

from harness_jobs.identity import OperationRefused
from harness_jobs.store import stored_outcome

# These descriptors delegate to AWS/Kubernetes or local governance; they do not
# represent additional cloud-provider accounts. AWS resource membership is read
# independently. Unknown wrapper kinds cannot receive an empty listing.
WRAPPERS = {
    "superplane-lifecycle": {"apply-infrastructure"},
    "superplane-governance": {"block-governed-admission", "drain-governed-workloads"},
    "superplane-registry": {"unregister-workspace"},
    "superplane-kubernetes": {"delete-controller-component", "revoke-grant"},
    "superplane-terraform": {"apply-reviewed-destroy"},
    "superplane-aws": {
        "revoke-grant",
        "revoke-network-prerequisite",
        "verify-resource-inventory",
    },
}


async def lifecycle_providers(connect, lease, allocation_id):
    async with connect() as connection:
        rows = await connection.fetch(
            "SELECT provider,operation_kind,provider_ref,outcome,stage FROM harness_provider_call_intent "
            "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 LIMIT 1024",
            lease.org_id,
            lease.workspace_id,
            allocation_id,
        )
    if not rows or len(rows) >= 1024:
        raise OperationRefused(
            "lifecycle provider journal is unavailable or exceeds its bound"
        )
    providers = {"aws"}
    for row in rows:
        provider = row["provider"]
        if (
            provider not in WRAPPERS
            or row["operation_kind"] not in WRAPPERS[provider]
            or stored_outcome(row["outcome"]) != "succeeded"
            or row["stage"] not in {"observed", "reconciled"}
            or (provider == "superplane-lifecycle" and row["provider_ref"] is not None)
        ):
            raise OperationRefused(
                "lifecycle wrapper has unverified provider obligations"
            )
        providers.add(provider)
    return tuple(sorted(providers))
