"""Explicit manual investigation activation from retained native setup evidence.

No provider I/O, consumer enablement or paid submission occurs while saving the
configuration. Run admission still requires fresh consent for the new revision.
"""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.api.configuration import view
from l5_services.devops.setup import original_setup
from l5_services.devops_configuration import manual_investigation_selection
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from sqlalchemy import select

from src.auth.aws_connection_authority import owned_aws_connection, verified_connection_evidence
from src.auth.l5_owner_delegation import _owner

GOAL_READBACK_MAX_AGE = timedelta(minutes=15)


def _denied(code, message):
    return HTTPException(409, detail={"code": code, "message": message})


async def authorize_activation(backend, configuration_id, settings):
    trigger = manual_investigation_selection(settings)
    db, tenant = backend.db, backend.actor.org_id
    config = await db.scalar(
        select(Configuration)
        .where(
            Configuration.tenant_id == tenant,
            Configuration.configuration_id == configuration_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if config is None or config.service_id != "devops":
        raise HTTPException(404, detail={"code": "not_found"})
    config, previous = await _owner(backend, config.product_id, config.project_id, "devops")
    await backend.authorize_settings(settings, config.project_id, "devops")
    selected = settings["devops"]
    connection = selected.get("hosting_connection_ref") or {}
    credential = await owned_aws_connection(db, connection.get("record_id"), backend.actor.user_id, tenant)
    verified_connection_evidence(credential)
    identity = {**(credential.scopes or {}), "region": selected.get("region")}
    if selected["accounts"][0].get("account_id") != identity.get("account_id"):
        raise _denied("activation_environment_changed", "Select the verified hosting account and its current Product environment.")
    binding = await db.get(Binding, (tenant, trigger["binding_id"]), populate_existing=True)
    expected_scope = {**view(config, previous)["scope"], "visibility": settings["visibility"]}
    if (
        binding is None
        or binding.state != "ready"
        or binding.gaps
        or (binding.product_id, binding.project_id, binding.configuration_id, binding.service_id)
        != (config.product_id, config.project_id, configuration_id, "devops")
        or binding.scope != expected_scope
        or binding.connection_ref != connection
        or not isinstance(binding.native_resource_ref, dict)
        or any((binding.provider_identity or {}).get(field) != identity.get(field) for field in ("account_id", "role_arn", "region"))
    ):
        raise _denied("setup_activation_required", "Complete setup of the exact selected DevOps space before activating manual investigation.")
    if selected.get("browser_launch_policy") == "scoped_session" and any(
        selected.get(field) != (previous.settings.get("devops") or {}).get(field)
        for field in ("browser_launch_policy", "authentication_mode", "launcher_role_arn", "operator_role_arn")
    ):
        raise _denied("setup_activation_required", "Keep the separately configured SSO policy unchanged during manual activation.")
    try:
        setup = await original_setup(db, binding, configuration=config, settings=settings)
    except (ValueError, TypeError, KeyError):
        raise _denied(
            "setup_provenance_unavailable", "The selected binding needs its successful original create or exact-space link operation."
        ) from None
    selection = SpaceSelection(tenant, config.product_id, config.project_id, binding.binding_id, identity["account_id"], identity["region"])
    observed = setup.prevention_observation or {}
    schedule = observed.get("native_schedule") or {}
    goals = schedule.get("goals")
    try:
        observed_at = datetime.fromisoformat(observed["observed_at"].replace("Z", "+00:00"))
        fresh = observed_at.tzinfo is not None and timedelta(0) <= datetime.now(UTC) - observed_at <= GOAL_READBACK_MAX_AGE
    except (KeyError, TypeError, ValueError, AttributeError):
        fresh = False
    if (
        not fresh
        or setup.scope_fingerprint != selection.fingerprint()
        or setup.selection
        != {
            "tenant_id": tenant,
            "product_id": config.product_id,
            "project_id": config.project_id,
            "binding_id": binding.binding_id,
            "account_id": identity["account_id"],
            "region": identity["region"],
        }
        or observed.get("scope_fingerprint") != selection.fingerprint()
        or observed.get("configuration_revision") not in {setup.configuration_revision, previous.revision}
        or observed.get("native_space_id") != setup.native_space_id
        or schedule.get("enabled") is not False
        or not isinstance(goals, list)
        or not goals
        or any(not isinstance(goal, dict) or goal.get("state") != "DISABLED" or not goal.get("goal_id") for goal in goals)
    ):
        raise _denied(
            "native_goal_readback_stale",
            "Activation requires a native readback from the last 15 minutes confirming every goal is disabled for this exact space.",
        )
