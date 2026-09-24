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
import hashlib
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

# The audience and action of an abort *receipt* — Issue #3963 review finding 1.
#
# Must match ABORT_RECEIPT_AUDIENCE / ABORT_RECEIPT_ACTION in
# src/agentauth/envelope.py. Both are deliberately distinct from the control
# envelope's, and the separation is the whole point of the artifact.
#
# A control envelope and a receipt answer different questions. The envelope says
# "an operator was authorized to abort this run" — it is minted when the command is
# *issued*, and stays valid for its full TTL whether or not the run ever took the
# command, including when the live re-check later refused it. The receipt says "the
# gateway accepted this abort against a live run, and recorded durable intent before
# saying so". Only the second fact justifies deleting the queue message and
# reporting a deliberate stop.
#
# If they shared an audience, an issuance envelope would satisfy the receipt check
# by itself and the distinction would collapse — the finalizer would again be unable
# to tell "someone asked" from "this run was actually stopped on purpose", which is
# the gap the receipt exists to close. Audience separation is what makes possession
# of a receipt evidence of acceptance rather than of a request.
ABORT_RECEIPT_AUDIENCE = "adp-agent-abort-receipt"
ABORT_RECEIPT_ACTION = "abort_accepted"

# Mirrors MAX_SENTINEL_ENVELOPE_LENGTH in control-abort-sentinel.ts and the byte
# ceiling both envelope verifiers apply.
MAX_SENTINEL_ENVELOPE_BYTES = 8192

