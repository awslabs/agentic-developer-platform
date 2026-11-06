"""Freeze and admit Transform occurrences under explicit owner consent as a service."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import HTTPException
from l5_services.adapters.transform.baseline import pinned_test_policy
from l5_services.api.configuration import view
from l5_services.configuration_readiness import effective_monitoring
from l5_services.events.schedules import schedule_window_reason
from l5_services.events.sources import _verify_transform_schedule
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.run_input import validate_run_input
from l5_services.transform_contract import TransformSettings
from sqlalchemy import JSON, or_, select, update

from src.auth.aws_connection_authority import verified_connection_evidence
from src.auth.l5_delegation import issue_l5_work_delegation
from src.auth.l5_execution_scope import require_execution_scope
from src.auth.l5_schedule_scope import schedule_product_scope, schedule_worker_id
from src.auth.l5_transform_branch_scope import OCCURRENCE, consent_for_trigger, current_occurrence
from src.internal.l5_transform_policy import run_policy
from src.l5_backend_transform import read_qualification
from src.l5_service_identity import worker_id
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.products.l5_branch_sources import branch_binding_identity, current_branch_binding, resolve_branch
from src.products.models import ProductProjectBinding
from src.products.source_routes import service_visible_bindings, source_ref_authorized
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


async def current_selection(backend, receipt, *, allow_unresolved=False):
    db, actor = backend.db, backend.actor
    denied = HTTPException(404, detail={"code": "not_found"})
    if (
        schedule_worker_id(actor) != worker_id("transform")
        or receipt.service_id != "transform"
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
        != (receipt.product_id, receipt.project_id, "transform", receipt.configuration_revision, "linked")
    ):
        raise denied
    settings = version.settings
    triggers = [item for item in settings.get("triggers", []) if item.get("trigger_id") == receipt.trigger_id]
    if (
        version.state != "ready"
        or settings.get("enabled") is not True
        or len(triggers) != 1
        or triggers[0].get("kind") != "schedule"
        or triggers[0].get("enabled") is not True
        or triggers[0].get("capability") not in settings.get("capabilities", [])
    ):
        raise HTTPException(409, detail={"code": "configuration_disabled"})
    trigger = triggers[0]
    branch = trigger.get("source_policy") == "branch"
    unresolved = branch and receipt.run_input is None and allow_unresolved
    transform = TransformSettings.model_validate(settings["transform"]).model_dump(exclude_none=True)
    connection = transform["connection_ref"]
    scope = view(configuration, version)["scope"]
    binding = await db.get(Binding, (receipt.tenant_id, trigger.get("binding_id")), populate_existing=True)
    credential = await db.get(UserCredential, connection.get("record_id"), populate_existing=True)
    sources = trigger.get("source_refs") if unresolved else (receipt.run_input or {}).get("sources")
    if (
        binding is None
        or credential is None
        or binding.configuration_id != configuration.configuration_id
        or (binding.product_id, binding.project_id, binding.service_id) != (receipt.product_id, receipt.project_id, "transform")
        or binding.state != "ready"
        or binding.gaps
        or binding.scope != scope
        or binding.connection_ref != connection
        or not isinstance(sources, list)
        or len(sources) != 1
        or (not branch and sources != trigger.get("source_refs"))
        or any(source not in settings.get("source_refs", []) for source in trigger.get("source_refs", []))
        or (credential.id, credential.org_id, credential.user_id, credential.service, credential.credential_type, credential.aws_verified_version_id)
        != (connection.get("source_connection_id"), receipt.tenant_id, configuration.owner_id, "aws", "aws_role", connection.get("record_revision"))
    ):
        raise denied
    verified_connection_evidence(credential)
    identity = binding.provider_identity
    if (
        not isinstance(identity, dict)
        or identity.get("region") != transform["region"]
        or any((credential.scopes or {}).get(key) != identity.get(key) for key in ("account_id", "role_arn"))
    ):
        raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
    parents = (
        await db.scalars(
            select(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.tenant_id == receipt.tenant_id,
                CredentialServiceDelegation.product_id == receipt.product_id,
                CredentialServiceDelegation.project_id == receipt.project_id,
                CredentialServiceDelegation.credential_id == credential.id,
                CredentialServiceDelegation.created_by == configuration.owner_id,
                CredentialServiceDelegation.service_id == worker_id("transform"),
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(parents) != 1:
        raise HTTPException(403, detail={"code": "delegation_denied"})
    parent = parents[0]
    if (
        not isinstance(parent.execution_scope, dict)
        or parent.execution_scope.get("configuration_id") != configuration.configuration_id
        or parent.binding_generation != binding.generation
        or trigger["capability"] not in parent.capabilities
        or not {"credential:session", "product:read", "source:pin", "source:read"} <= set(parent.operations)
        or any(source not in parent.sources for source in [{**connection, "record_type": "credential"}, *trigger["source_refs"]])
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
    if branch:
        if not consent_for_trigger(parent.execution_scope, trigger):
            raise HTTPException(
                403,
                detail={
                    "code": "branch_schedule_consent_required",
                    "message": "Review delegation-scope and authorize this configuration's exact repository, branch and scheduled goal.",
                },
            )
        source_binding, _ = await current_branch_binding(db, parent, trigger)
        if not unresolved:
            proof = (receipt.target or {}).get(OCCURRENCE, {})
            subject = SimpleNamespace(
                **{
                    field: getattr(parent, field)
                    for field in ("tenant_id", "product_id", "project_id", "service_id", "binding_generation", "capabilities")
                },
                parent_delegation_id=parent.id,
                execution_scope={**parent.execution_scope, OCCURRENCE: proof},
                sources=[{**connection, "record_type": "credential"}, *sources],
            )
            if (
                not await current_occurrence(db, subject)
                or proof.get("source_binding_id") != source_binding.id
                or proof.get("source_binding_generation") != source_binding.generation
            ):
                raise HTTPException(403, detail={"code": "branch_occurrence_invalid"})
    else:
        visible = await service_visible_bindings(db, parent)
        if any(not any(source_ref_authorized(parent, item, source) for item in visible) for source in sources):
            raise denied
    verified = {
        "scope": scope,
        "service_id": "transform",
        "actor_id": parent.created_by,
        "actor_type": "service",
        "configuration_id": configuration.configuration_id,
        "configuration_revision": version.revision,
        "configuration_hash": version.hash,
        "configuration_enabled": True,
        "binding_id": binding.binding_id,
        "binding_generation": binding.generation,
        "binding_state": binding.state,
        "connection_ref": connection,
        "binding_connection_ref": binding.connection_ref,
        "delegation_id": parent.id,
        "delegation_version": parent.revision,
        "delegation_active": True,
        "trigger": deepcopy(trigger),
        "source_refs": sources,
        "capabilities": settings["capabilities"],
        "transform_settings": transform,
        "qualified_executor_ref": transform["executor_binding_ref"],
        "provider_identity": deepcopy(identity),
        "profile_ref": settings["profile_ref"],
        "adapter_revision": "transform-v1",
        "event_id": receipt.event_id,
        "event_kind": "schedule",
        "event_source_ref": None,
        "causal_origin": "schedule",
        "limits": settings["limits"],
        "visibility": settings["visibility"],
        "classification": settings["classification"],
        "exclusions": settings.get("exclusions", []),
        "monitoring_policy": effective_monitoring(settings),
        "max_bytes": 6 * 1024 * 1024,
        "max_files": 1000,
    }
    if unresolved:
        # Validate the persisted target before any provider read. This temporary
        # shape is never retained or submitted and grants no source authority.
        check = SimpleNamespace(
            **{field: getattr(receipt, field) for field in ("event_id", "event_kind", "configuration_revision")},
            target={**(receipt.target or {}), "pinned_source": sources[0]},
            run_input={"sources": sources, "transform_input": {"goal": trigger["goal"]}},
        )
        _verify_transform_schedule(check, verified)
    else:
        _verify_transform_schedule(receipt, verified)
    return configuration, version, binding, credential, parent, verified


async def freeze_branch_occurrence(backend, receipt):
    """Conditionally retain the first resolved commit before admission can fail."""
    if (receipt.target or {}).get("source_policy") != "branch" or receipt.run_input is not None:
        return
    db = backend.db
    _, _, _, _, parent, verified = await current_selection(backend, receipt, allow_unresolved=True)
    trigger = verified["trigger"]
    tick = datetime.fromisoformat(receipt.target["scheduled_at"].replace("Z", "+00:00"))
    now = datetime.now(UTC)
    reason = "schedule_not_due" if now < tick else schedule_window_reason(trigger, tick, now)
    if reason:
        raise HTTPException(409, detail={"code": reason})
    source_binding, mapping = await current_branch_binding(db, parent, trigger)
    selected_identity = branch_binding_identity(source_binding, mapping)
    pinned = await resolve_branch(source_binding, mapping, trigger["branch_selector"])
    # Current authority is checked again after the external read, before the
    # first commit is persisted. A concurrent committed freeze always wins.
    await db.refresh(receipt)
    if receipt.run_input is not None:
        return
    _, _, _, _, parent, current = await current_selection(backend, receipt, allow_unresolved=True)
    source_binding, mapping = await current_branch_binding(db, parent, trigger)
    if current != verified or branch_binding_identity(source_binding, mapping) != selected_identity:
        raise HTTPException(409, detail={"code": "stale_revision"})
    run_input = {
        "service_id": "transform",
        "configuration_revision": receipt.configuration_revision,
        "binding_id": verified["binding_id"],
        "capability": trigger["capability"],
        "sources": [pinned],
        "transform_input": {"goal": deepcopy(trigger["goal"])},
    }
    validate_run_input(run_input)
    proof = {
        "event_id": receipt.event_id,
        "trigger_id": receipt.trigger_id,
        "idempotency_key": receipt.idempotency_key,
        "parent_delegation_id": parent.id,
        "source_ref": pinned,
        "source_binding_id": source_binding.id,
        "source_binding_generation": source_binding.generation,
    }
    await retain_first_branch_source(db, receipt, run_input, proof)


async def retain_first_branch_source(db, receipt, run_input, proof):
    """Only the first authorized freeze can transition an unresolved receipt."""
    key = {field: getattr(receipt, field) for field in ("tenant_id", "product_id", "project_id", "service_id", "trigger_id", "event_id")}
    written = await db.execute(
        update(EventReceipt)
        .where(
            *(getattr(EventReceipt, field) == value for field, value in key.items()),
            EventReceipt.state == "pending",
            or_(EventReceipt.run_input.is_(None), EventReceipt.run_input == JSON.NULL),
        )
        .values(run_input=run_input, target={**receipt.target, "pinned_source": run_input["sources"][0], OCCURRENCE: proof})
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    await db.refresh(receipt)
    return written.rowcount == 1


async def schedule_authority(backend, receipt):
    configuration, version, binding, credential, parent, verified = await current_selection(backend, receipt)
    frozen, frozen_settings = deepcopy(verified), deepcopy(version.settings)
    native_ref = deepcopy(binding.native_resource_ref)
    record = await read_qualification(receipt.tenant_id, receipt.product_id, receipt.project_id, verified["qualified_executor_ref"])
    transform = verified["transform_settings"]
    if record.get("provider_identity") != verified["provider_identity"]:
        raise HTTPException(409, detail={"code": "native_unqualified"})
    policy = record.get("transform_test_policy")
    try:
        pinned_test_policy({"transform_settings": transform, "transform_test_policy": policy})
    except (TypeError, ValueError):
        raise HTTPException(409, detail={"code": "transform_test_policy_unavailable"}) from None
    configuration, version, binding, credential, parent, current = await current_selection(backend, receipt)
    if current != frozen or version.settings != frozen_settings or binding.native_resource_ref != native_ref:
        raise HTTPException(409, detail={"code": "stale_revision"})
    verified.update(transform_qualification=record["transform_qualification"], transform_test_policy=policy)
    # Validate the exact definition/provider against the existing broker policy;
    # this only builds a policy and cannot acquire credentials or start work.
    run_policy(version.settings, binding, SimpleNamespace(service_id="transform", input=receipt.run_input, admission=verified), parent.expires_at)
    # A consent subject, not a login token. Authenticated actor and run attribution
    # remain a service, with the retained consent owner recorded separately.
    subject = SimpleNamespace(org_id=parent.tenant_id, user_id=parent.created_by, team_id="", is_admin=False)
    child = await issue_l5_work_delegation(
        backend.db,
        subject,
        parent,
        credential,
        binding,
        verified["connection_ref"],
        verified["source_refs"],
        receipt.run_input["capability"],
        verified["limits"]["deadline_seconds"],
        "submit",
        receipt.idempotency_key,
        source_occurrence=(receipt.target or {}).get(OCCURRENCE),
    )
    verified.update(delegation_id=child.id, delegation_version=child.revision)
    capture_verified = deepcopy(verified)
    capture_verified["trigger"]["source_refs"] = deepcopy(receipt.run_input["sources"])
    return verified, GatewayAdmissionSourceClient(
        backend.db, backend.actor, child.id, capture_verified, "submit", receipt.idempotency_key, service_id="transform"
    )
