"""Project current L5 configuration readiness without changing admission authority."""

import re
from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.api.bindings import view_binding
from l5_services.configuration_readiness import assess_readiness
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.security_registration import current_model, current_registration
from sqlalchemy import select

from src.auth.aws_connection_authority import verified_connection_evidence
from src.auth.l5_delegation import required_l5_work_operations
from src.auth.l5_execution_scope import current_execution_scope
from src.l5_service_identity import worker_id
from src.products.models import ProductChange, ProductOperation, ProductSourceBinding
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


def _active(expiry):
    return expiry is not None and expiry.replace(tzinfo=expiry.tzinfo or UTC) > datetime.now(UTC)


async def current_profile_revision(db, configuration, version, profile):
    reference = version.settings.get("profile_ref") or {}
    saved_revision = reference.get("record_revision")
    current_revision = profile.get("revision")
    if saved_revision == str(current_revision):
        return True
    if (
        not isinstance(saved_revision, str)
        or not re.fullmatch(r"[1-9][0-9]*", saved_revision)
        or type(current_revision) is not int
        or current_revision <= int(saved_revision)
    ):
        return False
    rows = (
        await db.execute(
            select(ProductOperation, ProductChange)
            .join(
                ProductChange,
                (ProductChange.tenant_id == ProductOperation.tenant_id) & (ProductChange.operation_id == ProductOperation.id),
            )
            .where(
                ProductChange.tenant_id == configuration.tenant_id,
                ProductChange.product_id == configuration.product_id,
                ProductChange.revision > int(saved_revision),
                ProductChange.revision <= current_revision,
            )
            .order_by(ProductChange.revision)
        )
    ).all()
    if configuration.handoff_state != "linked" or len(rows) != current_revision - int(saved_revision):
        return False
    for expected_revision, (operation, change) in enumerate(rows, start=int(saved_revision) + 1):
        request = operation.request or {}
        linked_ref = request.get("configuration_ref") or {}
        linked_revision = linked_ref.get("record_revision")
        if (
            change.revision != expected_revision
            or operation.operation != "linkL5Configuration"
            or operation.state != "succeeded"
            or operation.actor_id != change.actor_id
            or operation.product_id != configuration.product_id
            or request.get("product_id") != configuration.product_id
            or operation.project_id != change.project_id
            or request.get("project_id") != change.project_id
            or request.get("expected_revision") != expected_revision - 1
            or operation.receipt != {"revision": expected_revision}
            or change.changed_paths != ["/project/configuration/selected_capabilities"]
            or not isinstance(linked_revision, str)
            or not re.fullmatch(r"[1-9][0-9]*", linked_revision)
        ):
            return False
        linked = await db.get(Configuration, (configuration.tenant_id, linked_ref.get("record_id")))
        linked_version = await db.get(ConfigurationVersion, (configuration.tenant_id, linked_ref.get("record_id"), int(linked_revision)))
        if (
            linked is None
            or linked_version is None
            or linked.product_id != configuration.product_id
            or linked.project_id != change.project_id
            or linked.owner_id != operation.actor_id
        ):
            return False
        expected_ref = {
            **(linked_version.settings.get("profile_ref") or {}),
            "record_type": "l5_configuration",
            "record_id": linked.configuration_id,
            "record_revision": str(linked_version.revision),
        }
        capabilities = (
            [linked.service_id] if linked.service_id in {"devops", "finops", "well_architected"} else linked_version.settings.get("capabilities")
        )
        if linked_ref != expected_ref or not capabilities or request.get("capabilities") != capabilities:
            return False
    return True


