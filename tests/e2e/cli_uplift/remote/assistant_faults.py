"""E47: observe owner-scoped replay after an explicitly selected, bounded fault."""

import hashlib
import re
import time
import urllib.parse

import assistant_baseline
import assistant_client
import common

FAULTS = frozenset(
    {
        "assistant_duplicate_delivery",
        "assistant_disconnect",
        "assistant_worker_loss",
        "assistant_queue_unavailable",
        "assistant_supervisor_restart",
    }
)
CONTROLLER_FAULTS = FAULTS - {"assistant_disconnect"}
CURSOR = re.compile(r"[a-f0-9]{32}:[0-9]{1,8}\Z")


def replay(gateway, session, session_id, cursor, task_id):
    started = time.monotonic()
    while True:
        response = assistant_client.http_json(
            gateway
            + "/chat/sessions/"
            + session_id
            + "/events?cursor="
            + urllib.parse.quote(cursor, safe=":"),
            session,
        )
        common.require(
            isinstance(response, dict)
            and response.get("status") == "ok"
            and isinstance(response.get("events"), list)
            and response.get("has_more") is False,
            "E47 cursor replay has a gap, missing events, or an unfinished page",
        )
        if any(
            isinstance(event, dict)
            and event.get("kind") == "terminal"
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("task_id") == task_id
            for event in response["events"]
        ):
            break
        common.require(
            time.monotonic() - started < 120,
            "E47 accepted turn has no visible terminal outcome within 120 seconds",
        )
        time.sleep(1)
    journal, position = cursor.split(":")
    previous = int(position)
    for event in response["events"]:
        previous += 1
        common.require(
            isinstance(event, dict)
            and type(event.get("sequence")) is int
            and event["sequence"] == previous
            and event.get("cursor") == f"{journal}:{previous}"
            and isinstance(event.get("event_id"), str)
            and event["event_id"],
            "E47 replay has a missing, repeated, or reordered event",
        )
    common.require(
        response.get("cursor") == f"{journal}:{previous}"
        and type(response.get("latest_sequence")) is int
        and response["latest_sequence"] == previous,
        "E47 replay cursor or latest sequence has a gap after the accepted turn",
    )
    return response


def fault_proof(fault, receipt, outcome, events):
    if fault == "assistant_duplicate_delivery":
        common.require(
            receipt.get("delivery_kind") in {"notification", "result"}
            and type(receipt.get("delivery_attempts")) is int
            and 2 <= receipt["delivery_attempts"] <= 3
            and (
                receipt.get("delivery_kind") == "result"
                and receipt.get("delivery_id") == outcome.get("delivery_id")
                or receipt.get("delivery_kind") == "notification"
                and any(
                    event.get("kind") == "ag_ui"
                    and event.get("event_id") == receipt.get("delivery_id")
                    for event in events
                )
            ),
            "E47 duplicate delivery was not bounded to the committed result",
        )
        return {
            "delivery_kind": receipt["delivery_kind"],
            "delivery_attempts": receipt["delivery_attempts"],
        }
    if fault == "assistant_worker_loss":
        common.require(
            receipt.get("lost_before_commit") is True
            and receipt.get("old_lease_write_denied") is True
            and type(receipt.get("old_generation")) is int
            and receipt["old_generation"] >= 1
            and (
                type(receipt.get("replacement_generation")) is int
                and receipt["replacement_generation"] > receipt["old_generation"]
                or receipt.get("replacement_generation") is None
                and outcome.get("status") == "interrupted"
            ),
            "E47 worker loss lacks a fenced or visibly interrupted pre-commit outcome",
        )
        return {
            "old_lease_write_denied": True,
            "replacement_generation": receipt.get("replacement_generation"),
        }
    if fault == "assistant_queue_unavailable":
        common.require(
            type(receipt.get("unavailable_seconds")) in {int, float}
            and 0 < receipt["unavailable_seconds"] <= 30
            and receipt.get("consumer_resumed") is True,
            "E47 queue outage was not bounded or its consumer did not recover",
        )
        return {
            "unavailable_seconds": receipt["unavailable_seconds"],
            "consumer_resumed": True,
        }
    if fault == "assistant_supervisor_restart":
        common.require(
            receipt.get("restarted") is True
            and receipt.get("old_lease_write_denied") is True
            and type(receipt.get("old_generation")) is int
            and type(receipt.get("replacement_generation")) is int
            and receipt["replacement_generation"] > receipt["old_generation"] >= 1,
            "E47 supervisor restart lacks a fenced replacement",
        )
        return {
            "restarted": True,
            "replacement_generation": receipt["replacement_generation"],
        }
    return {"reconnected": True}


