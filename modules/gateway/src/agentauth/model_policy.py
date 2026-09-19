"""Gateway-authoritative model-policy snapshots for agent chains (PMM-06).

The queue envelope deliberately carries no policy data.  It is sealed before
work admission, so changing it afterwards would invalidate the protected
envelope digest.  Instead, admission stores one canonical snapshot on the
worker-unwritable execution record and descendants inherit that snapshot from
their protected parent record.

This module implements the report-only half of the contract.  It computes and
signs the decision the gateway would enforce, but it does not change the model
the legacy worker actually invokes.  PMM-07 owns live admission and enforcement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.persona_models._personas import VALID_PERSONAS
from src.admin.persona_models.catalogue import (
    HARNESS_CONTRACT_REVISION,
    PERSONA_ALLOWED_PATTERNS,
    PLATFORM_MODEL_CATALOGUE,
    persona_compatibility_class,
)
from src.admin.persona_models.catalogue_schemas import SelectionRejection
from src.admin.persona_models.catalogue_service import validate_selection
from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, EnvelopeError, sign_envelope
from src.agentauth.grants import AUTHORITY_GATE_DECISION, AUTHORITY_GITHUB_EVENT, AUTHORITY_SERVICE_POLICY, DelegatedGrant
from src.agentauth.store import AuthorityStoreError
from src.shared.config import get_settings
from src.shared.identity.resolver import resolve_root_user_entity_id
from src.shared.models.organization import User
from src.shared.models.persona_models import (
    ALIAS_SOURCES,
    PersonaModelPolicySetting,
    PersonaModelPreference,
    ServicePrincipal,
    ServicePrincipalAlias,
)
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.agentauth.model_policy")

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_AUDIENCE = "adp-agent-model-policy"
SNAPSHOT_SOURCE_LIVE = "live"
MAX_SNAPSHOT_BYTES = 128 * 1024
LKG_CACHE_TTL_ENV = "AGENT_MODEL_POLICY_LKG_TTL_SECONDS"
DEFAULT_LKG_CACHE_TTL_SECONDS = 300
MAX_LKG_CACHE_TTL_SECONDS = 900


class ModelPolicyError(Exception):
    """A snapshot or decision was unavailable or invalid."""

    def __init__(self, reason: str, *, evidence: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ModelPolicyError("snapshot_malformed")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ModelPolicyError("snapshot_malformed") from None


def _lkg_ttl_seconds() -> int:
    try:
        configured = int(os.environ.get(LKG_CACHE_TTL_ENV, DEFAULT_LKG_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        configured = DEFAULT_LKG_CACHE_TTL_SECONDS
    return max(0, min(configured, MAX_LKG_CACHE_TTL_SECONDS))


def canonical_json(value: dict) -> bytes:
    """Return the one byte representation used for storage and hashing."""
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    except (TypeError, ValueError):
        raise ModelPolicyError("snapshot_malformed") from None
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ModelPolicyError("snapshot_too_large")
    return raw


def policy_digest(value: dict) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _active_allowlist_policy_revision(
    *,
    tenant_patterns: list[str] | None,
    tenant_policy_source: str,
    service_restriction_pattern_sets: list[list[str]],
    service_policy_unavailable_reason: str | None,
    service_principal_status: str | None,
) -> str:
    """Hash the canonical policy intersection applied at one point in time."""
    effective_tenant_patterns = list(PERSONA_ALLOWED_PATTERNS) if tenant_patterns is None else tenant_patterns
    canonical_service_sets = sorted(
        (sorted(set(patterns)) for patterns in service_restriction_pattern_sets),
        key=lambda patterns: canonical_json({"patterns": patterns}),
    )
    return policy_digest(
        {
            "tenant_policy_source": tenant_policy_source,
            "tenant_patterns": sorted(set(effective_tenant_patterns)),
            "service_restriction_pattern_sets": canonical_service_sets,
            "service_policy_state": service_policy_unavailable_reason or "available",
            "service_principal_status": service_principal_status or "not_applicable",
        }
    )


@dataclass(frozen=True)
class ModelPolicySnapshot:
    schema_version: int
    tenant_id: str
    principal_kind: Literal["human", "service_account"]
    principal_id: str
    mappings: dict[str, str]
    class_defaults: dict[str, dict[str, object]]
    persona_contracts: dict[str, dict[str, str]]
    policy_revision: str
    allowlist_policy_revision: str
    catalogue_revision: str
    correlation_id: str
    root_invocation_id: str
    issued_at: datetime
    expires_at: datetime
    audience: str
    source: Literal["live", "last_known_good_cache"]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "principal_kind": self.principal_kind,
            "principal_id": self.principal_id,
            "mappings": dict(sorted(self.mappings.items())),
            "class_defaults": {key: self.class_defaults[key] for key in sorted(self.class_defaults)},
            "persona_contracts": {key: self.persona_contracts[key] for key in sorted(self.persona_contracts)},
            "policy_revision": self.policy_revision,
            "allowlist_policy_revision": self.allowlist_policy_revision,
            "catalogue_revision": self.catalogue_revision,
            "correlation_id": self.correlation_id,
            "root_invocation_id": self.root_invocation_id,
            "issued_at": _iso(self.issued_at),
            "expires_at": _iso(self.expires_at),
            "audience": self.audience,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: dict) -> ModelPolicySnapshot:
        try:
            if not isinstance(value, dict):
                raise ModelPolicyError("snapshot_malformed")
            if value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
                raise ModelPolicyError("snapshot_unsupported_revision")
            tenant_id = value["tenant_id"]
            principal_kind = value["principal_kind"]
            principal_id = value["principal_id"]
            mappings = value["mappings"]
            class_defaults = value["class_defaults"]
            persona_contracts = value["persona_contracts"]
            if value.get("audience") != SNAPSHOT_AUDIENCE:
                raise ModelPolicyError("snapshot_audience_mismatch")
            scalar_fields = (
                value.get("policy_revision"),
                value.get("allowlist_policy_revision"),
                value.get("catalogue_revision"),
                value.get("correlation_id"),
                value.get("root_invocation_id"),
            )
            if (
                not all(isinstance(item, str) and item for item in (tenant_id, principal_id))
                or not all(isinstance(item, str) and item for item in scalar_fields)
                or principal_kind not in {"human", "service_account"}
                or not isinstance(mappings, dict)
                or not isinstance(class_defaults, dict)
                or not isinstance(persona_contracts, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in mappings.items())
                or value.get("source") not in {"live", "last_known_good_cache"}
            ):
                raise ModelPolicyError("snapshot_malformed")
            issued = _parse_time(value["issued_at"])
            expires = _parse_time(value["expires_at"])
            if expires <= issued:
                raise ModelPolicyError("snapshot_malformed")
            return cls(
                schema_version=SNAPSHOT_SCHEMA_VERSION,
                tenant_id=tenant_id,
                principal_kind=principal_kind,
                principal_id=principal_id,
                mappings=dict(mappings),
                class_defaults=dict(class_defaults),
                persona_contracts=dict(persona_contracts),
                policy_revision=value["policy_revision"],
                allowlist_policy_revision=value["allowlist_policy_revision"],
                catalogue_revision=value["catalogue_revision"],
                correlation_id=value["correlation_id"],
                root_invocation_id=value["root_invocation_id"],
                issued_at=issued,
                expires_at=expires,
                audience=value["audience"],
                source=value["source"],
            )
        except KeyError:
            raise ModelPolicyError("snapshot_malformed") from None


@dataclass(frozen=True)
class ModelPolicyDecision:
    schema_version: int
    tenant_id: str
    invocation_id: str
    correlation_id: str
    snapshot_digest: str
    persona: str
    compatibility_class: str
    harness_contract_revision: str
    requested_model_id: str | None
    resolved_model_id: str
    resolution_source: Literal["explicit-direct", "principal-mapping", "system-default"]
    runtime_posture: Literal["report_only"]
    posture_revision: int
    policy_revision: str
    catalogue_revision: str
    snapshot_allowlist_policy_revision: str
    live_allowlist_policy_revision: str | None = None
    allowlist_policy_drift: bool | None = None
    destination_account_id: str | None = None
    destination_region: str | None = None
    evidence_verified_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "invocation_id": self.invocation_id,
            "correlation_id": self.correlation_id,
            "snapshot_digest": self.snapshot_digest,
            "persona": self.persona,
            "compatibility_class": self.compatibility_class,
            "harness_contract_revision": self.harness_contract_revision,
            "requested_model_id": self.requested_model_id,
            "resolved_model_id": self.resolved_model_id,
            "resolution_source": self.resolution_source,
            "runtime_posture": self.runtime_posture,
            "posture_revision": self.posture_revision,
            "policy_revision": self.policy_revision,
            "catalogue_revision": self.catalogue_revision,
            "snapshot_allowlist_policy_revision": self.snapshot_allowlist_policy_revision,
            "live_allowlist_policy_revision": self.live_allowlist_policy_revision,
            "allowlist_policy_drift": self.allowlist_policy_drift,
            "destination_account_id": self.destination_account_id,
            "destination_region": self.destination_region,
            "evidence_verified_at": (_iso(self.evidence_verified_at) if self.evidence_verified_at is not None else None),
        }


@dataclass(frozen=True)
class ActiveAllowlistPolicy:
    """Server-resolved policy inputs and their canonical active revision."""

    context: TokenContext
    routing_user_id: str
    tenant_patterns: list[str] | None
    tenant_policy_source: str
    service_restriction_pattern_sets: list[list[str]]
    service_policy_unavailable_reason: str | None
    principal_status: str | None
    revision: str


def parse_snapshot(
    raw: str,
    expected_digest: str,
    *,
    tenant_id: str | None = None,
    policy_revision: str | None = None,
    correlation_id: str | None = None,
    root_invocation_id: str | None = None,
) -> ModelPolicySnapshot:
    if not isinstance(raw, str) or not isinstance(expected_digest, str):
        raise ModelPolicyError("snapshot_missing")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ModelPolicyError("snapshot_malformed") from None
    if not isinstance(value, dict) or policy_digest(value) != expected_digest:
        raise ModelPolicyError("snapshot_altered")
    snapshot = ModelPolicySnapshot.from_dict(value)
    if tenant_id is not None and snapshot.tenant_id != tenant_id:
        raise ModelPolicyError("snapshot_cross_tenant")
    if policy_revision is not None and snapshot.policy_revision != policy_revision:
        raise ModelPolicyError("snapshot_revision_mismatch")
    if correlation_id is not None and snapshot.correlation_id != correlation_id:
        raise ModelPolicyError("snapshot_chain_mismatch")
    if root_invocation_id is not None and snapshot.root_invocation_id != root_invocation_id:
        raise ModelPolicyError("snapshot_chain_mismatch")
    return snapshot


def resolve_decision(
    snapshot: ModelPolicySnapshot,
    *,
    invocation_id: str,
    persona: str,
    direct_override: str | None = None,
    direct_requested: str | None = None,
    now: datetime | None = None,
) -> ModelPolicyDecision:
    """Resolve one hop from gateway-owned, frozen policy facts.

    Only the report-only posture is deliberately accepted by this PMM-07
    slice.  Returning an enforcing decision before the live admission and
    invocability gates exist would turn a partial implementation into the
    load-bearing selector.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current >= snapshot.expires_at:
        raise ModelPolicyError("snapshot_expired")
    contract = snapshot.persona_contracts.get(persona)
    if persona not in VALID_PERSONAS or not isinstance(contract, dict):
        raise ModelPolicyError("persona_incompatible")
    compatibility_class = contract.get("compatibility_class", "")
    harness_revision = contract.get("harness_contract_revision", "")
    if not compatibility_class or not harness_revision:
        raise ModelPolicyError("persona_incompatible")
    class_policy = snapshot.class_defaults.get(compatibility_class)
    if not isinstance(class_policy, dict):
        raise ModelPolicyError("class_default_unavailable")
    posture = class_policy.get("posture")
    posture_revision = class_policy.get("posture_revision")
    if posture != "report_only" or type(posture_revision) is not int or posture_revision < 1:
        raise ModelPolicyError("runtime_posture_unsupported")

    # An explicit directive that edge validation could not resolve is a
    # proposed refusal, not permission to silently continue down the ladder.
    # Report-only callers record this refusal while preserving legacy runtime
    # behaviour; enforcing callers are intentionally not implemented here.
    if direct_requested and not direct_override:
        raise ModelPolicyError("direct_override_unresolved")
    if direct_override and not direct_requested:
        direct_requested = direct_override

    mapping = snapshot.mappings.get(persona)
    resolved = direct_override or mapping
    requested = direct_requested if direct_override else mapping
    source: Literal["explicit-direct", "principal-mapping", "system-default"] = "explicit-direct" if direct_override else "principal-mapping"
    if resolved is None:
        if not isinstance(class_policy.get("model_id"), str) or not class_policy["model_id"]:
            raise ModelPolicyError("class_default_unavailable")
        resolved = class_policy["model_id"]
        source = "system-default"
    known = next((row for row in PLATFORM_MODEL_CATALOGUE if row.canonical_model_id == resolved), None)
    if known is None or known.lifecycle != "active":
        raise ModelPolicyError("model_unavailable")
    if known.compatibility_class != compatibility_class or known.harness_contract_revision != harness_revision:
        raise ModelPolicyError("harness_incompatible")
    return ModelPolicyDecision(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        tenant_id=snapshot.tenant_id,
        invocation_id=invocation_id,
        correlation_id=snapshot.correlation_id,
        snapshot_digest=policy_digest(snapshot.to_dict()),
        persona=persona,
        compatibility_class=compatibility_class,
        harness_contract_revision=harness_revision,
        requested_model_id=requested,
        resolved_model_id=resolved,
        resolution_source=source,
        runtime_posture="report_only",
        posture_revision=posture_revision,
        policy_revision=snapshot.policy_revision,
        catalogue_revision=snapshot.catalogue_revision,
        snapshot_allowlist_policy_revision=snapshot.allowlist_policy_revision,
    )


