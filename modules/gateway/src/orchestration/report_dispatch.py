"""Replay the existing dispatch outbox from durable reporting assignments."""

from __future__ import annotations

import json
import os

from sqlalchemy import JSON, or_, select

from src.shared.models.base import utcnow

from .models import DecisionKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from .run_reports import OrchestrationRunReport, RunReportError, prepare_run_report, run_result_for_assignment


def reporting_enabled() -> bool:
    """Explicit rollout switch; a secret's presence never activates new workers."""
    return os.environ.get("ADP_SHARED_RUN_REPORTING_ENABLED", "false").lower() == "true"


async def validate_report_start(session, assignment) -> None:
    """Current flow/policy/claim fences apply to initial starts and outbox replay."""
    flow = await session.get(OrchestrationFlow, assignment.flow_id)
    if flow is None or flow.org_id != assignment.org_id or flow.state not in {"pending", "running"}:
        raise RunReportError("report_flow_inactive")
    from .policy_admission import load_in_force_policy

    inputs = await load_in_force_policy(session, org_id=assignment.org_id, flow_id=assignment.flow_id)
    if inputs.policy is not None or inputs.refusal is not None:
        from .review_cycle import CycleBlockedError
        from .shared_policy import authorize_shared_model

        try:
            await authorize_shared_model(session, assignment)
        except CycleBlockedError as exc:
            raise RunReportError(exc.reason) from None


async def recover_pending_reports(session, *, config, report) -> None:
    """Use the same FIFO publisher after an SQL-commit/SQS-publication crash.

    Reporting metadata is the saved envelope without its credential. Rebuilding
    only this delivery never increments the story attempt or launches a new run.
    SQS deduplication plus the worker's atomic start receipt handles lost replies.
    """
    if not reporting_enabled() or not config.configured or os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true":
        return
    from .dispatch_pass import PendingPublish, attempt_run_id, message_deduplication_id, message_group_id
    from .genesis import resolve_engine_genesis

    pending_ids = {pending.envelope["message_id"] for pending in report.pending}
    room = max(0, config.max_dispatches_per_tick - len(report.pending))
    if not room:
        return
    rows = (
        await session.scalars(
            select(OrchestrationRunReport)
            .where(
                OrchestrationRunReport.run_id.like("orch:%"),
                OrchestrationRunReport.expires_at > utcnow(),
                or_(OrchestrationRunReport.worker_receipt.is_(None), OrchestrationRunReport.worker_receipt == JSON.NULL),
                or_(OrchestrationRunReport.terminal_receipt.is_(None), OrchestrationRunReport.terminal_receipt == JSON.NULL),
            )
            .order_by(OrchestrationRunReport.created_at)
            .limit(max(100, room))
        )
    ).all()
    for row in rows:
        if row.run_id in pending_ids:
            continue
        node = await session.get(OrchestrationNode, row.node_id)
        if (
            node is None
            or node.state != "running"
            or row.org_id != node.org_id
            or row.flow_id != node.flow_id
            or row.attempt != node.attempts
            or row.run_id != attempt_run_id(node.id, node.attempts)
        ):
            continue
        decision = (
            await session.scalars(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.org_id == row.org_id,
                    OrchestrationDecision.node_id == row.node_id,
                    OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value,
                )
                .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
                .limit(1)
            )
        ).first()
        if decision is None:
            continue
        dispatch = json.loads(decision.reason or "{}")
        # Validate the immutable dispatch address; no advisory ingress row grants
        # permission to republish a story or supplies the envelope contents.
        await run_result_for_assignment(session, node=node, dispatch=dispatch)
        envelope = {key: value for key, value in row.dispatch_metadata.items() if key != "report_nonce"}
        root = envelope["orchestration"]["root_decision_id"]
        genesis = await resolve_engine_genesis(session, org_id=row.org_id, decision_id=root)
        if genesis.flow_id != row.flow_id:
            continue
        try:
            await validate_report_start(session, row)
            await prepare_run_report(session, envelope)
        except RunReportError as exc:
            row.block_code, row.retryable = exc.code, exc.retryable
            report.record(row.org_id, "publish_failed")
            continue
        if row.block_code == "execution_assignment_unverifiable":
            row.block_code, row.retryable = None, False
        report.pending.append(
            PendingPublish(
                node_id=row.node_id,
                org_id=row.org_id,
                node_attempt=row.attempt,
                node_kind=node.kind,
                envelope=envelope,
                genesis=genesis,
                group_id=message_group_id(org_id=row.org_id, node_id=row.node_id),
                deduplication_id=message_deduplication_id(node_id=row.node_id, decision_id=root, attempt=row.attempt),
            )
        )
        pending_ids.add(row.run_id)
        if len(report.pending) >= config.max_dispatches_per_tick:
            break
