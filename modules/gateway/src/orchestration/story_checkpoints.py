"""Private, authenticated operations progress; never execution or acceptance evidence."""

import hashlib
import json
import os
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .models import OrchestrationAcceptedPlan, OrchestrationNode
from .run_reports import OrchestrationRunReport
from .story_run_insights import TaskBoardSnapshot

MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_ENVELOPE_BYTES = 128 * 1024
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Status = Literal["open", "in_progress", "done", "blocked"]
SECRET = re.compile(
    r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{12,}|(?:AKIA|ASIA)[A-Z0-9]{16}|\bBearer\s+\S+|-----BEGIN[A-Z ]*PRIVATE KEY-----|"
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|"
    r"(?:password|token|secret|api[_-]?key)[\"']?\s*[:=]\s*[\"']?[^\s,;\"']{8,}",
    re.I,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CheckpointScope(Strict):
    tenant_id: str = Field(min_length=1, max_length=255)
    flow_id: str = Field(min_length=1, max_length=255)
    node_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=255)
    attempt: int = Field(ge=1)
    claim_generation: int = Field(ge=1)
    accepted_plan_version: int = Field(ge=1)
    issue: int = Field(ge=1)
    repository_id: int = Field(ge=1)
    installation_id: int = Field(ge=1)
    repository: str = Field(min_length=1, max_length=255)
    branch: str = Field(min_length=1, max_length=255)


class Source(Strict):
    issue: int = Field(ge=1)
    repository_id: int = Field(ge=1)
    branch: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=255)
    sha256: Digest
    confirmed_source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")


class Update(Strict):
    base_task_hash: Digest
    status: Status


class Addition(Strict):
    id: Identifier
    kind: Literal["code", "test", "infra"]
    title: str = Field(min_length=1, max_length=1000, pattern=r"^[^\r\n\x00]+$")
    status: Status


class Conflict(Strict):
    id: Identifier
    reason: Literal["branch_edited", "branch_deleted", "addition_incorporated"]
    claimed_status: Status


class History(Strict):
    id: Identifier
    status: Status
    run_id: str = Field(min_length=1, max_length=255)
    observed_at: str = Field(min_length=1, max_length=64)


class Delta(Strict):
    version: Literal[1]
    progress_only: Literal[True]
    source: Source
    updates: dict[Identifier, Update] = Field(max_length=1000)
    additions: dict[Identifier, Addition] = Field(max_length=1000)
    conflicts: list[Conflict] = Field(max_length=1000)
    history: list[History] = Field(max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def exact_version(cls, value):
        if isinstance(value, dict) and (type(value.get("version")) is not int or value["version"] != 1 or value.get("progress_only") is not True):
            raise ValueError("story_checkpoint_version")
        return value

    @model_validator(mode="after")
    def bounded(self):
        text = json.dumps(self.model_dump(), ensure_ascii=False, separators=(",", ":"))
        if len(text.encode()) > MAX_CHECKPOINT_BYTES:
            raise ValueError("story_checkpoint_oversized")
        if SECRET.search(text) or any(
            value in text
            for key, value in os.environ.items()
            if re.search(r"TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY|API_KEY", key) and len(value) >= 8
        ):
            raise ValueError("story_checkpoint_sensitive_content")
        if self.updates.keys() & self.additions.keys() or any(key != task.id for key, task in self.additions.items()):
            raise ValueError("story_checkpoint_task_identity")
        return self


class CheckpointPayload(Strict):
    scope: CheckpointScope
    delta: Delta

    @model_validator(mode="after")
    def source_scope(self):
        source, scope = self.delta.source, self.scope
        if (
            source.issue != scope.issue
            or source.repository_id != scope.repository_id
            or source.branch != scope.branch
            or source.path != f".adp/tasks/{scope.issue}.json"
            or scope.branch != f"agent/issue-{scope.issue}"
        ):
            raise ValueError("story_checkpoint_source_scope")
        return self


class TaskBoardWrite(TaskBoardSnapshot):
    story_checkpoint: CheckpointPayload | None = None
    expected_checkpoint_revision: Digest | None = None

    @model_validator(mode="after")
    def envelope_bound(self):
        if len(self.model_dump_json().encode()) > MAX_ENVELOPE_BYTES:
            raise ValueError("story_checkpoint_envelope_oversized")
        return self


class CheckpointError(Exception):
    pass


async def report_scope(session, row):
    node = await session.get(OrchestrationNode, row.node_id)
    metadata = row.dispatch_metadata
    source = metadata.get("source_ref") or {}
    fences = metadata.get("execution_continuation") or metadata.get("handoff_expect") or {}
    if (
        row.persona not in {"operations", "agent-codex-operations"}
        or node is None
        or node.kind != "story"
        or node.org_id != row.org_id
        or node.flow_id != row.flow_id
        or not re.fullmatch(r"[1-9][0-9]{0,18}", node.issue_ref or "")
        or source.get("issue") != int(node.issue_ref)
    ):
        raise CheckpointError("story_checkpoint_scope_unavailable")
    try:
        return CheckpointScope(
            tenant_id=row.org_id,
            flow_id=row.flow_id,
            node_id=row.node_id,
            run_id=row.run_id,
            attempt=row.attempt,
            claim_generation=fences.get("claim_generation"),
            accepted_plan_version=fences.get("accepted_plan_version"),
            issue=int(node.issue_ref),
            repository_id=row.provider_repository_id,
            installation_id=row.installation_id,
            repository=row.repo,
            branch=f"agent/issue-{node.issue_ref}",
        )
    except ValueError:
        raise CheckpointError("story_checkpoint_scope_unavailable") from None


def stable_scope(scope):
    return scope.model_dump(exclude={"run_id", "attempt", "claim_generation"})


def revision(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


async def preserved_scope(session, original, current):
    before, after = stable_scope(original), stable_scope(current)
    old_version, new_version = before.pop("accepted_plan_version"), after.pop("accepted_plan_version")
    if before != after or old_version > new_version:
        return False
    if old_version == new_version:
        return True
    from .plan_lineage import ancestor_plan

    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == current.tenant_id,
            OrchestrationAcceptedPlan.flow_id == current.flow_id,
            OrchestrationAcceptedPlan.version == new_version,
        )
    )
    return await ancestor_plan(session, plan, old_version, node_id=current.node_id) is not None


