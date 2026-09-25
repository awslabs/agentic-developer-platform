"""Pull-based SSE transport with a bound on every ASGI write."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from src.tasks.limits import SSE_BLOCKED_WRITE_DISCONNECT_SECONDS
from src.tasks.stream_leases import RENEW_SECONDS, StreamLease
from src.tasks.streaming import CLOSE_SLOW_CONSUMER, StreamOutcome


class TaskStreamingResponse(StreamingResponse):
    """Hold one outgoing frame; disconnect blocked readers without advancing it.

    The iterator resumes only after a successful send, preserving its conservative
    replay cursor. Slot ownership belongs here, including when response_start
    fails before the body generator has ever run.
    """

    def __init__(self, content, *, outcome: StreamOutcome, release: Callable, lease: StreamLease | None = None, **kwargs):
        super().__init__(content, **kwargs)
        self.outcome = outcome
        self.release = release
        self._released = False
        self.lease = lease

    async def _bounded_send(self, send: Send, message: Message) -> bool:
        remaining = self.lease.remaining() if self.lease else float("inf")
        if remaining <= 0:
            self.outcome.reason = "lease_lost"
            return False
        try:
            async with asyncio.timeout(min(SSE_BLOCKED_WRITE_DISCONNECT_SECONDS, remaining)):
                await send(message)
        except TimeoutError:
            self.outcome.reason = "lease_lost" if self.lease and self.lease.remaining() <= 0 else CLOSE_SLOW_CONSUMER
            return False
        if self.lease and self.lease.remaining() <= 0:
            self.outcome.reason = "lease_lost"
            return False
        return True

    async def stream_response(self, send: Send) -> None:
        if not await self._bounded_send(
            send,
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            },
        ):
            return
        async for chunk in self.body_iterator:
            if not isinstance(chunk, bytes | memoryview):
                chunk = chunk.encode(self.charset)
            if not await self._bounded_send(
                send,
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                },
            ):
                # Do not manufacture a final frame after a failed write. Returning
                # an incomplete response makes the HTTP server close transport;
                # retained events remain available from the last delivered cursor.
                return
        await self._bounded_send(send, {"type": "http.response.body", "body": b"", "more_body": False})

    async def _guard_lease(self) -> None:
        while self.lease and self.lease.remaining() > 0:
            await asyncio.sleep(min(RENEW_SECONDS, self.lease.remaining()))
            if not await self.lease.renew():
                break
        self.outcome.reason = "lease_lost"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = guard = None
        try:
            if self.lease is None:
                await super().__call__(scope, receive, send)
            else:
                response = asyncio.create_task(super().__call__(scope, receive, send))
                guard = asyncio.create_task(self._guard_lease())
                done, _ = await asyncio.wait((response, guard), return_when=asyncio.FIRST_COMPLETED)
                if guard in done:
                    # Cancel even a pending ASGI write when renewal fails.
                    response.cancel()
                    await asyncio.gather(response, return_exceptions=True)
                else:
                    await response
        finally:
            # Starlette's older ASGI disconnect path uses an AnyIO cancel scope.
            # Shield bounded cleanup so disconnects also release shared capacity.
            with anyio.CancelScope(shield=True):
                for task in (response, guard):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(t for t in (response, guard) if t is not None), return_exceptions=True)
                try:
                    close = getattr(self.body_iterator, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    if not self._released:
                        self._released = True
                        result = self.release()
                        if inspect.isawaitable(result):
                            await result
