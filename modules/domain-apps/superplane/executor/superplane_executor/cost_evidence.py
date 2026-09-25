"""Allocation resource evidence is not an allocation-attributed AWS bill.

The shared ledger retains ownership of budgets. No billing observation is available
through this executor's resource inventory, so neither removal nor reservation release
can populate a billed amount or a measured usage interval.
"""

_CATEGORIES = {
    "compute": frozenset({"instance"}),
    "storage": frozenset({"volume"}),
    "network": frozenset({"network_interface", "address", "network_dependency"}),
    # These are contextual handles, never evidence of bytes transferred.
    "transfer": frozenset({"instance", "network_interface", "network_dependency"}),
}

_USAGE_REASONS = {
    "compute": "Instance disposition does not establish billed running time.",
    "storage": "Volume disposition does not establish allocated byte-hours.",
    "network": "Network resource disposition does not establish metered usage.",
    "transfer": "Resource inventory does not measure transferred bytes or billing direction.",
}


def build_cost_evidence(operation, plan, resources, accounting):
    """Project retained handles and separate unknowns without estimating charges.

    ``resources`` is the original allocation's durable shared inventory, including
    deleted resources. ``accounting`` contains the engine's current dispositions.
    The observation timestamp dates those dispositions, not a fabricated billing
    coverage interval. A future billing source must supply its own attribution and
    interval; it cannot infer either from this report.
    """
    lease = operation.grant.lease
    dispositions = accounting["resource_dispositions"]
    categories = {}
    for category, kinds in _CATEGORIES.items():
        observations = []
        for resource in sorted(resources.values(), key=lambda item: item.resource_id):
            if resource.provider != "aws" or resource.kind not in kinds:
                continue
            observations.append(
                {
                    "resource_id": resource.resource_id,
                    "provider_reference": resource.provider_reference,
                    "kind": resource.kind,
                    "disposition": dispositions.get(resource.resource_id, "unobserved"),
                }
            )
        categories[category] = {
            "resource_observations": observations,
            "accounting_checked_at": accounting["checked_at"],
            "resource_inventory_complete": accounting["inventory_complete"],
            "usage": {
                "status": "unknown",
                "quantity": None,
                "unit": None,
                "interval_start": None,
                "interval_end": None,
                "reason": _USAGE_REASONS[category],
            },
            "billed_cost": {
                "status": "unknown",
                "amount": None,
                "currency": None,
                "interval_start": None,
                "interval_end": None,
                "reason": "No allocation-attributed provider billing observation is available.",
            },
        }
    return {
        "version": 1,
        "source": "shared_allocation_inventory",
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
        "allocation_id": operation.request.parameters["allocation_id"],
        "observation_operation_id": lease.operation_id,
        "source_operation_id": operation.request.parameters.get(
            "controller_source_operation_id"
        )
        or (lease.operation_id if operation.request.action == "provision" else None),
        "approved_provider_account_id": plan.data["provider_account_id"],
        "categories": categories,
    }
