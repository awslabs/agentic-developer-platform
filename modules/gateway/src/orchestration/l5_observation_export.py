"""Export historical human facts to their own currently authorized release job."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from l5_services.models.configuration import Configuration
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from src.products.models import Product, ProductProjectBinding, Project
from src.shared.identity.product_scope import product_member, product_visibility_scope
from src.shared.identity.workflow_access import is_workflow_member

from .accepted_node import contract_from_plan
from .generic_delivery_job import snapshot as job_snapshot
from .l5_observation_contract import CONTRACT, OBSERVED, PREPARED, SERVICES, PrepareObservation, RecordObservation
from .l5_observation_receipts import launch_history, verify_launch
from .l5_observation_scope import ObservationError, aware, digest, require
from .l5_observations import append, owner_endorsement, validate_observed_fields
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .operations_release_job import NativeJobRequest
from .policy_admission import load_in_force_policy
from .shared_window import accepted_document_hash, policy_owner_matches

EXPORT_CONTRACT = "l5-human-observation-export/v1"
EXPORTED = "l5_observation_exported"


class ExportSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    product_id: str = Field(min_length=1, max_length=36)
    project_id: str = Field(min_length=1, max_length=255)
    service_id: SERVICES
    binding_id: str = Field(min_length=1, max_length=36)
    configuration_id: str = Field(min_length=1, max_length=36)
    configuration_revision: int = Field(gt=0)
    check_id: str = Field(min_length=1, max_length=64)
    subject: Literal["configuration", "result", "native_ui", "native_denial", "run_submission"]
    result_id: str | None = Field(default=None, min_length=1, max_length=36)
    result_revision: int | None = Field(default=None, gt=0)
    result_run_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def result_scope(self):
        values = (self.result_id, self.result_revision, self.result_run_id)
        if (self.subject == "result" and any(value is None for value in values)) or (
            self.subject != "result" and any(value is not None for value in values)
        ):
            raise ValueError("Select the exact result and run only for a result observation")
        return self


class ExportObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    job: NativeJobRequest
    challenge_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    selection: ExportSelection


def decision_reference(row):
    return {
        "decision_id": row.id,
        "sha256": digest(
            {
                "id": row.id,
                "org_id": row.org_id,
                "flow_id": row.flow_id,
                "node_id": row.node_id,
                "kind": row.kind,
                "actor_id": row.actor_id,
                "actor_kind": row.actor_kind,
                "actor_role": row.actor_role,
                "data": json.loads(row.reason),
            }
        ),
    }


async def current_content_authority(db, *, parent, selection):
    """Current content access is separate from historical evidence validity."""
    admission = await load_in_force_policy(db, org_id=parent.org_id, flow_id=parent.flow_id)
    require(
        admission.refusal is None and admission.policy is not None and admission.policy.expires_at > datetime.now(UTC),
        "l5_observation_policy_unavailable",
    )
    require(await policy_owner_matches(db, parent.actor_id, admission.policy), "l5_observation_owner_revoked", 403)
    members = []
    for user_id in {parent.actor_id, selection["owner_id"]}:
        member = await product_member(db, user_id=user_id, org_id=parent.org_id)
        require(member is not None and await is_workflow_member(db, user_id=user_id, org_id=parent.org_id), "not_found", 404)
        members.append(member)
    for member in members:
        product = await db.scalar(
            select(Product)
            .where(
                Product.id == selection["product_id"],
                product_visibility_scope(member, parent.org_id),
            )
            .execution_options(populate_existing=True)
        )
        require(product is not None and product.archived_at is None and product.owner_user_id == selection["owner_id"], "not_found", 404)
    project = await db.get(Project, (parent.org_id, selection["project_id"]), populate_existing=True)
    relationship = await db.scalar(
        select(ProductProjectBinding).where(
            ProductProjectBinding.tenant_id == parent.org_id,
            ProductProjectBinding.product_id == selection["product_id"],
            ProductProjectBinding.project_id == selection["project_id"],
            ProductProjectBinding.valid_to.is_(None),
        )
    )
    config = await db.get(Configuration, (parent.org_id, selection["configuration_id"]), populate_existing=True)
    require(
        project is not None
        and project.archived_at is None
        and relationship is not None
        and config is not None
        and config.owner_id == selection["owner_id"]
        and (config.product_id, config.project_id, config.service_id)
        == (
            selection["product_id"],
            selection["project_id"],
            selection["service_id"],
        ),
        "not_found",
        404,
    )


async def historical_challenge(db, *, parent, expected, dispatch, now):
    """Validate immutable collection-time facts; no old session is authenticated."""
    require(parent is not None and parent.kind == PREPARED and parent.actor_kind == "human", "not_found", 404)
    execution = await db.get(OrchestrationExecution, dispatch.execution_id)
    require(
        execution is not None
        and (parent.org_id, parent.flow_id, parent.node_id)
        == (
            execution.org_id,
            execution.flow_id,
            execution.node_id,
        ),
        "l5_observation_export_mismatch",
    )
    try:
        data = json.loads(parent.reason)
        request = PrepareObservation.model_validate(data["request"])
        snapshot = data["snapshot"]
        selected, release = snapshot["selection"], snapshot["release"]
        created, expired = (datetime.fromisoformat(data[key]) for key in ("created_at", "expires_at"))
        require(
            data["contract"] == CONTRACT
            and created.tzinfo is not None
            and expired.tzinfo is not None
            and created < expired <= created + timedelta(minutes=30)
            and created <= now
            and digest(snapshot) == data["snapshot_sha256"] == expected.snapshot_sha256
            and request.node_id == parent.node_id
            and release["provenance"] == "oidc-authorized-delivery-dispatch-and-provider-run"
            and release["dispatch_action_id"] == dispatch.id
            and release["execution_id"] == dispatch.execution_id
            and release["cycle"] == request.expected_attempt == execution.cycle
            and release["accepted_plan_version"] == request.expected_plan_version == execution.accepted_plan_version,
            "l5_observation_export_mismatch",
        )
        require(
            selected["tenant_id"] == parent.org_id
            and all(
                selected[key] == getattr(request, key)
                for key in (
                    "product_id",
                    "project_id",
                    "service_id",
                    "binding_id",
                )
            ),
            "l5_observation_export_mismatch",
        )
        actual = {
            key: selected[key]
            for key in (
                "product_id",
                "project_id",
                "service_id",
                "binding_id",
                "configuration_id",
                "configuration_revision",
            )
        }
        result = selected.get("result") or {}
        actual.update(
            check_id=request.check_id,
            subject=request.subject,
            result_id=result.get("result_id"),
            result_revision=result.get("revision"),
            result_run_id=result.get("run_id"),
        )
        require(actual == expected.selection.model_dump(), "l5_observation_export_mismatch")
        context = {
            key: dispatch.detail[key]
            for key in (
                "correlation",
                "contract_digest",
                "repo",
                "provider_repository_id",
                "claim_id",
                "claim_generation",
                "workflow",
                "source_revision",
                "definition_revision",
                "dispatch_revision",
                "branch",
            )
        }
        context.update(run_id=int(dispatch.detail["release_job"]["run_id"]), run_attempt=int(dispatch.detail["release_job"]["run_attempt"]))
        require(
            all(release[key] == value for key, value in context.items()) and release["dispatch_context_sha256"] == digest(context),
            "l5_observation_export_mismatch",
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ObservationError):
            raise
        raise ObservationError("l5_observation_unverifiable") from None
    plan = await db.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == parent.org_id,
            OrchestrationAcceptedPlan.flow_id == parent.flow_id,
            OrchestrationAcceptedPlan.version == request.expected_plan_version,
        )
    )
    flow = await db.get(OrchestrationFlow, parent.flow_id)
    node = await db.get(OrchestrationNode, parent.node_id)
    require(plan is not None and flow is not None and node is not None, "l5_observation_unverifiable")
    require(plan.plan_hash == request.expected_plan_hash == snapshot["plan_hash"] == accepted_document_hash(plan), "l5_observation_plan_changed")
    contract = contract_from_plan(flow, node, plan)
    accepted = contract.node.operational_release_binding if contract else None
    require(
        accepted is not None
        and accepted.component == "l5-" + request.service_id.replace("_", "-")
        and accepted.delivery_contract is not None
        and accepted.delivery_contract.digest == snapshot["delivery_contract_digest"] == release["contract_digest"]
        and request.check_id in accepted.delivery_contract.required_checks,
        "l5_observation_check_not_accepted",
    )
    return data, request, selected, release, actual, created, expired


async def historical_observation(db, *, parent, expected, dispatch, now):
    data, request, selected, release, actual, created, expired = await historical_challenge(
        db, parent=parent, expected=expected, dispatch=dispatch, now=now
    )
    observed = await db.get(OrchestrationDecision, str(uuid5(NAMESPACE_URL, CONTRACT + ":observation:" + parent.id)))
    require(
        observed is not None
        and observed.kind == OBSERVED
        and observed.actor_kind == "human"
        and observed.actor_id == request.observer_id
        and (observed.org_id, observed.flow_id, observed.node_id) == (parent.org_id, parent.flow_id, parent.node_id),
        "l5_observation_not_recorded",
    )
    try:
        payload = json.loads(observed.reason)
        observed_at = datetime.fromisoformat(payload["observed_at"])
        body = RecordObservation.model_validate(payload["observed"])
        require(
            created <= observed_at < expired
            and observed_at <= now
            and payload["contract"] == CONTRACT
            and payload["challenge_id"] == parent.id
            and payload["snapshot_sha256"] == body.snapshot_sha256 == data["snapshot_sha256"]
            and payload["provenance"] == "authenticated-human-observation"
            and payload["native_qualified"] is False
            and payload["release_check_completed"] is False,
            "l5_observation_unverifiable",
        )
        validate_observed_fields(request, selected, body)
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ObservationError):
            raise
        raise ObservationError("l5_observation_unverifiable") from None
    endorsement = await owner_endorsement(db, row=parent, data=data, now=observed_at)
    references = [decision_reference(parent), decision_reference(observed)]
    if endorsement is not None:
        references.append(decision_reference(await db.get(OrchestrationDecision, endorsement["receipt_id"])))
    if request.subject == "run_submission":
        from src.l5_observation_effects import receipt_id as effect_receipt_id
        from src.l5_observation_effects import verify_submission

        await verify_submission(db, parent=parent, data=data, body=body, now=observed_at)
        if body.observation == "submitted":
            for phase in ("before", "after"):
                references.append(decision_reference(await db.get(OrchestrationDecision, effect_receipt_id(parent.id, phase))))
    if request.subject.startswith("native_"):
        await verify_launch(db, parent=parent, data=data, body=body, now=observed_at)
        for launch in await launch_history(db, parent=parent, data=data, now=observed_at):
            if endorsement is not None:
                require(
                    datetime.fromisoformat(endorsement["endorsed_at"])
                    <= datetime.fromisoformat(launch["observed_at"])
                    < datetime.fromisoformat(endorsement["expires_at"]),
                    "l5_observation_owner_endorsement_unverified",
                )
            references.append(decision_reference(await db.get(OrchestrationDecision, launch["receipt_id"])))
    else:
        require(body.launch_receipt_id is None, "l5_observation_fields_mismatch")
    await current_content_authority(db, parent=parent, selection=selected)
    return {
        "challenge_id": parent.id,
        "snapshot_sha256": data["snapshot_sha256"],
        "release": release,
        "selection": actual,
        "actor_id": observed.actor_id,
        "observed_at": payload["observed_at"],
        "observed": body.model_dump(),
        "provenance": "authenticated-human-observation",
        "decision_references": references,
        "release_check_completed": False,
        "native_qualified": False,
    }


async def export_observation(service, body, claims):
    return await export_document(
        service,
        body,
        claims,
        collector=historical_observation,
        contract=EXPORT_CONTRACT,
        kind=EXPORTED,
        actor="system:l5-observation-export",
        field="observation",
    )


async def export_document(service, body, claims, *, collector, contract, kind, actor, field):
    async def read(db, action):
        parent = await db.get(OrchestrationDecision, body.challenge_id)
        require(parent is not None and parent.org_id == action.org_id, "not_found", 404)
        facts = await collector(db, parent=parent, expected=body, dispatch=action, now=datetime.now(UTC))
        value = {"contract": contract, field: facts, "release_check_completed": False, "native_qualified": False}
        identity = str(uuid5(NAMESPACE_URL, contract + ":" + digest(value)))
        row = await db.get(OrchestrationDecision, identity)
        if row is None:
            row = append(db, identity=parent, kind=kind, actor_id=actor, actor_kind="service", data=value, row_id=identity)
            await db.flush()
        else:
            require(
                row.kind == kind
                and row.actor_kind == "service"
                and row.actor_id == actor
                and (row.org_id, row.flow_id, row.node_id) == (parent.org_id, parent.flow_id, parent.node_id)
                and json.loads(row.reason) == value,
                "l5_observation_unverifiable",
            )
        result = {**value, "export_receipt": decision_reference(row)}
        return result

    return await authorized_read(service, body.job, claims, read)


async def authorized_read(service, job, claims, reader):
    require(job.contract_digest is not None, "l5_observation_export_mismatch")
    async with service.factory() as db:
        action, binding = await job_snapshot(service, db, job, claims)
        require(
            action.status == "succeeded"
            and action.detail.get("release_job") == {"run_id": claims.get("run_id"), "run_attempt": claims.get("run_attempt")},
            "l5_observation_workflow_unverified",
        )
        action_id = action.id
    started_at = await service.verify_run(claims, binding, job)
    async with service.factory() as db:
        action, _ = await job_snapshot(service, db, job, claims, expected_action=action_id, started_at=started_at)
        execution = await db.get(OrchestrationExecution, action.execution_id, populate_existing=True)
        claim = await db.get(OrchestrationWorkClaim, execution.claim_id, populate_existing=True) if execution is not None else None
        flow = await db.get(OrchestrationFlow, execution.flow_id, populate_existing=True) if execution is not None else None
        require(
            execution is not None
            and execution.status in {"runnable", "awaiting_external"}
            and flow is not None
            and flow.state in {"running", "pending"}
            and not flow.execution_paused
            and flow.archived_at is None,
            "l5_observation_execution_changed",
        )
        require(
            claim is not None
            and claim.state == "held"
            and claim.org_id == action.org_id
            and claim.owner_kind == "engine_flow"
            and claim.owner_ref == execution.flow_id
            and claim.generation == execution.claim_generation
            and claim.active_run_id == (execution.handoff_receipt_ref or job.correlation)
            and claim.lease_expires_at is not None
            and aware(claim.lease_expires_at) > datetime.now(UTC),
            "l5_observation_claim_changed",
        )
        result = await reader(db, action)
        await db.commit()
        return result