async def latest_checkpoint(session, scope):
    rows = list(
        await session.scalars(
            select(OrchestrationRunReport)
            .where(
                OrchestrationRunReport.org_id == scope.tenant_id,
                OrchestrationRunReport.flow_id == scope.flow_id,
                OrchestrationRunReport.node_id == scope.node_id,
                OrchestrationRunReport.repo == scope.repository,
                OrchestrationRunReport.provider_repository_id == scope.repository_id,
                OrchestrationRunReport.installation_id == scope.installation_id,
                OrchestrationRunReport.attempt <= scope.attempt,
            )
            .order_by(OrchestrationRunReport.attempt.desc(), OrchestrationRunReport.created_at.desc())
            .limit(65)
        )
    )
    for row in rows[:64]:
        saved = (row.task_board_snapshot or {}).get("story_checkpoint")
        if not saved:
            continue
        try:
            payload = CheckpointPayload.model_validate({key: saved[key] for key in ("scope", "delta")})
            original = await report_scope(session, row)
            if (
                payload.scope != original
                or not await preserved_scope(session, payload.scope, scope)
                or saved.get("revision") != revision(payload.model_dump())
            ):
                raise ValueError("scope or revision")
        except (ValueError, KeyError, TypeError):
            raise CheckpointError("story_checkpoint_requires_reconciliation") from None
        return saved
    if len(rows) > 64:
        raise CheckpointError("story_checkpoint_history_exceeds_bound")
    return None


async def read_story_checkpoint(session, assignment):
    """Called only after current operations assignment/execution authorization."""
    scope = None
    try:
        row = await session.get(OrchestrationRunReport, assignment.invocation_id)
        if (
            row is None
            or row.org_id != assignment.tenant_id
            or row.node_id != assignment.node_id
            or row.repo != assignment.repository
            or row.provider_repository_id != assignment.repository_id
            or row.installation_id != assignment.installation_id
        ):
            raise CheckpointError("story_checkpoint_scope_unavailable")
        scope = await report_scope(session, row)
        node = await session.get(OrchestrationNode, assignment.node_id)
        if node is None or any(
            (
                scope.tenant_id != assignment.tenant_id,
                scope.node_id != assignment.node_id,
                scope.attempt != assignment.attempt,
                scope.claim_generation != assignment.claim_generation,
                scope.accepted_plan_version != assignment.accepted_plan_version,
                scope.repository_id != assignment.repository_id,
                scope.installation_id != assignment.installation_id,
                scope.repository != assignment.repository,
                scope.branch != assignment.branch,
                scope.flow_id != node.flow_id,
            )
        ):
            raise CheckpointError("story_checkpoint_scope_changed")
        saved = await latest_checkpoint(session, scope)
        return {"state": "available" if saved else "absent", "scope": scope.model_dump(), "checkpoint": saved, "preserved_plan": saved is not None}
    except CheckpointError as error:
        return {
            "state": "unavailable",
            "reason": str(error),
            **({"scope": scope.model_dump()} if scope else {}),
            "next_action": "Preserve the branch board and prior run activity; reconcile checkpoint scope before saving new durable progress.",
        }


async def store_checkpoint(session, row, body):
    payload = body.story_checkpoint
    if payload is None:
        return None
    scope = await report_scope(session, row)
    if payload.scope != scope:
        raise CheckpointError("story_checkpoint_scope_changed")
    previous = await latest_checkpoint(session, scope)
    value = payload.model_dump()
    digest = revision(value)
    if previous and previous["revision"] == digest:
        return previous  # Lost acknowledgement: an identical retry is harmless.
    if body.expected_checkpoint_revision != (previous["revision"] if previous else None):
        raise CheckpointError("story_checkpoint_revision_conflict")
    return {**value, "revision": digest, "parent_revision": body.expected_checkpoint_revision}


def public_snapshot(value):
    """Private deltas must never enter StoryRunInsight or its public schema."""
    return {key: value[key] for key in ("generation", "sequence", "observed_at", "text") if key in value}
