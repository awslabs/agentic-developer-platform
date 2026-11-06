"""Opt-in durable canonical API effects; these are never UI attestations."""

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run, view_run
from sqlalchemy import select

from src.orchestration.l5_observation_contract import PREPARED
from src.orchestration.l5_observation_routes import human_session
from src.orchestration.l5_observation_scope import ObservationError, digest, require
from src.orchestration.l5_observations import append, current, owner_endorsement
from src.orchestration.models import OrchestrationDecision

CONTRACT = "l5-request-effect/v1"
KIND = "l5_observation_effect"
HEADER = "X-ADP-Observation-Effect"
RECEIPT_HEADER = "X-ADP-Effect-Receipt"


def receipt_id(challenge_id, phase):
    return str(uuid5(NAMESPACE_URL, f"{CONTRACT}:{challenge_id}:{phase}"))


async def configuration_state(db, tenant_id, configuration_id):
    configuration = await db.get(Configuration, (tenant_id, configuration_id), populate_existing=True)
    require(configuration is not None, "not_found", 404)
    version = await db.get(ConfigurationVersion, (tenant_id, configuration_id, configuration.current_revision), populate_existing=True)
    require(version is not None, "l5_effect_unverifiable")
    return {
        "configuration_id": configuration_id,
        "revision": version.revision,
        "sha256": version.hash,
        "state": version.state,
        "handoff_state": configuration.handoff_state,
    }


class Effect:
    def __init__(self, factory, parent, data, prepared):
        self.factory, self.parent, self.data, self.prepared = factory, parent, data, prepared

    async def finish(self, *, response=None, status=None, error_code=None):
        # The operation may have committed before its HTTP response was lost.
        # This transaction is independent of the original request's rollback.
        async with self.factory() as db:
            after = await configuration_state(db, self.parent.org_id, self.data["snapshot"]["selection"]["configuration_id"])
            payload = {
                **self.prepared,
                "phase": "after",
                "recorded_at": datetime.now(UTC).isoformat(),
                "after": after,
                "http_status": status,
                "error_code": error_code if isinstance(error_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error_code) else None,
                "response_sha256": digest(response) if response is not None else None,
                "outcome": "returned" if response is not None else "returned_error" if status is not None else "unknown",
            }
            if self.prepared["operation"] == "save" and response is not None:
                payload["returned_configuration"] = {
                    "configuration_id": response["configuration_id"],
                    "revision": response["revision"],
                    "sha256": response["hash"],
                    "state": response["state"],
                    "handoff_state": response["handoff_state"],
                }
            if self.prepared["operation"] == "check" and response is not None:
                payload["readiness"] = {"ready": response.get("ready"), "provider_contacted": response.get("provider_contacted")}
            if self.prepared["operation"] in {"replay", "submit"} and response is not None:
                payload["returned_run_id"] = response["run_id"]
            append(
                db,
                identity=self.parent,
                kind=KIND,
                actor_id="system:l5-request-effect",
                actor_kind="service",
                row_id=receipt_id(self.parent.id, "after"),
                data=payload,
            )
            await db.commit()
        return receipt_id(self.parent.id, "after")


