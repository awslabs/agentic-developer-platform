"""Resolve explicit Task model selection using live policy and exact evidence.

Each Task transport has its own probe key. A successful CLI/reviewer probe
cannot silently certify a different Task payload.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from src.admin.persona_models.catalogue import catalogue_lookup, persona_compatibility_class
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

TASK_CYBER_PERSONA = "agent-task-cyber"
TASK_CYBER_CONTRACT_REVISION = "task-cyber-sdk-messages-v1"
TASK_CYBER_PROBE_BODY = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "Call task_probe with value OK."}]}],
    "tools": [
        {
            "name": "task_probe",
            "description": "Return probe evidence.",
            "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
        }
    ],
    "tool_choice": {"type": "tool", "name": "task_probe"},
}
TASK_CYBER_REQUEST_SHAPE = hashlib.sha256(json.dumps(TASK_CYBER_PROBE_BODY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# Probe the gateway-normalized text transport, not the legacy reviewer's direct
# SDK/proxy grant. Full tool/reasoning history will require a new contract probe.
TASK_RESPONSES_PROBE_BODY = {
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply OK."}]}],
    "reasoning": {"effort": "medium"},
    "max_output_tokens": 64,
}
TASK_RESPONSES_REQUEST_SHAPE = hashlib.sha256(json.dumps(TASK_RESPONSES_PROBE_BODY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def resolve_task_model(db, *, tenant, principal, deadline, expected_policy_version, include_context=False, persona=TASK_PERSONA):
    from src.agentauth.task_responses_contract import TASK_RESPONSES_REVISION, TASK_RESPONSES_TRANSPORT

    responses = persona.startswith("agent-task-") and persona_compatibility_class(persona) == "codex-sdk"
    if persona not in {TASK_PERSONA, TASK_CYBER_PERSONA} and not responses:
        raise ModelPolicyError("task_model_transport_unsupported")
    transport = TASK_RESPONSES_TRANSPORT if responses else TASK_TRANSPORT
    compatibility = "codex-sdk" if responses else TASK_TRANSPORT
    revision = TASK_RESPONSES_REVISION if responses else TASK_CYBER_CONTRACT_REVISION if persona == TASK_CYBER_PERSONA else TASK_CONTRACT_REVISION
    shape = TASK_RESPONSES_REQUEST_SHAPE if responses else TASK_CYBER_REQUEST_SHAPE if persona == TASK_CYBER_PERSONA else TASK_REQUEST_SHAPE
    policy = await _resolve_active_allowlist_policy(
        db, tenant_id=tenant, principal_kind="service_account", principal_id=principal, expires_at=deadline
    )
    if policy.principal_status != "active" or policy.service_policy_unavailable_reason:
        raise ModelPolicyError("task_model_policy_unavailable")
    preference = await db.scalar(
        select(PersonaModelPreference).where(
            PersonaModelPreference.org_id == tenant,
            PersonaModelPreference.principal_kind == "service_account",
            PersonaModelPreference.principal_id == principal,
            PersonaModelPreference.persona_key == persona,
        )
    )
    # This transport has no implicit CLI-class default. Enrollment must record
    # an explicit administrator-selected model for the Task persona.
    if preference is None:
        raise ModelPolicyError("task_model_selection_missing")
    model_id = preference.canonical_model_id
    model = catalogue_lookup(model_id)
    from pricing_policy import canonical_billing_model_id
    from pricing_policy.policy import is_openai_model

    supported = is_openai_model(canonical_billing_model_id(model_id)) if responses else ".anthropic." in model_id
    if model is None or model.lifecycle == "retired" or not supported:
        raise ModelPolicyError("task_model_transport_unsupported")
    if not _model_matches_patterns(model_id, policy.tenant_patterns) or not _model_matches_service_restrictions(
        model_id, policy.service_restriction_pattern_sets
    ):
        raise ModelPolicyError("task_model_not_permitted")
    if str(preference.revision) != expected_policy_version:
        raise ModelPolicyError("task_model_policy_version_changed")
    target = await bedrock_routing_resolver.resolve(db, policy.context, user_id=policy.routing_user_id)
    settings = get_settings()
    account = target.account_id or (settings.platform_bedrock_account_id if target.is_platform else None)
    region = target.region or settings.aws_region
    if not account or not region:
        raise ModelPolicyError("task_model_destination_unavailable")
    evidence = await lookup_evidence(
        db,
        account_id=account,
        region=region,
        canonical_model_id=model_id,
        compatibility_class=compatibility,
        harness_contract_revision=revision,
        request_shape_sha256=shape,
    )
    if evidence is None or evidence.is_stale or not evidence.is_proven or not evidence.provider_request_id:
        raise ModelPolicyError("task_model_probe_required")
    state = await get_rate_state(db)
    from pricing_policy.policy import model_rate_candidates, staleness_reasons
    from pricing_policy.storage import utc_now_iso

    rates = model_rate_candidates(state.rows, canonical_billing_model_id(model_id), served_service_tier="standard")
    if (
        not state.from_database
        or state.reasons
        or not rates
        or any(staleness_reasons(row_verified_at=row.verified_at, now_iso=utc_now_iso()) for row in rates)
    ):
        raise ModelPolicyError("task_model_pricing_unavailable")
    pricing_version = hashlib.sha256(
        json.dumps({"generation": state.generation_id, "pointer": state.pointer_revision, "model": model_id}, sort_keys=True).encode()
    ).hexdigest()
    binding = {
        "model_id": model_id,
        "transport": transport,
        "model_policy_version": str(preference.revision),
        "request_shape_version": shape,
        "pricing_evidence_version": pricing_version,
        "invocability_verified": True,
    }

    return (binding, policy, target) if include_context else binding