def execute(config, evidence):
    fault = config.get("fault", "none")
    common.require(fault in FAULTS, "E47 requires an explicit assistant fault")
    common.require(config.get("evaluation_id"), "E47 requires a run identity")
    target = config.get("fault_target")
    common.require(isinstance(target, dict), "E47 requires a run-owned session target")
    session_id = target.get("session_id")
    cursor = target.get("cursor")
    common.require(
        isinstance(session_id, str)
        and assistant_baseline.SESSION.fullmatch(session_id)
        and isinstance(cursor, str)
        and CURSOR.fullmatch(cursor)
        and isinstance(target.get("turn_digest"), str)
        and re.fullmatch(r"[a-f0-9]{64}", target["turn_digest"])
        and isinstance(target.get("task_id"), str)
        and target["task_id"]
        and target.get("evaluation_id") == config["evaluation_id"],
        "E47 target session or cursor is not bound to this run",
    )
    if fault in CONTROLLER_FAULTS:
        receipt = config.get("fault_receipt")
        common.require(
            isinstance(receipt, dict)
            and receipt.get("evaluation_id") == config["evaluation_id"]
            and receipt.get("fault") == fault
            and receipt.get("session_id") == session_id
            and isinstance(target.get("worker_id"), str)
            and target["worker_id"]
            and receipt.get("worker_id") == target.get("worker_id")
            and receipt.get("injected") is True,
            "E47 controller fault injection is unverified; no ordinary-user fallback",
        )
    label = target.get("user_label")
    fixture = (config.get("assistant_users") or {}).get(label)
    common.require(isinstance(fixture, dict), "E47 target owner has no fixture")
    session = assistant_baseline.session_for(config, fixture)
    gateway = config["gateway_url"].rstrip("/")
    snapshot = assistant_client.http_json(
        gateway + "/chat/sessions/" + session_id, session
    )
    assistant_baseline.owned(snapshot, fixture, session_id)
    mode = assistant_client.http_json(
        gateway + "/chat/sessions/" + session_id + "/mode", session
    )
    common.require(
        isinstance(mode, dict) and mode.get("mode") == "persistent",
        "E47 target is not a bound persistent session",
    )
    common.require(
        snapshot.get("truncated") is False
        and isinstance(snapshot.get("messages"), list)
        and sum(
            isinstance(message, dict)
            and message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and hashlib.sha256(message["content"].encode()).hexdigest()
            == target["turn_digest"]
            for message in snapshot["messages"]
        )
        == 1,
        "E47 accepted turn is absent from the owned persistent history",
    )
    if fault == "assistant_disconnect":
        with assistant_client.connect(config["websocket_url"], session):
            first = replay(gateway, session, session_id, cursor, target["task_id"])
        with assistant_client.connect(config["websocket_url"], session):
            second = replay(gateway, session, session_id, cursor, target["task_id"])
    else:
        first = replay(gateway, session, session_id, cursor, target["task_id"])
        second = replay(gateway, session, session_id, cursor, target["task_id"])
    common.require(
        first["events"] == second["events"] and first["cursor"] == second["cursor"],
        "E47 reconnect replay changed committed events",
    )
    terminal = [
        event
        for event in second["events"]
        if event.get("kind") == "terminal"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("task_id") == target["task_id"]
    ]
    common.require(
        len(terminal) == 1
        and len({event["event_id"] for event in second["events"]})
        == len(second["events"]),
        "E47 accepted turn has missing or duplicate committed events",
    )
    outcome = terminal[0]["payload"]
    common.require(
        outcome.get("session_id") == session_id
        and outcome.get("terminal_delivery") is True
        and isinstance(outcome.get("delivery_id"), str)
        and outcome["delivery_id"]
        and outcome.get("automatic_replay_permitted") is False
        and outcome.get("accounting_status") in {"not_used", "settled", "unresolved"}
        and (
            outcome.get("status") == "completed"
            and outcome["accounting_status"] in {"not_used", "settled"}
            or (
                outcome.get("status") == "interrupted"
                and type(outcome.get("retryable")) is bool
                and (
                    outcome["retryable"] is False
                    or outcome["accounting_status"] == "not_used"
                )
            )
        ),
        "E47 accepted turn lacks a committed or visible non-replayed interrupted outcome",
    )
    proof = (
        fault_proof(fault, receipt, outcome, second["events"])
        if fault in CONTROLLER_FAULTS
        else {"reconnected": True}
    )
    evidence.update(
        success=True,
        stage="replay_observed",
        detail={
            "fault": fault,
            "session_id": session_id,
            "events_replayed": len(second["events"]),
            "outcome": outcome["status"],
            "cursor": second["cursor"],
            "fault_proof": proof,
            "accounting_status": outcome["accounting_status"],
            "automatic_replay_permitted": False,
        },
    )