async def validate_live_decision(
    session: AsyncSession,
    *,
    snapshot: ModelPolicySnapshot,
    decision: ModelPolicyDecision,
) -> ModelPolicyDecision:
    """Re-evaluate live admission facts for one frozen per-persona choice.

    The snapshot freezes *which* model the root selected.  It must never freeze
    whether that model is still usable.  This check therefore runs for every
    hop at the trusted gateway bootstrap boundary and reads PMM-03's exact
    destination/harness/request-shape evidence.  It performs no model call,
    STS lookup, or additional HTTP round trip.

    A successful result enriches the signed decision with the exact
    destination and evidence timestamp it was admitted against.  A rejected
    result raises its stable PMM-03 reason (for example ``evidence_stale`` or
    ``not_invocable``), which report-only bootstrap returns as unavailable
    rather than signing a misleading proposal.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        logger.warning(
            "Live model-policy configuration unavailable",
            extra={"error_type": type(exc).__name__},
        )
        raise ModelPolicyError("not_permitted") from None
    # Per-hop bootstrap also shares the caller's session, so this live read is
    # isolated for the same reason: its SQL failure must not abort the caller's
    # transaction.  The handler in bootstrap_model_policy_live() catches outside.
    async with _optional_read_savepoint(session):
        active_policy = await _resolve_active_allowlist_policy(
            session,
            tenant_id=snapshot.tenant_id,
            principal_kind=snapshot.principal_kind,
            principal_id=snapshot.principal_id,
            expires_at=snapshot.expires_at,
            settings=settings,
        )
    drift = active_policy.revision != snapshot.allowlist_policy_revision
    decision = replace(
        decision,
        snapshot_allowlist_policy_revision=snapshot.allowlist_policy_revision,
        live_allowlist_policy_revision=active_policy.revision,
        allowlist_policy_drift=drift,
    )
    revision_evidence: dict[str, object] = {
        "snapshot_allowlist_policy_revision": snapshot.allowlist_policy_revision,
        "live_allowlist_policy_revision": active_policy.revision,
        "allowlist_policy_drift": drift,
    }

    from src.proxy.bedrock_routing import bedrock_routing_resolver

    target = await bedrock_routing_resolver.resolve(
        session,
        active_policy.context,
        user_id=active_policy.routing_user_id,
    )
    account_id = target.account_id or (settings.platform_bedrock_account_id if target.is_platform else None)
    region = target.region or settings.aws_region or None

    validation = await validate_selection(
        session,
        org_id=snapshot.tenant_id,
        principal_kind=snapshot.principal_kind,
        canonical_principal_id=snapshot.principal_id,
        persona_key=decision.persona,
        model=decision.resolved_model_id,
        account_id=account_id,
        region=region,
        tenant_allowed_patterns=active_policy.tenant_patterns,
        service_restriction_pattern_sets=active_policy.service_restriction_pattern_sets,
        policy_unavailable_reason=active_policy.service_policy_unavailable_reason,
        principal_status=active_policy.principal_status,
    )
    if isinstance(validation, SelectionRejection):
        logger.warning(
            "Live model-policy decision rejected",
            extra={
                "tenant_id": snapshot.tenant_id,
                "principal_kind": snapshot.principal_kind,
                "persona": decision.persona,
                "model_id": decision.resolved_model_id,
                "destination_account_id": account_id,
                "destination_region": region,
                "reason": validation.reason,
            },
        )
        raise ModelPolicyError(validation.reason, evidence=revision_evidence)
    if (
        validation.canonical_model_id != decision.resolved_model_id
        or validation.compatibility_class != decision.compatibility_class
        or validation.harness_contract_revision != decision.harness_contract_revision
        or validation.evidence_verified_at is None
    ):
        raise ModelPolicyError("model_validation_mismatch", evidence=revision_evidence)
    return replace(
        decision,
        destination_account_id=account_id,
        destination_region=region,
        evidence_verified_at=validation.evidence_verified_at,
    )


async def _resolve_principal(
    session: AsyncSession,
    *,
    tenant_id: str,
    grant: DelegatedGrant,
    authority: dict,
) -> tuple[Literal["human", "service_account"], str]:
    if grant.authority.kind in {AUTHORITY_GITHUB_EVENT, AUTHORITY_GATE_DECISION}:
        return "human", await resolve_root_user_entity_id(session, tenant_id, grant.authority.human_id)
    if grant.authority.kind != AUTHORITY_SERVICE_POLICY:
        raise ModelPolicyError("authority_kind_unsupported")
    service_identity = authority.get("service_identity", {}).get("S", "")
    alias_source = service_identity.partition(":")[0]
    if not service_identity or alias_source not in ALIAS_SOURCES:
        raise ModelPolicyError("service_principal_unregistered")
    row = await session.scalar(
        select(ServicePrincipalAlias)
        .join(
            ServicePrincipal,
            ServicePrincipal.canonical_service_principal_id == ServicePrincipalAlias.canonical_service_principal_id,
        )
        .where(
            ServicePrincipalAlias.org_id == tenant_id,
            ServicePrincipalAlias.alias_source == alias_source,
            ServicePrincipalAlias.alias_id == service_identity,
            ServicePrincipalAlias.is_active.is_(True),
            ServicePrincipal.status == "active",
        )
    )
    if row is None:
        raise ModelPolicyError("service_principal_unregistered")
    return "service_account", row.canonical_service_principal_id


async def _resolve_active_allowlist_policy(
    session: AsyncSession,
    *,
    tenant_id: str,
    principal_kind: Literal["human", "service_account"],
    principal_id: str,
    expires_at: datetime,
    settings=None,
) -> ActiveAllowlistPolicy:
    """Resolve the exact live policy intersection used by every PMM-06 seam."""
    principal_status: str | None = None
    if principal_kind == "human":
        user = await session.scalar(
            select(User).where(
                User.id == principal_id,
                User.org_id == tenant_id,
            )
        )
        if user is None:
            raise ModelPolicyError("principal_unavailable")
        context = TokenContext(
            user_id=user.id,
            org_id=tenant_id,
            team_id=user.team_id,
            department_id="",
            account_type="human",
            expires_at=expires_at,
        )
        routing_user_id = user.id
    else:
        service_principal = await session.scalar(
            select(ServicePrincipal).where(
                ServicePrincipal.canonical_service_principal_id == principal_id,
                ServicePrincipal.org_id == tenant_id,
            )
        )
        if service_principal is None:
            raise ModelPolicyError("principal_unavailable")
        principal_status = service_principal.status
        context = TokenContext(
            user_id=principal_id,
            org_id=tenant_id,
            team_id="",
            department_id="",
            account_type="service",
            expires_at=expires_at,
            canonical_service_principal_id=principal_id,
        )
        routing_user_id = ""

    from src.admin.persona_models.catalogue_routes import resolve_managed_service_restriction_policy
    from src.proxy.model_resolver import production_model_resolver

    try:
        active_settings = settings or get_settings()
        tenant_patterns, tenant_policy_source = production_model_resolver(active_settings).get_configured_allowed_models(context)
    except Exception as exc:
        logger.warning(
            "Live tenant model policy unavailable",
            extra={
                "tenant_id": tenant_id,
                "principal_kind": principal_kind,
                "error_type": type(exc).__name__,
            },
        )
        raise ModelPolicyError("not_permitted") from None

    service_restriction_pattern_sets: list[list[str]] = []
    service_policy_unavailable_reason: str | None = None
    if principal_kind == "service_account":
        service_restriction_pattern_sets, service_policy_unavailable_reason = await resolve_managed_service_restriction_policy(
            session,
            org_id=tenant_id,
            canonical_service_principal_id=principal_id,
        )

    revision = _active_allowlist_policy_revision(
        tenant_patterns=tenant_patterns,
        tenant_policy_source=tenant_policy_source,
        service_restriction_pattern_sets=service_restriction_pattern_sets,
        service_policy_unavailable_reason=service_policy_unavailable_reason,
        service_principal_status=principal_status,
    )
    return ActiveAllowlistPolicy(
        context=context,
        routing_user_id=routing_user_id,
        tenant_patterns=tenant_patterns,
        tenant_policy_source=tenant_policy_source,
        service_restriction_pattern_sets=service_restriction_pattern_sets,
        service_policy_unavailable_reason=service_policy_unavailable_reason,
        principal_status=principal_status,
        revision=revision,
    )


def _contract_maps() -> tuple[dict[str, dict[str, str]], str]:
    contracts: dict[str, dict[str, str]] = {}
    for persona in sorted(VALID_PERSONAS):
        compatibility_class = persona_compatibility_class(persona)
        if compatibility_class:
            contracts[persona] = {
                "compatibility_class": compatibility_class,
                "harness_contract_revision": HARNESS_CONTRACT_REVISION,
            }
    catalogue_value = [
        {
            "model_id": row.canonical_model_id,
            "compatibility_class": row.compatibility_class,
            "harness_contract_revision": row.harness_contract_revision,
            "lifecycle": row.lifecycle,
        }
        for row in PLATFORM_MODEL_CATALOGUE
    ]
    catalogue_revision = policy_digest({"personas": contracts, "models": catalogue_value})
    return contracts, catalogue_revision


def _lkg_cache_key(
    tenant_id: str,
    owner_locator: str,
) -> tuple[str, str]:
    """Return a tenant/canonical-owner key without exposing identity in SK."""
    owner_digest = hashlib.sha256(f"{tenant_id}\0{owner_locator}".encode()).hexdigest()
    return f"TENANT#{tenant_id}", f"MODEL_POLICY_LKG#{owner_digest}"


def _canonical_owner_locator(
    principal_kind: Literal["human", "service_account"],
    principal_id: str,
) -> str:
    """Bind cache addressing to the protected preference owner, never an alias."""
    if not principal_id:
        raise ModelPolicyError("snapshot_cache_owner_unproven")
    return f"{principal_kind}:{principal_id}"


def _trusted_root_locator(grant: DelegatedGrant, authority: dict) -> str:
    """Derive the cache root only from the already-verified authority record."""
    if grant.authority.kind in {AUTHORITY_GITHUB_EVENT, AUTHORITY_GATE_DECISION}:
        human_id = authority.get("human_id", {}).get("S")
        if not human_id or human_id != grant.authority.human_id:
            raise ModelPolicyError("unverified_provenance")
        return f"{grant.authority.kind}:{human_id}"
    if grant.authority.kind == AUTHORITY_SERVICE_POLICY:
        service_identity = authority.get("service_identity", {}).get("S")
        if not service_identity:
            raise ModelPolicyError("service_principal_unregistered")
        return f"service_policy:{service_identity}"
    raise ModelPolicyError("authority_kind_unsupported")


async def _store_lkg_snapshot(
    *,
    store,
    snapshot: ModelPolicySnapshot,
    owner_locator: str,
    now: datetime,
) -> None:
    """Best-effort cache of a successful read; failure states are never cached."""
    ttl_seconds = _lkg_ttl_seconds()
    if ttl_seconds == 0 or not hasattr(store, "client") or not hasattr(store, "table"):
        return
    fresh_until = min(snapshot.expires_at, now + timedelta(seconds=ttl_seconds))
    if fresh_until <= now:
        return
    raw = canonical_json(snapshot.to_dict()).decode("ascii")
    digest = policy_digest(snapshot.to_dict())
    expected_owner_locator = _canonical_owner_locator(snapshot.principal_kind, snapshot.principal_id)
    if owner_locator != expected_owner_locator:
        raise ModelPolicyError("snapshot_cache_owner_mismatch")
    pk, sk = _lkg_cache_key(snapshot.tenant_id, owner_locator)
    item = {
        "pk": {"S": pk},
        "sk": {"S": sk},
        "tenant_id": {"S": snapshot.tenant_id},
        "principal_kind": {"S": snapshot.principal_kind},
        "principal_id": {"S": snapshot.principal_id},
        "owner_locator": {"S": owner_locator},
        "policy_revision": {"S": snapshot.policy_revision},
        "catalogue_revision": {"S": snapshot.catalogue_revision},
        "allowlist_policy_revision": {"S": snapshot.allowlist_policy_revision},
        "snapshot": {"S": raw},
        "snapshot_digest": {"S": digest},
        "cached_at": {"S": _iso(now)},
        "fresh_until": {"S": _iso(fresh_until)},
        # DynamoDB TTL is cleanup only. Reads always validate fresh_until because
        # expired TTL rows can remain visible for hours.
        "ttl": {"N": str(int(fresh_until.timestamp()))},
    }
    try:
        await run_in_threadpool(
            store.client.put_item,
            TableName=store.table,
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(pk) OR cached_at < :cached OR "
                "(cached_at = :cached AND policy_revision = :revision AND "
                "catalogue_revision = :catalogue AND allowlist_policy_revision = :allowlist)"
            ),
            ExpressionAttributeValues={
                ":cached": {"S": _iso(now)},
                ":revision": {"S": snapshot.policy_revision},
                ":catalogue": {"S": snapshot.catalogue_revision},
                ":allowlist": {"S": snapshot.allowlist_policy_revision},
            },
        )
    except (ClientError, BotoCoreError, AuthorityStoreError):
        logger.warning(
            "Model-policy last-known-good cache write unavailable",
            extra={"tenant_id": snapshot.tenant_id},
        )


async def _load_lkg_snapshot(
    *,
    store,
    tenant_id: str,
    owner_locator: str,
    principal_kind: Literal["human", "service_account"],
    principal_id: str,
    invocation_id: str,
    correlation_id: str,
    authority_expires_at: datetime,
    active_allowlist_policy_revision: str,
    now: datetime,
) -> ModelPolicySnapshot:
    """Load only a correctly bound, internally consistent, still-fresh LKG row."""
    if _lkg_ttl_seconds() == 0:
        raise ModelPolicyError("snapshot_cache_disabled")
    expected_owner_locator = _canonical_owner_locator(principal_kind, principal_id)
    if owner_locator != expected_owner_locator:
        raise ModelPolicyError("snapshot_cache_owner_mismatch")
    pk, sk = _lkg_cache_key(tenant_id, owner_locator)
    cached = await run_in_threadpool(store._read, pk, sk)
    if not cached:
        raise ModelPolicyError("snapshot_cache_missing")
    if cached.get("tenant_id") != {"S": tenant_id} or cached.get("owner_locator") != {"S": owner_locator}:
        raise ModelPolicyError("snapshot_cache_owner_mismatch")
    cached_kind = cached.get("principal_kind", {}).get("S")
    cached_principal = cached.get("principal_id", {}).get("S")
    if (
        cached_kind not in {"human", "service_account"}
        or not isinstance(cached_principal, str)
        or not cached_principal
        or cached_kind != principal_kind
        or cached_principal != principal_id
    ):
        raise ModelPolicyError("snapshot_cache_owner_mismatch")
    policy_revision = cached.get("policy_revision", {}).get("S")
    catalogue_revision = cached.get("catalogue_revision", {}).get("S")
    allowlist_revision = cached.get("allowlist_policy_revision", {}).get("S")
    if allowlist_revision != active_allowlist_policy_revision:
        raise ModelPolicyError("snapshot_revision_mismatch")
    fresh_until = _parse_time(cached.get("fresh_until", {}).get("S"))
    cached_at = _parse_time(cached.get("cached_at", {}).get("S"))
    if (
        fresh_until <= now
        or cached_at > now
        or (now - cached_at).total_seconds() >= _lkg_ttl_seconds()
        or (fresh_until - cached_at).total_seconds() > MAX_LKG_CACHE_TTL_SECONDS
    ):
        raise ModelPolicyError("snapshot_cache_stale")
    snapshot = parse_snapshot(
        cached.get("snapshot", {}).get("S"),
        cached.get("snapshot_digest", {}).get("S"),
        tenant_id=tenant_id,
        policy_revision=policy_revision,
    )
    if snapshot.catalogue_revision != catalogue_revision or snapshot.allowlist_policy_revision != allowlist_revision:
        raise ModelPolicyError("snapshot_revision_mismatch")
    if snapshot.principal_kind != cached_kind or snapshot.principal_id != cached_principal:
        raise ModelPolicyError("snapshot_cache_owner_mismatch")
    expires_at = min(fresh_until, snapshot.expires_at, authority_expires_at)
    if expires_at <= now:
        raise ModelPolicyError("snapshot_cache_stale")
    return replace(
        snapshot,
        correlation_id=correlation_id,
        root_invocation_id=invocation_id,
        issued_at=now,
        expires_at=expires_at,
        source="last_known_good_cache",
    )


@asynccontextmanager
async def _optional_read_savepoint(session: AsyncSession):
    """Run a report-only read so its SQL failure cannot abort the caller's work.

    PMM-06 reads run on the *same* ``AsyncSession`` that orchestration work
    admission has already used to reserve a work claim, and deliberately before
    that claim is committed.  PostgreSQL aborts the whole transaction on the
    first failed statement (``25P02``), so catching the Python exception is not
    enough: without a rollback the pending claim is silently discarded at COMMIT
    while the producer still receives a successful admission receipt.

    ``session.begin_nested()`` issues a SAVEPOINT and rolls back to it as this
    context manager exits, so the caller's reservation and any later statement on
    the session survive.  Callers MUST keep their ``except`` clause OUTSIDE this
    block: the rollback only completes on exit, so a fallback evaluated inside it
    would still be running inside the aborted transaction.  Only the savepoint is
    rolled back -- the surrounding transaction belongs to the admission caller and
    is never committed or rolled back here.
    """
    async with session.begin_nested():
        yield


async def build_root_snapshot(
    session: AsyncSession,
    *,
    store,
    invocation_id: str,
    tenant_id: str,
    execution: dict,
    grant: DelegatedGrant,
    now: datetime,
) -> ModelPolicySnapshot:
    authority = await run_in_threadpool(store._read, f"TENANT#{tenant_id}", f"AUTHORITY#{grant.authority.reference_id}")
    if not authority:
        raise ModelPolicyError("authority_unavailable")
    if authority.get("authority_kind", {}).get("S") != grant.authority.kind:
        raise ModelPolicyError("unverified_provenance")
    # Validate that the authority record still agrees with the signed grant.
    # Its human subject or service alias is provenance, but it is deliberately
    # not a cache address: both can be remapped to a different canonical owner.
    _trusted_root_locator(grant, authority)
    expires = grant.expires_at or now + timedelta(days=7)
    if expires <= now:
        raise ModelPolicyError("authority_expired")
    correlation_id = execution.get("flow_id", {}).get("S") or invocation_id
    try:
        # The savepoint must wrap the read itself: a failure here aborts the
        # transaction that still holds an uncommitted work claim.
        async with _optional_read_savepoint(session):
            principal_kind, principal_id = await _resolve_principal(
                session,
                tenant_id=tenant_id,
                grant=grant,
                authority=authority,
            )
    except SQLAlchemyError:
        # Without a current canonical owner, an alias-addressed cache entry
        # could belong to the principal that owned a recycled alias yesterday.
        # Refuse before reading any cache row.
        raise ModelPolicyError("snapshot_cache_owner_unproven") from None
    owner_locator = _canonical_owner_locator(principal_kind, principal_id)
    # No ``except`` here on purpose: this read has no fallback, so its failure
    # should keep propagating to the report-only handler exactly as before.  The
    # savepoint still rolls back as the block unwinds, which is what preserves
    # the caller's uncommitted work claim.
    async with _optional_read_savepoint(session):
        active_policy = await _resolve_active_allowlist_policy(
            session,
            tenant_id=tenant_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
            expires_at=expires,
        )
    try:
        async with _optional_read_savepoint(session):
            preferences = list(
                await session.scalars(
                    select(PersonaModelPreference).where(
                        PersonaModelPreference.org_id == tenant_id,
                        PersonaModelPreference.principal_kind == principal_kind,
                        PersonaModelPreference.principal_id == principal_id,
                    )
                )
            )
            settings = list(await session.scalars(select(PersonaModelPolicySetting)))
    except SQLAlchemyError:
        # Reached only after the savepoint rollback above has completed, so the
        # last-known-good fallback below runs on a usable transaction and the
        # caller's work claim still commits.
        return await _load_lkg_snapshot(
            store=store,
            tenant_id=tenant_id,
            owner_locator=owner_locator,
            principal_kind=principal_kind,
            principal_id=principal_id,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            authority_expires_at=expires,
            active_allowlist_policy_revision=active_policy.revision,
            now=now,
        )
    mappings = {row.persona_key: row.canonical_model_id for row in preferences}
    class_defaults = {
        row.compatibility_class: {
            "model_id": row.active_default_model_id,
            "revision": row.revision,
            "posture": row.enforcement_posture,
            "posture_revision": row.posture_revision,
            "harness_contract_revision": row.harness_contract_revision,
        }
        for row in settings
    }
    revision_input = {
        "preferences": [
            {"persona": row.persona_key, "model": row.canonical_model_id, "revision": row.revision}
            for row in sorted(preferences, key=lambda item: item.persona_key)
        ],
        "defaults": class_defaults,
    }
    contracts, catalogue_revision = _contract_maps()
    snapshot = ModelPolicySnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        tenant_id=tenant_id,
        principal_kind=principal_kind,
        principal_id=principal_id,
        mappings=mappings,
        class_defaults=class_defaults,
        persona_contracts=contracts,
        policy_revision=policy_digest(revision_input),
        allowlist_policy_revision=active_policy.revision,
        catalogue_revision=catalogue_revision,
        correlation_id=correlation_id,
        root_invocation_id=invocation_id,
        issued_at=now,
        expires_at=expires,
        audience=SNAPSHOT_AUDIENCE,
        source=SNAPSHOT_SOURCE_LIVE,
    )
    if active_policy.service_policy_unavailable_reason is None:
        await _store_lkg_snapshot(
            store=store,
            snapshot=snapshot,
            owner_locator=owner_locator,
            now=now,
        )
    return snapshot


async def _persist_snapshot(*, store, invocation_id: str, tenant_id: str, snapshot: ModelPolicySnapshot) -> str:
    value = snapshot.to_dict()
    raw = canonical_json(value).decode("ascii")
    digest = policy_digest(value)
    try:
        await run_in_threadpool(
            store.client.update_item,
            TableName=store.table,
            Key={"pk": {"S": f"TENANT#{tenant_id}"}, "sk": {"S": f"EXEC#{invocation_id}"}},
            UpdateExpression=(
                "SET model_policy_snapshot = :snapshot, model_policy_snapshot_digest = :digest, "
                "model_policy_snapshot_schema = :schema, model_policy_root_invocation_id = :root, "
                "model_policy_revision = :revision, model_policy_correlation_id = :correlation"
            ),
            ConditionExpression=(
                "#st = :pending AND tenant_id = :tenant AND "
                "(attribute_not_exists(model_policy_snapshot_digest) OR model_policy_snapshot_digest = :digest)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":snapshot": {"S": raw},
                ":digest": {"S": digest},
                ":schema": {"N": str(SNAPSHOT_SCHEMA_VERSION)},
                ":root": {"S": snapshot.root_invocation_id},
                ":revision": {"S": snapshot.policy_revision},
                ":correlation": {"S": snapshot.correlation_id},
                ":pending": {"S": "pending"},
                ":tenant": {"S": tenant_id},
            },
        )
    except (ClientError, BotoCoreError):
        existing = await run_in_threadpool(store._read, f"TENANT#{tenant_id}", f"EXEC#{invocation_id}")
        if (
            not existing
            or existing.get("model_policy_snapshot_digest") != {"S": digest}
            or existing.get("model_policy_revision") != {"S": snapshot.policy_revision}
            or existing.get("model_policy_correlation_id") != {"S": snapshot.correlation_id}
            or existing.get("model_policy_root_invocation_id") != {"S": snapshot.root_invocation_id}
        ):
            raise ModelPolicyError("snapshot_persistence_failed") from None
    return digest


def _parse_execution_snapshot(execution: dict, *, tenant_id: str) -> ModelPolicySnapshot:
    raw = execution.get("model_policy_snapshot", {}).get("S")
    digest = execution.get("model_policy_snapshot_digest", {}).get("S")
    revision = execution.get("model_policy_revision", {}).get("S")
    correlation = execution.get("model_policy_correlation_id", {}).get("S")
    root = execution.get("model_policy_root_invocation_id", {}).get("S")
    if raw is None and digest is None:
        raise ModelPolicyError("snapshot_missing")
    if not all(isinstance(value, str) and value for value in (revision, correlation, root)):
        raise ModelPolicyError("snapshot_binding_missing")
    snapshot = parse_snapshot(
        raw,
        digest,
        tenant_id=tenant_id,
        policy_revision=revision,
        correlation_id=correlation,
        root_invocation_id=root,
    )
    execution_flow = execution.get("flow_id", {}).get("S")
    if execution_flow and snapshot.correlation_id != execution_flow:
        raise ModelPolicyError("snapshot_chain_mismatch")
    return snapshot


async def ensure_snapshot_for_admission(
    session: AsyncSession,
    *,
    store,
    invocation_id: str,
    now: datetime | None = None,
) -> dict:
    """Associate a root or child execution with one immutable snapshot.

    This function raises specific failures.  The work-admission caller converts
    them to report-only evidence so snapshot defects cannot block dispatch yet.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    pointer = await run_in_threadpool(store._read, f"INVOCATION#{invocation_id}", "DISPATCH")
    tenant_id = (pointer or {}).get("tenant_id", {}).get("S", "")
    if not tenant_id:
        raise ModelPolicyError("dispatch_unresolved")
    execution = await run_in_threadpool(store._read, f"TENANT#{tenant_id}", f"EXEC#{invocation_id}")
    if not execution or execution.get("status") != {"S": "pending"}:
        raise ModelPolicyError("dispatch_not_pending")

    existing_raw = execution.get("model_policy_snapshot", {}).get("S")
    existing_digest = execution.get("model_policy_snapshot_digest", {}).get("S")
    if existing_raw or existing_digest:
        snapshot = _parse_execution_snapshot(execution, tenant_id=tenant_id)
        return {"status": "available", "snapshot_digest": existing_digest, "root_invocation_id": snapshot.root_invocation_id}

    parent = execution.get("parent_principal", {}).get("S", "").rsplit("#", 1)[0]
    if parent:
        parent_execution = await run_in_threadpool(store._read, f"TENANT#{tenant_id}", f"EXEC#{parent}")
        if not parent_execution:
            raise ModelPolicyError("parent_snapshot_missing")
        snapshot = _parse_execution_snapshot(parent_execution, tenant_id=tenant_id)
        child_flow = execution.get("flow_id", {}).get("S")
        if child_flow and snapshot.correlation_id != child_flow:
            raise ModelPolicyError("snapshot_chain_mismatch")
    else:
        grant = await run_in_threadpool(
            store.live_grant,
            invocation_id=invocation_id,
            tenant_id=tenant_id,
            attempt=1,
            now=current,
        )
        snapshot = await build_root_snapshot(
            session,
            store=store,
            invocation_id=invocation_id,
            tenant_id=tenant_id,
            execution=execution,
            grant=grant,
            now=current,
        )
    digest = await _persist_snapshot(
        store=store,
        invocation_id=invocation_id,
        tenant_id=tenant_id,
        snapshot=snapshot,
    )
    return {"status": "available", "snapshot_digest": digest, "root_invocation_id": snapshot.root_invocation_id}


