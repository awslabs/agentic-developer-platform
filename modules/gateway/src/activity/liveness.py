"""Three-value liveness verdict for agent runs (issue #4176).

ADP has exactly two buckets for a run today, and both are wrong at the edges: a
non-terminal status renders as "in progress" regardless of age, and a terminal
status renders as finished. The indeterminate case — the pod was evicted,
OOM-killed, or hit the 6h `activeDeadlineSeconds` before reaching its own
status-write path — has no representation, so a run that died two days ago still
displays as healthy.

This module supplies the missing vocabulary as a **derived** field:

- ``live``          — a recent positive signal exists.
- ``exited``        — a terminal status was actually observed.
- ``unverifiable``  — neither holds. We could not learn the answer.

The rule, from ORCA's `ssh-execution-boundary.md` via
`docs/research/orca-fit-assessment.md` §Q2 item 1:

    **Loss of contact is not evidence of exit.** ``exited`` requires positive
    evidence of absence from the host that owns the process; a transport or
    observation failure can only ever produce ``unverifiable``.

Collapsing ``unverifiable`` into ``exited`` is the expensive direction of the
mistake: it "orphans live work and can cold-start a duplicate over the same
worktree" — the ADP-shaped version being two agents racing on one issue, two
competing PRs, and a corrupted lineage chain. So the default for anything we do
not recognise is ``unverifiable``, never ``exited``.

The same three-value trust shape is already established in ADP at
`modules/agent-factory/webhook-ingress/lambda/common/gateway_client.py:393`
("Unknown is not untrusted"). This is that discipline applied to liveness.

Scope (deliberate): read-side only. Nothing here is persisted — no new DynamoDB
attribute, no migration, and no detector/reaper. A persisted verdict needs a
reconciler that does not exist yet; that is tracked separately as #4186.

Issue #4235 makes this module the single owner of the "is a run still active,
and is its signal still recent?" vocabulary. The dependency used to point the
other way (importing the cutoff from `stats_service`), which left two
definitions of "active" — `{"in_progress", "webhook_received"}` here versus
`{"in_progress"}` there — free to disagree about the same row. `stats_service`
now imports from this module instead: it holds the boto3 query layer, this holds
the pure predicates, so the arrow points at the side with no I/O.
"""

import re
from datetime import datetime, timedelta
from typing import Literal

LivenessVerdict = Literal["live", "unverifiable", "exited"]

# Staleness cutoff for active runs (hours). An active run whose last signal is
# older than this can no longer be claimed as `live` — the tuned value from the
# #3696 stats guard, kept as ONE constant rather than two. Two independent
# cutoffs would drift, and the dashboard's `stale_count` and this per-run verdict
# would then disagree about the same run — exactly the confusion this field
# exists to remove.
ACTIVE_STALENESS_HOURS = 24

# Statuses that constitute POSITIVE OBSERVATION of an exit. Every one of these is
# written by a component that saw the outcome it is reporting:
#   - complete / failed        -- agent-worker-image/lib/invocation_status.py
#   - rejected / rate_limited  -- webhook-ingress/lambda/github/handler.py
#   - no_op                    -- ditto (the delivery asked for no work)
#   - blocked / skipped        -- #4020: a guard stopped the spawn / the worker
#                                 deduplicated a redelivery
#   - budget_stopped           -- #4187: a per-run or per-chain spend cap ended
#                                 the run (agent-worker-image/entrypoint.py)
#
# Hoisted out of the inline literal that `ActivityService._map_item` used to
# carry, so the `completed_at` derivation and this verdict cannot drift apart.
# Membership here is the ONLY way `compute_liveness` will ever return "exited";
# `test_liveness.py` asserts that as an invariant.
OBSERVED_TERMINAL_STATUSES = frozenset(
    {
        "complete",
        "failed",
        "rejected",
        "rate_limited",
        "no_op",
        "blocked",
        "skipped",
        "budget_stopped",
    }
)

# Statuses meaning "a run is under way, and its last signal is dated by
# `last_signal_at`". `webhook_received` is included: the delivery was accepted but
# the row has not advanced, so it is subject to the same "is this signal still
# recent?" question.
#
# Canonical source of `in_progress`: agent-worker-image/lib/invocation_status.py.
#
# Issue #4235: this is the ONE definition of "active" for the whole read path.
# `stats_service` imports it for its `active_runs` / `stale_count` split, so the
# per-run badge and the operator-facing stale total cannot disagree about
# `webhook_received` (they previously did — see the module docstring). The set is
# the union of the two former definitions, which is behaviour-preserving on both
# sides: the badge keeps reading `live` for a freshly-accepted delivery, and
# `stale_count` is unaffected because `stats_service._fetch_items` already
# excludes `webhook_received` at the DynamoDB layer before aggregation runs.
ACTIVE_STATUSES = frozenset({"in_progress", "webhook_received"})

