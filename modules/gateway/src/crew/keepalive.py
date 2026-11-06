"""Keepalives so idle Crew streams survive edge idle and read timeouts (#7902).

CloudFront caps a VPC origin's read timeout at 60 seconds and ALBs have their own
idle timeouts, so a proxied stream that stays silent long enough is cut by the
edge, and the user sees the dashboard reconnect. The proxy therefore keeps bytes
moving while nothing else does:

* SSE / streaming HTTP bodies with ``text/event-stream`` get the comment line
  ``: keepalive\\n\\n`` whenever no bytes were sent for one interval. EventSource
  clients ignore comment lines, so nothing reaches application code.
* The WebSocket hop to the pod uses the ``websockets`` client's protocol ping at
  the same interval. The browser hop is pinged by the ASGI server itself: ASGI has
  no ping event, and uvicorn's ``websockets`` implementation already pings every
  ``ws_ping_interval`` seconds (default 20, configurable through
  ``UVICORN_WS_PING_INTERVAL``), which ``tests/crew/test_keepalive.py`` guards.

``CREW_PROXY_KEEPALIVE_SECONDS`` sets the interval (default 25, ``0`` disables).
"""

import asyncio
import math
import os
from collections.abc import AsyncIterator

KEEPALIVE_ENV = "CREW_PROXY_KEEPALIVE_SECONDS"
DEFAULT_KEEPALIVE_SECONDS = 25.0
SSE_KEEPALIVE = b": keepalive\n\n"


def keepalive_seconds() -> float:
    """Configured keepalive interval; ``0`` means disabled, garbage means the default."""
    raw = os.environ.get(KEEPALIVE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_KEEPALIVE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_KEEPALIVE_SECONDS
    if not math.isfinite(value):
        return DEFAULT_KEEPALIVE_SECONDS
    return value if value > 0 else 0.0


def is_event_stream(content_type: str | None) -> bool:
    return bool(content_type) and content_type.split(";", 1)[0].strip().lower() == "text/event-stream"


async def keepalive_stream(chunks: AsyncIterator[bytes], interval: float, *, comment: bytes = SSE_KEEPALIVE) -> AsyncIterator[bytes]:
    """Yield ``chunks`` unchanged and in order, inserting ``comment`` after each idle ``interval``.

    The pending read is held across keepalives rather than cancelled, so an upstream
    chunk that arrives while a comment is being written is still delivered next.
    """
    if interval <= 0:
        async for chunk in chunks:
            yield chunk
        return
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(chunks))
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield comment
                continue
            finished, pending = pending, None
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
