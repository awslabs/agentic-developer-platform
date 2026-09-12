"""Versioned request/response schemas for the live run-control channel (#3960).

Two audiences share these models, and the split between them is the security
boundary this module exists to hold:

**Public models** (``ControlStateResponse``, ``ControlCommandResponse``) are what
a browser receives. They deliberately have no field for the pod's address, its
port, or its per-run bearer token. The omission is the control: a response model
that *could* carry the token is one refactor away from carrying it, and the
private half of the control record is the thing that lets a caller talk to the
pod directly. Internal fields live in :class:`ControlTarget` in
``control_service.py``, which is never a response model anywhere.

**Request models** set ``extra="forbid"``, so a body carrying ``actor``,
``target``, ``token`` or a pod address is rejected rather than quietly ignored.
Actor attribution is resolved from the authenticated session, and the transport
target is resolved from the registered control record; neither is readable off
the wire. Rejecting loudly is right here because the only reason to send those
fields is to try to override them (FR-7.3, AC-S7).

Vocabulary note (revival-design §2): ``ControlState`` is a *transient control
phase*, not an addition to the invocation terminal-status vocabulary in
``activity/liveness.py``. A run whose control phase is ``terminal`` has whatever
terminal invocation status its row records; the two sets are read by different
consumers and are deliberately not unified.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The four verbs the control channel routes. Verb-agnostic by construction: the
# listener, the NetworkPolicy and the authorization path all treat these
# uniformly, so a later story enables one by flipping its capability rather than
# by adding a route or a policy rule (ADR-9, FR-1.10).
ControlAction = Literal["pause", "resume", "steer", "abort"]

# The transient control phase of a run. `unavailable` is NOT an error state — it
# is the honest answer when the row exists but carries no reachable control
# registration, which is the ordinary case for every run started before this
# feature or with the flag off (FR-1.13).
ControlState = Literal[
    "running",
    "pause_requested",
    "paused",
    "abort_requested",
    "terminal",
    "unavailable",
]

# The lifecycle of one submitted command, keyed by its client-supplied
# ``command_id``.
#
# `delivered` means the worker handed the command to the SDK at a real boundary.
# It does NOT mean the model understood or acted on it — FR-7.12 separates
# delivery from comprehension precisely so the UI never blocks on a
# model-generated acknowledgement. `unknown` is the required answer for an
# expired ID, an unrecognised ID, or a worker-generation change: reporting such a
# command as `delivered` would be a lie, and replaying it could double-apply a
# command the previous generation already consumed (revival-design §2).
CommandStatus = Literal["pending", "delivered", "applied", "cancelled", "rejected", "unknown"]

# Bounds on client-supplied text. Enforced in the schema (not only at the
# transport layer) so an oversized instruction is a 400 from the model rather
# than something the worker has to defend against.
MAX_INSTRUCTION_CHARS = 4000
MAX_REASON_CHARS = 1000

# Total request-body cap, enforced by the route before parsing (413, FR-7.8).
# 16 KiB leaves generous headroom over MAX_INSTRUCTION_CHARS while keeping a
# hostile body from reaching the JSON parser at all.
MAX_REQUEST_BYTES = 16 * 1024


class ControlCapabilities(BaseModel):
    """Which verbs this deployment can actually perform right now.

    Every field defaults to ``False`` and S1 ships them all ``False``. This is
    the field the dashboard reads to decide whether to *offer* a control, which
    is why the default matters: a capability that defaulted true would advertise
    a button whose handler returns 501, and an operator who believes a run is
    pausing stops watching it. Each later story flips exactly its own flag once
    its behaviour is proven (revival-design §2).
    """

    model_config = ConfigDict(extra="forbid")

    pause: bool = False
    resume: bool = False
    steer: bool = False
    abort: bool = False


class CommandAcknowledgement(BaseModel):
    """One entry in the bounded acknowledgement journal.

    Carries no instruction text. The journal is a delivery ledger read by a
    polling dashboard, and echoing the steering text back into every state
    response would put untrusted user input on a hot read path for no benefit —
    the submitter already knows what they sent.
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str
    action: ControlAction
    status: CommandStatus
    accepted_at: str | None = None
    delivered_at: str | None = None
    reason: str | None = None


class ControlStateResponse(BaseModel):
    """The polled read contract (``GET .../agent/state``).

    S7 renders this and nothing else, so it must be sufficient on its own: the
    reason a control is unavailable, the honest per-command status, and the
    generation the state belongs to. ``generation`` is what makes a stale poll
    detectable — a response from a different worker generation describes a
    process that no longer exists, and the UI must not attribute it to the
    current one.

    ``active_tool_count`` is populated by the pause story once it can observe
    tool admission. S1 reports ``None`` rather than ``0``: an unproven zero would
    read as "no tools running", which is exactly the false quiescence claim
    revival-design §4 forbids.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    generation: int | None = None
    available: bool = False
    reason: str | None = None
    capabilities: ControlCapabilities = Field(default_factory=ControlCapabilities)
    state: ControlState = "unavailable"
    active_tool_count: int | None = None
    updated_at: str | None = None
    commands: list[CommandAcknowledgement] = Field(default_factory=list)


class ControlCommandResponse(BaseModel):
    """The result of submitting one command.

    Extends the ``RunControlResponse`` shape declared by the orchestration seam
    additively (``run_id``, ``action``, ``state`` retained) so the existing
    declared contract is honoured rather than replaced (revival-design §2).

    The status code carries the meaning the body cannot: 202 is accepted and
    pending, 200 is already applied or applied synchronously. Neither means the
    model comprehended anything (FR-7.10).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    action: ControlAction
    state: ControlState
    command_id: str
    command_status: CommandStatus


class ControlCommandRequest(BaseModel):
    """Body for pause / resume / abort.

    ``command_id`` is client-supplied and is the idempotency key: the same ID
    with the same payload returns the recorded outcome without reapplying, and
    the same ID with different content is a 409. A client-supplied key is right
    here because the client is the only party that can tell a retry of one
    intent from two separate intents — a server-minted ID would make a retried
    abort look like a second abort.

    UUID format is required so the key space is collision-free without
    coordination.
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class ControlSteerRequest(BaseModel):
    """Body for steer — the one verb carrying free text.

    The instruction is untrusted user input destined for an agent prompt, so it
    is length-bounded here and passes the worker's existing untrusted-input
    wrapper before reaching the model (FR-6.10). ``min_length=1`` rejects an
    empty steer, which would otherwise be accepted and delivered as a no-op
    turn.
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str
    instruction: str = Field(min_length=1, max_length=MAX_INSTRUCTION_CHARS)


class ControlPingResponse(BaseModel):
    """The foundation slice (``GET .../agent/ping``).

    Proves the whole authenticated path — browser auth, tenant and owner
    authorization, registered-target resolution, NetworkPolicy, pod token — with
    no run-affecting side effect. It answers a reachability question, so it
    reports the generation it reached and nothing about the pod's location.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    available: bool
    reason: str | None = None
    generation: int | None = None
