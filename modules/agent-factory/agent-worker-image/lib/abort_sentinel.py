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

import base64
import json
import logging
import re
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .run_identity import (
    ENVELOPE_ISSUER,
    ENVELOPE_VERSION,
    MAX_ENVELOPE_TTL_SECONDS,
    load_model_policy_verification_keys,
)

logger = logging.getLogger(__name__)

# The audience the gateway stamps on a control-command envelope. Must match
# ENVELOPE_AUDIENCE in src/agentauth/envelope.py and control-envelope.ts. An
# envelope minted for the model-policy audience is a valid signature over a
# *different* decision and must not be accepted as an abort authorization.
CONTROL_ENVELOPE_AUDIENCE = "adp-agent-control-listener"

# The only action an abort sentinel's envelope may authorize.
_ABORT_ACTION = "abort"

# Mirrors MAX_SENTINEL_ENVELOPE_LENGTH in control-abort-sentinel.ts and the byte
# ceiling both envelope verifiers apply.
MAX_SENTINEL_ENVELOPE_BYTES = 8192

# Claims this verifier requires. A subset of the envelope's full claim set: the
# supervisor deliberately does not check `body_digest`, because it never saw the
# HTTP request body — that binding is the listener's to enforce, and the listener
# already did before the command was admitted. Claiming to check it here would
# mean either inventing the bytes or skipping the check while implying otherwise.
_REQUIRED_CLAIMS = (
    "iss",
    "aud",
    "alg",
    "kid",
    "tenant_id",
    "principal",
    "target_run_id",
    "target_generation",
    "action",
    "command_id",
    "iat",
    "nbf",
    "exp",
)

_STRING_CLAIMS = tuple(claim for claim in _REQUIRED_CLAIMS if claim != "target_generation")

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

# A generation as both halves define it. `re.fullmatch` with an explicit ASCII
# class rather than `\d`, which in Python matches Unicode decimal digits too.
_ASCII_DIGITS = re.compile(r"[0-9]+")


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

        # Bound the bytes actually read, not a previously-stat'd size. `getsize`
        # followed by an unbounded `json.load` is racy in the one direction that
        # matters: the file can grow between the two calls, so the ceiling would
        # be enforced against a size the reader never saw. Reading one byte past
        # the ceiling and rejecting on overflow enforces it against the real
        # bytes, and never loads an arbitrarily large /tmp file into memory
        # during teardown.
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(MAX_SENTINEL_BYTES + 1)
        if len(raw) > MAX_SENTINEL_BYTES:
            logger.warning(
                "Ignoring abort sentinel: exceeds the %d-byte ceiling",
                MAX_SENTINEL_BYTES,
            )
            return None

        document = json.loads(raw)

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

    # `isinstance(True, int)` is True in Python, so a JSON `true` would compare
    # equal to schema version 1 and validate. TypeScript's strict `!==` rejects
    # it, so accepting it here was a real cross-language divergence: a document
    # this reader honoured and the writer's own validator refused. The version
    # must be an actual integer, not a bool that happens to equal one.
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if version != ABORT_SENTINEL_VERSION:
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

    # The envelope is surfaced, never judged here: validation answers "is this a
    # well-formed sentinel for this run", and authorization is the separate
    # question `verify_abort_authorization` answers. Keeping them apart is what
    # lets the caller tell an unauthorized abort from a corrupt file — collapsing
    # both into `None` would make those two indistinguishable, and they call for
    # different handling.
    envelope = document.get("envelope")
    if (
        not isinstance(envelope, str)
        or not envelope
        or len(envelope.encode("utf-8")) > MAX_SENTINEL_ENVELOPE_BYTES
    ):
        envelope = None

    return {
        "version": ABORT_SENTINEL_VERSION,
        "run_id": candidate_run,
        "generation": candidate_generation,
        "command_id": command_id,
        "requested_at": requested_at,
        "reason": bound_sentinel_reason(document.get("reason")),
        "envelope": envelope,
    }


