"""Resolve scheduled work from current owner consent; the caller stays a service."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import botocore.session
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from l5_services.adapters.well_architected.binding import ProfileBindingProvider, ProfileSelection
from l5_services.api.configuration import view
from l5_services.configuration_readiness import effective_monitoring
from l5_services.events.schedules import wa_configured
from l5_services.events.sources import _verify_well_architected_schedule
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import semantic_key
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.auth.aws_connection_authority import verified_connection_evidence
from src.auth.l5_delegation import issue_l5_work_delegation
from src.auth.l5_execution_scope import require_execution_scope
from src.auth.l5_schedule_scope import schedule_product_scope, schedule_worker_id
from src.l5_backend_well_architected import WellArchitectedService, qualify_source_role
from src.l5_service_identity import worker_id
from src.l5_wa_provider_selection import verify_provider_references
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.products.models import ProductProjectBinding
from src.products.source_routes import service_visible_bindings, source_ref_authorized
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


async def current_selection(backend, receipt):
    """No caller-supplied owner, delegation, provider or source can enter here."""
    db, actor = backend.db, backend.actor
    denied = HTTPException(404, detail={"code": "not_found"})
    if (
        actor.account_type != "service"
        or actor.auth_source != "iam"
        or schedule_worker_id(actor) != worker_id("well_architected")
        or actor.scope not in {"internal", "platform"}
        or receipt.service_id != "well_architected"
        or receipt.event_kind != "schedule"
        or receipt.state not in {"pending", "admitted"}
        or receipt.idempotency_key != semantic_key(receipt)
    ):
        raise denied
    configuration = await db.get(Configuration, (receipt.tenant_id, receipt.configuration_id), populate_existing=True)
    version = await db.get(
        ConfigurationVersion, (receipt.tenant_id, receipt.configuration_id, receipt.configuration_revision), populate_existing=True
    )
    if (
        configuration is None
        or version is None
        or (configuration.product_id, configuration.project_id, configuration.service_id, configuration.current_revision, configuration.handoff_state)
        != (receipt.product_id, receipt.project_id, receipt.service_id, receipt.configuration_revision, "linked")
    ):
        raise denied
    settings = version.settings
    triggers = [item for item in settings.get("triggers", []) if item.get("trigger_id") == receipt.trigger_id]
    if (
        not wa_configured(version)
        or settings.get("enabled") is not True
        or len(triggers) != 1
        or triggers[0].get("kind") != "schedule"
        or triggers[0].get("enabled") is not True
        or (receipt.run_input or {}).get("capability") != "architecture_review"
        or (receipt.run_input or {}).get("sources") != settings.get("source_refs")
    ):
        raise HTTPException(409, detail={"code": "configuration_disabled"})
    backend._require_execution_inputs(settings, configuration)
    trigger = triggers[0]
    binding = await db.get(Binding, (receipt.tenant_id, trigger.get("binding_id")), populate_existing=True)
    connection = settings.get("connection_ref") or {}
    scope = view(configuration, version)["scope"]
    credential = await db.get(UserCredential, connection.get("record_id"), populate_existing=True)
    reference = (binding.native_resource_ref or {}) if binding is not None else {}
    if (
        binding is None
        or credential is None
        or binding.configuration_id != configuration.configuration_id
        or (binding.product_id, binding.project_id, binding.service_id) != (receipt.product_id, receipt.project_id, receipt.service_id)
        or binding.state != "ready"
        or binding.gaps
        or binding.scope != scope
        or binding.connection_ref != connection
        or reference.get("tenant_id") != receipt.tenant_id
        or reference.get("project_id") != receipt.project_id
        or reference.get("record_type") != "agent_profile"
        or reference.get("source_connection_id") != credential.id
        or (credential.id, credential.org_id, credential.user_id, credential.service, credential.credential_type, credential.aws_verified_version_id)
        != (connection.get("source_connection_id"), receipt.tenant_id, configuration.owner_id, "aws", "aws_role", connection.get("record_revision"))
    ):
        raise denied
    verified_connection_evidence(credential)
    parents = (
        await db.scalars(
            select(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.tenant_id == receipt.tenant_id,
                CredentialServiceDelegation.product_id == receipt.product_id,
                CredentialServiceDelegation.project_id == receipt.project_id,
                CredentialServiceDelegation.credential_id == credential.id,
                CredentialServiceDelegation.created_by == configuration.owner_id,
                CredentialServiceDelegation.service_id == schedule_worker_id(actor),
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
            .limit(2)
        )
    ).all()
    if len(parents) != 1:
        raise HTTPException(403, detail={"code": "delegation_denied"})
    parent = parents[0]
    # Legacy unscoped grants cannot opt a newly installed periodic path into work.
    if (
        not isinstance(parent.execution_scope, dict)
        or parent.execution_scope.get("configuration_id") != configuration.configuration_id
        or parent.binding_generation != binding.generation
        or "architecture_review" not in parent.capabilities
        or not {"credential:session", "product:read", "source:pin", "source:read"} <= set(parent.operations)
        or any(source not in parent.sources for source in [{**connection, "record_type": "credential"}, *settings["source_refs"]])
        or await schedule_product_scope(db, actor, parent.id, receipt.product_id, receipt.project_id, "product:read") is None
    ):
        raise HTTPException(403, detail={"code": "delegation_denied"})
    await require_execution_scope(db, parent, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation)
    relationship = await db.scalar(
        select(ProductProjectBinding).where(
            ProductProjectBinding.tenant_id == receipt.tenant_id,
            ProductProjectBinding.product_id == receipt.product_id,
            ProductProjectBinding.project_id == receipt.project_id,
            ProductProjectBinding.valid_to.is_(None),
        )
    )
    if relationship is None or str(relationship.relationship_revision) != configuration.relationship_revision:
        raise denied
    visible = await service_visible_bindings(db, parent)
    if any(not any(source_ref_authorized(parent, item, source) for item in visible) for source in settings["source_refs"]):
        raise denied
    verified = {
        "scope": scope,
        "service_id": "well_architected",
        "actor_id": parent.created_by,
        "actor_type": "service",
        "configuration_id": configuration.configuration_id,
        "configuration_revision": version.revision,
        "configuration_hash": version.hash,
        "configuration_enabled": True,
        "source_refs": settings["source_refs"],
        "profile_ref": settings["profile_ref"],
        "connection_ref": connection,
        "binding_connection_ref": binding.connection_ref,
        "binding_id": binding.binding_id,
        "binding_generation": binding.generation,
        "binding_state": binding.state,
        "delegation_id": parent.id,
        "delegation_version": parent.revision,
        "delegation_active": True,
        "trigger": deepcopy(trigger),
        "event_id": receipt.event_id,
        "event_kind": "schedule",
        "event_source_ref": None,
        "adapter_revision": "well_architected-v1",
        "limits": settings["limits"],
        "visibility": settings["visibility"],
        "classification": settings["classification"],
        "causal_origin": "schedule",
        "exclusions": settings.get("exclusions", []),
        "monitoring_policy": effective_monitoring(settings),
        "max_bytes": 6 * 1024 * 1024,
        "max_files": 1000,
    }
    _verify_well_architected_schedule(receipt, verified)
    return configuration, version, binding, credential, parent, verified


async def schedule_authority(backend, receipt):
    configuration, version, binding, credential, parent, verified = await current_selection(backend, receipt)
    frozen = deepcopy(verified)
    frozen_settings = deepcopy(version.settings)
    native_ref, provider_identity = deepcopy(binding.native_resource_ref), deepcopy(binding.provider_identity)
    native = WellArchitectedService(backend)
    await verify_provider_references(backend, receipt.project_id, version.settings, service_grant=parent)
    provider, identity = await native.selected_provider(credential, version.settings, audit_run_id=receipt.idempotency_key, service_grant=parent)
    review = version.settings["well_architected"]
    try:
        qualification = await run_in_threadpool(qualify_source_role, provider, version.settings, identity)
        selection = ProfileSelection(
            receipt.tenant_id,
            receipt.product_id,
            receipt.project_id,
            identity["account_id"],
            identity["region"],
            identity["role_arn"],
            review["execution_role_ref"]["record_id"],
            tuple(review["pillars"]),
            qualification,
        )
        observed = await ProfileBindingProvider(
            provider.client("wellarchitected", region_name=identity["region"]), selection, verified["connection_ref"]
        ).read(native_ref, {**verified, **identity})
    except (BotoCoreError, ClientError, ConnectionError, TimeoutError, AttributeError) as error:
        raise HTTPException(
            503,
            detail={
                "code": "native_readback_unavailable",
                "message": "Verify that the selected account supports the native Profile APIs and that its connection permits readback.",
            },
        ) from error
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(409, detail={"code": "native_unqualified"}) from error
    # ADP ticks request the same source-only architecture generation as manual
    # work. Native scheduled eligibility describes AWS resource recommendations.
    if (
        observed["state"] != "ready"
        or observed["readiness"][0].get("eligible") is not True
        or observed["readiness"][0]["snapshot_hash"] != native_ref.get("record_revision")
        or any(provider_identity.get(key) != value for key, value in identity.items())
        or any(goal not in verified["source_refs"] for goal in review.get("goal_refs", []))
    ):
        raise HTTPException(409, detail={"code": "native_unqualified"})
    configuration, version, binding, credential, parent, current = await current_selection(backend, receipt)
    if (
        current != frozen
        or version.settings != frozen_settings
        or binding.native_resource_ref != native_ref
        or binding.provider_identity != provider_identity
    ):
        raise HTTPException(409, detail={"code": "stale_revision"})
    # This is an authorization subject from the retained grant, never a token or
    # a replacement for the authenticated service actor. The run records a service
    # schedule; actor_id keeps the existing broker's consent-owner attribution.
    subject = SimpleNamespace(org_id=parent.tenant_id, user_id=parent.created_by, team_id="", is_admin=False)
    child = await issue_l5_work_delegation(
        backend.db,
        subject,
        parent,
        credential,
        binding,
        verified["connection_ref"],
        verified["source_refs"],
        "architecture_review",
        verified["limits"]["deadline_seconds"],
        "submit",
        receipt.idempotency_key,
    )
    expiry = child.expires_at.replace(tzinfo=child.expires_at.tzinfo or UTC).isoformat()
    model = botocore.session.get_session().get_service_model("wellarchitected")
    verified.update(
        delegation_id=child.id,
        delegation_version=child.revision,
        native_profile_ref=native_ref,
        profile_hash=native_ref["record_revision"],
        provider_identity=provider_identity,
        native_creator=provider_identity["role_arn"],
        generation_scope={"pillars": review["pillars"]},
        goal_refs=review.get("goal_refs", []),
        generation_context={},
        architecture_coverage={},
        generation_model_hash=sha256(json.dumps(model._service_description, sort_keys=True).encode()).hexdigest(),
        source_retention={"required_until": expiry, "policy_expires_at": expiry},
    )
    version.state, version.gaps = "ready", []
    await backend.db.flush()
    return verified, GatewayAdmissionSourceClient(
        backend.db, backend.actor, child.id, verified, "submit", receipt.idempotency_key, service_id="well_architected"
    )
