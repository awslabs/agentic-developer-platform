"""Seal original paid infrastructure from actual state and fresh AWS observations."""

import asyncio
import json
from types import SimpleNamespace

from harness_jobs.execution import CallOutcome
from harness_jobs.inventory import InventoryAuthority, ResourcePresence

from .artifacts import read_artifact
from .authority import current_operation
from .provider_listings import lifecycle_providers
from .retirement_inventory import RetirementInventory
from .retirement_observation import RetirementObservations
from .runtime_config import LifecycleRefused

IDENTITY_FIELDS = frozenset(
    {
        "id",
        "arn",
        "name",
        "role",
        "policy_arn",
        "cluster_name",
        "node_group_name",
        "addon_name",
        "route_table_id",
        "allocation_id",
        "unique_id",
        "key_id",
        "target_key_id",
    }
)


def identities_from_state(state):
    """Retain identity fields only; full Terraform state never enters SQL artifacts."""
    if not isinstance(state, dict) or state.get("format_version") != "1.0":
        raise LifecycleRefused("applied Terraform state format is unavailable")
    root = state.get("values", {}).get("root_module")
    if not isinstance(root, dict):
        raise LifecycleRefused("applied Terraform state has no root module")
    result, seen = [], set()

    def visit(module, depth=0):
        if depth > 16 or not isinstance(module.get("resources", []), list):
            raise LifecycleRefused("applied resource inventory is incomplete")
        for resource in module.get("resources", []):
            if (
                resource.get("mode") == "data"
                or resource.get("type") == "terraform_data"
            ):
                continue
            kind, address, values = (
                resource.get("type"),
                resource.get("address"),
                resource.get("values"),
            )
            if (
                resource.get("mode") != "managed"
                or not isinstance(kind, str)
                or not kind.startswith("aws_")
                or not isinstance(address, str)
                or address in seen
                or not isinstance(values, dict)
                or len(result) >= 500
            ):
                raise LifecycleRefused("applied resource ownership is malformed")
            identity = {
                key: values[key]
                for key in IDENTITY_FIELDS
                if isinstance(values.get(key), str) and values[key]
            }
            if not identity:
                raise LifecycleRefused("applied resource has no provider identity")
            seen.add(address)
            result.append({"address": address, "type": kind, "identity": identity})
        children = module.get("child_modules", [])
        if not isinstance(children, list):
            raise LifecycleRefused("applied child module inventory is malformed")
        for child in children:
            visit(child, depth + 1)

    visit(root)
    if not result:
        raise LifecycleRefused("applied resource inventory is empty")
    return sorted(result, key=lambda row: row["address"])


def infrastructure_document(rows):
    # Reuse the maintained physical-resource catalogue without manufacturing a
    # destroy approval. This document supplies only observed resource identities.
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        raise LifecycleRefused("original applied resource inventory is unavailable")
    resources = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"address", "type", "identity"}
            or not isinstance(row["identity"], dict)
            or set(row["identity"]) - IDENTITY_FIELDS
            or not all(
                isinstance(value, str) and value for value in row["identity"].values()
            )
        ):
            raise LifecycleRefused("original applied identities changed")
        resources.append(
            {
                "mode": "managed",
                "type": row["type"],
                "address": row["address"],
                "change": {"before": row["identity"]},
            }
        )
    return {"resource_changes": resources}


