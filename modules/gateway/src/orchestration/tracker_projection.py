"""One-way projection of engine state onto the EPIC issue's tracker region.

Issue #5284 (EPIC #4191, coordinator #5134). The AIDLC inception persona writes a
generated region into the intent issue body between `<!-- aidlc-tracker:start -->`
and `<!-- aidlc-tracker:end -->` (`rules/personas/aidlc.md`, "Live Tracker"), and it
writes it twice: at its own run start, and before it ends. Nothing writes it after
that. Execution then proceeds — stories dispatch, run, merge, and release their
dependents — and none of it reaches the issue, so the region keeps showing the
planning snapshot indefinitely.

That is the whole defect: on 2026-09-16 the engine held seven stories `passed` and
one `running`, GitHub held the seven merged pull requests, and the region still read
"U1 just dispatched". The engine's state was correct throughout. **This module adds
the missing projection, not a state fix** — no transition, no dispatch, no gate.

**The direction is one-way and it is a security property, not a style choice.** The
engine writes the region; it never reads it back. Nothing in this module returns a
value that any caller uses to decide a transition, and the only thing it reads from
GitHub is the current body — used solely to locate the sentinels and to preserve
everything outside them. If the projection ever became an input, a human editing
generated prose would be steering execution, and the accepted plan would no longer
be the only authority over what runs.

Four invariants are load-bearing:

**Remote writes happen after the caller's commit.** Split in the same two-phase
shape as `engine_commands.flush_engine_commands` and `dispatch_pass.publish_pending`:
:func:`run_tracker_projection_pass` reads state and renders text while the transition
session is open, resolving the installation id then; the flush does the HTTP after
that session commits. The flush uses a separate session only to hold a target-scoped
advisory lock. No row or transition lock is held across the GitHub round trip.

**Projection failure cannot touch execution (AC3).** The flush never raises. A
provider error is counted, forces a non-success report, and is retried by the next
tick from freshly-read state. Because the flush runs post-commit and its lock session
never reads or writes engine rows, there is no code path from a failed GitHub call
back to a node's state — the isolation is structural rather than a promise to be
careful.

**Only the region between the sentinels is ever written (AC2).** :func:`splice_region`
refuses on zero, malformed, duplicated or inverted sentinels, and returns the reason.
A refusal writes nothing at all; it never falls back to appending or to replacing the
body. The user's intent text is what the issue is *for*, and a projection that can
eat it is worse than one that is stale.

**A stale snapshot never overwrites a fresher one (AC2/AC3).** Each rendered region
carries a server-generated flow-id marker plus
`<!-- aidlc-tracker-snapshot: ... -->`, holding that flow's accepted-plan version and
a monotonic engine watermark. A different flow targeting an already-bound region is
refused because its counters are not comparable. A post-commit PostgreSQL advisory
lock serializes each issue's engine writers across tick invocations; inside that lock
the flush reads the current body, compares its markers, and only then writes. The
markers decide identity and freshness while the lock closes the read/modify/write
race. This deliberately stores no new column — the durable execution ledger is
#5142's scope, not this story's.

Idempotence (AC1) falls out of rendering being pure: an unchanged graph renders
byte-identical text, which compares equal to what is already on the issue, so the
pass skips the write entirely rather than editing the issue on every wake.

Tenant isolation: every read filters on `org_id`, and the token is minted for that
tenant's installation scoped to the one repository with `issues:write` only. A target
that does not resolve unambiguously to the flow's authorized tenant, repository and
EPIC issue is refused rather than guessed at.

**Authorization, and why it is not implied by the above.** An earlier revision of this
docstring claimed the write target came from "the flow's own nodes rather than from
anything a caller supplied". That was wrong, and the review of PR #5337 found it: the
EPIC number is parsed out of `epic_ref`, which `compile.upsert_nodes` stores verbatim
from the submitted node address, so the nodes *are* caller data. Combined with a
process-wide `BG_ORCH_DISPATCH_REPO`, the target was author-chosen. Since `PLAN_DRAFT`
reaches every ordinary member and a draft still compiles a graph, that let an
unprivileged user have the engine overwrite another team's tracker region under its own
bot identity. :func:`_is_approved` now requires the same human approval that arms
dispatch, so the authority to publish about an EPIC is the authority to run work there.

**Author text cannot forge the region's structure.** Everything interpolated into the
rendered region is author-supplied, and a literal sentinel inside it would make
:func:`splice_region` refuse forever after one write — silently, since a refusal is not
a failure. :func:`_neutralized` closes that, along with the newline that let a forged
"passed" row be published under the engine's attribution.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .display_state import ENGINE_TO_DISPLAY, DisplayState, derive_flow_status
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from .pr_bindings import BindingRefusal, active_bindings_for_flow
from .state import ActorKind, NodeState

logger = logging.getLogger("bedrockgateway.orchestration.tracker_projection")

__all__ = [
    "REGION_END",
    "REGION_START",
    "PendingTrackerProjection",
    "RegionRefusal",
    "TrackerProjectionConfig",
    "TrackerProjectionReport",
    "flush_tracker_projections",
    "render_region",
    "run_tracker_projection_pass",
    "splice_region",
]


# The sentinels, spelled exactly as `rules/personas/aidlc.md` specifies and as the
# inception persona already writes them. Re-declared rather than imported because
# that spec is a markdown rules file in a different deploy unit; the pair is pinned
# by a test asserting these literals appear in that document.
REGION_START = "<!-- aidlc-tracker:start -->"
REGION_END = "<!-- aidlc-tracker:end -->"

# Machine-readable freshness marker embedded in the rendered region. Parsed back off
# the issue to reject a stale overwrite. `version` is the accepted-plan version and
# `watermark` a monotonic engine counter; both are integers so the comparison is
# total and needs no clock (the tick has no trustworthy wall clock ordering across
# overlapping invocations).
_SNAPSHOT_MARKER = "<!-- aidlc-tracker-snapshot: v{version} w{watermark} -->"
_SNAPSHOT_RE = re.compile(r"<!--\s*aidlc-tracker-snapshot:\s*v(\d+)\s+w(\d+)\s*-->")

# Plan versions and watermarks are ordered only inside one flow. The schema permits
# two approved flows to name the same EPIC, so bind the region to the first
# execution-time writer rather than letting unrelated counters overwrite each other.
# Kept separate from the snapshot marker so a legacy/persona region can be adopted.
_FLOW_MARKER = "<!-- aidlc-tracker-flow: {flow_id} -->"
_FLOW_RE = re.compile(r"<!--\s*aidlc-tracker-flow:\s*([A-Za-z0-9-]{1,64})\s*-->")

# The engine flag, read here rather than given one of its own — same reasoning as
# `engine_commands.py` and `diagnose.py`: a second flag would let this pass write
# progress claims about an engine that is switched off. Reusing it also keeps this
# story out of the three-place parity contract in `test_feature_flag_parity.py`.
FEATURE_FLAG_ENV = "FEATURE_ORCHESTRATION_ENGINE_ENABLED"

# The repository the engine delivers into, same variable `dispatch_pass` reads.
# `OrchestrationNode` carries no repository column, so there is no per-node
# alternative to read instead.
REPO_ENV = "BG_ORCH_DISPATCH_REPO"

# Flows projected per invocation. Bounds one very large tenant's cost so it cannot
# starve every other tenant's projection, following `_ITEM_BACKSTOP` in `tick.py`.
# Hitting it sets `capped`, which is logged — "we ran out of budget" must never read
# as "everything is up to date".
DEFAULT_MAX_FLOWS_PER_PASS = 20
MAX_FLOWS_ENV = "ORCH_MAX_PROJECTIONS_PER_PASS"

# The scheduled tick defaults to every five minutes.  Capped selection uses that
# interval only to choose a stable rotation slot; freshness and stale-write safety
# continue to use engine state, never wall-clock ordering.
_ROTATION_INTERVAL_SECONDS = 5 * 60

# Rendered rows per flow. A plan with thousands of nodes would otherwise produce an
# issue body past GitHub's 65536-character limit, and a rejected write is a silent
# stall. Overflow is stated in the rendered text, never dropped quietly.
_MAX_ROWS = 60

# PostgreSQL is the production database, so an advisory transaction lock gives all
# tick Lambdas one serialization point without adding a row or entering #5142's
# durable-ledger scope. SQLite is used only by local development and tests; its
# fallback is process-local because it has no advisory-lock primitive.
_LOCAL_WRITE_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


class RegionRefusal(str):
    """Why a splice wrote nothing. A `str` subclass so it renders in a log line.

    Distinct values rather than one "bad sentinels" reason because they call for
    different human responses: a *missing* region means this issue was never
    initialised by the inception persona (expected, benign), while a *duplicated*
    one means two writers disagree about which region is authoritative and needs a
    person to look.
    """


REGION_MISSING = RegionRefusal("region_missing")
REGION_DUPLICATED = RegionRefusal("region_duplicated")
REGION_MALFORMED = RegionRefusal("region_malformed")
REGION_STALE = RegionRefusal("region_stale")


def _neutralized(text: str) -> str:
    """Make engine-stored text unable to forge a sentinel or the snapshot marker.

    Review finding, PR #5337. Every string interpolated into the rendered region —
    a node's `title`, its `issue_ref`, the flow's `slug` — is author-supplied and
    reaches the database through plan registration, which any holder of
    `PLAN_DRAFT` can call. Untreated, a title containing a literal
    `<!-- aidlc-tracker:end -->` renders inside the region and the spliced body then
    carries two end sentinels, so :func:`splice_region` answers `region_duplicated`
    on **every subsequent tick** — a refusal that is deliberately not a failure, so
    the tick stays green and the tracker is frozen with nobody told. The poison
    lives on the node row, so it re-renders even after a human repairs the issue by
    hand. That is this story's own defect, reachable by anyone who can name a story.

    Same shape and same reason as `adapters/github_comments._neutralize_marker`:
    only `<` can open an HTML comment, so replacing that one character is
    sufficient, and the substitution is one character to one character — hence
    length-preserving, so it composes with the row truncation below in either
    order. `<!-- aidlc-tracker:end -->` becomes `(!-- aidlc-tracker:end -->`, which
    no `count()` for the real sentinel and no `_SNAPSHOT_RE` search can see.

    **Every** comment opener is neutralized, not only the ones that currently spell
    a sentinel. Matching `aidlc-tracker` specifically would have to agree with two
    different readers — `splice_region`'s literal `count()` and `_SNAPSHOT_RE`,
    which accepts arbitrary whitespace after `<!--` — and a neutralizer that
    disagrees with either is a hole shaped exactly like the bug it was written to
    close. Nothing legible is lost: an HTML comment renders as nothing at all, so a
    story title cannot be relying on one to say something to a reader.

    Newlines are folded to spaces in the same pass. A title carrying `\\n` breaks
    out of its table row, which is how a forged extra row claiming another story
    `✅ passed` gets published under the region's "written by the engine from its
    own records" attribution — misrepresentation to the human about to answer a
    gate, not merely a broken table.
    """
    if not text:
        return ""
    flattened = text.replace("\r", " ").replace("\n", " ")
    if "<!--" not in flattened:
        return flattened
    out = list(flattened)
    start = flattened.find("<!--")
    while start != -1:
        out[start] = "("
        start = flattened.find("<!--", start + 1)
    return "".join(out)


def _display_of(state: str) -> DisplayState | None:
    """Map an engine state through the single existing projection.

    Deliberately delegates to `display_state.ENGINE_TO_DISPLAY` rather than carrying
    its own table: two copies of a state mapping is the exact drift that module was
    written to end, and its parity test already pins it against the frontend. An
    unknown state (a member added later without touching this file) maps to `None`
    and is rendered as unknown rather than silently bucketed as done.
    """
    try:
        return ENGINE_TO_DISPLAY.get(NodeState(state))
    except ValueError:
        return None


def _rotation_slot() -> int:
    """A process-independent slot used to rotate capped reconciliation work."""
    return int(datetime.now(UTC).timestamp()) // _ROTATION_INTERVAL_SECONDS


def _mixed_start(slot: int, pool_size: int) -> int:
    """Map a slot to a rotation start whose coverage does not depend on the cadence.

    Review finding F1, PR #5337. The previous form was ``(slot * rotation_count) %
    pool_size``, which is only fair when the slot advances by exactly one per pass.
    It does not: :func:`_rotation_slot` divides the wall clock by a hardcoded five
    minutes, while the tick's cadence is a supported Terraform knob
    (``orchestration_tick_schedule`` -> ``tick_schedule``) whose own description
    invites a slower value. At a cadence of ``k`` times five minutes the slot
    advances by ``k``, the reachable starts are the multiples of
    ``gcd(rotation_count * k, pool_size)``, and any flow outside that subgroup is
    **never** selected -- tracker frozen while the flow advances, tick green, no
    error, because a skipped projection is deliberately not a failure. That is the
    exact defect this module exists to remove, reinstated by changing a documented
    setting.

    Mixing the slot through a fixed integer avalanche (splitmix64's finalizer)
    breaks the arithmetic relationship between the step size and the start, so no
    cadence collapses the sequence onto a subgroup. Measured over cap 1..24 and
    pools up to 79: the old form starves 224 combinations at double the default
    cadence and 623 at sixty times it, and this form starves none at any of those
    steps.

    **The tradeoff, stated because "starves none" measures reachability and not
    latency.** The old form was a clean sequential sweep, so at the default cadence
    its worst-case wait was bounded and short; this samples pseudorandomly, so waits
    have a geometric tail -- at cap 20 the worst observed gap over 4000 ticks is ~69
    ticks at a pool of 79 and ~190 at 200, against a handful for a sweep. Every flow
    is still reached, and a bounded-but-long delay is the thing the cap already
    trades for; permanent staleness at a supported cadence is not. A persisted
    per-pass cursor would give both, and is the better long-term shape -- it is left
    out here only because it needs durable state, which is #5142's scope.

    ``slot % pool_size`` was measured too and only *reduces* starvation (78
    combinations at double cadence), which is why it is not what ships.

    The mixing must be reproducible across processes, because two tick Lambdas on the
    same slot have to select the same flows rather than take turns writing conflicting
    snapshots. Plain arithmetic and the :func:`hashlib.blake2b` already used for the
    advisory-lock key below both satisfy that; builtin :func:`hash` on a string does
    not, being per-process salted. Arithmetic is used here simply because it needs no
    encoding step. Nothing is authenticated by the result -- the input is a
    clock-derived slot number, so this is a fairness device, not a security one.
    """
    mixed = (slot + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    mixed = ((mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    mixed = ((mixed ^ (mixed >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    mixed ^= mixed >> 31
    return mixed % pool_size


def _select_capped_flows(flow_ids: list[str], *, cap: int, slot: int) -> list[str]:
    """Keep recent work responsive while guaranteeing old work gets a turn.

    ``flow_ids`` arrives most-recently-active first.  Half the cap remains reserved
    for that priority order; the other half walks a stable circular order.  With a
    cap of one, fairness owns the single slot because reserving it permanently for
    the newest flow would make starvation unavoidable.

    The rotation start comes from :func:`_mixed_start`, so fairness holds at every
    tick cadence rather than only at the default one.
    """
    if len(flow_ids) <= cap:
        return flow_ids

    priority_count = cap // 2
    priority = flow_ids[:priority_count]
    priority_set = set(priority)
    rotation_pool = sorted(flow_id for flow_id in flow_ids if flow_id not in priority_set)
    rotation_count = min(cap - len(priority), len(rotation_pool))
    start = _mixed_start(slot, len(rotation_pool))
    rotating = [rotation_pool[(start + offset) % len(rotation_pool)] for offset in range(rotation_count)]
    return [*priority, *rotating]


_DISPLAY_LABEL: dict[DisplayState, str] = {
    DisplayState.QUEUED: "⏳ waiting",
    DisplayState.IN_PROGRESS: "🔄 running",
    DisplayState.GATE: "🚦 needs your decision",
    DisplayState.STALLED: "⚠️ needs attention",
    DisplayState.COMPLETE: "✅ passed",
}

# Engine states that mean "the worker finished, the merge/checks are not verified
# yet" — surfaced separately from plain `running` because the two need different
# reader responses, and AC1 names review/repair as its own visible condition.
_IN_REVIEW = {NodeState.AWAITING_MERGE.value}


@dataclass(frozen=True)
class TrackerProjectionConfig:
    """Whether the pass runs, where it writes, and how much per pass.

    Frozen: read once per pass, so a mid-pass mutation could not have a coherent
    meaning (same reasoning as `EngineCommandConfig`).
    """

    enabled: bool = False
    repo: str = ""
    max_flows_per_pass: int = DEFAULT_MAX_FLOWS_PER_PASS

    @property
    def configured(self) -> bool:
        """Whether this config can actually reach an issue.

        An empty repository means there is nothing to write to. Reported as a
        disabled pass rather than raising: the tick has five other passes to run.
        """
        return bool(self.repo)

    @classmethod
    def from_env(cls) -> TrackerProjectionConfig:
        """Build from the process environment. **Fail-closed, and never raises.**

        Enabled only on the literal ``"true"`` (case-insensitive); absent, empty,
        ``"1"`` and ``"yes"`` all resolve to *off*, matching
        `features/routes.py::_is_enabled_strict` and hand-rolled here for the reason
        `engine_commands.py` hand-rolls it — importing a route module onto the tick
        path to read one boolean would make the tick depend on the request stack.

        Never raises: this runs on the tick path, and the alternative to a usable
        config is a broken tick. A malformed cap degrades to the default.
        """
        enabled = (os.environ.get(FEATURE_FLAG_ENV) or "").strip().lower() == "true"

        raw_cap = (os.environ.get(MAX_FLOWS_ENV) or "").strip()
        cap = DEFAULT_MAX_FLOWS_PER_PASS
        if raw_cap:
            try:
                cap = int(raw_cap)
                if cap < 1:
                    raise ValueError(f"cap must be at least 1; got {cap}")
            except ValueError as exc:
                logger.warning(
                    "orchestration tracker projection: %s=%r is not a usable cap (%s); using default %d",
                    MAX_FLOWS_ENV,
                    raw_cap,
                    exc,
                    DEFAULT_MAX_FLOWS_PER_PASS,
                )
                cap = DEFAULT_MAX_FLOWS_PER_PASS

        return cls(
            enabled=enabled,
            repo=(os.environ.get(REPO_ENV) or "").strip(),
            max_flows_per_pass=cap,
        )


@dataclass(frozen=True)
class PendingTrackerProjection:
    """One rendered region awaiting its post-commit write.

    Frozen, and carrying everything the flush needs — including `installation_id`,
    resolved during the pass while the session is open, exactly as `PendingEngineAck`
    does. The flush therefore touches no database, which is what lets it run after
    the commit with no risk of reopening a transaction.
    """

    org_id: str
    flow_id: str
    repo: str
    issue_number: int
    installation_id: int
    body_region: str
    #: Accepted-plan version and engine watermark this region was rendered from.
    #: Compared against the marker already on the issue to refuse going backwards.
    version: int
    watermark: int


@dataclass
class TrackerProjectionReport:
    """What one projection pass did.

    Counters are the only way an operator can tell "no flow needed an update" from
    "every write was refused" — nothing else surfaces a refusal, because by
    construction this pass never tells a human anything except by updating the
    region it failed to update.
    """

    flows_examined: int = 0
    #: Regions actually written to GitHub.
    projections_written: int = 0
    #: Rendered identically to what the issue already held, so no write was made.
    #: Counted apart from `written` because this is the steady state on a quiet
    #: flow, and folding the two together would make a pass that changes nothing
    #: indistinguishable from a pass that could not write.
    projections_unchanged: int = 0
    #: No unambiguous tenant/repository/issue target, or no installation. A refusal
    #: is not a failure: an EPIC issue that was never initialised with sentinels is
    #: the normal case for a hand-run flow.
    projections_refused: int = 0
    #: Declined because the issue already held a newer snapshot.
    projections_stale: int = 0
    #: A GitHub read or write that did not land. Its own counter, and it forces a
    #: non-success report: an update that never arrived is the invisible-staleness
    #: outcome this story exists to remove.
    projections_failed: int = 0
    errors: int = 0
    #: True when the per-pass cap stopped the pass early. Work is delayed, not
    #: dropped.
    capped: bool = False
    #: False when the flag is off or no repository is configured. Surfaced so a
    #: disabled path is visible as disabled rather than looking like a clean pass.
    enabled: bool = True
    pending: list[PendingTrackerProjection] = field(default_factory=list)
    per_org: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """A refusal is not a failure; an undelivered update is.

        `projections_stale` is likewise not a failure — declining to go backwards is
        the correct outcome, and counting it as an error would make healthy
        overlapping ticks look broken.
        """
        return self.errors == 0 and self.projections_failed == 0

    def _org(self, org_id: str) -> dict[str, int]:
        """Per-org counters, seeded with this report's own key set.

        Seeded explicitly (as in `EngineCommandReport`) so `record` raises on a
        typo'd key rather than silently inventing a counter nobody reads.
        """
        return self.per_org.setdefault(
            org_id,
            {
                "flows_examined": 0,
                "projections_written": 0,
                "projections_unchanged": 0,
                "projections_refused": 0,
                "projections_stale": 0,
                "projections_failed": 0,
                "errors": 0,
            },
        )

    def record(self, org_id: str, key: str, amount: int = 1) -> None:
        """Increment a counter both in total and for one org."""
        setattr(self, key, getattr(self, key) + amount)
        self._org(org_id)[key] += amount


def splice_region(body: str, region: str) -> str | RegionRefusal:
    """Replace only what lies between the sentinels. Never touches anything else.

    Returns the new body, or a :class:`RegionRefusal` explaining why nothing should
    be written. **There is deliberately no "append the region if it is missing"
    path.** Creating the region is the inception persona's job; an engine that
    appends one would write a progress block onto whatever issue it was pointed at,
    including one that is not a tracker at all.

    Refuses when the sentinels are absent, duplicated, or out of order. Each refusal
    is a no-write, so the caller's fallback is always "leave the issue alone" rather
    than a partial edit. The text before the start sentinel and after the end
    sentinel is preserved byte-for-byte, including the sentinels themselves.
    """
    starts = body.count(REGION_START)
    ends = body.count(REGION_END)
    if starts == 0 or ends == 0:
        return REGION_MISSING
    if starts > 1 or ends > 1:
        # Two regions means two writers disagree about which is authoritative.
        # Picking one would leave the other permanently stale and contradicting it.
        return REGION_DUPLICATED

    head = body.index(REGION_START)
    tail = body.index(REGION_END)
    if tail < head:
        return REGION_MALFORMED

    prefix = body[: head + len(REGION_START)]
    suffix = body[tail:]
    return f"{prefix}\n{region.strip()}\n{suffix}"


def _generated_region(body: str) -> str:
    """Only the text the engine owns: what lies between the sentinels.

    Review finding, PR #5337. Both markers below used to be searched across the
    *whole* body, but a marker is only authoritative when the engine wrote it, and
    the engine only ever writes inside the sentinels. Everything outside them is the
    human's own text, and quoting a tracker snapshot there ("last week it read…") is
    an ordinary thing for a person to do on their own EPIC issue.

    Untreated, that quoted text was read as the published state: a pasted
    `w9999` made every real update look stale, and a pasted flow marker made the
    issue look bound to another flow. Both outcomes are *refusals*, which are
    deliberately not failures, so the tick stayed green and the region froze with
    nobody told — this story's own defect, reachable without any privilege by
    editing one's own issue.

    Returns `""` when the sentinels are absent, duplicated or inverted. That reads
    as "no marker published", which is the same conservative answer the readers
    already gave for an uninitialised region, and `splice_region` independently
    refuses those bodies anyway — so no write decision rests on this alone.
    """
    if body.count(REGION_START) != 1 or body.count(REGION_END) != 1:
        return ""
    head = body.index(REGION_START) + len(REGION_START)
    tail = body.index(REGION_END)
    return body[head:tail] if tail >= head else ""


def read_snapshot(body: str) -> tuple[int, int] | None:
    """The `(version, watermark)` already published in this body, if any.

    `None` means the region carries no marker — either it predates this feature or
    it was written by the inception persona, which does not emit one. Treated as
    "older than anything" by the caller, so the first execution-time projection is
    free to land on a persona-written region.

    Reads the *first* marker inside the generated region only. A marker in the
    human's own text outside the sentinels is not a published snapshot and must not
    be able to make a real update look stale.
    """
    found = _SNAPSHOT_RE.search(_generated_region(body))
    if found is None:
        return None
    return int(found.group(1)), int(found.group(2))


def _read_flow_identity(body: str) -> str | None:
    """Return the server-generated flow id owning the region, if present.

    Scoped to the generated region for the same reason as :func:`read_snapshot`: a
    flow marker quoted in the human's own prose is not a binding.
    """
    found = _FLOW_RE.search(_generated_region(body))
    return found.group(1) if found is not None else None


def _row_status(node: OrchestrationNode, binding: object | None) -> str:
    """The human-readable status for one story row.

    Review/repair is surfaced distinctly from running (AC1): a node in
    `awaiting_merge` has had its worker finish, and what it is waiting for is merge
    and check verification — a materially different thing to tell a reader than
    "still executing".

    **Merge is deliberately not asserted here.** There is no `merged` column on the
    binding: merge is live provider evidence (`MergeEvidence`, read by `results.py`),
    and the engine's durable answer to "did this merge and was it accepted" is the
    node reaching `passed`. Rendering "merged" from a binding's mere existence would
    claim evidence this pass never read — a projection must not manufacture a
    verification status. So a bound-but-unaccepted story reads as awaiting
    verification, and the PR column carries the pull request for the reader to check.
    """
    if node.state == NodeState.WAIVED.value:
        return "↷ waived by owner approval (not evaluated)"
    display = _display_of(node.state)
    if display is None:
        return "❔ unknown"
    if node.state in _IN_REVIEW:
        return "🔍 in review / awaiting merge verification"
    return _DISPLAY_LABEL[display]


def _pr_cell(binding: object | None) -> str:
    """The pull-request column for one story.

    An ambiguous binding is rendered as such rather than resolved: two active
    bindings for one story means the engine cannot say which pull request carried
    the work, and printing either would assert a provenance nobody verified.
    """
    if isinstance(binding, BindingRefusal):
        return "⚠️ ambiguous"
    if isinstance(binding, OrchestrationPullRequestBinding):
        number = getattr(binding, "pr_number", None)
        if number:
            return f"#{number}"
    return "—"


def render_region(
    *,
    flow: OrchestrationFlow,
    nodes: list[OrchestrationNode],
    bindings: dict[str, object],
    version: int,
    watermark: int,
    observed_at: datetime,
) -> str:
    """Render the tracker region from engine state. Pure — no IO, no clock.

    `observed_at` is passed in rather than read here so the output is a function of
    its inputs alone; that is what makes the idempotence test (AC1) able to assert
    byte-equality across two renders, and what keeps a stray clock read from
    producing a spurious write on every tick.

    The rendered text states the source flow and the snapshot time (AC1) so a reader
    can judge freshness, and labels the planning inventory as historical (AC2) so the
    kickoff plan is not mistaken for live status. It carries no approval language and
    no acceptance criteria: this is a status display, and rewriting acceptance from a
    projection is explicitly out of scope.
    """
    counts = {display: 0 for display in DisplayState}
    for node in nodes:
        display = _display_of(node.state)
        if display is not None:
            counts[display] += 1

    status = derive_flow_status(
        queued=counts[DisplayState.QUEUED],
        in_progress=counts[DisplayState.IN_PROGRESS],
        gate=counts[DisplayState.GATE],
        stalled=counts[DisplayState.STALLED],
        complete=counts[DisplayState.COMPLETE],
    )

    total = sum(counts.values())
    done = counts[DisplayState.COMPLETE]
    filled = round(10 * done / total) if total else 0
    bar = "▓" * filled + "░" * (10 - filled)

    lines = [
        _SNAPSHOT_MARKER.format(version=version, watermark=watermark),
        _FLOW_MARKER.format(flow_id=flow.id),
        "",
        "### Execution progress",
        "",
        f"**Status**: {status.value.replace('_', ' ')} · `{bar}` {done}/{total} stories passed",
        "",
        "| Story | Status | PR |",
        "|-------|--------|----|",
    ]

    shown = nodes[:_MAX_ROWS]
    for node in shown:
        # Every interpolated value here is author-supplied (plan registration writes
        # them verbatim), so each is neutralized before it can forge a sentinel or
        # the snapshot marker, and `|` is escaped so it cannot forge a table cell.
        issue = _neutralized(node.issue_ref or "").replace("|", "\\|").lstrip("#").strip()
        label = f"#{issue}" if issue else _neutralized(node.node_ref).replace("|", "\\|").strip()
        title = _neutralized(node.title or "").replace("|", "\\|").strip()
        if len(title) > 80:
            title = title[:77] + "…"
        lines.append(f"| {label} {title} | {_row_status(node, bindings.get(node.id))} | {_pr_cell(bindings.get(node.id))} |")

    if len(nodes) > len(shown):
        # Stated, never silently truncated: a table that stops without saying so
        # reads as a complete inventory.
        lines.append(f"| _…{len(nodes) - len(shown)} more not shown_ | — | — |")

    lines += [
        "",
        f"_Live execution snapshot from flow `{_neutralized(flow.slug)}` (accepted plan v{version}), "
        f"verified at {observed_at.strftime('%Y-%m-%d %H:%M:%S')} UTC. "
        "Written by the engine from its own records; any planning inventory above this region is historical._",
    ]
    return "\n".join(lines)


async def _epic_issue_number(nodes: list[OrchestrationNode]) -> int | None:
    """The EPIC issue this flow's nodes agree on, or None.

    The EPIC number lives in the graph address as the `epic-<N>` segment — the
    emission skill writes `<flow>/epic-<EPIC>/wave-<K>/<node>` — because containers
    are derived and no EPIC table exists to read instead.

    **Ambiguity is a refusal, not a choice** (the rule `_resolve_target` in
    `engine_commands.py` applies for the same reason). A flow whose nodes name two
    different EPICs gives no basis to pick, and guessing would publish one EPIC's
    progress onto another's issue.
    """
    refs = {node.epic_ref for node in nodes if node.epic_ref}
    numbers = set()
    for ref in refs:
        found = re.fullmatch(r"epic-#?(\d+)", ref.strip())
        if found is None:
            return None
        numbers.add(int(found.group(1)))
    if len(numbers) != 1:
        return None
    number = numbers.pop()
    return number if number > 0 else None


async def _is_approved(session: AsyncSession, *, org_id: str, flow_id: str) -> bool:
    """Whether a human has approved this flow's plan, by the shared rule.

    Review finding, PR #5337. `APPROVAL_DECISION_KINDS` is imported from `genesis.py`
    rather than restated, for the reason that module states: the set is what counts as
    a human accepting work, and a second copy is free to drift. `PLAN_DRAFTED` is
    deliberately absent from it, which is exactly the distinction that matters here —
    a draft is an unapproved proposal, and the engine must not publish status about an
    EPIC on the strength of one.

    This mirrors the complete authorization gate used by engine genesis: the row
    must both carry an approval kind and identify a real human actor.  Checking
    only the kind would let a service-written or unattributed row publish a draft
    as though a human had accepted it, even though `resolve_engine_genesis` would
    correctly refuse to dispatch from that same row.  The query is also filtered
    on `org_id` in SQL as well as `flow_id`, so a flow id from another tenant must
    resolve to nothing rather than to a row.

    An unapproved flow is a `projections_refused`, alongside the uninitialised-region
    case — both mean "nothing to publish here yet", and neither is a failure.
    """
    from .genesis import APPROVAL_DECISION_KINDS

    decision = (
        await session.execute(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == org_id,
                OrchestrationDecision.flow_id == flow_id,
                OrchestrationDecision.kind.in_(sorted(APPROVAL_DECISION_KINDS)),
            )
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return decision is not None and decision.actor_kind == ActorKind.HUMAN.value and bool(decision.actor_id)


async def run_tracker_projection_pass(
    session: AsyncSession,
    config: TrackerProjectionConfig | None = None,
) -> TrackerProjectionReport:
    """Read engine state and render what each active flow's region should say.

    **Reads only. Commits nothing, transitions nothing, dispatches nothing.** The
    returned report carries the rendered regions in `pending`; the caller commits and
    then calls :func:`flush_tracker_projections`. Call this after the tick's own
    passes so the rendered snapshot reflects the motion this invocation produced.

    A flow that raises is counted and skipped; every other flow still projects. One
    tenant's malformed plan must not blank every other tenant's progress display.
    """
    cfg = config if config is not None else TrackerProjectionConfig.from_env()
    report = TrackerProjectionReport()
    if not cfg.enabled or not cfg.configured:
        report.enabled = False
        return report

    # Candidate flows arrive most-recently advanced first.  Capped selection keeps
    # half that recency order for responsive active-flow updates and rotates the
    # remainder in stable flow-id order.  Recency alone is not sufficient: if 21
    # flows finish under a cap of 20, the omitted terminal flow never advances again,
    # while the same top 20 remain top forever.  The rotation is what turns the cap
    # into bounded delay rather than permanent starvation.
    #
    # `coalesce` because `updated_at` is NULL until a row is first updated (it is an
    # `onupdate` column with no default), and a flow whose nodes have never moved must
    # still order by when it was created rather than sorting below everything.
    last_activity = func.max(func.coalesce(OrchestrationNode.updated_at, OrchestrationNode.created_at))
    flow_ids = list(
        (
            await session.execute(
                select(OrchestrationNode.flow_id)
                .where(
                    OrchestrationNode.state.notin_([NodeState.SUPERSEDED.value]),
                )
                .group_by(OrchestrationNode.flow_id)
                # `flow_id` only as a tiebreak, so the order is deterministic when two
                # flows share a timestamp rather than being left to the driver.
                .order_by(last_activity.desc(), OrchestrationNode.flow_id)
            )
        )
        .scalars()
        .all()
    )
    if len(flow_ids) > cfg.max_flows_per_pass:
        report.capped = True
        candidate_count = len(flow_ids)
        flow_ids = _select_capped_flows(
            flow_ids,
            cap=cfg.max_flows_per_pass,
            slot=_rotation_slot(),
        )
        logger.warning(
            "orchestration tracker projection: selected %d of %d flow(s) this pass; capacity is split between "
            "recent activity and a cross-tick fair rotation",
            cfg.max_flows_per_pass,
            candidate_count,
        )

    for flow_id in flow_ids:
        try:
            await _project_one(session, flow_id=flow_id, config=cfg, report=report)
        except Exception:
            # Counted and skipped. Projection is a display concern; a flow whose
            # render fails must not stop the others, and must not fail the tick's
            # durable work either.
            flow = await session.get(OrchestrationFlow, flow_id)
            org_id = flow.org_id if flow is not None else "unknown"
            logger.exception("orchestration tracker projection: failed to render flow %s", flow_id)
            report.record(org_id, "errors")

    return report


async def _project_one(
    session: AsyncSession,
    *,
    flow_id: str,
    config: TrackerProjectionConfig,
    report: TrackerProjectionReport,
) -> None:
    """Render one flow's region and queue it, or record why it was refused."""
    from .dispatch_pass import resolve_installation_id

    flow = await session.get(OrchestrationFlow, flow_id)
    if flow is None:  # pragma: no cover - the id came from a node in this session
        return
    org_id = flow.org_id
    report.record(org_id, "flows_examined")

    nodes = list(
        (
            await session.execute(
                select(OrchestrationNode)
                .where(
                    OrchestrationNode.org_id == org_id,
                    OrchestrationNode.flow_id == flow_id,
                    OrchestrationNode.state != NodeState.SUPERSEDED.value,
                )
                .order_by(OrchestrationNode.epic_ref, OrchestrationNode.wave_ref, OrchestrationNode.node_ref)
            )
        )
        .scalars()
        .all()
    )
    if not nodes:
        report.record(org_id, "projections_refused")
        return

    issue_number = await _epic_issue_number(nodes)
    if issue_number is None:
        # No unambiguous EPIC issue to write to (AC3: a wrong target is refused).
        report.record(org_id, "projections_refused")
        return

    if not await _is_approved(session, org_id=org_id, flow_id=flow_id):
        # Review finding, PR #5337. The write target comes out of `epic_ref`, which
        # plan registration stores verbatim from the author's node address, and the
        # repository is one process-wide variable. So without this check a holder of
        # `PLAN_DRAFT` — which `admin/config.py` grants to every ordinary member,
        # including read-only ones — could register an inert draft naming *another
        # team's* EPIC and have the engine overwrite that issue's tracker region
        # under its own bot identity, with content the drafter chose. Requiring the
        # same human approval that arms dispatch means the authority to make the
        # engine write about an EPIC is the authority that decides what runs there.
        report.record(org_id, "projections_refused")
        return

    installation_id = await resolve_installation_id(session, org_id=org_id)
    if installation_id is None:
        # Fail-closed on zero and on more than one installation, by the shared rule.
        report.record(org_id, "projections_refused")
        return

    # The plan *in force*, which is `superseded_at IS NULL` — the same rule
    # `repository.get_accepted_plan` applies. Ordering by version alone would keep
    # naming a version that an amendment has already superseded, so the region would
    # attribute live progress to a plan that is no longer the authority (AC2:
    # accepted-plan changes must be handled). Version 0 means "no accepted plan
    # recorded", rendered as such rather than as version 1.
    plan = (
        await session.execute(
            select(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id == flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
            .order_by(OrchestrationAcceptedPlan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    version = plan.version if plan is not None else 0

    bindings = await active_bindings_for_flow(session, org_id=org_id, flow_id=flow_id)
    current: dict[str, object] = {}
    for node in nodes:
        found = bindings.get(node.id)
        # Match the node's current attempt, exactly as dispatch records are matched:
        # a superseded attempt's binding is not this attempt's pull request.
        if isinstance(found, OrchestrationPullRequestBinding) and found.attempt != node.attempts:
            continue
        if found is not None:
            current[node.id] = found

    observed_at = _observed_at(nodes, flow)
    # Computed once and shared by the rendered text and the queued marker. Calling
    # `_watermark` twice would let the marker embedded in the region disagree with
    # the one the staleness check compares, which is the sort of split that only
    # shows up as an update that mysteriously never lands.
    #
    # Read here, while the transition session is open, because the flush's separate
    # advisory-lock session deliberately reads no engine rows.
    watermark = await _watermark(session, org_id=org_id, flow_id=flow_id)
    region = render_region(
        flow=flow,
        nodes=nodes,
        bindings=current,
        version=version,
        watermark=watermark,
        observed_at=observed_at,
    )
    report.pending.append(
        PendingTrackerProjection(
            org_id=org_id,
            flow_id=flow_id,
            repo=config.repo,
            issue_number=issue_number,
            installation_id=installation_id,
            body_region=region,
            version=version,
            watermark=watermark,
        )
    )


def _observed_at(nodes: list[OrchestrationNode], flow: OrchestrationFlow) -> datetime:
    """The most recent engine activity time for this flow, as an aware UTC datetime.

    Every timestamp is normalised to UTC-aware before being compared, because the two
    columns involved do not reliably agree on awareness. `created_at` is set by the
    Python-side `utcnow` default and stays aware in the identity map, while
    `updated_at` uses `onupdate` — so once SQLAlchemy expires and reloads it, its
    awareness is whatever the driver returns. Postgres `TIMESTAMPTZ` returns aware;
    SQLite returns naive. Comparing the two raises `TypeError`, which surfaced as the
    whole flow's projection being counted as an error and silently skipped — the exact
    invisible staleness this story exists to remove, reintroduced on the dev path only.

    Naive values are read as UTC rather than local: every writer in this schema stores
    UTC (`utcnow`), so interpreting them as local time would shift the reported
    snapshot time by the deployment's offset.
    """
    candidates = [stamp for node in nodes for stamp in (node.updated_at, node.created_at) if stamp is not None]
    candidates.append(flow.created_at)
    aware = [stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC) for stamp in candidates if stamp is not None]
    return max(aware) if aware else datetime.now(UTC)


async def _watermark(session: AsyncSession, *, org_id: str, flow_id: str) -> int:
    """A monotonic engine counter for this flow's progress.

    The number of append-only decision rows this flow has accumulated. Compared only
    against the same flow's own previous value, so its absolute magnitude is
    meaningless — which is the point: it needs no stored column and no clock, and two
    overlapping ticks reading the same state compute the same number.

    **Monotonicity is structural, and it has to be.** This value gates every write:
    `_write_one` refuses when the snapshot already published is greater, so a
    watermark that can *decrease* while the flow really moves forward makes the
    engine permanently decline to publish real progress — and because a stale
    decline is a correct outcome that leaves the tick green, nobody is told. That is
    the invisible staleness this story exists to remove, so the counter must not be
    something a later state addition can break.

    `orchestration_decisions` gives that for free: it is append-only, enforced three
    ways in `models.py` (no update method on the repository, a `before_update` mapper
    hook, and a statement-level `do_orm_execute` hook that catches the bulk `update()`
    the mapper event cannot see), and every writer of `OrchestrationNode.state`
    appends a row in the same transaction as the transition — `tick.py`, `dispatch.py`,
    `controls.py`, `engine_commands.py`, `amend.py` and `adapters/github_comments.py`.
    A count of rows that can only be appended cannot go backwards.

    A *derived* weighting over current node states cannot have that property, and the
    original arithmetic here (attempts plus a weight per resolved node) decreased on
    three ordinary flows, each of them a real regression rather than a corner case:

    - a human rejecting a gate — `awaiting_gate` scores 2 and `rejected_at_gate`
      scores 0, and `controls.py` is explicit that a gate answer deliberately does
      **not** increment `attempts`, so nothing compensated;
    - execution failing — `running` scores 1 and `failed` scores 0;
    - an amendment superseding nodes — superseded rows are filtered out of the pass
      entirely, so their contribution vanished from the sum.

    Note the guard refuses only on a *strictly* greater published snapshot, so an
    equal watermark still writes. Non-decreasing is therefore sufficient, and no
    claim is made that every render strictly advances it: two ticks with no decision
    between them render the same number and the second is skipped as unchanged, which
    is what idempotence (AC1) wants anyway.

    Counted in SQL rather than by loading rows: a long-running flow accumulates
    decisions without bound, and the pass only ever needs the cardinality.
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(OrchestrationDecision)
                .where(
                    OrchestrationDecision.org_id == org_id,
                    OrchestrationDecision.flow_id == flow_id,
                )
            )
        ).scalar_one()
        or 0
    )


def _advisory_lock_key(repo: str, issue_number: int) -> int:
    """Stable signed-bigint key for one GitHub issue target.

    Repository names are case-insensitive at GitHub, so the normalized repository
    and issue number identify the resource being protected. BLAKE2 gives the
    PostgreSQL advisory-lock namespace a stable 64-bit key without Python's
    process-randomized ``hash()``.
    """
    target = f"{repo.lower()}#{issue_number}".encode()
    return int.from_bytes(hashlib.blake2b(target, digest_size=8).digest(), "big", signed=True)


def _factory_dialect_name(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """Return the configured dialect without opening a transaction."""
    bind = session_factory.kw.get("bind")
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    if not isinstance(name, str):
        raise RuntimeError("tracker projection lock session has no database dialect")
    return name


@asynccontextmanager
async def _projection_write_lock(
    pending: PendingTrackerProjection,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> AsyncIterator[None]:
    """Serialize one issue's complete read/compare/write sequence.

    Production uses a transaction-scoped PostgreSQL advisory lock. The transaction
    contains no engine-row work; it exists after the tick's transition commit and
    protects only this remote output operation. Closing or rolling back the session
    releases the lock even when the provider raises.

    SQLite has no cross-process advisory lock. Its process-local fallback keeps the
    local/test path deterministic while production's PostgreSQL path provides the
    cross-invocation guarantee.
    """
    target = (pending.repo.lower(), pending.issue_number)
    if session_factory is None or _factory_dialect_name(session_factory) != "postgresql":
        lock = _LOCAL_WRITE_LOCKS.setdefault(target, asyncio.Lock())
        async with lock:
            yield
        return

    async with session_factory() as lock_session:
        await lock_session.execute(select(func.pg_advisory_xact_lock(_advisory_lock_key(*target))))
        try:
            yield
        finally:
            # No data is changed in this transaction. Rollback both documents that
            # fact and releases the xact-scoped advisory lock immediately.
            await lock_session.rollback()


async def flush_tracker_projections(
    report: TrackerProjectionReport,
    *,
    client_factory: object | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Write the queued regions to GitHub. **Call after the caller's commit.**

    The post-commit half of the pass, and async because a GitHub round trip is an
    `await`. Mutates `report` in place and **never raises**: the tick's durable work
    is already committed by the time this runs, and a projection failure must not
    turn into a failed tick that retries transitions (AC3). A failure is counted,
    which forces a non-success report, and the next tick re-renders from current
    state — so a transient outage self-heals and converges rather than losing the
    update.

    Per-projection containment: one issue's failure never stops the others.
    """
    if not report.pending:
        return

    # Every caller gets a lock independently of which provider implementation it
    # supplies. Keying safety off "default provider" would let a future production
    # adapter silently fall back to process-local serialization.
    if session_factory is None:
        from src.shared.database import get_session_factory

        session_factory = get_session_factory()

    for pending in report.pending:
        try:
            await _write_one(
                pending,
                client_factory=client_factory,
                session_factory=session_factory,
                report=report,
            )
        except Exception:
            # Never re-raised. The region stays stale until the next tick, which is
            # the recoverable outcome; counting it forces non-success so a
            # permanently failing projection is visible rather than silent.
            logger.exception(
                "orchestration tracker projection: failed to update issue %s in org %s",
                pending.issue_number,
                pending.org_id,
            )
            report.record(pending.org_id, "projections_failed")


async def _write_one(
    pending: PendingTrackerProjection,
    *,
    client_factory: object | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    report: TrackerProjectionReport,
) -> None:
    """Read the issue, splice the region, write it back if that is safe and needed.

    Read-then-write rather than blind write, because preserving the user's text
    requires knowing what it currently is. The target-scoped advisory lock closes
    the read/modify/write window across tick invocations; the snapshot marker then
    tells the serialized writer whether its pending render is stale.
    """
    from .tracker_provider import GitHubTrackerProvider

    provider = client_factory if client_factory is not None else GitHubTrackerProvider()

    # The lock covers the whole remote read/compare/write sequence. A marker check
    # before this lock is not a compare-and-swap: two ticks can both read the old
    # body, the newer one can PATCH, and the older one can then overwrite it.
    async with _projection_write_lock(pending, session_factory=session_factory):
        body = await provider.read_issue_body(  # type: ignore[attr-defined]
            org_id=pending.org_id,
            installation_id=pending.installation_id,
            repo=pending.repo,
            issue_number=pending.issue_number,
        )

        published_flow = _read_flow_identity(body)
        if published_flow is not None and published_flow != pending.flow_id:
            # Snapshot counters are meaningful only inside one flow. A second
            # approved flow naming this issue is an ambiguous target, not a newer
            # or older snapshot of the same execution.
            logger.warning(
                "orchestration tracker projection: issue %s is already bound to flow %s; refusing flow %s",
                pending.issue_number,
                published_flow,
                pending.flow_id,
            )
            report.record(pending.org_id, "projections_refused")
            return

        published = read_snapshot(body)
        if published is not None and published > (pending.version, pending.watermark):
            # A newer snapshot landed before this writer acquired the lock.
            # Regressing the display to an older truth is worse than doing nothing.
            report.record(pending.org_id, "projections_stale")
            return

        spliced = splice_region(body, pending.body_region)
        if isinstance(spliced, RegionRefusal):
            # Deterministic, safe, and never a whole-issue overwrite (AC2).
            logger.info(
                "orchestration tracker projection: issue %s not updated (%s)",
                pending.issue_number,
                str(spliced),
            )
            report.record(pending.org_id, "projections_refused")
            return

        if spliced == body:
            # Nothing changed, so nothing is written. This is what makes a repeated
            # identical pass idempotent (AC1) and keeps the issue's edit history free
            # of a no-op revision on every tick.
            report.record(pending.org_id, "projections_unchanged")
            return

        await provider.write_issue_body(  # type: ignore[attr-defined]
            org_id=pending.org_id,
            installation_id=pending.installation_id,
            repo=pending.repo,
            issue_number=pending.issue_number,
            body=spliced,
        )
        report.record(pending.org_id, "projections_written")
