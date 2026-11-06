"""Read settled release evidence under its original, preserved contract."""

from dataclasses import replace

from .proposal import ProposedNode


def settled_retry_contract(current, previous_plan):
    """Permit only adding an explicit job role across verified plan lineage.

    This does not authorize a workflow or rewrite an old receipt. The next
    attempt records the current contract and must pass current job authority.
    Target, checks, cleanup, source, workflow and predecessor pins stay exact.
    """
    matches = [
        item for item in previous_plan.plan_document.get("nodes", []) if isinstance(item, dict) and item.get("address") == current.node.address
    ]
    if len(matches) != 1:
        return None
    try:
        previous = ProposedNode.model_validate(matches[0])
    except ValueError:
        return None
    before, after = previous.operational_release_binding, current.node.operational_release_binding
    if before == after:
        return current
    if (
        before is None
        or after is None
        or before.delivery_contract is None
        or after.delivery_contract is None
        or before.delivery_contract.job_mutation_role_arn is not None
        or after.delivery_contract.job_mutation_role_arn is None
    ):
        return None
    original = before.model_dump(mode="json")
    amended = after.model_dump(mode="json")
    amended["delivery_contract"].pop("job_mutation_role_arn")
    if original != amended:
        return None
    return replace(current, plan_version=previous_plan.version, node=previous)
