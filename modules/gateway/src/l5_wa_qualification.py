"""Owner-selected durable WA observations; no faults, provider calls or new authority."""

import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Header, Response
from l5_services.api.bindings import _intent_hash
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.event import EventReceipt
from l5_services.models.setup import SetupIntent
from l5_services.models.source_retention import SourceRetention
from l5_services.models.wa_qualification import WellArchitectedQualification
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.auth.l5_owner_delegation import _aware, _digest, _error, _owner, delegation_scope
from src.shared.models.audit import AuditLog

SERVICE = "well_architected"
LIMITATIONS = [
    "fault_history_not_recorded",
    "provider_send_count_not_observed",
    "native_state_not_read",
    "source_object_presence_not_read",
    "historical_transitions_not_proved",
]


class EventSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger_id: str = Field(min_length=1, max_length=255)
    event_id: str = Field(min_length=1, max_length=255)


class ObservationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: str = Field(min_length=1, max_length=36)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)
    events: list[EventSelection] = Field(default_factory=list, max_length=10)
    source_revocation_binding_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def bounded_selection(self):
        identities = [(item.trigger_id, item.event_id) for item in self.events]
        if not self.run_id and not identities or len(identities) != len(set(identities)):
            raise ValueError("select an existing run or unique exact event identities")
        if self.source_revocation_binding_id is not None and self.run_id is None:
            raise ValueError("a source revocation observation requires an existing run")
        return self


class CreateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: ObservationSelection
    selection_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime


async def owner(backend, product_id, project_id, service_id, *, enabled=True, active=True):
    if service_id != SERVICE:
        raise _error(404, "not_found", "Qualification selection not found.")
    current = await _owner(backend, product_id, project_id, service_id, active=active)
    if enabled and os.environ.get("L5_WA_QUALIFICATION_OBSERVATIONS_ENABLED") != "true":
        raise _error(
            503,
            "qualification_observations_disabled",
            "Install and explicitly enable the reviewed WA observation capability before collecting evidence.",
        )
    return current


async def event_rows(backend, scope, selected):
    rows = []
    for event in selected.events:
        row = await backend.db.get(
            EventReceipt,
            (scope["tenant_id"], scope["product_id"], scope["project_id"], SERVICE, event.trigger_id, event.event_id),
            populate_existing=True,
        )
        if (
            row is None
            or row.configuration_id != scope["configuration_id"]
            or row.configuration_revision != scope["configuration_revision"]
            or row.event_kind != "schedule"
        ):
            raise _error(404, "not_found", "Selected schedule observation not found.")
        rows.append(row)
    return rows


async def selected_run(backend, scope, selected):
    if selected.run_id is None:
        return None
    run = await backend.get_run(scope["product_id"], scope["project_id"], SERVICE, selected.run_id)
    await backend.db.refresh(run)
    if (
        run.tenant_id,
        run.product_id,
        run.project_id,
        run.service_id,
        run.configuration_id,
        run.configuration_revision,
        run.binding_id,
        run.binding_generation,
    ) != (
        scope["tenant_id"],
        scope["product_id"],
        scope["project_id"],
        SERVICE,
        scope["configuration_id"],
        scope["configuration_revision"],
        scope["binding_id"],
        scope["binding_generation"],
    ) or (run.admission or {}).get("configuration_hash") != scope["configuration_hash"]:
        raise _error(404, "not_found", "Selected run observation not found.")
    return run


