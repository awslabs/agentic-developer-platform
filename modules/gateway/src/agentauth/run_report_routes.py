"""Shared-role reporting, scoped by dispatcher-issued capability, never run IDs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.agentauth.pr_binding_routes import BindPullRequestRequest
from src.agentauth.routes import require_agent_transport
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import (
    REPORT_HEADER,
    RunReportError,
    authenticate_run_report,
    record_terminal,
    report_snapshot,
    reviewer_artifact_assignment,
)

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


@router.post("/terminal")
async def terminal_report(body: TerminalReport, request: Request):
    async with _sessions()() as session:
        row = await _authenticate(session, request)
        try:
            record_terminal(row, body.outcome, failure=body.failure.model_dump() if body.failure else None)
        except RunReportError as exc:
            raise HTTPException(409, exc.code) from None
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
