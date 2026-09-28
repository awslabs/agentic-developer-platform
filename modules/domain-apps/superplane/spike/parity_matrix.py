"""The parity contract for migrating the SkyPilot-to-EKS path into ADP.

Issue #5040 (U12), EPIC #4910.

This module answers one question: what has to be true before anyone may say the
migrated service behaves like the baseline? It covers the eight dimensions the
story requires — provider selection/provisioning, node registration/readiness,
batch workloads, serving workloads, status/logs, stop/cancellation, controller
lifecycle, and cost observation with verified cleanup.

The central design decision is that a passing offline run cannot produce a
parity claim. ``EvidenceKind`` separates how a check was satisfied, and
``ParityResult.live_verified`` is a computed property, not a settable field:
only ``EvidenceKind.LIVE_CAPTURE`` yields True. A fixture-driven suite can
therefore prove that the assertions are well-formed and that the adapter shape
is right, while still reporting every live criterion as unmet. That is what
stops "the harness is green" from being misread as "the migration works".

``BASELINE_UNKNOWN`` is the other half of the same idea. A dimension whose
baseline behavior was never captured cannot be compared against anything, so
claiming parity for it is meaningless regardless of how the ADP side behaves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .baseline_inventory import CHAIN_GAPS, SCENARIOS


class EvidenceKind(str, Enum):
    """How a parity assertion was satisfied."""

    # Source-derived fixtures. Proves the assertion is well-formed and the
    # adapter speaks the right protocol. Proves nothing about a real provider.
    SOURCE_FIXTURE = "source_fixture"

    # A recorded response captured from an authorized real environment, replayed
    # offline. Stronger than a fixture, still not a live run.
    CAPTURED_REPLAY = "captured_replay"

    # Executed against a real environment with recorded evidence. The only kind
    # that can support a live-parity claim.
    LIVE_CAPTURE = "live_capture"

    # Not attempted.
    NOT_RUN = "not_run"


class Dimension(str, Enum):
    """The eight required parity dimensions."""

    PROVIDER_SELECTION = "provider_selection_and_provisioning"
    NODE_REGISTRATION = "node_registration_and_readiness"
    BATCH_WORKLOAD = "batch_workload_scheduling"
    SERVING_WORKLOAD = "serving_workload"
    STATUS_AND_LOGS = "status_and_logs"
    STOP_CANCELLATION = "stop_and_cancellation"
    CONTROLLER_LIFECYCLE = "controller_lifecycle"
    COST_AND_CLEANUP = "cost_observation_and_verified_cleanup"


@dataclass(frozen=True)
class ParityCheck:
    """A single comparable assertion within a dimension."""

    check_id: str
    assertion: str
    # How the baseline side of the comparison is attested. When this is
    # EvidenceKind.NOT_RUN the baseline was never captured, so no parity claim
    # is possible for this check no matter what the ADP side does.
    baseline_evidence: EvidenceKind
    # Scenario ids in baseline_inventory that this check compares against.
    baseline_scenarios: tuple[str, ...] = ()
    # Chain gap ids that must be resolved before this check can pass live.
    blocked_by_gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.assertion.strip():
            raise ValueError(f"{self.check_id}: empty assertion")
        known = {s.scenario_id for s in SCENARIOS}
        for ref in self.baseline_scenarios:
            if ref not in known:
                raise ValueError(f"{self.check_id}: unknown scenario {ref!r}")
        known_gaps = {g.gap_id for g in CHAIN_GAPS}
        for ref in self.blocked_by_gaps:
            if ref not in known_gaps:
                raise ValueError(f"{self.check_id}: unknown gap {ref!r}")

    @property
    def baseline_unknown(self) -> bool:
        """True when the baseline side was never captured."""
        return self.baseline_evidence is EvidenceKind.NOT_RUN


@dataclass(frozen=True)
class ParityDimension:
    """One of the eight dimensions, with its checks and live gates."""

    dimension: Dimension
    intent: str
    checks: tuple[ParityCheck, ...]
    # Preconditions that must hold before a LIVE run of this dimension is
    # authorized. Non-empty for anything that spends money or touches a
    # tenant's cluster.
    live_gates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.checks:
            raise ValueError(f"{self.dimension.value}: no checks")

    @property
    def unknown_baseline_checks(self) -> tuple[ParityCheck, ...]:
        return tuple(c for c in self.checks if c.baseline_unknown)


@dataclass(frozen=True)
class ParityResult:
    """The outcome of running one check.

    ``live_verified`` is deliberately a property. If it were a field, a mock or
    a careless adapter test could set it True and manufacture a parity claim —
    exactly the failure mode the issue calls out ("mock results are reported as
    parity"). Deriving it from ``evidence`` makes that unrepresentable.
    """

    check_id: str
    passed: bool
    evidence: EvidenceKind
    detail: str = ""
    # Independent confirmation that a resource is gone, for cleanup checks.
    # A SkyPilot purge succeeding is not this.
    provider_side_absence_confirmed: bool = False

    @property
    def live_verified(self) -> bool:
        """Only a real captured run against a real environment counts."""
        return self.passed and self.evidence is EvidenceKind.LIVE_CAPTURE

    @property
    def supports_parity_claim(self) -> bool:
        """Whether this result may be cited as evidence of parity.

        Requires a live run AND a captured baseline to compare against: a live
        ADP-side run with no baseline observation is a measurement of one
        system, not a comparison of two.
        """
        if not self.live_verified:
            return False
        check = check_by_id(self.check_id)
        return not check.baseline_unknown


MATRIX: tuple[ParityDimension, ...] = (
    ParityDimension(
        dimension=Dimension.PROVIDER_SELECTION,
        intent=(
            "The migrated service picks the same provider/region option, in "
            "the same order, and falls back the same way on failure."
        ),
        checks=(
            ParityCheck(
                check_id="provider.ordering-cheapest-first",
                assertion=(
                    "Given the baseline's pricing table for a GPU type, "
                    "available options are ordered by hourly cost ascending "
                    "with near-ties broken by cloud name, and with PreferSpot "
                    "the spot price is used when lower and non-zero."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("provider-selection-cheapest-first",),
            ),
            ParityCheck(
                check_id="provider.configured-clouds-restrict-selection",
                assertion=(
                    "Selection is restricted to the clouds listed in the "
                    "NodePool's Clouds field, an empty list meaning no "
                    "restriction. Selection among statically priced options "
                    "does NOT consult /enabled_clouds, which the baseline uses "
                    "only as a fallback for GPU types absent from the static "
                    "pricing map."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("provider-selection-cheapest-first",),
            ),
            ParityCheck(
                check_id="provider.fallback-on-launch-failure",
                assertion=(
                    "When an option fails to onboard, its cluster is torn down "
                    "before the next option is tried, and exhausting all "
                    "options marks the node Failed rather than leaving it "
                    "Provisioning."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=(
                    "provider-selection-cheapest-first",
                    "skypilot-launch-and-stream",
                ),
            ),
            ParityCheck(
                check_id="provider.launch-request-shape",
                assertion=(
                    "The launch request carries resources.cloud, "
                    "resources.accelerators as '<type>:<count>', disk_size "
                    "defaulting to 256, and idle_minutes_to_autostop "
                    "defaulting to 120."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=(
                    "skypilot-launch-and-stream",
                    "autostop-and-spot-defaults",
                ),
            ),
        ),
        live_gates=(
            "Authorized access to the selected baseline environment",
            "Spend limit, deadline and named cleanup owner for any launch",
        ),
    ),
    ParityDimension(
        dimension=Dimension.NODE_REGISTRATION,
        intent=(
            "A provisioned node actually becomes a Ready Kubernetes node in "
            "the intended workspace EKS cluster, and that fact is observable."
        ),
        checks=(
            ParityCheck(
                check_id="node.join-produces-ready-node",
                assertion=(
                    "After provisioning and the join step, a Kubernetes Node "
                    "exists in the workspace cluster and reaches "
                    "NodeReady=True."
                ),
                # Never captured: the user reports it works, but no revision,
                # config or log evidence exists in the snapshot.
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("eks-join-via-onboarding-scripts",),
                blocked_by_gaps=(
                    "launch-task-has-no-join-step",
                    "k8s-node-name-never-assigned",
                ),
            ),
            ParityCheck(
                check_id="node.status-links-to-k8s-node",
                assertion=(
                    "The node record exposes the Kubernetes node name, so "
                    "health monitoring is actually reachable rather than "
                    "silently skipped."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("node-health-monitoring",),
                blocked_by_gaps=("k8s-node-name-never-assigned",),
            ),
            ParityCheck(
                check_id="node.cni-prerequisite-recorded",
                assertion=(
                    "The join path installs Cilium (VPC CNI is incompatible "
                    "for hybrid nodes) before the node is treated as Ready."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("eks-join-via-onboarding-scripts",),
            ),
            ParityCheck(
                check_id="node.activation-secret-not-leaked",
                assertion=(
                    "SSM activation id and code never appear in captured "
                    "output, logs or node status."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("eks-join-via-onboarding-scripts",),
                blocked_by_gaps=("ssm-activation-credentials-in-task-envs",),
            ),
        ),
        live_gates=(
            "Authorized access to the workspace EKS cluster",
            (
                "Confirmation that tenant GPU nodes join the workspace cluster "
                "and never the ADP management cluster"
            ),
        ),
    ),
    ParityDimension(
        dimension=Dimension.BATCH_WORKLOAD,
        intent=(
            "Batch/training workloads that exist in the baseline schedule onto "
            "the joined node and run to completion."
        ),
        checks=(
            ParityCheck(
                check_id="batch.gpu-workload-schedules",
                assertion=(
                    "A GPU pod requesting nvidia.com/gpu is scheduled onto the "
                    "joined hybrid node and reaches Succeeded."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("eks-join-via-onboarding-scripts",),
                blocked_by_gaps=("launch-task-has-no-join-step",),
            ),
            ParityCheck(
                check_id="batch.device-plugin-advertises-gpu",
                assertion=(
                    "The node advertises allocatable nvidia.com/gpu matching "
                    "the requested GPU count."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("eks-join-via-onboarding-scripts",),
            ),
        ),
        live_gates=(
            "Spend limit, deadline and cleanup owner for GPU capacity",
            "Live GPU work remains gated per the wave-1 execution inputs",
        ),
    ),
    ParityDimension(
        dimension=Dimension.SERVING_WORKLOAD,
        intent=(
            "Serving scenarios actually present in the baseline keep working, "
            "with their own reachability, authentication and lifecycle "
            "evidence. Batch success cannot stand in for any of it."
        ),
        checks=(
            ParityCheck(
                check_id="serving.endpoint-reachable",
                assertion=(
                    "The served endpoint answers an inference request on its "
                    "declared port from an authorized client."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("serving-via-sky-serve-yaml",),
                blocked_by_gaps=("no-owning-controller-for-serving",),
            ),
            ParityCheck(
                check_id="serving.unauthenticated-request-refused",
                assertion=(
                    "An unauthenticated request to the served endpoint is "
                    "refused. Reachability without this is an open endpoint, "
                    "not working serving."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("serving-via-sky-serve-yaml",),
                blocked_by_gaps=("no-owning-controller-for-serving",),
            ),
            ParityCheck(
                check_id="serving.owning-controller-identified",
                assertion=(
                    "Exactly one controller owns each serving resource and "
                    "reconciles its replicas."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("serving-via-sky-serve-yaml",),
                blocked_by_gaps=("no-owning-controller-for-serving",),
            ),
            ParityCheck(
                check_id="serving.teardown-removes-replicas",
                assertion=(
                    "Tearing down the service removes every replica, confirmed "
                    "provider-side, with no orphaned GPU capacity."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("serving-via-sky-serve-yaml",),
                blocked_by_gaps=("no-owning-controller-for-serving",),
            ),
        ),
        live_gates=(
            "Authorized baseline access AND a named owning controller",
            "Spend limit, deadline and cleanup owner for serving replicas",
        ),
    ),
    ParityDimension(
        dimension=Dimension.STATUS_AND_LOGS,
        intent=(
            "Operators can see what a provisioning run is doing, with the same "
            "fidelity as the baseline's SSE stream."
        ),
        checks=(
            ParityCheck(
                check_id="status.progress-lines-streamed",
                assertion=(
                    "Progress events are surfaced in order as they arrive, not "
                    "only after the operation finishes."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("skypilot-launch-and-stream",),
            ),
            ParityCheck(
                check_id="status.terminal-event-ends-stream",
                assertion=(
                    "A 'complete' event ends the stream successfully and an "
                    "'error' event fails the operation with its message "
                    "preserved."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("skypilot-launch-and-stream",),
            ),
            ParityCheck(
                check_id="status.cluster-status-mapped",
                assertion=(
                    "SkyPilot INIT/UP/STOPPED map to the same node phases as "
                    "the baseline, and a non-UP cluster is not reported as "
                    "provisioned."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("skypilot-launch-and-stream",),
            ),
        ),
    ),
    ParityDimension(
        dimension=Dimension.STOP_CANCELLATION,
        intent=(
            "An operator can stop an in-flight provision or a running node, "
            "and the system converges rather than leaking capacity."
        ),
        checks=(
            ParityCheck(
                check_id="cancel.in-flight-launch-stops",
                assertion=(
                    "Cancelling during streaming stops the launch and does not "
                    "leave the node stuck in Provisioning."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("skypilot-launch-and-stream",),
            ),
            ParityCheck(
                check_id="cancel.timeout-bounded",
                assertion=(
                    "Onboarding is bounded by a timeout (30 minutes in the "
                    "baseline) after which the attempt is abandoned."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("skypilot-launch-and-stream",),
            ),
            ParityCheck(
                check_id="cancel.cancelled-launch-releases-capacity",
                assertion=(
                    "A cancelled launch does not leave a running provider "
                    "instance behind, confirmed provider-side."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("teardown-via-down-then-purge",),
            ),
        ),
        live_gates=("Cleanup owner for any capacity left by a cancelled run",),
    ),
    ParityDimension(
        dimension=Dimension.CONTROLLER_LIFECYCLE,
        intent=(
            "The controller can restart, and exactly one controller owns each "
            "resource across a migration."
        ),
        checks=(
            ParityCheck(
                check_id="lifecycle.restart-resumes-not-duplicates",
                assertion=(
                    "After a controller restart, in-flight nodes are resumed "
                    "from persisted state and no duplicate cluster is launched "
                    "for the same node record."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("skypilot-launch-and-stream",),
            ),
            ParityCheck(
                check_id="lifecycle.single-owner-per-resource",
                assertion=(
                    "During cutover, exactly one controller reconciles a given "
                    "SkyPilot cluster; two controllers must not both act on it."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("teardown-via-down-then-purge",),
            ),
            ParityCheck(
                check_id="lifecycle.api-state-store-survives-redeploy",
                assertion=(
                    "Redeploying the SkyPilot API server preserves cluster "
                    "handles; fresh storage must not orphan running clusters."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("teardown-via-down-then-purge",),
            ),
        ),
        live_gates=(
            "Rehearsed rollback before any cutover",
            (
                "Explicit adopt / drain-relaunch / no-existing-state decision "
                "per resource class"
            ),
        ),
    ),
    ParityDimension(
        dimension=Dimension.COST_AND_CLEANUP,
        intent=(
            "Cost is observable and released capacity is verifiably gone — not "
            "merely forgotten by SkyPilot."
        ),
        checks=(
            ParityCheck(
                check_id="cost.hourly-and-daily-aggregation",
                assertion=(
                    "Pool hourly cost is the sum of active nodes' hourly cost "
                    "and the daily estimate is that times 24."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("cost-aggregation-per-nodepool",),
            ),
            ParityCheck(
                check_id="cost.observation-not-a-spend-control",
                assertion=(
                    "Cost figures are labelled as estimates from a static "
                    "pricing table, not billed spend, and are not presented as "
                    "a budget enforcement mechanism."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("cost-aggregation-per-nodepool",),
            ),
            ParityCheck(
                check_id="cleanup.down-then-purge-fallback",
                assertion=(
                    "Teardown issues Down without purge first and only retries "
                    "with purge on failure."
                ),
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("teardown-via-down-then-purge",),
            ),
            ParityCheck(
                check_id="cleanup.provider-side-absence-verified",
                assertion=(
                    "After teardown the provider reports no running instance. "
                    "A successful purge is NOT evidence of this, because purge "
                    "drops local state regardless of provider outcome."
                ),
                baseline_evidence=EvidenceKind.NOT_RUN,
                baseline_scenarios=("teardown-via-down-then-purge",),
            ),
        ),
        live_gates=(
            "Named cleanup owner and a deadline for every provisioned resource",
            "Provider-side verification access to confirm absence",
        ),
    ),
)


def dimension_by_name(dimension: Dimension) -> ParityDimension:
    for entry in MATRIX:
        if entry.dimension is dimension:
            return entry
    raise KeyError(dimension)


def all_checks() -> tuple[ParityCheck, ...]:
    return tuple(check for entry in MATRIX for check in entry.checks)


def check_by_id(check_id: str) -> ParityCheck:
    for check in all_checks():
        if check.check_id == check_id:
            return check
    raise KeyError(check_id)


def outstanding_live_criteria(
    results: dict[str, ParityResult],
) -> tuple[str, ...]:
    """Check ids with no live-parity evidence, given a set of results.

    Any check that was not run, was satisfied only by fixtures or replay, or has
    no captured baseline to compare against is reported here. A fully green
    offline run therefore returns every check id — which is the intended and
    load-bearing outcome, not a limitation to work around.
    """
    outstanding = []
    for check in all_checks():
        result = results.get(check.check_id)
        if result is None or not result.supports_parity_claim:
            outstanding.append(check.check_id)
    return tuple(outstanding)
