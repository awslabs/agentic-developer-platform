"""Scoped, durable worker reports for shared-role engine dispatch.

The domain-separated reporting capability authorizes only one server-created assignment.
It is not an IAM grant or a substitute for protected execution authority. Workers
share the existing transport role; request bodies never select a tenant or run.
Only the capability hash is stored; dispatch can reconstruct it for publication
retries using its existing signing key. Credentials must not enter decisions,
logs, or public API projections. Queue redelivery retains the original envelope.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, utcnow

from .models import JSON_DOC, OrchestrationFlow, OrchestrationNode

REPORT_HEADER = "X-Adp-Report-Credential"
CONTRACT_VERSION = 1


class RunReportError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class OrchestrationRunReport(Base):
    __tablename__ = "orchestration_run_reports"
    __table_args__ = (Index("ix_orchestration_run_reports_node", "org_id", "node_id", "attempt"),)

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    credential_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    flow_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    persona: Mapped[str] = mapped_column(String(64), nullable=False)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    installation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_repository_id: Mapped[int | None] = mapped_column(BigInteger)
    dispatch_metadata: Mapped[dict] = mapped_column(JSON_DOC, nullable=False)
    candidate_pr: Mapped[dict | None] = mapped_column(JSON_DOC)
    binding_receipt: Mapped[dict | None] = mapped_column(JSON_DOC)
    worker_receipt: Mapped[dict | None] = mapped_column(JSON_DOC)
    terminal_receipt: Mapped[dict | None] = mapped_column(JSON_DOC)
    review_receipt: Mapped[dict | None] = mapped_column(JSON_DOC)
    block_code: Mapped[str | None] = mapped_column(String(80))
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _digest(credential: str) -> str:
    if not isinstance(credential, str) or not credential.startswith("adprpt1.") or len(credential) != 51:
        raise RunReportError("report_unauthorized")
    return hashlib.sha256(credential.encode()).hexdigest()


def _mint(metadata: dict) -> str:
    from .report_signing import signing_key

    key = signing_key()
    if not key:
        raise RunReportError("report_signing_key_unavailable", retryable=True)
    message = b"adp-run-report:v1:" + json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(key.encode(), message, hashlib.sha256).digest()
    return "adprpt1." + base64.urlsafe_b64encode(mac).decode().rstrip("=")


async def prepare_run_report(session: AsyncSession, envelope: dict) -> OrchestrationRunReport:
    """Persist assignment before publication; reconstruct the same token on replay.

    Persist envelopes without ``run_report``. Raw capabilities belong only in
    the transport envelope; public decisions can retain the remaining metadata.
    """
    run_id = envelope["message_id"]
    existing = await session.get(OrchestrationRunReport, run_id)
    if existing is not None:
        credential = _mint(existing.dispatch_metadata)
        if not secrets.compare_digest(existing.credential_hash, _digest(credential)):
            raise RunReportError("report_signing_key_changed", retryable=True)
        envelope["run_report"] = {"contract_version": CONTRACT_VERSION, "credential": credential}
        return existing
    scope = envelope["orchestration"]
    source = envelope["source_ref"]
    node = await session.get(OrchestrationNode, scope["node_id"])
    if (
        node is None
        or node.org_id != envelope["tenant_id"]
        or node.flow_id != scope["flow_id"]
        or node.attempts != scope["attempt"]
        or node.state not in {"running", "awaiting_merge"}
    ):
        raise RunReportError("report_assignment_scope_mismatch")
    metadata = json.loads(json.dumps({key: value for key, value in envelope.items() if key != "run_report"}))
    metadata["report_nonce"] = secrets.token_hex(16)
    credential = _mint(metadata)
    row = OrchestrationRunReport(
        run_id=run_id,
        credential_hash=_digest(credential),
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        attempt=node.attempts,
        persona=envelope["persona"],
        repo=source["repo"],
        installation_id=int(source["installation_id"]),
        provider_repository_id=source.get("provider_repository_id"),
        dispatch_metadata=metadata,
        expires_at=utcnow() + timedelta(days=15),
    )
    session.add(row)
    await session.flush()
    envelope["run_report"] = {"contract_version": CONTRACT_VERSION, "credential": credential}
    return row


async def authenticate_run_report(session: AsyncSession, credential: str, *, lock: bool = True) -> OrchestrationRunReport:
    query = select(OrchestrationRunReport).where(OrchestrationRunReport.credential_hash == _digest(credential))
    row = (await session.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
    if row is None:
        raise RunReportError("report_unauthorized")
    expiry = row.expires_at.replace(tzinfo=UTC) if row.expires_at.tzinfo is None else row.expires_at
    if expiry <= utcnow():
        raise RunReportError("report_expired")
    node = (
        await session.execute(
            select(OrchestrationNode)
            .where(
                OrchestrationNode.id == row.node_id,
                OrchestrationNode.org_id == row.org_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    flow = await session.get(OrchestrationFlow, row.flow_id)
    if (
        node is None
        or flow is None
        or flow.org_id != row.org_id
        or node.flow_id != row.flow_id
        or node.attempts != row.attempt
        or node.state not in {"running", "awaiting_merge", "passed"}
        or flow.state not in {"pending", "running", "completed"}
    ):
        raise RunReportError("report_superseded")
    return row


def report_snapshot(row: OrchestrationRunReport) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "run_id": row.run_id,
        "attempt": row.attempt,
        "candidate_pr": row.candidate_pr,
        "binding_receipt": row.binding_receipt,
        "terminal_receipt": row.terminal_receipt,
        "worker_receipt": row.worker_receipt,
        "review_receipt": row.review_receipt,
        "block_code": row.block_code,
        "retryable": row.retryable,
    }


def record_terminal(row: OrchestrationRunReport, outcome: str) -> dict:
    if outcome not in {"complete", "failed"}:
        raise RunReportError("invalid_terminal_outcome")
    if outcome == "complete" and row.dispatch_metadata.get("pr_binding_required"):
        binding = row.binding_receipt
        expected_role = "reviewer_artifact" if row.persona in {"reviewer", "agent-codex-reviewer"} else "implementation"
        if (
            not isinstance(binding, dict)
            or binding.get("bound") is not True
            or binding.get("run_id") != row.run_id
            or type(binding.get("attempt")) is not int
            or binding.get("attempt") != row.attempt
            or binding.get("node_id") != row.node_id
            or not isinstance(binding.get("repo"), str)
            or binding["repo"].lower() != row.repo.lower()
            or type(binding.get("pr_number")) is not int
            or binding["pr_number"] <= 0
            or type(binding.get("provider_repository_id")) is not int
            or binding["provider_repository_id"] <= 0
            or (row.provider_repository_id is not None and binding["provider_repository_id"] != row.provider_repository_id)
            or not isinstance(binding.get("provider_pr_node_id"), str)
            or not binding["provider_pr_node_id"]
            or not isinstance(binding.get("head_sha"), str)
            or len(binding["head_sha"]) not in {40, 64}
            or binding.get("role") != expected_role
            or binding.get("state") != "active"
        ):
            raise RunReportError("pr_binding_unacknowledged", retryable=True)
    if outcome == "complete" and row.dispatch_metadata.get("review_expect") and not (row.review_receipt or {}).get("recorded"):
        raise RunReportError("review_result_unacknowledged", retryable=True)
    if row.terminal_receipt:
        if row.terminal_receipt["outcome"] != outcome:
            raise RunReportError("terminal_outcome_conflict")
        return row.terminal_receipt
    row.terminal_receipt = {
        "contract_version": CONTRACT_VERSION,
        "run_id": row.run_id,
        "attempt": row.attempt,
        "outcome": outcome,
        "recorded_at": utcnow().isoformat(),
    }
    return row.terminal_receipt


# Descriptive alias for model/review consumers; same authentication and fences.
authenticate_report_assignment = authenticate_run_report


async def run_result_for_assignment(session: AsyncSession, *, node: OrchestrationNode, dispatch: dict) -> dict | None:
    """A reporting assignment owns its terminal outcome, regardless of DDB lag.

    ``None`` means this is an older, unassigned run whose legacy reader applies.
    An assignment without a receipt is unfinished, never a reason to trust an
    advisory completion write. No receipt or identity is reconstructed from DDB.
    """
    row = (
        await session.scalars(
            select(OrchestrationRunReport)
            .where(
                OrchestrationRunReport.run_id == dispatch.get("run_id"),
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    metadata = row.dispatch_metadata
    scope = metadata.get("orchestration") or {}
    if (
        row.org_id != node.org_id
        or row.flow_id != node.flow_id
        or row.node_id != node.id
        or row.attempt != node.attempts
        or row.attempt != dispatch.get("attempt")
        or row.run_id != dispatch.get("run_id")
        or metadata.get("message_id") != row.run_id
        or metadata.get("tenant_id") != row.org_id
        or scope.get("node_id") != row.node_id
        or scope.get("flow_id") != row.flow_id
        or scope.get("attempt") != row.attempt
    ):
        raise RunReportError("report_result_scope_mismatch")
    status = "in_progress"
    receipt = row.terminal_receipt
    if receipt is not None:
        if (
            receipt.get("contract_version") != CONTRACT_VERSION
            or receipt.get("run_id") != row.run_id
            or receipt.get("attempt") != row.attempt
            or receipt.get("outcome") not in {"complete", "failed"}
            or not receipt.get("recorded_at")
        ):
            raise RunReportError("terminal_receipt_scope_mismatch")
        status = receipt["outcome"]
        if status == "complete":
            binding = row.binding_receipt or {}
            if metadata.get("pr_binding_required") and (
                binding.get("bound") is not True
                or binding.get("run_id") != row.run_id
                or binding.get("attempt") != row.attempt
                or binding.get("node_id") != row.node_id
                or (
                    row.persona not in {"reviewer", "agent-codex-reviewer"}
                    and (binding.get("role") != "implementation" or binding.get("state") != "active")
                )
            ):
                raise RunReportError("terminal_receipt_missing_binding")
            if metadata.get("review_expect") and (row.review_receipt or {}).get("recorded") is not True:
                raise RunReportError("terminal_receipt_missing_review")
    return {
        "tenant_id": row.org_id,
        "engine_node_id": row.node_id,
        "engine_attempt": row.attempt,
        "status": status,
        "status_source": "authenticated_run_report",
    }
