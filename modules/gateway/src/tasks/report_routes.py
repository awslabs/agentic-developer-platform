"""``POST /internal/v1/agent/task/report`` — host-authenticated progress ingest.

This is the write side of the live-progress requirement: the host process calls
it for each authored update, the gateway persists the event and returns its
allocated sequence, and only then can a subscriber observe it. Progress is
durable before it is deliverable, which is what makes a reconnect able to replay
it.

Three rules govern this route, and all three are about where authority comes
from.

**The body binds; it does not select.** The request carries an attempt binding
(``task_id``, ``invocation_id``, ``generation``, ``runtime_attempt_id``) and every
one of those is compared against the protected authority the transport
established — never used to look one up. Design section 12: "Run routes require
IAM transport, run credential and live TokenReview binding; the gateway derives
tenant/task/run/generation. Commands cannot supply another task ID to a run
route." A route that read the task ID from the body would let a worker report
progress into another tenant's task with its own valid credential, which is
exactly the class of defect the run-credential mechanism exists to remove.

**A superseded attempt is refused, not appended out of band.** Invariant LC-10:
late output from a replaced worker alters no outcome. The fence is the task row's
current ``generation`` and ``runtime_attempt_id``, checked inside the same
conditional write that allocates the sequence — not read first and trusted,
which would leave a window where a replacement commits between the check and the
append.

**The host cannot author state.** Only kinds that carry no server-owned authority
field are accepted here; see ``HOST_REPORTABLE_EVENT_TYPES``. Status transitions,
terminal outcomes and command receipts are authored by the gateway from its own
committed state on its own routes, because their data includes ``status`` and
``version`` — fields a producer must never be able to set.

Design reference: implementation-design.md sections 7, 9 and 12;
``internal-adapters.schema.json#/$defs/report_request``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from src.agentauth.routes import require_agent_transport
from src.tasks import errors, http
from src.tasks.events import SCHEMA_VERSION, validate_event_data
from src.tasks.limits import MAX_PROGRESS_EVENT_BYTES, MAX_REPORT_FRAME_BYTES
from src.tasks.store import (
    EventBudgetExhaustedError,
    ReportConflictError,
    SequenceFencedError,
    TaskStore,
    TaskStoreError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1/agent/task", tags=["task-api"], dependencies=[Depends(require_agent_transport)])

#: The kinds a host process may author. Deliberately three.
#:
#: Every other kind's contract data requires ``status``, ``version``, a
#: ``command_id`` or a terminal ``outcome`` — all server-owned. Accepting
#: ``task.completed`` here would let a worker declare its own task complete with
#: its own version number, bypassing the finalization path that first stores and
#: verifies result artifacts and validates process exit (design section 8). The
#: worker's job is to report what it observed; deciding what that means for the
#: task's state is the gateway's.
HOST_REPORTABLE_EVENT_TYPES = frozenset({"progress.updated", "artifact.created", "input.required"})

#: A verified attempt is a per-request fact, so the authenticator is injected the
#: same way the store is. T3/T4 own the bootstrap and attempt-registration routes
#: that mint the credential this verifies; wiring the real one is their lane, and
#: guessing at their execution-record shape here would create a second definition
#: of run identity to drift against.
_AUTHENTICATOR = None
_STORE: TaskStore | None = None


@dataclass(frozen=True)
class VerifiedAttempt:
    """The attempt identity the transport proved, as this route needs it.

    Frozen because this object *is* the authority. Code that could reassign
    ``task_id`` after verification would have rebuilt the body-selects-authority
    defect the module docstring describes.
    """

    task_id: str
    invocation_id: str
    generation: int
    runtime_attempt_id: str


def set_authenticator(authenticator) -> None:
    """Install the attempt authenticator. Called by T3/T4's wiring and by tests."""
    global _AUTHENTICATOR
    _AUTHENTICATOR = authenticator


def set_store(store: TaskStore | None) -> None:
    global _STORE
    _STORE = store