async def verified_native_binding(backend, scope):
    binding = await backend.db.get(Binding, (backend.actor.org_id, scope["binding_id"]), populate_existing=True)
    if (
        binding is None
        or (binding.product_id, binding.project_id, binding.service_id, binding.configuration_id, binding.generation)
        != (scope["product_id"], scope["project_id"], SERVICE, scope["configuration_id"], scope["binding_generation"])
        or binding.state != "ready"
        or binding.gaps
        or binding.ownership != "adp_created"
        or not isinstance(binding.creation_receipt, dict)
        or not binding.creation_receipt
        or not isinstance(binding.native_resource_ref, dict)
        or binding.native_resource_ref.get("record_type") != "agent_profile"
        or not binding.native_resource_ref.get("record_id")
        or any(
            (binding.provider_identity or {}).get(field) != scope[key]
            for field, key in (("account_id", "provider_account_id"), ("region", "region"), ("role_arn", "provider_role_arn"))
        )
        or any(
            binding.native_resource_ref.get(field) != scope[key]
            for field, key in (("tenant_id", "tenant_id"), ("project_id", "project_id"), ("source_connection_id", "credential_id"))
        )
    ):
        raise _error(409, "qualification_binding_unverified", "Select a ready receipt-owned WA Profile before recording qualification observations.")
    receipt = binding.creation_receipt
    creation = await backend.db.get(SetupIntent, (backend.actor.org_id, receipt.get("operation_id")), populate_existing=True)
    if (
        creation is None
        or creation.request.get("action") != "create"
        or (creation.product_id, creation.project_id, creation.service_id, creation.configuration_id)
        != (scope["product_id"], scope["project_id"], SERVICE, scope["configuration_id"])
        or creation.scope != binding.scope
        or creation.intent_hash != _intent_hash(creation)
        or receipt.get("intent_hash") != creation.intent_hash
        or receipt.get("native_resource_ref") != binding.native_resource_ref
        or receipt.get("provider_identity") != binding.provider_identity
    ):
        raise _error(409, "qualification_binding_unverified", "The selected Profile's durable creation receipt could not be verified.")
    return binding


async def preview(backend, product_id, project_id, service_id, selected):
    _, version = await owner(backend, product_id, project_id, service_id)
    scope = (await delegation_scope(backend, product_id, project_id, service_id, selected.binding_id))["scope"]
    scope = deepcopy(scope)
    binding = await verified_native_binding(backend, scope)
    scope.update(
        native_resource_ref=deepcopy(binding.native_resource_ref),
        provider_identity=deepcopy(binding.provider_identity),
        creation_receipt_digest=_digest(binding.creation_receipt),
    )
    run = await selected_run(backend, scope, selected)
    await event_rows(backend, scope, selected)
    result = {
        "mode": "observations",
        "scope": scope,
        "targets": selected.model_dump(exclude={"source_revocation_binding_id"} if selected.source_revocation_binding_id is None else set()),
        "run_input_digest": _digest(run.input) if run is not None else None,
        "run_admission_digest": _digest(run.admission) if run is not None else None,
    }
    if selected.source_revocation_binding_id is not None:
        from src.l5_wa_source_revocation import freeze_source

        result["source_revocation"] = await freeze_source(backend, scope, run, selected.source_revocation_binding_id, version.settings)
    return {"selection": result, "selection_digest": _digest(result), "execution_authorized": False}


def view(row):
    return {
        "experiment_id": row.experiment_id,
        "mode": row.mode,
        "selection": row.selection,
        "selection_digest": row.selection_digest,
        "revision": row.revision,
        "created_at": _aware(row.created_at).isoformat(),
        "expires_at": _aware(row.expires_at).isoformat(),
        "revoked_at": _aware(row.revoked_at).isoformat() if row.revoked_at else None,
        "active": row.revoked_at is None and _aware(row.expires_at) > datetime.now(UTC),
        "execution_authorized": False,
    }


async def load(backend, product_id, project_id, service_id, experiment_id):
    configuration, _ = await owner(backend, product_id, project_id, service_id, enabled=False, active=False)
    row = await backend.db.get(WellArchitectedQualification, (backend.actor.org_id, experiment_id), populate_existing=True)
    if (
        row is None
        or row.actor_id != backend.actor.user_id
        or row.configuration_id != configuration.configuration_id
        or row.mode != "observations"
        or _digest(row.selection) != row.selection_digest
        or row.selection.get("scope", {}).get("product_id") != product_id
        or row.selection.get("scope", {}).get("project_id") != project_id
    ):
        raise _error(404, "not_found", "Qualification selection not found.")
    return row


