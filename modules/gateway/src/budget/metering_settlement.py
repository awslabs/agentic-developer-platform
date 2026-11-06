"""Settle a verified final journal version against one authoritative SQL debit."""

import asyncio
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal

import rfc8785
from sqlalchemy import select, text

from pricing_policy import RoutingEvidence, verify_pricing_decision
from pricing_policy.policy import PricingDecision
from src.budget.metering_admission import MeteringAdmission, MeteringAdmissionError
from src.budget.metering_reconciliation import (
    MeteringReconciliationCommand,
    check_reconciliation_command,
    record_reconciliation_command,
)
from src.budget.settlement import settle_usage
from src.orchestration.provider_quotes import adapter_for
from src.shared.models.budget import BudgetSettlementReceipt
from src.shared.models.metering import MeteringAttempt, MeteringScopeGate
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.metering_journal import ImmutableMeteringJournal, MeteringObjectReceipt
from src.usage.service import UsageService


def _matches_usage(existing: UsageLog, attempt: MeteringAttempt, decision: dict, amount) -> bool:
    usage = decision["usage"]
    return (
        existing.user_id == attempt.owner_id
        and existing.cost_usd == amount
        and existing.input_tokens == usage["uncached_input_tokens"]
        and existing.output_tokens == usage["output_tokens"]
        and existing.pricing_decision is not None
        and rfc8785.dumps(existing.pricing_decision) == rfc8785.dumps(decision)
    )


