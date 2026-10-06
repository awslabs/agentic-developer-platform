"""Revalidate original bootstrap ownership before each new cleanup access effect."""

import asyncio
import json
from dataclasses import dataclass

from harness_jobs.allocation import allocation_id_for, sealed_revision
from harness_jobs.identity import decode_payload, encode_payload, payload_digest
from harness_jobs.store import OperationStore

from .artifacts import canonical, read_artifact
from .authority import load_policy
from .effects import LifecycleEffects
from .retirement_access_authority import access_request, validate_access_request
from .retirement_access_plan import PHASE, compile_access_plan
from .retirement_inventory import load_bootstrap_retirement_review
from .runtime_config import LifecycleRefused


async def current_access_operation(operation, context):
    current = await context.authority.resolve(operation.grant.lease.operation_id)
    if (
        current.request != operation.request
        or current.job_id != operation.job_id
        or current.plan_digest != operation.plan_digest
        or current.request_payload != encode_payload(current.request)
        or current.plan_digest != payload_digest(current.request)
        or current.reservation_state != "confirmed"
        or any(
            getattr(current.grant.lease, key) != getattr(operation.grant.lease, key)
            for key in (
                "operation_id",
                "org_id",
                "workspace_id",
                "holder",
                "attempt_id",
                "fence_token",
            )
        )
    ):
        raise LifecycleRefused("cleanup access execution authority changed")
    validate_access_request(current, context)
    if getattr(context, "policy_fixture", None) is not True:
        await context.authority.preflight(current)
    return current


@dataclass(frozen=True)
class AccessContext:
    operation: object
    source: object
    artifact: dict
    inventory: object
    plan: object
    config: dict


async def require_original_seal(connection, source, parameters):
    original_allocation_id = allocation_id_for(source)
    if original_allocation_id != parameters["original_allocation_id"]:
        raise LifecycleRefused("cleanup access changed the original allocation")
    revision = await sealed_revision(
        connection,
        org_id=source.org_id,
        workspace_id=source.workspace_id,
        allocation_id=original_allocation_id,
    )
    if revision is None or revision == "quarantined":
        raise LifecycleRefused(
            "cleanup access requires the original allocation to be sealed"
        )


async def load_access_context(operation, context, registration_store, *, current=True):
    """With current=False, perform SQL/policy validation only for recovery selection.

    The recovery form returns facts; it never grants provider or execution access.
    Normal effects always re-resolve the current separately approved control grant.
    """
    if current:
        operation = await current_access_operation(operation, context)
    config = validate_access_request(operation, context)
    lease, parameters = operation.grant.lease, operation.request.parameters
    async with context.connect() as connection:
        source = await OperationStore().get(
            connection,
            operation.grant.principal,
            parameters["retirement_source_operation_id"],
        )
    if (
        source is None
        or any(
            getattr(source, field) != parameters["retirement_source_" + field]
            for field in (
                "operation_id",
                "job_id",
                "attempt_id",
            )
        )
        or source.plan_digest != parameters["retirement_source_payload_digest"]
    ):
        raise LifecycleRefused("cleanup access original bootstrap admission changed")
    original = decode_payload(source.request_payload)
    artifact = await read_artifact(
        context.domain_connect,
        artifact_id=parameters["lifecycle_artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    if original.parameters.get("lifecycle_artifact_id") != artifact["artifact_id"]:
        raise LifecycleRefused("cleanup access changed the original workspace artifact")
    async with context.connect() as connection:
        producer = await OperationStore().get(
            connection, operation.grant.principal, artifact["source_operation_id"]
        )
    if (
        producer is None
        or producer.state != "succeeded"
        or original.parameters.get("lifecycle_source_operation_id")
        != producer.operation_id
        or producer.job_id != artifact["source_job_id"]
        or producer.attempt_id != artifact["source_attempt_id"]
        or producer.plan_digest != artifact["source_payload_digest"]
        or producer.request_payload != artifact["source_request_payload"]
    ):
        raise LifecycleRefused("cleanup access historical artifact producer changed")
    async with context.domain_connect() as connection:
        workspace = await connection.fetchrow(
            "SELECT w.provisioning_operation_id,w.status FROM workspaces w JOIN organizations o ON o.id=w.org_id "
            "WHERE w.id::text=$1 AND (o.id::text=$2 OR o.adp_org_id=$2) AND w.is_default=false",
            lease.workspace_id,
            lease.org_id,
        )
    if (
        workspace is None
        or workspace["provisioning_operation_id"] != source.operation_id
        or workspace["status"] not in {"Active", "active"}
    ):
        raise LifecycleRefused(
            "cleanup access source is no longer the active registered bootstrap"
        )
    inventory = await asyncio.to_thread(
        load_bootstrap_retirement_review,
        registration_store=registration_store,
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
    )
    managed = inventory.cluster_ownership == "adp-created"
    if managed:
        from .retirement_managed_access import (
            compile_managed_access_review,
            managed_recipe_inputs,
            require_managed_paid_plan,
        )

        metadata = json.loads(artifact["artifact_metadata_json"])
        if (
            metadata.get("allocation_source_operation_id") != producer.operation_id
            or metadata.get("next_phase") != "bootstrap-workspace"
        ):
            raise LifecycleRefused("managed cleanup lost its paid apply lineage")
        plan = compile_managed_access_review(
            inventory,
            config,
            original_allocation_id=parameters["original_allocation_id"],
            bootstrap_artifact_id=artifact["artifact_id"],
            retirement_request_id=parameters["retirement_request_id"],
            **managed_recipe_inputs(inventory, config),
        )
        async with context.connect() as connection:
            await require_managed_paid_plan(connection, producer, plan)
    else:
        plan = compile_access_plan(
            inventory,
            config,
            original_allocation_id=parameters["original_allocation_id"],
            retirement_request_id=parameters["retirement_request_id"],
        )
        async with context.connect() as connection:
            await require_original_seal(connection, source, parameters)
    expected = access_request(
        plan,
        source,
        load_policy(context, lease.org_id),
        allocation_source=producer if managed else None,
    )
    if (
        expected != operation.request
        or artifact["account_id"] != parameters["aws_account_id"]
    ):
        raise LifecycleRefused(
            "cleanup access differs from current immutable ownership and recipe"
        )
    return AccessContext(operation, source, artifact, inventory, plan, config)


class RetirementAccessEffects(LifecycleEffects):
    """Use the durable finite-effect journal with a dedicated access authority."""

    def __init__(self, operation, context, *, registration_store, plan):
        self.registration_store = registration_store
        super().__init__(operation, context, phase=PHASE, recipe=plan.recipe())

    async def authority(self):
        facts = await load_access_context(
            self.operation, self.context, self.registration_store
        )
        if canonical(facts.plan.recipe()) != canonical(self.recipe):
            raise LifecycleRefused(
                "cleanup access recipe changed before a provider effect"
            )
        return facts.operation
