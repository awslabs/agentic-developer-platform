"""Exact retained-result checkpoints; never publish, reuse, or start native work.

The existing immutable checkpoint store holds a separate versioned contract.
These observations require verified owned Profile provenance and current owner/source rights;
they confer neither consumer consent nor permission to execute a provider action.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from fastapi import Header, Response
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.publication_retry import PublicationRetryReceipt
from l5_services.models.run import Run
from l5_services.models.wa_periodic import WAPeriodicCheckpoint, WATransportObservation
from l5_services.models.wa_usage import WellArchitectedUsage
from l5_services.models.work import Work
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.auth.l5_owner_delegation import _aware, _digest, _error
from src.l5_wa_periodic_native import read_native
from src.l5_wa_periodic_observations import IntervalSelection, bounded, fields, view
from src.l5_wa_qualification import owner
from src.l5_wa_reuse_authorization import selection as retained_result

CONTRACT = "wa-reuse-checkpoint/v1"


class ReuseSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(min_length=1, max_length=36)
    result_revision: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=128)
    context_retry_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    insights_retry_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


async def authority(backend, product_id, project_id, service_id, selected):
    await owner(backend, product_id, project_id, service_id)
    run, result = await retained_result(backend, product_id, project_id, selected.result_id, allow_ready_observation=True)
    if result["result_revision"] != selected.result_revision or selected.task_id == run.run_id:
        raise _error(404, "not_found", "Selected retained result not found.")
    version = await backend.db.get(ConfigurationVersion, (run.tenant_id, run.configuration_id, run.configuration_revision), populate_existing=True)
    if version is None or version.hash != run.admission.get("configuration_hash"):
        raise _error(409, "reuse_scope_changed", "The original immutable configuration could not be verified.")
    # Bind every observation to the original native identity, including after the
    # owned Profile is gone. read_native(interval=...) only calls LookupEvents.
    from l5_services.models.binding import Binding

    binding = await backend.db.get(Binding, (run.tenant_id, run.binding_id), populate_existing=True)
    scope = {
        **result,
        "configuration_id": run.configuration_id,
        "configuration_revision": run.configuration_revision,
        "configuration_hash": version.hash,
        "binding_id": run.binding_id,
        "binding_generation": run.binding_generation,
        "binding_state": binding.state,
        "native_resource_ref": deepcopy(binding.native_resource_ref),
        "provider_identity": deepcopy(binding.provider_identity),
        "credential_id": binding.connection_ref["source_connection_id"],
        "run_admission_digest": _digest(run.admission),
        "run_input_digest": _digest(run.input),
    }
    return scope, version


async def runtime_snapshot(backend, scope, selected):
    db = backend.db
    runs = await bounded(
        db,
        select(Run)
        .where(
            Run.tenant_id == scope["tenant_id"],
            Run.product_id == scope["product_id"],
            Run.project_id == scope["project_id"],
            Run.service_id == "well_architected",
            Run.configuration_id == scope["configuration_id"],
        )
        .order_by(Run.run_id),
        100,
    )
    # Across all revisions of this configuration: a changed trigger/revision
    # cannot hide a new Start inside an allegedly effect-free replay interval.
    transport = await bounded(
        db,
        select(WATransportObservation)
        .where(
            WATransportObservation.tenant_id == scope["tenant_id"],
            WATransportObservation.configuration_id == scope["configuration_id"],
        )
        .order_by(WATransportObservation.observed_at, WATransportObservation.observation_id),
        400,
    )
    work = (
        await bounded(
            db,
            select(Work).where(Work.tenant_id == scope["tenant_id"], Work.run_id.in_([run.run_id for run in runs])).order_by(Work.work_id),
            200,
        )
        if runs
        else []
    )
    retries = []
    publications = {item["destination"]: item for item in scope["publications"]}
    for destination, key in (("product-context", selected.context_retry_key), ("engineering-insights", selected.insights_retry_key)):
        row = await db.get(
            PublicationRetryReceipt,
            (
                scope["tenant_id"],
                scope["product_id"],
                scope["project_id"],
                "well_architected",
                scope["actor_id"],
                key,
            ),
            populate_existing=True,
        )
        if row is None:
            continue
        if row.publication_id != publications[destination]["publication_id"]:
            raise _error(409, "reuse_replay_mismatch", "This original replay key belongs to a different publication.")
        retries.append({"destination": destination, **fields(row, ("idempotency_key", "publication_id", "request_hash", "response"))})
    usage = await db.get(
        WellArchitectedUsage,
        (
            scope["tenant_id"],
            publications["product-context"]["publication_id"],
            selected.task_id,
        ),
        populate_existing=True,
    )
    if usage is not None and (usage.actor_id != scope["actor_id"] or usage.result_id != scope["result_id"]):
        raise _error(404, "not_found", "Selected retained use not found.")
    return {
        "runs": [fields(row, ("run_id", "configuration_revision", "binding_id", "binding_generation", "state", "native_job_ref")) for row in runs],
        "work": [fields(row, ("work_id", "run_id", "kind", "generation", "step", "fence")) for row in work],
        "transport": [
            fields(row, ("observation_id", "send_id", "configuration_revision", "run_id", "attempt_id", "observed_at", "phase", "facts"))
            for row in transport
        ],
        "replays": retries,
        "usage": fields(usage, ("publication_id", "task_id", "result_id", "actor_id", "observed_at", "retrieval_receipt", "usage_receipt"))
        if usage
        else None,
    }


async def retained(backend, product_id, project_id, service_id, identifier):
    await owner(backend, product_id, project_id, service_id)
    row = await backend.db.get(WAPeriodicCheckpoint, (backend.actor.org_id, identifier), populate_existing=True)
    if (
        row is None
        or row.actor_id != backend.actor.user_id
        or row.snapshot.get("contract") != CONTRACT
        or row.snapshot_digest != _digest(row.snapshot)
        or (row.snapshot["scope"]["product_id"], row.snapshot["scope"]["project_id"]) != (product_id, project_id)
    ):
        raise _error(404, "not_found", "Reuse checkpoint not found.")
    scope, _ = await authority(backend, product_id, project_id, service_id, ReuseSelection(**row.snapshot["selection"]))
    if scope != row.snapshot["scope"] or _aware(row.observed_at).isoformat() != row.snapshot["observed_at"]:
        raise _error(409, "reuse_scope_changed", "Owner, source, publication, or original result changed; preserve the existing checkpoint.")
    return row


async def checkpoint(backend, product_id, project_id, service_id, selected, key, etag):
    scope, _ = await authority(backend, product_id, project_id, service_id, selected)
    if etag != '"' + scope["configuration_hash"] + '"':
        raise _error(412, "reuse_scope_changed", "Supply the original result configuration ETag.")
    request = _digest({"selection": selected.model_dump(), "scope": scope})
    identifier = str(uuid5(NAMESPACE_URL, _digest([CONTRACT, scope["tenant_id"], product_id, project_id, backend.actor.user_id, key])))
    existing = await backend.db.get(WAPeriodicCheckpoint, (scope["tenant_id"], identifier), populate_existing=True)
    if existing is not None:
        if existing.request_digest != request:
            raise _error(409, "idempotency_conflict", "This checkpoint key already records a different original selection.")
        return view(await retained(backend, product_id, project_id, service_id, identifier))
    started = datetime.now(UTC)
    runtime = await runtime_snapshot(backend, scope, selected)
    latest, _ = await authority(backend, product_id, project_id, service_id, selected)
    if latest != scope or await runtime_snapshot(backend, scope, selected) != runtime:
        raise _error(409, "reuse_scope_changed", "Work or authority changed during readback; retry observation without replaying work.")
    now = datetime.now(UTC)
    snapshot = {
        "contract": CONTRACT,
        "source": "authenticated-reuse-readback",
        "selection": selected.model_dump(),
        "scope": scope,
        "read_started_at": started.isoformat(),
        "observed_at": now.isoformat(),
        "runtime": runtime,
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
        raise _error(409, "reuse_checkpoint_conflict", "Read the original checkpoint before retrying the same key.") from None
    return view(row)


async def interval(backend, product_id, project_id, service_id, selected):
    before = await retained(backend, product_id, project_id, service_id, selected.before_id)
    after = await retained(backend, product_id, project_id, service_id, selected.after_id)
    first, last = before.snapshot, after.snapshot
    if (
        first["scope"] != last["scope"]
        or first["selection"] != last["selection"]
        or not _aware(before.observed_at) < datetime.fromisoformat(last["read_started_at"])
        or _aware(after.observed_at) - _aware(before.observed_at) > timedelta(hours=24)
    ):
        raise _error(409, "reuse_interval_invalid", "Select ordered checkpoints of the same original result within 24 hours.")
    audit = {"source": "unavailable", "code": "reuse_native_audit_not_selected"}
    if selected.read_native_audit:
        scope, version = await authority(backend, product_id, project_id, service_id, ReuseSelection(**first["selection"]))
        audit = await read_native(
            backend, scope, version.settings, interval=(_aware(before.observed_at), datetime.fromisoformat(last["read_started_at"]))
        )
    await retained(backend, product_id, project_id, service_id, selected.before_id)
    await retained(backend, product_id, project_id, service_id, selected.after_id)
    return {
        "contract": "wa-reuse-interval/v1",
        "before": view(before),
        "after": view(after),
        "native_audit": audit,
        "execution_authorized": False,
        "native_qualified": False,
    }


def add_routes(router, backend_dependency):
    from fastapi import Depends

    @router.post("/reuse-checkpoints")
    async def create_checkpoint(
        product_id: str,
        project_id: str,
        service_id: str,
        body: ReuseSelection,
        response: Response,
        backend=Depends(backend_dependency),
        if_match: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    ):
        if not idempotency_key or if_match is None:
            raise _error(428, "precondition_required", "Supply the original configuration ETag and a checkpoint Idempotency-Key.")
        response.headers["Cache-Control"] = "no-store"
        return await checkpoint(backend, product_id, project_id, service_id, body, idempotency_key, if_match)

    @router.get("/reuse-checkpoints/{checkpoint_id}")
    async def get_checkpoint(
        product_id: str, project_id: str, service_id: str, checkpoint_id: str, response: Response, backend=Depends(backend_dependency)
    ):
        response.headers["Cache-Control"] = "no-store"
        return view(await retained(backend, product_id, project_id, service_id, checkpoint_id))

    @router.post("/reuse-intervals/read")
    async def read_interval(
        product_id: str, project_id: str, service_id: str, body: IntervalSelection, response: Response, backend=Depends(backend_dependency)
    ):
        response.headers["Cache-Control"] = "no-store"
        return await interval(backend, product_id, project_id, service_id, body)
