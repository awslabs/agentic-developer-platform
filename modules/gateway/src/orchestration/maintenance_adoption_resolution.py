"""Verify an owner maintenance checkpoint before dispatch or evidence consumption."""

import json
from dataclasses import replace

from sqlalchemy import select

from .continuation import digest
from .maintenance_adoption import CONTRACT, KIND, repair_history
from .maintenance_adoption_provider import require
from .models import OrchestrationAcceptedPlan, OrchestrationDecision
from .plan_lineage import ancestor_plan

MAX_CHECKPOINTS = 100


def inherited_squash_proofs(parent):
    """Deterministic coverage inherited only from an already verified parent."""
    proofs = [*parent.get("inherited_squash_proofs", []), *parent["proof"].get("squashed_repair_history", [])]
    unique = {digest(proof): proof for proof in proofs}
    return [unique[key] for key in sorted(unique)]


def _before_contract(current, data):
    from .proposal import ProposedNode

    document = current.node.model_dump(mode="json")
    document["operational_release_binding"] = data["before_binding"]
    return replace(current, plan_version=data["plan_version"], node=ProposedNode.model_validate(document))


async def verified_adoption(session, node, contract, *, appending=False):
    chain = await _verified_chain(session, node, contract)
    # A valid chain at the limit remains readable, but cannot accept a receipt
    # that would make its very next dispatch unverifiable.
    require(not appending or len(chain) < MAX_CHECKPOINTS, "receipt_chain_limit")
    return chain[0][1] if chain else None


async def verified_adoption_checkpoints(session, node, contract):
    """Read all verified source checkpoints without changing prior receipts."""
    return [data for _, data in await _verified_chain(session, node, contract)]


async def _verified_chain(session, node, contract):
    """Verify every immutable checkpoint, including ones without a later run."""
    try:
        chain, seen = [], set()
        if contract.node.operational_release_binding is None:
            return chain
        while contract.node.operational_release_binding.maintenance_adoption_id is not None:
            identity = contract.node.operational_release_binding.maintenance_adoption_id
            require(identity not in seen, "receipt_chain_changed")
            require(len(chain) < MAX_CHECKPOINTS, "receipt_chain_limit")
            seen.add(identity)
            data = await _verified_adoption(session, node, contract)
            if chain:
                newer = chain[-1][1]
                require(
                    newer.get("prior_adoption_digest") == digest(data)
                    and newer.get("inherited_squash_proofs", []) == inherited_squash_proofs(data)
                    and data["checkpoint_cycle"] <= newer["checkpoint_cycle"],
                    "receipt_chain_changed",
                )
            chain.append((contract, data))
            contract = _before_contract(contract, data)
        require(not chain or not chain[-1][1].get("inherited_squash_proofs"), "receipt_chain_changed")
        return chain
    except (AttributeError, KeyError, ValueError, TypeError):
        require(False, "receipt_unverified")


