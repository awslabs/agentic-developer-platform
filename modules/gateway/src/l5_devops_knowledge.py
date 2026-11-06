"""Admit selected Product knowledge without dispatching an unqualified native worker."""

from copy import deepcopy
from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.api.bindings import BindingStore
from l5_services.api.configuration import view
from l5_services.devops.knowledge_admission import MAX_SOURCE_BYTES, admitted_selection, digest, publication_selection
from l5_services.models.configuration import Configuration
from l5_services.models.preparation import AdmissionPreparation
from l5_services.models.setup import SetupIntent
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.auth.l5_devops_knowledge_scope import PURPOSE, current_knowledge_scope, require_knowledge_scope
from src.auth.l5_owner_delegation import _owner
from src.l5_service_identity import worker_id
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.shared.models.vault import CredentialServiceDelegation


async def admit(backend, product_id, project_id, request, key, etag):
    if (
        set(request) != {"action", "configuration_revision", "binding_id", "binding_revision", "binding_generation"}
        or request.get("action") != "sync_knowledge"
        or not isinstance(request.get("binding_id"), str)
        or not request["binding_id"]
        or any(
            type(request.get(field)) is not int or request[field] < 1
            for field in ("configuration_revision", "binding_revision", "binding_generation")
        )
    ):
        raise HTTPException(422, detail={"code": "knowledge_selection_invalid"})
    configuration, version = await _owner(backend, product_id, project_id, "devops")
    if etag is None:
        raise HTTPException(428, detail={"code": "precondition_required"})
    previous = await backend.db.scalar(
        select(SetupIntent)
        .where(
            SetupIntent.tenant_id == backend.actor.org_id,
            SetupIntent.product_id == product_id,
            SetupIntent.project_id == project_id,
            SetupIntent.service_id == "devops",
            SetupIntent.actor_id == backend.actor.user_id,
            SetupIntent.idempotency_key == key,
        )
        .execution_options(populate_existing=True)
    )
    store = BindingStore(backend.db, backend.actor.org_id, product_id, project_id, "devops")

    async def replay(previous):
        if previous.request != request:
            raise HTTPException(409, detail={"code": "idempotency_conflict", "operation_id": previous.operation_id})
        await store.operation(previous.operation_id)
        # Replay revalidates only the originally retained grant. Another current
        # grant cannot replace expired/revoked consent or change source selection.
        grant = await backend.db.get(CredentialServiceDelegation, previous.authorization.get("delegation_id"), populate_existing=True)
        if (
            grant is None
            or previous.authorization.get("delegation_revision") != grant.revision
            or (grant.tenant_id, grant.product_id, grant.project_id, grant.created_by)
            != (backend.actor.org_id, product_id, project_id, backend.actor.user_id)
        ):
            raise HTTPException(403, detail={"code": "knowledge_publication_consent_required"})
        await require_knowledge_scope(backend.db, grant, admitted=True)
        verified = previous.authorization.get("selection") or {}
        return await store.admit_knowledge(request, key, etag, verified, previous.authorization["knowledge_admission"])

    if previous is not None:
        return await replay(previous)
    if request["configuration_revision"] != version.revision or etag != f'"{request["binding_revision"]}"':
        raise HTTPException(412, detail={"code": "knowledge_selection_changed"})
    try:
        selection = publication_selection(version.settings["devops"]["knowledge_policy"], backend.actor.org_id, project_id)
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(409, detail={"code": str(error)}) from None
    candidates = (
        await backend.db.scalars(
            select(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.tenant_id == backend.actor.org_id,
                CredentialServiceDelegation.product_id == product_id,
                CredentialServiceDelegation.project_id == project_id,
                CredentialServiceDelegation.service_id == worker_id("devops"),
                CredentialServiceDelegation.created_by == backend.actor.user_id,
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
                CredentialServiceDelegation.execution_scope["purpose"].as_string() == PURPOSE,
                CredentialServiceDelegation.execution_scope["configuration_id"].as_string() == configuration.configuration_id,
                CredentialServiceDelegation.execution_scope["configuration_revision"].as_integer() == version.revision,
                CredentialServiceDelegation.execution_scope["configuration_hash"].as_string() == version.hash,
                CredentialServiceDelegation.execution_scope["binding_id"].as_string() == request["binding_id"],
                CredentialServiceDelegation.binding_generation == request["binding_generation"] + 1,
                CredentialServiceDelegation.execution_scope["knowledge_selection"]["binding_revision"].as_integer() == request["binding_revision"],
                CredentialServiceDelegation.execution_scope["knowledge_selection"]["policy_hash"].as_string() == selection["policy_hash"],
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    eligible = [candidate for candidate in candidates if await current_knowledge_scope(backend.db, candidate, admitted=False)]
    if not eligible:
        raise HTTPException(403, detail={"code": "knowledge_publication_consent_required"})
    if len(eligible) != 1:
        raise HTTPException(
            409,
            detail={
                "code": "knowledge_publication_consent_ambiguous",
                "message": "Resolve the duplicate current grants for this exact knowledge selection before submitting.",
            },
        )
    grant = eligible[0]
    original_selection = grant.execution_scope["knowledge_selection"]
    await backend.authorize_settings(version.settings, project_id, "devops")
    expected = request
    sources = [item["source_ref"] for item in selection["publications"]]
    deadline = grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC)
    verified = {
        "scope": view(configuration, version)["scope"],
        "service_id": "devops",
        "actor_id": backend.actor.user_id,
        "configuration_id": configuration.configuration_id,
        "configuration_revision": version.revision,
        "configuration_hash": version.hash,
        "binding_id": expected["binding_id"],
        "binding_generation": expected["binding_generation"],
        "binding_revision": expected["binding_revision"],
        "connection_ref": original_selection["connection_ref"],
        "native_space_ref": original_selection["native_space_ref"],
        "provider_identity": original_selection["provider_identity"],
        "policy_hash": selection["policy_hash"],
        "trigger": {"source_refs": sources},
        "max_bytes": MAX_SOURCE_BYTES,
        "max_files": 1,
        "exclusions": [],
        "authorization": {
            "actor_id": backend.actor.user_id,
            "delegation_id": grant.id,
            "delegation_revision": grant.revision,
            "purpose": PURPOSE,
            "control_action": PURPOSE,
            "expires_at": deadline.isoformat(),
        },
    }
    prep_key = (backend.actor.org_id, product_id, project_id, "devops", backend.actor.user_id, "sync_knowledge", key)
    preparation = await backend.db.get(AdmissionPreparation, prep_key, populate_existing=True)
    verified["authorization"]["authorized_at"] = (
        preparation.verified["authorization"]["authorized_at"] if preparation is not None else datetime.now(UTC).isoformat()
    )
    if preparation is None:
        preparation = AdmissionPreparation(
            tenant_id=backend.actor.org_id,
            product_id=product_id,
            project_id=project_id,
            service_id="devops",
            actor_id=backend.actor.user_id,
            operation="sync_knowledge",
            idempotency_key=key,
            request_hash=digest(request),
            verified=deepcopy(verified),
            deadline_at=deadline,
        )
        backend.db.add(preparation)
        try:
            await backend.db.commit()
        except IntegrityError:
            await backend.db.rollback()
            preparation = await backend.db.get(AdmissionPreparation, prep_key, populate_existing=True)
            if preparation is None:
                raise
    # Capture returns use nested savepoints/flush only. Keep this row lock until
    # final intent/work commit so same-key requests cannot recapture concurrently.
    preparation = await backend.db.scalar(
        select(AdmissionPreparation)
        .where(
            *(
                getattr(AdmissionPreparation, field) == value
                for field, value in zip(("tenant_id", "product_id", "project_id", "service_id", "actor_id", "operation", "idempotency_key"), prep_key)
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    verified["authorization"]["authorized_at"] = preparation.verified["authorization"]["authorized_at"]
    retained = await backend.db.scalar(
        select(SetupIntent)
        .where(
            SetupIntent.tenant_id == backend.actor.org_id,
            SetupIntent.product_id == product_id,
            SetupIntent.project_id == project_id,
            SetupIntent.service_id == "devops",
            SetupIntent.actor_id == backend.actor.user_id,
            SetupIntent.idempotency_key == key,
        )
        .execution_options(populate_existing=True)
    )
    if retained is not None:
        return await replay(retained)
    await require_knowledge_scope(backend.db, grant, admitted=False)
    if preparation.request_hash != digest(request):
        raise HTTPException(409, detail={"code": "idempotency_conflict"})
    if preparation.verified != verified:
        raise HTTPException(409, detail={"code": "knowledge_selection_changed"})
    source = GatewayAdmissionSourceClient(backend.db, backend.actor, grant.id, verified, "sync_knowledge", key, service_id="devops")
    snapshots = []
    for index, reference in enumerate(sources):
        snapshots.append(
            await source.pin_source(
                product_id,
                project_id,
                {
                    "delegation_id": grant.id,
                    "delegation_revision": grant.revision,
                    "source_ref": reference,
                    "exclusions": [],
                    "max_bytes": MAX_SOURCE_BYTES,
                    "max_files": 1,
                    "retain_until": deadline.isoformat(),
                },
                idempotency_key=digest([list(prep_key), index]),
            )
        )
    # Each capture checks source rights and consent before and after connector I/O.
    # Recheck the complete selection once more before admitting any work.
    # Match owner-consent/configuration lock ordering. Once capture is finished,
    # neither an edit nor grant revocation may cross the final admission commit.
    await backend.db.scalar(
        select(Configuration)
        .where(
            Configuration.tenant_id == backend.actor.org_id,
            Configuration.configuration_id == configuration.configuration_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    grant = await backend.db.scalar(
        select(CredentialServiceDelegation)
        .where(
            CredentialServiceDelegation.id == grant.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if grant is None:
        raise HTTPException(403, detail={"code": "knowledge_publication_consent_required"})
    await require_knowledge_scope(backend.db, grant, admitted=False)
    await backend.authorize_settings(version.settings, project_id, "devops")
    try:
        admission = admitted_selection(selection, snapshots, deadline)
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(409, detail={"code": str(error)}) from None
    return await store.admit_knowledge(request, key, etag, verified, admission)
