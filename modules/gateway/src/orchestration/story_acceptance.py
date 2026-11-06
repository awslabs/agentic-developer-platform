"""Bounded, durable display of verified story acceptance; never grants authority.

Writers run inside the existing fenced settlement transaction. The original
receipts remain the audit evidence. One fixed ledger key per execution keeps
completed checks visible independently of the recent-action window.
"""

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .models import OrchestrationAction

KEY = "story-acceptance:v1"
KIND = "story_acceptance"


def is_acceptance_observation(action):
    """Recognize our display-only ledger row without trusting it as authority."""
    return (
        action.kind == KIND
        and action.operation_key == KEY
        and action.status == "succeeded"
        and action.artifact_ref is None
        and action.receipt_ref is None
    )


def scope_digest(scope):
    return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()


class AcceptanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    node_id: str
    cycle: int
    plan_version: int
    binding_id: str
    scope_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    head_sha: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    merge_sha: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    code_only: bool
    merge_action_id: str
    merged_at: datetime
    requirements_verified_at: datetime
    deployment_verified_at: datetime | None = None
    deployment_action_id: str | None = None
    docs_only: bool = False
    completed_at: datetime | None = None


class AcceptanceCheck(BaseModel):
    id: str
    label: str
    status: Literal["passed", "pending", "running", "waiting", "recovering", "blocked", "paused"]
    detail: str
    observed_at: str | None = None
    # Internal ledger ID, not an arbitrary provider URL or private artifact path.
    evidence_id: str | None = None


class StoryAcceptance(BaseModel):
    status: Literal["in_progress", "complete", "history_unavailable"]
    checks: list[AcceptanceCheck] = Field(default_factory=list)
    detail: str
    next_action: str | None = None
    next_check_at: str | None = None


async def _row(session, execution):
    return await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == execution.org_id,
            OrchestrationAction.execution_id == execution.id,
            OrchestrationAction.operation_key == KEY,
            OrchestrationAction.kind == KIND,
        )
    )


def _read(row, execution):
    try:
        record = AcceptanceRecord.model_validate(row.detail)
    except (AttributeError, TypeError, ValueError, ValidationError):
        return None
    if (record.node_id, record.cycle, record.plan_version) != (execution.node_id, execution.cycle, execution.accepted_plan_version):
        return None
    return record


async def record_merge_acceptance(session, context, receipt, snapshot, action):
    """Called only after settle_merge revalidates binding, review, CI and contract."""
    row = await _row(session, context.execution)
    if row is not None:
        # Settlement is idempotent. Never erase later checks on replay.
        return
    record = AcceptanceRecord(
        node_id=context.identity.node_id,
        cycle=context.identity.cycle,
        plan_version=context.identity.accepted_plan_version,
        binding_id=snapshot["binding_id"],
        scope_digest=scope_digest(snapshot["accepted_scope"]),
        head_sha=receipt.reviewed_head_sha,
        merge_sha=receipt.merge_sha,
        code_only=snapshot.get("code_only") is True,
        merge_action_id=action.id,
        merged_at=receipt.observed_at,
        requirements_verified_at=receipt.eligibility_observed_at,
    )
    session.add(
        OrchestrationAction(
            org_id=context.identity.org_id,
            execution_id=context.execution.id,
            operation_key=KEY,
            kind=KIND,
            status="succeeded",
            attempt=1,
            detail=record.model_dump(mode="json"),
            observed_at=receipt.observed_at,
        )
    )
    await session.flush()


async def record_deployment_acceptance(session, context, receipt):
    """Only a full verified delivery settles the combined deployment/runtime check."""
    row = await _row(session, context.execution)
    record = _read(row, context.execution)
    if record is None or record.code_only or receipt.source_revision != record.merge_sha or not receipt.delivery_complete:
        return
    if record.deployment_verified_at is not None:
        return
    action = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == context.identity.org_id,
            OrchestrationAction.execution_id == context.execution.id,
            OrchestrationAction.operation_key == receipt.operation_key,
            OrchestrationAction.status == "succeeded",
        )
    )
    if action is None:
        return
    record.deployment_verified_at = receipt.observed_at
    record.deployment_action_id = action.id
    record.docs_only = receipt.docs_only
    row.detail = record.model_dump(mode="json")
    row.observed_at = receipt.observed_at