# Claims this verifier requires, including `body_digest` — Issue #3963.
#
# An earlier revision deliberately omitted `body_digest` on the reasoning that the
# supervisor never saw the HTTP request body, so it had nothing to compare the
# digest against. That reasoning was sound about the mechanism and wrong about the
# consequence: it left the *reason text* outside everything the signature covers.
# The envelope proved an operator authorized an abort of this run; the words shown
# as that operator's went on being read from a plain field of a file the agent's
# own `Bash` tool can write. A real reproduction minted a valid envelope for the
# reason "original operator reason", wrote it into a correctly-bound sentinel whose
# reason said something else, and this resolver attributed the substituted text to
# the human.
#
# The fix is to stop the supervisor being blind to the bytes rather than to keep
# reasoning about why blindness is acceptable: the sentinel now carries the exact
# signed request body (`body_base64`), and `verify_abort_authorization` requires
# sha256 of those bytes to equal this claim. The digest was always signed — what
# was missing was the preimage, and the writer already had it.
_REQUIRED_CLAIMS = (
    "body_digest",
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

# Bound on the operator reason, applied where the reason is *derived* — see
# `authorized_abort_reason`. Matches MAX_SENTINEL_REASON_LENGTH on the writing
# side.
#
# Applied even though the text arrives inside signed bytes. The signature proves
# the operator sent it; it says nothing about the length being sensible for a
# GitHub comment, and the listener's own bound (1000 chars) is looser than this
# one. Whitespace is collapsed for the same reason: the text is interpolated into
# a comment and must not be able to forge its layout.
MAX_SENTINEL_REASON_LENGTH = 200

# Bound on the recorded signed body. The listener caps a control request body at
# 16 KiB (`MAX_BODY_BYTES`), and base64 inflates by 4/3, so this is that ceiling
# plus padding. The bytes are hashed, never executed or interpolated, but they are
# still attacker-influenced input read during teardown and get a bound like every
# other field here.
MAX_SIGNED_BODY_BYTES = 4 * ((16 * 1024 + 2) // 3) + 4

# Byte ceiling on the whole file, so that reading it during teardown cannot mean
# loading an arbitrary /tmp file into memory. Mirrors MAX_SENTINEL_BYTES in
# control-abort-sentinel.ts, and like that constant it is DERIVED from the two
# fields that are large by nature rather than written as a round number.
#
# It was a flat 8192 while the document has to hold a base64 signed body of up to
# MAX_SIGNED_BODY_BYTES (21852) plus an envelope of up to
# MAX_SENTINEL_ENVELOPE_BYTES. A control request the listener fully accepts — its
# body cap is 16 KiB, and JSON whitespace in a two-field body reaches it — therefore
# produced a document the TypeScript writer stored and this reader then refused on
# size. The operator was told the abort was recorded, and finalization fell back to
# exit-code classification: a deliberate stop reported as a crash, the exact
# mislabelling this module exists to prevent.
#
# The 2048-byte tail is slack for the short fields and JSON punctuation (version,
# run id, generation, UUID command id, ISO timestamp, keys). It is not a budget for
# anything.
#
# `2 *` the envelope bound, because as of review finding 1 a genuine document holds
# TWO independent gateway tokens: the issuance `envelope` and the `abort_receipt`
# that attests acceptance. Budgeting for one was the same defect described above,
# reintroduced — a document carrying a large envelope and a large receipt would be
# written successfully and then refused here on size, so the operator would be told
# the abort was recorded while finalization fell back to calling it a crash. The
# ceiling has to admit every document the writer can legitimately produce.
MAX_SENTINEL_BYTES = MAX_SIGNED_BODY_BYTES + 2 * MAX_SENTINEL_ENVELOPE_BYTES + 2048

# A generation as both halves define it. `re.fullmatch` with an explicit ASCII
# class rather than `\d`, which in Python matches Unicode decimal digits too.
_ASCII_DIGITS = re.compile(r"[0-9]+")

# The largest integer both runtimes represent exactly. JavaScript numbers are IEEE
# doubles, so `parseStrictGeneration` already refuses anything above this via
# `Number.isSafeInteger`; Python's arbitrary-precision `int` accepted it happily.
# That was a divergence in the dangerous direction — Python is the half that
# deletes the queue message — so the bound is applied here too. Rejected rather
# than clamped: a generation this large is not a counter value either half
# produced, and silently mapping it to a different number would make a stale
# sentinel match a live run.
MAX_SAFE_GENERATION = 2**53 - 1

# NOTE: there was an `ACCEPTED_DELIVERY = "accepted"` constant here, and its removal
# is the substance of Issue #3963 review finding 1.
#
# The problem it addressed is real and still holds: a signed envelope proves the
# gateway *issued* an authorization, not that the authorization survived the live
# recheck performed immediately before the executor ran. `deliverAuthorized` re-asks
# the gateway and settles the command `rejected` when the grant has been revoked, the
# epoch moved or the human's membership lapsed — and a rejected command leaves a
# perfectly valid envelope behind. Honouring that envelope alone would let an
# explicitly denied abort finalize the run.
#
# The attempted fix was to have the writer record which of the two happened, and for
# this reader to require the string "accepted". That is not a check. The sentinel is a
# file at a fixed path, the agent process runs with a `Bash` tool, and so any code in
# the pod can write those eight characters beside a genuine-but-merely-issued envelope.
# Requiring the literal constrained only the honest writer, while the finalizer went on
# deleting queue messages and reporting deliberate stops on the strength of a claim the
# run made about itself.
#
# What replaces it is `abort_receipt`: a separate Ed25519 token the gateway mints
# during revalidation, only after the live re-check passed *and* durable abort intent
# was persisted, under its own audience and action. The pod cannot produce one. The
# `delivery` field is no longer read at all — see `read_abort_sentinel` — because a
# self-asserted field that looks like evidence is worse than no field.


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
        # be enforced against a size the reader never saw. Reading one unit past
        # the ceiling and rejecting on overflow enforces it against what was
        # really read, and never loads an arbitrarily large /tmp file into memory
        # during teardown.
        #
        # Opened in BINARY mode, which is the difference between a byte ceiling
        # and a character ceiling. A text-mode `read(n)` bounds *characters*: a
        # document of multibyte UTF-8 passes an `n`-byte check at up to 4n bytes,
        # so the declared 8192-byte limit admitted roughly 32 KiB of astral-plane
        # text. `MAX_SENTINEL_BYTES` is a byte limit on both sides — the
        # TypeScript writer measures `Buffer.byteLength` — so it is enforced on
        # bytes here, before any decoding allocates a string from them.
        with open(path, "rb") as handle:
            raw_bytes = handle.read(MAX_SENTINEL_BYTES + 1)
        if len(raw_bytes) > MAX_SENTINEL_BYTES:
            logger.warning(
                "Ignoring abort sentinel: exceeds the %d-byte ceiling",
                MAX_SENTINEL_BYTES,
            )
            return None

        # Decoded explicitly after the bound, and strictly: `errors="strict"` is
        # the default and is relied upon, because a sentinel containing invalid
        # UTF-8 is not a document this writer produced. The `UnicodeDecodeError`
        # is caught by the blanket handler below and becomes "no abort".
        document = json.loads(raw_bytes.decode("utf-8"))

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
    #
    # `_integral` rather than a bare `isinstance(..., int)` because the two JSON
    # parsers disagree about `1.0`. JavaScript has one number type, so
    # `JSON.parse('{"version":1.0}').version === 1` is true and the TS validator
    # accepts it; Python produces a `float`, which an int check rejects. That is a
    # divergence in the *safe* direction for version but the unsafe one for
    # generation, and a contract enforced asymmetrically is not a contract. Both
    # sides now define an integral number the same way: integer-valued, whether it
    # was written `1` or `1.0`.
    version = _integral(document.get("version"))
    if version is None:
        return None
    if version != ABORT_SENTINEL_VERSION:
        return None

    candidate_run = document.get("run_id")
    command_id = document.get("command_id")
    requested_at = document.get("requested_at")

    if not isinstance(candidate_run, str) or not candidate_run:
        return None
    # Booleans are excluded and `1.0` is accepted, on the same shared definition
    # the version uses above. Bounded at `MAX_SAFE_GENERATION` so a value the
    # TypeScript half cannot represent exactly is refused by both.
    candidate_generation = _integral(document.get("generation"))
    if candidate_generation is None:
        return None
    if candidate_generation < 1 or candidate_generation > MAX_SAFE_GENERATION:
        return None
    if not isinstance(command_id, str) or not command_id:
        return None
    if not isinstance(requested_at, str) or not requested_at:
        return None

    # The gateway's acceptance receipt — Issue #3963 review finding 1.
    #
    # Required here for *shape* only; its signature is verified in
    # `verify_abort_authorization` alongside the envelope's, because verification
    # needs the public keys and this function is pure document validation.
    #
    # Note what is deliberately NOT read: the document's own `delivery` field. A
    # previous revision required it to equal "accepted", which excluded only honest
    # writers — the field is written by the pod whose abort it attests. The receipt
    # replaces it because the pod cannot mint one.
    receipt = document.get("abort_receipt")
    if (
        not isinstance(receipt, str)
        or not receipt
        or len(receipt.encode("utf-8")) > MAX_SENTINEL_ENVELOPE_BYTES
    ):
        logger.warning("Ignoring abort sentinel: it carries no gateway acceptance receipt")
        return None

    # The exact bytes the operator's request was signed over. Required, because it
    # is what binds the reason below to the signature; a document without it cannot
    # have its operator-facing text verified and must not supply any.
    signed_body = document.get("signed_body_base64")
    if (
        not isinstance(signed_body, str)
        or not signed_body
        or len(signed_body) > MAX_SIGNED_BODY_BYTES
    ):
        logger.warning("Ignoring abort sentinel: it carries no bounded signed request body")
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
        "abort_receipt": receipt,
        "signed_body_base64": signed_body,
        # Deliberately NOT carried through from the document's own `reason` field.
        #
        # That field is what the reproduction exploited: it sits next to a genuine
        # envelope, is covered by no signature, and was being printed as the
        # operator's words. The operator-facing reason is derived instead from
        # `signed_body_base64` once `verify_abort_authorization` has confirmed
        # those bytes hash to the envelope's signed `body_digest` — so the text in
        # the closing comment is the text the operator actually sent, or there is
        # no text at all. `authorized_abort_reason` is that derivation.
        #
        # Dropping the key entirely rather than passing it through unverified: a
        # field named `reason` in this dict would be read as the reason by the next
        # caller who needs one, which is exactly how the gap arose.
        "envelope": envelope,
    }


def _verify_signed_token(
    token: object,
    *,
    audience: str,
    action: str,
    run_id: str,
    generation: int,
    command_id: object,
    keys: dict[str, Ed25519PublicKey],
) -> dict | None:
    """Verify one gateway-signed token and return its payload, or ``None``.

    Shared by the two tokens an abort sentinel carries — the command *envelope* and
    the acceptance *receipt* — because every structural check is identical between
    them and the only differences are the ``audience`` and ``action`` claims.
    Factored out rather than duplicated so the two cannot drift: a check tightened
    for one and forgotten for the other would leave the weaker token able to
    authorize the stronger claim.

    Verifies, in this order: token shape, required claims present, claim JSON types,
    algorithm, issuer, audience, then the Ed25519 signature — and only afterwards
    the action and the run/generation/command bindings. Signature first matters
    because until it checks out every claim is attacker-controlled text.

    ``audience`` is the separation that keeps these two tokens from substituting for
    each other. Both are real signatures by the same key over statements about the
    same abort, so without it an "operator asked to abort" envelope would satisfy a
    check meant to establish "the gateway accepted this abort".

    Bindings are compared against facts the caller knows independently: ``run_id`` is
    the message id this pod dequeued and ``generation`` is what its own
    ``register_control_endpoint`` returned. Comparing a token claim against a value
    read out of the same token would be a tautology.

    Returns the verified payload so the caller can use claims (``body_digest``, the
    timestamps) that only make sense once the signature holds.
    """
    if not isinstance(token, str) or not token:
        return None
    if len(token.encode("utf-8")) > MAX_SENTINEL_ENVELOPE_BYTES:
        return None

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_VERSION:
        return None
    body = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
    signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    payload = json.loads(body.decode("utf-8"))

    if not isinstance(payload, dict) or payload.get("v") != ENVELOPE_VERSION:
        return None
    if any(payload.get(claim) in (None, "") for claim in _REQUIRED_CLAIMS):
        return None
    # Types are required, not coerced. `str(value)` on hostile JSON is a silent
    # accept, and the two runtimes disagree about what it produces — `["k1"]`
    # becomes "k1" in JS and "['k1']" here.
    if any(not isinstance(payload[claim], str) for claim in _STRING_CLAIMS):
        return None
    if type(payload["target_generation"]) is not int:
        return None

    # `alg` is compared against a single permitted value and never dispatched on,
    # so `alg: "none"` is simply not in the list.
    if payload["alg"] != "ed25519":
        return None
    if payload["iss"] != ENVELOPE_ISSUER:
        return None
    if payload["aud"] != audience:
        # A signature over a different statement is still a real signature.
        # Audience separation is what stops one being replayed as the other.
        return None

    key = keys.get(payload["kid"])
    if key is None:
        return None
    # Verified before any claim is acted on.
    key.verify(signature, ENVELOPE_VERSION.encode("ascii") + b"." + body)

    if payload["action"] != action:
        logger.warning("Abort sentinel token is for a different action")
        return None
    if payload["target_run_id"] != run_id:
        logger.warning("Abort sentinel token names a different run")
        return None
    if payload["target_generation"] != generation:
        logger.warning("Abort sentinel token is bound to a superseded control generation")
        return None
    if payload["command_id"] != command_id:
        # Binds the token to the command the sentinel reports, so it cannot be
        # paired with a different command's record.
        logger.warning("Abort sentinel token does not match the recorded command")
        return None
    return payload


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

    Two Ed25519 tokens make the claim checkable, and **both** are required, because
    they answer different questions and neither answer alone is sufficient.

    The **envelope** answers "was an operator authorized to abort this run?" It is
    minted when the command is issued and binds the run, generation, command and a
    digest of the operator's request bytes — which is what lets the reason text be
    read from inside a signature instead of from beside one.

    The **receipt** answers "did the gateway actually accept this abort against a
    live run?" That is a different fact, and it is the one that justifies deleting a
    queue message. An envelope stays valid for its whole TTL whether or not the run
    ever took the command, including when the live re-check refused it — so an
    envelope alone cannot distinguish an abort that happened from one that was merely
    requested, or even explicitly denied. The gateway mints the receipt only after
    that re-check passed *and* durable abort intent was persisted, under a distinct
    audience and action so the envelope cannot stand in for it.

    Why signatures at all: the signing key exists only in the gateway. This image
    holds public verification keys and has no signing path (see
    ``run_identity._verification_keys``), so these two tokens are the only artifacts
    here that could not have been produced from inside the pod. Every other field is
    self-asserted, which is why the previous ``delivery: "accepted"`` string proved
    nothing (#3963 review finding 1).

    Bindings are checked against facts this process knows independently — ``run_id``
    is the message id it dequeued and ``generation`` is the value its own
    ``register_control_endpoint`` call returned. Comparing a token claim against a
    value taken from the same token would be a tautology.

    Returns ``False``, never raises: a verification failure means the abort is not
    *proven*, which the caller must treat as "finalize by exit code" — the
    pre-feature behaviour. Raising here would risk the SQS acknowledgement.
    """
    try:
        keys = public_keys if public_keys is not None else load_model_policy_verification_keys()
        if not keys:
            # Fail closed, the same direction the listener takes when it has no
            # keys: "cannot check" must mean "refuse", not "allow".
            logger.warning("No control envelope verification keys; cannot prove the abort")
            return False

        command_id = sentinel.get("command_id")
        payload = _verify_signed_token(
            sentinel.get("envelope"),
            audience=CONTROL_ENVELOPE_AUDIENCE,
            action=_ABORT_ACTION,
            run_id=run_id,
            generation=generation,
            command_id=command_id,
            keys=keys,
        )
        if payload is None:
            logger.warning("Abort sentinel carries no valid gateway authorization; not honouring it")
            return False

        # The gateway's acceptance receipt — Issue #3963 review finding 1.
        #
        # Verified with the same rigor and the same independently-known bindings as the
        # envelope, differing only in audience and action. This is the check that
        # distinguishes an abort the gateway *accepted* from one an operator merely
        # requested: the receipt is minted only after the live re-check passed and
        # durable intent was recorded, so a fabricated field, a replayed issuance
        # envelope, or a command refused at delivery all fail here.
        #
        # Required, not optional. There is no fallback, because every weaker signal
        # available at this point is something the pod could have written about itself.
        if (
            _verify_signed_token(
                sentinel.get("abort_receipt"),
                audience=ABORT_RECEIPT_AUDIENCE,
                action=ABORT_RECEIPT_ACTION,
                run_id=run_id,
                generation=generation,
                command_id=command_id,
                keys=keys,
            )
            is None
        ):
            logger.warning(
                "Abort sentinel has no valid gateway acceptance receipt; it proves only "
                "that an abort was requested, not that this run accepted one"
            )
            return False

        # The request body binding — Issue #3963.
        #
        # This is what makes the operator's *words* as trustworthy as their
        # permission to stop the run. `body_digest` is a signed claim over the exact
        # bytes the gateway authorized; the sentinel carries those bytes, and the
        # two must agree. Without this check the envelope proved only "an abort of
        # this run was authorized", leaving the reason text free for any code in the
        # pod to choose — which a real-signature reproduction demonstrated by
        # substituting the reason under a valid envelope and having it attributed to
        # the human who authorized the abort.
        #
        # Checked here rather than at parse time because it needs the verified
        # payload: comparing against an unverified `body_digest` would be comparing
        # attacker-supplied bytes to an attacker-supplied hash of them.
        recorded_body = sentinel.get("signed_body_base64")
        if not isinstance(recorded_body, str) or not recorded_body:
            logger.warning("Abort authorization cannot be bound: no signed request body recorded")
            return False
        if len(recorded_body) > MAX_SIGNED_BODY_BYTES:
            return False
        # `validate=True`: without it, base64 silently discards characters outside
        # the alphabet, so two different recorded strings could decode to the same
        # bytes and the stored value would not be the one that was checked.
        body_bytes = base64.b64decode(recorded_body, validate=True)
        if hashlib.sha256(body_bytes).hexdigest() != payload["body_digest"]:
            logger.warning(
                "Abort authorization does not cover the recorded request body; "
                "refusing to attribute its contents to an operator"
            )
            return False

        # The command id inside the signed bytes must also be this command. The
        # digest proves the bytes were signed; this proves they were signed for the
        # command this sentinel claims, so a body legitimately signed for one
        # command cannot be presented alongside another command's envelope.
        body_payload = json.loads(body_bytes.decode("utf-8"))
        if (
            not isinstance(body_payload, dict)
            or body_payload.get("command_id") != payload["command_id"]
        ):
            logger.warning("Signed request body names a different command")
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


def _integral(value: object) -> int | None:
    """An integer-valued JSON number as *both* runtimes define one, else ``None``.

    Exists because the two JSON parsers do not agree by default, and the sentinel
    is one contract read by both:

    * ``true`` — ``isinstance(True, int)`` is True in Python, so a bare int check
      reads a JSON boolean as 1. JavaScript's ``!==`` never did. Excluded.
    * ``1.0`` — JavaScript has a single number type, so ``1.0`` *is* the integer 1
      and ``Number.isInteger`` accepts it; Python produces a ``float``, which a
      bare int check rejects. Accepted, because rejecting it here while the writer
      accepts it is a document one half honours and the other refuses.
    * ``1.5`` — integer-valued in neither. Rejected.

    Returns the value as an ``int`` so callers compare integers regardless of which
    JSON spelling arrived.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # `is_integer` rather than `value == int(value)`: the latter raises on nan
        # and infinity, which `json.loads` produces for `NaN`/`Infinity` literals.
        return int(value) if value.is_integer() else None
    return None


def authorized_abort_reason(sentinel: dict) -> str | None:
    """The operator's reason, taken from the bytes the gateway signed — #3963.

    Call only on a sentinel that :func:`verify_abort_authorization` has already
    accepted. That function proves ``signed_body_base64`` hashes to the envelope's
    signed ``body_digest``, so the reason parsed out of those bytes is the reason
    the operator actually submitted — not a sibling field of a file the agent's own
    shell could have written.

    Returns ``None`` when the operator supplied no reason, or when the signed body
    does not carry a usable one. ``None`` means "no verified reason", and the
    caller must then say nothing about a reason rather than fall back to the
    document's unsigned field: falling back is precisely the behaviour that let
    fabricated text be attributed to a human.

    Never raises; it runs on the teardown path with everything else here.
    """
    try:
        recorded = sentinel.get("signed_body_base64")
        if not isinstance(recorded, str) or not recorded:
            return None
        payload = json.loads(base64.b64decode(recorded, validate=True).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        # Re-bounded on the way out even though it came from signed bytes. The
        # signature proves the operator sent this text; it says nothing about the
        # text being a sensible length for a GitHub comment, and the listener's own
        # bound (1000 chars) is looser than this one.
        return bound_sentinel_reason(payload.get("reason"))
    except Exception:  # noqa: BLE001 - teardown path; must never raise
        return None


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