async def ensure_snapshot_report_only(session: AsyncSession, *, store, invocation_id: str) -> dict:
    """Build evidence without changing admission while the posture is report-only."""
    try:
        return await ensure_snapshot_for_admission(session, store=store, invocation_id=invocation_id)
    except ModelPolicyError as exc:
        logger.warning(
            "Model-policy snapshot unavailable in report-only mode",
            extra={"invocation_id": invocation_id, "reason": exc.reason},
        )
        return {"status": "unavailable", "reason": exc.reason}
    except Exception as exc:
        # Deliberately fail-soft only in report-only.  Do not include exception
        # text: database/identity details are not part of the producer receipt.
        logger.warning(
            "Model-policy snapshot failed in report-only mode",
            extra={"invocation_id": invocation_id, "error_type": type(exc).__name__},
        )
        return {"status": "unavailable", "reason": "snapshot_unavailable"}


def _sign_policy_decision(
    *,
    decision: ModelPolicyDecision,
    snapshot: ModelPolicySnapshot,
    record,
    grant: DelegatedGrant,
    env: dict[str, str] | None,
) -> dict:
    decision_body = canonical_json(decision.to_dict())
    assertion = sign_envelope(
        tenant_id=record.tenant_id,
        principal=record.principal,
        target_run_id=record.invocation_id,
        target_generation=record.current_attempt,
        action="resolve_model",
        command_id=decision.snapshot_digest,
        request_body=decision_body,
        grant_id=grant.grant_id,
        revocation_epoch=grant.revocation_epoch,
        flow_id=grant.flow_id,
        authority_reference_id=grant.authority.reference_id,
        audience=MODEL_POLICY_AUDIENCE,
        chain_id=snapshot.correlation_id,
        env=env,
    )
    return {
        "posture": decision.runtime_posture,
        "status": "proposed",
        "decision": decision.to_dict(),
        "assertion": assertion,
    }