async def seal_applied_inventory(operation, context, result):
    """ExecutionRPCServer after-step hook; executes before closing the apply lease."""
    from .runtime import delivery_session

    if result[0].outcome is not CallOutcome.SUCCEEDED:
        return
    current = await current_operation(operation, context)
    lease = current.grant.lease
    if current.request.parameters.get("lifecycle_phase") != "apply-infrastructure":
        raise LifecycleRefused(
            "only original paid apply can produce infrastructure inventory"
        )
    async with context.domain_connect() as connection:
        rows = await connection.fetch(
            "SELECT artifact_id FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
    if len(rows) != 1:
        raise LifecycleRefused("original apply artifact is unavailable or ambiguous")
    row = await read_artifact(
        context.domain_connect,
        artifact_id=rows[0]["artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    metadata = json.loads(row["artifact_metadata_json"])
    if (
        metadata.get("allocation_source_operation_id") != lease.operation_id
        or metadata.get("next_phase") != "bootstrap-workspace"
    ):
        raise LifecycleRefused("applied inventory lost its original paid source")
    document = infrastructure_document(metadata.get("applied_resources"))
    outputs = {key: value["value"] for key, value in metadata["outputs"].items()}
    if (outputs["org_id"], outputs["workspace_id"], outputs["account_id"]) != (
        lease.org_id,
        lease.workspace_id,
        row["account_id"],
    ):
        raise LifecycleRefused("applied inventory belongs to another scope")
    clusters = [
        item["change"]["before"]
        for item in document["resource_changes"]
        if item["type"] == "aws_eks_cluster"
    ]
    if (
        len(clusters) != 1
        or clusters[0].get("arn") != outputs["cluster_arn"]
        or clusters[0].get("name") != outputs["cluster_name"]
    ):
        raise LifecycleRefused("applied state omits the exact managed cluster")
    session = await delivery_session(
        current, context, outputs["account_id"], outputs["aws_region"]
    )
    inventory = RetirementInventory(
        lease.workspace_id,
        lease.org_id,
        outputs["cluster_arn"],
        "adp-created",
        "",
        "",
        False,
        (),
        (),
        components_complete=True,
    )
    reader = RetirementObservations(
        session=session,
        kubernetes=SimpleNamespace(
            target=SimpleNamespace(
                cluster_arn=outputs["cluster_arn"],
                account_id=outputs["account_id"],
                region=outputs["aws_region"],
            )
        ),
        eks=None,
    )
    resources = {}

    async def authenticate(_token):
        return (await current_operation(operation, context)).grant

    async def query(_lease, members, _query_id):
        if _lease.operation_id != lease.operation_id:
            raise LifecycleRefused("applied inventory query changed operation")
        observed = {}
        for member in members:
            await current_operation(operation, context)
            observed[member.resource_id] = await asyncio.to_thread(
                reader.observe, inventory, member
            )
        await current_operation(operation, context)
        return observed

    authority = InventoryAuthority(
        connect=context.connect, authenticate=authenticate, query_provider=query
    )
    creation_keys = frozenset({result[0].idempotency_key})

    async def discover():
        await current_operation(operation, context)
        found = await asyncio.to_thread(
            reader.catalog,
            inventory,
            None,
            current.request.parameters,
            resources,
            creation_keys,
            include_bootstrap=False,
            infrastructure_document=document,
        )
        await current_operation(operation, context)
        return found

    resources = await discover()
    if not resources:
        raise LifecycleRefused("original apply has no established physical inventory")
    allocation = current.request.parameters["allocation_id"]
    providers = await lifecycle_providers(context.connect, lease, allocation)
    attempts = {}
    async with context.connect() as connection:
        sealed = await connection.fetchval(
            "SELECT sealed_revision FROM harness_allocation_seal WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation,
        )
        if not sealed:
            await authority.enumerate_resources(
                connection, lease, resources=tuple(resources.values())
            )
        for provider in providers:
            attempts[provider] = await authority.begin_provider_enumeration(
                connection, lease, provider=provider
            )
    listing_returned = False
    try:
        fresh = await discover()
        observed = await query(lease, tuple(fresh.values()), None)
        if any(
            value.presence is ResourcePresence.UNKNOWN for value in observed.values()
        ):
            raise LifecycleRefused("original apply provider census is incomplete")
        if await lifecycle_providers(context.connect, lease, allocation) != providers:
            raise LifecycleRefused("original apply provider journal changed")
        listing_returned = True
        async with context.connect() as connection:
            for provider, attempt in attempts.items():
                await authority.record_provider_enumeration(
                    connection,
                    lease,
                    provider=provider,
                    provider_references=frozenset(
                        member.provider_reference
                        for key, member in fresh.items()
                        if member.provider == provider
                        and observed[key].presence is ResourcePresence.PRESENT
                    ),
                    attempt=attempt,
                )
            await authority.seal_allocation(connection, lease)
            report = await authority.observe_report(connection, lease)
            await authority.publish_report(connection, lease, observations=report)
    except Exception:
        if not listing_returned:
            async with context.connect() as connection:
                for attempt in attempts.values():
                    await authority.fail_provider_enumeration(
                        connection, lease, attempt=attempt
                    )
        raise
