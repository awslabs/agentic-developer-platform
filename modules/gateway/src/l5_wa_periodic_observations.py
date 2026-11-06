"""Owner-selected periodic checkpoints; readbacks cannot enable or replay work."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from fastapi import Header, Response
from l5_services.models.binding import Binding
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.models.schedule import EventObservation, ScheduleState
from l5_services.models.wa_periodic import WAPeriodicCheckpoint, WATransportObservation
from l5_services.models.work import Work
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.auth.l5_owner_delegation import _aware, _digest, _error, delegation_scope
from src.l5_wa_periodic_native import read_native
from src.l5_wa_qualification import SERVICE, owner, verified_native_binding

CONTRACT = "wa-periodic-checkpoint/v1"


class CheckpointSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: str = Field(min_length=1, max_length=36)
    trigger_id: str = Field(min_length=1, max_length=255)


class IntervalSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before_id: str = Field(min_length=1, max_length=36)
    after_id: str = Field(min_length=1, max_length=36)
    read_native_audit: bool = False


def stable_scope(scope):
    return {key: value for key, value in scope.items() if key not in {"configuration_revision", "configuration_hash"}}


async def authority(backend, product_id, project_id, service_id, selected):
    _, version = await owner(backend, product_id, project_id, service_id)
    scope = deepcopy((await delegation_scope(backend, product_id, project_id, service_id, selected.binding_id))["scope"])
    binding = await verified_native_binding(backend, scope)
    scope.update(
        native_resource_ref=deepcopy(binding.native_resource_ref),
        provider_identity=deepcopy(binding.provider_identity),
        creation_receipt_digest=_digest(binding.creation_receipt),
    )
    triggers = [item for item in version.settings.get("triggers", []) if item.get("trigger_id") == selected.trigger_id]
    if len(triggers) != 1 or triggers[0].get("kind") != "schedule" or triggers[0].get("binding_id") != selected.binding_id:
        raise _error(404, "not_found", "Selected schedule not found.")
    return scope, version


async def bounded(db, statement, maximum):
    rows = (await db.scalars(statement.limit(maximum + 1).execution_options(populate_existing=True))).all()
    if len(rows) > maximum:
        raise _error(
            409,
            "periodic_observation_incomplete",
            "The selected interval exceeds its readback bound. Retain evidence and select a smaller fresh interval.",
        )
    return rows


def fields(row, names):
    return {key: _aware(value).isoformat() if isinstance(value := getattr(row, key), datetime) else deepcopy(value) for key in names}


async def runtime_snapshot(backend, scope, selected):
    db = backend.db
    cursor = await db.get(ScheduleState, (scope["tenant_id"], scope["configuration_id"], scope["configuration_revision"]), populate_existing=True)
    if cursor is None or cursor.service_id != SERVICE or selected.trigger_id not in cursor.triggers:
        raise _error(
            409,
            "periodic_cursor_not_observed",
            "Wait for installed repair to initialize this exact schedule revision; do not create a retrospective cursor.",
        )
    events = await bounded(
        db,
        select(EventReceipt)
        .where(
            EventReceipt.tenant_id == scope["tenant_id"],
            EventReceipt.product_id == scope["product_id"],
            EventReceipt.project_id == scope["project_id"],
            EventReceipt.service_id == SERVICE,
            EventReceipt.configuration_id == scope["configuration_id"],
            EventReceipt.configuration_revision == scope["configuration_revision"],
            EventReceipt.trigger_id == selected.trigger_id,
        )
        .order_by(EventReceipt.event_id),
        100,
    )
    keys = [event.idempotency_key for event in events]
    if keys and await db.scalar(
        select(EventReceipt.event_id)
        .where(EventReceipt.tenant_id == scope["tenant_id"], EventReceipt.idempotency_key.in_(keys), EventReceipt.service_id != SERVICE)
        .limit(1)
    ):
        raise _error(
            409,
            "periodic_history_ambiguous",
            "The older event journal cannot distinguish this key from another service. Retain it and select a new unique trigger.",
        )
    history = (
        await bounded(
            db,
            select(EventObservation)
            .where(EventObservation.tenant_id == scope["tenant_id"], EventObservation.idempotency_key.in_(keys))
            .order_by(EventObservation.observed_at, EventObservation.observation_id),
            400,
        )
        if keys
        else []
    )
    runs = await bounded(
        db,
        select(Run)
        .where(
            Run.tenant_id == scope["tenant_id"],
            Run.product_id == scope["product_id"],
            Run.project_id == scope["project_id"],
            Run.service_id == SERVICE,
            Run.configuration_id == scope["configuration_id"],
            Run.configuration_revision == scope["configuration_revision"],
        )
        .order_by(Run.run_id),
        100,
    )
    sends = await bounded(
        db,
        select(WATransportObservation)
        .where(
            WATransportObservation.tenant_id == scope["tenant_id"],
            WATransportObservation.configuration_id == scope["configuration_id"],
            WATransportObservation.configuration_revision == scope["configuration_revision"],
        )
        .order_by(WATransportObservation.observed_at, WATransportObservation.observation_id),
        200,
    )
    work = (
        await bounded(
            db, select(Work).where(Work.tenant_id == scope["tenant_id"], Work.run_id.in_([run.run_id for run in runs])).order_by(Work.work_id), 200
        )
        if runs
        else []
    )
    return {
        "cursor": fields(cursor, ("initialized_at", "next_due_at", "triggers")),
        "events": [
            fields(
                item,
                (
                    "trigger_id",
                    "event_id",
                    "idempotency_key",
                    "configuration_revision",
                    "state",
                    "reason",
                    "run_id",
                    "received_at",
                    "recovery_attempts",
                ),
            )
            for item in events
        ],
        "event_history": [
            fields(item, ("observation_id", "idempotency_key", "observed_at", "kind", "state", "reason", "run_id")) for item in history
        ],
        "runs": [
            fields(item, ("run_id", "configuration_revision", "binding_id", "binding_generation", "trigger_id", "state", "native_job_ref"))
            for item in runs
        ],
        "work": [fields(item, ("work_id", "run_id", "kind", "generation", "step", "fence")) for item in work],
        "transport": [
            fields(item, ("observation_id", "send_id", "configuration_revision", "run_id", "attempt_id", "observed_at", "phase", "facts"))
            for item in sends
        ],
    }


def view(row):
    return {
        "checkpoint_id": row.checkpoint_id,
        "snapshot_sha256": row.snapshot_digest,
        "snapshot": deepcopy(row.snapshot),
        "execution_authorized": False,
        "native_qualified": False,
    }


async def retained(backend, product_id, project_id, service_id, checkpoint_id):
    await owner(backend, product_id, project_id, service_id)
    row = await backend.db.get(WAPeriodicCheckpoint, (backend.actor.org_id, checkpoint_id), populate_existing=True)
    if (
        row is None
        or row.actor_id != backend.actor.user_id
        or row.snapshot.get("contract") != CONTRACT
        or row.snapshot_digest != _digest(row.snapshot)
    ):
        raise _error(404, "not_found", "Periodic checkpoint not found.")
    snapshot = row.snapshot
    if snapshot["scope"]["product_id"] != product_id or snapshot["scope"]["project_id"] != project_id:
        raise _error(404, "not_found", "Periodic checkpoint not found.")
    original_scope = snapshot["scope"]
    scope = (await delegation_scope(backend, product_id, project_id, service_id))["scope"]
    binding = await backend.db.get(Binding, (backend.actor.org_id, original_scope["binding_id"]), populate_existing=True)
    if (
        binding is None
        or binding.product_id != product_id
        or binding.project_id != project_id
        or binding.service_id != SERVICE
        or binding.configuration_id != row.configuration_id
        or binding.state not in {"ready", "cleaned"}
        or (
            binding.generation != original_scope["binding_generation"]
            if binding.state == "ready"
            else binding.generation <= original_scope["binding_generation"]
        )
        or binding.native_resource_ref != original_scope["native_resource_ref"]
        or binding.provider_identity != original_scope["provider_identity"]
        or _digest(binding.creation_receipt) != original_scope["creation_receipt_digest"]
    ):
        raise _error(409, "periodic_scope_changed", "The retained Profile or its owned cleanup provenance changed.")
    scope.update(
        binding_id=binding.binding_id,
        binding_generation=original_scope["binding_generation"],
        native_resource_ref=deepcopy(binding.native_resource_ref),
        provider_identity=deepcopy(binding.provider_identity),
        creation_receipt_digest=_digest(binding.creation_receipt),
    )
    if stable_scope(scope) != stable_scope(original_scope) or _aware(row.observed_at).isoformat() != snapshot["observed_at"]:
        raise _error(409, "periodic_scope_changed", "Current owner, source, connection or Profile differs from the retained checkpoint.")
    original = await backend.db.get(
        ConfigurationVersion, (scope["tenant_id"], row.configuration_id, snapshot["scope"]["configuration_revision"]), populate_existing=True
    )
    if original is None or original.hash != snapshot["scope"]["configuration_hash"] or original.settings != snapshot["settings"]:
        raise _error(409, "periodic_scope_changed", "The retained immutable configuration no longer matches its checkpoint.")
    return row


async def checkpoint(backend, product_id, project_id, service_id, selected, key, etag):
    scope, version = await authority(backend, product_id, project_id, service_id, selected)
    if etag != '"' + scope["configuration_hash"] + '"':
        raise _error(412, "periodic_scope_changed", "Read the current configuration ETag before selecting its periodic checkpoint.")
    request = _digest({"selection": selected.model_dump(), "scope": scope})
    identifier = str(uuid5(NAMESPACE_URL, _digest([scope["tenant_id"], product_id, project_id, backend.actor.user_id, key])))
    existing = await backend.db.get(WAPeriodicCheckpoint, (scope["tenant_id"], identifier), populate_existing=True)
    if existing is not None:
        if existing.request_digest != request:
            raise _error(409, "idempotency_conflict", "This key already records a different periodic checkpoint.")
        return view(await retained(backend, product_id, project_id, service_id, identifier))
    started = datetime.now(UTC)
    runtime = await runtime_snapshot(backend, scope, selected)
    native_ids = [(run["native_job_ref"] or {}).get("generation_id") for run in runtime["runs"]]
    native = await read_native(backend, scope, version.settings, generation_ids=sorted({value for value in native_ids if value}))
    latest, current_version = await authority(backend, product_id, project_id, service_id, selected)
    if latest != scope or current_version.settings != version.settings or await runtime_snapshot(backend, scope, selected) != runtime:
        raise _error(
            409, "periodic_scope_changed", "Schedule state or authority changed during observation. Retry readback without replaying native work."
        )
    now = datetime.now(UTC)
    snapshot = {
        "contract": CONTRACT,
        "source": "authenticated-periodic-readback",
        "selection": selected.model_dump(),
        "scope": scope,
        "settings": deepcopy(version.settings),
        "read_started_at": started.isoformat(),
        "observed_at": now.isoformat(),
        "runtime": runtime,
        "native": native,
        "execution_authorized": False,
    }
    row = WAPeriodicCheckpoint(
        tenant_id=scope["tenant_id"],
        checkpoint_id=identifier,
        configuration_id=scope["configuration_id"],
        actor_id=backend.actor.user_id,
        observed_at=now,
        request_digest=request,
        snapshot_digest=_digest(snapshot),
        snapshot=snapshot,
    )
    backend.db.add(row)
    try:
        await backend.db.commit()
    except IntegrityError:
        await backend.db.rollback()
        raise _error(409, "periodic_checkpoint_conflict", "Read the original checkpoint before retrying the same key.") from None
    return view(row)


async def interval(backend, product_id, project_id, service_id, selected):
    before = await retained(backend, product_id, project_id, service_id, selected.before_id)
    after = await retained(backend, product_id, project_id, service_id, selected.after_id)
    first, last = before.snapshot, after.snapshot
    if (
        first["scope"] != last["scope"]
        or first["selection"] != last["selection"]
        or first["settings"] != last["settings"]
        or not _aware(before.observed_at) < datetime.fromisoformat(last["read_started_at"])
        or _aware(after.observed_at) - _aware(before.observed_at) > timedelta(hours=24)
    ):
        raise _error(
            409,
            "periodic_interval_invalid",
            "Select two ordered checkpoints of one exact revision within 24 hours; retain separate enabled and disabled intervals.",
        )
    native = {"source": "unavailable", "code": "periodic_native_audit_not_selected"}
    if selected.read_native_audit:
        native = await read_native(
            backend, first["scope"], first["settings"], interval=(_aware(before.observed_at), datetime.fromisoformat(last["read_started_at"]))
        )
    # Do not return retained facts if source or membership was revoked during I/O.
    await retained(backend, product_id, project_id, service_id, selected.before_id)
    await retained(backend, product_id, project_id, service_id, selected.after_id)
    return {
        "contract": "wa-periodic-interval/v1",
        "before": view(before),
        "after": view(after),
        "native_audit": native,
        "execution_authorized": False,
        "native_qualified": False,
    }


def add_routes(router, backend_dependency):
    from fastapi import Depends

    @router.post("/periodic-checkpoints")
    async def create_checkpoint(
        product_id: str,
        project_id: str,
        service_id: str,
        body: CheckpointSelection,
        response: Response,
        backend=Depends(backend_dependency),
        if_match: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    ):
        if not idempotency_key or if_match is None:
            raise _error(428, "precondition_required", "Supply the current configuration ETag and a checkpoint Idempotency-Key.")
        response.headers["Cache-Control"] = "no-store"
        return await checkpoint(backend, product_id, project_id, service_id, body, idempotency_key, if_match)

    @router.get("/periodic-checkpoints/{checkpoint_id}")
    async def get_checkpoint(
        product_id: str, project_id: str, service_id: str, checkpoint_id: str, response: Response, backend=Depends(backend_dependency)
    ):
        response.headers["Cache-Control"] = "no-store"
        return view(await retained(backend, product_id, project_id, service_id, checkpoint_id))

    @router.post("/periodic-intervals/read")
    async def read_interval(
        product_id: str, project_id: str, service_id: str, body: IntervalSelection, response: Response, backend=Depends(backend_dependency)
    ):
        response.headers["Cache-Control"] = "no-store"
        return await interval(backend, product_id, project_id, service_id, body)