def _resolve_execution_decision(*, raw_execution: dict, record) -> tuple[ModelPolicySnapshot, ModelPolicyDecision]:
    snapshot = _parse_execution_snapshot(raw_execution, tenant_id=record.tenant_id)
    decision = resolve_decision(
        snapshot,
        invocation_id=record.invocation_id,
        persona=raw_execution.get("persona", {}).get("S", ""),
        direct_override=raw_execution.get("direct_model_override", {}).get("S") or None,
        direct_requested=raw_execution.get("direct_model_requested", {}).get("S") or None,
    )
    return snapshot, decision


def bootstrap_model_policy(*, store, record, grant: DelegatedGrant, env: dict[str, str] | None = None) -> dict:
    """Build a deterministic report-only decision without live DB admission.

    Retained as the pure snapshot/crypto seam used by unit and compatibility
    tests.  Production bootstrap uses :func:`bootstrap_model_policy_live`,
    which must pass PMM-03's exact fresh evidence check before this decision is
    signed.
    """
    raw_execution = store._read(f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}") or {}
    try:
        snapshot, decision = _resolve_execution_decision(
            raw_execution=raw_execution,
            record=record,
        )
        return _sign_policy_decision(
            decision=decision,
            snapshot=snapshot,
            record=record,
            grant=grant,
            env=env,
        )
    except ModelPolicyError as exc:
        return {"posture": "report_only", "status": "unavailable", "reason": exc.reason}
    except (EnvelopeError, AuthorityStoreError):
        return {"posture": "report_only", "status": "unavailable", "reason": "decision_unavailable"}


