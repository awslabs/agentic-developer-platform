"""Provider-truth reporting: cleanup failure is reported as failure, non-zero.

Issue #5049 (U11), EPIC #4910. R15 acceptances 2, 3 and 4.

## The standard this matches, and the two paths that do not

`infra/hybrid-node-prereqs/deprovision-gpu-node-aws.sh` gets this right, and R15
acceptance 3 names it as the standard rather than inventing one. Its Step 9
re-checks `sky status`, lists remaining hybrid nodes, looks for orphaned
transit-gateway attachments, and:

```bash
if [[ -n "${ORPHAN_ATTACHMENTS}" ]]; then
    echo "  WARNING: Orphan TGW attachments found: ${ORPHAN_ATTACHMENTS}"
    ERRORS=$((ERRORS + 1))
fi
...
exit ${ERRORS}
```

Three properties worth naming, because they are what the Go paths lack: it
**re-checks** rather than trusting its own earlier steps, it **counts** errors
rather than logging and forgetting them, and its **exit status carries the
count** so a caller that checks nothing else still sees the failure.

The two paths that do not match it:

* `consolidator.go:419-425` — a failed Node delete logs "continuing anyway" and
  the phase advances to `Terminated`.
* `provisioner.go:270-272` — the cleanup terminate is fire-and-forget: its error
  is logged and the loop proceeds to the next cloud.

Neither is silent, and that is the trap. Both log. But a logged error that does
not reach the result is invisible to every automated caller, and the status field
they leave behind reads as success.

## What `TeardownReport.exit_code` is for

It is the shell contract expressed in Python: a report with unresolved findings
produces a non-zero code, so an adapter driven from a script or a workflow step
fails the step. `failure_count` is the count, matching `ERRORS`, rather than a
boolean — the number of things still outstanding is the operator's workload, and
collapsing it to a flag loses that.

The code is capped at 125. Above that, shells reassign meaning: 126 is "found but
not executable", 127 "not found", and 128+n a fatal signal. An adapter that
reported 130 unresolved resources would exit 130 and be indistinguishable from
one killed by SIGINT — an entirely different incident.

## Acceptance 2, stated rather than implied

Acceptance 2 requires the *expected behaviour on deliberate deletion* to be
stated, "or operators will read auto-repair as a bug". `ReleaseIntent` is that
statement in the contract: a deliberate release records that owner intent is
withdrawn and pending workload demand is stopped, so the two live recreation
paths in the snapshot have nothing left to act on.

Those paths are verified present and registered: auto-repair creates a
replacement after 15 minutes unhealthy (`health_monitor.go:279-333`, registered
`main.go:136`) and the pod watcher creates capacity for any persistently
unschedulable GPU pod (`pod_watcher.go:297-331`, registered `main.go:117`). So
deleting capacity without withdrawing intent and clearing demand is not a release
— it is a delete that will be undone by a controller doing its job. Changing
those controllers is **U11b**; what A does is refuse to call the release complete
while the inputs that drive recreation are still standing.

`ReleaseIntent` is a declaration A carries and reports, not an authority A
exercises. A stops nothing itself: B's driver invokes the release path, and this
type is how the report says whether that was done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .accounting import ReleaseAssessment, ReleaseState
from .handles import HandleRecord
from .health import ContractViolation

# Ceiling on a reported exit code. 126, 127 and 128+n are reserved by POSIX
# shells for "not executable", "not found" and "killed by signal n", so a count
# that ran into them would be read as a different kind of failure entirely.
MAX_EXIT_CODE = 125


class RecreationDriver(str, Enum):
    """An input that will recreate capacity if it is still standing at release.

    Each member is a mechanism verified present and registered in the pinned
    snapshot. Enumerating them means a deliberate release states which ones it
    withdrew, rather than asserting "intent stopped" with nothing behind it.
    """

    # The owner's desired state — the CR or record saying this capacity should
    # exist. Left standing, auto-repair replaces the capacity after 15 minutes
    # unhealthy (health_monitor.go:279-333).
    OWNER_INTENT = "owner_intent"

    # Pending workload demand. Left standing, the pod watcher provisions capacity
    # for any persistently unschedulable GPU pod (pod_watcher.go:297-331).
    PENDING_WORKLOAD = "pending_workload"


@dataclass(frozen=True)
class ReleaseIntent:
    """Whether a release is deliberate, and which recreation drivers it stopped.

    A deliberate release must stop every driver in `RecreationDriver`. Requiring
    the full set rather than accepting a subset is the point: a release that
    withdrew owner intent but left a pending GPU pod queued will have its capacity
    rebuilt by the pod watcher, and an operator will read that as a bug in the
    delete rather than as a driver nobody cleared.
    """

    deliberate: bool
    stopped_drivers: frozenset[RecreationDriver] = frozenset()
    requested_by: str = ""

    def __post_init__(self) -> None:
        if self.deliberate:
            if not self.requested_by.strip():
                # A deliberate retirement is somebody's decision. Without the
                # requester there is no way to distinguish it from an accident,
                # which is the distinction acceptance 2 exists to preserve.
                raise ContractViolation(
                    "a deliberate release must record who requested it"
                )
            missing = set(RecreationDriver) - set(self.stopped_drivers)
            if missing:
                raise ContractViolation(
                    "a deliberate release must stop every recreation driver; "
                    f"still standing: {sorted(d.value for d in missing)}"
                )
        elif self.stopped_drivers:
            raise ContractViolation(
                "only a deliberate release stops recreation drivers; an "
                "accidental deletion must remain repairable"
            )

    @property
    def recreation_expected(self) -> bool:
        """True when a controller is expected to rebuild this capacity.

        The direct answer to acceptance 2. An accidental deletion returns True and
        that is correct behaviour, not a defect: auto-repair exists to restore
        capacity nobody meant to lose. A deliberate retirement returns False
        because its drivers were withdrawn first.
        """
        return not self.deliberate


@dataclass(frozen=True)
class Finding:
    """One thing a re-check found still outstanding.

    `resource` names it, so the report is actionable in the way the shell script's
    "Orphan TGW attachments found: <ids>" is and a bare count is not.
    """

    resource: str
    detail: str

    def __post_init__(self) -> None:
        if not self.resource.strip():
            raise ContractViolation("a finding must name the resource")
        if not self.detail.strip():
            raise ContractViolation("a finding must say what is outstanding")


@dataclass(frozen=True)
class TeardownReport:
    """Provider truth about a teardown, with a non-zero result when it failed.

    A report, not a ledger entry: it says what the provider showed and what is
    still outstanding. C writes the accounting record from this; A does not.
    """

    allocation_id: str
    assessment: ReleaseAssessment
    intent: ReleaseIntent
    findings: tuple[Finding, ...] = ()
    credential_failure: bool = False
    reported_at: datetime | None = None
    handles: tuple[HandleRecord, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.allocation_id.strip():
            raise ContractViolation("allocation_id must be a non-empty string")
        if self.reported_at is not None and self.reported_at.tzinfo is None:
            raise ContractViolation("reported_at must be timezone-aware")
        if self.credential_failure and self.assessment.may_mark_released:
            # Design note §8: do not claim cleanup succeeded after losing
            # credentials. The assessment should already be UNRESOLVED, since a
            # credential failure yields UNKNOWN observations — this refuses the
            # combination outright so no caller can assemble the claim by hand.
            raise ContractViolation(
                "cleanup cannot be reported as successful after a credential "
                "failure; the provider was not successfully re-checked"
            )
        if self.assessment.state is ReleaseState.RELEASED and self.findings:
            raise ContractViolation(
                "a released allocation cannot carry outstanding findings"
            )

    @property
    def failure_count(self) -> int:
        """Number of outstanding conditions, matching the shell script's `ERRORS`.

        Counts explicit findings plus the credential failure, which is its own
        error rather than a property of a resource: it is the reason the re-check
        could not be trusted at all.
        """
        return len(self.findings) + (1 if self.credential_failure else 0)

    @property
    def succeeded(self) -> bool:
        """True only when the provider re-check confirmed release with nothing left."""
        return self.failure_count == 0 and self.assessment.may_mark_released

    @property
    def exit_code(self) -> int:
        """Non-zero when teardown did not confirm, capped below shell-reserved codes.

        This is the property that makes acceptance 3 mechanical. An adapter whose
        release path returns this code cannot mark a failed teardown as success,
        because the code is derived from the findings rather than set alongside
        them.

        A minimum of 1 when unsuccessful matters for the case with zero findings
        and a non-RELEASED assessment — an assessment that established nothing
        must not exit 0 just because it also found nothing to name.
        """
        if self.succeeded:
            return 0
        return min(max(self.failure_count, 1), MAX_EXIT_CODE)