class RunBinding(BaseModel):
    """``internal-adapters.schema.json#/$defs/run_binding``."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    invocation_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    generation: int = Field(ge=1, le=64, strict=True)


class AttemptBinding(BaseModel):
    """``internal-adapters.schema.json#/$defs/attempt_binding``.

    ``runtime_attempt_id`` is required and non-null here, though the public event
    schema permits null. The contract's own note says why: "Active callbacks are
    always fenced to one concrete in-process attempt; null is only valid in public
    history before an attempt exists." A report is an active callback, so a null
    attempt would be an unfenced write.
    """

    model_config = ConfigDict(extra="forbid")

    run: RunBinding
    runtime_attempt_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class ReportRequest(BaseModel):
    """``internal-adapters.schema.json#/$defs/report_request``.

    ``extra="forbid"`` throughout, mirroring the schema's ``additionalProperties:
    false``. An unknown field is a refusal rather than a silently ignored one,
    because a producer sending a field this gateway does not implement is a
    version mismatch, and ignoring it would let a host believe it had reported
    something the durable record never carried.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=r"^1\.0$")
    attempt: AttemptBinding
    report_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    event_type: str
    producer_timestamp: str | None
    data: dict


def verified_attempt(request: Request) -> VerifiedAttempt:
    if _AUTHENTICATOR is None:
        raise errors.prerequisite_unavailable("Task attempt verification is not configured in this environment.")
    return _AUTHENTICATOR(request)


def check_binding(body: ReportRequest, attempt: VerifiedAttempt) -> None:
    """Require the body's binding to match the proven attempt exactly.

    One refusal for all four mismatches. A response that distinguished "wrong
    task" from "wrong generation" would let a worker probe the gateway's view of
    other executions one field at a time, and the correct action is identical in
    every case: stop and re-bootstrap.
    """
    presented = (body.attempt.run.task_id, body.attempt.run.invocation_id, body.attempt.run.generation, body.attempt.runtime_attempt_id)
    proven = (attempt.task_id, attempt.invocation_id, attempt.generation, attempt.runtime_attempt_id)
    if presented != proven:
        logger.warning("Task API report refused: body binding does not match the verified attempt")
        raise errors.state_conflict("The report's attempt binding does not match the verified run attempt.")


def check_payload(body: ReportRequest) -> None:
    """Validate kind, data shape and size before anything is persisted.

    Size is checked on the serialized data rather than on the raw request body:
    the durable record is what the limit protects, and a request whose framing
    overhead pushed it over the line would otherwise be refused for carrying
    payload that fits.

    Oversize is refused, never truncated. A truncated progress message is a
    durable, externally readable record that misquotes what the agent reported —
    worse than an explicit refusal the host can log and shorten.
    """
    if body.event_type not in HOST_REPORTABLE_EVENT_TYPES:
        # Not 400: the kind may be perfectly valid and simply not the host's to
        # author. Naming that distinction is what stops an implementer from
        # "fixing" a rejected task.completed by widening the allowlist.
        raise errors.disallowed_scope("A run attempt may not author this event kind.")

    try:
        validate_event_data(body.event_type, body.data)
    except ValueError as error:
        # ``validate_event_data`` names keys and kinds and never echoes a value,
        # so its message is safe to return to this authenticated internal caller.
        raise errors.invalid_request(str(error)) from None

    encoded = len(json.dumps(body.data, separators=(",", ":"), sort_keys=True).encode())
    if encoded > MAX_PROGRESS_EVENT_BYTES:
        raise errors.payload_too_large(f"Event data exceeds the {MAX_PROGRESS_EVENT_BYTES}-byte per-event limit.")


