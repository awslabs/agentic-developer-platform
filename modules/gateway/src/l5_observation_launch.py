"""Durable, sanitized launch observations; never retain a federation credential."""

import re
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session
from src.orchestration.l5_observation_contract import CONTRACT, LAUNCH, PREPARED
from src.orchestration.l5_observation_scope import ObservationError, require
from src.orchestration.l5_observations import append, current, owner_endorsement
from src.orchestration.models import OrchestrationDecision
from src.shared.database import get_session_factory

HEADER = "X-ADP-Observation-ID"


class LaunchObservation:
    def __init__(self, factory, identity, data, human):
        self.factory, self.identity, self.data, self.human = factory, identity, data, human
        self.provider_started = False

    async def event(self, phase, *, status=None, error_code=None, revalidate=False):
        # Separate committed transactions survive the original HTTP request's
        # rollback. A persisted pre-call marker prevents a restart from claiming
        # zero provider calls or silently repeating an uncertain federation.
        async with self.factory() as db:
            if revalidate:
                row, data, _ = await current(db, flow_id=self.identity.flow_id, challenge_id=self.identity.id, human=self.human)
                await owner_endorsement(db, row=row, data=data, now=datetime.now(UTC))
                self.provider_started = True
            parent = await db.get(OrchestrationDecision, self.identity.id)
            require(parent is not None and parent.kind == PREPARED, "l5_observation_unverifiable")
            payload = {
                "contract": CONTRACT,
                "challenge_id": parent.id,
                "snapshot_sha256": self.data["snapshot_sha256"],
                "observer_id": self.human.user_id,
                "phase": phase,
                "observed_at": datetime.now(UTC).isoformat(),
                "http_status": status,
                "error_code": error_code,
                "provider_session_calls": None if self.provider_started else 0,
                "provider_invocation_started": self.provider_started,
            }
            identity = str(uuid5(NAMESPACE_URL, f"{CONTRACT}:launch:{parent.id}:{phase}"))
            require(await db.get(OrchestrationDecision, identity) is None, "l5_observation_launch_already_attempted")
            row = append(db, identity=parent, kind=LAUNCH, actor_id="system:l5-native-launch", actor_kind="service", row_id=identity, data=payload)
            await db.commit()
            return row.id

    async def before_provider(self):
        await self.event("provider_started", revalidate=True)

    async def finish(self, *, status, error_code=None):
        phase = "issued" if status == 200 else "denied" if status in {401, 403, 404} and not self.provider_started else "unknown"
        safe_code = error_code if isinstance(error_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,79}", error_code) else None
        return await self.event(phase, status=status, error_code=safe_code)


async def start(backend, request, product_id, project_id, binding_id, configuration_revision):
    selected = request.headers.get(HEADER)
    if selected is None:
        return None
    if re.fullmatch(r"[a-f0-9-]{36}", selected) is None:
        raise HTTPException(422, detail={"code": "l5_observation_invalid"})
    factory = get_session_factory()
    try:
        async with factory() as db:
            require(backend.actor.token_use == "access" and backend.actor.expires_at is not None, "l5_observation_human_required", 401)
            human = await authorize_human_session(backend.actor, db)
            parent = await db.get(OrchestrationDecision, selected)
            require(parent is not None and parent.org_id == human.tenant_id and parent.kind == PREPARED, "not_found", 404)
            row, data, selection = await current(db, flow_id=parent.flow_id, challenge_id=selected, human=human, lock=True)
            await owner_endorsement(db, row=row, data=data, now=datetime.now(UTC))
            require(
                selection.observer_id == human.user_id
                and selection.subject in {"native_ui", "native_denial"}
                and (selection.product_id, selection.project_id, selection.binding_id, selection.service_id)
                == (product_id, project_id, binding_id, "devops")
                and data["snapshot"]["selection"]["configuration_revision"] == configuration_revision,
                "not_found",
                404,
            )
            prepared_id = str(uuid5(NAMESPACE_URL, f"{CONTRACT}:launch:{selected}:prepared"))
            require(await db.get(OrchestrationDecision, prepared_id) is None, "l5_observation_launch_already_attempted")
            # This row and the challenge lock are committed atomically. There is
            # at most one selected launch, including lost-response retries.
            append(
                db,
                identity=row,
                kind=LAUNCH,
                actor_id="system:l5-native-launch",
                actor_kind="service",
                row_id=prepared_id,
                data={
                    "contract": CONTRACT,
                    "challenge_id": row.id,
                    "snapshot_sha256": data["snapshot_sha256"],
                    "observer_id": human.user_id,
                    "phase": "prepared",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "provider_session_calls": 0,
                    "provider_invocation_started": False,
                },
            )
            await db.commit()
        return LaunchObservation(factory, row, data, human)
    except BootstrapRefusedError:
        raise HTTPException(403, detail={"code": "l5_observation_human_required"}) from None
    except ObservationError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from None