async def settle_metering_receipt(
    session_factory,
    journal: ImmutableMeteringJournal,
    *,
    receipt: MeteringObjectReceipt,
    admission: MeteringObjectReceipt,
    scope_keys: tuple[str, ...],
    lock_timeout_ms: int | None = None,
    admin_command: MeteringReconciliationCommand | None = None,
) -> bool | dict:
    """Verify S3 before SQL; gate/attempt/receipt/usage/settled commit together."""
    final = await journal.read_final(receipt, deadline=asyncio.get_running_loop().time() + 5)
    if (
        receipt.tenant_id != admission.tenant_id
        or receipt.request_id != admission.request_id
        or receipt.owner_id != admission.owner_id
        or final.get("admission_sha256") != admission.sha256
        or final.get("admission_version_id") != admission.version_id
        or not isinstance(final.get("usage"), dict)
        or not isinstance(final.get("pricing_decision"), dict)
        or type(final.get("latency_ms")) is not int
        or final["latency_ms"] < 0
    ):
        raise MeteringAdmissionError("final receipt does not bind acknowledged admission and measured usage")
    completed = datetime.fromisoformat(final["completed_at"])
    if completed.tzinfo is None or completed.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=1):
        raise MeteringAdmissionError("final completion timestamp is invalid")
    adapter = adapter_for("/openai/v1/responses")
    trusted = await adapter.reconcile({"usage": final["usage"]}) if adapter is not None else None
    if trusted is None or not trusted.known:
        raise MeteringAdmissionError("final receipt does not contain trusted usage")
    payload = final["pricing_decision"]
    amount = verify_pricing_decision(payload, request_id=receipt.request_id, org_id=receipt.tenant_id)
    routing_data = payload.get("routing")
    if not isinstance(routing_data, dict):
        raise MeteringAdmissionError("final routing evidence is unavailable")
    routing = RoutingEvidence(**{field.name: routing_data[field.name] for field in fields(RoutingEvidence) if field.name in routing_data})
    decision = PricingDecision(**{**payload, "variant_key": tuple(payload["variant_key"]), "estimate_reasons": tuple(payload["estimate_reasons"])})
    async with session_factory() as session:
        if lock_timeout_ms is not None:
            if type(lock_timeout_ms) is not int or not 0 < lock_timeout_ms <= 2000:
                raise ValueError("invalid recovery lock timeout")
            await session.execute(text("SELECT set_config('lock_timeout', :duration, true)"), {"duration": f"{lock_timeout_ms}ms"})
            await session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
        for scope_key in sorted(scope_keys):
            gate = await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == scope_key).with_for_update())
            if gate is None:
                raise MeteringAdmissionError("settlement gate unavailable")
        row = await session.scalar(
            select(MeteringAttempt)
            .where(MeteringAttempt.tenant_id == receipt.tenant_id, MeteringAttempt.request_id == receipt.request_id)
            .with_for_update()
        )
        if row is None:
            raise MeteringAdmissionError("final receipt conflicts with durable attempt")
        if admin_command is not None:
            if (
                admin_command.action != "apply_trusted_receipt"
                or admin_command.evidence_ref != receipt.key
                or admin_command.evidence_sha256 != receipt.sha256
            ):
                raise MeteringAdmissionError("administrator evidence does not match the verified final receipt")
            replay = await check_reconciliation_command(session, row, admin_command)
            if replay is not None:
                return replay
        allowed_states = {"dispatch_armed", "unknown", "quarantined", "settled"}
        if admin_command is not None:
            allowed_states.add("cancelled_unsubmitted")
        if (
            row.owner_id != receipt.owner_id
            or row.state not in allowed_states
            or row.admission_version != admission.version_id
            or row.admission_sha256 != admission.sha256
            or row.final_key != receipt.key
            or (row.final_version is None and admin_command is None)
            or (row.final_version is not None and (row.final_version, row.final_sha256) != (receipt.version_id, receipt.sha256))
            or row.attribution_snapshot.get("scopes") != list(sorted(scope_keys))
            or row.quote_snapshot.get("billing_model_id") != routing.billing_model_id
            or row.bound_usd != Decimal(str(row.quote_snapshot["total_usd"])).quantize(Decimal("0.000001"), rounding=ROUND_UP)
            or amount > Decimal(str(row.quote_snapshot.get("upper_bound_usd") or row.bound_usd))
            or completed < row.created_at
        ):
            raise MeteringAdmissionError("final receipt conflicts with durable attempt")
        expected = MeteringAdmission.price_final(
            pricing_basis=row.quote_snapshot["pricing_basis"],
            usage=final["usage"],
            routing=routing,
            request_id=row.request_id,
            tenant_id=row.tenant_id,
            bound=Decimal(str(row.quote_snapshot.get("upper_bound_usd") or row.bound_usd)),
        )
        if rfc8785.dumps(expected.to_dict()) != rfc8785.dumps(payload):
            raise MeteringAdmissionError("final receipt disagrees with admitted historical prices")
        attribution = row.attribution_snapshot
        bindings = attribution["scope_bindings"]
        entities = sorted({(kind, identity) for kind, identity in bindings.values() if kind != "person"})
        project_scope = None
        project_keys = []
        if attribution.get("project_attribution"):
            from src.budget.project_budget import project_target
            from src.products.work_scope import ProjectAttribution

            project_scope = ProjectAttribution(**attribution["project_attribution"])
            if project_scope.tenant_id != row.tenant_id:
                raise MeteringAdmissionError("Project attribution tenant mismatch")
            entities.append(("project", project_scope.project_id))
            project_keys = [project_target(row.tenant_id, project_scope.project_id).key()]
        tokens = sum(
            payload["usage"][key] for key in ("uncached_input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        if row.state == "cancelled_unsubmitted":
            prior = row.revision
            row.state = "quarantined"
            row.reconciliation_status = "contradictory_receipt"
            row.final_version, row.final_sha256 = receipt.version_id, receipt.sha256
            row.revision += 1
            result = await record_reconciliation_command(
                session, row, admin_command, prior_revision=prior, receipt_sha256=receipt.sha256, scope_keys=scope_keys
            )
            await session.commit()
            return result
        if row.state == "settled":
            existing_receipt = await session.get(BudgetSettlementReceipt, (row.tenant_id, row.request_id))
            usage_row = await session.scalar(select(UsageLog).where(UsageLog.org_id == row.tenant_id, UsageLog.request_id == row.request_id).limit(1))
            if (
                existing_receipt is None
                or usage_row is None
                or existing_receipt.cost_usd != amount
                or not _matches_usage(usage_row, row, payload, amount)
            ):
                raise MeteringAdmissionError("settled attempt has incomplete or conflicting ledger")
            if admin_command is None:
                return False
            prior = row.revision
            row.revision += 1
            result = await record_reconciliation_command(
                session, row, admin_command, prior_revision=prior, receipt_sha256=receipt.sha256, scope_keys=scope_keys
            )
            await session.commit()
            return result
        await settle_usage(
            session,
            org_id=row.tenant_id,
            request_id=row.request_id,
            user_id=row.owner_id,
            cost=amount,
            total_tokens=tokens,
            entities=entities,
            timestamp=completed,
            verified_provider_usage=True,
            reservation_scope_keys=project_keys,
        )
        existing_usage = await session.scalar(
            select(UsageLog).where(UsageLog.org_id == row.tenant_id, UsageLog.request_id == row.request_id).limit(1)
        )
        if existing_usage is not None:
            if not _matches_usage(existing_usage, row, payload, amount):
                raise MeteringAdmissionError("conflicting diagnostic usage row")
        else:
            context = TokenContext(
                user_id=row.owner_id,
                org_id=row.tenant_id,
                attributed_user_id=attribution.get("root_user_id") or row.owner_id,
                attributed_org_id=row.tenant_id,
                account_type=attribution["account_type"],
                team_id=attribution["team_id"],
                department_id=attribution["department_id"],
                expires_at=completed + timedelta(hours=1),
            )
            context._project_attribution = project_scope
            context._budget_request_timestamp = completed
            await UsageService(session).log_request(
                context=context,
                model=row.model_id,
                input_tokens=payload["usage"]["uncached_input_tokens"],
                output_tokens=payload["usage"]["output_tokens"],
                cost_usd=amount,
                latency_ms=final["latency_ms"],
                status_code=200,
                request_id=row.request_id,
                agent_run_id=project_scope.run_id if project_scope else None,
                cache_read_input_tokens=final["usage"].get("cache_read_input_tokens"),
                cache_creation_input_tokens=final["usage"].get("cache_creation_input_tokens"),
                provider_request_id=final.get("provider_request_id"),
                pricing_decision=decision,
                settlement_already_claimed=True,
                commit=False,
            )
        prior = row.revision
        row.final_version, row.final_sha256 = receipt.version_id, receipt.sha256
        row.state = "settled"
        row.reconciliation_status = "settled"
        row.revision += 1
        result = (
            await record_reconciliation_command(
                session, row, admin_command, prior_revision=prior, receipt_sha256=receipt.sha256, scope_keys=scope_keys
            )
            if admin_command is not None
            else True
        )
        await session.commit()
        return result
