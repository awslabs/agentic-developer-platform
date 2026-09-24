"""Read the abort sentinel the Node worker writes — Issue #3963 (S4).

The supervising half of a run (``entrypoint.py``) owns the closing comment, the
invocation status, the check-run conclusion and the queue acknowledgement. The
agent half (``agent-worker.ts``) is where an operator's abort actually lands: it
holds the control listener, the pause gate and the cancellation signal. Only the
supervisor can write the invocation row, because only it holds both halves of
that row's key (``event_id`` AND ``arrived_at``); the agent is given
``ADP_MESSAGE_ID`` alone. So the abort has to travel between processes, and this
module is the reading end of that channel.

The writing end is ``control-abort-sentinel.ts``. The two are one contract and
the validation rules below are a deliberate mirror of ``validateAbortSentinel``.
Keep them in step: a rule enforced on only one side is not enforced.

## The asymmetry that shapes every decision here

A sentinel that passes validation makes this run report itself deliberately
stopped: status ``aborted``, check conclusion ``cancelled``, controls revoked and
its SQS message deleted so it never runs again. A sentinel that fails validation
makes the run classify itself by exit code, exactly as it did before this
feature existed.

Those two failure modes are not equally bad. Losing an abort signal costs a
mislabelled outcome that an operator can see and re-issue. *Fabricating* one
deletes a live run's queue message and tells the operator a run stopped on
purpose when it actually crashed. So this reader is strict, every rejection path
returns the same "no abort" answer, and no path raises: an exception during
teardown could cost the acknowledgement entirely.

Concretely, all of these mean **no abort happened**:

* no sentinel file (the overwhelmingly common case — no abort was requested),
* unreadable, truncated or unparseable bytes,
* a schema version this reader does not implement,
* a document naming a different run, or a superseded control generation,
* any missing or wrongly-typed required field.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

# Schema version. Matched exactly, not as a minimum — see the TS writer's note:
# a newer payload may narrow the abort's scope in a field this reader does not
# know to look at, and "understand what I can" is how that narrowing gets
# silently dropped. Both halves ship in one image, so a mismatch means something
# is genuinely wrong rather than merely old.
ABORT_SENTINEL_VERSION = 1

# Must equal ABORT_SENTINEL_PATH in control-abort-sentinel.ts. Pod-local /tmp,
# the same bridge shape as /tmp/adp-result-metadata.json and
# /tmp/adp-check-run-final.md.
ABORT_SENTINEL_PATH = "/tmp/adp-abort-sentinel.json"

# Matches MAX_SENTINEL_REASON_LENGTH on the writing side. Re-bounded on read
# rather than trusted: the reason is operator-supplied and is interpolated into a
# GitHub comment, and a sentinel written by any other path does not get to
# smuggle an unbounded string into it.
MAX_SENTINEL_REASON_LENGTH = 200

# A sentinel is a handful of short fields. Anything larger is not a sentinel this
# writer produced, and reading it would mean loading an arbitrary file from /tmp
# into memory during teardown. Mirrors the size guard in control-credentials.ts.
MAX_SENTINEL_BYTES = 8192


def read_abort_sentinel(
    run_id: str,
    generation: int | str | None,
    *,
    path: str = ABORT_SENTINEL_PATH,
) -> dict | None:
    """Return the validated sentinel for this run, or ``None`` for no abort.

    ``None`` means "this run was not aborted" in every case, including error
    cases. Callers must not distinguish absent from malformed: doing so would
    reintroduce the possibility of reporting an abort that was never requested.

    Never raises. Runs during teardown, where an exception could cost the SQS
    acknowledgement and strand the message.
    """
    try:
        if not run_id:
            # Without a run to bind against there is nothing to validate, and an
            # unvalidated sentinel is exactly what must never be honoured.
            return None

        expected_generation = _coerce_generation(generation)
        if expected_generation is None:
            return None

        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        if size > MAX_SENTINEL_BYTES:
            logger.warning(
                "Ignoring abort sentinel: %d bytes exceeds the %d-byte ceiling",
                size,
                MAX_SENTINEL_BYTES,
            )
            return None

        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)

        return validate_abort_sentinel(document, run_id, expected_generation)
    except Exception:  # noqa: BLE001 - the fail-soft contract, not defensive padding
        # Absent, unreadable, torn mid-write, not JSON, wrong encoding. All the
        # same answer, and none of them worth a traceback in a teardown log.
        #
        # The breadth is the point: this runs during teardown, where an escaping
        # exception could cost the SQS acknowledgement and strand the message.
        # Narrowing to OSError/JSONDecodeError would let some third error class
        # through to exactly the place that must not raise.
        return None


def validate_abort_sentinel(
    document: object,
    run_id: str,
    generation: int,
) -> dict | None:
    """Validate a parsed sentinel against this run. Mirror of the TS validator.

    Separated from the I/O so the rules can be unit-tested directly against the
    same table of cases the TypeScript suite uses.
    """
    if not isinstance(document, dict):
        # `json.load` happily returns a list, a string or None; none of those
        # have the fields below and all would raise on attribute access.
        return None

    if document.get("version") != ABORT_SENTINEL_VERSION:
        return None

    candidate_run = document.get("run_id")
    candidate_generation = document.get("generation")
    command_id = document.get("command_id")
    requested_at = document.get("requested_at")

    if not isinstance(candidate_run, str) or not candidate_run:
        return None
    # `isinstance(True, int)` is True in Python, so booleans are excluded
    # explicitly — a JSON `true` must not read as generation 1.
    if not isinstance(candidate_generation, int) or isinstance(candidate_generation, bool):
        return None
    if not isinstance(command_id, str) or not command_id:
        return None
    if not isinstance(requested_at, str) or not requested_at:
        return None

    # The run binding. Both halves must match. The run id alone would accept a
    # sentinel left by a superseded attempt of this same run; the generation
    # alone would accept one from an unrelated run that happened to share a
    # generation number. Generation is equality, not "at least": an abort aimed
    # at an attempt that has already ended must not finalize its replacement.
    if candidate_run != run_id:
        logger.warning("Ignoring abort sentinel written for a different run")
        return None
    if candidate_generation != generation:
        logger.warning(
            "Ignoring abort sentinel from control generation %s (this run is generation %s)",
            candidate_generation,
            generation,
        )
        return None

    return {
        "version": ABORT_SENTINEL_VERSION,
        "run_id": candidate_run,
        "generation": candidate_generation,
        "command_id": command_id,
        "requested_at": requested_at,
        "reason": bound_sentinel_reason(document.get("reason")),
    }


def bound_sentinel_reason(reason: object) -> str | None:
    """Collapse whitespace and truncate. Non-strings and blanks become ``None``.

    ``None`` rather than ``""`` so that a downstream ``if reason`` and a
    downstream ``if reason is not None`` cannot disagree about whether an
    operator supplied one.
    """
    if not isinstance(reason, str):
        # Deliberately not `str(reason)`: coercing a dict would put
        # "{'injected': True}" into an operator-facing comment.
        return None
    collapsed = " ".join(reason.split())
    if not collapsed:
        return None
    if len(collapsed) <= MAX_SENTINEL_REASON_LENGTH:
        return collapsed
    return collapsed[: MAX_SENTINEL_REASON_LENGTH - 1] + "…"


def _coerce_generation(generation: int | str | None) -> int | None:
    """Accept the generation as an int or its decimal string form.

    It reaches callers both ways: as the int ``register_control_endpoint``
    returned and as the ``ADP_CONTROL_GENERATION`` string handed to the child
    process. Anything else — ``None``, blank, non-numeric, zero or negative — is
    not a usable binding and yields ``None`` so the read refuses.
    """
    if isinstance(generation, bool):
        return None
    if isinstance(generation, int):
        return generation if generation >= 1 else None
    if isinstance(generation, str):
        try:
            value = int(generation.strip())
        except ValueError:
            return None
        return value if value >= 1 else None
    return None