async def parse_body(request: Request) -> ReportRequest:
    """Read and validate the request body, bounding it before it is parsed.

    Deliberately not a FastAPI body parameter, for two reasons that are both
    correctness rather than taste:

    * **Status.** FastAPI renders a Pydantic validation failure as ``422``, and
      ``422`` is not in the Task API's status table (design section 5) or in
      ``errors.schema.json``'s code/status map. A declared body parameter would
      make every malformed request answer with a status and body the contract
      forbids — a response no conformance check would accept, from a path that
      otherwise looks like it works.
    * **Size.** The frame limit has to apply *before* the JSON is parsed. Declaring
      the body would have the framework parse it first, so a multi-megabyte
      document would be fully decoded in order to be told it was too large, which
      is the cost the limit exists to avoid.
    """
    declared = request.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_REPORT_FRAME_BYTES:
        raise errors.payload_too_large(f"A report frame may not exceed {MAX_REPORT_FRAME_BYTES} bytes.")

    raw = await request.body()
    # Checked again against the actual bytes: Content-Length is a claim, and a
    # chunked request carries none at all.
    if len(raw) > MAX_REPORT_FRAME_BYTES:
        raise errors.payload_too_large(f"A report frame may not exceed {MAX_REPORT_FRAME_BYTES} bytes.")

    try:
        return ReportRequest.model_validate_json(raw)
    except ValueError:
        # The validation detail is not returned. Pydantic echoes offending input
        # values into its messages, and a report body carries agent-authored task
        # text, so reflecting it would put untrusted content into an error
        # response and every log that records one.
        logger.info("Task API report refused: body does not match the report contract")
        raise errors.invalid_request("The request body does not match the report contract.") from None


@router.post("/report")
@http.contract_errors
async def report(request: Request):
    """Persist one authored progress event and return its committed position.

    The response is the same whether the event was committed now or on an earlier
    identical call. That is what lets a host retry after a lost response without
    choosing between double-reporting and dropping progress: it cannot tell a lost
    response from a lost request, so the only safe protocol is one where retrying
    is free.
    """
    http.require_flag(http.FLAG_WORKER)

    if _STORE is None:
        raise errors.prerequisite_unavailable("Task storage is not configured in this environment.")

    # Attempt verification precedes reading the body. The transport guard has
    # already run, but the run credential is what proves *which* execution is
    # calling, and confirming it first means an unproven caller cannot make this
    # process buffer and parse up to 64 KiB before being refused.
    attempt = verified_attempt(request)
    body = await parse_body(request)
    check_binding(body, attempt)
    check_payload(body)

    try:
        result = _STORE.append_event(
            task_id=attempt.task_id,
            report_id=body.report_id,
            event_type=body.event_type,
            data=body.data,
            producer_timestamp=body.producer_timestamp,
            timestamp=http.server_timestamp(),
            # The fence travels into the conditional write rather than being
            # checked here. A pre-read check would leave a window in which a
            # verified replacement commits between the check and the append, and
            # the superseded worker's event would land anyway.
            expect_generation=attempt.generation,
            expect_runtime_attempt_id=attempt.runtime_attempt_id,
        )
    except SequenceFencedError:
        # The host must stop, not retry. 409 rather than 404 because this producer
        # already proved it owns this invocation, so "you have been replaced" tells
        # it nothing it is not entitled to know — and a retryable-looking refusal
        # would have it re-sending progress that can never be accepted.
        logger.info("Task API report fenced: the reporting attempt is not current")
        raise errors.state_conflict("This run attempt has been superseded; it can no longer report.") from None
    except ReportConflictError:
        raise errors.TaskApiError(
            409,
            "idempotency_conflict",
            "This report ID was already committed with different content.",
        ) from None
    except EventBudgetExhaustedError:
        # 429 rather than 413: the individual report is fine, the task has used
        # its allowance. The reserved tail means terminal evidence can still be
        # recorded, so refusing progress here does not make the outcome
        # unrecordable.
        raise errors.rate_limited("This task has exhausted its event budget.") from None
    except TaskStoreError:
        logger.warning("Task API report failed: task storage unavailable", exc_info=True)
        raise errors.prerequisite_unavailable("Task storage is unavailable.") from None

    return http.ok(
        {
            "schema_version": SCHEMA_VERSION,
            "report_id": body.report_id,
            "sequence": result.event.sequence,
            "event_id": result.event.event_id,
        }
    )
