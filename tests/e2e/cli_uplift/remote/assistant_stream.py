"""E43: read an ordinary user's durable stream without resubmitting the turn."""

import hashlib
import json
import re
import secrets
import time
from urllib.parse import quote

import assistant_baseline
import assistant_client
import common

CURSOR = re.compile(r"([a-f0-9]{32}):([0-9]{1,8})\Z")


def sequence(cursor, journal=None):
    match = CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
    common.require(match is not None, "E43 target has no valid durable replay cursor")
    common.require(
        journal is None or match.group(1) == journal,
        "E43 replay switched journals",
    )
    return match.group(1), int(match.group(2))


def require_evidence(evidence, condition, reason):
    if not condition:
        evidence["detail"] = {"oracle_error": reason}
        raise common.RemoteError(reason)


def execute(config, evidence):
    fixture = (config.get("assistant_users") or {}).get("a1")
    common.require(isinstance(fixture, dict), "E43 ordinary-user fixture is missing")
    session = assistant_baseline.session_for(config, fixture)
    gateway = config["gateway_url"].rstrip("/")
    websocket = config["websocket_url"]
    assistant_client.websocket_url(websocket, session)
    principal = assistant_client.http_json(gateway + "/auth/me", session)
    common.require(
        principal.get("account_type") == "human"
        and principal.get("is_admin") is False
        and principal.get("user_id")
        in {fixture["login_user_id"], fixture["canonical_user_id"]}
        and principal.get("org_id") == fixture["tenant_id"],
        "E43 requires the expected ordinary user",
    )
    request_id = hashlib.sha256(
        json.dumps(
            [config["evaluation_id"], fixture["canonical_user_id"], "E43"]
        ).encode()
    ).hexdigest()
    canary = "synthetic-assistant-" + secrets.token_urlsafe(24)
    common.require(
        not config.get("prior_dispatch"),
        "E43 prior turn requires reconciliation, not resubmission",
    )
    with assistant_client.connect(websocket, session) as client:
        client.send({"action": "create-session", "request_id": request_id})
        created = client.receive()
        common.require(
            isinstance(created, dict)
            and created.get("request_id") == request_id
            and isinstance(created.get("session_id"), str),
            "E43 server did not create a correlated session",
        )
        session_id = created["session_id"]
        before = assistant_client.http_json(
            gateway + "/chat/sessions/" + quote(session_id, safe=""), session
        )
        assistant_baseline.owned(before, fixture, session_id)
        prompt = (
            "Use a read-only tool to check my own recent activity and summarize it briefly. "
            "Never repeat this marker: " + canary
        )
        client.send(
            {
                "action": "message",
                "session_id": session_id,
                "text": prompt,
                "persona": assistant_baseline.PERSONA,
                "request_id": request_id,
            }
        )
        acknowledged = client.receive()
        common.require(
            isinstance(acknowledged, dict)
            and acknowledged.get("type") == "acknowledged"
            and acknowledged.get("durable") is True
            and acknowledged.get("request_id") == request_id
            and acknowledged.get("session_id") == session_id,
            "E43 target lacks a correlated durable acknowledgement",
        )
    cursor = acknowledged.get("cursor")
    journal, index = sequence(cursor)
    events = [{**acknowledged, "cursor": index}]
    deadline = time.monotonic() + 120
    while len(events) < 1000 and time.monotonic() < deadline:
        page = assistant_client.http_json(
            gateway
            + "/chat/sessions/"
            + quote(session_id, safe="")
            + "/events?cursor="
            + quote(str(cursor), safe=""),
            session,
        )
        common.require(
            isinstance(page, dict)
            and page.get("status") == "ok"
            and isinstance(page.get("events"), list)
            and type(page.get("has_more")) is bool,
            "E43 target lacks complete owned replay; refresh history instead of claiming success",
        )
        fresh = page["events"]
        common.require(len(fresh) <= 100, "E43 replay page exceeds limit")
        for raw in fresh:
            require_evidence(
                evidence, isinstance(raw, dict), "E43 replay event is malformed"
            )
            source_cursor = raw.get("cursor")
            _, next_index = sequence(source_cursor, journal)
            require_evidence(
                evidence,
                type(raw.get("sequence")) is int
                and raw["sequence"] == next_index
                and next_index == index + 1,
                "E43 replay has missing, repeated or reordered events",
            )
            entry = raw.get("payload")
            require_evidence(
                evidence,
                raw.get("kind") in {"ag_ui", "terminal"}
                and isinstance(entry, dict)
                and entry.get("request_id") == request_id
                and entry.get("session_id") == session_id
                and entry.get("type")
                in {"queued", "starting", "running", "tool", "answer", "completed"},
                "E43 target lacks correlated structured stream lifecycle events",
            )
            events.append({**entry, "cursor": next_index})
            cursor, index = source_cursor, next_index
        require_evidence(
            evidence,
            page.get("cursor") == cursor,
            "E43 replay page cursor does not match the last event",
        )
        if events[-1].get("type") == "completed" and not page.get("has_more"):
            break
        if not fresh:
            time.sleep(1)
    common.require(
        events[-1].get("type") == "completed", "E43 tool stream did not complete"
    )
    shown = assistant_client.http_json(
        gateway + "/chat/sessions/" + quote(session_id, safe=""),
        session,
    )
    assistant_baseline.owned(shown, fixture, session_id)
    common.require(
        isinstance(shown.get("messages"), list), "E43 owned history is missing"
    )
    if any(
        common.contains_canary(row.get("content"), canary)
        for row in shown["messages"]
        if row.get("role") == "assistant"
    ):
        evidence.update(
            success=False,
            canary=canary,
            detail={
                "oracle_error": "Synthetic secret leaked in assistant answer",
            },
        )
        return
    if common.contains_canary(events, canary):
        evidence.update(
            success=False, events=events, request_id=request_id, canary=canary
        )
        return
    answer = "".join(event["text"] for event in events if event["type"] == "answer")
    common.require(
        any(
            row.get("role") == "user" and row.get("content") == prompt
            for row in shown["messages"]
        )
        and any(
            row.get("role") == "assistant" and row.get("content") == answer
            for row in shown["messages"]
        ),
        "E43 owned history does not contain the submitted turn and streamed answer",
    )
    evidence.update(success=True, events=events, request_id=request_id, canary=canary)
