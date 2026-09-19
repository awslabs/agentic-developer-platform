"""Owner-facing availability using the same live policy and catalogue gates."""

from datetime import UTC, datetime, timedelta

from . import catalogue, catalogue_service
from .catalogue_schemas import SelectionRejection


async def annotate_preferences(db, entries: list[dict], *, org_id: str, principal_kind: str, principal_id: str) -> list[dict]:
    from src.agentauth.model_policy import _resolve_active_allowlist_policy
    from src.proxy.bedrock_routing import bedrock_routing_resolver
    from src.shared.config import get_settings

    active = None
    destination = None
    policy_error = None
    for entry in entries:
        model_id = entry.get("effective_model_id")
        model = catalogue.catalogue_lookup(model_id) if model_id else None
        reason = None
        entry["model_lifecycle"] = model.lifecycle if model else None
        entry["availability_status"] = "unknown"
        if not model_id:
            reason = "class_default_unavailable"
        elif model is None:
            reason = "unknown_model"
        elif model.lifecycle == "retired":
            reason = "retired"
        elif entry.get("effective_is_candidate"):
            reason = "class_default_unproven"
        else:
            try:
                async with db.begin_nested():
                    if active is None and policy_error is None:
                        try:
                            settings = get_settings()
                            active = await _resolve_active_allowlist_policy(
                                db,
                                tenant_id=org_id,
                                principal_kind=principal_kind,
                                principal_id=principal_id,
                                expires_at=datetime.now(UTC) + timedelta(minutes=1),
                                settings=settings,
                            )
                            target = await bedrock_routing_resolver.resolve(db, active.context, user_id=active.routing_user_id)
                            destination = (
                                target.account_id or (settings.platform_bedrock_account_id if target.is_platform else None),
                                target.region or settings.aws_region or None,
                            )
                        except Exception as exc:
                            policy_error = getattr(exc, "reason", "availability_unknown")
                            raise
                    if policy_error:
                        reason = policy_error
                    else:
                        result = await catalogue_service.validate_selection(
                            db,
                            org_id=org_id,
                            principal_kind=principal_kind,
                            canonical_principal_id=principal_id,
                            persona_key=entry["persona_key"],
                            model=model_id,
                            account_id=destination[0],
                            region=destination[1],
                            tenant_allowed_patterns=active.tenant_patterns,
                            service_restriction_pattern_sets=active.service_restriction_pattern_sets,
                            policy_unavailable_reason=active.service_policy_unavailable_reason,
                            principal_status=active.principal_status,
                        )
                        if isinstance(result, SelectionRejection):
                            reason = result.reason
                        else:
                            entry["availability_status"] = "verified"
            except Exception as exc:
                reason = getattr(exc, "reason", "availability_unknown")
        if reason:
            entry["availability_status"] = (
                "stale"
                if reason == "evidence_stale"
                else "disallowed"
                if reason == "not_permitted"
                else "unknown"
                if reason in {"availability_unknown", "principal_unavailable"}
                else "unavailable"
            )
            entry["availability_reason"] = reason
            entry["warnings"] = [f"{model_id or 'Class default'}: {reason.replace('_', ' ')}. Choose a verified model before starting a new run."]
        else:
            entry["availability_reason"] = None
            entry["warnings"] = []
    return entries
