"""Shared-role reporting, scoped by dispatcher-issued capability, never run IDs."""

from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.agentauth.pr_binding_routes import BindPullRequestRequest
from src.agentauth.routes import require_agent_transport
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import (
    CONTRACT_VERSION,
    REPORT_HEADER,
    RunReportError,
    authenticate_run_report,
    record_terminal,
    report_snapshot,
    reviewer_artifact_assignment,
    validate_spool_document,
)
from src.orchestration.story_checkpoints import CheckpointError, TaskBoardWrite, public_snapshot, store_checkpoint

router = APIRouter(prefix="/internal/v1/agent/report", tags=["agent-reports"], dependencies=[Depends(require_agent_transport)])


def _sessions():
    from src.shared.database import get_session_factory

    return get_session_factory()


async def _authenticate(session, request, *, current=True):
    try:
        row = await authenticate_run_report(session, request.headers.get(REPORT_HEADER, ""))
        if current:
            from src.orchestration.shared_cycle import validate_current_report_assignment
            from src.orchestration.shared_policy import is_shared_continuation

            if row.dispatch_metadata.get("execution_continuation") or await is_shared_continuation(session, org_id=row.org_id, flow_id=row.flow_id):
                await validate_current_report_assignment(session, row)
        return row
    except (RunReportError, CycleBlockedError):
        raise HTTPException(404, "not found") from None


@router.get("")
async def read_report(request: Request):
    async with _sessions()() as session:
        row = await _authenticate(session, request, current=False)
        return report_snapshot(row)


@router.post("/task-board")
async def record_task_board(body: TaskBoardWrite, request: Request):
    """Observe the existing live board; it is never delivery evidence."""
    async with _sessions()() as session:
        row = await _authenticate(session, request)
        if row.terminal_receipt or not row.worker_receipt:
            raise HTTPException(409, "task_board_run_not_active")
        previous = row.task_board_snapshot or {}
        try:
            checkpoint = await store_checkpoint(session, row, body)
        except CheckpointError as error:
            raise HTTPException(409, str(error)) from None
        value = dict(previous)
        version = (body.generation, body.sequence)
        if not previous or version > (previous["generation"], previous["sequence"]):
            value.update(public_snapshot(body.model_dump(mode="json")))
        if checkpoint is not None:
            value["story_checkpoint"] = checkpoint
        # Legacy display-only updates never erase acknowledged structured progress.
        row.task_board_snapshot = value
        await session.commit()
        return {
            "contract_version": 1,
            "run_id": row.run_id,
            "attempt": row.attempt,
            "recorded": True,
            **({"checkpoint_revision": checkpoint["revision"]} if checkpoint else {}),
        }


def _spool_view(row) -> dict:
    return {"contract_version": CONTRACT_VERSION, "run_id": row.run_id, "attempt": row.attempt, "spool_document": row.spool_document}


@router.get("/spool")
async def read_spool(request: Request):
    """Untrusted pending-report marker for a protected worker (#7392)."""
    async with _sessions()() as session:
        row = await _authenticate(session, request, current=False)
        return _spool_view(row)


class SpoolWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: dict
    create: bool = False


@router.post("/spool")
async def write_spool(body: SpoolWrite, request: Request):
    """Store the worker's marker verbatim after checking it names this assignment.

    ``create`` mirrors the S3 ``IfNoneMatch`` precondition: a second creator sees
    409 and must run delivery recovery, exactly as with the bucket spool. The
    marker never grants anything; ``/started`` still locks ownership.
    """
    async with _sessions()() as session:
        row = await _authenticate(session, request, current=False)
        if body.create and row.spool_document is not None:
            raise HTTPException(409, "spool_exists")
        try:
            row.spool_document = validate_spool_document(row, body.document)
        except RunReportError as exc:
            raise HTTPException(422, exc.code) from None
        await session.commit()
        return _spool_view(row)


@router.post("/pull-request")
async def register_pull_request(body: BindPullRequestRequest, request: Request):
    # Persist the candidate before provider I/O. A killed process or lost response
    # leaves sufficient exact evidence for queue redelivery to retry reporting only.
    async with _sessions()() as session:
        row = await _authenticate(session, request)
        candidate = body.model_dump()
        if row.dispatch_metadata.get("pr_binding_required") and not reviewer_artifact_assignment(row) and body.reviewer_artifact:
            raise HTTPException(409, "implementation_binding_required")
        if row.terminal_receipt:
            if row.binding_receipt and row.candidate_pr == candidate:
                return report_snapshot(row)
            raise HTTPException(409, "terminal_report_immutable")
        if body.repo.lower() != row.repo.lower():
            raise HTTPException(409, "repository_mismatch")
        if len(body.head_sha) not in {40, 64}:
            raise HTTPException(422, "full_head_sha_required")
        if row.candidate_pr and row.candidate_pr != candidate:
            raise HTTPException(409, "candidate_conflict")
        if row.binding_receipt and row.candidate_pr == candidate:
            return report_snapshot(row)
        row.candidate_pr = candidate
        row.block_code = "pr_binding_pending"
        row.retryable = True
        await session.commit()
    return await retry_pull_request(request)