async def bootstrap_model_policy_live(
    session: AsyncSession,
    *,
    store,
    record,
    grant: DelegatedGrant,
    env: dict[str, str] | None = None,
) -> dict:
    """Return a signed proposal only after current per-hop admission succeeds."""
    raw_execution = (
        await run_in_threadpool(
            store._read,
            f"TENANT#{record.tenant_id}",
            f"EXEC#{record.invocation_id}",
        )
        or {}
    )
    try:
        snapshot, decision = _resolve_execution_decision(
            raw_execution=raw_execution,
            record=record,
        )
        decision = await validate_live_decision(
            session,
            snapshot=snapshot,
            decision=decision,
        )
        return _sign_policy_decision(
            decision=decision,
            snapshot=snapshot,
            record=record,
            grant=grant,
            env=env,
        )
    except ModelPolicyError as exc:
        response = {
            "posture": "report_only",
            "status": "unavailable",
            "reason": exc.reason,
        }
        if exc.evidence is not None:
            response["evidence"] = exc.evidence
        return response
    except SQLAlchemyError:
        logger.warning(
            "Live model-policy admission unavailable",
            extra={"invocation_id": record.invocation_id},
            exc_info=True,
        )
        return {
            "posture": "report_only",
            "status": "unavailable",
            "reason": "model_validation_unavailable",
        }
    except (EnvelopeError, AuthorityStoreError):
        return {
            "posture": "report_only",
            "status": "unavailable",
            "reason": "decision_unavailable",
        }
    except Exception as exc:
        # Report-only must never turn a successful bootstrap into an outage.
        # Keep the response stable and log only the exception type; destination
        # credentials and database details are not requester-visible evidence.
        logger.warning(
            "Live model-policy decision failed",
            extra={
                "invocation_id": record.invocation_id,
                "error_type": type(exc).__name__,
            },
        )
        return {
            "posture": "report_only",
            "status": "unavailable",
            "reason": "decision_unavailable",
        }
