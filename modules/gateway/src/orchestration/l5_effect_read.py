"""Human forensic read of an attempted effect after its collection window ends."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select

from src.l5_observation_effects import receipt_id
from src.products.models import Product
from src.shared.identity.product_scope import product_member, product_visibility_scope

from .l5_effect_export import historical_effect
from .l5_observation_contract import PREPARED
from .l5_observation_export import ExportSelection
from .l5_observation_scope import require
from .models import OrchestrationAction, OrchestrationDecision


async def read_effect(db, *, flow_id, challenge_id, human):
    parent = await db.get(OrchestrationDecision, challenge_id)
    require(parent is not None and parent.kind == PREPARED and parent.org_id == human.tenant_id and parent.flow_id == flow_id, "not_found", 404)
    data = json.loads(parent.reason)
    request, selection = data["request"], data["snapshot"]["selection"]
    require(human.user_id in {parent.actor_id, request["observer_id"], selection["owner_id"]}, "not_found", 404)
    member = await product_member(db, user_id=human.user_id, org_id=human.tenant_id)
    require(
        member is not None
        and await db.scalar(select(Product.id).where(Product.id == selection["product_id"], product_visibility_scope(member, human.tenant_id)))
        is not None,
        "not_found",
        404,
    )
    before = await db.get(OrchestrationDecision, receipt_id(parent.id, "before"))
    require(before is not None, "l5_effect_not_recorded", 404)
    payload = json.loads(before.reason)
    result = selection.get("result") or {}
    actual = {key: selection[key] for key in ("product_id", "project_id", "service_id", "binding_id", "configuration_id", "configuration_revision")}
    actual.update(
        check_id=request["check_id"],
        subject=request["subject"],
        result_id=result.get("result_id"),
        result_revision=result.get("revision"),
        result_run_id=result.get("run_id"),
    )
    expected = SimpleNamespace(
        snapshot_sha256=data["snapshot_sha256"],
        selection=ExportSelection.model_validate(actual),
        operation=payload["operation"],
        request_sha256=payload["request_sha256"],
    )
    dispatch = await db.get(OrchestrationAction, request["workflow_action_id"])
    require(dispatch is not None, "l5_effect_unverifiable")
    return await historical_effect(db, parent=parent, expected=expected, dispatch=dispatch, now=datetime.now(UTC))
