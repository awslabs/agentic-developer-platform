"""Resolve a saved preference once, before publishing a persona invocation.

The trusted producer supplies the initiating human, including on delegated and
AI-DLC work. The result chooses a model; it grants no identity, credentials,
destination access or budget. Protected-worker policy remains a separate path.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from src.admin.persona_models import catalogue_service, service


def mapping_enabled() -> bool:
    return os.environ.get("PERSONA_MODEL_MAPPING_ENABLED", "false").lower() == "true"


async def select_for_dispatch(db, *, org_id: str, user_id: str, persona: str, direct_model: str | None = None) -> dict:
    """Read the canonical human's preference with current selection restrictions."""
    from src.agentauth.model_policy import _resolve_active_allowlist_policy
    from src.shared.config import get_settings

    principal = await service.validate_human_principal(db, user_id=user_id, org_id=org_id)
    preference = await service.get_preference(db, org_id=org_id, principal_kind="human", principal_id=principal, persona_key=persona)
    compatibility, default, _ = await service.get_persona_class_default(db, persona)
    model = direct_model if direct_model is not None else preference.canonical_model_id if preference else default
    if model is None:
        return {"model": None, "source": "runtime-default", "persona": persona, "principal_id": principal}
    policy = await _resolve_active_allowlist_policy(
        db,
        tenant_id=org_id,
        principal_kind="human",
        principal_id=principal,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        settings=get_settings(),
    )
    result = await catalogue_service.validate_selection(
        db,
        org_id=org_id,
        principal_kind="human",
        canonical_principal_id=principal,
        persona_key=persona,
        model=model,
        tenant_allowed_patterns=policy.tenant_patterns,
        require_evidence=False,
    )
    if isinstance(result, catalogue_service.SelectionRejection):
        raise service.PreferenceRejectedError(result.reason, result.message)
    return {
        "model": result.canonical_model_id,
        "source": "explicit-direct" if direct_model is not None else "principal-mapping" if preference else "system-default",
        "persona": persona,
        "principal_id": principal,
        "preference_revision": preference.revision if preference and direct_model is None else None,
        "compatibility_class": compatibility,
    }


async def apply_dispatch_selection(db, envelope: dict) -> dict:
    """For in-process trusted producers, select before the envelope is sealed."""
    if not mapping_enabled():
        return envelope
    correlation = envelope.get("correlation") or {}
    if correlation.get("is_human_rooted") is not True:
        return envelope
    selection = await select_for_dispatch(
        db,
        org_id=envelope["tenant_id"],
        user_id=correlation.get("root_human_id", ""),
        persona=envelope["persona"],
        direct_model=envelope.get("model_requested"),
    )
    result = {**envelope, "model_selection": selection}
    if selection["model"] is not None:
        result["model_resolved"] = selection["model"]
    return result


async def resolve_launch_configuration(db, *, org_id: str, user_id: str, persona: str) -> dict:
    """Resolve agent settings before admission/sealing, regardless of trigger.

    The flow engine supplies identity and persona only. This launch layer owns
    preference lookup, feature posture, validation, and envelope configuration.
    Lookup failures propagate so a producer cannot silently use a worker default.
    """
    identity = {
        "tenant_id": org_id,
        "persona": persona,
        "correlation": {"is_human_rooted": True, "root_human_id": user_id},
    }
    configured = await apply_dispatch_selection(db, identity)
    return {key: value for key, value in configured.items() if key not in identity}
