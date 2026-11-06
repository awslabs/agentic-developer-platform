"""Explicit branch consent and exact committed-occurrence child source authority."""

import re
from copy import deepcopy

from fastapi import HTTPException
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.run_input import RecordRef, TransformGoal

from src.l5_service_identity import worker_id

BRANCHES = "transform_schedule_branches"
OCCURRENCE = "transform_source_occurrence"


def branch_selections(settings, tenant_id, project_id):
    """Owner preview includes each complete selection; absence never grants branches."""
    selections = []
    for trigger in settings.get("triggers", []):
        if trigger.get("kind") != "schedule" or trigger.get("enabled") is not True or trigger.get("source_policy") != "branch":
            continue
        try:
            branch = RecordRef.model_validate(trigger["branch_selector"]).model_dump()
            source = RecordRef.model_validate(trigger["source_refs"][0]).model_dump()
            goal = TransformGoal.model_validate(trigger["goal"]).model_dump(exclude_none=True)
            name = branch["record_revision"]
            if (
                len(trigger["source_refs"]) != 1
                or source not in settings["source_refs"]
                or source["record_type"] != "repository_commit"
                or branch["record_type"] != "repository_branch"
                or source["tenant_id"] != tenant_id
                or source["project_id"] != project_id
                or any(branch[key] != source[key] for key in ("tenant_id", "project_id", "source_connection_id", "record_id"))
                or not re.fullmatch(r"[!-~]{1,255}", name)
                or re.search(r"[~^:?*\[\\]", name)
                or any(value in name for value in ("..", "@{", "//"))
                or name.startswith(("/", "."))
                or name.endswith(("/", ".", ".lock"))
                or any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))
                or name.lower() in {"head", "latest", "null"}
                or name == "@"
                or goal["kind"] != trigger["capability"]
                or trigger["capability"] not in settings["capabilities"]
                or not isinstance(trigger["binding_id"], str)
                or not trigger["binding_id"]
                or not isinstance(trigger["trigger_id"], str)
                or not trigger["trigger_id"]
            ):
                raise ValueError("invalid branch selection")
            selections.append(
                {
                    key: deepcopy(trigger[key])
                    for key in ("trigger_id", "binding_id", "capability", "schedule", "goal", "branch_selector", "source_refs")
                }
            )
        except (ValueError, TypeError, KeyError, IndexError):
            raise HTTPException(422, detail={"code": "branch_schedule_scope_invalid"}) from None
    if len({entry["trigger_id"] for entry in selections}) != len(selections):
        raise HTTPException(422, detail={"code": "branch_schedule_scope_invalid"})
    return sorted(selections, key=lambda item: item["trigger_id"])


def consent_for_trigger(scope, trigger):
    expected = {key: trigger.get(key) for key in ("trigger_id", "binding_id", "capability", "schedule", "goal", "branch_selector", "source_refs")}
    return isinstance(scope, dict) and expected in scope.get(BRANCHES, [])


def parent_scope(scope):
    return {key: value for key, value in scope.items() if key != OCCURRENCE} if isinstance(scope, dict) else scope


def occurrence_source(grant):
    """Pure projection only; callers also require current_execution_scope."""
    scope = grant.execution_scope
    proof = scope.get(OCCURRENCE) if isinstance(scope, dict) else None
    if (
        grant.service_id != worker_id("transform")
        or grant.parent_delegation_id is None
        or not isinstance(proof, dict)
        or set(proof)
        != {"event_id", "trigger_id", "idempotency_key", "parent_delegation_id", "source_ref", "source_binding_id", "source_binding_generation"}
        or proof.get("parent_delegation_id") != grant.parent_delegation_id
    ):
        return None
    source = proof.get("source_ref")
    try:
        selected = RecordRef.model_validate(source).model_dump()
    except (ValueError, TypeError):
        return None
    if (
        selected != source
        or source["record_type"] != "repository_commit"
        or source["tenant_id"] != grant.tenant_id
        or source["project_id"] != grant.project_id
        or not re.fullmatch(r"[a-f0-9]{40}", source["record_revision"])
        or source not in grant.sources
        or len(grant.sources) != 2
        or not any(
            entry.get("trigger_id") == proof.get("trigger_id")
            and all(
                entry.get("branch_selector", {}).get(key) == source.get(key)
                for key in ("tenant_id", "project_id", "source_connection_id", "record_id")
            )
            for entry in scope.get(BRANCHES, [])
        )
    ):
        return None
    return source


def occurrence_binding(grant, binding):
    source = occurrence_source(grant)
    proof = (grant.execution_scope or {}).get(OCCURRENCE, {})
    return bool(
        source
        and binding.kind == "repository"
        and binding.provider == "github"
        and binding.id == proof.get("source_binding_id")
        and binding.generation == proof.get("source_binding_generation")
        and binding.tenant_id == grant.tenant_id
        and binding.project_id == grant.project_id
        and binding.installation_id == source["source_connection_id"]
        and binding.native_id == source["record_id"]
    )


async def current_occurrence(db, grant):
    source = occurrence_source(grant)
    if source is None:
        return False
    scope, proof = grant.execution_scope, grant.execution_scope[OCCURRENCE]
    receipt = await db.get(
        EventReceipt,
        (grant.tenant_id, grant.product_id, grant.project_id, "transform", proof["trigger_id"], proof["event_id"]),
        populate_existing=True,
    )
    return bool(
        receipt is not None
        and receipt.state in {"pending", "admitted"}
        and receipt.event_kind == "schedule"
        and receipt.configuration_id == scope["configuration_id"]
        and receipt.configuration_revision == scope["configuration_revision"]
        and receipt.idempotency_key == proof["idempotency_key"] == semantic_key(receipt)
        and (receipt.target or {}).get(OCCURRENCE) == proof
        and receipt.target.get("pinned_source") == source
        and receipt.target.get("binding_generation") == grant.binding_generation
        and consent_for_trigger(scope, receipt.target)
        and (receipt.run_input or {}).get("sources") == [source]
        and receipt.run_input.get("capability") in grant.capabilities
        and receipt.run_input.get("transform_input", {}).get("goal") == receipt.target.get("goal")
    )
