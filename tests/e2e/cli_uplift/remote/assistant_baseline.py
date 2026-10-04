"""E50: existing WebSocket protocol, not future durable/replay qualification.

Wire references: ingest.handle_create_session, WebSocketRouter.route and
GET /chat/sessions/{session_id}. A private journal prevents an interrupted
non-idempotent WebSocket message from being automatically submitted again.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

import assistant_client
import common

PROTOCOL = "webchat-response-v1"
SESSION = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
PROMPT = "Reply briefly with the word ready. Do not use tools or take any actions."


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def persist(path, record):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def session_for(config, fixture):
    # Resolution is privileged fixture setup; only the resulting ordinary tokens
    # cross into the sandbox, after it confirms confinement is active.
    value = common.fixture_secret(
        {**config, "credential_secret": fixture["fixture_name"]},
        common.clean_env(config),
        "ordinary_session",
    )
    common.require(
        isinstance(value, dict)
        and isinstance(value.get("expires_at"), (int, float))
        and not isinstance(value.get("expires_at"), bool)
        and value["expires_at"] > time.time() + 60,
        "Assistant ordinary_session fixture is missing or expired; renew the fixture",
    )
    session = assistant_client.UserSession(value)
    session.token()
    session.token("id_token")
    return session


def owned(document, fixture, session_id=None):
    common.require(
        isinstance(document, dict)
        and document.get("user_id") == fixture["canonical_user_id"]
        and document.get("tenant_id") == fixture["tenant_id"]
        and (session_id is None or document.get("session_id") == session_id),
        "Assistant server identity or session does not match the ordinary fixture",
    )


def collect(client):
    """Accept only one correlated, complete response using the current envelope."""
    task_id = None
    chunks = []
    chunk_total = None
    deadline = time.monotonic() + 120
    for _ in range(128):
        common.require(time.monotonic() < deadline, "Assistant response timed out")
        event = client.receive()
        common.require(isinstance(event, dict), "Assistant response must be an object")
        common.require(
            not event.get("error")
            and event.get("status") != "failed"
            and event.get("type") != "session_invalid",
            "Assistant server rejected the baseline turn",
        )
        identifier = event.get("task_id")
        common.require(
            isinstance(identifier, str) and SESSION.fullmatch(identifier),
            "Assistant response lacks a valid task identity",
        )
        task_id = task_id or identifier
        common.require(
            identifier == task_id, "Assistant response crossed task identities"
        )
        if (
            event.get("type") in {"progress", "notification", "ag_ui"}
            or event.get("status") == "notification"
        ):
            continue
        common.require(
            event.get("type") == "response" and event.get("status") == "completed",
            "Assistant response lacks a completed outcome",
        )
        content = event.get("content")
        common.require(
            isinstance(content, str) and content, "Assistant final response is empty"
        )
        if any(key in event for key in ("chunk_index", "chunk_total")):
            total, index = event.get("chunk_total"), event.get("chunk_index")
            common.require(
                type(total) is int
                and 1 <= total <= 128
                and type(index) is int
                and index == len(chunks) + 1
                and index <= total
                and (chunk_total is None or total == chunk_total),
                "Assistant response chunks are missing, repeated or out of order",
            )
            chunk_total = total
        else:
            common.require(not chunks, "Assistant response changed chunk protocol")
            chunk_total = 1
        chunks.append(content)
        common.require(
            sum(len(chunk) for chunk in chunks) <= 100000,
            "Assistant response exceeds evidence limit",
        )
        if len(chunks) == chunk_total:
            return task_id, "".join(chunks), len(chunks)
    raise common.RemoteError("Assistant response did not complete within event limit")


def execute(config, evidence):
    fixture = (config.get("assistant_users") or {}).get("a1")
    common.require(
        isinstance(fixture, dict)
        and all(
            isinstance(fixture.get(key), str) and fixture[key]
            for key in (
                "fixture_name",
                "canonical_user_id",
                "tenant_id",
                "login_user_id",
            )
        ),
        "Assistant baseline requires an A1 ordinary-user fixture",
    )
    common.require(
        config.get("evaluation_id") and config.get("work_dir"),
        "Assistant baseline requires a durable run directory",
    )
    gateway = config["gateway_url"].rstrip("/")
    websocket = config["websocket_url"]
    key = digest(
        json.dumps(
            [config["evaluation_id"], gateway, websocket, fixture], sort_keys=True
        )
    )
    path = Path(config["work_dir"]) / ("assistant-baseline-" + key + ".json")
    record = {
        "protocol": PROTOCOL,
        "phase": "prepared",
        "evaluation_id": config["evaluation_id"],
    }
    if path.exists():
        record = json.loads(path.read_text())
        common.require(
            record.get("protocol") == PROTOCOL
            and record.get("evaluation_id") == config["evaluation_id"],
            "Assistant recovery journal identity changed",
        )
        if record.get("phase") == "completed":
            evidence.update(success=True, detail={**record, "resumed": True})
            return
        evidence["detail"] = record
        raise common.RemoteError(
            "Assistant prior attempt is incomplete; retained evidence requires reconciliation, not automatic resubmission"
        )
    common.require(
        not config.get("prior_dispatch"),
        "Assistant prior dispatch has no local journal; reconcile retained caller intent before any new submission",
    )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = session_for(config, fixture)
    principal = assistant_client.http_json(gateway + "/auth/me", session)
    common.require(
        isinstance(principal, dict)
        and principal.get("account_type") == "human"
        and principal.get("is_admin") is False
        and principal.get("user_id")
        in {fixture["login_user_id"], fixture["canonical_user_id"]}
        and principal.get("org_id") == fixture["tenant_id"],
        "Assistant baseline requires the expected ordinary non-admin identity",
    )
    identity = assistant_client.http_json(gateway + "/chat/capabilities", session)
    owned(identity, fixture)
    common.require(
        identity.get("enabled") is True and identity.get("history_configured") is True,
        "Assistant chat or owned history is unavailable",
    )
    record.update(
        user_id=identity["user_id"], tenant_id=identity["tenant_id"], request_id=key
    )
    evidence["detail"] = record
    # Persist before either mutation. Neither create-session nor message currently
    # supplies idempotent replay; a lost reply must never cause a second turn.
    record["phase"] = "session_create_attempted"
    persist(path, record)
    try:
        with assistant_client.connect(websocket, session) as client:
            client.send({"action": "create-session", "request_id": key})
            created = client.receive()
            common.require(
                isinstance(created, dict)
                and created.get("request_id") == key
                and not created.get("error")
                and isinstance(created.get("session_id"), str)
                and SESSION.fullmatch(created["session_id"]),
                "Assistant did not return a correlated server-issued session",
            )
            sid = created["session_id"]
            record.update(session_id=sid, phase="session_created")
            persist(path, record)
            shown = assistant_client.http_json(
                gateway + "/chat/sessions/" + sid, session
            )
            owned(shown, fixture, sid)
            record["phase"] = "submit_attempted"
            persist(path, record)
            client.send({"action": "message", "session_id": sid, "text": PROMPT})
            task, answer, count = collect(client)
            record.update(
                task_id=task,
                response_sha256=digest(answer),
                response_chunks=count,
                phase="response_received",
            )
            persist(path, record)
            shown = assistant_client.http_json(
                gateway + "/chat/sessions/" + sid, session
            )
            owned(shown, fixture, sid)
            messages = shown.get("messages") or []
            common.require(
                any(
                    row.get("role") == "user" and row.get("content") == PROMPT
                    for row in messages
                )
                and any(
                    row.get("role") == "assistant" and row.get("content") == answer
                    for row in messages
                ),
                "Assistant owned history does not contain the submitted turn and observed answer",
            )
            record.update(
                phase="completed",
                history_verified=True,
                qualification="Supported chat baseline only; durable ACK, replay, tool and sandbox qualification remain pending",
                retention="Server-owned conversation history follows existing retention; no delete API is assumed",
            )
            persist(path, record)
            evidence["success"] = True
    finally:
        # No plaintext prompt/answer or session tokens are journaled or emitted.
        evidence["detail"] = record
