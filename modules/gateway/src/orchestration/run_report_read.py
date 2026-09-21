"""Operator observations of server-acknowledged shared-worker reports.

This projection never authenticates a worker, reconstructs a capability, or
changes delivery state. A terminal acknowledgment is not merge approval.
"""

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, select

from src.shared.models.base import utcnow

from .models import OrchestrationNode
from .run_reports import OrchestrationRunReport

MAX_REPORTS_PER_PAGE = 200


class ReceiptObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["worker_started", "pull_request_bound", "terminal", "review"]
    acknowledged: bool
    recorded_at: datetime | None = None
    outcome: Literal["complete", "failed"] | None = None


class ReportBindingObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    pr_number: int
    head_sha: str
    role: Literal["implementation", "reviewer_artifact"]
    state: Literal["active"]


class RunReportObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    node_id: str
    attempt: int
    persona: str
    repo: str
    is_current_attempt: bool
    status: Literal["assigned", "worker_started", "terminal_acknowledged", "unverifiable"]
    assigned_at: datetime
    expires_at: datetime
    binding: ReportBindingObservation | None
    receipts: list[ReceiptObservation]
    block_code: str | None
    retryable: bool


class FlowRunReportsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_id: str
    server_time: datetime
    source: Literal["authenticated_run_reports"] = "authenticated_run_reports"
    reports: list[RunReportObservation]
    total: int
    limit: int
    offset: int


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        return parsed if isinstance(parsed, datetime) and parsed.tzinfo is not None else None
    except ValueError:
        return None


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _matches(receipt, row):
    return (
        isinstance(receipt, dict)
        and receipt.get("run_id") == row.run_id
        and type(receipt.get("attempt")) is int
        and receipt["attempt"] == row.attempt
    )


def project_report(row):
    started, terminal, review, bound = row.worker_receipt, row.terminal_receipt, row.review_receipt, row.binding_receipt
    started_ok = _matches(started, row) and _time(started.get("recorded_at")) is not None
    terminal_ok = (
        _matches(terminal, row)
        and terminal.get("contract_version") == 1
        and terminal.get("outcome") in {"complete", "failed"}
        and _time(terminal.get("recorded_at")) is not None
    )
    review_ok = _matches(review, row) and review.get("contract_version") == 1 and review.get("recorded") is True
    binding = None
    if (
        _matches(bound, row)
        and bound.get("bound") is True
        and bound.get("node_id") == row.node_id
        and isinstance(bound.get("repo"), str)
        and bound["repo"].lower() == row.repo.lower()
        and type(bound.get("pr_number")) is int
        and bound["pr_number"] > 0
        and isinstance(bound.get("head_sha"), str)
        and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", bound["head_sha"])
        and bound.get("role") in {"implementation", "reviewer_artifact"}
        and bound.get("state") == "active"
    ):
        # This is the binding at acknowledgment. The graph exposes its current
        # head/state; later repairs must never inherit this historical approval.
        binding = ReportBindingObservation(**{key: bound[key] for key in ReportBindingObservation.model_fields})
    invalid = any(
        value is not None and not valid
        for value, valid in ((started, started_ok), (terminal, terminal_ok), (review, review_ok), (bound, binding is not None))
    )
    status = "unverifiable" if invalid else "terminal_acknowledged" if terminal_ok else "worker_started" if started_ok else "assigned"
    receipts = [
        ReceiptObservation(kind="worker_started", acknowledged=started_ok, recorded_at=_time(started.get("recorded_at")) if started_ok else None),
        # Binding/review receipts currently omit timestamps; retain null rather
        # than fabricate one from assignment creation or the poll time.
        ReceiptObservation(
            kind="pull_request_bound", acknowledged=binding is not None, recorded_at=_time(bound.get("recorded_at")) if binding else None
        ),
        ReceiptObservation(
            kind="terminal",
            acknowledged=terminal_ok,
            recorded_at=_time(terminal.get("recorded_at")) if terminal_ok else None,
            outcome=terminal.get("outcome") if terminal_ok else None,
        ),
        ReceiptObservation(kind="review", acknowledged=review_ok, recorded_at=_time(review.get("recorded_at")) if review_ok else None),
    ]
    return RunReportObservation(
        run_id=row.run_id,
        node_id=row.node_id,
        attempt=row.attempt,
        persona=row.persona,
        repo=row.repo,
        is_current_attempt=row.attempt == row.current_attempt,
        status=status,
        assigned_at=_aware(row.created_at),
        expires_at=_aware(row.expires_at),
        binding=binding,
        receipts=receipts,
        block_code=row.block_code if isinstance(row.block_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", row.block_code) else None,
        retryable=row.retryable,
    )


async def load_flow_run_reports(session, *, org_id, flow_id, limit, offset):
    report, node = OrchestrationRunReport, OrchestrationNode
    scope = (report.org_id == org_id, report.flow_id == flow_id, node.org_id == org_id, node.flow_id == flow_id)
    join = and_(node.id == report.node_id, node.org_id == report.org_id, node.flow_id == report.flow_id)
    total = await session.scalar(select(func.count()).select_from(report).join(node, join).where(*scope))
    # Select only observation fields. Credential hashes, dispatch envelopes,
    # candidates and acceptance/work-claim bindings are never loaded here.
    rows = (
        await session.execute(
            select(
                report.run_id,
                report.node_id,
                report.attempt,
                report.persona,
                report.repo,
                report.created_at,
                report.expires_at,
                report.worker_receipt,
                report.binding_receipt,
                report.terminal_receipt,
                report.review_receipt,
                report.block_code,
                report.retryable,
                node.attempts.label("current_attempt"),
            )
            .join(node, join)
            .where(*scope)
            .order_by(report.created_at, report.run_id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return FlowRunReportsResponse(
        flow_id=flow_id, server_time=utcnow(), reports=[project_report(row) for row in rows], total=total, limit=limit, offset=offset
    )
