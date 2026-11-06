"""Fail-closed, pre-transmission WA Start journal; not a native effect counter.

The before-send SDK hook commits before the HTTP transport runs. An unmatched
before record is uncertain, including a crash before transmission. It must never
be relabelled as an accepted generation or a reason to repeat Start.
"""

import asyncio
from hashlib import sha256
import json
from datetime import UTC, datetime
from uuid import uuid4

from l5_services.models.wa_periodic import WATransportObservation


CONTRACT = "wa-start-transport/v1"
EVENT = "wellarchitected.StartAgentRecommendationGeneration"


class StartObserver:
    def __init__(self, sessions, run, attempt, prepared):
        self.sessions = sessions
        self.loop = asyncio.get_running_loop()
        self.scope = {key: getattr(run, key) for key in (
            "tenant_id", "configuration_id", "configuration_revision", "run_id")}
        self.scope["attempt_id"] = attempt.attempt_id
        self.identity = {"contract": CONTRACT, "request_hash": prepared.request_hash,
                         "profile_arn": prepared.profile_arn, "name": prepared.name,
                         "source_manifest_sha256": sha256(json.dumps(run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                         "native_effect_count": None}
        self.send_id = None

    async def append(self, phase, facts):
        async with self.sessions() as session:
            session.add(WATransportObservation(
                observation_id=str(uuid4()), send_id=self.send_id, **self.scope,
                observed_at=datetime.now(UTC), phase=phase, facts={**self.identity, **facts}))
            await session.commit()

    def record(self, phase, facts):
        # Executed only in the SDK thread; never block its owning asyncio loop.
        future = asyncio.run_coroutine_threadsafe(self.append(phase, facts), self.loop)
        future.result(timeout=15)

    def before_send(self, request, **kwargs):
        if self.send_id is not None:
            raise RuntimeError("a second non-idempotent WA Start transmission is forbidden")
        self.send_id = str(uuid4())
        self.record("before_send", {})

    def after_call(self, http_response, parsed, **kwargs):
        if self.send_id is None:
            raise RuntimeError("WA Start response has no committed before-send observation")
        metadata = parsed.get("ResponseMetadata") or {}
        self.record("after_call", {
            "http_status": http_response.status_code,
            "provider_request_id": metadata.get("RequestId"),
            "generation_id": parsed.get("id"),
            "response_profile_arn": parsed.get("profileArn"),
            "response_name": parsed.get("name"),
        })

    def install(self, client):
        if client.meta.config.retries.get("total_max_attempts") != 1:
            raise ValueError("WA transport observations require disabled SDK retries")
        client.meta.events.register("before-send." + EVENT, self.before_send)
        client.meta.events.register("after-call." + EVENT, self.after_call)
