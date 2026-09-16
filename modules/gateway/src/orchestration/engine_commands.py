"""Apply `@agent-engine` commands from GitHub comments on the tick (Issue #4527).

The inbound half of the GitHub↔engine bridge. Before this module a running plan
could only be driven from the web UI: the comment translator existed and nothing
in the running system called it. Here a human comments `@agent-engine halt` on the
issue in front of them and the plan halts on the engine's next wake.

--------------------------------------------------------------------------------
The delivery mechanism is a marked row, not a call
--------------------------------------------------------------------------------

The webhook Lambda recognises the tag, writes `engine_command_status=pending` onto
the event row it already writes, and returns. It enqueues nothing and calls no
gateway — that component holds no VPC, no database and no gateway reach, and #4303's
closed-routes table forbids it gaining any. This pass is the consumer: one DynamoDB
Query per wake against the sparse `engine-command-index`, oldest first.

So the two halves are coupled by a table and by two string constants, not by an
import. They live in separate deploy units (the Lambda is a zip under
`modules/agent-factory/webhook-ingress/lambda/`, this runs inside the gateway
container image), which is the same constraint that forces the Lambda's
`_emit_row_write_dropped` to re-implement the gateway's metric helper. The values
are therefore re-declared here and the pair is pinned by a test on each side.

--------------------------------------------------------------------------------
Nothing in a comment is trusted except as a reference
--------------------------------------------------------------------------------

A comment body is written by anyone who can comment on the issue. Each of the four
bug classes the issue names is answered by a structural property here, not by a
check somebody has to remember:

- **The tag must not reach persona dispatch.** Handled on the Lambda side, before
  the persona scan. Nothing in this module can spawn a pod: it holds no queue and
  builds no envelope.
- **The commenter's identity is resolved server-side.** The row carries a numeric
  GitHub account id — an opaque reference — and
  :func:`~.adapters.github_comments._resolve_platform_identity` turns it into a
  platform identity by looking it up in `user_identities` *inside one org*. No
  login, no display name and no claimed role is read from the payload. An account
  with no identity in the org is refused with the uniform message.
- **The org is resolved from the installation, never from the comment.** The row's
  tenant was resolved by the Lambda from the tenant registry keyed on the GitHub
  App installation, and this module additionally re-derives the org's installation
  through the *same* :func:`resolve_installation_id` dispatch uses and refuses a
  mismatch. A plan that can dispatch at all already satisfies that check, so the
  guard costs nothing real while closing the "row says tenant X" path.
- **Consume is idempotent.** The marker is flipped `pending -> consumed` with a
  conditional update, so a second tick that read the same row before the flip
  writes nothing further.
- **The flag gates everything, silently.** With
  ``FEATURE_ORCHESTRATION_ENGINE_ENABLED`` unset the pass makes no query, applies
  nothing and — deliberately — posts no ack. A "the engine is disabled" reply
  would tell anyone who can comment that the bridge exists and is switched off,
  and would turn a disabled feature into an unbounded comment generator.

--------------------------------------------------------------------------------
Authority is borrowed, never re-implemented
--------------------------------------------------------------------------------

Every command is gated on ``Permission.PLAN_APPROVE`` — the permission #4200
minted for writes into promotion state — and every state change goes through the
same three-guard shape the UI controls use: narrow *which* edge this caller may
request, let ``transition()`` decide legality for a HUMAN actor, then UPDATE
conditional on the observed state. Gate answers are not re-implemented at all:
they delegate to :func:`apply_gate_answer_for_context`, the shared core the
dashboard route already calls, so the comment path is the same code with a
different `input_path` stamped on the row rather than a second writer that agrees
today.

`actor_kind` is ``HUMAN`` on every write here, and that is correct rather than
convenient: the acting identity was resolved from a real linked account and
`PLAN_APPROVE` was checked against it. The engine is only the *transport*. What
the engine must never do — clear its own halt — remains impossible, because this
module never passes ``ActorKind.SERVICE`` and the recovery edges are
``_HUMAN_ONLY`` in `state.py`.

--------------------------------------------------------------------------------
What each verb does in v1
--------------------------------------------------------------------------------

``accept``
    Answers the flow's single outstanding gate. Not "compile a plan": acceptance
    *is* compilation in this schema (`compile.py`), and compiling needs a whole
    `LoopProposal` document, which a comment does not carry. Where the engine is
    actually waiting on a human there is exactly one thing "accept" can mean, and
    this is it. Two gates awaiting an answer is ambiguous and is refused — say
    `approve gate <n>`.
``approve gate <n>``
    Answers the gate node whose `node_ref` is ``<n>`` within the resolved flow.
``halt``
    Stops spend on the plan: every node with a human-legal edge to `halted` takes
    it, each recorded as ``NODE_HALTED``.
``resume``
    The inverse recovery: every `failed` / `halted` node returns to `ready`,
    recorded as ``NODE_STALLED`` / ``HALT_OVERRIDDEN`` exactly as the resume
    control records them.
``replan: <text>``
    **Records and notifies only.** One ``REPLAN_REQUESTED`` row with
    ``to_state = NULL``, so the row is structurally incapable of moving anything.
    Turning the request into an amended plan is the authoring loop, a separate
    story; recording it is what stops the request being lost in a comment thread.

--------------------------------------------------------------------------------
Ordering: commit, then consume, then acknowledge
--------------------------------------------------------------------------------

Like the dispatch pass, this one commits nothing and hands the caller the work to
flush afterwards (:func:`flush_engine_commands`). The order is deliberate and the
failure modes are asymmetric:

1. The caller commits the decisions and state changes.
2. The marker is flipped conditionally. A crash between 1 and 2 re-applies the
   command on a later tick — absorbed, because every write is conditional on the
   state it observed, so the second application changes nothing and merely acks a
   refusal.
3. The ack comment is posted. It is last because a failed comment must never be
   able to un-apply a committed decision.

Claiming the row *before* applying would invert that: a crash would lose the
command silently, and a human waiting for a plan to halt would never learn that
nothing happened. A duplicate no-op is the cheaper failure.

Every command gets a visible reply — applied or refused — because a bridge that
silently ignores malformed or unauthorized commands is indistinguishable from one
that is broken.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

from .adapters.github_commands import CommandVerb, EngineCommand, parse_engine_command

# Underscore import across modules inside this package, matching
# `diagnose.py`'s `from .tick import _ITEM_BACKSTOP, _PAGE_SIZE`. Deliberate: this
# is THE server-side GitHub-account-to-platform-identity resolution, and a second
# copy of it here is precisely the "two writers that agree today" failure the
# adapter package exists to prevent. It stays private to signal that it is not a
# general-purpose helper.
from .adapters.github_comments import (
    _UNIFORM_REFUSAL as _GATE_UNIFORM_REFUSAL,
)
from .adapters.github_comments import (
    GateAnswerStatus,
    InputPath,
    _resolve_platform_identity,
    apply_gate_answer_for_context,
)

# Issue #4539's verifier. Same package, so this IS an ordinary import — unlike the
# signer, which lives in the webhook-ingress Lambda zip and cannot be imported from
# here at all. That asymmetry is why the canonical form is written twice and pinned by
# a shared golden fixture rather than shared as code.
from .command_attribution import (
    SIGNATURE_ATTR,
    AttributionError,
    VerifiedCommand,
    verify_row,
)
from .dispatch_pass import resolve_installation_id
from .models import DecisionKind, NodeKind, OrchestrationFlow, OrchestrationNode
from .repository import OrchestrationRepository
from .state import ActorKind, NodeState, transition

logger = logging.getLogger("bedrockgateway.orchestration.engine_commands")

__all__ = [
    "ENGINE_COMMAND_STATUS_CONSUMED",
    "ENGINE_COMMAND_STATUS_PENDING",
    "ENGINE_COMMAND_STATUS_QUARANTINED",
    "EngineCommandConfig",
    "EngineCommandReport",
    "PendingEngineAck",
    "QuarantinedRow",
    "flush_engine_commands",
    "run_engine_command_pass",
]


# The engine's own fail-closed flag, read here rather than given one of its own —
# same reasoning as `diagnose.py`: a second flag would let the bridge accept
# commands against an engine that is switched off.
FEATURE_FLAG_ENV = "FEATURE_ORCHESTRATION_ENGINE_ENABLED"

# Must stay identical to `webhook_events.ENGINE_COMMAND_STATUS_*` in the
# webhook-ingress Lambda. Re-declared, not imported: separate deploy units (see the
# module docstring). A test on each side asserts the pair is equal.
ENGINE_COMMAND_STATUS_PENDING = "pending"
ENGINE_COMMAND_STATUS_CONSUMED = "consumed"

# Issue #4539: the terminal status for a row whose attribution did not verify.
# Deliberately NOT `consumed`:
#
#   * `consumed` means "a real command was read and answered". A row that never
#     established it came from a verified delivery was neither, and an operator
#     reading the table must be able to tell those apart — a forgery attempt that
#     looks identical to an applied command is an incident nobody can investigate.
#   * it is still a terminal value on the sparse index's partition key, so the
#     `pending` query stops returning the row. That is what stops the endless
#     re-read: an unverifiable row costs one verification once, not once per wake
#     forever.
ENGINE_COMMAND_STATUS_QUARANTINED = "quarantined"

# The sparse GSI the Lambda's marker makes this row visible on.
ENGINE_COMMAND_INDEX = "engine-command-index"

# The events table's name. Same env var the activity and stats services already
# read (`activity/service.py`), because it is the same table — a second var could
# point the bridge at a different one from the UI that renders its rows.
TABLE_ENV = "WEBHOOK_EVENTS_TABLE"

# Per-pass cap. A bound on how much a single wake will do, so a burst of comments
# (or a stuck marker) cannot turn one tick into an unbounded run of GitHub API
# calls. Work is delayed to the next wake, never dropped.
DEFAULT_MAX_COMMANDS_PER_PASS = 20
MAX_COMMANDS_PER_PASS_ENV = "BG_ORCH_MAX_COMMANDS_PER_PASS"

# Returned verbatim for every isolation refusal: unknown commenter, no such flow,
# ambiguous target, cross-tenant row. One string, so the cases are
# indistinguishable to whoever commented — two similarly-worded strings drift into
# distinguishable, and a distinguishable "no such flow" is an oracle for which
# plans exist in other tenants.
_UNIFORM_REFUSAL = "this command cannot be applied by this account"

# Provenance stamped on every decision row this pass writes. The comment path
# already has a declared input path; a third value would make "where did this
# approval come from?" answerable in two vocabularies.
_INPUT_PATH = InputPath.GITHUB_COMMENT

# Attribution for the transitions this pass effects. The *actor* is the resolved
# human; this role string records the authority they exercised, snapshotted at
# decision time exactly as the repository requires.
_COMMAND_ACTOR_ROLE_FALLBACK = "engine-command"

# States a `halt` may act from, mapped to the kind that records it. Narrower than
# `state.py`'s table on purpose: these are the two states in which a human halting
# a plan is stopping *live spend*, which is what the command means. `pending` and
# `ready` nodes have no legal edge to `halted` at all, so a plan that has not
# started is refused rather than silently marked halted.
_HALTABLE_STATES: dict[str, DecisionKind] = {
    NodeState.RUNNING.value: DecisionKind.NODE_HALTED,
    NodeState.AWAITING_GATE.value: DecisionKind.NODE_HALTED,
}

# States a `resume` may recover from, mapped to the kind that records it. The
# same two entries, and the same two kinds, as `controls._RESUMABLE_STATES`.
#
# Re-derived rather than imported: `controls.py` is a FastAPI route module on the
# operator plane, and importing it would put the whole request stack on the tick's
# import path, so a route-layer import error would become a tick outage. The two
# maps are asserted equal by a test instead — the same "pinned by a test rather
# than shared as code" trade this module already makes across the deploy-unit
# boundary.
_RESUMABLE_STATES: dict[str, DecisionKind] = {
    NodeState.FAILED.value: DecisionKind.NODE_STALLED,
    NodeState.HALTED.value: DecisionKind.HALT_OVERRIDDEN,
}


@dataclass(frozen=True)
class EngineCommandConfig:
    """Whether the bridge runs, which table it reads, and how much per pass.

    Frozen: read once per pass, so a mid-pass mutation could not have a coherent
    meaning.
    """

    enabled: bool = False
    table_name: str = ""
    aws_region: str = "us-east-1"
    max_commands_per_pass: int = DEFAULT_MAX_COMMANDS_PER_PASS

    @property
    def configured(self) -> bool:
        """Whether this config can actually read a command.

        An empty table name means there is nothing to query. Reported as a
        disabled pass rather than raising: the tick has four other passes to run.
        """
        return bool(self.table_name)

    @classmethod
    def from_env(cls) -> EngineCommandConfig:
        """Build from the process environment. **Fail-closed, and never raises.**

        Enabled only when the flag is the literal ``"true"`` (case-insensitive);
        absent, empty, ``"1"`` and ``"yes"` all resolve to *off*. That matches
        `features/routes.py::_is_enabled_strict`, hand-rolled here for the same
        reason `diagnose.py` hand-rolls it — importing a route module onto the tick
        path to read one boolean would make the tick depend on the request stack.

        Never raises: this runs on the tick path, and the alternative to a usable
        config is a broken tick. A malformed cap degrades to the default, which is
        the conservative value anyway.
        """
        enabled = (os.environ.get(FEATURE_FLAG_ENV) or "").strip().lower() == "true"

        raw_cap = (os.environ.get(MAX_COMMANDS_PER_PASS_ENV) or "").strip()
        cap = DEFAULT_MAX_COMMANDS_PER_PASS
        if raw_cap:
            try:
                cap = int(raw_cap)
                if cap < 1:
                    raise ValueError(f"cap must be at least 1; got {cap}")
            except ValueError as exc:
                logger.warning(
                    "orchestration engine commands: %s=%r is not a usable cap (%s); using default %d",
                    MAX_COMMANDS_PER_PASS_ENV,
                    raw_cap,
                    exc,
                    DEFAULT_MAX_COMMANDS_PER_PASS,
                )
                cap = DEFAULT_MAX_COMMANDS_PER_PASS

        return cls(
            enabled=enabled,
            table_name=(os.environ.get(TABLE_ENV) or "").strip(),
            aws_region=os.environ.get("AWS_REGION") or os.environ.get("BG_AWS_REGION") or "us-east-1",
            max_commands_per_pass=cap,
        )


@dataclass(frozen=True)
class PendingEngineAck:
    """One committed command's consume-and-acknowledge work.

    Mirrors `dispatch_pass.PendingPublish` and exists for the same reason: the
    database write commits before anything leaves the process, so the remaining
    intent is held as data between the two phases rather than being an accident of
    where the `await` happens.

    `installation_id` is resolved during the pass, while a session is open, so the
    flush needs no database at all.
    """

    event_id: str
    arrived_at: str
    org_id: str
    repo: str
    issue_number: int
    installation_id: int | None
    message: str


@dataclass(frozen=True)
class QuarantinedRow:
    """One row whose attribution did not verify, held for the flush to seal off.

    A separate type from :class:`PendingEngineAck`, not a `message=""` special case
    of it, because the two carry different *intent* and must not be able to drift
    into each other:

    * a `PendingEngineAck` may post a comment, addressed with a repository and an
      installation. A quarantined row must never produce a GitHub write at all —
      those fields came from whoever wrote the row, so posting with them is a
      confused-deputy write regardless of what the body says. Having no such fields
      on this type makes that impossible rather than merely intended.
    * its conditional write is bound to the signature bytes actually observed, so a
      concurrent legitimate rewrite of the row is not clobbered by a verdict reached
      about the *previous* content.

    `org_id` is the row's own unverified tenant string. It is a counter label and
    nothing else — never used to resolve a credential, read a plan, or route a reply.
    """

    event_id: str
    arrived_at: str
    org_id: str
    #: The signature exactly as read from the row, or `""` when the row carried
    #: none. The quarantine write is conditional on this still being the value in
    #: DynamoDB.
    observed_signature: str
    #: Bounded, sanitized :mod:`command_attribution` reason. Safe as a metric
    #: dimension: never derived from row content.
    reason: str


@dataclass
class EngineCommandReport:
    """What one engine-command pass did.

    Counters are the only way an operator can tell "nobody commented" from "every
    command was refused" — the commenter sees their own reply, and nothing else
    surfaces a refusal.
    """

    commands_read: int = 0
    commands_applied: int = 0
    # Read, tagged, and deliberately not acted on: unparseable body, unknown
    # commenter, missing permission, ambiguous or absent target. Counted as one
    # number because the *commenter* is told nothing that distinguishes them.
    commands_refused: int = 0
    # Issue #4539: rows whose attribution signature did not verify. Counted apart
    # from `commands_refused` because they are a different kind of event entirely: a
    # refusal is a real command the platform declined to apply, whereas a
    # quarantine is a row that never established it came from a verified delivery.
    # One number for both would let a forgery attempt hide inside ordinary
    # permission refusals, and would make "the key is not seeded in this
    # environment" indistinguishable from "people are sending commands they lack
    # permission for".
    commands_quarantined: int = 0
    consumes_failed: int = 0
    # Issue #4539: a quarantine write that did not land. The row therefore stays
    # `pending` and is verified again next wake — safe (verification is pure and it
    # will fail identically), but a row that can NEVER be sealed off is a permanent
    # loop, which is exactly the endless re-read this work has to prevent. Its own
    # counter, and it forces a non-success report.
    quarantines_failed: int = 0
    acks_posted: int = 0
    acks_failed: int = 0
    errors: int = 0
    # True when the per-pass cap stopped the pass early. Work is delayed, not
    # dropped — but "we ran out of budget" must not read as "there was nothing
    # to do".
    capped: bool = False
    # False when the flag is off or no table is configured. Surfaced so a disabled
    # path is visible as disabled rather than looking like a pass that found
    # nothing.
    enabled: bool = True
    pending: list[PendingEngineAck] = field(default_factory=list)
    #: Issue #4539: rows to seal off during the flush. Separate from `pending` so
    #: that the ack loop cannot reach them — see :class:`QuarantinedRow`.
    quarantined: list[QuarantinedRow] = field(default_factory=list)
    #: Issue #4539: how many rows failed verification for each sanitized reason.
    #: Bounded keys (the `command_attribution.REASON_*` set), so this is safe to emit
    #: as a metric dimension. Operationally this is the difference between "the key
    #: is not seeded in this environment" (every row `no_verification_key`) and
    #: "somebody is writing rows" (`invalid_signature`), which are the same number in
    #: `commands_quarantined` and require opposite responses.
    quarantine_reasons: dict[str, int] = field(default_factory=dict)
    per_org: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """A refusal is not a failure; an undelivered ack is.

        `acks_failed` counts commands that were applied and whose author was never
        told. That is the invisible outcome this story exists to remove, so it
        forces a non-success report rather than being a footnote.
        """
        return self.errors == 0 and self.consumes_failed == 0 and self.acks_failed == 0 and self.quarantines_failed == 0

    def _org(self, org_id: str) -> dict[str, int]:
        """Per-org counters, seeded with this report's own key set.

        The key set is seeded explicitly (as in `DispatchPassReport`) so that
        :meth:`record` raises on a typo'd key rather than silently inventing a
        counter nobody reads.
        """
        return self.per_org.setdefault(
            org_id,
            {
                "commands_read": 0,
                "commands_applied": 0,
                "commands_refused": 0,
                "commands_quarantined": 0,
                "consumes_failed": 0,
                "quarantines_failed": 0,
                "acks_posted": 0,
                "acks_failed": 0,
                "errors": 0,
            },
        )

    def record(self, org_id: str, key: str, amount: int = 1) -> None:
        """Increment a counter both in total and for one org."""
        setattr(self, key, getattr(self, key) + amount)
        self._org(org_id)[key] += amount

    def record_quarantine_reason(self, reason: str) -> None:
        """Tally one sanitized verification-failure reason.

        Kept off :meth:`record` because this is a dict of bounded string keys rather
        than a fixed counter set — passing it through `record` would mean `setattr`
        on a name that is not an attribute.
        """
        self.quarantine_reasons[reason] = self.quarantine_reasons.get(reason, 0) + 1


def _issue_ref_candidates(issue_number: int) -> list[str]:
    """The spellings of one issue number as `issue_ref` / `intent_ref` may hold it.

    Both `"4527"` and `"#4527"` occur in real proposals (`dispatch_pass` and
    `diagnose` both `lstrip("#")` when reading them back), so matching one spelling
    only would silently fail to find half the plans.
    """
    return [str(issue_number), f"#{issue_number}"]


async def _resolve_target(
    session: AsyncSession,
    *,
    org_id: str,
    issue_number: int,
) -> tuple[str, OrchestrationNode | None] | None:
    """The flow a command on this issue addresses, and the node if it is one.

    Two lookups, both filtered on `org_id` in SQL, then reconciled:

    1. A node materialised as this issue. This is the common case — a human
       comments on the story or gate they are looking at.
    2. A flow whose `intent_ref` is this issue, so commands on the originating
       intent issue address the whole plan. `halt`, `resume` and `replan` are
       flow-scoped, and that is where a human reads plan status.

    Returns `(flow_id, node_or_None)`, or None when the issue addresses nothing
    unambiguously. **Ambiguity is a refusal, not a choice.** There is no basis to
    pick, so the engine refuses, when:

    * two nodes carry the same `issue_ref` — picking the first would apply a halt
      to whichever row happened to sort first; or
    * a node in one flow AND a *different* flow's `intent_ref` both name this issue.
      `ProposedNode.issue_ref` is author-chosen free text and node authoring is
      open to any ``PLAN_DRAFT`` holder, so an attacker can plant a node whose
      `issue_ref` collides with a victim flow's intent issue (or a flow whose
      `intent_ref` collides with a victim's node). Preferring either match would
      route a human's ``@agent-engine accept``/``halt`` onto attacker-controlled
      work and root the resulting approval under the human's identity — the exact
      privilege escalation this bridge exists to prevent. Both lookups must agree
      on a single flow, or the command is refused.
    """
    candidates = _issue_ref_candidates(issue_number)

    nodes = list(
        (
            await session.execute(
                select(OrchestrationNode).where(
                    OrchestrationNode.org_id == org_id,
                    OrchestrationNode.issue_ref.in_(candidates),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(nodes) > 1:
        logger.warning(
            "orchestration engine commands: issue %s matches %d nodes in org %s — refusing as ambiguous",
            issue_number,
            len(nodes),
            org_id,
        )
        return None

    flows = list(
        (
            await session.execute(
                select(OrchestrationFlow).where(
                    OrchestrationFlow.org_id == org_id,
                    OrchestrationFlow.intent_ref.in_(candidates),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(flows) > 1:
        logger.warning(
            "orchestration engine commands: issue %s matches %d flows in org %s — refusing as ambiguous",
            issue_number,
            len(flows),
            org_id,
        )
        return None

    node = nodes[0] if nodes else None
    flow = flows[0] if flows else None

    # The distinct flows this issue names, across both lookups. A node's parent
    # and a flow's own intent home may legitimately be the SAME flow (self-
    # consistent); two DIFFERENT flows is the collision described above.
    addressed = {flow_id for flow_id in (node.flow_id if node else None, flow.id if flow else None) if flow_id is not None}
    if len(addressed) != 1:
        if addressed:
            logger.warning(
                "orchestration engine commands: issue %s names %d distinct flows in org %s (node parent vs. intent_ref) — refusing as ambiguous",
                issue_number,
                len(addressed),
                org_id,
            )
        return None

    flow_id = next(iter(addressed))
    # The node is returned only when it is genuinely in the addressed flow; when the
    # intent issue is what matched, there is no single node and the command is
    # flow-scoped.
    return flow_id, (node if node is not None and node.flow_id == flow_id else None)


async def _resolve_gate(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    gate_ref: str | None,
) -> OrchestrationNode | None:
    """The gate node a gate answer addresses, or None if that is not unambiguous.

    With `gate_ref` supplied (`approve gate 3`) the gate is addressed by its
    `node_ref` within the flow. Without one (`accept`) the gate is the flow's
    single node in `awaiting_gate` — the one place the engine is actually waiting
    on a human, which is the only thing a bare "accept" can mean. Two gates
    awaiting an answer is ambiguous and refused; the commenter can name one.
    """
    stmt = select(OrchestrationNode).where(
        OrchestrationNode.org_id == org_id,
        OrchestrationNode.flow_id == flow_id,
        OrchestrationNode.kind.in_([NodeKind.GATE.value, NodeKind.EVAL.value]),
    )
    if gate_ref is not None:
        stmt = stmt.where(OrchestrationNode.node_ref == gate_ref)
    else:
        stmt = stmt.where(OrchestrationNode.state == NodeState.AWAITING_GATE.value)

    gates = list((await session.execute(stmt)).scalars().all())
    if len(gates) == 1:
        return gates[0]
    logger.warning(
        "orchestration engine commands: gate_ref=%r matches %d gates in flow %s — refusing",
        gate_ref,
        len(gates),
        flow_id,
    )
    return None


async def _bulk_transition(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    context: TokenContext,
    actor_role: str,
    eligible: dict[str, DecisionKind],
    to_state: NodeState,
    reason: str,
) -> int:
    """Move every eligible node in one flow, recording each move. Returns the count.

    The one place this module changes a node's state, and it is the same three
    guards `controls.resume_node` and `_gate_transition` use:

    1. `eligible` narrows *which* states this command may act from, before the
       table is consulted. This is a narrowing of the caller's authority, not a
       second copy of `state.py`'s table: `running -> passed` is legal for a HUMAN
       because that is how a green evaluation promotes a node, and a `halt` must
       not be able to take it.
    2. ``transition()`` decides whether the edge is legal for a HUMAN actor. A
       refusal is skipped rather than forced — the table stays authoritative.
    3. The UPDATE is conditional on the observed state, so a node another writer
       moved between our read and our write matches 0 rows and records nothing.

    Nodes are read and moved one at a time rather than in a single bulk UPDATE
    precisely so guard 3 applies per node: one lost race must not discard the
    other nodes' transitions.
    """
    nodes = list(
        (
            await session.execute(
                select(OrchestrationNode).where(
                    OrchestrationNode.org_id == org_id,
                    OrchestrationNode.flow_id == flow_id,
                    OrchestrationNode.state.in_(list(eligible)),
                )
            )
        )
        .scalars()
        .all()
    )

    repo = OrchestrationRepository(session)
    moved = 0

    for node in nodes:
        observed_state = node.state
        result = transition(observed_state, to_state, actor_kind=ActorKind.HUMAN, reason=reason)
        if not result.allowed:  # pragma: no cover - unreachable while `eligible` stands
            # Currently unreachable: every state in `eligible` has a human-legal
            # edge to `to_state`. Kept, and deliberately not an assert, because
            # `transition()` owns the table: if a future edit narrows those edges
            # this must skip rather than proceed to the UPDATE on a refused
            # transition.
            logger.warning(
                "orchestration engine commands: transition refused for node %s (%s -> %s): %s",
                node.id,
                observed_state,
                to_state.value,
                result.rejection_reason,
            )
            continue

        stmt = (
            update(OrchestrationNode)
            .where(
                OrchestrationNode.id == node.id,
                OrchestrationNode.org_id == org_id,
                OrchestrationNode.state == observed_state,
            )
            .values(state=result.new_state.value, updated_at=utcnow())
        )
        rows = (await session.execute(stmt)).rowcount or 0
        await session.flush()
        if rows == 0:
            # Lost race. Whoever won recorded their own decision; a second row
            # would make the trail claim the node moved twice.
            continue

        await repo.append_decision(
            org_id=org_id,
            flow_id=flow_id,
            node_id=node.id,
            kind=eligible[observed_state].value,
            actor_id=context.user_id,
            actor_role=actor_role,
            actor_kind=ActorKind.HUMAN.value,
            reason=reason,
            from_state=observed_state,
            to_state=result.new_state.value,
        )
        moved += 1

    return moved


async def _apply_command(
    session: AsyncSession,
    *,
    command: EngineCommand,
    org_id: str,
    flow_id: str,
    context: TokenContext,
    access: AccessControl,
) -> tuple[bool, str]:
    """Apply one authorized command. Returns `(applied, message_for_the_commenter)`.

    The permission check happens here, once, for every verb — including `replan`,
    which writes no state. A request to re-plan is a statement about promotion
    state that a human will act on, and letting anyone record one would make the
    decisions table forgeable by comment.
    """
    actor_role = _COMMAND_ACTOR_ROLE_FALLBACK
    try:
        actor_role = (await access.get_user_role(context))[0].value
    except Exception:
        # A role that cannot be read must not stop the permission check below from
        # running: the check is the authority, the role is only what gets recorded
        # alongside it. Falling back keeps attribution honest ("acted through the
        # engine-command bridge") instead of guessing at a role.
        logger.warning("orchestration engine commands: could not resolve a role for %s; recording %r", context.user_id, actor_role)

    try:
        await access.check_permission(context, Permission.PLAN_APPROVE, target_org_id=org_id)
    except (AccessDeniedError, InvalidScopeError):
        # In-org but unauthorized. Unlike the gate adapter this records nothing:
        # there is no single node to attribute the attempt to for the flow-scoped
        # verbs, and a decision row with a null node on a flow the commenter never
        # named would be evidence of the wrong thing. The refusal is logged and
        # replied to.
        logger.warning(
            "orchestration engine commands: %s lacks %s in org %s — refusing %s",
            context.user_id,
            Permission.PLAN_APPROVE.value,
            org_id,
            command.verb.value,
        )
        return False, _UNIFORM_REFUSAL

    if command.verb in (CommandVerb.ACCEPT, CommandVerb.APPROVE_GATE):
        gate = await _resolve_gate(session, org_id=org_id, flow_id=flow_id, gate_ref=command.gate_ref)
        if gate is None:
            return False, _UNIFORM_REFUSAL

        outcome = await apply_gate_answer_for_context(
            session,
            context=context,
            node_id=gate.id,
            approve=True,
            reason=f"{command.verb.value} via @agent-engine comment",
            access=access,
            input_path=_INPUT_PATH,
            # The gate path's own uniform constant, so a refusal from inside the
            # adapter is worded exactly as the adapter documents it rather than
            # being rewritten here.
            refusal_message=_GATE_UNIFORM_REFUSAL,
        )
        if outcome.status is GateAnswerStatus.APPLIED:
            return True, f"gate `{gate.node_ref}` approved."
        if outcome.status is GateAnswerStatus.ALREADY_ANSWERED:
            return False, f"gate `{gate.node_ref}` was already answered."
        return False, outcome.message

    if command.verb is CommandVerb.HALT:
        moved = await _bulk_transition(
            session,
            org_id=org_id,
            flow_id=flow_id,
            context=context,
            actor_role=actor_role,
            eligible=_HALTABLE_STATES,
            to_state=NodeState.HALTED,
            reason="halted via @agent-engine comment",
        )
        if moved == 0:
            return False, "nothing on this plan is running or awaiting a gate, so there is nothing to halt."
        return True, f"halted {moved} node(s) on this plan. Comment `@agent-engine resume` to continue."

    if command.verb is CommandVerb.RESUME:
        moved = await _bulk_transition(
            session,
            org_id=org_id,
            flow_id=flow_id,
            context=context,
            actor_role=actor_role,
            eligible=_RESUMABLE_STATES,
            to_state=NodeState.READY,
            reason="resumed via @agent-engine comment",
        )
        if moved == 0:
            return False, "nothing on this plan is halted or failed, so there is nothing to resume."
        return True, f"resumed {moved} node(s) on this plan."

    # REPLAN. Recorded, never acted on: `to_state=None` and no `node_id`, so the
    # row cannot express a promotion and is not attributed to a node nobody named.
    repo = OrchestrationRepository(session)
    await repo.append_decision(
        org_id=org_id,
        flow_id=flow_id,
        kind=DecisionKind.REPLAN_REQUESTED.value,
        actor_id=context.user_id,
        actor_role=actor_role,
        actor_kind=ActorKind.HUMAN.value,
        reason=command.text or "no detail given",
    )
    return True, "re-plan requested and recorded. The plan is unchanged until someone authors an amendment."


async def _handle_row(
    session: AsyncSession,
    row: dict[str, Any],
    report: EngineCommandReport,
    *,
    access: AccessControl,
) -> None:
    """Verify, resolve, authorize and apply one marked event row.

    Attribution is verified FIRST (#4539). Everything below that point acts on the
    signed tuple, so "the tick trusts only what a verified GitHub delivery carried"
    holds by construction rather than by every later reader remembering to check.

    Every exit either applies the command or queues a reply, so no comment is
    silently ignored. There are three exceptions, all consumed quietly:

    * **A row whose attribution does not verify** (#4539). Quarantined: no lookup,
      no decision, no dispatch, no reply. A reply would be a GitHub write addressed
      with fields whoever wrote the row chose.
    * **A body that parses to nothing.** The Lambda marks any comment containing the
      tag, so `cc @agent-engine` in prose — or a doc quoting a command inside
      backticks — is a marked row with no command in it. Replying to those would
      turn every mention of the engine into a comment.
    * **A comment a bot authored.** A live command is a human act; the platform's own
      agents narrating what a command does are not issuing one.
    """
    event_id = str(row.get("event_id") or "")
    arrived_at = str(row.get("arrived_at") or "")

    # Issue #4539: VERIFY ATTRIBUTION FIRST — before the installation is resolved,
    # before any identity or permission lookup, and before any row field is used for
    # a side effect. The ordering is the security property, not a performance choice:
    #
    #   * an identity lookup keyed on an attacker-chosen sender in an attacker-chosen
    #     tenant is itself a probe, and a distinguishable outcome is an oracle for
    #     which accounts and orgs exist;
    #   * an acknowledgement addressed with an attacker-chosen repository and
    #     installation would make this component post to a repository of the
    #     attacker's choosing using a credential it holds — a confused-deputy write,
    #     regardless of whether the command was ever applied.
    #
    # So an unverified row produces no lookup, no decision, no dispatch and no
    # acknowledgement. It is quarantined (consumed with an empty message, so it stops
    # being re-read) and counted.
    #
    # A valid signature is NOT authorization. It establishes only that this tuple
    # came from a delivery that passed GitHub's webhook signature check, unaltered.
    # Membership, PLAN_APPROVE and the human-only gates all run below, unchanged.
    try:
        verified: VerifiedCommand = verify_row(row)
    except AttributionError as exc:
        _quarantine(row, report, reason=exc.reason)
        return

    # Authority and routing come from the SIGNED tuple, not from the row. The row's
    # mutable copies were checked against it during verification (a disagreement
    # refuses), so these are equal — but reading them from `verified` is what makes
    # "the tick acts only on signed values" true by construction rather than by
    # coincidence, and it is what stays true if a future change adds a row attribute
    # that is not in the signed set.
    org_id = verified.tenant_id
    repo_full = verified.repo
    issue_number = verified.issue_number
    body = verified.command_body
    github_user_id = verified.sender_github_id

    report.record(org_id, "commands_read")

    def _queue(message: str, *, installation_id: int | None) -> None:
        report.pending.append(
            PendingEngineAck(
                event_id=event_id,
                arrived_at=arrived_at,
                org_id=org_id,
                repo=repo_full,
                issue_number=issue_number,
                installation_id=installation_id,
                message=message,
            )
        )

    # Issue #4599: a bot did not *ask* for anything. A live engine command is a
    # human act, so a comment authored by a GitHub App or a bot account is consumed
    # with no reply — checked before parsing, because the point is to say nothing at
    # all rather than to say it more quietly.
    #
    # THIS IS A NOISE FILTER, NOT AN AUTHORIZATION BOUNDARY. Authority already holds
    # without it: bot identities seed with `role="agent"`, which is absent from
    # `_MEMBERSHIP_ROLE_TO_ADMIN_ROLE`, so `membership_role_to_admin_role` fails
    # closed to MEMBER, which lacks `PLAN_APPROVE`. Deleting this branch makes the
    # thread noisy again; it does not make a bot able to approve anything. Do not
    # relax that RBAC on the strength of this check.
    #
    # Issue #4539 changed WHERE author kind is read from: `verified.sender_is_bot`
    # derives it from the SIGNED `sender_type`, which is GitHub's own value for the
    # delivery, instead of from the row's mutable `engine_command_sender_is_bot`
    # flag. Same behaviour, but the flag was one more attribute anything writing the
    # row could flip. Verification refuses any row whose signed tuple is absent, so
    # there is no "field postdates existing rows" window to be tolerant about here.
    if verified.sender_is_bot:
        logger.info(
            "orchestration engine commands: event %s was authored by a bot; consuming quietly",
            event_id,
        )
        report.record(org_id, "commands_refused")
        _queue("", installation_id=None)
        return

    command = parse_engine_command(body)
    if command is None:
        # Tagged but not a command. Consumed with no reply — see the docstring.
        logger.info("orchestration engine commands: event %s carries no recognised command; consuming quietly", event_id)
        report.record(org_id, "commands_refused")
        _queue("", installation_id=None)
        return

    # The org's installation, resolved server-side from the org record through the
    # SAME fail-closed rule dispatch applies. Required before anything is applied:
    # it both confirms the row's tenant against the installation that delivered it
    # and is the credential the ack needs. A plan that can dispatch at all already
    # satisfies this, so it refuses nothing that was ever going to work.
    #
    # Issue #4539 fixed two defects in this check at once. It compared against the
    # ROW's `installation_id`, which whoever wrote the row chose — so the check
    # confirmed the row against itself. And the `row_installation and` conjunct made
    # it skip entirely when that attribute was absent or empty, so omitting the field
    # was enough to bypass it. It now compares against the SIGNED installation, and
    # there is no conditional: an empty signed value cannot equal a resolved id, so
    # an absent installation refuses instead of passing.
    installation_id = await resolve_installation_id(session, org_id=org_id) if org_id else None
    signed_installation = verified.installation_id.strip()
    if installation_id is None or str(installation_id) != signed_installation:
        logger.warning(
            "orchestration engine commands: org %r resolves to installation %r but event %s was delivered on %r — refusing",
            org_id,
            installation_id,
            event_id,
            signed_installation,
        )
        report.record(org_id, "commands_refused")
        _queue("", installation_id=None)
        return

    resolved = await _resolve_platform_identity(session, org_id=org_id, github_user_id=github_user_id)
    if resolved is None:
        # No linked identity in this org. Nothing about the plan is read and
        # nothing is written; the reply is the uniform constant, so an outsider
        # learns neither whether the plan exists nor whether the account is known.
        logger.warning("orchestration engine commands: no github identity %r in org %s — refusing", github_user_id, org_id)
        report.record(org_id, "commands_refused")
        _queue(_UNIFORM_REFUSAL, installation_id=installation_id)
        return

    context, _team_id = resolved

    target = await _resolve_target(session, org_id=org_id, issue_number=issue_number)
    if target is None:
        report.record(org_id, "commands_refused")
        _queue(_UNIFORM_REFUSAL, installation_id=installation_id)
        return

    flow_id, _node = target
    applied, message = await _apply_command(
        session,
        command=command,
        org_id=org_id,
        flow_id=flow_id,
        context=context,
        access=access,
    )

    report.record(org_id, "commands_applied" if applied else "commands_refused")
    _queue(message, installation_id=installation_id)


def _quarantine(row: dict[str, Any], report: EngineCommandReport, *, reason: str) -> None:
    """Record one unverifiable row for sealing off, and do nothing else (#4539).

    Deliberately does NOT: append a decision, dispatch work, resolve an identity or
    an installation, or queue an acknowledgement. Every one of those would either act
    on, or address a GitHub write with, fields whoever wrote the row selected.

    The row's `tenant_id` is used as a counter label and nothing else. It is
    unverified — the label may be a tenant the writer chose — but attributing the
    event to *some* tenant is what makes a per-org spike visible at all, and it is
    never used to resolve a credential or read a plan.
    """
    org_id = str(row.get("tenant_id") or "")
    event_id = str(row.get("event_id") or "")
    arrived_at = str(row.get("arrived_at") or "")

    # Reason and row keys only. Never the signature, the key id, the body, or any
    # other row content: this line goes to CloudWatch Logs, where attacker-chosen
    # text is both an injection surface and something an operator may reasonably read
    # as the platform's own output.
    logger.warning(
        "orchestration engine commands: event %s failed attribution verification (%s); quarantining",
        event_id,
        reason,
    )

    report.record(org_id, "commands_quarantined")
    report.record_quarantine_reason(reason)
    report.quarantined.append(
        QuarantinedRow(
            event_id=event_id,
            arrived_at=arrived_at,
            org_id=org_id,
            # The signature exactly as observed — not stripped, not normalised. The
            # conditional write below is bound to this value, so it must be the bytes
            # that were actually compared against, byte for byte.
            observed_signature=_observed_signature(row),
            reason=reason,
        )
    )


def _observed_signature(row: dict[str, Any]) -> str:
    """The row's signature attribute as a string, `""` when absent.

    A non-string attribute (a number, a map — anything a writer could put there)
    becomes `""` rather than raising, so a hostile row cannot make quarantining
    itself fail. `""` is a distinguishable state for the conditional write, which
    binds to "still absent" in that case.
    """
    value = row.get(SIGNATURE_ATTR)
    return value if isinstance(value, str) else ""


def _quarantine_write(table, quarantined: QuarantinedRow) -> bool:
    """Seal off one unverifiable row. Returns whether this call did it.

    Conditional on BOTH facts this verdict was reached about:

    * the marker is still `pending` — so a tick that already sealed or consumed the
      row wins and this one applies nothing;
    * the signature attribute still holds exactly the value that was verified (or is
      still absent, when the row carried none).

    The second condition is the one that matters for correctness. Without it, a
    verdict about content read at time T would be applied to whatever the row holds
    at time T+n: a legitimate signed rewrite landing in that gap would be quarantined
    on the strength of the *previous* content, turning a race into a lost human
    command. Binding the write to the observed signature makes the outcome "somebody
    changed it, re-verify next wake" instead.
    """
    from botocore.exceptions import ClientError

    if quarantined.observed_signature:
        condition = f"engine_command_status = :pending AND {SIGNATURE_ATTR} = :sig"
        values: dict[str, Any] = {
            ":quarantined": ENGINE_COMMAND_STATUS_QUARANTINED,
            ":pending": ENGINE_COMMAND_STATUS_PENDING,
            ":sig": quarantined.observed_signature,
            ":reason": quarantined.reason,
        }
    else:
        # No signature was observed. `attribute_not_exists` is the honest binding for
        # that state — a row that has since ACQUIRED a signature must not be sealed
        # off by a verdict reached when it had none.
        condition = f"engine_command_status = :pending AND attribute_not_exists({SIGNATURE_ATTR})"
        values = {
            ":quarantined": ENGINE_COMMAND_STATUS_QUARANTINED,
            ":pending": ENGINE_COMMAND_STATUS_PENDING,
            ":reason": quarantined.reason,
        }

    try:
        table.update_item(
            Key={"event_id": quarantined.event_id, "arrived_at": quarantined.arrived_at},
            # The sanitized reason is stored on the row so an operator investigating
            # later can tell "no key was seeded" from "this signature did not verify"
            # without correlating against metrics. Bounded enum, never row content.
            UpdateExpression=("SET engine_command_status = :quarantined, engine_command_quarantine_reason = :reason"),
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Either another tick got there first, or the row changed under us. Both
            # are normal concurrency: nothing was applied, and if the row is still
            # unverifiable it is quarantined on the next wake.
            logger.info(
                "orchestration engine commands: event %s was not quarantined; the row changed or another tick sealed it",
                quarantined.event_id,
            )
            return False
        raise


def _get_table(config: EngineCommandConfig):
    """The events table handle. Imported lazily so `boto3` is not a test dependency."""
    import boto3

    return boto3.resource("dynamodb", region_name=config.aws_region).Table(config.table_name)


def _query_pending(table, *, limit: int) -> list[dict[str, Any]]:
    """Outstanding commands, oldest first, from the sparse index.

    Oldest first because commands are a sequence a human typed: `halt` then
    `resume` must not be applied in the other order. `ScanIndexForward=True` on
    `arrived_at` is what makes that true.
    """
    from boto3.dynamodb.conditions import Key

    response = table.query(
        IndexName=ENGINE_COMMAND_INDEX,
        KeyConditionExpression=Key("engine_command_status").eq(ENGINE_COMMAND_STATUS_PENDING),
        ScanIndexForward=True,
        Limit=limit,
    )
    return list(response.get("Items", []))


async def run_engine_command_pass(
    session: AsyncSession,
    config: EngineCommandConfig | None = None,
    *,
    table: Any | None = None,
) -> EngineCommandReport:
    """One engine-command pass. **Commits nothing; sends nothing.**

    Reads the outstanding `@agent-engine` commands, applies each through the same
    permission check and guarded transitions the UI controls use, and returns the
    consume-and-acknowledge work for the caller to flush after its commit.

    Args:
        session: Caller-owned session. Nothing is committed here, so a command's
            state change and its decision row land atomically or not at all.
        config: Whether the bridge runs, which table, and the per-pass cap. Read
            from the environment when omitted, where it is fail-closed.
        table: Injected DynamoDB table handle, for tests. Resolved from `config`
            when omitted.

    Returns:
        An :class:`EngineCommandReport`. `success` is False if any row errored, any
        marker could not be consumed, or any ack could not be delivered.

    Never raises for one bad row: it records the error and continues, so one
    malformed command cannot stop every other tenant's commands being applied.
    """
    cfg = config if config is not None else EngineCommandConfig.from_env()
    report = EngineCommandReport(enabled=cfg.enabled and cfg.configured)

    if not report.enabled:
        # Fail-closed, and SILENT: no query, no write, and deliberately no ack.
        # An "the engine is disabled" reply would advertise the bridge to anyone
        # who can comment and would make a switched-off feature post comments.
        logger.debug(
            "orchestration engine commands: disabled (%s is not 'true', or %s is unset); no commands read",
            FEATURE_FLAG_ENV,
            TABLE_ENV,
        )
        return report

    handle = table if table is not None else _get_table(cfg)

    try:
        rows = _query_pending(handle, limit=cfg.max_commands_per_pass)
    except Exception:
        # A page we cannot read is a real failure, not an empty one: reporting it
        # as success would make a broken bridge look like a quiet one.
        logger.exception("orchestration engine commands: failed to query %s", ENGINE_COMMAND_INDEX)
        report.errors += 1
        return report

    if len(rows) >= cfg.max_commands_per_pass:
        # At the cap there may be more waiting. Delayed to the next wake, never
        # dropped — but it must not read as "that was all of them".
        report.capped = True
        logger.warning(
            "orchestration engine commands: per-pass cap of %d reached; remaining commands wait for the next pass", cfg.max_commands_per_pass
        )

    for row in rows:
        try:
            await _handle_row(session, row, report, access=AccessControl(session))
        except Exception:
            # Per-row containment: log, count, force non-success, keep going. The
            # marker is NOT queued for consume, so the row is retried on the next
            # wake rather than being lost to one transient failure.
            logger.exception("orchestration engine commands: failed to handle event %r", row.get("event_id"))
            report.record(str(row.get("tenant_id") or ""), "errors")

    return report


def _consume(table, ack: PendingEngineAck) -> bool:
    """Flip one marker `pending -> consumed`. Returns whether this call did it.

    Conditional on the marker still being `pending`, which is the whole
    idempotency guarantee: a second tick that read the same row before this write
    lands matches the condition, fails, and applies nothing further.
    """
    from botocore.exceptions import ClientError

    try:
        table.update_item(
            Key={"event_id": ack.event_id, "arrived_at": ack.arrived_at},
            UpdateExpression="SET engine_command_status = :consumed",
            ConditionExpression="engine_command_status = :pending",
            ExpressionAttributeValues={
                ":consumed": ENGINE_COMMAND_STATUS_CONSUMED,
                ":pending": ENGINE_COMMAND_STATUS_PENDING,
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Another tick consumed it first. Normal concurrency, not a failure —
            # and the reason a duplicate application cannot produce a duplicate ack.
            logger.info("orchestration engine commands: event %s was already consumed by a concurrent tick", ack.event_id)
            return False
        raise


async def _post_ack(ack: PendingEngineAck) -> None:
    """Post one reply to the issue the command arrived on.

    Credentials are the tenant's own GitHub App key and an installation token
    minted from it — the same pair every other GitHub write in this platform uses.
    Nothing here reads a token from the event row.
    """
    from src.admin.connections.github_client import GitHubAppClient
    from src.knowledge.github_app_service import resolve_tenant_app_credentials

    app_id, private_key = await resolve_tenant_app_credentials(ack.org_id)
    client = GitHubAppClient(app_id, private_key)
    try:
        await client.create_issue_comment(
            installation_id=ack.installation_id or 0,
            repo=ack.repo,
            issue_number=ack.issue_number,
            body=ack.message,
        )
    finally:
        await client.aclose()


async def flush_engine_commands(report: EngineCommandReport, config: EngineCommandConfig | None = None, *, table: Any | None = None) -> None:
    """Consume the markers and post the acks. **Call after the caller's commit.**

    The post-commit half of the pass, and async rather than sync (unlike
    `dispatch_pass.publish_pending`) because posting a comment is an `await`.
    Mutates `report` in place and never raises: a failed consume or ack is counted,
    which forces a non-success report, and the durable decisions are already
    committed by the time this runs.

    Consume comes first so that a failure to *reply* can never cause the command to
    be applied a second time. An empty `message` means "consume, say nothing" — a
    marked comment that carried no command.

    Quarantined rows (#4539) are sealed off here too, in a loop of their own that
    cannot post anything. They are handled BEFORE the acks: a row that failed
    verification has no legitimate reply to wait behind, and sealing it first means a
    GitHub outage stalling the ack loop cannot leave unverifiable rows `pending` and
    being re-verified every wake.
    """
    if not report.pending and not report.quarantined:
        return

    cfg = config if config is not None else EngineCommandConfig.from_env()
    if not cfg.configured:  # pragma: no cover - unreachable while `pending` implies an enabled pass
        logger.warning(
            "orchestration engine commands: %s is unset; %d marker(s) not consumed and %d not quarantined",
            TABLE_ENV,
            len(report.pending),
            len(report.quarantined),
        )
        report.consumes_failed += len(report.pending)
        report.quarantines_failed += len(report.quarantined)
        return

    handle = table if table is not None else _get_table(cfg)

    for quarantined in report.quarantined:
        try:
            _quarantine_write(handle, quarantined)
        except Exception:
            # The row stays `pending` and is verified again next wake. Verification is
            # pure and will fail identically, so nothing is applied — but a row that
            # can never be sealed off is the permanent re-read this work exists to
            # prevent, so it is counted and forces a non-success report.
            logger.exception(
                "orchestration engine commands: failed to quarantine event %s",
                quarantined.event_id,
            )
            report.record(quarantined.org_id, "quarantines_failed")

    for ack in report.pending:
        try:
            claimed = _consume(handle, ack)
        except Exception:
            # An unconsumed marker means this command is re-applied next wake.
            # Counted rather than swallowed: every write it made is state-
            # conditional, so the repeat is a no-op, but a marker that can never
            # be consumed is a permanent loop and must be visible.
            logger.exception("orchestration engine commands: failed to consume event %s", ack.event_id)
            report.record(ack.org_id, "consumes_failed")
            continue

        if not claimed or not ack.message:
            continue

        try:
            await _post_ack(ack)
            report.record(ack.org_id, "acks_posted")
        except Exception:
            # The command was applied and its author was not told. That is the
            # invisible outcome this story exists to remove, so it is its own
            # counter and it forces a non-success report.
            logger.exception("orchestration engine commands: failed to acknowledge event %s on %s#%s", ack.event_id, ack.repo, ack.issue_number)
            report.record(ack.org_id, "acks_failed")
