"""Typed Task API errors that render the frozen error contract.

The gateway already has two error idioms: ``BedrockGatewayError`` subclasses
(rendered by an app-level handler into ``{error, message, details}``) and bare
``HTTPException`` with deliberately opaque messages on the internal plane.
Neither produces the Task API body, which requires ``schema_version``, a ``code``
from a closed enum, a ``safe_message``, a ``request_id`` and an optional
``retry_after_ms`` — and which constrains *which* code may accompany which HTTP
status (``errors.schema.json``). A third idiom is not introduced lightly; it is
introduced because the code↔status pairing is a contract invariant that
components are checked against, and encoding it in one raising type is what keeps
two routes from answering the same condition differently.

Design reference: implementation-design.md section 5; ``errors.schema.json``.
"""

from __future__ import annotations

from src.tasks.events import SCHEMA_VERSION

#: The only code permitted with each status, from
#: ``errors.schema.json#/$defs/error_response``'s conditional branch. Asserted on
#: construction so an impossible pair fails in tests rather than reaching a
#: caller as a body that violates the contract it is validated against.
CODES_BY_STATUS: dict[int, frozenset[str]] = {
    400: frozenset({"invalid_request", "invalid_cursor"}),
    401: frozenset({"invalid_credential"}),
    403: frozenset({"disallowed_scope", "disallowed_persona"}),
    404: frozenset({"not_found"}),
    409: frozenset({"idempotency_conflict", "command_conflict", "state_conflict"}),
    410: frozenset({"history_expired"}),
    413: frozenset({"payload_too_large"}),
    429: frozenset({"rate_limited", "queue_full"}),
    503: frozenset({"prerequisite_unavailable"}),
}


class TaskApiError(Exception):
    """A Task API failure with its contract-valid code, status and body."""

    def __init__(self, status: int, code: str, message: str, *, details: dict | None = None, retry_after_ms: int | None = None) -> None:
        permitted = CODES_BY_STATUS.get(status)
        if permitted is None or code not in permitted:
            raise ValueError(f"code {code!r} is not permitted with HTTP {status}")
        self.status = status
        self.code = code
        # Truncated to the contract's ``safe_message`` bound rather than
        # rejected: an over-long internal message must not turn a legitimate
        # refusal into a 500 that tells the caller nothing.
        self.message = message[:1000]
        self.details = details
        self.retry_after_ms = retry_after_ms
        super().__init__(self.message)

    def body(self, request_id: str) -> dict:
        """Render ``errors.schema.json#/$defs/error_response``.

        ``request_id`` is supplied by the route rather than generated here so
        that the identifier in the error body is the same one recorded for the
        request, which is the only way an operator can correlate a caller's
        complaint with a server-side record.
        """
        body: dict = {"schema_version": SCHEMA_VERSION, "code": self.code, "message": self.message, "request_id": request_id}
        if self.retry_after_ms is not None:
            body["retry_after_ms"] = self.retry_after_ms
        body["http_status"] = self.status
        if self.details is not None:
            body["details"] = self.details
        return body


def not_found() -> TaskApiError:
    """The single answer for absent, invisible and unauthorized resources.

    One indistinguishable response for all three is the point. If "this task
    belongs to another principal" were distinguishable from "no such task", a
    caller could enumerate the existence of other tenants' tasks by observing
    which handles produce which refusal — so ownership failures deliberately
    return the same body as a genuine miss.
    """
    return TaskApiError(404, "not_found", "No such task.")


def invalid_cursor(message: str) -> TaskApiError:
    return TaskApiError(400, "invalid_cursor", message)


def invalid_request(message: str) -> TaskApiError:
    return TaskApiError(400, "invalid_request", message)


def disallowed_scope(message: str = "The credential does not carry the required Task API scope.") -> TaskApiError:
    return TaskApiError(403, "disallowed_scope", message)


def prerequisite_unavailable(message: str = "A required Task API dependency is unavailable.") -> TaskApiError:
    """Denial on an unavailable dependency, never a degraded success.

    Design section 4: "An unavailable authorization dependency denies new
    operations and closes streams; it cannot turn into anonymous access."
    ``retry_after_ms`` is set because this is one of the two retryable statuses
    and a client with no guidance either hammers the dependency or gives up.
    """
    return TaskApiError(503, "prerequisite_unavailable", message, retry_after_ms=1000)


def rate_limited(message: str) -> TaskApiError:
    """429 with a retry delay, for a caller over its own concurrency bound.

    Retryable because the condition is the caller's own resource use and clears
    when it releases what it holds — unlike a scope refusal, which no amount of
    retrying resolves.
    """
    return TaskApiError(429, "rate_limited", message, retry_after_ms=1000)


def payload_too_large(message: str) -> TaskApiError:
    return TaskApiError(413, "payload_too_large", message)


def state_conflict(message: str) -> TaskApiError:
    return TaskApiError(409, "state_conflict", message)


def history_expired(*, task_id: str, current_status: str, oldest_event_cursor: str, latest_event_cursor: str) -> TaskApiError:
    """410 with the retained bounds and an explicit gap flag.

    The flag is what makes this response honest rather than merely informative:
    it states that history between the caller's cursor and the oldest retained
    event is *gone*, so a client cannot read the response as "start here and you
    have everything". The contract ships a rejected fixture that omits the flag
    for precisely this reason.
    """
    return TaskApiError(
        410,
        "history_expired",
        "The supplied cursor is older than the oldest retained event for this task.",
        details={
            "task_id": task_id,
            "current_status": current_status,
            "oldest_event_cursor": oldest_event_cursor,
            "latest_event_cursor": latest_event_cursor,
            "history_gap": True,
        },
    )
