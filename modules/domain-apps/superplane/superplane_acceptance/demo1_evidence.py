"""Versioned private inputs and provenance for the dedicated workspace scenario."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

VERSION = "demo1-v1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceError(ValueError):
    """Invalid input or evidence; messages never include private values."""


def fields(value: object, names: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != names:
        raise EvidenceError(f"{label}: missing or unexpected fields")
    return value


def text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value.strip() != value
    ):
        raise EvidenceError(f"{label}: invalid value")
    return value


def identifier(value: object, label: str) -> str:
    candidate = text(value, label)
    try:
        if candidate != str(UUID(candidate)):
            raise ValueError
    except ValueError as error:
        raise EvidenceError(f"{label}: invalid identifier") from error
    return candidate


def digest(value: object, label: str) -> str:
    candidate = text(value, label)
    if not SHA256.fullmatch(candidate):
        raise EvidenceError(f"{label}: invalid digest")
    return candidate


def source_revision(value: object) -> str:
    candidate = text(value, "release_source")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise EvidenceError("release_source: full Git revision required")
    return candidate


def instant(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text(value, label))
    except ValueError as error:
        raise EvidenceError(f"{label}: invalid timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError(f"{label}: timezone required")
    return parsed


@dataclass(frozen=True)
class DemoInput:
    release_source: str
    image_digest: str
    schema_revision: str
    connection_id: str
    role: str
    account: str
    region: str
    org_id: str
    requester_id: str
    approver_id: str
    workspace_name: str
    request_id: str
    plan_revision: str
    budget_usd: Decimal
    authorized_at: datetime
    deadline: datetime
    cleanup_owner: str
    recovery_checkpoint: str
    survivors: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> DemoInput:
        selected = fields(
            value,
            {
                "version",
                "release_source",
                "image_digest",
                "schema_revision",
                "connection_id",
                "role",
                "account",
                "region",
                "org_id",
                "requester_id",
                "approver_id",
                "workspace_name",
                "mode",
                "cluster_placement",
                "request_id",
                "plan_revision",
                "budget_usd",
                "authorized_at",
                "deadline",
                "cleanup_owner",
                "recovery_checkpoint",
                "survivors",
            },
            "input",
        )
        if selected["version"] != VERSION:
            raise EvidenceError("input: unsupported version")
        if (
            selected["mode"] != "managed"
            or selected["cluster_placement"] != "dedicated"
        ):
            raise EvidenceError("input: only managed dedicated placement is supported")
        account = text(selected["account"], "account")
        if not re.fullmatch(r"[0-9]{12}", account):
            raise EvidenceError("account: invalid selection")
        try:
            budget = Decimal(text(selected["budget_usd"], "budget_usd"))
        except InvalidOperation as error:
            raise EvidenceError("budget_usd: invalid amount") from error
        if not budget.is_finite() or budget <= 0:
            raise EvidenceError("budget_usd: finite positive limit required")
        authorized = instant(selected["authorized_at"], "authorized_at")
        deadline = instant(selected["deadline"], "deadline")
        if deadline <= authorized:
            raise EvidenceError("deadline: must follow authorization")
        survivors = selected["survivors"]
        if not isinstance(survivors, list) or not survivors:
            raise EvidenceError("survivors: baseline required")
        baseline = tuple(text(item, "survivor") for item in survivors)
        if len(set(baseline)) != len(baseline):
            raise EvidenceError("survivors: duplicate baseline")
        requester = identifier(selected["requester_id"], "requester_id")
        approver = identifier(selected["approver_id"], "approver_id")
        if requester == approver:
            raise EvidenceError("approver_id: distinct human required")
        return cls(
            release_source=source_revision(selected["release_source"]),
            image_digest=digest(selected["image_digest"], "image_digest"),
            schema_revision=text(selected["schema_revision"], "schema_revision"),
            connection_id=identifier(selected["connection_id"], "connection_id"),
            role=text(selected["role"], "role"),
            account=account,
            region=text(selected["region"], "region"),
            org_id=identifier(selected["org_id"], "org_id"),
            requester_id=requester,
            approver_id=approver,
            workspace_name=text(selected["workspace_name"], "workspace_name"),
            request_id=identifier(selected["request_id"], "request_id"),
            plan_revision=digest(selected["plan_revision"], "plan_revision"),
            budget_usd=budget,
            authorized_at=authorized,
            deadline=deadline,
            cleanup_owner=text(selected["cleanup_owner"], "cleanup_owner"),
            recovery_checkpoint=text(
                selected["recovery_checkpoint"], "recovery_checkpoint"
            ),
            survivors=baseline,
        )


@dataclass(frozen=True)
class PhaseEvidence:
    workspace_id: str
    request_id: str
    phase_name: str
    operation_id: str
    approval_id: str
    plan_revision: str
    artifact_digest: str
    attempt_id: str
    fence: str
    admitted_at: datetime
    observed_at: datetime
    source: str
    state: str

    @classmethod
    def parse(cls, value: object, selected: DemoInput) -> PhaseEvidence:
        phase = fields(
            value,
            {
                "workspace_id",
                "request_id",
                "phase_name",
                "operation_id",
                "approval_id",
                "plan_revision",
                "artifact_digest",
                "attempt_id",
                "fence",
                "admitted_at",
                "observed_at",
                "approval",
                "source",
                "state",
            },
            "phase",
        )
        if phase["phase_name"] not in ("creation", "bootstrap", "retirement"):
            raise EvidenceError("phase: unsupported lifecycle phase")
        if phase["source"] not in ("fixture", "domain", "provider"):
            raise EvidenceError("phase: invalid source")
        if phase["state"] not in ("pending", "unknown", "failed", "observed"):
            raise EvidenceError("phase: invalid state")
        observed = instant(phase["observed_at"], "observed_at")
        if not selected.authorized_at <= observed <= selected.deadline:
            raise EvidenceError("phase: outside authorized window")
        admitted = instant(phase["admitted_at"], "admission time")
        if not selected.authorized_at <= admitted <= observed:
            raise EvidenceError("phase: admission outside authorized window")
        approval = fields(
            phase["approval"],
            {
                "approval_id",
                "requester",
                "approvers",
                "result",
                "decided_by",
                "decided_at",
                "expires_at",
                "revoked",
            },
            "approval excerpt",
        )
        if (
            identifier(approval["approval_id"], "approval_id") != phase["approval_id"]
            or identifier(approval["requester"], "approval requester")
            != selected.requester_id
            or approval["result"] != "allowed-once"
            or identifier(approval["decided_by"], "approver") != selected.approver_id
            or not isinstance(approval["approvers"], list)
            or selected.approver_id not in approval["approvers"]
            or approval["revoked"] is not False
        ):
            raise EvidenceError("phase: approval decision or binding refused")
        decided = instant(approval["decided_at"], "approval decision")
        expires = instant(approval["expires_at"], "approval expiry")
        if not selected.authorized_at <= decided <= admitted < expires:
            raise EvidenceError("phase: approval expired or decision not yet made")
        request = identifier(phase["request_id"], "request_id")
        plan = digest(phase["plan_revision"], "plan_revision")
        if request != selected.request_id or plan != selected.plan_revision:
            raise EvidenceError("phase: selected request or plan mismatch")
        return cls(
            identifier(phase["workspace_id"], "workspace_id"),
            request,
            phase["phase_name"],
            identifier(phase["operation_id"], "operation_id"),
            identifier(phase["approval_id"], "approval_id"),
            plan,
            digest(phase["artifact_digest"], "artifact_digest"),
            text(phase["attempt_id"], "attempt_id"),
            text(phase["fence"], "fence"),
            admitted,
            observed,
            phase["source"],
            phase["state"],
        )


def link_phases(
    selected: DemoInput, records: list[object]
) -> tuple[PhaseEvidence, ...]:
    if not records:
        raise EvidenceError("phase: missing records")
    phases = tuple(PhaseEvidence.parse(record, selected) for record in records)
    if len({phase.workspace_id for phase in phases}) != 1:
        raise EvidenceError("phase: workspace mismatch")
    if len({(phase.operation_id, phase.attempt_id) for phase in phases}) != len(
        phases
    ) or len({(phase.operation_id, phase.fence) for phase in phases}) != len(phases):
        raise EvidenceError("phase: replayed attempt")
    operations = {}
    approvals = {}
    for phase in phases:
        if (
            phase.approval_id in approvals
            and approvals[phase.approval_id] != phase.operation_id
        ):
            raise EvidenceError("phase: approval reused across operations")
        approvals[phase.approval_id] = phase.operation_id
        lineage = (phase.phase_name, phase.approval_id, phase.artifact_digest)
        if (
            phase.operation_id in operations
            and operations[phase.operation_id] != lineage
        ):
            raise EvidenceError(
                "phase: operation authority or artifact changed on retry"
            )
        operations[phase.operation_id] = lineage
    if len({phase.phase_name for phase in phases}) != len(
        {phase.operation_id for phase in phases}
    ):
        raise EvidenceError("phase: duplicate operation for one lifecycle phase")
    if [phase.observed_at for phase in phases] != sorted(
        phase.observed_at for phase in phases
    ):
        raise EvidenceError("phase: unordered observations")
    return phases
