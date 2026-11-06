"""Bound CPU scrubbing admission independently of the default asyncio pool."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import BoundedSemaphore

from prometheus_client import Counter

scrub_limits = Counter("chat_scrub_limits_total", "Content redacted or refused at a scrubbing limit", ["reason"])
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chat-scrub")
_slots = BoundedSemaphore(2)


class ScrubCapacityError(Exception):
    """All bounded scrub workers are occupied; do not queue more content."""


async def run_scrub(function, *args, **kwargs):
    if not _slots.acquire(blocking=False):
        scrub_limits.labels(reason="capacity").inc()
        raise ScrubCapacityError
    try:
        future = _pool.submit(partial(function, *args, **kwargs))
    except BaseException:
        _slots.release()
        raise
    # Release only when CPU work actually ends, including if its awaiter is
    # cancelled. Shielding prevents cancellation from freeing a queued slot early.
    future.add_done_callback(lambda _: _slots.release())
    return await asyncio.shield(asyncio.wrap_future(future))
