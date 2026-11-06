"""E45 via ordinary-user admission and trusted, fixed sandbox execution."""

import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path

import assistant_baseline
import assistant_client
import assistant_isolation_clients
import assistant_isolation_evidence as oracle
import common


def blocked(evidence, capability):
    evidence.update(
        success=False,
        stage="blocked",
        detail={"status": "BLOCKED", "unavailable_capability": capability},
    )


def capability_name(error):
    match = re.search(r"\((chat_qualification_[a-z_]+)\)", str(error))
    return match.group(1) if match else "chat_qualification_interface_unavailable"


def request_body(config, run_id):
    return {
        "run_id": run_id,
        "evaluation_id": config["evaluation_id"],
        "manifest_digest": oracle.MANIFEST_DIGEST,
    }


def http(gateway, session, session_id, operation, body):
    return assistant_client.http_json(
        gateway + "/chat/sessions/" + session_id + "/qualification" + operation,
        session,
        method="POST",
        body=body,
    )


def await_result(gateway, session, session_id, body, *, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = http(gateway, session, session_id, "/result", body)
        common.require(isinstance(result, dict), "Qualification result is malformed")
        if result.get("sandbox_removed") is True:
            return result
        time.sleep(0.5)
    raise common.RemoteError("Qualification execution or observed teardown timed out")


def capabilities(gateway, config, sessions):
    readiness = {}
    for actor in oracle.ACTORS:
        readiness[actor] = assistant_client.http_json(
            gateway
            + "/chat/qualification?"
            + urllib.parse.urlencode({"evaluation_id": config["evaluation_id"]}),
            sessions[actor],
        )
        ready = readiness[actor]
        common.require(
            ready.get("protocol") == "e45-v1"
            and ready.get("actor") == actor
            and ready.get("evaluation_id") == config["evaluation_id"]
            and ready.get("manifest_digest") == oracle.MANIFEST_DIGEST,
            "Qualification capability binding mismatched",
        )
    common.require(
        len({ready["target"] for ready in readiness.values()}) == 1
        and len({ready["image_digest"] for ready in readiness.values()}) == 1,
        "Qualification target changed across fixtures",
    )
    return readiness


def execute(config, evidence):
    gateway = config["gateway_url"].rstrip("/")
    common.require(
        config.get("evaluation_id") and config.get("work_dir"),
        "Qualification needs a durable run directory",
    )
    try:
        sessions = assistant_isolation_clients.fixture_sessions(config)
        assistant_isolation_clients.verify_identities(config, sessions)
    except (common.RemoteError, assistant_client.ClientError):
        blocked(evidence, "chat_qualification_ordinary_fixture_unavailable")
        return
    directory = Path(config["work_dir"])
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / (
        "assistant-isolation-"
        + hashlib.sha256(config["evaluation_id"].encode()).hexdigest()
        + ".json"
    )
    prior = path.exists()
    if prior:
        record = json.loads(path.read_text())
        common.require(
            record.get("evaluation_id") == config["evaluation_id"],
            "Qualification journal changed evaluation",
        )
    else:
        common.require(
            not config.get("prior_dispatch"),
            "Qualification prior dispatch lacks a recovery journal",
        )
        try:
            readiness = capabilities(gateway, config, sessions)
        except assistant_client.ClientError as error:
            blocked(evidence, capability_name(error))
            return
        record = {
            "evaluation_id": config["evaluation_id"],
            "target": readiness["a1"]["target"],
            "started_at": int(time.time()),
            "requests": {},
            "results": {},
            "cleanup": {},
            "created": {},
        }
        assistant_baseline.persist(path, record)
    try:
        common.require(
            not prior, "Qualification requests cannot be replayed; cleanup only"
        )
        for actor in oracle.ACTORS:
            session = sessions[actor]
            request_id = hashlib.sha256(
                f"{config['evaluation_id']}:{actor}".encode()
            ).hexdigest()
            with assistant_client.connect(config["websocket_url"], session) as client:
                client.send({"action": "create-session", "request_id": request_id})
                created = client.receive()
                common.require(
                    created.get("request_id") == request_id
                    and isinstance(created.get("session_id"), str)
                    and assistant_baseline.SESSION.fullmatch(created["session_id"])
                    and not created.get("error"),
                    "Qualification session creation failed",
                )
                session_id = created["session_id"]
                record["created"][actor] = {"session_id": session_id}
                assistant_baseline.persist(path, record)
                shown = assistant_client.http_json(
                    gateway + "/chat/sessions/" + session_id, session
                )
                assistant_baseline.owned(
                    shown, config["assistant_users"][actor], session_id
                )
                client.send(
                    {
                        "action": "message",
                        "session_id": session_id,
                        "persona": assistant_baseline.PERSONA,
                        "text": "Run the server-admitted fixed qualification request. No model response is requested.",
                    }
                )
                run_id = None
                for _ in range(32):
                    event = client.receive()
                    common.require(
                        isinstance(event, dict) and not event.get("error"),
                        "Qualification turn was refused",
                    )
                    if isinstance(
                        event.get("task_id"), str
                    ) and assistant_baseline.SESSION.fullmatch(event["task_id"]):
                        run_id = event["task_id"]
                        break
                common.require(run_id, "Qualification turn has no server run identity")
                record["created"][actor]["run_id"] = run_id
                assistant_baseline.persist(path, record)
                body = request_body(config, run_id)
                deadline = time.monotonic() + 20
                while True:
                    try:
                        binding = http(gateway, session, session_id, "", body)
                        break
                    except assistant_client.ClientError as error:
                        if time.monotonic() >= deadline or not any(
                            f"returned {status}" in str(error)
                            for status in (404, 409, 503)
                        ):
                            blocked(evidence, capability_name(error))
                            return
                        time.sleep(0.25)
                common.require(
                    binding.get("session_id") == session_id
                    and binding.get("run_id") == run_id
                    and binding.get("actor") == actor
                    and binding.get("image_digest") == readiness[actor]["image_digest"],
                    "Qualification admission returned a different execution",
                )
                record["requests"][actor] = binding
                assistant_baseline.persist(path, record)
                result = await_result(gateway, session, session_id, body)
                record["results"][actor] = result
                assistant_baseline.persist(path, record)
                receipt = result.get("receipt") or {}
                failed = [
                    row.get("probe")
                    for row in receipt.get("observations", [])
                    if row.get("executed") is not True
                    or row.get("outcome") in {"error", "timeout"}
                ]
                common.require(
                    result.get("state") == "finished"
                    and receipt.get("status") == "completed",
                    f"Qualification execution incomplete/{actor}: {receipt.get('failure')}; probes={failed}",
                )
                try:
                    oracle.check_execution(actor, receipt)
                except (ValueError, KeyError, TypeError, AttributeError) as error:
                    raise common.RemoteError(
                        f"Invalid qualification evidence: {error}"
                    ) from None
    finally:
        cleanup_errors = []
        for actor, created in record["created"].items():
            session_id = created["session_id"]
            try:
                if created.get("run_id"):
                    body = request_body(config, created["run_id"])
                    if actor not in record["requests"]:
                        try:
                            current = http(
                                gateway, sessions[actor], session_id, "/result", body
                            )
                            record["requests"][actor] = current["binding"]
                        except assistant_client.ClientError as error:
                            if (
                                capability_name(error)
                                != "chat_qualification_request_unavailable"
                            ):
                                raise
                    if actor in record["requests"]:
                        http(gateway, sessions[actor], session_id, "/cancel", body)
            except (
                common.RemoteError,
                assistant_client.ClientError,
                ValueError,
                KeyError,
            ) as error:
                cleanup_errors.append(f"{actor} cancellation: {error}")
            try:
                assistant_client.http_json(
                    gateway + "/chat/sessions/" + session_id + "/end",
                    sessions[actor],
                    method="POST",
                    body={},
                )
                if actor in record["requests"]:
                    await_result(
                        gateway, sessions[actor], session_id, body, timeout=120
                    )
                    record["cleanup"][actor] = http(
                        gateway, sessions[actor], session_id, "/cleanup", body
                    )
                    common.require(
                        record["cleanup"][actor].get("cleanup_complete") is True
                        and record["cleanup"][actor].get("nonce")
                        == record["requests"][actor]["nonce"],
                        f"Unverified qualification cleanup/{actor}",
                    )
            except (
                common.RemoteError,
                assistant_client.ClientError,
                ValueError,
                KeyError,
            ) as error:
                cleanup_errors.append(f"{actor}: {error}")
        record["finished_at"] = int(time.time())
        assistant_baseline.persist(path, record)
        evidence["qualification"] = oracle.wire(
            {key: value for key, value in record.items() if key != "created"}
        )
        evidence["resources"] = [
            ["assistant_worker", created["run_id"]]
            for created in record["created"].values()
            if created.get("run_id")
        ]
        evidence["removed"] = [
            ["assistant_worker", record["requests"][actor]["run_id"]]
            for actor, result in record["cleanup"].items()
            if result.get("cleanup_complete") is True
            and result.get("nonce") == record["requests"][actor]["nonce"]
        ]
        if cleanup_errors:
            evidence.update(
                success=False, stage="failed", detail={"cleanup_errors": cleanup_errors}
            )
    if evidence.get("detail", {}).get("cleanup_errors"):
        return
    try:
        verified = oracle.verify(
            evidence["qualification"],
            config["assistant_users"],
            config["evaluation_id"],
            now=int(time.time()),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise common.RemoteError(f"Invalid qualification evidence: {error}") from None
    evidence.update(
        success=True,
        detail={
            "trusted_receipts_verified": True,
            "image_digest": verified["image_digest"],
            "probes": verified["rows"],
        },
    )
