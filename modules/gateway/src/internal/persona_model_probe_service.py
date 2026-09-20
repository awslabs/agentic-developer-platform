"""Durable, at-most-once admission for paid persona-model probes."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.persona_models.catalogue import PLATFORM_MODEL_CATALOGUE
from src.admin.persona_models.request_shape_manifest import expected_request_shape, request_shape_manifest
from src.proxy.bedrock_routing import BedrockTarget
from src.proxy.bedrock_signing import DestinationCredentials, bedrock_destination_signer
from src.shared.config import Settings, get_settings
from src.shared.models.bedrock_routing import BedrockDestinationRegistry
from src.shared.models.persona_model_catalogue import (
    ModelInvocabilityEvidence,
    ModelProbeCycle,
    ModelProbeSlot,
)

Trigger = Literal["scheduled", "manual", "change"]
Outcome = Literal["proven", "refused", "error"]


class ProbeConflictError(Exception):
    """The requested transition is stale, invalid, or a conflicting replay."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    reason: str | None = None
    slot: ModelProbeSlot | None = None
    lease_token: str | None = None


@dataclass(frozen=True)
class StartedProbe:
    slot: ModelProbeSlot
    credentials: DestinationCredentials


@dataclass(frozen=True)
class CompletionResult:
    slot: ModelProbeSlot
    evidence_recorded: bool


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _lease_token_sha256(lease_token: str) -> str:
    return hashlib.sha256(lease_token.encode()).hexdigest()


def _verify_lease_token(slot: ModelProbeSlot, lease_token: str) -> None:
    # Compare fixed-length digests so verification remains constant-time even
    # when the attacker supplies a token with a different length.
    if not hmac.compare_digest(slot.lease_token_sha256, _lease_token_sha256(lease_token)):
        raise ProbeConflictError("invalid_lease_token", "Probe lease token is invalid")


def _configuration_reason(settings: Settings) -> str | None:
    if not settings.model_probe_enabled:
        return "disabled"
    if settings.model_probe_max_slots_per_cycle <= 0:
        return "zero_slots"
    if settings.model_probe_budget_usd_per_cycle <= 0 or settings.model_probe_max_budget_usd_per_attempt <= 0:
        return "zero_budget"
    if not re.fullmatch(r"[0-9]{12}", settings.platform_bedrock_account_id):
        return "platform_account_unconfigured"
    return None


