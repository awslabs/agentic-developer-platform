"""Resolve explicit Task Messages selection using live policy and exact evidence.

This is a Messages transport, not a Claude CLI/SDK persona. Its own probe key
prevents a successful CLI probe from silently certifying a different payload.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from src.admin.persona_models.catalogue import catalogue_lookup
from src.admin.persona_models.catalogue_service import (
    _model_matches_patterns,
    _model_matches_service_restrictions,
    lookup_evidence,
)
from src.agentauth.model_policy import ModelPolicyError, _resolve_active_allowlist_policy
from src.budget.pricing_v2_reader import get_rate_state
from src.proxy.bedrock_routing import bedrock_routing_resolver
from src.shared.config import get_settings
from src.shared.models.persona_models import PersonaModelPreference

TASK_PERSONA = "agent-task-investigator"
TASK_TRANSPORT = "anthropic_messages"
TASK_CONTRACT_REVISION = "task-messages-v1"
TASK_PROBE_BODY = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 16,
    "system": "Reply briefly.",
    "messages": [{"role": "user", "content": [{"type": "text", "text": "Reply OK."}]}],
}
TASK_REQUEST_SHAPE = hashlib.sha256(json.dumps(TASK_PROBE_BODY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def resolve_task_model(db, *, tenant, principal, deadline, expected_policy_version, include_context=False):
    policy = await _resolve_active_allowlist_policy(db, tenant_id=tenant,
        principal_kind="service_account", principal_id=principal, expires_at=deadline)
    if policy.principal_status != "active" or policy.service_policy_unavailable_reason:
        raise ModelPolicyError("task_model_policy_unavailable")
    preference = await db.scalar(select(PersonaModelPreference).where(
        PersonaModelPreference.org_id == tenant,
        PersonaModelPreference.principal_kind == "service_account",
        PersonaModelPreference.principal_id == principal,
        PersonaModelPreference.persona_key == TASK_PERSONA,
    ))
    # This transport has no implicit CLI-class default. Enrollment must record
    # an explicit administrator-selected model for the Task persona.
    if preference is None:
        raise ModelPolicyError("task_model_selection_missing")
    model_id = preference.canonical_model_id
    model = catalogue_lookup(model_id)
    if model is None or model.lifecycle == "retired" or ".anthropic." not in model_id:
        raise ModelPolicyError("task_model_transport_unsupported")
    if (not _model_matches_patterns(model_id, policy.tenant_patterns)
            or not _model_matches_service_restrictions(model_id, policy.service_restriction_pattern_sets)):
        raise ModelPolicyError("task_model_not_permitted")
    if str(preference.revision) != expected_policy_version:
        raise ModelPolicyError("task_model_policy_version_changed")
    target = await bedrock_routing_resolver.resolve(db, policy.context, user_id=policy.routing_user_id)
    settings = get_settings()
    account = target.account_id or (settings.platform_bedrock_account_id if target.is_platform else None)
    region = target.region or settings.aws_region
    if not account or not region:
        raise ModelPolicyError("task_model_destination_unavailable")
    evidence = await lookup_evidence(db, account_id=account, region=region, canonical_model_id=model_id,
        compatibility_class=TASK_TRANSPORT, harness_contract_revision=TASK_CONTRACT_REVISION, request_shape_sha256=TASK_REQUEST_SHAPE)
    if evidence is None or evidence.is_stale or not evidence.is_proven or not evidence.provider_request_id:
        raise ModelPolicyError("task_model_probe_required")
    state = await get_rate_state(db)
    from pricing_policy import canonical_billing_model_id
    from pricing_policy.policy import model_rate_candidates, staleness_reasons
    from pricing_policy.storage import utc_now_iso
    rates = model_rate_candidates(state.rows, canonical_billing_model_id(model_id), served_service_tier="standard")
    if (not state.from_database or state.reasons or not rates
            or any(staleness_reasons(row_verified_at=row.verified_at, now_iso=utc_now_iso()) for row in rates)):
        raise ModelPolicyError("task_model_pricing_unavailable")
    pricing_version = hashlib.sha256(json.dumps({"generation": state.generation_id,
        "pointer": state.pointer_revision, "model": model_id}, sort_keys=True).encode()).hexdigest()
    binding = {
        "model_id": model_id, "transport": TASK_TRANSPORT,
        "model_policy_version": str(preference.revision), "request_shape_version": TASK_REQUEST_SHAPE,
        "pricing_evidence_version": pricing_version, "invocability_verified": True,
    }

    return (binding, policy, target) if include_context else binding
