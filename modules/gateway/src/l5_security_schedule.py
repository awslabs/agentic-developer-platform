"""Resolve scheduled Security work from current owner consent; the caller stays a service.

Structural receipt creation and current authorization are deliberately separate.
A normal Security save persists ``needs_input``, so nothing here depends on a
stored ready state: configuration, binding, native registration, selected threat
model, source rights and an explicit active owner consent are all re-resolved at
the scheduled UTC instant. Only a scoped child grant is issued; the installed
worker remains a service actor and never becomes the owner.
"""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import boto3
from fastapi import HTTPException
from l5_services.adapters.security.binding import SecurityBindingAdapter
from l5_services.api.bindings import view_binding
from l5_services.api.configuration import view
from l5_services.configuration_readiness import assess_readiness, effective_monitoring
from l5_services.events.schedules import SCHEDULE_CAPABILITIES, security_configured
from l5_services.events.sources import _verify_security_schedule
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import semantic_key
from l5_services.security_registration import current_model, current_registration
from sqlalchemy import select

from src.auth.aws_connection_authority import connection_material, require_assumed_identity, verified_connection_evidence
from src.auth.l5_delegation import issue_l5_work_delegation
from src.auth.l5_execution_scope import require_execution_scope
from src.auth.l5_schedule_scope import schedule_product_scope, schedule_worker_id
from src.internal.sts_assume_service import assume_role
from src.l5_service_identity import worker_id
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.products.models import ProductProjectBinding
from src.products.source_routes import service_visible_bindings, source_ref_authorized
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper

SECURITY_REGION = "us-east-1"


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _eligible_consent(parent, configuration, binding, capability, connection, sources):
    """Retained owner consent whose exact scope covers this tick's selection.

    An owner legitimately keeps a separate consent per scheduled binding, so a
    sibling binding's grant must neither authorize nor block this tick. This is
    selection only; the authorization checks below still run on the chosen
    grant, including ``require_execution_scope``.
    """
    scope = parent.execution_scope if isinstance(parent.execution_scope, dict) else None
    return bool(
        scope
        and scope.get("configuration_id") == configuration.configuration_id
        and scope.get("binding_id") in {None, binding.binding_id}
        and parent.binding_generation == binding.generation
        and capability in (parent.capabilities or [])
        and {"credential:session", "product:read", "source:pin"} <= set(parent.operations or [])
        and not any(source not in (parent.sources or []) for source in [{**connection, "record_type": "credential"}, *sources])
    )


def _bound_model(binding):
    """The threat model already selected by the owner for this binding."""
    for item in binding.readiness or []:
        if item.get("kind") == "threat_model" and isinstance(item.get("native_model_ref"), dict):
            return item["native_model_ref"]
    return None


