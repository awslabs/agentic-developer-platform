"""Historical canonical effects, exported only to their own active release job."""

import json
import re
from datetime import datetime
from typing import Literal

from l5_services.models.configuration import ConfigurationVersion
from pydantic import Field

from src.l5_observation_effects import CONTRACT, KIND, receipt_id

from .l5_observation_export import (
    ExportObservation,
    current_content_authority,
    decision_reference,
    export_document,
    historical_challenge,
)
from .l5_observation_scope import ObservationError, require
from .l5_observations import owner_endorsement
from .models import OrchestrationDecision

EXPORT_CONTRACT = "l5-request-effect-export/v1"
EXPORTED = "l5_effect_exported"
BEFORE_FIELDS = {
    "contract",
    "challenge_id",
    "snapshot_sha256",
    "actor_id",
    "operation",
    "request_sha256",
    "phase",
    "recorded_at",
    "before",
    "replay",
    "provenance",
    "release_check_completed",
    "native_qualified",
}
AFTER_FIELDS = {"after", "http_status", "error_code", "response_sha256", "outcome"}


class ExportEffect(ExportObservation):
    operation: Literal["save", "check", "replay", "submit"]
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


async def version_matches(db, parent, value):
    require(isinstance(value, dict) and set(value) == {"configuration_id", "revision", "sha256", "state", "handoff_state"}, "l5_effect_unverifiable")
    require(
        type(value["revision"]) is int
        and value["revision"] > 0
        and isinstance(value["sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is not None,
        "l5_effect_unverifiable",
    )
    version = await db.get(ConfigurationVersion, (parent.org_id, value["configuration_id"], value["revision"]))
    require(version is not None and version.hash == value["sha256"] and version.state == value["state"], "l5_effect_unverifiable")


async def historical_effect(db, *, parent, expected, dispatch, now):
    data, request, selected, release, actual, created, expired = await historical_challenge(
        db, parent=parent, expected=expected, dispatch=dispatch, now=now
    )
    require(
        request.check_id == "ui-cli-mcp-parity"
        and request.subject
        == ("result" if expected.operation == "replay" else "run_submission" if expected.operation == "submit" else "configuration"),
        "l5_effect_selection_mismatch",
    )
    before_fields = BEFORE_FIELDS | ({"submission"} if expected.operation == "submit" else set())
    events = []
    references = [decision_reference(parent)]
    for phase in ("before", "after"):
        row = await db.get(OrchestrationDecision, receipt_id(parent.id, phase))
        if row is None:
            require(phase == "after", "l5_effect_not_recorded")
            break
        require(
            row.kind == KIND
            and row.actor_kind == "service"
            and row.actor_id == "system:l5-request-effect"
            and (row.org_id, row.flow_id, row.node_id) == (parent.org_id, parent.flow_id, parent.node_id),
            "l5_effect_unverifiable",
        )
        try:
            value = json.loads(row.reason)
            recorded = datetime.fromisoformat(value["recorded_at"])
            require(
                recorded.tzinfo is not None
                and recorded <= now
                and value["phase"] == phase
                and value["contract"] == CONTRACT
                and value["challenge_id"] == parent.id
                and value["snapshot_sha256"] == data["snapshot_sha256"]
                and value["actor_id"] == request.observer_id
                and value["operation"] == expected.operation
                and value["request_sha256"] == expected.request_sha256
                and value["provenance"] == "authenticated-canonical-request-effect"
                and value["release_check_completed"] is False
                and value["native_qualified"] is False,
                "l5_effect_unverifiable",
            )
            if phase == "before":
                require(
                    set(value) == before_fields
                    and created <= recorded < expired
                    and (value["before"]["configuration_id"], value["before"]["revision"], value["before"]["sha256"])
                    == (selected["configuration_id"], selected["configuration_revision"], selected["configuration_sha256"]),
                    "l5_effect_unverifiable",
                )
                endorsement = await owner_endorsement(db, row=parent, data=data, now=recorded)
                if endorsement is not None:
                    references.append(decision_reference(await db.get(OrchestrationDecision, endorsement["receipt_id"])))
                await version_matches(db, parent, value["before"])
                if expected.operation == "replay":
                    replay = value["replay"]
                    require(
                        isinstance(replay, dict)
                        and set(replay) == {"run_id", "request_sha256", "idempotency_key_sha256"}
                        and replay["run_id"] == selected["result"]["run_id"]
                        and all(
                            isinstance(replay[key], str) and re.fullmatch(r"[a-f0-9]{64}", replay[key])
                            for key in ("request_sha256", "idempotency_key_sha256")
                        ),
                        "l5_effect_unverifiable",
                    )
                else:
                    require(value["replay"] is None, "l5_effect_unverifiable")
            else:
                before = events[0]
                extra = {"save": "returned_configuration", "check": "readiness", "replay": "returned_run_id", "submit": "returned_run_id"}[
                    expected.operation
                ]
                require(
                    value["outcome"] in {"returned", "returned_error", "unknown"}
                    and set(value) == before_fields | AFTER_FIELDS | ({extra} if value["outcome"] == "returned" else set())
                    and recorded >= datetime.fromisoformat(before["recorded_at"])
                    and all(value[key] == before[key] for key in before_fields - {"phase", "recorded_at"})
                    and value["after"]["configuration_id"] == selected["configuration_id"],
                    "l5_effect_unverifiable",
                )
                await version_matches(db, parent, value["after"])
                if value["outcome"] == "returned":
                    require(
                        isinstance(value["response_sha256"], str)
                        and re.fullmatch(r"[a-f0-9]{64}", value["response_sha256"]) is not None
                        and value["http_status"] == (202 if expected.operation in {"replay", "submit"} else 200)
                        and value["error_code"] is None,
                        "l5_effect_unverifiable",
                    )
                    if expected.operation == "save":
                        returned = value["returned_configuration"]
                        require(
                            returned["configuration_id"] == selected["configuration_id"]
                            and returned["revision"] >= selected["configuration_revision"],
                            "l5_effect_unverifiable",
                        )
                        await version_matches(db, parent, returned)
                    elif expected.operation == "check":
                        require(
                            set(value["readiness"]) == {"ready", "provider_contacted"}
                            and all(type(item) is bool for item in value["readiness"].values()),
                            "l5_effect_unverifiable",
                        )
                    elif expected.operation == "submit":
                        from types import SimpleNamespace

                        from src.l5_observation_effects import verify_submission

                        await verify_submission(
                            db, parent=parent, data=data, body=SimpleNamespace(submitted_run_id=value["returned_run_id"], observation="submitted")
                        )
                    else:
                        require(value["returned_run_id"] == selected["result"]["run_id"], "l5_effect_unverifiable")
                else:
                    require(value["response_sha256"] is None, "l5_effect_unverifiable")
                    require(
                        (value["outcome"] == "returned_error" and type(value["http_status"]) is int and 400 <= value["http_status"] <= 599)
                        or (value["outcome"] == "unknown" and value["http_status"] is None),
                        "l5_effect_unverifiable",
                    )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ObservationError):
                raise
            raise ObservationError("l5_effect_unverifiable") from None
        events.append(value)
        references.append(decision_reference(row))
    await current_content_authority(db, parent=parent, selection=selected)
    return {
        "challenge_id": parent.id,
        "snapshot_sha256": data["snapshot_sha256"],
        "selection": actual,
        "release": release,
        "operation": expected.operation,
        "request_sha256": expected.request_sha256,
        "events": events,
        "outcome": events[-1].get("outcome", "unknown"),
        "decision_references": references,
        "provenance": "authenticated-canonical-request-effect",
        "release_check_completed": False,
        "native_qualified": False,
    }


async def export_effect(service, body, claims):
    return await export_document(
        service, body, claims, collector=historical_effect, contract=EXPORT_CONTRACT, kind=EXPORTED, actor="system:l5-effect-export", field="effect"
    )
