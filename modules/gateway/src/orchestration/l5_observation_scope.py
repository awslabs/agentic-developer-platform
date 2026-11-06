"""Rebuild an observation's accepted release and Product scope from server facts."""

import hashlib
import json
from datetime import UTC, datetime

from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.run import Run
from l5_services.release_identity import release_identity
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.products.models import Product, ProductProjectBinding, Project
from src.shared.identity.product_scope import product_member, product_visibility_scope
from src.shared.identity.workflow_access import is_workflow_member

from .accepted_node import contract_from_plan
from .dispatch_pass import attempt_run_id
from .l5_observation_contract import CHECKS
from .l5_observation_workflow import running_workflow
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .operations_release_resolution import operational_execution_pins
from .operations_repair import repair_revisions
from .policy_admission import load_in_force_policy
from .shared_window import accepted_document_hash, policy_owner_matches


class ObservationError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def require(value, code="l5_observation_changed", status=409):
    if not value:
        raise ObservationError(code, status)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC)


async def scope(db, *, flow_id, tenant_id, owner_id, request, now=None):
    """No caller-supplied source, runtime identity, actor claims or native facts."""
    now = now or datetime.now(UTC)
    flow = await db.scalar(
        select(OrchestrationFlow)
        .where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    require(flow is not None and flow.archived_at is None, "not_found", 404)
    require(flow.state in {"pending", "running"} and not flow.execution_paused, "l5_observation_execution_changed")
    plan = await db.scalar(
        select(OrchestrationAcceptedPlan)
        .where(
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.org_id == tenant_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    node = await db.scalar(
        select(OrchestrationNode)
        .where(OrchestrationNode.id == request.node_id, OrchestrationNode.flow_id == flow_id, OrchestrationNode.org_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    require(plan is not None and node is not None, "not_found", 404)
    try:
        document_hash = accepted_document_hash(plan)
    except (ValueError, TypeError, KeyError):
        raise ObservationError("l5_observation_plan_changed") from None
    require(
        (plan.version, plan.plan_hash, node.attempts) == (request.expected_plan_version, request.expected_plan_hash, request.expected_attempt)
        and plan.plan_hash == document_hash,
        "l5_observation_plan_changed",
    )
    admission = await load_in_force_policy(db, org_id=tenant_id, flow_id=flow_id)
    policy = admission.policy
    require(admission.refusal is None and policy is not None and policy.expires_at > now, "l5_observation_policy_unavailable")
    require(
        await policy_owner_matches(db, owner_id, policy) and await is_workflow_member(db, user_id=owner_id, org_id=tenant_id),
        "l5_observation_owner_revoked",
        403,
    )
    contract = contract_from_plan(flow, node, plan)
    release = contract.node.operational_release_binding if contract else None
    delivery = release.delivery_contract if release else None
    require(
        delivery is not None
        and release.component == "l5-" + request.service_id.replace("_", "-")
        and request.check_id in CHECKS
        and request.check_id in delivery.required_checks
        and release.workflows == ["l5-services-release.yml"],
        "l5_observation_check_not_accepted",
    )
    action = await db.scalar(
        select(OrchestrationAction)
        .where(OrchestrationAction.id == request.workflow_action_id, OrchestrationAction.org_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    require(
        action is not None
        and action.kind == "delivery_workflow_dispatch"
        and action.status == "succeeded"
        and action.attempt == 1
        and isinstance(action.detail, dict),
        "l5_observation_workflow_unverified",
    )
    execution = await db.get(OrchestrationExecution, action.execution_id, populate_existing=True)
    require(
        execution is not None
        and execution.org_id == tenant_id
        and execution.flow_id == flow_id
        and execution.node_id == node.id
        and execution.cycle == node.attempts
        and execution.status in {"runnable", "awaiting_external"}
        and execution.accepted_plan_version == plan.version
        and node.state == "running",
        "l5_observation_execution_changed",
    )
    claim = await db.get(OrchestrationWorkClaim, execution.claim_id, populate_existing=True)
    require(
        claim is not None
        and claim.org_id == tenant_id
        and claim.state == "held"
        and claim.generation == execution.claim_generation
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == flow_id
        and claim.active_run_id is not None
        and claim.lease_expires_at is not None
        and aware(claim.lease_expires_at) > now,
        "l5_observation_claim_changed",
    )
    pins = await operational_execution_pins(db, node=node, execution=execution, contract=contract, run_id=attempt_run_id(node.id, node.attempts))
    require(pins is not None, "l5_observation_source_unverified")
    pins = await repair_revisions(db, node_id=node.id, org_id=tenant_id, plan_version=plan.version, cycle=node.attempts, pins=pins, contract=contract)
    require(pins is not None, "l5_observation_source_unverified")
    workflow = await running_workflow(db, action=action, execution=execution, node=node, claim=claim, contract=contract, pins=pins)
    selected = await product_scope(db, tenant_id=tenant_id, owner_id=owner_id, request=request)
    try:
        installed = await run_in_threadpool(release_identity)
    except Exception:
        raise ObservationError("l5_observation_runtime_unavailable", 503) from None
    # Full Gateway/frontend/worker digest and native verification remain the
    # release driver's responsibility. This snapshot is only the local package.
    snapshot = {
        "release": workflow,
        "plan_hash": plan.plan_hash,
        "delivery_contract_digest": delivery.digest,
        "correlation": attempt_run_id(node.id, node.attempts),
        "policy_hash": policy.policy_hash,
        "active_run_id": claim.active_run_id,
        "selection": selected,
        "local_package": installed,
    }
    return node, snapshot, min(policy.expires_at, aware(claim.lease_expires_at))


async def product_scope(db, *, tenant_id, owner_id, request):
    member = await product_member(db, user_id=owner_id, org_id=tenant_id)
    product = await db.scalar(
        select(Product).where(Product.id == request.product_id, product_visibility_scope(member, tenant_id)).execution_options(populate_existing=True)
    )
    project = await db.get(Project, (tenant_id, request.project_id), populate_existing=True)
    relationship = await db.scalar(
        select(ProductProjectBinding)
        .where(
            ProductProjectBinding.tenant_id == tenant_id,
            ProductProjectBinding.product_id == request.product_id,
            ProductProjectBinding.project_id == request.project_id,
            ProductProjectBinding.valid_to.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    config = await db.scalar(
        select(Configuration)
        .where(
            Configuration.tenant_id == tenant_id,
            Configuration.product_id == request.product_id,
            Configuration.project_id == request.project_id,
            Configuration.service_id == request.service_id,
        )
        .execution_options(populate_existing=True)
    )
    binding = await db.get(Binding, (tenant_id, request.binding_id), populate_existing=True)
    require(
        product is not None
        and project is not None
        and relationship is not None
        and config is not None
        and product.owner_user_id == config.owner_id
        and product.archived_at is None
        and project.archived_at is None
        and config.handoff_state == "linked"
        and config.relationship_revision == str(relationship.relationship_revision)
        and binding is not None
        and binding.configuration_id == config.configuration_id
        and (binding.product_id, binding.project_id, binding.service_id) == (request.product_id, request.project_id, request.service_id),
        "not_found",
        404,
    )
    version = await db.get(ConfigurationVersion, (tenant_id, config.configuration_id, config.current_revision), populate_existing=True)
    require(version is not None, "not_found", 404)
    actual_owner = await product_member(db, user_id=product.owner_user_id, org_id=tenant_id)
    require(
        actual_owner is not None
        and await is_workflow_member(db, user_id=actual_owner.id, org_id=tenant_id)
        and await db.scalar(select(Product.id).where(Product.id == product.id, product_visibility_scope(actual_owner, tenant_id))) is not None,
        "l5_observation_product_owner_revoked",
        403,
    )
    observer = await product_member(db, user_id=request.observer_id, org_id=tenant_id)
    require(
        observer is not None and observer.id == request.observer_id and await is_workflow_member(db, user_id=observer.id, org_id=tenant_id),
        "l5_observation_observer_revoked",
        403,
    )
    # A selected negative-access observer need not have target access. The
    # owner authorizes only this challenge; no Product data is returned to them.
    if request.subject != "native_denial":
        visible = await db.scalar(select(Product.id).where(Product.id == product.id, product_visibility_scope(observer, tenant_id)))
        require(visible is not None, "not_found", 404)
    selected = {
        "tenant_id": tenant_id,
        "product_id": product.id,
        "project_id": project.id,
        "service_id": request.service_id,
        "owner_id": actual_owner.id,
        "configuration_id": config.configuration_id,
        "configuration_revision": config.current_revision,
        "configuration_sha256": version.hash,
        "relationship_revision": config.relationship_revision,
        "binding_id": binding.binding_id,
        "binding_generation": binding.generation,
        "binding_revision": binding.revision,
        "binding_sha256": digest(
            {
                "scope": binding.scope,
                "connection": binding.connection_ref,
                "native": binding.native_resource_ref,
                "provider": binding.provider_identity,
                "state": binding.state,
            }
        ),
    }
    if request.result_id is not None:
        result = await db.get(AssessmentResult, (tenant_id, request.result_id), populate_existing=True)
        run = await db.get(Run, (tenant_id, result.run_id), populate_existing=True) if result else None
        require(
            run is not None
            and run.configuration_id == config.configuration_id
            and run.configuration_revision == config.current_revision
            and run.binding_id == binding.binding_id
            and run.binding_generation == binding.generation
            and (run.product_id, run.project_id, run.service_id) == (product.id, project.id, request.service_id),
            "not_found",
            404,
        )
        selected["result"] = {
            "result_id": result.result_id,
            "revision": result.version,
            "run_id": run.run_id,
            "payload_sha256": digest(result.payload),
            "native_sha256": digest(run.native_job_ref),
        }
    if request.subject.startswith("native_"):
        native_id = (binding.native_resource_ref or {}).get("record_id")
        import re

        require(isinstance(native_id, str) and re.fullmatch(r"[A-Za-z0-9-]{3,128}", native_id), "l5_observation_native_scope_unavailable")
        selected["native_hostname"] = native_id + ".aidevops.global.app.aws"
    return selected
