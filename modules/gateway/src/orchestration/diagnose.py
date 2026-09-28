"""Summon an agent to explain a stalled or halted node. **Propose, never dispose.**

Issue #4214 (EPIC #4191, intent #4120). The last and deliberately smallest story
of the EPIC, and the one with the least authority.

When a delivery loop stalls or halts, someone still has to work out why. This
module summons a diagnosing agent that assembles the context — what was attempted,
what failed, what state the node was observed in — and records a **diagnosis** the
human reads next to the stalled node. Instead of starting from a blank stack trace,
the reviewer starts from a first pass.

--------------------------------------------------------------------------------
Authority is the entire design (D-R10, D-C1)
--------------------------------------------------------------------------------

The diagnoser is a **worker persona at a node**, with exactly the authority any
other worker has and no more. Concretely, and each of these is asserted by
`test_diagnose.py` rather than left as a claim:

- It holds **no** promotion permission and cannot change a node's state. This
  module does not import `transition` and does not import
  `apply_guarded_transition` — not "imports them but never calls them", does not
  import them at all, which is the stronger property and the one a source-level
  test can prove cheaply.
- It **cannot clear a halt**. The `halted -> ready` edge is `_HUMAN_ONLY` in
  `state.py`, this module adds no bypass, and it never names `READY` as a target
  because it never names a target at all.
- It writes **only** a diagnosis record that the engine and the human read. The
  output is **advisory**.
- It is **engine-summoned**: no persona drives the loop. This module decides when
  to summon; the diagnoser never decides what happens next.

The one structural expression of all of the above: a diagnosis row is written with
``to_state = None``. Every other decision kind that concerns a node carries a
state pair. A diagnosis proposes *nowhere* for the node to go, so the record is
**incapable of expressing a promotion** — the guarantee lives in the shape of the
row, not in reviewer vigilance. `NODE_DIAGNOSIS_PROPOSED` is named for the same
reason: `NODE_DIAGNOSED` would read as a settled finding.

--------------------------------------------------------------------------------
Presentation matters as much as permission
--------------------------------------------------------------------------------

A diagnosis that reads authoritative turns a human gate into a rubber stamp, which
defeats the gate — so the record renders explicitly as an agent's **proposal**:
attributed to the diagnoser, labelled unverified, with :data:`ADVISORY_PREFIX`
carried in the persisted `reason` itself rather than added by a rendering layer.
Putting it in the stored text is deliberate: a caller that reads the row directly,
or a future surface nobody has written yet, cannot accidentally present an agent's
guess as a system conclusion.

--------------------------------------------------------------------------------
The summon bound is a cost guard, and it is per EVENT — not per tick, not forever
--------------------------------------------------------------------------------

Each diagnosis is a real agent run with real Bedrock cost, so a node must never be
re-diagnosed on every tick while it sits halted. The bound is: **at most one
diagnosis per node per stall/halt event.**

That is implemented by comparing timestamps, not by asking "has this node ever
been diagnosed?":

    summon iff  newest stall/halt event  is newer than  newest diagnosis

Both halves of that matter, and the naive alternatives each fail in one direction:

- A per-tick check with no marker at all re-diagnoses a halted node every five
  minutes forever. That is the spend bug, and it is the one the issue calls out.
- A "has a diagnosis row ever existed" check bounds spend correctly but silently
  breaks the *second* incident: a node that stalls, is resumed by a human, runs,
  and stalls again is a genuinely new event with new context, and it would never
  be diagnosed again. The failure is invisible, which makes it the worse of the
  two.

The timestamp comparison bounds spend *and* survives the resume path. Both
directions are tested — five consecutive ticks yield exactly one diagnosis, and a
fresh event after a diagnosis yields a second one.

`created_at` ordering is well-defined here because a diagnosis is always written
in the same transaction as, and therefore no earlier than, the event that
triggered it. Ties (equal timestamps, which SQLite can produce at low resolution)
are resolved *against* summoning: the comparison is strictly-newer, so a tie means
"already diagnosed". Erring toward not spending is the correct bias for a cost
guard.

--------------------------------------------------------------------------------
Cut-safe by construction
--------------------------------------------------------------------------------

Nothing else in this EPIC imports this module, and `test_diagnose.py` asserts that
at source level across the whole `src/orchestration/` package. If this story is
dropped, stall and halt detection, notification, the graph view and every control
still work exactly as specified. **Do not add a caller in another story** — doing
so is what would make the story non-droppable, and the test is there to notice.

The module is additionally feature-flagged behind the engine's own fail-closed
flag, so it is off by default even once it has a caller.

Tenant isolation: the summon path resolves nodes with an `org_id` filter, the
diagnosis record carries that `org_id`, and :func:`record_diagnosis` refuses a
mismatched pair outright rather than trusting its caller.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .dispatch_pass import message_deduplication_id, message_group_id, resolve_installation_id
from .models import DecisionKind, OrchestrationDecision, OrchestrationNode
from .state import NodeState
from .tick import _ITEM_BACKSTOP, _PAGE_SIZE

logger = logging.getLogger("bedrockgateway.orchestration.diagnose")

__all__ = [
    "ADVISORY_PREFIX",
    "DIAGNOSABLE_STATES",
    "DIAGNOSER_ACTOR_ROLE",
    "DIAGNOSER_PERSONA",
    "Diagnosis",
    "DiagnosisConfig",
    "DiagnosisReport",
    "PendingDiagnosis",
    "TenantMismatchError",
    "record_diagnosis",
    "run_diagnosis_pass",
]


# The engine's own fail-closed flag. Read here rather than given a flag of its own:
# this is part of the engine path, and a second flag would let the two disagree —
# a diagnoser summoning against an engine that is switched off.
FEATURE_FLAG_ENV = "FEATURE_ORCHESTRATION_ENGINE_ENABLED"

# The dispatch story's target-repo var, reused rather than duplicated. A diagnoser
# goes where the engine's other work goes; a second var could disagree with it.
REPO_ENV = "BG_ORCH_DISPATCH_REPO"

# Envelope schema version. Must match the dispatch story's `_ENVELOPE_VERSION` and
# the webhook path's, so one worker code path parses every producer's messages.
_ENVELOPE_VERSION = "1.0"

# The persona summoned to diagnose. A worker persona; `persona is not authority`
# (R-O5d), so nothing downstream may read this string as a grant of anything.
DIAGNOSER_PERSONA = "exception-diagnoser"

# Attribution on the diagnosis row. `actor_role` is snapshotted at decision time
# per the store story, and `diagnoser` is the whole of the authority claimed.
DIAGNOSER_ACTOR_ID = "system:orchestration-exception-diagnoser"
DIAGNOSER_ACTOR_ROLE = "diagnoser"

# Carried verbatim at the head of the persisted `reason`. In the stored text on
# purpose — see the module docstring on presentation. A reader that never learned
# about this module still cannot mistake the row for a system conclusion.
ADVISORY_PREFIX = "[ADVISORY — unverified agent proposal, not a verdict]"

# The decision kinds that constitute a stall/halt *event*: the trigger source from
# the stall story. Read, never written, by this module.
TRIGGER_KINDS: frozenset[DecisionKind] = frozenset({DecisionKind.NODE_STALLED, DecisionKind.NODE_HALTED})

# The states a node can be diagnosed in. Exactly the two outcomes the stall story
# produces. Deliberately narrow: diagnosing a `running` node would summon an agent
# to explain work that is still in progress, and diagnosing a `passed` node would
# summon one to explain a success.
DIAGNOSABLE_STATES: frozenset[NodeState] = frozenset({NodeState.FAILED, NodeState.HALTED})


class TenantMismatchError(ValueError):
    """A diagnosis whose `org_id` does not match the node it diagnoses.

    Raised rather than silently corrected. A mismatch means the caller resolved
    the node in one tenant and is about to attribute the finding to another, which
    would place one org's failure context in another org's audit trail. There is
    no safe repair — the caller's intent is already ambiguous.
    """


# Per-pass cost cap, and the env var that tunes it. Independent of the per-node
# bound: the per-node marker stops one node being re-diagnosed, this stops a
# hundred nodes halting at once from becoming a hundred simultaneous agent runs.
# Two different unbounded surfaces, so two bounds.
DEFAULT_MAX_SUMMONS_PER_PASS = 5
MAX_SUMMONS_PER_PASS_ENV = "BG_ORCH_MAX_DIAGNOSES_PER_PASS"


@dataclass(frozen=True)
class DiagnosisConfig:
    """Whether diagnosis runs, and how much of it may run in one pass.

    Frozen: the cap is read once per pass, so a mid-pass mutation could not have a
    coherent meaning.

    Args:
        enabled: Whether to summon at all. Defaults to False — see
            :meth:`from_env`; the flag is fail-closed.
        max_summons_per_pass: Cost cap for a single pass. See
            :data:`DEFAULT_MAX_SUMMONS_PER_PASS`.
    """

    enabled: bool = False
    max_summons_per_pass: int = DEFAULT_MAX_SUMMONS_PER_PASS
    # `owner/name` of the repository the diagnoser is dispatched into. Read from
    # the dispatch story's own env var, not a second one: dispatching a diagnoser
    # somewhere other than where the engine dispatches its work would be a
    # surprise, and two vars could disagree. Empty means "record the diagnosis but
    # dispatch nothing" — see `_build_envelope`.
    repo: str = ""

    def __post_init__(self) -> None:
        if self.max_summons_per_pass < 1:
            raise ValueError(f"max_summons_per_pass must be at least 1; got {self.max_summons_per_pass}")

    @classmethod
    def from_env(cls) -> DiagnosisConfig:
        """Build from the process environment. **Fail-closed.**

        Enabled only when the flag is explicitly the literal ``"true"``
        (case-insensitive). Absent, empty, `"1"`, `"yes"` and anything else all
        resolve to *off*, matching `features/routes.py::_is_enabled_strict` — which
        is the semantics this flag is documented with in all three places it
        lands. `test_diagnose.py` pins the two implementations to the same
        behaviour so they cannot drift.

        Never raises: this runs on the tick path, and the alternative to a usable
        config is a broken tick. A bad cap degrades to the default, which is the
        conservative value anyway.
        """
        enabled = (os.environ.get(FEATURE_FLAG_ENV) or "").strip().lower() == "true"

        raw_cap = (os.environ.get(MAX_SUMMONS_PER_PASS_ENV) or "").strip()
        cap = DEFAULT_MAX_SUMMONS_PER_PASS
        if raw_cap:
            try:
                cap = int(raw_cap)
                if cap < 1:
                    raise ValueError(f"cap must be at least 1; got {cap}")
            except ValueError as exc:
                logger.warning(
                    "orchestration diagnose: %s=%r is not a usable cap (%s); using default %d",
                    MAX_SUMMONS_PER_PASS_ENV,
                    raw_cap,
                    exc,
                    DEFAULT_MAX_SUMMONS_PER_PASS,
                )
                cap = DEFAULT_MAX_SUMMONS_PER_PASS

        return cls(
            enabled=enabled,
            max_summons_per_pass=cap,
            repo=(os.environ.get(REPO_ENV) or "").strip(),
        )


@dataclass(frozen=True)
class Diagnosis:
    """One advisory diagnosis, before it is persisted.

    Deliberately carries no target state and no proposed transition. There is no
    field here in which a promotion could be expressed, which is propose-never-
    dispose at the level of the type rather than the level of the caller.
    """

    node_id: str
    org_id: str
    flow_id: str
    # The state the node was observed in. `from_state` on the persisted row — the
    # only state a diagnosis records, and it is a past-tense observation.
    observed_state: str
    # Which event triggered this: `node_stalled` or `node_halted`.
    trigger_kind: str
    # The human-readable finding. Persisted with `ADVISORY_PREFIX` prepended.
    summary: str

    @property
    def advisory(self) -> bool:
        """Always True. A diagnosis is never anything else.

        A property rather than a field so no caller can construct a diagnosis that
        claims to be authoritative. There is no code path to `advisory=False`.
        """
        return True


@dataclass(frozen=True)
class PendingDiagnosis:
    """A summon that has been recorded and not yet dispatched.

    Mirrors `dispatch_pass.PendingPublish` and exists for the same reason: the
    database write commits before anything is published, so the intent is held as
    data between the two phases rather than being an accident of where the `await`
    happens. The caller publishes after its commit.

    The envelope claims **no** human root (`is_human_rooted=False`,
    `root_human_id=None`). A diagnoser is engine-summoned housekeeping, not work a
    human asked for, and claiming otherwise would be exactly the elevated genesis
    this story must not have.
    """

    node_id: str
    org_id: str
    envelope: dict[str, Any]
    group_id: str
    deduplication_id: str


@dataclass
class DiagnosisReport:
    """What one diagnosis pass did.

    `suppressed_by_bound` is the cost guard's observability: it is how an operator
    confirms the bound is doing its job rather than inferring it from an absence of
    spend.
    """

    nodes_examined: int = 0
    diagnoses_recorded: int = 0
    # Candidates skipped because their newest stall/halt event is not newer than
    # their newest diagnosis. The bound working as designed, counted so it is
    # visible.
    suppressed_by_bound: int = 0
    # Diagnosed, but no diagnoser agent could be dispatched — no issue, ambiguous
    # installation, or unconfigured repo. NOT an error: the diagnosis record is the
    # deliverable and it landed. Counted so a permanently undispatchable
    # environment is visible rather than looking like every run succeeded.
    undispatchable: int = 0
    errors: int = 0
    # True when the per-pass cap stopped the pass early. Work is delayed, not
    # dropped — but "we ran out of budget" must not read as "there was nothing to
    # do".
    capped: bool = False
    # False when the feature flag is off. Surfaced so a disabled path is visible as
    # disabled rather than looking like a pass that found nothing.
    enabled: bool = True
    pending: list[PendingDiagnosis] = field(default_factory=list)
    per_org: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.errors == 0

    def _org(self, org_id: str) -> dict[str, int]:
        return self.per_org.setdefault(
            org_id,
            {
                "nodes_examined": 0,
                "diagnoses_recorded": 0,
                "suppressed_by_bound": 0,
                "undispatchable": 0,
                "errors": 0,
            },
        )

    def record(self, org_id: str, key: str, amount: int = 1) -> None:
        """Increment a counter both in total and for one org."""
        setattr(self, key, getattr(self, key) + amount)
        self._org(org_id)[key] += amount


def _build_summary(*, node: OrchestrationNode, trigger_kind: str, trigger_reason: str | None) -> str:
    """Assemble the context a reviewer would otherwise gather by hand.

    Deliberately mechanical: it states what is known — the graph address, the
    observed state, the attempt count and the recorded reason for the stall or halt
    — and draws no conclusion. The agent run summoned alongside this record is what
    produces analysis; this text is the starting point it and the human share.

    It is not a placeholder for model output. A record that existed only to be
    filled in later would be a row asserting a diagnosis exists when none does.
    """
    address = f"{node.flow_id}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
    recorded = trigger_reason or "no reason recorded"
    return (
        f"Node {address} ({node.kind}) was observed in '{node.state}' after {node.attempts} attempt(s). "
        f"Trigger: {trigger_kind}. Recorded reason: {recorded}. "
        "A diagnosing agent has been summoned to investigate; this record is the context it starts from "
        "and carries no authority to move the node."
    )


def record_diagnosis(session: AsyncSession, diagnosis: Diagnosis, *, node_org_id: str) -> OrchestrationDecision:
    """Append one advisory diagnosis row. **Writes no state.**

    An append to the append-only decisions table — the same table the rest of the
    engine records to, because a diagnosis is a decision *about* a node in exactly
    the sense that table already models. What makes it advisory rather than
    authoritative is the kind, the attribution and `to_state=None`, not a separate
    table with weaker guarantees.

    Args:
        session: The session to append through. Not committed.
        diagnosis: The finding. Carries no target state by construction.
        node_org_id: The `org_id` of the node as it was actually resolved. Passed
            separately and compared rather than taken from `diagnosis` on trust:
            the whole point is to catch a caller that resolved a node in one
            tenant and is attributing the finding to another.

    Returns:
        The appended row, un-flushed. The caller owns the transaction.

    Raises:
        TenantMismatchError: If `diagnosis.org_id` and `node_org_id` disagree.
    """
    if diagnosis.org_id != node_org_id:
        raise TenantMismatchError(
            f"refusing to write a diagnosis for node {diagnosis.node_id}: diagnosis claims org {diagnosis.org_id!r} "
            f"but the node resolved in org {node_org_id!r}; a diagnosis must never cross a tenant boundary"
        )

    row = OrchestrationDecision(
        org_id=diagnosis.org_id,
        flow_id=diagnosis.flow_id,
        node_id=diagnosis.node_id,
        kind=DecisionKind.NODE_DIAGNOSIS_PROPOSED.value,
        actor_id=DIAGNOSER_ACTOR_ID,
        actor_role=DIAGNOSER_ACTOR_ROLE,
        # SERVICE, never HUMAN. A human `actor_kind` on an agent's proposal is the
        # rubber-stamp failure written straight into the audit trail.
        actor_kind="service",
        reason=f"{ADVISORY_PREFIX} {diagnosis.summary}",
        # The state observed. Past tense, and the only state on the row.
        from_state=diagnosis.observed_state,
        # NULL, always. A diagnosis proposes nowhere for the node to go, so this
        # row cannot express a promotion. See the module docstring.
        to_state=None,
    )
    session.add(row)
    return row


def _build_envelope(
    *,
    node: OrchestrationNode,
    diagnosis: Diagnosis,
    installation_id: int,
    issue: int,
    config: DiagnosisConfig,
) -> dict[str, Any]:
    """Build the diagnoser's agent envelope. Same contract, no elevated genesis.

    The shape mirrors `dispatch_pass._build_envelope` because the envelope contract
    is what the worker consumes — including `source_ref`, whose three fields the
    worker's `parse_envelope` requires and rejects the message without. What differs
    is the only thing that should: `correlation` claims no human root, because no
    human asked for this run.

    `orchestration.advisory_only` is carried so the run itself knows it holds no
    promotion authority, rather than that being implied by its persona.
    """
    return {
        "version": _ENVELOPE_VERSION,
        "channel": "orchestration",
        "tenant_id": diagnosis.org_id,
        "persona": DIAGNOSER_PERSONA,
        "source_ref": {
            "installation_id": installation_id,
            "repo": config.repo,
            "issue": issue,
        },
        "intent": {
            "trigger": "engine_diagnosis",
            "label": None,
            "persona": DIAGNOSER_PERSONA,
        },
        "correlation": {
            # No human root. Engine-summoned housekeeping, and claiming a human
            # root would be the elevated genesis this story must not have.
            "root_human_id": None,
            "is_human_rooted": False,
            "chain_depth": 0,
        },
        "orchestration": {
            "node_id": node.id,
            "flow_id": diagnosis.flow_id,
            "graph_address": f"{node.flow_id}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}",
            "observed_state": diagnosis.observed_state,
            "trigger_kind": diagnosis.trigger_kind,
            # Explicit, so the run cannot mistake its own authority.
            "advisory_only": True,
        },
        "payload": {},
    }


@dataclass(frozen=True)
class _Candidate:
    """A node in a diagnosable state, with the trigger event that flagged it."""

    node: OrchestrationNode
    trigger_kind: str
    trigger_reason: str | None


async def _newest_decision(session: AsyncSession, *, node_id: str, org_id: str, kinds: frozenset[DecisionKind]) -> OrchestrationDecision | None:
    """The most recent decision of any of `kinds` for one node, or None.

    Filtered on `org_id` as well as `node_id`. The node id is a uuid and is
    already unique, so the tenant predicate is redundant for correctness — it is
    here because a tenant filter that is only present where it is strictly
    necessary is one refactor away from being absent where it is.
    """
    stmt = (
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.node_id == node_id,
            OrchestrationDecision.org_id == org_id,
            OrchestrationDecision.kind.in_(sorted(kind.value for kind in kinds)),
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _should_summon(session: AsyncSession, *, node_id: str, org_id: str) -> tuple[bool, OrchestrationDecision | None]:
    """The summon bound: one diagnosis per node per stall/halt event.

    Returns `(should_summon, trigger_event)`. The trigger is returned alongside the
    verdict so the caller does not re-query for the context it is about to record.

    The comparison is **strictly newer**, so a timestamp tie resolves against
    summoning. That is the correct bias for a cost guard: a tie is far more likely
    to be low clock resolution on an already-diagnosed node than a genuine second
    incident in the same instant.
    """
    trigger = await _newest_decision(session, node_id=node_id, org_id=org_id, kinds=TRIGGER_KINDS)
    if trigger is None:
        # No stall/halt event. The node reached a diagnosable state some other way
        # (a human failing it by hand, say), and nothing asked for a diagnosis.
        return False, None

    last_diagnosis = await _newest_decision(
        session,
        node_id=node_id,
        org_id=org_id,
        kinds=frozenset({DecisionKind.NODE_DIAGNOSIS_PROPOSED}),
    )
    if last_diagnosis is None:
        return True, trigger

    return trigger.created_at > last_diagnosis.created_at, trigger


async def _fetch_candidate_page(session: AsyncSession, *, after_id: str | None, limit: int) -> list[OrchestrationNode]:
    """One keyset page of nodes in a diagnosable state, ordered by id.

    Keyset rather than OFFSET, matching the tick and the stall detector:
    pagination stays correct while rows are updated underneath it and stays
    index-served as the table grows.
    """
    watched = sorted(state.value for state in DIAGNOSABLE_STATES)

    stmt = select(OrchestrationNode).where(OrchestrationNode.state.in_(watched))
    if after_id is not None:
        stmt = stmt.where(OrchestrationNode.id > after_id)

    return list((await session.execute(stmt.order_by(OrchestrationNode.id).limit(limit))).scalars().all())


async def _resolve_dispatch_target(session: AsyncSession, node: OrchestrationNode, *, config: DiagnosisConfig) -> tuple[int, int] | None:
    """The `(installation_id, issue)` a diagnoser envelope needs, or None.

    None means "record the diagnosis, dispatch nothing". That asymmetry is
    deliberate: the diagnosis *record* is this story's deliverable and is always
    written, while the agent run is an enhancement on top of it. A gate node with
    no issue, an org with an ambiguous installation, or an unconfigured target repo
    therefore still gets its assembled context in front of the human — it just gets
    no agent run alongside it.

    `resolve_installation_id` is imported from the dispatch story rather than
    reimplemented: it is a fail-closed check, and a second copy of a fail-closed
    check is free to drift from the original in the direction that matters.
    """
    if not config.repo:
        logger.warning("orchestration diagnose: %s is unset; recorded a diagnosis for node %s but dispatching no diagnoser", REPO_ENV, node.id)
        return None

    if not node.issue_ref:
        logger.info("orchestration diagnose: node %s has no issue_ref; diagnosis recorded but no diagnoser dispatched", node.id)
        return None

    try:
        issue = int(str(node.issue_ref).lstrip("#"))
    except ValueError:
        logger.warning(
            "orchestration diagnose: node %s has issue_ref=%r which is not an issue number; diagnosis recorded but no diagnoser dispatched",
            node.id,
            node.issue_ref,
        )
        return None

    installation_id = await resolve_installation_id(session, org_id=node.org_id)
    if installation_id is None:
        logger.warning(
            "orchestration diagnose: org %s has no single unambiguous GitHub installation; diagnosis recorded for node %s, no diagnoser sent",
            node.org_id,
            node.id,
        )
        return None

    return installation_id, issue


async def _diagnose_one(session: AsyncSession, candidate: _Candidate, report: DiagnosisReport, *, config: DiagnosisConfig) -> None:
    """Record one advisory diagnosis and queue the diagnoser's dispatch."""
    node = candidate.node

    diagnosis = Diagnosis(
        node_id=node.id,
        org_id=node.org_id,
        flow_id=node.flow_id,
        observed_state=node.state,
        trigger_kind=candidate.trigger_kind,
        summary=_build_summary(node=node, trigger_kind=candidate.trigger_kind, trigger_reason=candidate.trigger_reason),
    )

    # `node.org_id` twice is the point: the value is compared against itself only
    # because this call site resolved both from the same row. A caller that
    # assembled the two from different places is what the check catches.
    record_diagnosis(session, diagnosis, node_org_id=node.org_id)
    await session.flush()

    report.record(node.org_id, "diagnoses_recorded")

    logger.info(
        "orchestration diagnose: recorded advisory diagnosis for node %s (org %s, state %s, trigger %s)",
        node.id,
        node.org_id,
        node.state,
        candidate.trigger_kind,
    )

    # --- The dispatch half. The record above is the deliverable; the agent run is
    # --- the bonus, so an undispatchable node still gets its diagnosis context.
    dispatchable = await _resolve_dispatch_target(session, node, config=config)
    if dispatchable is None:
        report.record(node.org_id, "undispatchable")
        return

    installation_id, issue = dispatchable

    report.pending.append(
        PendingDiagnosis(
            node_id=node.id,
            org_id=node.org_id,
            envelope=_build_envelope(
                node=node,
                diagnosis=diagnosis,
                installation_id=installation_id,
                issue=issue,
                config=config,
            ),
            group_id=message_group_id(org_id=node.org_id, node_id=node.id),
            deduplication_id=message_deduplication_id(
                node_id=node.id,
                # Keyed on the triggering event rather than an approval decision:
                # a diagnosis is rooted in the stall/halt that caused it, and this
                # makes a second genuine incident a distinct message while a
                # re-examined node inside the dedup window is not.
                decision_id=f"diagnose:{candidate.trigger_kind}",
                attempt=node.attempts,
            ),
        )
    )