@router.post("/pull-request/retry")
async def retry_pull_request(request: Request):
    from src.orchestration.models import BindingRole
    from src.orchestration.pr_bindings import BindingError, register_binding, resolve_registration_target
    from src.orchestration.pr_identity import PrIdentityError, resolve_pr_identity
    from src.orchestration.state import ActorKind

    async with _sessions()() as session:
        row = await _authenticate(session, request)
        if row.binding_receipt:
            return report_snapshot(row)
        if not row.candidate_pr:
            raise HTTPException(409, "pr_candidate_missing")
        if row.block_code and not row.retryable:
            return report_snapshot(row)
        candidate = row.candidate_pr
        try:
            if (row.dispatch_metadata.get("review_cycle_input") or {}).get("operation_key"):
                # K2 owns these run IDs; only its verified action/dispatch receipt
                # can resolve the retained story target for a same-attempt repair.
                from src.orchestration.shared_cycle import registration_target_for_report

                target = await registration_target_for_report(session, row)
            else:
                target = await resolve_registration_target(session, run_id=row.run_id, expected_org_id=row.org_id)
            if (
                target.node_id != row.node_id
                or target.flow_id != row.flow_id
                or target.attempt != row.attempt
                or target.repo.lower() != row.repo.lower()
                or target.installation_id != row.installation_id
            ):
                raise RunReportError("report_scope_changed")
            pr = await resolve_pr_identity(org_id=row.org_id, installation_id=row.installation_id, repo=row.repo, pr_number=candidate["pr_number"])
            if pr.provider_repository_id != candidate["provider_repository_id"] or (
                row.provider_repository_id is not None and pr.provider_repository_id != row.provider_repository_id
            ):
                raise RunReportError("repository_mismatch")
            if pr.provider_pr_node_id != candidate["provider_pr_node_id"]:
                raise RunReportError("incomplete_identity")
            if pr.head_sha != candidate["head_sha"]:
                raise RunReportError("head_moved")
            binding, created = await register_binding(
                session,
                target=target,
                pr=pr,
                actor_id=f"run-report:{row.run_id}",
                actor_kind=ActorKind.SERVICE,
                declared_role=BindingRole.REVIEWER_ARTIFACT if reviewer_artifact_assignment(row) or candidate.get("reviewer_artifact") else None,
            )
            row.binding_receipt = {
                "bound": True,
                "created": created,
                "run_id": row.run_id,
                "attempt": row.attempt,
                "node_id": row.node_id,
                "pr_number": binding.pr_number,
                "repo": binding.repo,
                "provider_repository_id": binding.provider_repository_id,
                "provider_pr_node_id": binding.provider_pr_node_id,
                "head_sha": binding.head_sha,
                "role": binding.role,
                "state": binding.state,
            }
            row.block_code = None
            row.retryable = False
        except PrIdentityError:
            row.block_code, row.retryable = "pr_identity_unavailable", True
        except BindingError as exc:
            row.block_code, row.retryable = exc.code.value, False
        except RunReportError as exc:
            row.block_code, row.retryable = exc.code, exc.retryable
        await session.commit()
        return report_snapshot(row)


class FailureDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal[
        "unknown",
        "cancelled",
        "deadline",
        "signal",
        "policy",
        "provider_refusal",
        "authentication",
        "transport",
        "stale_head",
        "git_validation",
        "inspection",
        "contract",
    ]
    exit_code: int = Field(strict=True, ge=-255, le=255)


class TerminalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["complete", "failed"]
    failure: FailureDetails | None = None


class OperationsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report: str = Field(min_length=1, max_length=12000)


