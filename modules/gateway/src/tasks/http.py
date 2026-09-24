"""Turning typed Task API failures into the exact bodies the contract fixes.

``errors.py`` knows what a Task API failure *is*; this module knows how one
reaches a caller. They are separate because ``errors.py`` is imported by the
store, the stream loop and the authorization code, none of which should depend on
FastAPI — and because the rendering rule has one property worth isolating: a
Task API response body is never produced by the gateway's existing exception
handlers.

That is not a stylistic preference. The app-level handler renders
``BedrockGatewayError`` as ``{error, message, details}``, and the Task API
contract requires ``{schema_version, code, message, request_id, http_status}``
with the code drawn from a closed per-status enum. A Task API route whose
refusals fell through to the existing handler would return a body that the
contract validator rejects — while still looking like a working error path. So
every route in this package wraps its own handler, rather than registering a new
app-level handler that would change how unrelated routes render.

Design reference: implementation-design.md section 5; ``errors.schema.json``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from src.tasks import errors
from src.tasks.events import format_timestamp, utc_now

logger = logging.getLogger(__name__)

#: Design section 11. Every flag defaults false, so a deployment that merely
#: picks up this code exposes nothing: the routes mount and refuse.
FLAG_READ = "ADP_TASK_API_READ_ENABLED"
FLAG_WORKER = "ADP_TASK_API_WORKER_ENABLED"


def flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def require_flag(name: str) -> None:
    """Refuse when a surface is not enabled in this environment.

    ``503`` rather than ``404``, matching how the rest of the gateway gates a
    disabled module, so an operator reading logs sees a disabled prerequisite
    rather than a routing mistake.

    Deliberately built without ``retry_after_ms``, unlike every other 503 here.
    A disabled feature flag will not become enabled in a second, and advertising
    a retry delay would invite a conforming client — which the contract instructs
    to retry 503 with bounded jitter — into an indefinite poll against a surface
    that is off on purpose.
    """
    if not flag_enabled(name):
        raise errors.TaskApiError(503, "prerequisite_unavailable", "This Task API surface is not enabled in this environment.")


def request_id(request: Request) -> str:
    """The correlation ID this request is already logged under, if there is one.

    Reused rather than generated per response, because the contract's
    ``request_id`` is only useful if the value in the body is the value an
    operator can search for. The gateway's logging middleware puts it on the ASGI
    state; generating a fresh UUID here would hand the caller an identifier that
    appears in no server-side record.
    """
    existing = request.scope.get("state", {}).get("request_id")
    return existing if isinstance(existing, str) and existing else f"req-{uuid.uuid4().hex[:16]}"


def parse_json_object(raw: bytes) -> dict:
    """Parse a request body as a JSON object, refusing what the contract refuses.

    Section 5 requires every JSON request to reject duplicate fields, nonfinite
    numbers and invalid UTF-8. ``json.loads`` accepts all three by default: it
    keeps the last of duplicate keys, parses ``NaN``/``Infinity`` as floats, and
    would raise an unhandled decode error on bad UTF-8. Each is hooked here rather
    than checked afterwards, because by then the evidence is gone — a duplicate
    key is indistinguishable from a single one once parsed.

    Duplicate fields matter beyond tidiness: two parsers that disagree about which
    of ``{"content_type": "text/plain", "content_type": "text/html"}`` wins is the
    classic way a validated value and a used value differ. Refusing is the only
    answer that cannot be inconsistent.
    """

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError("duplicate field")
            seen.add(key)
        return dict(pairs)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("body is not valid UTF-8") from None

    body = json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=_reject_constant,
    )
    if not isinstance(body, dict):
        raise ValueError("body is not a JSON object")
    return body


def _reject_constant(name: str) -> None:
    """Refuse ``NaN``, ``Infinity`` and ``-Infinity``.

    Not representable in the contract's number types, and a nonfinite value that
    reached storage would compare false against itself — making a field that can
    never match on retry and never equal its own committed copy.
    """
    raise ValueError(f"nonfinite number {name}")


def server_timestamp() -> str:
    """The server's own RFC3339 UTC time for a durable record.

    Separate from any producer timestamp a caller supplies, and never substituted
    for it. A producer's clock is untrusted input: ordering and retention decisions
    use this value, while the producer's time is retained only as reported
    evidence, which is what keeps a host with a skewed clock from reordering
    durable history.
    """
    return format_timestamp(utc_now())


def error_response(error: errors.TaskApiError, correlation_id: str) -> JSONResponse:
    """Render a refusal, including the transport header a client acts on.

    ``Retry-After`` duplicates ``retry_after_ms`` in the body because the two
    have different audiences: the body is for the client's own backoff logic, the
    header is for the proxies and HTTP libraries between us that will never parse
    our JSON. Sending only the body means a retryable refusal is invisible to
    every generic client in the path.
    """
    headers = {"Cache-Control": "no-store"}
    if error.retry_after_ms is not None:
        headers["Retry-After"] = str(max(1, round(error.retry_after_ms / 1000)))
    return JSONResponse(status_code=error.status, content=error.body(correlation_id), headers=headers)


def contract_errors(handler: Callable[..., Awaitable]) -> Callable[..., Awaitable]:
    """Render ``TaskApiError`` from one route, and refuse to leak anything else.

    The bare ``except Exception`` is the point rather than an oversight. An
    unexpected exception in a route that has already authorized a caller would
    otherwise reach the ASGI layer, which renders a body this contract does not
    define and, depending on configuration, can include exception text. Task
    state carries caller instructions and tool output, so an internal message is
    not safe to echo. It is logged with a traceback and answered as an
    unavailable prerequisite — the honest statement that this request did not
    complete and may be retried.
    """

    @functools.wraps(handler)
    async def wrapper(*args, **kwargs):
        request = kwargs.get("request") or next((arg for arg in args if isinstance(arg, Request)), None)
        correlation_id = request_id(request) if request is not None else f"req-{uuid.uuid4().hex[:16]}"
        try:
            return await handler(*args, **kwargs)
        except errors.TaskApiError as error:
            return error_response(error, correlation_id)
        except Exception:
            logger.exception("Task API route failed unexpectedly", extra={"request_id": correlation_id})
            return error_response(errors.prerequisite_unavailable(), correlation_id)

    return wrapper


def ok(body: dict, *, status: int = 200) -> JSONResponse:
    """A success body that is never cached.

    ``no-store`` on every one of them: a task snapshot is a point-in-time
    authorization-scoped read, and a cached copy served to a later request is
    both a stale state report and a potential cross-principal disclosure through
    any shared cache in the path.
    """
    return JSONResponse(status_code=status, content=body, headers={"Cache-Control": "no-store"})