async def create(backend, product_id, project_id, service_id, body, key, etag):
    current = await preview(backend, product_id, project_id, service_id, body.selection)
    scope, now = current["selection"]["scope"], datetime.now(UTC)
    if current["selection_digest"] != body.selection_digest or etag != f'"{scope["configuration_hash"]}"':
        raise _error(412, "qualification_scope_changed", "Preview the exact current selection again before recording observations.")
    if not now + timedelta(minutes=1) < body.expires_at <= now + timedelta(hours=24):
        raise _error(422, "qualification_expiry_invalid", "Select an observation expiry between one minute and 24 hours from now.")
    configuration = await backend.db.scalar(
        select(Configuration)
        .where(Configuration.tenant_id == scope["tenant_id"], Configuration.configuration_id == scope["configuration_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if configuration is None or configuration.current_revision != scope["configuration_revision"]:
        raise _error(412, "qualification_scope_changed", "The configuration changed. Preview the current selection again.")
    latest = await preview(backend, product_id, project_id, service_id, body.selection)
    if latest != current:
        raise _error(412, "qualification_scope_changed", "Source or owner authority changed while recording the selection.")
    identity = _digest([scope["tenant_id"], product_id, project_id, backend.actor.user_id, key])
    experiment_id = str(uuid5(NAMESPACE_URL, "adp:wa-observation-selection:" + identity))
    request_digest = _digest({"selection": current["selection"], "expires_at": body.expires_at.astimezone(UTC).isoformat()})
    row = await backend.db.get(WellArchitectedQualification, (scope["tenant_id"], experiment_id), populate_existing=True)
    if row is not None:
        if row.request_digest != request_digest:
            raise _error(409, "idempotency_conflict", "This key already records another selection or expiry.")
        if row.revoked_at is not None or _aware(row.expires_at) <= now:
            raise _error(409, "qualification_inactive", "This observation selection is revoked or expired. Review a new selection explicitly.")
        return view(row)
    row = WellArchitectedQualification(
        tenant_id=scope["tenant_id"],
        experiment_id=experiment_id,
        configuration_id=scope["configuration_id"],
        configuration_revision=scope["configuration_revision"],
        actor_id=backend.actor.user_id,
        mode="observations",
        selection=current["selection"],
        selection_digest=body.selection_digest,
        request_digest=request_digest,
        revision=1,
        created_at=now,
        expires_at=body.expires_at,
    )
    backend.db.add(row)
    backend.db.add(
        AuditLog(
            org_id=scope["tenant_id"],
            actor_id=backend.actor.user_id,
            event_type="l5_wa_observation_selected",
            details={"experiment_id": experiment_id, "selection_digest": body.selection_digest, "request_digest": request_digest},
        )
    )
    try:
        await backend.db.commit()
    except IntegrityError:
        await backend.db.rollback()
        raise _error(409, "qualification_selection_conflict", "The selection changed concurrently. Read it before retrying the same key.") from None
    return view(row)


async def observations(backend, product_id, project_id, service_id, experiment_id):
    row = await load(backend, product_id, project_id, service_id, experiment_id)
    if not view(row)["active"]:
        raise _error(410, "qualification_inactive", "This observation selection is revoked or expired.")
    selected = ObservationSelection.model_validate(row.selection["targets"])
    current = await preview(backend, product_id, project_id, service_id, selected)
    if current["selection"] != row.selection:
        raise _error(
            409,
            "qualification_scope_changed",
            "The exact owner/configuration/source selection changed. Retain prior evidence and review the new scope.",
        )
    scope = row.selection["scope"]
    run = await selected_run(backend, scope, selected)
    holds = []
    if run is not None:
        held = (
            await backend.db.scalars(
                select(SourceRetention)
                .where(
                    SourceRetention.tenant_id == scope["tenant_id"], SourceRetention.project_id == project_id, SourceRetention.run_id == run.run_id
                )
                .order_by(SourceRetention.artifact_id)
                .limit(201)
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(held) > 200:
            raise _error(
                409, "qualification_observation_incomplete", "The selected run exceeds the bounded hold readback; no partial evidence is returned."
            )
        for hold in held:
            holds.append(
                {
                    "artifact": deepcopy(hold.artifact_ref),
                    "reason": hold.reason,
                    "required_until": _aware(hold.required_until).isoformat(),
                    "policy_expires_at": _aware(hold.policy_expires_at).isoformat(),
                    "revoked_at": _aware(hold.revoked_at).isoformat() if hold.revoked_at else None,
                    "released_at": _aware(hold.released_at).isoformat() if hold.released_at else None,
                    "terminal_proof": deepcopy(hold.last_evidence),
                }
            )
    events = [
        {
            field: getattr(event, field)
            for field in (
                "trigger_id",
                "event_id",
                "event_kind",
                "configuration_revision",
                "state",
                "reason",
                "run_id",
                "received_at",
                "recovery_attempts",
                "retry_at",
            )
        }
        for event in await event_rows(backend, scope, selected)
    ]
    # Re-read all authorization and the revocable selection before returning data.
    latest = await preview(backend, product_id, project_id, service_id, selected)
    checked = await load(backend, product_id, project_id, service_id, experiment_id)
    if latest != current or not view(checked)["active"]:
        raise _error(409, "qualification_scope_changed", "Qualification authority changed while reading evidence.")
    return {
        "schema_version": 1,
        "source": "durable-runtime",
        "experiment_id": experiment_id,
        "selection_digest": row.selection_digest,
        "observed_at": datetime.now(UTC).isoformat(),
        "run": None
        if run is None
        else {
            "run_id": run.run_id,
            "revision": run.revision,
            "state": run.state,
            "provider_state": run.provider_state,
            "native_job_ref": deepcopy(run.native_job_ref),
            "deadline_at": _aware(run.deadline_at).isoformat(),
        },
        "holds": holds,
        "events": events,
        "limitations": LIMITATIONS,
        "execution_authorized": False,
    }


def create_router(backend_dependency):
    router = APIRouter(
        prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}/qualification-experiments", tags=["l5-qualification"]
    )
    from src.l5_wa_periodic_observations import add_routes

    add_routes(router, backend_dependency)
    from src.l5_wa_reuse_observations import add_routes as add_reuse_routes

    add_reuse_routes(router, backend_dependency)

    @router.post("/preview")
    async def preview_route(
        product_id: str, project_id: str, service_id: str, body: ObservationSelection, response: Response, backend=Depends(backend_dependency)
    ):
        result = await preview(backend, product_id, project_id, service_id, body)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["selection"]["scope"]["configuration_hash"]}"'})
        return result

    @router.post("")
    async def create_route(
        product_id: str,
        project_id: str,
        service_id: str,
        body: CreateSelection,
        response: Response,
        idempotency_key: str | None = Header(default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        if_match: str | None = Header(default=None),
        backend=Depends(backend_dependency),
    ):
        if not idempotency_key or if_match is None:
            raise _error(428, "precondition_required", "Supply the preview ETag and an Idempotency-Key for this selection.")
        result = await create(backend, product_id, project_id, service_id, body, idempotency_key, if_match)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["revision"]}"'})
        return result

    @router.get("/{experiment_id}")
    async def get_route(
        product_id: str, project_id: str, service_id: str, experiment_id: str, response: Response, backend=Depends(backend_dependency)
    ):
        result = view(await load(backend, product_id, project_id, service_id, experiment_id))
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["revision"]}"'})
        return result

    @router.get("/{experiment_id}/observations")
    async def read_route(
        product_id: str, project_id: str, service_id: str, experiment_id: str, response: Response, backend=Depends(backend_dependency)
    ):
        response.headers["Cache-Control"] = "no-store"
        return await observations(backend, product_id, project_id, service_id, experiment_id)

    @router.get("/{experiment_id}/source-revocation-observations/{operation_id}")
    async def source_revocation_route(
        product_id: str,
        project_id: str,
        service_id: str,
        experiment_id: str,
        operation_id: str,
        response: Response,
        backend=Depends(backend_dependency),
    ):
        from src.l5_wa_source_revocation import observe_source_revocation

        response.headers["Cache-Control"] = "no-store"
        return await observe_source_revocation(backend, product_id, project_id, service_id, experiment_id, operation_id)

    @router.delete("/{experiment_id}")
    async def revoke_route(
        product_id: str,
        project_id: str,
        service_id: str,
        experiment_id: str,
        response: Response,
        if_match: str | None = Header(default=None),
        backend=Depends(backend_dependency),
    ):
        row = await load(backend, product_id, project_id, service_id, experiment_id)
        if if_match is None:
            raise _error(428, "precondition_required", "Supply the observation selection revision in If-Match.")
        if if_match != f'"{row.revision}"':
            raise _error(412, "stale_revision", "Read the current observation selection before revoking it.")
        if row.revoked_at is None:
            changed = await backend.db.execute(
                update(WellArchitectedQualification)
                .where(
                    WellArchitectedQualification.tenant_id == backend.actor.org_id,
                    WellArchitectedQualification.experiment_id == experiment_id,
                    WellArchitectedQualification.revision == row.revision,
                    WellArchitectedQualification.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC), revision=row.revision + 1)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise _error(412, "stale_revision", "Observation selection changed; read it before retrying revocation.")
            backend.db.add(
                AuditLog(
                    org_id=backend.actor.org_id,
                    actor_id=backend.actor.user_id,
                    event_type="l5_wa_observation_revoked",
                    details={"experiment_id": experiment_id},
                )
            )
            await backend.db.commit()
            await backend.db.refresh(row)
        result = view(row)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["revision"]}"'})
        return result

    return router