@router.post("/operations")
async def attach_operations_report(body: OperationsReport, request: Request):
    from src.orchestration.accepted_node import accepted_node_contract
    from src.orchestration.developer_personas import operations_executor_enabled
    from src.orchestration.models import OrchestrationNode
    from src.shared.models.base import utcnow

    async with _sessions()() as session:
        row = await _authenticate(session, request)
        scope = row.dispatch_metadata.get("orchestration") or {}
        fences = row.dispatch_metadata.get("handoff_expect") or {}
        node = await session.get(OrchestrationNode, row.node_id)
        contract = await accepted_node_contract(session, node) if node is not None else None
        if (
            not operations_executor_enabled()
            or row.persona not in {"operations", "agent-codex-operations"}
            or scope.get("executor_role") != "operate"
            or contract is None
            or contract.node.executor is None
            or contract.node.executor.role != "operate"
            or contract.node.executor.persona != row.persona
            # The accepted node may leave its repository identity unset; the
            # dispatched repository was then bounded by the accepted policy's
            # repository ids at admission and by the operations grant (#7349).
            # When the node names one, the report must come from it.
            or contract.node.repository_identity not in (None, row.repo)
            or contract.plan_version != fences.get("accepted_plan_version")
            or not contract.node.evidence_expectations
            or any(not item.startswith("runtime_verified:") for item in contract.node.evidence_expectations)
        ):
            raise HTTPException(404, "not found")
        if len(body.report.encode("utf-8")) > 12000:
            raise HTTPException(422, "operations_report_too_large")
        digest = hashlib.sha256(body.report.encode("utf-8")).hexdigest()
        existing = row.operations_report_receipt
        if existing:
            if existing.get("sha256") != digest:
                raise HTTPException(409, "operations_report_immutable")
            return report_snapshot(row)
        if row.terminal_receipt:
            raise HTTPException(409, "terminal_report_immutable")
        row.operations_report_receipt = {
            "run_id": row.run_id,
            "attempt": row.attempt,
            "node_id": row.node_id,
            "receipt_id": f"operations-report:{row.run_id}",
            "sha256": digest,
            "report": body.report,
            "recorded": True,
            "recorded_at": utcnow().isoformat(),
        }
        await session.commit()
        return report_snapshot(row)


@router.post("/terminal")
async def terminal_report(body: TerminalReport, request: Request):
    async with _sessions()() as session:
        row = await _authenticate(session, request)
        if (
            body.outcome == "complete"
            and row.persona in {"operations", "agent-codex-operations"}
            and (row.dispatch_metadata.get("orchestration") or {}).get("executor_role") == "operate"
            and not row.operations_report_receipt
        ):
            raise HTTPException(409, "operations_report_missing")
        try:
            if body.outcome == "complete" and not row.terminal_receipt:
                from src.orchestration.review_assignment import require_reviewer_merge

                await require_reviewer_merge(session, org_id=row.org_id, node_id=row.node_id, run_id=row.run_id)
            record_terminal(row, body.outcome, failure=body.failure.model_dump() if body.failure else None)
        except RunReportError as exc:
            raise HTTPException(503 if exc.code == "reviewer_merge_unverifiable" else 409, exc.code) from None
        await session.commit()
        return report_snapshot(row)


class ReportingBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Literal[
        "delivery_recovery_required", "report_spool_unavailable", "report_spool_unconfigured", "report_spool_scope_mismatch", "pr_candidate_missing"
    ]


@router.post("/block")
async def reporting_block(body: ReportingBlock, request: Request):
    async with _sessions()() as session:
        row = await _authenticate(session, request)
        if not row.terminal_receipt:
            row.block_code = body.code
            row.retryable = body.code == "report_spool_unavailable"
            await session.commit()
        return report_snapshot(row)


class WorkerStarted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ownership_nonce: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.post("/started")
async def worker_started(body: WorkerStarted, request: Request):
    from src.shared.models.base import utcnow

    async with _sessions()() as session:
        row = await _authenticate(session, request)
        if row.terminal_receipt or (row.worker_receipt and row.worker_receipt["ownership_nonce"] != body.ownership_nonce):
            raise HTTPException(409, "delivery_already_started")
        from src.orchestration.report_dispatch import validate_report_start

        try:
            await validate_report_start(session, row)
        except RunReportError as exc:
            row.block_code, row.retryable = exc.code, exc.retryable
            await session.commit()
            raise HTTPException(409, exc.code) from None
        if not row.worker_receipt:
            row.worker_receipt = {
                "run_id": row.run_id,
                "attempt": row.attempt,
                "ownership_nonce": body.ownership_nonce,
                "recorded_at": utcnow().isoformat(),
            }
            if row.block_code == "delivery_recovery_required":
                row.block_code, row.retryable = None, False
        if row.block_code == "execution_assignment_unverifiable":
            row.block_code, row.retryable = None, False
        await session.commit()
        return report_snapshot(row)
