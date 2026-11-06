"""E46: run-owned persistent WebSocket sessions with observable cleanup."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import assistant_baseline
import assistant_client
import common

PERSONA = assistant_baseline.PERSONA


def controller_revoke(_config, _fixture, _session_id):
    raise common.RemoteError(
        "E46 membership revocation requires a controller-owned fixture and "
        "verified scoped permission; ordinary clients cannot revoke membership"
    )


def controller_ready(_config, _fixture):
    return False


def controller_observe(_config, _fixture, _session_id):
    raise common.RemoteError(
        "E46 sandbox absence requires a run-owned controller observation; TTL is not proof"
    )


def observed(config, fixture, session_id, proof):
    common.require(
        isinstance(proof, dict)
        and proof.get("evaluation_id") == config["evaluation_id"]
        and proof.get("session_id") == session_id
        and proof.get("observed_absent") is True
        and proof.get("observation_source") == "worker_teardown",
        "E46 sandbox destruction needs observed worker teardown, not TTL-only cleanup",
    )


def state(gateway, session, session_id):
    result = assistant_client.http_json(
        gateway + "/chat/sessions/" + session_id + "/mode", session
    )
    common.require(isinstance(result, dict), "E46 session mode has no state")
    return result


def destroyed(
    config, fixture, gateway, session, session_id, *, mode="persistent", started_at=None
):
    started = time.monotonic() if started_at is None else started_at
    while time.monotonic() - started <= 120:
        current = state(gateway, session, session_id)
        if (mode == "persistent" and current.get("health") == "ended") or (
            mode == "ephemeral"
            and current.get("mode") == "ephemeral"
            and current.get("health") == "idle"
        ):
            observed(
                config,
                fixture,
                session_id,
                controller_observe(config, fixture, session_id),
            )
            elapsed = time.monotonic() - started
            common.require(
                elapsed <= 120,
                "E46 sandbox cleanup exceeded the two-minute reconciliation target",
            )
            return round(elapsed, 3)
        common.require(
            current.get("health") != "cleanup_delayed",
            "E46 sandbox cleanup exceeded the two-minute reconciliation target",
        )
        time.sleep(1)
    raise common.RemoteError(
        "E46 sandbox destruction was not observed within 120 seconds"
    )


def denied(gateway, websocket, session, session_id):
    try:
        assistant_client.http_json(gateway + "/chat/sessions/" + session_id, session)
    except assistant_client.ClientError as error:
        common.require(
            "returned 404" in str(error),
            "E46 foreign session access failed for a reason other than denial",
        )
    else:
        raise common.RemoteError("E46 foreign session was readable")
    turn_denied(websocket, session, session_id)


def turn_denied(websocket, session, session_id):
    with assistant_client.connect(websocket, session) as client:
        client.send(
            {
                "action": "message",
                "session_id": session_id,
                "text": "Prove this session is denied.",
                "persona": PERSONA,
            }
        )
        response = client.receive()
        common.require(
            isinstance(response, dict) and response.get("type") == "session_invalid",
            "E46 ended, revoked, or foreign WebSocket turn was not denied",
        )


def turn(gateway, websocket, fixture, session, session_id, marker, *, followup):
    prompt = (
        "Which marker did I ask you to remember? Reply with it exactly."
        if followup
        else f"Remember this marker for the next turn: {marker}. Reply with it exactly."
    )
    with assistant_client.connect(websocket, session) as client:
        client.send(
            {
                "action": "message",
                "session_id": session_id,
                "text": prompt,
                "persona": PERSONA,
            }
        )
        task_id, answer, _chunks = assistant_baseline.collect(client, session_id)
    common.require(
        marker in answer,
        "E46 bound worker did not retain same-session context",
    )
    snapshot = assistant_client.http_json(
        gateway + "/chat/sessions/" + session_id, session
    )
    assistant_baseline.owned(snapshot, fixture, session_id)
    messages = snapshot.get("messages")
    common.require(
        snapshot.get("truncated") is False
        and isinstance(messages, list)
        and len([row for row in messages if row.get("role") == "user"])
        == (2 if followup else 1)
        and len([row for row in messages if row.get("role") == "assistant"])
        == (2 if followup else 1)
        and sum(
            row.get("role") == "user" and row.get("content") == prompt
            for row in messages
        )
        == 1
        and sum(
            row.get("role") == "assistant"
            and row.get("task_id") == task_id
            and row.get("content") == answer
            for row in messages
        )
        == 1,
        "E46 accepted turn is missing or duplicated in committed history",
    )
    return task_id


def execute(config, evidence):
    users = config.get("assistant_users") or {}
    common.require(
        all(label in users for label in ("a1", "a2", "b1")),
        "E46 requires three ordinary-user fixtures",
    )
    common.require(
        config.get("evaluation_id") and config.get("work_dir"),
        "E46 requires durable run identity and directory",
    )
    gateway = config["gateway_url"].rstrip("/")
    websocket = config["websocket_url"]
    key = hashlib.sha256(config["evaluation_id"].encode()).hexdigest()
    journal = Path(config["work_dir"]) / ("assistant-sessions-" + key + ".json")
    if journal.exists():
        prior = json.loads(journal.read_text())
        common.require(
            prior.get("evaluation_id") == config["evaluation_id"],
            "E46 recovery journal changed identity",
        )
        if prior.get("phase") == "completed":
            evidence.update(success=True, detail={**prior["detail"], "resumed": True})
            return
        evidence["detail"] = prior
        raise common.RemoteError(
            "E46 prior attempt needs reconciliation; never replay uncertain turns"
        )
    common.require(
        not config.get("prior_dispatch"),
        "E46 prior dispatch has no local journal; reconcile before resubmission",
    )
    owned_membership = users["a2"]
    common.require(
        isinstance(owned_membership, dict)
        and owned_membership.get("membership_run_owned") is True
        and isinstance(owned_membership.get("membership_id"), str)
        and owned_membership["membership_id"],
        "E46 requires a run-owned membership for the revocation case",
    )
    common.require(
        controller_ready(config, owned_membership),
        "E46 controller revocation and sandbox proof are unavailable; no sessions created",
    )
    journal.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    record = {
        "evaluation_id": config["evaluation_id"],
        "phase": "prepared",
        "sessions": {},
    }
    assistant_baseline.persist(journal, record)
    sessions = {}
    for label in ("a1", "a2", "b1"):
        fixture = users[label]
        common.require(
            isinstance(fixture, dict)
            and all(
                isinstance(fixture.get(field), str) and fixture[field]
                for field in (
                    "fixture_name",
                    "canonical_user_id",
                    "tenant_id",
                    "login_user_id",
                )
            ),
            "E46 ordinary-user fixture is incomplete",
        )
        session = assistant_baseline.session_for(config, fixture)
        principal = assistant_client.http_json(gateway + "/auth/me", session)
        common.require(
            isinstance(principal, dict)
            and principal.get("account_type") == "human"
            and principal.get("is_admin") is False
            and principal.get("org_id") == fixture["tenant_id"]
            and principal.get("user_id")
            in {fixture["login_user_id"], fixture["canonical_user_id"]},
            "E46 requires the expected ordinary-user identity",
        )
        sessions[label] = (fixture, session)

    created = {}
    for slot in (1, 2):
        for label, (fixture, session) in sessions.items():
            request_id = hashlib.sha256(
                json.dumps([config["evaluation_id"], label, slot]).encode()
            ).hexdigest()[:32]
            record["phase"] = "create_attempted"
            assistant_baseline.persist(journal, record)
            with assistant_client.connect(websocket, session) as client:
                client.send({"action": "create-session", "request_id": request_id})
                response = client.receive()
            common.require(
                isinstance(response, dict)
                and response.get("request_id") == request_id
                and isinstance(response.get("session_id"), str)
                and assistant_baseline.SESSION.fullmatch(response["session_id"]),
                "E46 server-issued session was not correlated",
            )
            session_id = response["session_id"]
            common.require(
                session_id not in created.values(),
                "E46 session identity was reused across users or slots",
            )
            created[label, slot] = session_id
            record["sessions"][f"{label}:{slot}"] = session_id
            record["phase"] = "session_created"
            assistant_baseline.persist(journal, record)
            snapshot = assistant_client.http_json(
                gateway + "/chat/sessions/" + session_id, session
            )
            assistant_baseline.owned(snapshot, fixture, session_id)
            chosen = assistant_client.http_json(
                gateway + "/chat/sessions/" + session_id + "/mode",
                session,
                method="PUT",
                body={"mode": "persistent"},
            )
            common.require(
                chosen.get("mode") == "persistent",
                "E46 persistent mode was not selected",
            )

    for label in ("a2", "b1"):
        denied(gateway, websocket, sessions["a1"][1], created[label, 1])
    markers = {
        pair: hashlib.sha256(f"{config['evaluation_id']}:{pair}".encode()).hexdigest()[
            :12
        ]
        for pair in created
    }
    results = {}
    for followup in (False, True):
        record["phase"] = "turn_attempted"
        assistant_baseline.persist(journal, record)
        with ThreadPoolExecutor(max_workers=6) as executor:
            tasks = {
                pair: executor.submit(
                    turn,
                    gateway,
                    websocket,
                    *sessions[pair[0]],
                    created[pair],
                    markers[pair],
                    followup=followup,
                )
                for pair in created
            }
            for pair, future in tasks.items():
                task_id = future.result()
                common.require(
                    task_id not in results.values(),
                    "E46 worker task identity crossed sessions",
                )
                results[pair, followup] = task_id

    timings = {}
    for pair, session_id in created.items():
        label, slot = pair
        fixture, session = sessions[label]
        if pair == ("a1", 2):
            started = time.monotonic()
            changed = assistant_client.http_json(
                gateway + "/chat/sessions/" + session_id + "/mode",
                session,
                method="PUT",
                body={"mode": "ephemeral"},
            )
            common.require(
                changed.get("mode") in {"persistent", "ephemeral"},
                "E46 mode switch was refused",
            )
            timings[f"{label}:{slot}"] = destroyed(
                config,
                fixture,
                gateway,
                session,
                session_id,
                mode="ephemeral",
                started_at=started,
            )
        elif pair == ("a2", 1):
            start = time.monotonic()
            idle = state(gateway, session, session_id).get("idle_timeout_seconds")
            common.require(
                type(idle) is int and idle == 300,
                "E46 idle expiry duration is not the supported five minutes",
            )
            time.sleep(idle)
            timings[f"{label}:{slot}"] = destroyed(
                config, fixture, gateway, session, session_id
            )
            common.require(
                time.monotonic() - start >= idle,
                "E46 idle expiry occurred without elapsed idle time",
            )
            turn_denied(websocket, session, session_id)
        elif pair == ("a2", 2):
            common.require(
                fixture.get("membership_run_owned") is True,
                "E46 cannot revoke a fixture membership the run does not own",
            )
            receipt = controller_revoke(config, fixture, session_id)
            common.require(
                isinstance(receipt, dict)
                and receipt.get("evaluation_id") == config["evaluation_id"]
                and receipt.get("session_id") == session_id
                and receipt.get("membership_id") == fixture.get("membership_id")
                and receipt.get("revoked") is True
                and receipt.get("observed_absent") is True
                and receipt.get("observation_source") == "worker_teardown"
                and type(receipt.get("cleanup_seconds")) in {int, float}
                and 0 <= receipt["cleanup_seconds"] <= 120,
                "E46 controller did not prove membership revocation and sandbox absence",
            )
            observed(config, fixture, session_id, receipt)
            denied(gateway, websocket, session, session_id)
            timings[f"{label}:{slot}"] = receipt["cleanup_seconds"]
        else:
            started = time.monotonic()
            ended = assistant_client.http_json(
                gateway + "/chat/sessions/" + session_id + "/end",
                session,
                method="POST",
            )
            common.require(
                ended.get("health") in {"ending", "ended"},
                "E46 explicit end was refused",
            )
            timings[f"{label}:{slot}"] = destroyed(
                config, fixture, gateway, session, session_id, started_at=started
            )
            turn_denied(websocket, session, session_id)

    detail = {
        "lifecycle_verified": True,
        "sessions_created": len(created),
        "concurrent_sessions": 6,
        "worker_turns": len(results),
        "cleanup_seconds": timings,
        "phase": "completed",
    }
    record.update(phase="completed", detail=detail)
    assistant_baseline.persist(journal, record)
    evidence.update(success=True, stage="completed", detail=detail)