async def current_selection(backend, receipt):
    """No caller-supplied owner, delegation, provider or source can enter here."""
    db, actor = backend.db, backend.actor
    denied = HTTPException(404, detail={"code": "not_found"})
    if (
        actor.account_type != "service"
        or actor.auth_source != "iam"
        or schedule_worker_id(actor) != worker_id("security")
        or actor.scope not in {"internal", "platform"}
        or receipt.service_id != "security"
        or receipt.event_kind != "schedule"
        or receipt.state not in {"pending", "admitted"}
        or receipt.idempotency_key != semantic_key(receipt)
        or not isinstance(receipt.run_input, dict)
    ):
        raise denied
    capability = receipt.run_input.get("capability")
    sources = receipt.run_input.get("sources")
    if capability not in SCHEDULE_CAPABILITIES["security"] or not isinstance(sources, list) or not sources:
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
        not security_configured(version)
        or settings.get("enabled") is not True
        or len(triggers) != 1
        or triggers[0].get("kind") != "schedule"
        or triggers[0].get("enabled") is not True
        or triggers[0].get("capability") != capability
        or triggers[0].get("source_refs") != sources
        or triggers[0].get("binding_id") != receipt.run_input.get("binding_id")
    ):
        raise HTTPException(409, detail={"code": "configuration_disabled"})
    trigger = triggers[0]
    binding = await db.get(Binding, (receipt.tenant_id, trigger.get("binding_id")), populate_existing=True)
    connection = settings.get("connection_ref") or {}
    scope = view(configuration, version)["scope"]
    credential = await db.get(UserCredential, connection.get("record_id"), populate_existing=True)
    native = (binding.native_resource_ref or {}) if binding is not None else {}
    identity = (binding.provider_identity or {}) if binding is not None else {}
    if (
        binding is None
        or credential is None
        or binding.configuration_id != configuration.configuration_id
        or (binding.product_id, binding.project_id, binding.service_id) != (receipt.product_id, receipt.project_id, receipt.service_id)
        or binding.state != "ready"
        or binding.gaps
        or binding.scope != scope
        or binding.connection_ref != connection
        or native.get("tenant_id") != receipt.tenant_id
        or native.get("project_id") != receipt.project_id
        or native.get("record_type") != "security_space"
        or not native.get("record_id")
        or native.get("source_connection_id") != credential.id
        or identity.get("region") != SECURITY_REGION
        or identity.get("account_id") != (credential.scopes or {}).get("account_id")
        or identity.get("role_arn") != (credential.scopes or {}).get("role_arn")
        # Native admission stays with the configuration owner's own connection.
        or (credential.id, credential.org_id, credential.user_id, credential.service, credential.credential_type, credential.aws_verified_version_id)
        != (connection.get("source_connection_id"), receipt.tenant_id, configuration.owner_id, "aws", "aws_role", connection.get("record_revision"))
    ):
        raise denied
    verified_connection_evidence(credential)
    # One configuration legitimately holds a separate ready binding per
    # capability, so saved readiness is assessed against every binding scoped
    # to it. The selected binding and its generation stay pinned above and in
    # the frozen target, so a sibling binding cannot widen this tick.
    scoped_bindings = (
        await db.scalars(
            select(Binding).where(
                Binding.tenant_id == receipt.tenant_id,
                Binding.configuration_id == configuration.configuration_id,
                Binding.product_id == receipt.product_id,
                Binding.project_id == receipt.project_id,
                Binding.service_id == receipt.service_id,
            )
        )
    ).all()
    if not any(item.binding_id == binding.binding_id for item in scoped_bindings):
        raise denied
    try:
        readiness, gaps, _ = assess_readiness(
            settings,
            configuration.configuration_id,
            [view_binding(item) for item in scoped_bindings],
            delegation_active=True,
        )
    except ValueError:
        raise HTTPException(409, detail={"code": "configuration_disabled"}) from None
    if readiness != "ready" or gaps:
        raise HTTPException(409, detail={"code": "configuration_disabled"})
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
            .order_by(CredentialServiceDelegation.id)
        )
    ).all()
    # Retained, unrevoked and unexpired owner grants are narrowed to this tick's
    # exact scope before any ambiguity is judged, so a valid consent for another
    # scheduled binding cannot mask this binding's consent.
    eligible = [item for item in parents if _eligible_consent(item, configuration, binding, capability, connection, sources)]
    if len(eligible) > 1:
        # Two indistinguishable exact-scope grants are refused, never guessed.
        raise HTTPException(403, detail={"code": "delegation_denied"})
    if not eligible and len(parents) != 1:
        # Expired or revoked consent is actionable: nightly operation requires a
        # legitimate renewal by the owner and is never silently extended here.
        raise HTTPException(403, detail={"code": "schedule_consent_required"})
    # A single retained grant still reaches the checks below, so a real scope
    # mismatch surfaces as a delegation denial instead of a consent prompt.
    parent = eligible[0] if eligible else parents[0]
    if (
        not isinstance(parent.execution_scope, dict)
        or parent.execution_scope.get("configuration_id") != configuration.configuration_id
        or parent.binding_generation != binding.generation
        or capability not in (parent.capabilities or [])
        or not {"credential:session", "product:read", "source:pin"} <= set(parent.operations or [])
        or any(source not in parent.sources for source in [{**connection, "record_type": "credential"}, *sources])
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
    if any(not any(source_ref_authorized(parent, item, source) for item in visible) for source in sources):
        raise denied
    verified = {
        "scope": scope,
        "service_id": "security",
        "actor_id": parent.created_by,
        "actor_type": "service",
        "configuration_id": configuration.configuration_id,
        "configuration_revision": version.revision,
        "configuration_hash": version.hash,
        "configuration_enabled": True,
        "profile_ref": settings["profile_ref"],
        "source_refs": deepcopy(sources),
        "connection_ref": connection,
        "binding_connection_ref": binding.connection_ref,
        "binding_id": binding.binding_id,
        "binding_generation": binding.generation,
        "binding_state": binding.state,
        "delegation_id": parent.id,
        "delegation_version": parent.revision,
        "delegation_active": True,
        "consent_expires_at": _aware(parent.expires_at).isoformat(),
        "trigger": deepcopy(trigger),
        "event_id": receipt.event_id,
        "event_kind": "schedule",
        "event_source_ref": None,
        "adapter_revision": "security-v1",
        "limits": settings["limits"],
        "visibility": settings["visibility"],
        "classification": settings["classification"],
        "causal_origin": "schedule",
        "exclusions": settings.get("exclusions", []),
        "monitoring_policy": effective_monitoring(settings),
        "max_bytes": 1024 * 1024 * 1024 if receipt.run_input["capability"] == "code_review" else 6 * 1024 * 1024,
        "max_files": 100000 if receipt.run_input["capability"] == "code_review" else 1000,
    }
    return configuration, version, binding, credential, parent, verified


async def _provider_session(backend, credential, identity, parent, audit_id):
    """Short-lived read session from the retained consent; no runner credentials."""
    helper = SecretsManagerHelper()
    try:
        async with asyncio.timeout(30):
            version_id = credential.aws_verified_version_id
            if await asyncio.to_thread(helper.current_version_id, credential.secret_arn) != version_id:
                raise ValueError("connection changed")
            payload, served = await asyncio.to_thread(helper.get_secret_at_version, credential.secret_arn, version_id)
            if served != version_id:
                raise ValueError("connection changed")
            role, external_id, account = connection_material(json.loads(payload), credential.scopes or {}, credential)
            if (role, account) != (identity["role_arn"], identity["account_id"]):
                raise ValueError("connection changed")
            assumed = await asyncio.to_thread(
                assume_role,
                role_arn=role,
                external_id=external_id,
                session_duration_seconds=900,
                default_region=identity["region"],
                aws_region=identity["region"],
                user_id=parent.created_by,
                agent_id="l5-security-schedule",
                task_id=audit_id,
                label=credential.label,
            )
            require_assumed_identity(assumed, role)
            if await asyncio.to_thread(helper.current_version_id, credential.secret_arn) != version_id:
                raise ValueError("connection changed")
            return boto3.Session(
                aws_access_key_id=assumed.access_key_id,
                aws_secret_access_key=assumed.secret_access_key,
                aws_session_token=assumed.session_token,
                region_name=identity["region"],
            )
    except Exception:
        raise HTTPException(
            503,
            detail={
                "code": "security_provider_identity_unavailable",
                "message": "Verify the selected Product AWS connection and owner consent; no assessment was submitted.",
            },
        ) from None


async def _refresh_native_rights(backend, binding, credential, parent, verified, capability, audit_id):
    """Observe the selected space now; a stale stored observation is never reused."""
    identity = {**(binding.provider_identity or {}), "verified_at": datetime.now(UTC).isoformat()}
    reference = deepcopy(binding.native_resource_ref)
    selected_model = _bound_model(binding)
    if capability == "threat_model" and selected_model is None:
        raise HTTPException(409, detail={"code": "security_model_selection_required"})
    session = await _provider_session(backend, credential, identity, parent, audit_id)
    adapter = SecurityBindingAdapter(session.client("securityagent", region_name=identity["region"]), identity)
    # A code-scan tick refreshes the observed space identity and source
    # registration only. Passing no binding keeps the adapter away from every
    # threat-model call, so admission can never create a provider threat model
    # as a side effect; the owner's stored selection is retained untouched
    # below. A threat-model tick passes the selection, which the adapter must
    # match against an existing model instead of creating one.
    scoped = {"binding": binding, "selected_model": selected_model} if capability == "threat_model" else {}
    try:
        observed = await adapter.read(reference, {**identity, "connection_ref": verified["connection_ref"]}, **scoped)
    except (ConnectionError, TimeoutError) as error:
        raise HTTPException(
            503,
            detail={
                "code": "security_native_refresh_unavailable",
                "message": "The selected Security Agent space could not be read; the scheduled assessment was not submitted.",
            },
        ) from error
    except (PermissionError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(409, detail={"code": "native_unqualified"}) from error
    if (
        observed.get("state") != "ready"
        or observed.get("gaps")
        or observed.get("native_resource_ref") != reference
        or any((observed.get("provider_identity") or {}).get(field) != identity[field] for field in ("account_id", "role_arn", "region"))
    ):
        raise HTTPException(409, detail={"code": "native_unqualified"})
    # The code-scan refresh observes no threat model, so the owner's selected
    # model entry is retained rather than dropped from the stored readiness.
    retained = (
        []
        if capability == "threat_model"
        else [
            item
            for item in binding.readiness or []
            if isinstance(item, dict) and item.get("kind") in {"threat_model", "threat_model_outcome_unknown", "threat_model_selection_required"}
        ][:1]
    )
    binding.state, binding.gaps = observed["state"], []
    binding.readiness = [*deepcopy(observed["readiness"]), *deepcopy(retained)]
    await backend.db.flush()
    registration = current_registration(binding)
    model = current_model(binding) if capability == "threat_model" else None
    if registration is None or (capability == "threat_model" and model is None):
        raise HTTPException(409, detail={"code": "native_unqualified"})
    if capability == "threat_model" and model != selected_model:
        # The owner's selected persistent threat model is never swapped silently.
        raise HTTPException(409, detail={"code": "security_model_selection_changed"})
    return registration, model


async def schedule_authority(backend, receipt):
    configuration, version, binding, credential, parent, verified = await current_selection(backend, receipt)
    frozen = deepcopy(verified)
    frozen_settings = deepcopy(version.settings)
    native_ref, provider_identity = deepcopy(binding.native_resource_ref), deepcopy(binding.provider_identity)
    capability = receipt.run_input["capability"]
    registration, model = await _refresh_native_rights(backend, binding, credential, parent, verified, capability, receipt.idempotency_key)
    configuration, version, binding, credential, parent, current = await current_selection(backend, receipt)
    if (
        current != frozen
        or version.settings != frozen_settings
        or binding.native_resource_ref != native_ref
        or binding.provider_identity != provider_identity
    ):
        raise HTTPException(409, detail={"code": "stale_revision"})
    # An authorization subject from the retained grant, never a token and never a
    # replacement for the authenticated service actor. actor_id keeps the existing
    # consent-owner attribution while the run records a service schedule.
    subject = SimpleNamespace(org_id=parent.tenant_id, user_id=parent.created_by, team_id="", is_admin=False)
    child = await issue_l5_work_delegation(
        backend.db,
        subject,
        parent,
        credential,
        binding,
        verified["connection_ref"],
        verified["source_refs"],
        capability,
        verified["limits"]["deadline_seconds"],
        "submit",
        receipt.idempotency_key,
    )
    verified.update(
        delegation_id=child.id,
        delegation_version=child.revision,
        native_space_ref=deepcopy(native_ref),
        provider_identity=deepcopy(provider_identity),
        source_registration=deepcopy(registration),
        **({"native_model_ref": deepcopy(model)} if model else {}),
    )
    _verify_security_schedule(receipt, verified)
    await backend.db.flush()
    return verified, GatewayAdmissionSourceClient(
        backend.db, backend.actor, child.id, verified, "submit", receipt.idempotency_key, service_id="security", run_input=receipt.run_input
    )
