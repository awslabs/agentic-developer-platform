"""Offline replay harness for the baseline's SkyPilot semantics.

Issue #5040 (U12), EPIC #4910.

This module re-implements, in Python and against source-derived fixtures, the
few decision rules the baseline applies: how an SSE progress stream terminates,
how cluster status maps to a node phase, how provider options are ordered, and
what teardown does. U19's adapter can be exercised against these same rules.

Why re-implement rather than call the Go code: the parity contract is about
observable behavior, and the CI lane is a Python lint-plus-unit-test job with no
Go toolchain and no network. Encoding the rules here makes them assertable now
and gives U19 an executable statement of what "same behavior" means. The risk of
a re-implementation drifting from the original is handled by citing the source
for each rule, so a reviewer can check the rule against the file it came from.

What this harness cannot do, by construction: reach a provider, reach a cluster,
or produce live evidence. Every result it returns is marked
``EvidenceKind.SOURCE_FIXTURE``, so ``ParityResult.live_verified`` is False for
all of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key

from .parity_matrix import EvidenceKind, ParityResult

# Defaults from provisioner/onboarder.go.
DEFAULT_IDLE_MINUTES_TO_AUTOSTOP = 120
DEFAULT_DISK_SIZE_GB = 256
DEFAULT_TIMEOUT_MINUTES = 30

# Terminal SSE event names from skypilot/types.go.
EVENT_COMPLETE = "complete"
EVENT_ERROR = "error"


@dataclass(frozen=True)
class StreamEvent:
    """One parsed SSE frame."""

    event_id: str
    event: str
    data: str

    @property
    def is_terminal(self) -> bool:
        return self.event in (EVENT_COMPLETE, EVENT_ERROR)


def parse_sse(raw: str) -> list[StreamEvent]:
    """Parse SkyPilot's SSE frames.

    Frames are separated by a blank line and carry ``id:``, ``event:`` and
    ``data:`` fields, matching what the upstream client's parseSSE consumes.

    Multiple ``data:`` lines in one frame are **joined with newlines**, not
    overwritten. That is upstream's behavior and it is load-bearing rather than
    cosmetic: ``streamLaunchProgress`` splits the accumulated data on ``\\n``
    and emits one ``[sky] `` line per non-empty part, so a parser that kept
    only the last data line would show an operator strictly less than the
    baseline does while still appearing to stream progress.

    Two upstream details are reproduced deliberately, because the baseline is
    what this harness records rather than what would be tidier:

    * exactly one leading space is stripped from a data payload (``data:  x``
      keeps its second space); ``id:`` and ``event:`` are fully trimmed.
    * the newline separator is added only when something has already been
      accumulated, so a leading empty data line contributes nothing while a
      trailing one leaves a trailing newline.

    Cited: ``skypilot/client.go`` parseSSE; pinned by
    ``skypilot/client_test.go::TestStreamProgress_MultilineData``.
    """
    # Go's bufio.ScanLines accepts CRLF as well as LF. Normalize only those
    # delimiters; str.splitlines() would also split valid payload characters.
    raw = raw.replace("\r\n", "\n")
    events: list[StreamEvent] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event_id = ""
        event_name = ""
        data: str | None = None
        for line in block.split("\n"):
            if line.startswith("id:"):
                event_id = line[len("id:") :].strip()
            elif line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                # removeprefix strips exactly one space, matching upstream's
                # `if data[0] == ' ' { data = data[1:] }`.
                chunk = line[len("data:") :].removeprefix(" ")
                # Mirrors: if event.Data != "" { event.Data += "\n" }
                data = chunk if not data else data + "\n" + chunk
            # Anything else (comments, padding, unknown fields) is ignored,
            # exactly as the upstream scanner ignores it.

        # Upstream dispatches a frame only when it carried data or an event
        # type: `if event.Data != "" || event.Event != ""`. A frame with just
        # an `id:` is therefore not an event at all, and must not occupy a
        # stream position (which would shift cancellation indices).
        if not data and not event_name:
            continue
        events.append(
            StreamEvent(
                event_id=event_id,
                # Upstream leaves Event as "" when the field is absent. This
                # harness reports the SSE default name instead. The difference
                # is inert for every rule here — neither "" nor "message" is
                # terminal and neither is the error type — and it is pinned by
                # test_missing_event_field_defaults_to_message.
                event=event_name or "message",
                data=data or "",
            )
        )
    return events


@dataclass
class StreamOutcome:
    """The result of consuming a progress stream."""

    lines: list[str]
    succeeded: bool
    error: str | None = None
    cancelled: bool = False


def consume_stream(raw: str, cancel_after: int | None = None) -> StreamOutcome:
    """Consume a progress stream the way the onboarder does.

    Emits one prefixed line per non-empty data line, stops at the first terminal
    event, and fails the operation on an ``error`` event while preserving its
    message.

    Because a frame's data lines are joined with newlines by :func:`parse_sse`,
    a multi-line frame produces multiple ``[sky] `` lines here, matching
    ``streamLaunchProgress``'s split-and-emit loop.

    ``cancel_after`` simulates operator cancellation *before* the Nth event
    (0-indexed) is processed: ``cancel_after=0`` cancels before any output,
    ``cancel_after=2`` yields the first two events' lines, and a value beyond
    the stream's length never triggers. Cancelling after a terminal event has
    already been consumed does not rewrite the outcome.
    """
    lines: list[str] = []
    for index, event in enumerate(parse_sse(raw)):
        if cancel_after is not None and index >= cancel_after:
            return StreamOutcome(lines=lines, succeeded=False, cancelled=True)
        for line in event.data.split("\n"):
            if line:
                lines.append(f"[sky] {line}")
        if event.event == EVENT_ERROR:
            return StreamOutcome(lines=lines, succeeded=False, error=event.data)
        if event.is_terminal:
            return StreamOutcome(lines=lines, succeeded=True)
    # Stream ended without a terminal event: the upstream client treats a closed
    # channel with no error as a non-failure.
    return StreamOutcome(lines=lines, succeeded=True)


def map_cluster_status(status: str) -> str:
    """Map a SkyPilot cluster status to a node phase.

    Only UP is provisioned. INIT and STOPPED must not be reported as ready,
    which is the rule getClusterIP enforces by refusing a non-UP cluster.
    """
    return {
        "UP": "ready",
        "INIT": "provisioning",
        "STOPPED": "stopped",
    }.get(status, "unknown")


def build_launch_task(
    cloud: str,
    gpu_type: str,
    gpu_count: int,
    disk_size_gb: int = 0,
    region: str = "",
    use_spot: bool = False,
) -> dict[str, object]:
    """Build the launch task exactly as buildTask does.

    Note what is absent: no ``setup`` and no ``run`` key. That omission is the
    baseline's actual behavior and the reason chain gap
    'launch-task-has-no-join-step' exists. This function deliberately reproduces
    it rather than silently adding the join step the baseline lacks.
    """
    resources: dict[str, object] = {
        "cloud": cloud,
        "accelerators": f"{gpu_type}:{gpu_count}",
        "disk_size": disk_size_gb or DEFAULT_DISK_SIZE_GB,
    }
    if region:
        resources["region"] = region
    if use_spot:
        resources["use_spot"] = True
    return {"resources": resources}


# Upstream's tie-break epsilon: costs within this of each other are treated as
# equal and ordered by cloud name for determinism (adapter.go SelectAllAvailable).
COST_TIE_EPSILON = 0.001


def filter_by_configured_clouds(
    pricing: tuple[dict[str, object], ...],
    configured_clouds: tuple[str, ...] = (),
) -> tuple[dict[str, object], ...]:
    """Restrict candidates to the clouds a NodePool configured.

    This is where the baseline actually excludes a cloud from selection:
    ``filterAdapters`` keeps only adapters whose name appears in
    ``pool.Spec.Clouds``, and an empty list means "no restriction".

    It is deliberately a separate function from :func:`select_options` because
    upstream keeps them separate, with different inputs and different
    contracts: this one is driven by pool configuration (a Kubernetes CRD
    field), NOT by the SkyPilot ``/enabled_clouds`` API. Conflating the two is
    the defect this function exists to prevent — see the note in
    :func:`select_options`.

    Cited: ``controllers/provisioner.go`` filterAdapters.
    """
    if not configured_clouds:
        return tuple(pricing)
    allowed = set(configured_clouds)
    return tuple(row for row in pricing if str(row["cloud"]) in allowed)


def select_options(
    pricing: tuple[dict[str, object], ...],
    prefer_spot: bool = False,
) -> list[dict[str, object]]:
    """Order available provider options cheapest-first, as the baseline does.

    Mirrors ``SelectAllAvailable``: it drops rows whose ``available`` is false,
    then sorts by effective hourly cost ascending, breaking near-ties
    (within ``COST_TIE_EPSILON``) by cloud name so the order is deterministic.
    With ``prefer_spot`` the spot price substitutes for on-demand when it is
    lower and non-zero — a zero spot cost means "unknown", not "free".

    **What is deliberately absent: any ``/enabled_clouds`` filter.** The
    baseline does not consult that endpoint when selecting among statically
    priced options. ``isCloudEnabled`` is reached from exactly two places —
    ``dynamicGPULookup`` and ``CheckAvailability`` — and both run *only* when
    the requested GPU type is missing from the adapter's static pricing map;
    upstream's own comment on ``isCloudEnabled`` says it is "used as a fallback
    when a GPU type is not in the static pricing map". ``CheckAvailability``
    additionally has no non-test caller in the pinned snapshot. So for a GPU
    type that *is* statically priced (H100, the fixture's own type), a cloud
    reported disabled by ``/enabled_clouds`` would still be selected.

    An earlier revision of this harness applied that filter to every candidate.
    It was removed because it made the harness assert behavior the baseline does
    not have, in a direction that cuts both ways: an adapter faithfully
    reproducing the baseline would have *failed* the harness, and one adding the
    filter to pass would have been certified at parity while diverging. Cloud
    exclusion in the selection path is :func:`filter_by_configured_clouds`. Any
    stricter rule ADP wants belongs in a recorded migration decision, not in a
    claim about captured baseline behavior.

    Note one upstream detail not modeled as an exception: when no option
    survives, upstream returns an error ("no available <type> GPUs across any
    cloud provider") where this returns an empty list. The provisioner's
    observable consequence — the node ends Failed rather than Provisioning — is
    covered by ``provider.fallback-on-launch-failure``.

    Cited: ``adapters/adapter.go`` SelectAllAvailable; ``adapters/aws.go``
    ListGPUPricing/dynamicGPULookup; ``adapters/helpers.go`` isCloudEnabled.
    """

    def effective_cost(row: dict[str, object]) -> float:
        hourly = float(row["hourly_cost"])
        spot = float(row.get("spot_cost") or 0.0)
        if prefer_spot and 0.0 < spot < hourly:
            return spot
        return hourly

    def compare(left: dict[str, object], right: dict[str, object]) -> int:
        left_cost, right_cost = effective_cost(left), effective_cost(right)
        if abs(left_cost - right_cost) < COST_TIE_EPSILON:
            left_cloud, right_cloud = str(left["cloud"]), str(right["cloud"])
            return (left_cloud > right_cloud) - (left_cloud < right_cloud)
        return (left_cost > right_cost) - (left_cost < right_cost)

    candidates = [row for row in pricing if row.get("available", True)]
    # Preserve upstream's pairwise comparator. Rounding into fixed buckets
    # changes the winner at bucket boundaries. The upstream epsilon relation
    # itself can be non-transitive for chains of near-ties; do not interpret
    # either runtime's sort result as a portable ordering for such inputs.
    return sorted(candidates, key=cmp_to_key(compare))


@dataclass
class TeardownOutcome:
    """What a teardown attempt did."""

    calls: list[tuple[str, bool]]
    succeeded: bool
    # True only when something independent of SkyPilot confirmed the resource is
    # gone. A purge cannot set this.
    provider_absence_confirmed: bool = False

    @property
    def used_purge(self) -> bool:
        return any(purge for _, purge in self.calls)


def teardown(
    cluster_name: str,
    first_attempt_fails: bool = False,
    purge_fails: bool = False,
) -> TeardownOutcome:
    """Tear down a cluster the way the consolidator does.

    Issues Down without purge first, and only retries with purge if that fails.
    ``provider_absence_confirmed`` stays False in every path: this harness has
    no provider access, and a purge success would not establish absence anyway.
    """
    calls: list[tuple[str, bool]] = [(cluster_name, False)]
    if not first_attempt_fails:
        return TeardownOutcome(calls=calls, succeeded=True)
    calls.append((cluster_name, True))
    return TeardownOutcome(calls=calls, succeeded=not purge_fails)


def aggregate_costs(hourly_costs: list[float]) -> tuple[float, float]:
    """Sum hourly cost and project a daily estimate, as the reconciler does.

    The ``round(..., 10)`` is this harness's own addition to keep float
    accumulation from making assertions brittle; upstream sums float64 without
    rounding. Immaterial at these magnitudes, but it is a harness artifact
    rather than captured baseline behavior.
    """
    hourly = round(sum(hourly_costs), 10)
    return hourly, round(hourly * 24, 10)


def fixture_result(check_id: str, passed: bool, detail: str = "") -> ParityResult:
    """Record a harness outcome as fixture evidence.

    This is the only way this module produces a ParityResult, which is what
    guarantees no offline run can emit a live-verified result.
    """
    return ParityResult(
        check_id=check_id,
        passed=passed,
        evidence=EvidenceKind.SOURCE_FIXTURE,
        detail=detail,
    )
