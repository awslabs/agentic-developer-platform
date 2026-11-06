"""Append-only human observations within the original L5 release contract."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .l5_observation_contract import CONTRACT, ENDORSED, OBSERVED, PREPARED, PrepareObservation, RecordObservation
from .l5_observation_receipts import launch_history, verify_launch
from .l5_observation_scope import ObservationError, aware, digest, require, scope
from .models import OrchestrationDecision


def append(db, *, identity, kind, actor_id, actor_kind, data, row_id=None):
    row = OrchestrationDecision(
        org_id=identity.org_id,
        flow_id=identity.flow_id,
        node_id=identity.node_id,
        kind=kind,
        actor_id=actor_id,
        actor_kind=actor_kind,
        actor_role="observer" if actor_kind == "human" else "engine",
        reason=json.dumps(data, sort_keys=True),
        **({"id": row_id} if row_id else {}),
    )
    db.add(row)
    return row


async def prepare(db, *, flow_id, human, request, now=None):
    now = now or datetime.now(UTC)
    node, snapshot, expiry = await scope(db, flow_id=flow_id, tenant_id=human.tenant_id, owner_id=human.user_id, request=request, now=now)
    payload = request.model_dump(mode="json")
    row_id = str(uuid5(NAMESPACE_URL, f"{CONTRACT}:{human.tenant_id}:{flow_id}:{human.user_id}:{request.request_key}"))
    previous = await db.get(OrchestrationDecision, row_id)
    if previous is not None:
        require(previous.kind == PREPARED and previous.org_id == human.tenant_id)
        require(json.loads(previous.reason).get("request") == payload, "l5_observation_request_key_reused")
        return await read(db, flow_id=flow_id, challenge_id=row_id, human=human, now=now)
    existing = list(
        await db.scalars(
            select(OrchestrationDecision.id)
            .where(OrchestrationDecision.org_id == human.tenant_id, OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == PREPARED)
            .limit(129)
        )
    )
    require(len(existing) < 128, "l5_observation_limit_reached")
    expiry = min(expiry, aware(human.expires_at), now + timedelta(minutes=30))
    require(expiry > now, "l5_observation_expired")
    data = {
        "contract": CONTRACT,
        "request": payload,
        "snapshot": snapshot,
        "snapshot_sha256": digest(snapshot),
        "created_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
    }
    # The owner prepares a bounded opportunity to observe, not a pass or policy change.
    identity = SimpleNamespace(org_id=node.org_id, flow_id=flow_id, node_id=node.id)
    row = append(db, identity=identity, kind=PREPARED, actor_id=human.user_id, actor_kind="human", data=data, row_id=row_id)
    await db.flush()
    return response(row, data, human.user_id)


async def current(db, *, flow_id, challenge_id, human, now=None, lock=False):
    now = now or datetime.now(UTC)
    query = (
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.id == challenge_id,
            OrchestrationDecision.org_id == human.tenant_id,
            OrchestrationDecision.flow_id == flow_id,
            OrchestrationDecision.kind == PREPARED,
        )
        .execution_options(populate_existing=True)
    )
    row = await db.scalar(query)
    require(row is not None and row.actor_kind == "human", "not_found", 404)
    try:
        data = json.loads(row.reason)
        request = PrepareObservation.model_validate(data["request"])
        expiry = datetime.fromisoformat(data["expires_at"])
    except (ValueError, TypeError, KeyError):
        raise ObservationError("l5_observation_unverifiable") from None
    product_owner = data.get("snapshot", {}).get("selection", {}).get("owner_id")
    require(data.get("contract") == CONTRACT and human.user_id in {row.actor_id, request.observer_id, product_owner}, "not_found", 404)
    require(expiry.tzinfo is not None and now < expiry and aware(human.expires_at) > now, "l5_observation_expired")
    node, snapshot, _ = await scope(db, flow_id=flow_id, tenant_id=human.tenant_id, owner_id=row.actor_id, request=request, now=now)
    if lock:
        # Scope locks the flow first, matching preparation and plan mutation.
        row = await db.scalar(query.with_for_update())
    require(
        node.id == row.node_id and snapshot == data["snapshot"] and digest(snapshot) == data["snapshot_sha256"], "l5_observation_snapshot_changed"
    )
    return row, data, request


def response(row, data, user_id, observed=None, launches=None, endorsement=None):
    request = data["request"]
    # Negative-scope observers may see their challenge, never the foreign Product facts.
    private = request["subject"] == "native_denial" and user_id not in {row.actor_id, data["snapshot"]["selection"]["owner_id"]}
    return {
        "contract": CONTRACT,
        "challenge_id": row.id,
        "snapshot_sha256": data["snapshot_sha256"],
        "expires_at": data["expires_at"],
        "service_id": request["service_id"],
        "check_id": request["check_id"],
        "subject": request["subject"],
        "observer_id": request["observer_id"],
        "snapshot": None if private else data["snapshot"],
        "observation_receipt": observed,
        "launch_attempt": launches,
        "owner_endorsement_required": data["snapshot"]["selection"]["owner_id"] != row.actor_id,
        "owner_endorsement": None if private else endorsement,
        "qualification_scope": "human-observation-only",
        "native_qualified": False,
        "release_check_completed": False,
    }


async def owner_endorsement(db, *, row, data, now, required=True):
    owner_id = data["snapshot"]["selection"]["owner_id"]
    if owner_id == row.actor_id:
        return None  # The current Product owner prepared this exact challenge.
    identity = str(uuid5(NAMESPACE_URL, CONTRACT + ":owner-endorsement:" + row.id))
    saved = await db.get(OrchestrationDecision, identity, populate_existing=True)
    if saved is None and not required:
        return None
    require(saved is not None, "l5_observation_owner_endorsement_required", 403)
    try:
        payload = json.loads(saved.reason)
        observed_at, expires_at = (datetime.fromisoformat(payload[key]) for key in ("endorsed_at", "expires_at"))
        valid = (
            saved.org_id == row.org_id
            and saved.flow_id == row.flow_id
            and saved.node_id == row.node_id
            and saved.kind == ENDORSED
            and saved.actor_kind == "human"
            and saved.actor_id == owner_id
            and payload["contract"] == CONTRACT
            and payload["challenge_id"] == row.id
            and payload["snapshot_sha256"] == data["snapshot_sha256"]
            and datetime.fromisoformat(data["created_at"]) <= observed_at <= now < expires_at <= datetime.fromisoformat(data["expires_at"])
        )
    except (TypeError, ValueError, KeyError):
        raise ObservationError("l5_observation_owner_endorsement_unverified") from None
    require(valid, "l5_observation_owner_endorsement_unverified")
    return {"receipt_id": saved.id, "actor_id": saved.actor_id, **payload}


async def endorse(db, *, flow_id, challenge_id, human, body, now=None):
    now = now or datetime.now(UTC)
    row, data, _ = await current(db, flow_id=flow_id, challenge_id=challenge_id, human=human, now=now, lock=True)
    require(human.user_id == data["snapshot"]["selection"]["owner_id"], "l5_observation_product_owner_required", 403)
    require(body.snapshot_sha256 == data["snapshot_sha256"], "l5_observation_snapshot_changed")
    if human.user_id != row.actor_id and await owner_endorsement(db, row=row, data=data, now=now, required=False) is None:
        append(
            db,
            identity=row,
            kind=ENDORSED,
            actor_id=human.user_id,
            actor_kind="human",
            row_id=str(uuid5(NAMESPACE_URL, CONTRACT + ":owner-endorsement:" + row.id)),
            data={
                "contract": CONTRACT,
                "challenge_id": row.id,
                "snapshot_sha256": data["snapshot_sha256"],
                "endorsed_at": now.isoformat(),
                "expires_at": min(datetime.fromisoformat(data["expires_at"]), aware(human.expires_at)).isoformat(),
            },
        )
        await db.flush()
    return await read(db, flow_id=flow_id, challenge_id=challenge_id, human=human, now=now)


async def read(db, *, flow_id, challenge_id, human, now=None):
    row, data, _ = await current(db, flow_id=flow_id, challenge_id=challenge_id, human=human, now=now)
    endorsement = await owner_endorsement(db, row=row, data=data, now=now or datetime.now(UTC), required=False)
    observation_id = str(uuid5(NAMESPACE_URL, CONTRACT + ":observation:" + row.id))
    observed = await db.get(OrchestrationDecision, observation_id)
    receipt = None
    if observed is not None:
        await owner_endorsement(db, row=row, data=data, now=now or datetime.now(UTC))
        require(
            observed.org_id == row.org_id
            and observed.flow_id == flow_id
            and observed.node_id == row.node_id
            and observed.kind == OBSERVED
            and observed.actor_kind == "human"
            and observed.actor_id == data["request"]["observer_id"],
            "l5_observation_unverifiable",
        )
        try:
            payload = json.loads(observed.reason)
            body = RecordObservation.model_validate(payload["observed"])
            observed_at = datetime.fromisoformat(payload["observed_at"])
            valid = (
                payload["contract"] == CONTRACT
                and payload["challenge_id"] == row.id
                and payload["snapshot_sha256"] == body.snapshot_sha256 == data["snapshot_sha256"]
                and payload["provenance"] == "authenticated-human-observation"
                and payload["native_qualified"] is False
                and payload["release_check_completed"] is False
                and datetime.fromisoformat(data["created_at"]) <= observed_at < datetime.fromisoformat(data["expires_at"])
                and observed_at <= (now or datetime.now(UTC))
            )
        except (TypeError, ValueError, KeyError):
            raise ObservationError("l5_observation_unverifiable") from None
        require(valid, "l5_observation_unverifiable")
        if data["request"]["subject"] == "run_submission":
            from src.l5_observation_effects import verify_submission

            await verify_submission(db, parent=row, data=data, body=body, now=observed_at)
        if data["request"]["subject"].startswith("native_"):
            await verify_launch(db, parent=row, data=data, body=body, now=observed_at)
        receipt = {"receipt_id": observed.id, "actor_id": observed.actor_id, **payload}
    launches = (
        await launch_history(db, parent=row, data=data, now=now or datetime.now(UTC)) if data["request"]["subject"].startswith("native_") else None
    )
    return response(row, data, human.user_id, receipt, launches, endorsement)


async def record(db, *, flow_id, challenge_id, human, body, now=None):
    now = now or datetime.now(UTC)
    row, data, request = await current(db, flow_id=flow_id, challenge_id=challenge_id, human=human, now=now, lock=True)
    require(human.user_id == request.observer_id, "l5_observation_selected_human_required", 403)
    require(body.snapshot_sha256 == data["snapshot_sha256"], "l5_observation_snapshot_changed")
    await owner_endorsement(db, row=row, data=data, now=now)
    observation_id = str(uuid5(NAMESPACE_URL, CONTRACT + ":observation:" + row.id))
    existing = await db.get(OrchestrationDecision, observation_id)
    if existing is not None:
        prior = json.loads(existing.reason)
        require(
            RecordObservation.model_validate(prior["observed"]).model_dump(mode="json") == body.model_dump(mode="json"),
            "l5_observation_already_recorded",
        )
        return await read(db, flow_id=flow_id, challenge_id=challenge_id, human=human, now=now)
    validate_observed_fields(request, data["snapshot"]["selection"], body)
    if request.subject == "run_submission":
        from src.l5_observation_effects import verify_submission

        await verify_submission(db, parent=row, data=data, body=body, now=now)
    if request.subject.startswith("native_"):
        await verify_launch(db, parent=row, data=data, body=body, now=now)
    else:
        require(body.launch_receipt_id is None, "l5_observation_fields_mismatch")
    append(
        db,
        identity=row,
        kind=OBSERVED,
        actor_id=human.user_id,
        actor_kind="human",
        row_id=observation_id,
        data={
            "contract": CONTRACT,
            "challenge_id": row.id,
            "snapshot_sha256": data["snapshot_sha256"],
            "observed_at": now.isoformat(),
            "observed": body.model_dump(mode="json"),
            "provenance": "authenticated-human-observation",
            "native_qualified": False,
            "release_check_completed": False,
        },
    )
    await db.flush()
    return await read(db, flow_id=flow_id, challenge_id=challenge_id, human=human, now=now)


def validate_observed_fields(request, selected, body):
    """Live collection and historical export apply the same bounded field rules."""
    observed = body.model_dump(mode="json")
    expected = {
        "configuration": {"displayed_configuration_revision": selected["configuration_revision"]},
        "result": {
            "displayed_result_id": selected.get("result", {}).get("result_id"),
            "displayed_result_revision": selected.get("result", {}).get("revision"),
        },
        "native_ui": {"displayed_native_hostname": selected.get("native_hostname")},
        "native_denial": {},
        "run_submission": {},
    }[request.subject]
    require(request.subject != "run_submission" or body.launch_receipt_id is None, "l5_observation_fields_mismatch")
    values = {key: value for key, value in observed.items() if key.startswith("displayed_") and value is not None}
    require(set(values) <= set(expected), "l5_observation_fields_mismatch")
    require(
        (body.submitted_run_id is not None) == (request.subject == "run_submission" and body.observation == "submitted"),
        "l5_observation_fields_mismatch",
    )
    if body.observation != "mismatch":
        require(
            values == expected
            and body.observation
            == ("denied" if request.subject == "native_denial" else "submitted" if request.subject == "run_submission" else "displayed"),
            "l5_observation_fields_mismatch",
        )