async def project_readiness(db, configuration, version, service_id, profile):
    settings = version.settings
    tenant_id, product_id, project_id = configuration.tenant_id, configuration.product_id, configuration.project_id
    selections = {
        item.get("capability")
        for item in (profile.get("configuration") or {}).get("selected_capabilities") or []
        if isinstance(item, dict) and item.get("ownership_mode") != "not_applicable"
    }
    required = {"security": set(settings.get("capabilities") or []), "finops": {"finops"}, "well_architected": {"well_architected"}}[service_id]
    gaps = []
    if not required or not required <= selections:
        gaps.append("capability_not_selected")
    if configuration.handoff_state != "linked":
        gaps.append("configuration_not_linked")
    if settings.get("enabled") is not True:
        gaps.append("configuration_disabled")
    if not await current_profile_revision(db, configuration, version, profile):
        gaps.append("profile_revision_stale")
    sources = settings.get("source_refs") or []
    repositories = [source for source in sources if isinstance(source, dict) and source.get("record_type") in {"repository", "repository_commit"}]
    if repositories:
        rows = (
            await db.scalars(
                select(ProductSourceBinding).where(
                    ProductSourceBinding.tenant_id == tenant_id,
                    ProductSourceBinding.project_id == project_id,
                    ProductSourceBinding.native_id.in_([source["record_id"] for source in repositories]),
                )
            )
        ).all()
        for source in repositories:
            if not any(
                row.kind == "repository"
                and row.status == "healthy"
                and row.unlinked_at is None
                and (row.installation_id or row.credential_id) == source.get("source_connection_id")
                and (row.observed or {}).get("source_revision") == source.get("record_revision")
                for row in rows
                if row.native_id == source.get("record_id")
            ):
                gaps.append("source_revision_stale")
                break
    connection = settings.get("connection_ref")
    credential = await db.get(UserCredential, connection.get("record_id")) if isinstance(connection, dict) else None
    if (
        credential is None
        or credential.id != connection.get("source_connection_id")
        or credential.org_id != tenant_id
        or credential.user_id != configuration.owner_id
        or credential.service != "aws"
        or credential.credential_type != "aws_role"
        or credential.aws_verified_version_id != connection.get("record_revision")
    ):
        gaps.append("connection_revision_unavailable")
    else:
        try:
            verified_connection_evidence(credential)
        except HTTPException:
            gaps.append("connection_revision_unavailable")
    bindings = (
        await db.scalars(
            select(Binding).where(
                Binding.tenant_id == tenant_id,
                Binding.product_id == product_id,
                Binding.project_id == project_id,
                Binding.service_id == service_id,
                Binding.configuration_id == configuration.configuration_id,
            )
        )
    ).all()
    scope = {
        "tenant_id": tenant_id,
        "product_id": product_id,
        "project_id": project_id,
        "relationship_revision": configuration.relationship_revision,
        "owner_id": configuration.owner_id,
        "visibility": settings.get("visibility"),
    }
    selected_ids = {
        trigger.get("binding_id") for trigger in settings.get("triggers") or [] if isinstance(trigger, dict) and trigger.get("enabled") is True
    }
    valid = [
        binding
        for binding in bindings
        if binding.state == "ready"
        and not binding.gaps
        and binding.scope == scope
        and binding.connection_ref == connection
        and (not selected_ids or binding.binding_id in selected_ids)
    ]
    if not valid or (selected_ids and selected_ids != {binding.binding_id for binding in valid}):
        gaps.append("binding_or_delegation_unavailable")
    grants = (
        await db.scalars(
            select(CredentialServiceDelegation).where(
                CredentialServiceDelegation.tenant_id == tenant_id,
                CredentialServiceDelegation.product_id == product_id,
                CredentialServiceDelegation.project_id == project_id,
                CredentialServiceDelegation.service_id == worker_id(service_id),
                CredentialServiceDelegation.created_by == configuration.owner_id,
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
        )
    ).all()
    session_ref = {**connection, "record_type": "credential"} if isinstance(connection, dict) else None
    scoped_grants = []
    for grant in grants:
        for binding in valid:
            if await current_execution_scope(db, grant, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation):
                scoped_grants.append(grant)
                break
    active = (
        any(
            grant.credential_id == getattr(credential, "id", None)
            and grant.parent_delegation_id is None
            and grant.operation_id is None
            and grant.attempt_id is None
            and grant.lease_fence is None
            and _active(grant.expires_at)
            and set(required_l5_work_operations(service_id, sources)).issubset(grant.operations)
            and set(settings.get("capabilities") or []) <= set(grant.capabilities)
            and session_ref in grant.sources
            and all(source in grant.sources for source in sources)
            and any(binding.generation == grant.binding_generation for binding in valid)
            for grant in scoped_grants
        )
        and "connection_revision_unavailable" not in gaps
    )
    if not active:
        gaps.append("execution_delegation")
    if service_id == "security":
        if (
            "code_review" in required
            and not any(current_registration(binding) is not None for binding in valid)
            or "threat_model" in required
            and not any(current_model(binding) is not None for binding in valid)
        ):
            gaps.append("native_unqualified")
        try:
            _, configured_gaps, _ = assess_readiness(
                settings,
                configuration.configuration_id,
                [view_binding(binding) for binding in valid],
                delegation_active=active,
            )
            gaps.extend(configured_gaps)
        except ValueError:
            gaps.append("binding_or_delegation_unavailable")
    else:
        try:
            _, configured_gaps, _ = assess_readiness(
                settings,
                configuration.configuration_id,
                [view_binding(binding) for binding in valid],
                delegation_active=active,
                service_id=service_id,
            )
        except ValueError:
            configured_gaps = ["configuration_disabled"]
        native = any(
            isinstance(binding.native_resource_ref, dict)
            and isinstance(binding.provider_identity, dict)
            and binding.readiness
            and credential is not None
            and binding.provider_identity.get("account_id") == (credential.scopes or {}).get("account_id")
            and binding.provider_identity.get("role_arn") == (credential.scopes or {}).get("role_arn")
            and binding.provider_identity.get("region") == settings.get("region")
            and binding.native_resource_ref.get("tenant_id") == tenant_id
            and binding.native_resource_ref.get("project_id") == project_id
            and binding.native_resource_ref.get("source_connection_id") == connection.get("source_connection_id")
            and (
                service_id != "well_architected"
                or binding.native_resource_ref.get("record_type") == "agent_profile"
                and isinstance(binding.native_resource_ref.get("record_revision"), str)
                and re.fullmatch(r"[a-f0-9]{64}", binding.native_resource_ref["record_revision"]) is not None
                and re.fullmatch(
                    rf"arn:aws:wellarchitected:{re.escape(settings['region'])}:"
                    rf"{re.escape(binding.provider_identity['account_id'])}:agent-profile/[A-Za-z0-9_-]+",
                    binding.native_resource_ref.get("record_id", ""),
                )
                is not None
                and any(
                    readback.get("snapshot_hash") == binding.native_resource_ref.get("record_revision")
                    for readback in binding.readiness
                    if isinstance(readback, dict)
                )
            )
            and (service_id != "finops" or binding.native_resource_ref.get("record_type") == "finops_agent")
            for binding in valid
        )
        placeholders = set() if service_id == "finops" else {"catalog_mapping_unresolved", "provider_unqualified"}
        gaps.extend(gap for gap in configured_gaps if gap not in placeholders or not (native and active))
        if not native:
            gaps.append("native_unqualified" if service_id == "finops" else "provider_unqualified")
    gaps = list(dict.fromkeys(gaps))
    return {"ready": not gaps, "gaps": gaps, "provider_contacted": False}
