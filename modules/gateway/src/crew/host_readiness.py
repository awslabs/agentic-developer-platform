"""Does the Dev Box's Crew actually answer yet? (issue #9619)

The controller activates a host run as soon as the box's private ingress is up and
enrolled; Kiro Crew behind that ingress needs another 20-30 s to warm up. In that
window ``GET /crew/target`` said ``ready`` and the one-click open handed off into a
mint the box could not serve. This module asks the ingress one cheap question — does
a request to the dashboard upstream get an HTTP answer at all — and lets the ec2
target report ``starting`` until it does.

The probe is side-effect free (a GET to ``/`` through the ingress; the ingress
answers 502 while Kiro Crew is not listening and any HTTP status once it is) and is
cached: a positive answer sticks for the host run, a negative one is not re-asked for
``NEGATIVE_TTL_SECONDS``. The target is resolved on every dashboard request, so the
steady state is a dictionary lookup, not a network call.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 3.0
NEGATIVE_TTL_SECONDS = 2.0
_MAX_REMEMBERED = 1000

_ready: dict[str, float] = {}
_last_negative: dict[str, float] = {}
_monotonic = time.monotonic


def _key(target) -> str:
    return target.host_run_id or f"{target.lease.target_ref}:{target.lease.generation}"


def _prune() -> None:
    for store in (_ready, _last_negative):
        if len(store) > _MAX_REMEMBERED:
            for key in sorted(store, key=store.get)[: len(store) - _MAX_REMEMBERED]:
                store.pop(key, None)


async def _probe(target) -> bool:
    from src.crew.handoff import crew_host_client

    client = await crew_host_client(target)
    try:
        async with client:
            response = await client.get(f"{target.endpoint.rstrip('/')}/", timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=False)
    except (httpx.HTTPError, OSError):
        return False
    # 502 is the ingress saying "nothing is listening behind me yet"; anything else is
    # Kiro Crew answering (a login redirect, 401, 200 ...), which is all the handoff needs.
    return response.status_code != 502


async def crew_host_answers(target) -> bool:
    """True once the box's Crew answers through its ingress; cached per host run."""
    key = _key(target)
    if key in _ready:
        return True
    now = _monotonic()
    if now - _last_negative.get(key, float("-inf")) < NEGATIVE_TTL_SECONDS:
        return False
    try:
        answered = await _probe(target)
    except Exception as exc:  # noqa: BLE001 - a probe failure is "not yet", never an error
        logger.info("crew host readiness probe failed for %s: %s", key, type(exc).__name__)
        answered = False
    if answered:
        _ready[key] = now
        _last_negative.pop(key, None)
    else:
        _last_negative[key] = now
    _prune()
    return answered


def forget(target) -> None:
    """Drop the cached answer (tests and fencing)."""
    key = _key(target)
    _ready.pop(key, None)
    _last_negative.pop(key, None)
