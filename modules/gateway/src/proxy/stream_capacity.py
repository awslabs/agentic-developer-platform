"""Optional per-worker inference admission without affecting tenant quotas."""

import asyncio
import json
import re

INFERENCE_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/messages",
        "/bedrock/invoke",
        "/bedrock/invoke-with-response-stream",
        "/openai/v1/responses",
    }
)
MODEL_INVOKE = re.compile(r"^/model/[^/]+/invoke(?:-with-response-stream)?$")


def admission_settings(environment):
    names = ("BG_MAX_ACTIVE_STREAMS_PER_WORKER", "BG_STREAM_ADMISSION_QUEUE_LIMIT", "BG_STREAM_ADMISSION_WAIT_MS")
    values = [environment.get(name) for name in names]
    if all(value is None for value in values):
        return None
    if any(value is None or not value.isdecimal() for value in values):
        raise ValueError("Inference admission needs complete numeric worker, queue and wait limits")
    active, queue, wait_ms = map(int, values)
    if not (1 <= active <= 100000 and 0 <= queue <= 10000 and 1 <= wait_ms <= 5000):
        raise ValueError("Inference admission limits exceed the finite safety bounds")
    return active, queue, wait_ms


class StreamCapacityMiddleware:
    def __init__(self, app, gateway, max_active, queue_limit, wait_ms):
        self.app = app
        self.gateway = gateway
        self.active = asyncio.Semaphore(max_active)
        self.queue_limit = queue_limit
        self.wait_seconds = wait_ms / 1000
        self.waiting = 0

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or (scope.get("path") not in INFERENCE_PATHS and not MODEL_INVOKE.fullmatch(scope.get("path", "")))
        ):
            await self.app(scope, receive, send)
            return
        acquired = False
        if not self.active.locked():
            await self.active.acquire()
            acquired = True
        elif self.waiting < self.queue_limit:
            self.waiting += 1
            try:
                await asyncio.wait_for(self.active.acquire(), timeout=self.wait_seconds)
                acquired = True
            except TimeoutError:
                pass
            finally:
                self.waiting -= 1
        if not acquired or getattr(self.gateway.state, "shutting_down", False):
            if acquired:
                self.active.release()
            reason = "gateway_shutting_down" if getattr(self.gateway.state, "shutting_down", False) else "gateway_overloaded"
            await self.reject(send, reason)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.active.release()

    @staticmethod
    async def reject(send, reason):
        await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json"), (b"retry-after", b"1")]})
        await send({"type": "http.response.body", "body": json.dumps({"error": reason}).encode()})
