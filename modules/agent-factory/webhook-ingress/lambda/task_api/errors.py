"""Task API error shape.

One shape for every refusal: ``code``, a safe ``message``, ``request_id`` and
optional ``retry_after_ms``/``details``. The code and HTTP status must agree —
a component cannot return 404 for a conflict or 202 for a denial.

Messages are constructed from fixed strings only. A caller's token, the
producer proof, a client secret or an unrestricted task body must never reach
a response body or a log line.
"""

from __future__ import annotations

from . import contract


class TaskApiError(Exception):
    """A refusal that maps to exactly one contract error code and status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_ms: int | None = None,
        details: dict | None = None,
    ) -> None:
        if code not in contract.ERROR_STATUS:
            raise ValueError(f"unknown task api error code: {code}")
        if details:
            unknown = set(details) - contract.ERROR_DETAIL_FIELDS
            if unknown:
                raise ValueError(f"unsafe error detail fields: {sorted(unknown)}")
        super().__init__(code)
        self.code = code
        self.status = contract.ERROR_STATUS[code]
        self.message = message[: contract.MAX_SAFE_MESSAGE_CHARACTERS]
        self.retry_after_ms = retry_after_ms
        self.details = dict(details) if details else None

    def body(self, request_id: str) -> dict:
        """Render the contract error body for this refusal."""
        payload: dict = {
            "schema_version": contract.SCHEMA_VERSION,
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
            "http_status": self.status,
        }
        if self.retry_after_ms is not None:
            payload["retry_after_ms"] = self.retry_after_ms
        if self.details:
            payload["details"] = self.details
        return payload


def invalid_request(message: str, **details) -> TaskApiError:
    return TaskApiError("invalid_request", message, details=details or None)


def invalid_credential(message: str = "A valid access token is required.") -> TaskApiError:
    return TaskApiError("invalid_credential", message)


def disallowed_scope(
    message: str = "The presented credential does not carry the required task scope.",
) -> TaskApiError:
    return TaskApiError("disallowed_scope", message)


def disallowed_persona(message: str) -> TaskApiError:
    return TaskApiError("disallowed_persona", message)


def payload_too_large(limit_name: str, limit_value: int) -> TaskApiError:
    return TaskApiError(
        "payload_too_large",
        "The request exceeds the permitted task submission size.",
        details={"limit_name": limit_name, "limit_value": limit_value},
    )


def prerequisite_unavailable(
    message: str = (
        "Task submission outcome is unavailable; retry with the same "
        "Idempotency-Key."
    ),
    *,
    retry_after_ms: int = 5000,
) -> TaskApiError:
    return TaskApiError(
        "prerequisite_unavailable", message, retry_after_ms=retry_after_ms
    )


def not_found(message: str = "No such resource.") -> TaskApiError:
    return TaskApiError("not_found", message)
