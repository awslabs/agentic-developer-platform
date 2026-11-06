"""Read-only Investigation preflight; admission remains authoritative at submission."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from l5_services.api.configuration import view


async def investigation_readiness(backend, configuration, version):
    result = {
        "ready": False,
        "gaps": [],
        "provider_contacted": False,
        "scope": view(configuration, version)["scope"],
        "configuration_id": configuration.configuration_id,
        "configuration_revision": version.revision,
        "configuration_hash": version.hash,
        "capability": "investigation",
    }
    if backend.actor.account_type != "human" or configuration.owner_id != backend.actor.user_id:
        raise HTTPException(404, detail={"code": "not_found"})
    if version.state != "ready" or version.gaps or version.settings.get("enabled") is not True or configuration.handoff_state != "linked":
        result["gaps"] = ["configuration_disabled_or_unlinked", *version.gaps]
        return result
    if not callable(backend.publish):
        result["gaps"] = ["work_queue_unavailable"]
        return result
    triggers = [
        trigger
        for trigger in (version.settings.get("devops") or {}).get("trigger_policies", [])
        if isinstance(trigger, dict)
        and trigger.get("kind") == "manual"
        and trigger.get("enabled") is True
        and trigger.get("capability") == "investigation"
        and trigger.get("binding_id")
    ]
    if "investigation" not in version.settings.get("capabilities", []) or len(triggers) != 1:
        result["gaps"] = ["manual_investigation_trigger_required"]
        return result
    try:
        binding, _credential, grant = await backend._devops_work_context(configuration, version, triggers[0])
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        result["gaps"] = [detail.get("code", "investigation_authority_unavailable")]
        return result
    now = datetime.now(UTC)
    expiry = grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC)
    if expiry <= now:
        result["gaps"] = ["delegation_denied"]
        return result
    result.update(
        ready=True,
        binding_id=binding.binding_id,
        binding_generation=binding.generation,
        binding_revision=binding.revision,
        checked_at=now.isoformat(),
        valid_until=min(expiry, now + timedelta(minutes=1)).isoformat(),
    )
    return result
