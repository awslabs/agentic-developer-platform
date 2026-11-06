"""Discover same-run human/effect receipts without pre-pinning future IDs."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from src.l5_observation_effects import receipt_id
from src.products.models import Product
from src.shared.identity.product_scope import product_member, product_visibility_scope
from src.shared.identity.workflow_access import is_workflow_member

from .accepted_node import accepted_node_contract
from .l5_effect_export import historical_effect
from .l5_observation_contract import CHECKS, OBSERVED, PREPARED
from .l5_observation_export import (
    ExportSelection,
    authorized_read,
    current_content_authority,
    historical_challenge,
    historical_observation,
)
from .l5_observation_scope import require
from .models import OrchestrationDecision, OrchestrationExecution, OrchestrationNode
from .operations_release_job import NativeJobRequest

CONTRACT = "l5-observation-discovery/v1"


class DiscoverObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job: NativeJobRequest
    selection: ExportSelection
    preparer_id: str = Field(min_length=1, max_length=255)
    observer_id: str = Field(min_length=1, max_length=255)
    evidence: Literal["human", "effect"]
    operation: Literal["save", "check", "replay", "submit"] | None = None
    request_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact_effect(self):
        if self.selection.check_id not in CHECKS:
            raise ValueError("Select an original accepted UI check")
        if self.evidence == "effect":
            if self.operation is None or self.request_sha256 is None or self.selection.check_id != "ui-cli-mcp-parity":
                raise ValueError("Effect discovery requires the exact operation and request digest")
        elif self.operation is not None or self.request_sha256 is not None:
            raise ValueError("Human discovery does not accept effect claims")
        return self


async def discover(service, body, claims):
    async def read(db, action):
        execution = await db.get(OrchestrationExecution, action.execution_id)
        node = await db.get(OrchestrationNode, execution.node_id)
        contract = await accepted_node_contract(db, node)
        release = contract.node.operational_release_binding if contract else None
        require(
            release is not None
            and release.component == "l5-" + body.selection.service_id.replace("_", "-")
            and release.delivery_contract is not None
            and body.selection.check_id in release.delivery_contract.required_checks,
            "l5_observation_check_not_accepted",
        )
        product = await db.get(Product, (action.org_id, body.selection.product_id))
        require(product is not None and product.tenant_id == action.org_id, "not_found", 404)
        owner = SimpleNamespace(org_id=action.org_id, flow_id=execution.flow_id, actor_id=body.preparer_id)
        await current_content_authority(db, parent=owner, selection={**body.selection.model_dump(), "owner_id": product.owner_user_id})
        observer = await product_member(db, user_id=body.observer_id, org_id=action.org_id)
        require(observer is not None and await is_workflow_member(db, user_id=observer.id, org_id=action.org_id), "not_found", 404)
        if body.selection.subject != "native_denial":
            require(
                await db.scalar(select(Product.id).where(Product.id == product.id, product_visibility_scope(observer, action.org_id))) is not None,
                "not_found",
                404,
            )
        candidates = list(
            await db.scalars(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.org_id == action.org_id,
                    OrchestrationDecision.node_id == node.id,
                    OrchestrationDecision.kind == PREPARED,
                    OrchestrationDecision.actor_id == body.preparer_id,
                )
                .limit(129)
            )
        )
        require(len(candidates) <= 128, "l5_observation_limit_reached")
        ready, pending = [], []
        now = datetime.now(UTC)
        for parent in candidates:
            data = json.loads(parent.reason)
            request, snapshot = data.get("request", {}), data.get("snapshot", {})
            selected = snapshot.get("selection", {})
            historical_release = snapshot.get("release", {})
            if (
                historical_release.get("dispatch_action_id") != action.id
                or historical_release.get("run_id") != int(claims["run_id"])
                or historical_release.get("run_attempt") != int(claims["run_attempt"])
                or request.get("observer_id") != body.observer_id
            ):
                continue
            actual = {
                key: selected.get(key)
                for key in ("product_id", "project_id", "service_id", "binding_id", "configuration_id", "configuration_revision")
            }
            result = selected.get("result") or {}
            actual.update(
                check_id=request.get("check_id"),
                subject=request.get("subject"),
                result_id=result.get("result_id"),
                result_revision=result.get("revision"),
                result_run_id=result.get("run_id"),
            )
            if actual != body.selection.model_dump():
                continue
            expected = SimpleNamespace(
                challenge_id=parent.id,
                snapshot_sha256=data.get("snapshot_sha256"),
                selection=body.selection,
                operation=body.operation,
                request_sha256=body.request_sha256,
            )
            await historical_challenge(db, parent=parent, expected=expected, dispatch=action, now=now)
            if body.evidence == "human":
                from uuid import NAMESPACE_URL, uuid5

                from .l5_observation_contract import CONTRACT as HUMAN_CONTRACT

                recorded = await db.get(OrchestrationDecision, str(uuid5(NAMESPACE_URL, HUMAN_CONTRACT + ":observation:" + parent.id)))
                if recorded is None:
                    if now < datetime.fromisoformat(data["expires_at"]):
                        pending.append(parent.id)
                    continue
                require(recorded.kind == OBSERVED, "l5_observation_unverifiable")
                await historical_observation(db, parent=parent, expected=expected, dispatch=action, now=now)
            else:
                before = await db.get(OrchestrationDecision, receipt_id(parent.id, "before"))
                if before is None:
                    if now < datetime.fromisoformat(data["expires_at"]):
                        pending.append(parent.id)
                    continue
                attempt = json.loads(before.reason)
                if (attempt.get("operation"), attempt.get("request_sha256")) != (body.operation, body.request_sha256):
                    continue
                facts = await historical_effect(db, parent=parent, expected=expected, dispatch=action, now=now)
                # A before-only attempt is exposed as unknown, not kept waiting
                # forever after a worker crash. It cannot complete any check.
                require(facts["outcome"] in {"returned", "returned_error", "unknown"}, "l5_effect_unverifiable")
            ready.append(
                {
                    "challenge_id": parent.id,
                    "snapshot_sha256": data["snapshot_sha256"],
                    "selection": actual,
                    **({"operation": body.operation, "request_sha256": body.request_sha256} if body.evidence == "effect" else {}),
                }
            )
        require(len(ready) <= 1, "l5_observation_discovery_ambiguous")
        return {
            "contract": CONTRACT,
            "status": "ready" if ready else "pending",
            "selector": ready[0] if ready else None,
            "pending_challenge_ids": pending,
            "release_check_completed": False,
            "native_qualified": False,
            "next_action": "Collect this exact receipt through the protected export."
            if ready
            else "The selected human must prepare/endorse and complete this exact current-run observation. No prior-run evidence is reusable.",
        }

    return await authorized_read(service, body.job, claims, read)