async def _verified_adoption(session, node, contract):
    from .shared_window import accepted_document_hash

    release = contract.node.operational_release_binding
    identity = release.maintenance_adoption_id if release else None
    if identity is None:
        return None
    receipt = await session.get(OrchestrationDecision, identity)
    require(
        receipt is not None
        and receipt.org_id == node.org_id
        and receipt.flow_id == node.flow_id
        and receipt.node_id == node.id
        and receipt.kind == KIND
        and receipt.actor_kind == "human"
        and receipt.actor_role == "platform_admin"
        and receipt.actor_id,
        "receipt_unverified",
    )
    try:
        data = json.loads(receipt.reason)
    except (TypeError, ValueError):
        require(False, "receipt_unverified")
    require(
        data.get("contract") == CONTRACT
        and data.get("node_id") == node.id
        and data.get("flow_id") == node.flow_id
        and data.get("org_id") == node.org_id
        and data.get("after_binding") == release.model_dump(mode="json"),
        "receipt_scope_changed",
    )
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == node.org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.version == contract.plan_version,
        )
    )
    accepted = await ancestor_plan(session, plan, data.get("resulting_plan_version"), node_id=node.id)
    previous = await ancestor_plan(session, plan, data.get("plan_version"), node_id=node.id)
    require(
        accepted is not None
        and previous is not None
        and accepted.plan_hash == data.get("resulting_plan_hash")
        and previous.plan_hash == data.get("plan_hash")
        and accepted.plan_hash == accepted_document_hash(accepted)
        and previous.plan_hash == accepted_document_hash(previous)
        and accepted.version == previous.version + 1,
        "receipt_plan_changed",
    )
    amendment = await session.get(OrchestrationDecision, accepted.accepted_by_decision_id)
    require(amendment is not None and amendment.actor_id == receipt.actor_id and amendment.actor_kind == "human", "receipt_approver_changed")
    before = [n for n in previous.plan_document["nodes"] if n.get("address") == contract.node.address]
    require(len(before) == 1 and before[0].get("operational_release_binding") == data.get("before_binding"), "receipt_original_changed")
    accepted_nodes = [n for n in accepted.plan_document["nodes"] if n.get("address") == contract.node.address]
    require(
        len(accepted_nodes) == 1 and accepted_nodes[0].get("operational_release_binding") == data["after_binding"],
        "receipt_contract_changed",
    )
    after = release.model_dump(mode="json")
    original = data["before_binding"]
    allowed = {"baseline_revision", "definition_revision", "job_mutation_role_arn"}
    require(
        {k: v for k, v in after.items() if k not in {"maintenance_adoption_id", "delivery_contract"}}
        == {k: v for k, v in original.items() if k not in {"maintenance_adoption_id", "delivery_contract"}}
        and {k: v for k, v in after["delivery_contract"].items() if k not in allowed}
        == {k: v for k, v in original["delivery_contract"].items() if k not in allowed},
        "receipt_contract_changed",
    )
    proof = data["proof"]
    expected = {
        "source_revision": proof["source_revision"],
        "definition_revision": proof["source_revision"],
        "default_branch": release.default_branch,
    }
    require(
        data.get("pins") == expected
        and after["delivery_contract"]["baseline_revision"] == expected["source_revision"]
        and after["delivery_contract"]["definition_revision"] == expected["definition_revision"]
        and after["delivery_contract"]["job_mutation_role_arn"] == data["reviewed_job_mutation_role_arn"],
        "receipt_pins_changed",
    )
    cycle = data.get("checkpoint_cycle")
    require(type(cycle) is int and 0 <= cycle <= node.attempts, "receipt_cycle_changed")
    require(await repair_history(session, node, cycle) == data.get("repair_history"), "receipt_history_changed")
    from .maintenance_squash_history import validate_squash_receipts

    validate_squash_receipts(proof, data["repair_history"], release.repository)
    return data


async def prior_contract_for_adoption(session, node, current, previous_plan):
    from .operations_release_retry_contract import settled_retry_contract

    if previous_plan is None:
        return None
    # Check the complete chain even when an intermediate checkpoint is enough
    # to identify the last attempt. Never reinterpret an intervening run.
    for contract, data in await _verified_chain(session, node, current):
        if previous_plan.version > data["plan_version"] or node.attempts != data["checkpoint_cycle"]:
            return None
        previous = settled_retry_contract(_before_contract(contract, data), previous_plan)
        if previous is not None:
            return previous
    return None


async def resolve_adopted_release(session, node, contract, plan, *, installation_id, repository_id, provider):
    from types import SimpleNamespace

    from .deployment_manifest import WorkflowRef
    from .operations_release_resolution import _retry_release

    data = await verified_adoption(session, node, contract)
    require(data is not None, "receipt_missing")
    proof, release = data["proof"], contract.node.operational_release_binding
    require(proof["installation_id"] == installation_id and proof["repository_id"] == repository_id, "repository_changed")
    if node.attempts:
        await _retry_release(session, node, contract, plan, installation_id=installation_id, repository_id=repository_id)
    binding = SimpleNamespace(org_id=node.org_id, repo=release.repository, installation_id=installation_id, provider_repository_id=repository_id)
    workflow = WorkflowRef(path=proof["workflow_path"], definition_revision=data["pins"]["definition_revision"])
    checked = await provider.definition(binding, workflow, data["pins"]["source_revision"], generic_delivery=True)
    require(
        checked.approved_revision == data["pins"]["definition_revision"]
        and checked.source_revision == data["pins"]["source_revision"]
        and checked.dispatch_ref == release.default_branch,
        "workflow_changed",
    )
    if checked.dispatch_revision != data["pins"]["source_revision"]:
        await provider.contains(binding, data["pins"]["source_revision"], checked.dispatch_revision)
    delivery = release.delivery_contract
    evidence = {
        "target": release.target,
        "connection_id": delivery.connection_id,
        "resource_kind": delivery.resource_kind,
        "resource_id": delivery.resource_id,
        "manifest_artifact_revision": delivery.baseline_revision,
        "manifest_definition_revision": delivery.definition_revision,
        "workflow": proof["workflow_path"],
        "component": release.component,
        "repository": release.repository,
        "accepted_plan_version": plan.version,
        "accepted_plan_hash": plan.plan_hash,
        "delivery_contract_digest": delivery.digest,
        "receipts": data["receipts"],
        "maintenance_adoption_id": release.maintenance_adoption_id,
        "maintenance_adoption_digest": digest(data),
    }
    return dict(data["pins"]), evidence
