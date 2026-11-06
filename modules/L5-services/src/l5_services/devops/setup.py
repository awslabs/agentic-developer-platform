"""Shared setup identities and atomic DevOps lifecycle registration."""

from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.models.devops_space import DevopsSpaceIntent


async def original_setup(session, binding, *, configuration=None, settings=None, require_setup_consent=True):
    """Resolve original create/link provenance; later checks never become setup.

    Adoption is proved by the exact successful link intent, not a creation
    receipt. Never select a newer observation or an arbitrary matching row.
    """
    from l5_services.api.bindings import _intent_hash
    from l5_services.models.configuration import Configuration, ConfigurationVersion
    from l5_services.models.setup import BindingOperation, SetupIntent

    if binding is None or binding.service_id != "devops" or binding.ownership not in {"adp_created", "customer_linked"}:
        raise ValueError("setup_provenance_unavailable")
    adopted = binding.ownership == "customer_linked"
    receipt = binding.creation_receipt
    if adopted and receipt is not None or not adopted and not isinstance(receipt, dict):
        raise ValueError("setup_provenance_unavailable")
    rows = (await session.execute(select(DevopsSpaceIntent, SetupIntent, BindingOperation).join(
        SetupIntent, (SetupIntent.tenant_id == DevopsSpaceIntent.tenant_id)
        & (SetupIntent.operation_id == DevopsSpaceIntent.operation_id),
    ).join(BindingOperation, (BindingOperation.tenant_id == SetupIntent.tenant_id)
           & (BindingOperation.operation_id == SetupIntent.operation_id)).where(
        DevopsSpaceIntent.tenant_id == binding.tenant_id,
        DevopsSpaceIntent.selection["binding_id"].as_string() == binding.binding_id,
        SetupIntent.request["action"].as_string() == ("link" if adopted else "create"),
        BindingOperation.binding_id == binding.binding_id,
    ).execution_options(populate_existing=True))).all()
    if len(rows) != 1:
        raise ValueError("setup_provenance_unavailable")
    setup, intent, operation = rows[0]
    configuration = configuration or await session.get(
        Configuration, (binding.tenant_id, binding.configuration_id), populate_existing=True)
    version = await session.get(ConfigurationVersion, (binding.tenant_id, binding.configuration_id,
                                                      setup.configuration_revision), populate_existing=True)
    identity = binding.provider_identity or {}
    reference = binding.native_resource_ref or {}
    selection = setup_selection(intent, {**identity, "binding_id": binding.binding_id})
    if (
        configuration is None or version is None
        or (configuration.tenant_id, configuration.configuration_id, configuration.product_id,
            configuration.project_id, configuration.service_id, configuration.owner_id)
        != (binding.tenant_id, binding.configuration_id, binding.product_id, binding.project_id,
            "devops", intent.actor_id)
        or (intent.configuration_id, intent.product_id, intent.project_id, intent.service_id, intent.scope)
        != (binding.configuration_id, binding.product_id, binding.project_id, "devops", binding.scope)
        or intent.binding_generation != 1 or not intent.idempotency_key
        or (require_setup_consent and (intent.authorization.get("purpose") != "devops_setup"
            or intent.authorization.get("control_action") != "devops_setup"))
        or intent.intent_hash != _intent_hash(intent)
        or operation.action != intent.request.get("action") or operation.state != "succeeded" or operation.gaps
        or setup.intent_hash != intent.intent_hash or setup.state != "succeeded"
        or setup.configuration_revision != intent.request.get("configuration_revision")
        or setup.ownership != binding.ownership or setup.selection != asdict(selection)
        or setup.scope_fingerprint != selection.fingerprint()
        or setup.native_space_id != reference.get("record_id")
        or (version.settings.get("devops") or {}).get("hosting_connection_ref") != binding.connection_ref
        or (require_setup_consent and reference != {**binding.connection_ref, "record_type": "agent_space",
                         "record_id": setup.native_space_id, "record_revision": setup.native_space_id})
    ):
        raise ValueError("setup_provenance_unavailable")
    for selected in [(version.settings.get("devops") or {}), ((settings or version.settings).get("devops") or {})]:
        if (selected.get("hosting_connection_ref") != binding.connection_ref
                or selected.get("region") != identity.get("region")
                or (require_setup_consent and (selected.get("space_mode") != ("adopt" if adopted else "create")
                    or selected.get("existing_space_id") != (setup.native_space_id if adopted else None)))):
            raise ValueError("setup_provenance_unavailable")
    if adopted:
        authority = intent.authorization
        if (intent.request.get("native_resource_ref") != reference or (require_setup_consent and (
                authority.get("setup_action") != "link"
                or authority.get("native_resource_ref") != reference
                or authority.get("connection_ref") != binding.connection_ref
                or authority.get("provider_identity") != identity
                or authority.get("configuration_hash") != version.hash))):
            raise ValueError("setup_provenance_unavailable")
    elif (receipt.get("operation_id") != intent.operation_id or receipt.get("intent_hash") != intent.intent_hash
          or receipt.get("native_resource_ref") != reference or receipt.get("provider_identity") != identity):
        raise ValueError("setup_provenance_unavailable")
    return setup


def setup_binding_id(intent):
    return str(uuid5(NAMESPACE_URL, f"l5:devops:{intent.tenant_id}:{intent.operation_id}"))


def setup_selection(intent, verified):
    return SpaceSelection(intent.tenant_id, intent.product_id, intent.project_id,
                          verified.get("binding_id") or intent.request.get("binding_id") or setup_binding_id(intent),
                          verified["account_id"], verified["region"])


async def record_setup(session, intent, binding, native):
    selection = setup_selection(intent, {**binding.provider_identity, "binding_id": binding.binding_id})
    observation = native["prevention_observation"]
    if (observation.get("scope_fingerprint") != selection.fingerprint()
            or observation.get("configuration_revision") != intent.request["configuration_revision"]
            or observation.get("native_space_id") != binding.native_resource_ref["record_id"]):
        raise ValueError("DevOps setup observation scope changed")
    if intent.request.get("action") == "check":
        # General execution consent historically allowed create/link readbacks.
        # Refreshing their observation grants no disabled-setup or activation authority.
        receipt = await original_setup(session, binding, require_setup_consent=False)
    else:
        receipt = (await session.scalars(select(DevopsSpaceIntent).where(
            DevopsSpaceIntent.tenant_id == intent.tenant_id,
            DevopsSpaceIntent.selection["binding_id"].as_string() == binding.binding_id,
            DevopsSpaceIntent.configuration_revision == intent.request["configuration_revision"],
        ))).one_or_none()
    if receipt is None:
        receipt = DevopsSpaceIntent(tenant_id=intent.tenant_id, operation_id=intent.operation_id,
                                   selection=asdict(selection), configuration_revision=intent.request["configuration_revision"],
                                   intent_hash=intent.intent_hash, state="succeeded", native_space_id=binding.native_resource_ref["record_id"],
                                   ownership=binding.ownership, scope_fingerprint=selection.fingerprint(),
                                   prevention_policy={"enabled": False}, prevention_observation=native["prevention_observation"])
        session.add(receipt)
    elif (receipt.selection != asdict(selection)
          or receipt.native_space_id != binding.native_resource_ref["record_id"]):
        raise ValueError("DevOps setup receipt changed")
    else:
        receipt.scope_fingerprint = selection.fingerprint()
        receipt.prevention_observation = observation
