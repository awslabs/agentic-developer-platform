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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.persona_models._personas import VALID_PERSONAS
from src.admin.persona_models.catalogue import (
    HARNESS_CONTRACT_REVISION,
    PERSONA_ALLOWED_PATTERNS,
    PLATFORM_MODEL_CATALOGUE,
    persona_compatibility_class,
)
from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, EnvelopeError, sign_envelope
from src.agentauth.grants import DelegatedGrant
from src.agentauth.store import AuthorityStoreError
from src.shared.identity.resolver import resolve_root_user_entity_id
from src.shared.models.persona_models import (
    ALIAS_SOURCES,
    PersonaModelPolicySetting,
    PersonaModelPreference,
    ServicePrincipal,
    ServicePrincipalAlias,
)

logger = logging.getLogger("bedrockgateway.agentauth.model_policy")

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_AUDIENCE = "adp-agent-model-policy"
SNAPSHOT_SOURCE_LIVE = "live"
MAX_SNAPSHOT_BYTES = 128 * 1024


class ModelPolicyError(Exception):
    """A snapshot or decision was unavailable or invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ModelPolicyError("snapshot_malformed")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ModelPolicyError("snapshot_malformed") from None


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
            if not isinstance(value, dict) or value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
                raise ModelPolicyError("snapshot_schema_unsupported")
            tenant_id = value["tenant_id"]
            principal_kind = value["principal_kind"]
            principal_id = value["principal_id"]
            mappings = value["mappings"]
            class_defaults = value["class_defaults"]
            persona_contracts = value["persona_contracts"]
            if (
                not all(isinstance(item, str) and item for item in (tenant_id, principal_id))
                or principal_kind not in {"human", "service_account"}
                or not isinstance(mappings, dict)
                or not isinstance(class_defaults, dict)
                or not isinstance(persona_contracts, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in mappings.items())
                or value.get("audience") != SNAPSHOT_AUDIENCE
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
    principal_kind: Literal["human", "service_account"]
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

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "principal_kind": self.principal_kind,
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
        }


def parse_snapshot(raw: str, expected_digest: str, *, tenant_id: str | None = None) -> ModelPolicySnapshot:
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
        principal_kind=snapshot.principal_kind,
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
    )


async def _resolve_principal(
    session: AsyncSession,
    *,
    tenant_id: str,
    grant: DelegatedGrant,
    authority: dict,
) -> tuple[Literal["human", "service_account"], str]:
    if grant.authority.kind in {"github_event", "gate_decision"}:
        return "human", await resolve_root_user_entity_id(session, tenant_id, grant.authority.human_id)
    if grant.authority.kind != "service_policy":
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


def _contract_maps() -> tuple[dict[str, dict[str, str]], str, str]:
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
    allowlist_revision = policy_digest({"patterns": sorted(PERSONA_ALLOWED_PATTERNS)})
    return contracts, catalogue_revision, allowlist_revision


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
    if not authority or authority.get("authority_kind", {}).get("S") != grant.authority.kind:
        raise ModelPolicyError("authority_unavailable")
    principal_kind, principal_id = await _resolve_principal(
        session,
        tenant_id=tenant_id,
        grant=grant,
        authority=authority,
    )
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
    contracts, catalogue_revision, allowlist_revision = _contract_maps()
    expires = grant.expires_at or now + timedelta(days=7)
    if expires <= now:
        raise ModelPolicyError("authority_expired")
    correlation_id = execution.get("flow_id", {}).get("S") or invocation_id
    return ModelPolicySnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        tenant_id=tenant_id,
        principal_kind=principal_kind,
        principal_id=principal_id,
        mappings=mappings,
        class_defaults=class_defaults,
        persona_contracts=contracts,
        policy_revision=policy_digest(revision_input),
        allowlist_policy_revision=allowlist_revision,
        catalogue_revision=catalogue_revision,
        correlation_id=correlation_id,
        root_invocation_id=invocation_id,
        issued_at=now,
        expires_at=expires,
        audience=SNAPSHOT_AUDIENCE,
        source=SNAPSHOT_SOURCE_LIVE,
    )


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
                "model_policy_snapshot_schema = :schema, model_policy_root_invocation_id = :root"
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
                ":pending": {"S": "pending"},
                ":tenant": {"S": tenant_id},
            },
        )
    except (ClientError, BotoCoreError):
        existing = await run_in_threadpool(store._read, f"TENANT#{tenant_id}", f"EXEC#{invocation_id}")
        if not existing or existing.get("model_policy_snapshot_digest") != {"S": digest}:
            raise ModelPolicyError("snapshot_persistence_failed") from None
    return digest


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
        snapshot = parse_snapshot(existing_raw, existing_digest, tenant_id=tenant_id)
        return {"status": "available", "snapshot_digest": existing_digest, "root_invocation_id": snapshot.root_invocation_id}

    parent = execution.get("parent_principal", {}).get("S", "").rsplit("#", 1)[0]
    if parent:
        parent_execution = await run_in_threadpool(store._read, f"TENANT#{tenant_id}", f"EXEC#{parent}")
        if not parent_execution:
            raise ModelPolicyError("parent_snapshot_missing")
        snapshot = parse_snapshot(
            parent_execution.get("model_policy_snapshot", {}).get("S"),
            parent_execution.get("model_policy_snapshot_digest", {}).get("S"),
            tenant_id=tenant_id,
        )
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


def bootstrap_model_policy(*, store, record, grant: DelegatedGrant, env: dict[str, str] | None = None) -> dict:
    """Return a signed, behaviour-neutral proposed decision for one worker hop."""
    raw_execution = store._read(f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}") or {}
    raw = raw_execution.get("model_policy_snapshot", {}).get("S")
    digest = raw_execution.get("model_policy_snapshot_digest", {}).get("S")
    try:
        snapshot = parse_snapshot(raw, digest, tenant_id=record.tenant_id)
        decision = resolve_decision(
            snapshot,
            invocation_id=record.invocation_id,
            persona=raw_execution.get("persona", {}).get("S", ""),
            direct_override=raw_execution.get("direct_model_override", {}).get("S") or None,
            direct_requested=raw_execution.get("direct_model_requested", {}).get("S") or None,
        )
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
    except ModelPolicyError as exc:
        return {"posture": "report_only", "status": "unavailable", "reason": exc.reason}
    except (EnvelopeError, AuthorityStoreError):
        return {"posture": "report_only", "status": "unavailable", "reason": "decision_unavailable"}
