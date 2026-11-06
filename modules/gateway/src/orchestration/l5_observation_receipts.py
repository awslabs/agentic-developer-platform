"""Verify the durable launch chain separately from human display assertions."""

import json
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from .l5_observation_contract import CONTRACT, LAUNCH
from .l5_observation_scope import ObservationError, require
from .models import OrchestrationDecision


def launch_id(challenge_id, phase):
    return str(uuid5(NAMESPACE_URL, f"{CONTRACT}:launch:{challenge_id}:{phase}"))


async def launch_history(db, *, parent, data, now):
    """Expose sanitized durable progress even when a launch lost its response."""
    history = []
    terminal = 0
    for phase in ("prepared", "provider_started", "issued", "denied", "unknown"):
        row = await db.get(OrchestrationDecision, launch_id(parent.id, phase), populate_existing=True)
        if row is not None:
            payload = launch_data(row, parent=parent, data=data, now=now, phase=phase)
            history.append({"receipt_id": row.id, **payload})
            terminal += phase in {"issued", "denied", "unknown"}
    require(terminal <= 1, "l5_observation_launch_unverified")
    return history


def launch_data(row, *, parent, data, now, phase=None):
    require(
        row is not None
        and row.org_id == parent.org_id
        and row.flow_id == parent.flow_id
        and row.node_id == parent.node_id
        and row.kind == LAUNCH
        and row.actor_kind == "service"
        and row.actor_id == "system:l5-native-launch",
        "l5_observation_launch_unverified",
    )
    try:
        result = json.loads(row.reason)
        observed_at = datetime.fromisoformat(result["observed_at"])
        created_at = datetime.fromisoformat(data["created_at"])
        expires_at = datetime.fromisoformat(data["expires_at"])
        valid = (
            result["contract"] == CONTRACT
            and result["challenge_id"] == parent.id
            and result["observer_id"] == data["request"]["observer_id"]
            and result["snapshot_sha256"] == data["snapshot_sha256"]
            and (phase is None or result["phase"] == phase)
            and row.id == launch_id(parent.id, result["phase"])
            and created_at <= observed_at < expires_at
            and observed_at <= now
        )
    except (KeyError, TypeError, ValueError):
        raise ObservationError("l5_observation_launch_unverified") from None
    require(valid, "l5_observation_launch_unverified")
    return result


async def verify_launch(db, *, parent, data, body, now):
    require(body.launch_receipt_id is not None, "l5_observation_launch_unverified")
    terminal = launch_data(await db.get(OrchestrationDecision, body.launch_receipt_id, populate_existing=True), parent=parent, data=data, now=now)
    require(terminal["phase"] in {"issued", "denied", "unknown"}, "l5_observation_launch_unverified")
    prepared = launch_data(
        await db.get(OrchestrationDecision, launch_id(parent.id, "prepared"), populate_existing=True),
        parent=parent,
        data=data,
        now=now,
        phase="prepared",
    )
    require(
        prepared.get("provider_invocation_started") is False
        and prepared.get("provider_session_calls") == 0
        and prepared["observed_at"] <= terminal["observed_at"],
        "l5_observation_launch_unverified",
    )
    marker = await db.get(OrchestrationDecision, launch_id(parent.id, "provider_started"), populate_existing=True)
    started = terminal.get("provider_invocation_started")
    require(isinstance(started, bool), "l5_observation_launch_unverified")
    if started:
        marker_data = launch_data(marker, parent=parent, data=data, now=now, phase="provider_started")
        require(
            marker_data.get("provider_invocation_started") is True
            and marker_data.get("provider_session_calls") is None
            and terminal.get("provider_session_calls") is None
            and prepared["observed_at"] <= marker_data["observed_at"] <= terminal["observed_at"],
            "l5_observation_launch_unverified",
        )
    else:
        require(marker is None and terminal.get("provider_session_calls") == 0, "l5_observation_denial_after_provider")
    if terminal["phase"] == "issued":
        require(started and terminal.get("http_status") == 200, "l5_observation_launch_unverified")
    if terminal["phase"] == "denied":
        require(not started and terminal.get("http_status") in {401, 403, 404}, "l5_observation_denial_after_provider")
    if body.observation != "mismatch":
        expected = "denied" if data["request"]["subject"] == "native_denial" else "issued"
        require(terminal["phase"] == expected, "l5_observation_launch_outcome_mismatch")
    return terminal
