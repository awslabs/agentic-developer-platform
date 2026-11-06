"""Fresh Product and connection authority for retained DevOps access policies."""

from copy import deepcopy
from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.api.configuration import view
from l5_services.devops.access_policy import PolicyUnavailable, access_policy_document
from l5_services.models.binding import Binding
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.devops_space import DevopsSpaceIntent
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.auth.aws_connection_authority import verified_connection_evidence
from src.products.models import Product, Project
from src.shared.config import get_settings
from src.shared.models.vault import UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper


def without_policy(settings):
    result = deepcopy(settings)
    if isinstance(result.get("devops"), dict):
        result["devops"].pop("access_policy_ref", None)
        # Repair admission may clear expired workload selections without relying
        # on those expired observations to authorize the current revision.
        for account in result["devops"].get("accounts", []):
            for environment in account.get("environments", []):
                environment.pop("resource_refs", None)
        for trigger in result["devops"].get("trigger_policies", []):
            trigger.pop("resource_refs", None)
            trigger.pop("telemetry_refs", None)
    return result


async def resolve_access_policy(backend, *, project_id, product_id=None, binding_id=None, reference=None, settings=None):
    """Resolve in two fresh transactions around external connection-version reads."""
    from src.l5_integration import GatewayConfigurationBackend

    async def read():
        async with backend.sessions() as db:
            actor = backend.actor.model_copy(deep=True)
            fresh = GatewayConfigurationBackend(db, actor, delegation_id=backend.delegation_id)
            if reference is not None:
                if (
                    not isinstance(reference, dict)
                    or set(reference) != {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"}
                    or any(not isinstance(value, str) or not value for value in reference.values())
                    or reference.get("tenant_id") != actor.org_id
                    or reference.get("project_id") != project_id
                    or reference.get("record_type") != "policy"
                ):
                    raise HTTPException(404, detail={"code": "not_found"})
                receipt = await db.get(DevopsSpaceIntent, (actor.org_id, reference["record_id"]))
                if receipt is None or (receipt.selection or {}).get("project_id") != project_id:
                    raise HTTPException(404, detail={"code": "not_found"})
                selected_binding = (receipt.selection or {}).get("binding_id")
                selected_product = (receipt.selection or {}).get("product_id")
                if binding_id is not None and binding_id != selected_binding or product_id is not None and product_id != selected_product:
                    raise HTTPException(404, detail={"code": "not_found"})
            else:
                if actor.account_type != "human":
                    raise HTTPException(404, detail={"code": "not_found"})
                selected_binding, selected_product = binding_id, product_id
                receipt = None
            profile = await fresh.profile(selected_product, project_id)
            product = await db.get(Product, (actor.org_id, selected_product))
            project = await db.get(Project, (actor.org_id, project_id))
            if (
                product is None
                or project is None
                or product.archived_at is not None
                or project.archived_at is not None
                or not any(item.get("capability") == "devops" for item in (profile.get("configuration") or {}).get("selected_capabilities", []))
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            configuration, version = await fresh.store.current(selected_product, project_id, "devops")
            relationship = await fresh.relationship(selected_product, project_id)
            owner = actor.user_id if actor.account_type == "human" else fresh.store.owner_id
            if (
                configuration.owner_id != owner
                or configuration.handoff_state != "linked"
                or relationship is None
                or configuration.relationship_revision != str(relationship.relationship_revision)
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            current = without_policy(version.settings)
            await fresh.authorize_settings(current, project_id, "devops")
            binding = await db.get(Binding, (actor.org_id, selected_binding))
            if (
                binding is None
                or binding.product_id != selected_product
                or binding.project_id != project_id
                or binding.configuration_id != configuration.configuration_id
                or binding.scope != view(configuration, version)["scope"]
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            receipts = (
                await db.scalars(
                    select(DevopsSpaceIntent)
                    .where(
                        DevopsSpaceIntent.tenant_id == actor.org_id,
                        DevopsSpaceIntent.selection["binding_id"].as_string() == binding.binding_id,
                        DevopsSpaceIntent.configuration_revision <= version.revision,
                        DevopsSpaceIntent.native_space_id == (binding.native_resource_ref or {}).get("record_id"),
                    )
                    .order_by(DevopsSpaceIntent.configuration_revision.desc())
                    .limit(2)
                )
            ).all()
            if not receipts or len(receipts) > 1 and receipts[0].configuration_revision == receipts[1].configuration_revision:
                raise HTTPException(409, detail={"code": "access_policy_unverified"})
            if receipt is not None and receipt.operation_id != receipts[0].operation_id:
                raise HTTPException(409, detail={"code": "access_policy_changed"})
            receipt = receipts[0]
            original = await db.get(ConfigurationVersion, (actor.org_id, configuration.configuration_id, receipt.configuration_revision))
            selected = (settings if settings is not None else version.settings).get("devops") or {}
            initial = (original.settings if original else {}).get("devops") or {}
            selected_context = without_policy({"devops": selected})["devops"]
            initial_context = without_policy({"devops": initial})["devops"]
            for field in ("hosting_connection_ref", "region", "accounts"):
                if selected_context.get(field) != initial_context.get(field) or (current.get("devops") or {}).get(field) != initial_context.get(
                    field
                ):
                    raise HTTPException(409, detail={"code": "access_policy_selection_changed"})
            from src.l5_devops_resources import require_workload_refs

            for candidate in (version.settings, settings if settings is not None else version.settings):
                await require_workload_refs(
                    db,
                    tenant_id=actor.org_id,
                    project_id=project_id,
                    owner_id=configuration.owner_id,
                    settings=candidate,
                    binding_id=binding.binding_id,
                )
            if selected.get("hosting_connection_ref") != binding.connection_ref:
                raise HTTPException(409, detail={"code": "access_policy_selection_changed"})
            try:
                result = access_policy_document(binding, receipt, now=datetime.now(UTC))
            except PolicyUnavailable as exc:
                raise HTTPException(409, detail={"code": str(exc)}) from None
            if reference is not None and reference != result["policy_ref"]:
                raise HTTPException(409, detail={"code": "access_policy_changed"})
            resource = result["document"]["resource_observation"]["resource_selection"]
            for connection, role, account in (
                (binding.connection_ref, (binding.provider_identity or {}).get("role_arn"), receipt.selection["account_id"]),
                (resource["connection_ref"], resource["setup_role_arn"], resource["account_id"]),
            ):
                credential = await db.get(UserCredential, connection["source_connection_id"])
                if (
                    credential is None
                    or credential.org_id != actor.org_id
                    or credential.user_id != configuration.owner_id
                    or credential.id != connection["record_id"]
                    or credential.service != "aws"
                    or credential.credential_type != "aws_role"
                    or credential.aws_verified_version_id != connection["record_revision"]
                    or (credential.scopes or {}).get("role_arn") != role
                    or (credential.scopes or {}).get("account_id") != account
                ):
                    raise HTTPException(404, detail={"code": "not_found"})
                if actor.account_type == "service" and (
                    fresh.grant is None or {**connection, "record_type": "credential"} not in fresh.grant.sources
                ):
                    raise HTTPException(404, detail={"code": "not_found"})
                verified_connection_evidence(credential)
                helper = SecretsManagerHelper(region_name=get_settings().aws_region)
                try:
                    current_version = await run_in_threadpool(helper.current_version_id, credential.secret_arn)
                except Exception:
                    raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from None
                if current_version != connection["record_revision"]:
                    raise HTTPException(409, detail={"code": "selected_connection_changed"})
            return result

    first = await read()
    second = await read()
    if first != second:
        raise HTTPException(409, detail={"code": "access_policy_changed"})
    return second