def verify_abort_authorization(
    sentinel: dict,
    *,
    run_id: str,
    generation: int,
    public_keys: dict[str, Ed25519PublicKey] | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether the gateway authorized this abort. ``False`` means "not proven".

    This is the answer to the question the sentinel's own fields cannot answer.
    Every field in the document is self-asserted: the agent process runs with a
    ``Bash`` tool, so any code in the pod can write a file at the sentinel path
    claiming this run was aborted. A reader that honoured that file would delete a
    live run's queue message and report a crash as a deliberate stop, on the
    strength of a document the run wrote about itself.

    The envelope is what makes the claim checkable. It is an Ed25519 token the
    *gateway* minted for this exact command, and the signing key exists only in
    the gateway: this image holds public verification keys and has no signing path
    at all (see ``run_identity._verification_keys``). So a valid envelope is the
    one artifact in the pod that could not have been produced from inside it.

    The bindings below are checked against facts this process knows
    independently — ``run_id`` is the message id it dequeued and ``generation`` is
    the value its own ``register_control_endpoint`` call returned. Comparing an
    envelope claim against a value taken from the envelope would be a tautology.

    Returns ``False``, never raises: a verification failure means the abort is not
    *proven*, which the caller must treat as "finalize by exit code" — the
    pre-feature behaviour. Raising here would risk the SQS acknowledgement.
    """
    try:
        token = sentinel.get("envelope")
        if not isinstance(token, str) or not token:
            # No proof offered. Not an error and not an authorization.
            logger.warning("Abort sentinel carries no gateway authorization; not honouring it")
            return False
        if len(token.encode("utf-8")) > MAX_SENTINEL_ENVELOPE_BYTES:
            return False

        keys = public_keys if public_keys is not None else load_model_policy_verification_keys()
        if not keys:
            # Fail closed, the same direction the listener takes when it has no
            # keys: "cannot check" must mean "refuse", not "allow".
            logger.warning("No control envelope verification keys; cannot prove the abort")
            return False

        parts = token.split(".")
        if len(parts) != 3 or parts[0] != ENVELOPE_VERSION:
            return False
        body = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        payload = json.loads(body.decode("utf-8"))

        if not isinstance(payload, dict) or payload.get("v") != ENVELOPE_VERSION:
            return False
        if any(payload.get(claim) in (None, "") for claim in _REQUIRED_CLAIMS):
            return False
        # Types are required, not coerced. `str(value)` on hostile JSON is a
        # silent accept, and the two runtimes disagree about what it produces —
        # `["k1"]` becomes "k1" in JS and "['k1']" here.
        if any(not isinstance(payload[claim], str) for claim in _STRING_CLAIMS):
            return False
        if type(payload["target_generation"]) is not int:
            return False

        # `alg` is compared against a single permitted value and never dispatched
        # on, so `alg: "none"` is simply not in the list.
        if payload["alg"] != "ed25519":
            return False
        if payload["iss"] != ENVELOPE_ISSUER:
            return False
        if payload["aud"] != CONTROL_ENVELOPE_AUDIENCE:
            # A signature over a model-policy decision is a real signature over a
            # different statement. Audience separation is what stops one being
            # replayed as the other.
            return False

        key = keys.get(payload["kid"])
        if key is None:
            return False
        # Verified before any claim is acted on: until the signature checks out,
        # every field above is attacker-controlled text.
        key.verify(signature, ENVELOPE_VERSION.encode("ascii") + b"." + body)

        if payload["action"] != _ABORT_ACTION:
            # An envelope for `pause` is a genuine authorization for something
            # else. Without this check a pause could be replayed as an abort.
            logger.warning("Abort sentinel authorization is for a different action")
            return False
        if payload["target_run_id"] != run_id:
            logger.warning("Abort authorization names a different run")
            return False
        if payload["target_generation"] != generation:
            logger.warning("Abort authorization is bound to a superseded control generation")
            return False
        if payload["command_id"] != sentinel.get("command_id"):
            # Binds the proof to the command the sentinel reports, so an envelope
            # cannot be paired with a different command's record.
            logger.warning("Abort authorization does not match the recorded command")
            return False

        issued = _parse_envelope_timestamp(payload["iat"])
        not_before = _parse_envelope_timestamp(payload["nbf"])
        expires = _parse_envelope_timestamp(payload["exp"])
        if issued is None or not_before is None or expires is None:
            return False
        if issued > not_before:
            return False
        if (expires - not_before).total_seconds() > MAX_ENVELOPE_TTL_SECONDS:
            # A signer may not overrule the platform's revocation-delay bound by
            # claiming a longer life. Rejected rather than truncated.
            return False

        # Deliberately NOT an expiry check against the current time.
        #
        # The envelope's life is 30 seconds and finalization happens after the run
        # winds down, which is routinely longer. What is being established here is
        # an authenticated *historical* fact — "the gateway authorized this abort
        # for this run and generation" — not a live grant. Liveness was already
        # enforced where it belongs: the listener verified the envelope within its
        # window, and `deliverAuthorized` re-checked the grant against the gateway
        # immediately before the executor ran. Requiring an unexpired envelope
        # here would reject every real abort and make the feature useless.
        #
        # The replay this leaves open is bounded to exactly the run and generation
        # the envelope names, and `now` is accepted so the bound is testable.
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if reference < not_before:
            # A proof that was not yet valid when the run ended cannot describe it.
            logger.warning("Abort authorization is not yet valid")
            return False
        return True
    except (InvalidSignature, ValueError, TypeError, UnicodeDecodeError, OSError):
        # Every failure is the same answer: the abort is not proven.
        logger.warning("Abort sentinel authorization could not be verified")
        return False
    except Exception:  # noqa: BLE001 - runs during teardown; must never raise
        logger.warning("Abort sentinel authorization could not be verified")
        return False


def _parse_envelope_timestamp(value: object) -> datetime | None:
    """Parse the envelope's ``%Y-%m-%dT%H:%M:%SZ`` form, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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
        # Not `int()`: it accepts PEP-515 underscores (``"1_0"`` -> 10), a
        # leading ``+``, and non-ASCII decimal digits (``"٣"`` -> 3), none of
        # which the TypeScript side's digit test accepts. The generation is the
        # sentinel's staleness defence, so both halves must agree on exactly
        # what a generation is: a bare run of ASCII digits.
        text = generation.strip()
        if not _ASCII_DIGITS.fullmatch(text):
            return None
        value = int(text)
        return value if value >= 1 else None
    return None
