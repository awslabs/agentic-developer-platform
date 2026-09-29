"""Bounded authenticated SSE transport for authored implementation explanations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import Counter
from datetime import UTC, datetime

import httpx
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from src.activity.control_service import ControlError, validate_control_destination
from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.human_control import require_canonical_protected_human_owner

MAX_FRAME_BYTES = 16 * 1024
MAX_CONNECTIONS = 64
MAX_RUN_CONNECTIONS = 4
_connections: Counter = Counter()


class BoundedStreamingResponse(StreamingResponse):
    """A stalled browser cannot hold authorization or an upstream slot forever."""

    def __init__(self, *args, cleanup, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup = cleanup

    async def stream_response(self, send):
        async def bounded_send(message):
            await asyncio.wait_for(send(message), timeout=5)

        try:
            await super().stream_response(bounded_send)
        finally:
            try:
                await self.body_iterator.aclose()
            finally:
                await self.cleanup()


def frame(kind: str, data: dict, cursor: str | None = None) -> bytes:
    prefix = f"id: {cursor}\n" if cursor else ""
    return (prefix + f"event: {kind}\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode()


async def open_explanation_stream(control, run_id: str, *, session, reauthorize, cursor: str | None = None):
    """Open before returning HTTP 200; periodically recheck current authority.

    reauthorize reads the current human membership in a fresh database session.
    No browser-provided destination or worker credential is accepted.
    """
    config = os.environ if control._env is None else control._env
    target = await run_in_threadpool(control.resolve_target, run_id, user_id=session.user_id, tenant_id=session.tenant_id)
    if config.get("FEATURE_AGENT_EXPLANATIONS_ENABLED") != "true":
        raise ControlError(503, "live explanations are disabled")
    if cursor is not None and (len(cursor) > 256 or any(c in cursor for c in "\r\n")):
        raise ControlError(400, "invalid event cursor")
    reason = control.unavailable_reason(target)
    if reason:
        raise ControlError(409, reason)
    validate_control_destination(target.address, target.port, env=control._env)
    if control._authority_store is None:
        if config.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true" or not config.get("AGENT_AUTHORITY_TABLE"):
            raise ControlError(503, "live explanation authorization is unavailable")
        import boto3

        control._authority_store = BootstrapStore(
            table_name=config["AGENT_AUTHORITY_TABLE"], dynamodb_client=boto3.client("dynamodb", region_name=config.get("AWS_REGION", "us-east-1"))
        )

    async def check():
        if session.expires_at <= datetime.now(UTC) or config.get("FEATURE_AGENT_EXPLANATIONS_ENABLED") != "true":
            raise ControlError(404, "run not found")
        await reauthorize()
        current = await run_in_threadpool(control.resolve_target, run_id, user_id=session.user_id, tenant_id=session.tenant_id)
        if current.is_terminal:
            return False
        if current != target or control.unavailable_reason(current):
            raise ControlError(409, "run registration changed")
        await require_canonical_protected_human_owner(
            control._authority_store,
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            run_id=run_id,
            generation=target.generation,
            now=control._now(),
        )
        return True

    try:
        if not await asyncio.wait_for(check(), timeout=5):
            raise ControlError(409, "run has reached a terminal state")
    except ControlError:
        raise
    except Exception as exc:
        raise ControlError(404, "run not found") from exc
    key = (session.tenant_id, run_id)
    if sum(_connections.values()) >= MAX_CONNECTIONS or _connections[key] >= MAX_RUN_CONNECTIONS:
        raise ControlError(429, "live explanation connection limit")
    _connections[key] += 1
    owned_client = control._http_client is None
    client = control._http_client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
    response = None

    def release():
        _connections[key] -= 1
        if not _connections[key]:
            del _connections[key]

    try:
        address = f"[{target.address}]" if ":" in target.address else target.address
        headers = {"Authorization": f"Bearer {target.token}", "X-Adp-Control-Generation": str(target.generation)}
        if cursor:
            headers["Last-Event-ID"] = cursor
        request = client.build_request("GET", f"http://{address}:{target.port}/agent/events", headers=headers, timeout=httpx.Timeout(8, connect=3))
        response = await client.send(request, stream=True, follow_redirects=False)
        if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
            raise ControlError(503, "live explanations unavailable")
    except BaseException:
        if response is not None:
            await response.aclose()
        if owned_client:
            await client.aclose()
        release()
        raise

    closed = False

    async def cleanup():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            await response.aclose()
        finally:
            try:
                if owned_client:
                    await client.aclose()
            finally:
                release()

    async def body():
        pending = None
        buffer = b""
        last_check = asyncio.get_running_loop().time()
        try:
            chunks = response.aiter_bytes().__aiter__()
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(chunks))
                done, _ = await asyncio.wait({pending}, timeout=1)
                now = asyncio.get_running_loop().time()
                if now - last_check >= 1:
                    if not await asyncio.wait_for(check(), timeout=5):
                        yield frame("finished", {})
                        return
                    last_check = now
                if not done:
                    continue
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    break
                pending = None
                # Never retain an unbounded upstream chunk or unterminated frame.
                for offset in range(0, len(chunk), 4096):
                    buffer += chunk[offset : offset + 4096]
                    while b"\n\n" in buffer:
                        raw, buffer = buffer.split(b"\n\n", 1)
                        if len(raw) > MAX_FRAME_BYTES:
                            raise ValueError("oversized event")
                        lines = raw.decode("utf-8").splitlines()
                        kinds = [s[7:] for s in lines if s.startswith("event: ")]
                        data = [s[6:] for s in lines if s.startswith("data: ")]
                        if len(kinds) != 1 or len(data) != 1:
                            raise ValueError("invalid event")
                        kind, value = kinds[0], json.loads(data[0])
                        if kind in {"explanation", "terminal"}:
                            if (
                                value.get("version") != 1
                                or value.get("invocation_id") != run_id
                                or value.get("generation") != target.generation
                                or type(value.get("sequence")) is not int
                                or value["sequence"] < 1
                                or not isinstance(value.get("timestamp"), str)
                            ):
                                raise ValueError("event identity mismatch")
                            timestamp = datetime.fromisoformat(value["timestamp"].replace("Z", "+00:00"))
                            if timestamp.tzinfo is None:
                                raise ValueError("event timestamp must include timezone")
                            text = value.get("payload", {}).get("text")
                            if kind == "explanation" and not isinstance(text, str):
                                raise ValueError("invalid explanation")
                            safe = {k: value[k] for k in ("version", "invocation_id", "generation", "sequence", "timestamp")}
                            safe["timestamp"] = timestamp.isoformat()
                            safe.update(kind=kind, payload={"text": text.replace(target.token, "[redacted]")} if kind == "explanation" else {})
                            progress = value.get("payload", {}).get("progress")
                            if kind == "explanation" and progress is not None:
                                if (
                                    not isinstance(progress, dict)
                                    or not isinstance(progress.get("id"), str)
                                    or not 0 < len(progress["id"]) <= 256
                                    or progress.get("category") not in {"message", "tool", "plan"}
                                    or progress.get("state") not in {"running", "completed", "failed"}
                                    or not isinstance(progress.get("started_at"), str)
                                    or len(progress["started_at"]) > 64
                                ):
                                    raise ValueError("invalid progress")
                                started = datetime.fromisoformat(progress["started_at"].replace("Z", "+00:00"))
                                if started.tzinfo is None:
                                    raise ValueError("invalid progress timestamp")
                                safe["payload"]["progress"] = {k: progress[k] for k in ("id", "category", "state", "started_at")}
                            yield frame(kind, safe, f"{run_id}:{target.generation}:{value['sequence']}")
                            if kind == "terminal":
                                return
                        elif kind == "heartbeat":
                            yield frame(kind, {"timestamp": datetime.now(UTC).isoformat()})
                        elif kind == "reset":
                            yield frame(kind, {"reason": "History unavailable; showing retained updates."})
                        else:
                            raise ValueError("unsupported event")
                    if len(buffer) > MAX_FRAME_BYTES:
                        raise ValueError("oversized event")
        except (Exception,):
            # Never send an upstream body, token, exception or raw model block.
            yield frame("unavailable", {"reason": "Live feed unavailable; reconnect to recheck access."})
        finally:
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
            await cleanup()

    return BoundedStreamingResponse(
        body(), cleanup=cleanup, media_type="text/event-stream", headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"}
    )
