"""Exact same-account Lambda/log selection for a disabled DevOps draft."""

from dataclasses import asdict

from l5_services.api.configuration import view
from l5_services.devops.resource_scope import ResourceSelection


def resource_selection_valid(raw, binding, configuration, version, credential, *, generation, admitted=None):
    if not isinstance(raw, dict):
        raise ValueError("resource_selection_required")
    selection = ResourceSelection(**raw)
    selection.fingerprint()
    connection = (version.settings.get("devops") or {}).get("hosting_connection_ref")
    identity = credential.scopes or {}
    if (
        selection.workload_kind != "lambda"
        or selection.account_id != identity.get("account_id")
        or selection.setup_role_arn != identity.get("role_arn")
        or selection.region != version.settings["devops"].get("region")
        or selection.connection_ref != {**connection, "record_type": "credential"}
        or binding is None
        or (binding.tenant_id, binding.product_id, binding.project_id, binding.service_id, binding.configuration_id)
        != (configuration.tenant_id, configuration.product_id, configuration.project_id, "devops", configuration.configuration_id)
        or binding.scope != view(configuration, version)["scope"]
        or binding.connection_ref != connection
        or generation
        not in (
            {binding.generation}
            if admitted is True
            else {binding.generation, binding.generation + 1}
            if admitted is None
            else {binding.generation + 1}
        )
        or binding.state not in {"checking", "ready", "blocked"}
        or binding.ownership not in {"adp_created", "customer_linked"}
        or (
            binding.ownership == "adp_created"
            and (
                not binding.creation_receipt
                or binding.creation_receipt.get("native_resource_ref") != binding.native_resource_ref
                or binding.creation_receipt.get("provider_identity") != binding.provider_identity
            )
        )
        or (
            binding.ownership == "customer_linked"
            and (
                binding.creation_receipt is not None
                or (binding.native_resource_ref or {}).get("record_id") != version.settings["devops"].get("existing_space_id")
            )
        )
        or binding.provider_identity != {"account_id": selection.account_id, "role_arn": selection.setup_role_arn, "region": selection.region}
    ):
        raise ValueError("resource_selection_outside_binding")
    return asdict(selection)


async def current_resource_environment(db, *, tenant_id, project_id, settings):
    from types import SimpleNamespace

    from sqlalchemy import select

    from src.products.models import ProductSourceBinding
    from src.products.source_routes import allowed_source_ref

    selected = settings.get("devops") or {}
    accounts = selected.get("accounts") or []
    sources = settings.get("source_refs") or []
    if len(accounts) != 1 or len(sources) != 1:
        return False
    environments = accounts[0].get("environments") or []
    connection = selected.get("hosting_connection_ref") or {}
    if (
        len(environments) != 1
        or environments[0].get("environment_ref") != sources[0]
        or sources[0].get("record_type") != "environment"
        or sources[0].get("source_connection_id") != connection.get("record_id")
    ):
        return False
    binding = await db.scalar(
        select(ProductSourceBinding)
        .where(
            ProductSourceBinding.tenant_id == tenant_id,
            ProductSourceBinding.project_id == project_id,
            ProductSourceBinding.credential_id == connection.get("record_id"),
            ProductSourceBinding.provider == "aws",
            ProductSourceBinding.kind == "environment",
            ProductSourceBinding.namespace == accounts[0].get("account_id"),
            ProductSourceBinding.native_id == selected.get("region"),
            ProductSourceBinding.status == "healthy",
            ProductSourceBinding.unlinked_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    return (
        binding is not None
        and allowed_source_ref(SimpleNamespace(tenant_id=tenant_id, project_id=project_id, sources=sources, execution_scope=None), binding)
        == sources[0]
    )