async def start(backend, request, *, operation, product_id, project_id, service_id, payload):
    selected = request.headers.get(HEADER)
    require(isinstance(selected, str) and re.fullmatch(r"[a-f0-9-]{36}", selected), "l5_effect_invalid", 422)
    factory = backend.sessions
    async with factory() as db:
        # Authenticated API effects do not assert who operated a UI. In
        # particular fixture credentials cannot manufacture displayed evidence.
        human = await human_session(request, backend.actor, db)
        parent = await db.get(OrchestrationDecision, selected)
        require(parent is not None and parent.org_id == human.tenant_id and parent.kind == PREPARED, "not_found", 404)
        row, data, scope = await current(db, flow_id=parent.flow_id, challenge_id=selected, human=human, lock=True)
        await owner_endorsement(db, row=row, data=data, now=datetime.now(UTC))
        require(
            scope.observer_id == human.user_id
            and scope.check_id == "ui-cli-mcp-parity"
            and scope.subject == ("result" if operation == "replay" else "run_submission" if operation == "submit" else "configuration")
            and (scope.product_id, scope.project_id, scope.service_id) == (product_id, project_id, service_id),
            "not_found",
            404,
        )
        before_id = receipt_id(selected, "before")
        require(await db.get(OrchestrationDecision, before_id) is None, "l5_effect_already_attempted")
        selection = data["snapshot"]["selection"]
        before = await configuration_state(db, row.org_id, selection["configuration_id"])
        require(
            (before["revision"], before["sha256"]) == (selection["configuration_revision"], selection["configuration_sha256"]),
            "l5_observation_snapshot_changed",
        )
        if operation == "save":
            # Creation has no existing configuration snapshot. This collector
            # intentionally records edits, without changing creation semantics.
            require(payload["if_none_match"] is None, "l5_effect_existing_configuration_required", 422)
        replay = None
        if operation == "submit":
            require(service_id == "devops" and request.url.path.endswith("/runs"), "l5_effect_operation_unsupported", 422)
            key = (row.org_id, product_id, project_id, service_id, human.user_id, "submit", payload["idempotency_key"])
            require(await db.get(RunReceipt, key) is None, "l5_effect_already_attempted")
            require(
                payload["input"].get("binding_id") == selection["binding_id"]
                and payload["input"].get("configuration_revision") == selection["configuration_revision"],
                "l5_effect_selection_mismatch",
            )
        if operation == "replay":
            key = (row.org_id, product_id, project_id, service_id, human.user_id, "submit", payload["idempotency_key"])
            receipt = await db.get(RunReceipt, key)
            run = await db.get(Run, (row.org_id, receipt.run_id)) if receipt is not None else None
            require(
                run is not None
                and receipt.run_id == selection["result"]["run_id"]
                and run.input == payload["input"]
                and run.configuration_id == selection["configuration_id"]
                and (run.product_id, run.project_id, run.service_id) == (product_id, project_id, service_id),
                "l5_effect_existing_replay_required",
            )
            expected = digest({"input": payload["input"], "event_id": None, "exclusions": run.admission.get("exclusions", [])})
            require(receipt.request_hash == expected, "l5_effect_replay_request_changed")
            replay = {"run_id": run.run_id, "request_sha256": receipt.request_hash, "idempotency_key_sha256": digest(payload["idempotency_key"])}
        prepared = {
            "contract": CONTRACT,
            "challenge_id": row.id,
            "snapshot_sha256": data["snapshot_sha256"],
            "actor_id": human.user_id,
            "operation": operation,
            "request_sha256": digest(payload),
            "phase": "before",
            "recorded_at": datetime.now(UTC).isoformat(),
            "before": before,
            "replay": replay,
            **(
                {"submission": {"input_sha256": digest(payload["input"]), "idempotency_key_sha256": digest(payload["idempotency_key"])}}
                if operation == "submit"
                else {}
            ),
            "provenance": "authenticated-canonical-request-effect",
            "release_check_completed": False,
            "native_qualified": False,
        }
        append(db, identity=row, kind=KIND, actor_id="system:l5-request-effect", actor_kind="service", row_id=before_id, data=prepared)
        await db.commit()
        identity = SimpleNamespace(id=row.id, org_id=row.org_id, flow_id=row.flow_id, node_id=row.node_id)
    return Effect(factory, identity, data, prepared)


async def verify_submission(db, *, parent, data, body, now=None):
    """Bind a human assertion to successful canonical effects and admission."""
    if body.observation == "mismatch":
        return
    now = now or datetime.now(UTC)
    selected = data["snapshot"]["selection"]
    try:
        events = []
        for phase in ("before", "after"):
            row = await db.get(OrchestrationDecision, receipt_id(parent.id, phase), populate_existing=True)
            require(
                row is not None
                and row.kind == KIND
                and row.actor_kind == "service"
                and row.actor_id == "system:l5-request-effect"
                and (row.org_id, row.flow_id, row.node_id) == (parent.org_id, parent.flow_id, parent.node_id),
                "l5_effect_unverifiable",
            )
            value = json.loads(row.reason)
            require(isinstance(value, dict), "l5_effect_unverifiable")
            recorded = datetime.fromisoformat(value["recorded_at"])
            require(
                value["contract"] == CONTRACT
                and value["operation"] == "submit"
                and value["phase"] == phase
                and value["challenge_id"] == parent.id
                and value["snapshot_sha256"] == data["snapshot_sha256"]
                and value["actor_id"] == data["request"]["observer_id"]
                and value["provenance"] == "authenticated-canonical-request-effect"
                and value["release_check_completed"] is False
                and value["native_qualified"] is False
                and recorded.tzinfo is not None
                and recorded <= now,
                "l5_effect_unverifiable",
            )
            events.append(value)
        before, after = events
        submission = before["submission"]
        require(
            isinstance(submission, dict)
            and set(submission) == {"input_sha256", "idempotency_key_sha256"}
            and all(isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item) for item in submission.values())
            and before["replay"] is None
            and after["outcome"] == "returned"
            and after["http_status"] == 202
            and after["error_code"] is None
            and after["returned_run_id"] == body.submitted_run_id
            and all(after[key] == before[key] for key in ("request_sha256", "submission", "before", "replay"))
            and (before["before"]["configuration_id"], before["before"]["revision"], before["before"]["sha256"])
            == (selected["configuration_id"], selected["configuration_revision"], selected["configuration_sha256"])
            and datetime.fromisoformat(data["created_at"])
            <= datetime.fromisoformat(before["recorded_at"])
            < datetime.fromisoformat(data["expires_at"])
            and datetime.fromisoformat(before["recorded_at"]) <= datetime.fromisoformat(after["recorded_at"]),
            "l5_effect_unverifiable",
        )
        run = await db.get(Run, (parent.org_id, body.submitted_run_id), populate_existing=True)
        require(
            run is not None
            and run.service_id == "devops"
            and (run.product_id, run.project_id, run.binding_id, run.configuration_id, run.configuration_revision)
            == (
                selected["product_id"],
                selected["project_id"],
                selected["binding_id"],
                selected["configuration_id"],
                selected["configuration_revision"],
            )
            and digest(run.input) == submission["input_sha256"],
            "l5_effect_unverifiable",
        )
        receipts = (
            await db.scalars(
                select(RunReceipt).where(
                    RunReceipt.tenant_id == parent.org_id,
                    RunReceipt.product_id == run.product_id,
                    RunReceipt.project_id == run.project_id,
                    RunReceipt.service_id == "devops",
                    RunReceipt.actor_id == data["request"]["observer_id"],
                    RunReceipt.operation == "submit",
                    RunReceipt.run_id == run.run_id,
                )
            )
        ).all()
        matching = [receipt for receipt in receipts if digest(receipt.idempotency_key) == submission["idempotency_key_sha256"]]
        require(
            len(matching) == 1
            and matching[0].request_hash == digest({"input": run.input, "event_id": None, "exclusions": run.admission.get("exclusions", [])})
            and before["request_sha256"] == digest({"input": run.input, "idempotency_key": matching[0].idempotency_key}),
            "l5_effect_unverifiable",
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ObservationError):
            raise
        raise ObservationError("l5_effect_unverifiable") from None
    return after