# A timestamp must LOOK like an ISO-8601 value before we compare it.
#
# The comparison below is lexicographic, which is only meaningful between two
# strings of the same shape. Without this guard, garbage sorts by its first
# character: "not-a-timestamp" > "2026-08-25T12:00:00Z" because "n" > "2", so an
# unparseable timestamp would be judged FRESH and report `live` — asserting a
# positive signal we never actually received. Anchored to the leading
# `YYYY-MM-DDTHH:MM` only; trailing precision and offset spelling vary between
# producers and do not affect the ordering.
_ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def last_signal_at(arrived_at: str | None, status_updated_at: str | None = None) -> str:
    """The timestamp of the most recent signal we have for a run.

    Issue #4235: freshness must be dated from when the run LAST DID SOMETHING,
    not from when it started. `arrived_at` is the DDB sort key — the moment the
    delivery landed — so using it made age a proxy for start time: a perfectly
    healthy run simply going for more than `ACTIVE_STALENESS_HOURS` read as
    `unverifiable`. That is precisely the multi-day workload EPIC #4210 exists to
    support, so the badge mislabelled the runs it matters most for.

    `status_updated_at` is written on every status transition by
    `agent-worker-image/lib/invocation_status.py` and seeded at row creation by
    `webhook-ingress/lambda/common/webhook_events.py`, so it is the better
    last-signal proxy available today, and it is the attribute a periodic
    heartbeat (#4186 Phase 1) would advance — at which point long-running healthy
    runs read `live` for free, with no further change here.

    Falls back to `arrived_at` when `status_updated_at` is absent or empty: rows
    written before the attribute existed must not lose their only timestamp and
    silently degrade to `unverifiable`. Returns "" when neither is usable, which
    routes an active run to `unverifiable`.

    Note this deliberately does NOT take the max of the two. A malformed or
    clock-skewed `status_updated_at` is caught by the ISO guard in
    `within_staleness_window`, and "prefer the authoritative last-transition
    attribute" is easier to reason about than "whichever string sorts higher".
    """
    return status_updated_at or arrived_at or ""


def within_staleness_window(signal_at: str, now: datetime, hours: int = ACTIVE_STALENESS_HOURS) -> bool:
    """Whether a run's last signal is recent enough to count as positive.

    Compares ISO-8601 strings lexicographically against a formatted cutoff — the
    same technique `stats_service._aggregate` used inline. Issue #4235 made that
    call site use THIS function: sharing the comparison and not just the constant
    is what makes the per-run verdict and the dashboard's `stale_count` agree by
    construction rather than by coincidence. It also does not raise on the
    malformed timestamps a `fromisoformat` parse would choke on.

    An absent or malformed `signal_at` yields False, which routes the caller to
    `unverifiable` — we cannot date the signal, so we do not claim it is fresh.
    It never routes to `exited`.
    """
    if not signal_at or not _ISO_PREFIX_RE.match(signal_at):
        return False
    cutoff = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return signal_at >= cutoff


def compute_liveness(
    status: str | None,
    arrived_at: str,
    now: datetime,
    status_updated_at: str | None = None,
) -> LivenessVerdict:
    """Derive the three-value liveness verdict for a single run.

    Args:
        status: The run's `status` attribute as read from DynamoDB. Untyped and
            possibly absent — producers may add values at any time and this
            function ships on its own cadence.
        arrived_at: The run's ISO-8601 `arrived_at` timestamp (the DDB sort key,
            i.e. when the run STARTED). Used only as the last-signal fallback.
        now: Current time, injected so the boundary is testable.
        status_updated_at: The run's ISO-8601 last-transition timestamp, when the
            row has one. Issue #4235: this is the preferred freshness input —
            dating liveness from `arrived_at` alone made a healthy multi-day run
            read `unverifiable` purely for having started a while ago. Optional
            so the pre-#4235 three-argument call shape keeps working (it then
            behaves exactly as before, dating from `arrived_at`).

    Returns:
        "exited" only when `status` is in `OBSERVED_TERMINAL_STATUSES`; "live"
        when an active run's LAST SIGNAL is inside the staleness window; and
        "unverifiable" in every other case — including unrecognised statuses,
        absent statuses, and unparseable timestamps.

    Never asserts an exit without positive evidence of one. An unrecognised
    status is indeterminate, NOT dead: a future producer adding a status this
    build has never heard of must not have its live runs reported as finished.
    """
    if status in OBSERVED_TERMINAL_STATUSES:
        return "exited"
    if status in ACTIVE_STATUSES:
        signal_at = last_signal_at(arrived_at, status_updated_at)
        return "live" if within_staleness_window(signal_at, now) else "unverifiable"
    return "unverifiable"