async def record_acceptance_completion(session, execution, now):
    """Runner calls this after successful conclusion, in the same transaction."""
    row = await _row(session, execution)
    record = _read(row, execution)
    if record is None or record.completed_at is not None:
        return
    if execution.status != "concluded" or (not record.code_only and record.deployment_verified_at is None):
        return
    record.completed_at = now
    row.detail = record.model_dump(mode="json")
    row.observed_at = now


async def current_story_acceptance(session, *, org_id, executions):
    if not executions:
        return {}
    by_id = {execution.id: execution for execution in executions.values()}
    rows = await session.scalars(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == org_id,
            OrchestrationAction.execution_id.in_(by_id),
            OrchestrationAction.operation_key == KEY,
            OrchestrationAction.kind == KIND,
            OrchestrationAction.status == "succeeded",
        )
    )
    return {row.execution_id: record for row in rows if (record := _read(row, by_id[row.execution_id])) is not None}


def with_story_acceptance(progress, *, node, execution, binding, records, current, paused=False):
    if progress is None or node.kind != "story":
        return progress
    progress = progress.model_copy(update={"acceptance": None})
    record = records.get(execution.id) if execution is not None and current else None
    if record is not None and (
        binding is None
        or binding.id != record.binding_id
        or binding.head_sha != record.head_sha
        or scope_digest(binding.accepted_scope) != record.scope_digest
    ):
        record = None
    if record is None:
        if node.state != "passed":
            return progress
        accepted = progress.stage == "complete"
        return progress.model_copy(
            update={
                "acceptance": StoryAcceptance(
                    status="history_unavailable",
                    detail="Accepted previously; detailed checks were not recorded."
                    if accepted
                    else "Code delivery was recorded; detailed acceptance checks are unavailable. Delivery is still in progress.",
                    next_action=progress.next_action,
                    next_check_at=progress.next_check_at,
                )
            }
        )
    checks = [
        AcceptanceCheck(
            id=key,
            label=label,
            status="passed",
            detail=detail,
            observed_at=moment.isoformat(),
            evidence_id=record.merge_action_id,
        )
        for key, label, detail, moment in (
            ("merge", "Merge verified", "The bound pull request and merged revision were verified.", record.merged_at),
            ("review", "Review evidence verified", "Review evidence was verified for the merged head.", record.requirements_verified_at),
            ("ci", "CI requirements verified", "The applicable repository check requirements were satisfied.", record.requirements_verified_at),
        )
    ]
    active_status = (
        "paused"
        if paused or progress.automation == "paused"
        else "blocked"
        if execution.status == "blocked"
        else "recovering"
        if progress.stage in {"repair", "repairing", "reconciliation", "continuation"}
        else "waiting"
        if execution.status == "waiting"
        else "running"
    )
    if not record.code_only:
        checks.append(
            AcceptanceCheck(
                id="deployment",
                label="Deployment and runtime verified",
                status="passed" if record.deployment_verified_at else active_status,
                detail="Documentation-only change verified; no deployment was required."
                if record.docs_only
                else "All required deployment and runtime evidence was verified."
                if record.deployment_verified_at
                else progress.detail,
                observed_at=record.deployment_verified_at.isoformat() if record.deployment_verified_at else progress.observed_at,
                evidence_id=record.deployment_action_id,
            )
        )
    checks.append(
        AcceptanceCheck(
            id="completion",
            label="Story delivery completed",
            status="passed" if record.completed_at else active_status if record.deployment_verified_at or record.code_only else "pending",
            detail="The engine recorded completion of this story's delivery obligations."
            if record.completed_at
            else "Waiting for the separate dependent evaluation before closing delivery."
            if execution.phase == "evaluation_pending"
            else "The engine will record completion after the remaining requirements pass.",
            observed_at=record.completed_at.isoformat() if record.completed_at else None,
        )
    )
    return progress.model_copy(
        update={
            "acceptance": StoryAcceptance(
                status="complete" if record.completed_at else "in_progress",
                checks=checks,
                detail="Story delivery accepted." if record.completed_at else "Merge is complete; engine acceptance is still in progress.",
                next_action=None if record.completed_at else progress.next_action,
                next_check_at=None if record.completed_at else progress.next_check_at,
            )
        }
    )
