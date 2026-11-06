"""Prepare a conditional Profile patch without changing unrelated selections."""

from copy import deepcopy

import httpx
from fastapi import HTTPException

from .configuration_population import REFERENCE_FIELDS


def configuration_patch(profile: dict, configuration_ref: dict, capabilities: list[str], expected_revision: int) -> dict | None:
    """Return the Product API patch, or None if this reference is already linked."""
    if profile.get("revision") != expected_revision:
        raise ValueError("Profile revision changed before handoff")
    if (
        not isinstance(configuration_ref, dict)
        or set(configuration_ref) != REFERENCE_FIELDS
        or configuration_ref["tenant_id"] != profile.get("tenant_id")
        or configuration_ref["project_id"] != profile.get("project_id")
        or configuration_ref["record_type"] != "l5_configuration"
        or not all(isinstance(value, str) and value for value in configuration_ref.values())
    ):
        raise ValueError("configuration_ref must identify this scoped L5 record")
    if not capabilities or len(set(capabilities)) != len(capabilities):
        raise ValueError("handoff requires distinct selected capabilities")
    configuration = deepcopy(profile.get("configuration") or {})
    selections = configuration.get("selected_capabilities") or []
    present = {selection["capability"] for selection in selections}
    if not set(capabilities) <= present:
        raise ValueError("Profile must select each linked capability explicitly")
    changed = False
    for selection in selections:
        if selection["capability"] in capabilities and selection.get("configuration_ref") != configuration_ref:
            selection["configuration_ref"] = deepcopy(configuration_ref)
            changed = True
    return {"configuration": configuration} if changed else None


async def link_configuration(read_profile, patch_profile, configuration_ref: dict, capabilities: list[str], idempotency_key: str) -> str:
    """Apply Product's conditional patch; retry only the link after uncertain saves."""
    if not idempotency_key:
        raise ValueError("idempotency key is required for Profile handoff")
    try:
        profile = await read_profile()
        patch = configuration_patch(profile, configuration_ref, capabilities, profile["revision"])
        if patch is None:
            return "linked"
        await patch_profile(patch, profile["revision"], idempotency_key)
        return "linked"
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 412:
            return "conflict"
        if error.response.status_code >= 500:
            return "unavailable"
        raise
    except HTTPException as error:
        if error.status_code == 412:
            return "conflict"
        if error.status_code >= 500:
            return "unavailable"
        raise
    except httpx.TransportError:
        return "unavailable"