class EffectBackend:
    """Wrap only external canonical calls; backend internal reads stay unchanged."""

    def __init__(self, backend, request, response):
        self.backend, self.request, self.response = backend, request, response

    def __getattr__(self, name):
        return getattr(self.backend, name)

    async def invoke(self, operation, product_id, project_id, service_id, payload, call):
        try:
            effect = await start(
                self.backend, self.request, operation=operation, product_id=product_id, project_id=project_id, service_id=service_id, payload=payload
            )
        except ObservationError as error:
            raise HTTPException(
                error.status,
                detail={
                    "code": error.code,
                    "next_action": (
                        "Read this challenge's effect receipts before retrying. A prior attempt may have committed; do not repeat uncertain work."
                    ),
                },
            ) from None
        before_id = receipt_id(effect.parent.id, "before")
        if self.response is not None:
            self.response.headers[RECEIPT_HEADER] = before_id
        try:
            result = await call()
            document = view_run(result) if operation in {"replay", "submit"} else result
            if operation == "replay":
                require(document["run_id"] == effect.prepared["replay"]["run_id"], "l5_effect_replay_changed")
            completed = await effect.finish(response=document, status=202 if operation in {"replay", "submit"} else 200)
            if self.response is not None:
                self.response.headers[RECEIPT_HEADER] = completed
            return result
        except BaseException as error:
            try:
                await effect.finish(
                    status=error.status_code if isinstance(error, HTTPException) else None,
                    error_code=error.detail.get("code") if isinstance(error, HTTPException) and isinstance(error.detail, dict) else None,
                )
            except Exception:
                # A retained before marker alone is deliberately an unknown
                # outcome. Never remove it or silently attempt the operation again.
                pass
            if isinstance(error, HTTPException):
                error.headers = {**(error.headers or {}), RECEIPT_HEADER: before_id}
            raise

    async def save(self, product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match):
        return await self.invoke(
            "save",
            product_id,
            project_id,
            service_id,
            {"settings": settings, "idempotency_key": idempotency_key, "if_none_match": if_none_match, "if_match": if_match},
            lambda: self.backend.save(product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match),
        )

    async def check(self, product_id, project_id, service_id):
        return await self.invoke("check", product_id, project_id, service_id, {}, lambda: self.backend.check(product_id, project_id, service_id))

    async def submit(self, product_id, project_id, service_id, run_input, idempotency_key):
        fresh = self.request.headers.get("X-ADP-Observation-Operation") == "submit"
        return await self.invoke(
            "submit" if fresh else "replay",
            product_id,
            project_id,
            service_id,
            {"input": run_input, "idempotency_key": idempotency_key},
            lambda: self.backend.submit(product_id, project_id, service_id, run_input, idempotency_key, replay_only=not fresh),
        )
