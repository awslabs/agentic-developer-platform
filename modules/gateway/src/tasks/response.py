"""Pull-based SSE transport with a bound on every ASGI write."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from src.tasks.limits import SSE_BLOCKED_WRITE_DISCONNECT_SECONDS
from src.tasks.streaming import CLOSE_SLOW_CONSUMER, StreamOutcome


class TaskStreamingResponse(StreamingResponse):
    """Hold one outgoing frame; disconnect blocked readers without advancing it.

    The iterator resumes only after a successful send, preserving its conservative
    replay cursor. Slot ownership belongs here, including when response_start
    fails before the body generator has ever run.
    """

    def __init__(self, content, *, outcome: StreamOutcome, release: Callable[[], None], **kwargs):
        super().__init__(content, **kwargs)
        self.outcome = outcome
        self.release = release
        self._released = False

    async def _bounded_send(self, send: Send, message: Message) -> bool:
        try:
            async with asyncio.timeout(SSE_BLOCKED_WRITE_DISCONNECT_SECONDS):
                await send(message)
        except TimeoutError:
            self.outcome.reason = CLOSE_SLOW_CONSUMER
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                if not self._released:
                    self._released = True
                    self.release()