async def run_diagnosis_pass(session: AsyncSession, config: DiagnosisConfig | None = None) -> DiagnosisReport:
    """One diagnosis pass. **Commits nothing, and changes no node's state.**

    Finds nodes in a diagnosable state whose newest stall/halt event is newer than
    their newest diagnosis, records one advisory diagnosis each, and returns the
    dispatches it intends to publish. The caller commits and publishes, matching
    the dispatch story's commit-then-publish ordering.

    Args:
        session: Caller-owned session. Nothing is committed here.
        config: Whether diagnosis runs and the per-pass cap. Read from the
            environment when omitted, where it is fail-closed.

    Returns:
        A `DiagnosisReport`. `success` is False if any node errored.

    Never raises for a per-node failure: it records the error and continues, so one
    bad node cannot stop every other flow from being diagnosed (R-NF3).

    **Nothing in this package calls this function, by design.** See the module
    docstring on cut-safety; `test_diagnose.py` asserts the absence of callers.
    """
    cfg = config if config is not None else DiagnosisConfig.from_env()
    report = DiagnosisReport(enabled=cfg.enabled)

    if not cfg.enabled:
        # Fail-closed: the flag is off, so no agent is summoned and no row is
        # written. Reported as disabled rather than as an empty pass.
        logger.debug("orchestration diagnose: disabled (%s is not 'true'); no nodes examined", FEATURE_FLAG_ENV)
        return report

    after_id: str | None = None

    while report.nodes_examined < _ITEM_BACKSTOP:
        remaining = _ITEM_BACKSTOP - report.nodes_examined
        try:
            page = await _fetch_candidate_page(session, after_id=after_id, limit=min(_PAGE_SIZE, remaining))
        except Exception:
            # A page we cannot read is a real failure, not an empty one.
            logger.exception("orchestration diagnose: failed to fetch candidate page after id=%s", after_id)
            report.errors += 1
            break

        if not page:
            break

        for node in page:
            report.record(node.org_id, "nodes_examined")
            try:
                should, trigger = await _should_summon(session, node_id=node.id, org_id=node.org_id)
                if not should:
                    if trigger is not None:
                        # A stall/halt event exists but has already been
                        # diagnosed. This is the cost guard firing.
                        report.record(node.org_id, "suppressed_by_bound")
                    continue

                if report.diagnoses_recorded >= cfg.max_summons_per_pass:
                    report.capped = True
                    logger.warning(
                        "orchestration diagnose: per-pass cap of %d reached; remaining nodes wait for the next pass",
                        cfg.max_summons_per_pass,
                    )
                    return report

                await _diagnose_one(
                    session,
                    _Candidate(node=node, trigger_kind=trigger.kind, trigger_reason=trigger.reason),
                    report,
                    config=cfg,
                )
            except Exception:
                # Per-node containment: log, count, force non-success, keep going.
                logger.exception("orchestration diagnose: failed to diagnose node %s (org %s)", node.id, node.org_id)
                report.record(node.org_id, "errors")

        after_id = page[-1].id

        if len(page) < _PAGE_SIZE:
            break
    else:
        logger.warning("orchestration diagnose: stopped at the %d-node backstop; more candidates remain", _ITEM_BACKSTOP)

    return report
