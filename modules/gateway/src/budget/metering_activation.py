"""Fail-closed gate staging and rollback for an opt-in metering cohort."""

import os

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.shared.models.budget import BudgetSettlementReceipt
from src.shared.models.metering import MeteringAttempt, MeteringAttemptScope, MeteringLegacyAdmission, MeteringScopeGate


async def transition_metering_gate(session, *, scope_key: str, scope_kind: str, tenant_id: str, activation: str) -> None:
    if not scope_key or not tenant_id or scope_kind not in {"org", "user", "agent", "team", "department", "root_user"}:
        raise ValueError("gate transition requires a tenant-bound scope")
    if activation not in {"legacy", "draining"}:
        raise ValueError("active metering rollout requires compatible mutation writers and a physical-send guard")
    if activation == "draining" and os.environ.get("BG_METERING_GATE_DRAIN_ENABLED") != "true":
        raise ValueError("metering drain requires the opt-in provider admission fence")
    if session.bind.dialect.name != "postgresql" or await session.scalar(text("SELECT to_regclass('metering_scope_gates')")) is None:
        raise ValueError("metering gate transition requires the durable PostgreSQL schema")

    await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"metering-gates:tenant:{tenant_id}"})
    gate = await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == scope_key).with_for_update())
    if gate is None:
        gate = MeteringScopeGate(scope_key=scope_key, scope_kind=scope_kind, tenant_id=tenant_id, activation=activation)
        session.add(gate)
        await session.flush()
        return
    if gate.scope_kind != scope_kind or gate.tenant_id != tenant_id:
        raise ValueError("metering gate scope identity cannot change")
    if gate.activation == activation:
        return
    if gate.activation == "active" and activation == "legacy":
        raise ValueError("active metering gate must drain before rollback")
    if activation == "legacy":
        unresolved = await session.scalar(
            select(
                exists().where(
                    MeteringAttemptScope.scope_key == scope_key,
                    MeteringAttemptScope.tenant_id == MeteringAttempt.tenant_id,
                    MeteringAttemptScope.request_id == MeteringAttempt.request_id,
                    MeteringAttempt.state.in_(("prepared", "dispatch_armed", "unknown", "quarantined")),
                )
            )
        )
        if unresolved or await session.scalar(
            select(
                exists().where(
                    MeteringLegacyAdmission.state == "pending",
                    or_(MeteringLegacyAdmission.tenant_id == tenant_id, MeteringLegacyAdmission.authenticated_tenant_id == tenant_id),
                )
            )
        ):
            raise ValueError("metering gate rollback retains unresolved exposure")
        if gate.activation == "draining" and os.environ.get("BG_METERING_GATE_DRAIN_ENABLED") != "true":
            raise ValueError("metering gate rollback requires the compatible provider admission fence")
    gate.activation = activation
    gate.revision += 1


async def require_non_draining_admission(session, *, tenant_id: str) -> None:
    if session.bind.dialect.name != "postgresql":
        return
    if await session.scalar(text("SELECT to_regclass('metering_scope_gates')")) is None:
        return
    await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"metering-gates:tenant:{tenant_id}"})
    if await session.scalar(
        select(MeteringScopeGate.scope_key)
        .where(MeteringScopeGate.tenant_id == tenant_id, MeteringScopeGate.activation.in_(("draining", "active")))
        .limit(1)
    ):
        raise ValueError("metering gate is not legacy; new provider admissions are unavailable")


async def record_legacy_cohort_admission(session, *, tenant_id: str, authenticated_tenant_id: str, request_id: str, owner_id: str) -> None:
    if not all((tenant_id, authenticated_tenant_id, request_id, owner_id)) or session.bind.dialect.name != "postgresql":
        raise ValueError("durable legacy cohort admission requires scoped PostgreSQL identity")
    if await session.scalar(text("SELECT to_regclass('metering_legacy_admissions')")) is None:
        raise ValueError("durable legacy cohort admission schema is unavailable")
    claimed = await session.scalar(
        pg_insert(MeteringLegacyAdmission)
        .values(tenant_id=tenant_id, authenticated_tenant_id=authenticated_tenant_id, request_id=request_id, owner_id=owner_id)
        .on_conflict_do_nothing(index_elements=["tenant_id", "request_id"])
        .returning(MeteringLegacyAdmission.request_id)
    )
    if claimed is None:
        raise ValueError("legacy request admission already exists; retry must not resubmit provider work")


async def mark_legacy_cohort_settled(session, *, tenant_id: str, request_id: str, owner_id: str) -> None:
    if session.bind.dialect.name != "postgresql" or await session.scalar(text("SELECT to_regclass('metering_legacy_admissions')")) is None:
        return
    row = await session.get(MeteringLegacyAdmission, (tenant_id, request_id), with_for_update=True)
    if row is not None:
        if row.owner_id != owner_id:
            raise ValueError("legacy cohort settlement owner mismatch")
        if row.state == "not_dispatched":
            raise ValueError("legacy cohort has contradictory financial evidence")
        row.state = "settled"


async def mark_legacy_cohort_not_dispatched(session, *, tenant_id: str, request_id: str, owner_id: str, disposition: str) -> None:
    if disposition not in {"budget_denied", "native_header_denied", "pre_provider_return"}:
        raise ValueError("unknown legacy admission disposition")
    row = await session.get(MeteringLegacyAdmission, (tenant_id, request_id), with_for_update=True)
    if row is None or row.owner_id != owner_id:
        raise ValueError("legacy cohort disposition identity mismatch")
    if row.state != "pending":
        return
    attempt_exists = await session.scalar(select(exists().where(MeteringAttempt.tenant_id == tenant_id, MeteringAttempt.request_id == request_id)))
    if attempt_exists or await session.scalar(
        select(exists().where(BudgetSettlementReceipt.org_id == tenant_id, BudgetSettlementReceipt.request_id == request_id))
    ):
        return
    row.state = "not_dispatched"
    row.disposition = disposition
    row.disposed_at = await session.scalar(select(func.now()))
