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
"""

import re
from datetime import datetime, timedelta
from typing import Literal

# Reuse the already-tuned staleness cutoff from the #3696 stats guard rather than
# inventing a second threshold. Two independent cutoffs would drift, and the
# dashboard's `stale_count` and this per-run verdict would then disagree about
# the same run — which is exactly the confusion this field exists to remove.
from src.activity.stats_service import _ACTIVE_STALENESS_HOURS

LivenessVerdict = Literal["live", "unverifiable", "exited"]

# Statuses that constitute POSITIVE OBSERVATION of an exit. Every one of these is
# written by a component that saw the outcome it is reporting:
#   - complete / failed        -- agent-worker-image/lib/invocation_status.py
#   - rejected / rate_limited  -- webhook-ingress/lambda/github/handler.py
#   - no_op                    -- ditto (the delivery asked for no work)
#   - blocked / skipped        -- #4020: a guard stopped the spawn / the worker
#                                 deduplicated a redelivery
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
    }
)

# Statuses meaning "a run is under way and last said so at `arrived_at`".
# `webhook_received` is included: the delivery was accepted but the row has not
# advanced, so it is subject to the same "is this signal still recent?" question.
# Canonical source of `in_progress`: agent-worker-image/lib/invocation_status.py.
ACTIVE_STATUSES = frozenset({"in_progress", "webhook_received"})

# `arrived_at` must LOOK like an ISO-8601 timestamp before we compare it.
#
# The comparison below is lexicographic, which is only meaningful between two
# strings of the same shape. Without this guard, garbage sorts by its first
# character: "not-a-timestamp" > "2026-08-25T12:00:00Z" because "n" > "2", so an
# unparseable timestamp would be judged FRESH and report `live` — asserting a
# positive signal we never actually received. Anchored to the leading
# `YYYY-MM-DDTHH:MM` only; trailing precision and offset spelling vary between
# producers and do not affect the ordering.
_ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _within_staleness_window(arrived_at: str, now: datetime, hours: int) -> bool:
    """Whether `arrived_at` is recent enough to count as a positive signal.

    Compares ISO-8601 strings lexicographically against a formatted cutoff —
    the same technique as `stats_service._aggregate` (`:270`, `:293`). Reusing
    the comparison and not just the constant is deliberate: it makes this verdict
    and the dashboard's `stale_count` agree by construction rather than by
    coincidence, and it does not raise on the malformed timestamps a
    `fromisoformat` parse would choke on.

    An absent or malformed `arrived_at` yields False, which routes the caller to
    `unverifiable` — we cannot date the signal, so we do not claim it is fresh.
    It never routes to `exited`.
    """
    if not arrived_at or not _ISO_PREFIX_RE.match(arrived_at):
        return False
    cutoff = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return arrived_at >= cutoff


def compute_liveness(status: str | None, arrived_at: str, now: datetime) -> LivenessVerdict:
    """Derive the three-value liveness verdict for a single run.

    Args:
        status: The run's `status` attribute as read from DynamoDB. Untyped and
            possibly absent — producers may add values at any time and this
            function ships on its own cadence.
        arrived_at: The run's ISO-8601 `arrived_at` timestamp (the DDB sort key).
        now: Current time, injected so the boundary is testable.

    Returns:
        "exited" only when `status` is in `OBSERVED_TERMINAL_STATUSES`; "live"
        when an active run's signal is inside the staleness window; and
        "unverifiable" in every other case — including unrecognised statuses,
        absent statuses, and unparseable timestamps.

    Never asserts an exit without positive evidence of one. An unrecognised
    status is indeterminate, NOT dead: a future producer adding a status this
    build has never heard of must not have its live runs reported as finished.
    """
    if status in OBSERVED_TERMINAL_STATUSES:
        return "exited"
    if status in ACTIVE_STATUSES:
        return "live" if _within_staleness_window(arrived_at, now, _ACTIVE_STALENESS_HOURS) else "unverifiable"
    return "unverifiable"