def _cycle_key(now: datetime) -> str:
    # All trigger types share the same daily spend envelope. Otherwise repeated
    # `manual` claims could manufacture new cycles and bypass the ceiling.
    manifest_fingerprint = hashlib.sha256(json.dumps(request_shape_manifest(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{now.date().isoformat()}:{manifest_fingerprint}"


async def _lock_cycle_namespace(db: AsyncSession, cycle_key: str) -> None:
    """Serialize absent-row creation on Postgres; row locks handle later claims."""
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": cycle_key})


async def _expire_leases(db: AsyncSession, now: datetime) -> None:
    rows = (
        await db.scalars(
            select(ModelProbeSlot)
            .where(
                ModelProbeSlot.status.in_(("reserved", "started")),
                ModelProbeSlot.lease_expires_at <= now,
            )
            .with_for_update()
        )
    ).all()
    for slot in rows:
        if slot.status == "started":
            # Never return a started target to the candidate pool: provider spend
            # may have happened even when the worker died before completion.
            slot.status = "indeterminate"
            slot.indeterminate_reason = "started_slot_lease_expired"
        else:
            slot.status = "expired"
            slot_cycle = await db.get(ModelProbeCycle, slot.cycle_id)
            if slot_cycle is not None:
                slot_cycle.reserved_usd -= slot.reserved_budget_usd
                slot_cycle.updated_at = now
        slot.updated_at = now


async def claim_probe(db: AsyncSession, *, trigger: Trigger = "scheduled") -> ClaimResult:
    """Atomically reserve a Gateway-selected destination/model and worst-case spend."""
    settings = get_settings()
    if reason := _configuration_reason(settings):
        return ClaimResult(claimed=False, reason=reason)

    now = datetime.now(UTC)
    cycle_key = _cycle_key(now)
    await _lock_cycle_namespace(db, cycle_key)
    cycle = await db.scalar(select(ModelProbeCycle).where(ModelProbeCycle.cycle_key == cycle_key).with_for_update())
    if cycle is None:
        cycle = ModelProbeCycle(
            id=str(uuid.uuid4()),
            cycle_key=cycle_key,
            trigger=trigger,
            status="active",
            max_slots=settings.model_probe_max_slots_per_cycle,
            budget_usd=settings.model_probe_budget_usd_per_cycle,
            reserved_usd=Decimal("0"),
            started_usd=Decimal("0"),
            created_at=now,
            expires_at=now + timedelta(hours=settings.model_probe_cycle_ttl_hours),
            updated_at=now,
        )
        db.add(cycle)
        await db.flush()
    elif cycle.status != "active" or _utc(cycle.expires_at) <= now:
        if cycle.status == "active":
            cycle.status = "expired"
            cycle.updated_at = now
        await db.commit()
        return ClaimResult(claimed=False, reason="cycle_budget_exhausted")

    await _expire_leases(db, now)
    slot_count = await db.scalar(
        select(func.count())
        .select_from(ModelProbeSlot)
        .where(
            ModelProbeSlot.cycle_id == cycle.id,
            ModelProbeSlot.status.in_(("reserved", "started", "completed", "indeterminate")),
        )
    )
    attempt_budget = settings.model_probe_max_budget_usd_per_attempt
    if (slot_count or 0) >= cycle.max_slots or cycle.reserved_usd + attempt_budget > cycle.budget_usd:
        await db.commit()
        return ClaimResult(claimed=False, reason="cycle_budget_exhausted")

    destinations = (
        await db.scalars(
            select(BedrockDestinationRegistry)
            .where(
                BedrockDestinationRegistry.routing_capable.is_(True),
                BedrockDestinationRegistry.verified_at.is_not(None),
                # Customer probing needs its own consent contract. This rollout
                # may qualify only the operator's configured platform account.
                BedrockDestinationRegistry.account_id == settings.platform_bedrock_account_id,
                BedrockDestinationRegistry.is_platform_registered.is_(True),
                BedrockDestinationRegistry.owner_org_id.is_(None),
                BedrockDestinationRegistry.credential_id.is_(None),
            )
            .order_by(BedrockDestinationRegistry.account_id, BedrockDestinationRegistry.region, BedrockDestinationRegistry.id)
        )
    ).all()

    candidate: tuple[BedrockDestinationRegistry, object] | None = None
    for destination in destinations:
        for model in PLATFORM_MODEL_CATALOGUE:
            expected_digest = expected_request_shape(model.canonical_model_id)
            if expected_digest is None:
                continue
            paid_or_live_attempt = await db.scalar(
                select(ModelProbeSlot.id).where(
                    ModelProbeSlot.destination_id == destination.id,
                    ModelProbeSlot.canonical_model_id == model.canonical_model_id,
                    ModelProbeSlot.compatibility_class == model.compatibility_class,
                    ModelProbeSlot.harness_contract_revision == model.harness_contract_revision,
                    ModelProbeSlot.expected_request_shape_sha256 == expected_digest,
                    or_(
                        ModelProbeSlot.status.in_(("started", "indeterminate")),
                        ((ModelProbeSlot.status == "reserved") & (ModelProbeSlot.lease_expires_at > now)),
                    ),
                )
            )
            if paid_or_live_attempt:
                continue
            already_slotted = await db.scalar(
                select(ModelProbeSlot.id).where(
                    ModelProbeSlot.cycle_id == cycle.id,
                    ModelProbeSlot.destination_id == destination.id,
                    ModelProbeSlot.canonical_model_id == model.canonical_model_id,
                    ModelProbeSlot.compatibility_class == model.compatibility_class,
                    ModelProbeSlot.harness_contract_revision == model.harness_contract_revision,
                    ModelProbeSlot.expected_request_shape_sha256 == expected_digest,
                )
            )
            if already_slotted:
                continue
            evidence = await db.get(
                ModelInvocabilityEvidence,
                (
                    destination.account_id,
                    destination.region,
                    model.canonical_model_id,
                    model.compatibility_class,
                    model.harness_contract_revision,
                    expected_digest,
                ),
            )
            if evidence is not None and _utc(evidence.expires_at) > now:
                continue
            candidate = (destination, model)
            break
        if candidate:
            break

    if candidate is None:
        await db.commit()
        return ClaimResult(claimed=False, reason="no_candidates")

    destination, model = candidate
    expected_digest = expected_request_shape(model.canonical_model_id)
    if expected_digest is None:  # guarded in the candidate loop; fail closed if the manifest changes mid-call
        await db.rollback()
        return ClaimResult(claimed=False, reason="no_candidates")
    lease_token = secrets.token_urlsafe(32)
    slot = ModelProbeSlot(
        id=str(uuid.uuid4()),
        cycle_id=cycle.id,
        destination_id=destination.id,
        account_id=destination.account_id,
        region=destination.region,
        canonical_model_id=model.canonical_model_id,
        compatibility_class=model.compatibility_class,
        harness_contract_revision=model.harness_contract_revision,
        expected_request_shape_sha256=expected_digest,
        reserved_budget_usd=attempt_budget,
        status="reserved",
        reserved_at=now,
        lease_expires_at=now + timedelta(seconds=max(settings.model_probe_slot_ttl_seconds, settings.model_probe_timeout_seconds + 30)),
        lease_token_sha256=_lease_token_sha256(lease_token),
        paid_attempt_started_at=None,
        completed_at=None,
        outcome=None,
        observed_request_shape_sha256=None,
        provider_request_id=None,
        error_code=None,
        completion_fingerprint=None,
        indeterminate_reason=None,
        updated_at=now,
    )
    db.add(slot)
    cycle.reserved_usd += attempt_budget
    cycle.updated_at = now
    await db.commit()
    return ClaimResult(claimed=True, slot=slot, lease_token=lease_token)


async def start_probe(
    db: AsyncSession,
    *,
    slot_id: str,
    lease_token: str,
    request_shape_sha256: str,
) -> StartedProbe:
    """Durably mark the paid attempt before releasing destination credentials."""
    now = datetime.now(UTC)
    slot = await db.scalar(select(ModelProbeSlot).where(ModelProbeSlot.id == slot_id).with_for_update())
    if slot is None:
        raise ProbeConflictError("slot_not_found", "Probe slot does not exist")
    _verify_lease_token(slot, lease_token)
    if slot.status != "reserved":
        raise ProbeConflictError("slot_not_reserved", f"Probe slot is {slot.status}, not reserved")
    settings = get_settings()
    if reason := _configuration_reason(settings):
        raise ProbeConflictError(reason, "Probe admission is currently unavailable")
    destination = await db.get(BedrockDestinationRegistry, slot.destination_id)
    if (
        destination is None
        or slot.account_id != settings.platform_bedrock_account_id
        or destination.account_id != slot.account_id
        or destination.region != slot.region
        or not destination.is_platform_registered
        or destination.owner_org_id is not None
        or destination.credential_id is not None
        or not destination.routing_capable
        or destination.verified_at is None
    ):
        raise ProbeConflictError("probe_destination_not_permitted", "Probe destination is not an admitted platform destination")
    if not re.fullmatch(r"[a-z0-9-]+", slot.region):
        raise ProbeConflictError("probe_region_invalid", "Probe region is malformed")
    if _utc(slot.lease_expires_at) <= now:
        slot.status = "expired"
        slot.updated_at = now
        cycle = await db.get(ModelProbeCycle, slot.cycle_id)
        if cycle is not None:
            cycle.reserved_usd -= slot.reserved_budget_usd
            cycle.updated_at = now
        await db.commit()
        raise ProbeConflictError("slot_expired", "Probe slot lease expired before start")
    if request_shape_sha256 != slot.expected_request_shape_sha256:
        raise ProbeConflictError("request_shape_mismatch", "Worker manifest does not match the admitted request shape")

    slot.status = "started"
    slot.paid_attempt_started_at = now
    slot.updated_at = now
    cycle = await db.scalar(select(ModelProbeCycle).where(ModelProbeCycle.id == slot.cycle_id).with_for_update())
    if cycle is None:
        raise ProbeConflictError("cycle_not_found", "Probe cycle no longer exists")
    cycle.started_usd += slot.reserved_budget_usd
    cycle.updated_at = now
    # This commit is deliberately before credential minting. Once credentials
    # can leave the Gateway, a crash must be treated as potentially paid.
    await db.commit()

    credentials = await bedrock_destination_signer.get_credentials(
        db,
        BedrockTarget(
            account_id=slot.account_id,
            rung="org",
            destination_id=slot.destination_id,
            region=slot.region,
        ),
        user_id="model-invocability-probe",
    )
    return StartedProbe(slot=slot, credentials=credentials)


def _completion_fingerprint(*, outcome: Outcome, request_shape_sha256: str, provider_request_id: str | None, error_code: str | None) -> str:
    payload = json.dumps(
        {
            "error_code": error_code,
            "outcome": outcome,
            "provider_request_id": provider_request_id,
            "request_shape_sha256": request_shape_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def complete_probe(
    db: AsyncSession,
    *,
    slot_id: str,
    lease_token: str,
    outcome: Outcome,
    request_shape_sha256: str,
    provider_request_id: str | None,
    error_code: str | None,
) -> CompletionResult:
    """Complete a started slot and upsert evidence using its immutable exact key."""
    now = datetime.now(UTC)
    fingerprint = _completion_fingerprint(
        outcome=outcome,
        request_shape_sha256=request_shape_sha256,
        provider_request_id=provider_request_id,
        error_code=error_code,
    )
    slot = await db.scalar(select(ModelProbeSlot).where(ModelProbeSlot.id == slot_id).with_for_update())
    if slot is None:
        raise ProbeConflictError("slot_not_found", "Probe slot does not exist")
    _verify_lease_token(slot, lease_token)
    if slot.status == "completed":
        if slot.completion_fingerprint != fingerprint:
            raise ProbeConflictError("conflicting_completion", "Slot was already completed with different evidence")
        return CompletionResult(
            slot=slot,
            evidence_recorded=not (slot.error_code or "").startswith("no_request_emitted"),
        )
    if slot.status != "started":
        raise ProbeConflictError("slot_not_started", f"Probe slot is {slot.status}, not started")
    if _utc(slot.lease_expires_at) <= now:
        slot.status = "indeterminate"
        slot.indeterminate_reason = "started_slot_lease_expired"
        slot.updated_at = now
        await db.commit()
        raise ProbeConflictError("slot_indeterminate", "Started slot expired; it will not be retried automatically")
    if outcome == "proven" and not provider_request_id:
        raise ProbeConflictError("provider_request_id_required", "Proven evidence requires a provider request ID")
    if request_shape_sha256 != slot.expected_request_shape_sha256 and outcome != "error":
        raise ProbeConflictError("request_shape_mismatch", "A shape mismatch can only be recorded as an error")

    no_request_emitted = outcome == "error" and bool(error_code) and error_code.startswith("no_request_emitted")
    if no_request_emitted:
        # The SDK never reached the capture proxy. The supplied digest is only
        # the manifest expectation, not an observation, so it must not become
        # invocability evidence of any outcome.
        slot.status = "completed"
        slot.completed_at = now
        slot.outcome = outcome
        slot.observed_request_shape_sha256 = None
        slot.provider_request_id = None
        slot.error_code = error_code
        slot.completion_fingerprint = fingerprint
        slot.updated_at = now
        await db.commit()
        return CompletionResult(slot=slot, evidence_recorded=False)

    key = (
        slot.account_id,
        slot.region,
        slot.canonical_model_id,
        slot.compatibility_class,
        slot.harness_contract_revision,
        request_shape_sha256,
    )
    evidence = await db.get(ModelInvocabilityEvidence, key)
    expires_at = now + timedelta(hours=get_settings().model_probe_evidence_ttl_hours)
    if evidence is None:
        evidence = ModelInvocabilityEvidence(
            account_id=slot.account_id,
            region=slot.region,
            canonical_model_id=slot.canonical_model_id,
            compatibility_class=slot.compatibility_class,
            harness_contract_revision=slot.harness_contract_revision,
            request_shape_sha256=request_shape_sha256,
            outcome=outcome,
            error_code=error_code,
            provider_request_id=provider_request_id,
            verified_at=now,
            expires_at=expires_at,
            updated_at=now,
        )
        db.add(evidence)
    else:
        evidence.outcome = outcome
        evidence.error_code = error_code
        evidence.provider_request_id = provider_request_id
        evidence.verified_at = now
        evidence.expires_at = expires_at
        evidence.updated_at = now

    slot.status = "completed"
    slot.completed_at = now
    slot.outcome = outcome
    slot.observed_request_shape_sha256 = request_shape_sha256
    slot.provider_request_id = provider_request_id
    slot.error_code = error_code
    slot.completion_fingerprint = fingerprint
    slot.updated_at = now
    await db.commit()
    return CompletionResult(slot=slot, evidence_recorded=True)
