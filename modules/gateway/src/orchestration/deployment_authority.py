"""Engine-only continuation after code completion; completed workers stay fenced."""

from dataclasses import asdict

from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError

from .execution_state import ExecutionPhase, OutcomeKind
from .execution_store import load_execution
from .merge_controller import MERGE_KIND, MergeReceipt
from .models import OrchestrationAction, OrchestrationWorkClaim
from .pr_bindings import active_binding_for_node, binding_scope_matches

DELIVERY_PHASES = frozenset({ExecutionPhase.DEPLOYMENT_PENDING, ExecutionPhase.AWAITING_RUNTIME_VERIFICATION, ExecutionPhase.EVALUATION_PENDING})


async def load_delivery_merge(session, *, identity, node):
    loaded = await load_execution(session, identity=identity)
    if loaded is None or loaded.kind is not OutcomeKind.APPLIED or loaded.record is None or loaded.record.phase not in DELIVERY_PHASES:
        raise BootstrapRefusedError("delivery execution unavailable")
    if node.kind != "story" or node.state != "passed" or node.attempts != identity.cycle:
        raise BootstrapRefusedError("delivery requires verified code completion")
    binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
    if binding is None or not binding_scope_matches(binding, node):
        raise BootstrapRefusedError("delivery binding changed")
    rows = list(
        (
            await session.scalars(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == loaded.record.id,
                    OrchestrationAction.kind == MERGE_KIND,
                    OrchestrationAction.status == "succeeded",
                )
                .order_by(OrchestrationAction.created_at.desc())
                .limit(101)
            )
        ).all()
    )
    if len(rows) > 100:
        raise BootstrapRefusedError("delivery merge history exceeds limit")
    for row in rows:
        raw = (row.detail or {}).get("merge_receipt")
        if raw is None:
            continue
        receipt = MergeReceipt.model_validate(raw)
        if any(getattr(receipt, key) != value for key, value in asdict(identity).items()):
            raise BootstrapRefusedError("delivery merge authority changed")
        if (
            receipt.execution_id,
            receipt.flow_id,
            receipt.repo,
            receipt.pr_number,
            receipt.provider_repository_id,
            receipt.provider_pr_node_id,
            receipt.reviewed_head_sha,
        ) != (
            loaded.record.id,
            node.flow_id,
            binding.repo,
            binding.pr_number,
            binding.provider_repository_id,
            binding.provider_pr_node_id,
            binding.head_sha,
        ):
            raise BootstrapRefusedError("delivery merge binding changed")
        return loaded.record, binding, receipt
    raise BootstrapRefusedError("delivery merge receipt missing")


async def validate_delivery_continuation(session, *, identity, node, execution, grant):
    if identity.org_id != grant.tenant_id or identity.node_id != node.id or node.flow_id != grant.flow_id:
        raise BootstrapRefusedError("delivery grant binding changed")
    if execution.get("status", {}).get("S") != "completed":
        raise BootstrapRefusedError("delivery requires a terminal worker")
    await load_delivery_merge(session, identity=identity, node=node)
    claim = await session.scalar(
        select(OrchestrationWorkClaim)
        .where(OrchestrationWorkClaim.org_id == identity.org_id, OrchestrationWorkClaim.id == identity.claim_id)
        .execution_options(populate_existing=True)
    )
    if (
        claim is None
        or claim.state != "held"
        or claim.generation != identity.claim_generation
        or claim.active_run_id != execution.get("invocation_id", {}).get("S")
    ):
        raise BootstrapRefusedError("delivery active run changed")
