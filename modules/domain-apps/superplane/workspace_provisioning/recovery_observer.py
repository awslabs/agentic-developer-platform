"""Protected API observation of immutable lifecycle results; never mutation authority."""

import json

from .artifacts import read_artifact
from .provider_observation import observe_applied_target
from .runtime_config import LifecycleRefused


async def observe_result(operation, context, *, artifact, provider):
    """Caller authenticates original_result and its live recovery claim first.

    This API-side observer uses only a bounded read provider. Persisted Terraform
    bytes are independently checked by the worker before it consumes these facts.
    It never delivers credentials, replays apply, changes registration or releases
    the original reservation. Bootstrap recovery requires an immutable canonical
    journal anchor and is deliberately not implemented by this observation path.
    """
    parameters = operation.request.parameters
    if parameters.get("lifecycle_phase") != "apply-infrastructure":
        raise LifecycleRefused(
            "lifecycle result phase has no read-only recovery observer"
        )
    metadata = json.loads(artifact["artifact_metadata_json"])
    if (
        metadata.get("next_phase") != "bootstrap-workspace"
        or metadata.get("allocation_source_operation_id")
        != artifact["source_operation_id"]
        or metadata.get("source_artifact_id") != parameters.get("lifecycle_artifact_id")
        or not metadata.get("provider_snapshot")
    ):
        raise LifecycleRefused(
            "apply result lacks its original immutable provider inventory"
        )
    source = await read_artifact(
        context.domain_connect,
        artifact_id=metadata["source_artifact_id"],
        org_id=artifact["org_id"],
        workspace_id=artifact["workspace_id"],
        require_fresh=False,
    )
    from .partial_apply_execution import result_source

    prepared = await result_source(operation, context, source, metadata)
    source_metadata = json.loads(prepared["artifact_metadata_json"])
    if (
        source_metadata.get("next_phase")
        != (
            "partial-apply-repair" if prepared is not source else "apply-infrastructure"
        )
        or metadata.get("module_sha256") != source_metadata.get("module_sha256")
        or artifact["target_json"] != source["target_json"]
    ):
        raise LifecycleRefused("apply result differs from its reviewed preparation")
    outputs = {key: value["value"] for key, value in metadata["outputs"].items()}
    target = json.loads(artifact["target_json"])
    if any(
        outputs.get(key) != target.get(key)
        for key in ("account_id", "aws_region", "org_id", "workspace_id")
    ):
        raise LifecycleRefused("apply recovery output belongs to another workspace")
    observed = await observe_applied_target(outputs, provider.aws_read)
    if metadata.get("repair_artifact_id"):
        from .partial_apply_plan import require

        addresses = await provider.aws_read(
            "ec2",
            "describe_addresses",
            Filters=[
                {"Name": "allocation-id", "Values": [metadata["replaced_eip_absent"]]}
            ],
        )
        require(addresses.get("Addresses") == [], "replaced EIP absence changed")
    if observed != metadata["provider_snapshot"]:
        raise LifecycleRefused(
            "applied provider inventory changed since its immutable result"
        )
    return {"provider_snapshot": observed, "source_artifact_id": source["artifact_id"]}
