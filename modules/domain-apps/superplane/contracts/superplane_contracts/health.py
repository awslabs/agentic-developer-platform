"""Check results and severity — a probe cannot report health it did not check.

Issue #5043 (U8), EPIC #4910. R11 acceptance 4.

## The failure this prevents

Two live examples motivate every rule below, both from the pinned upstream
snapshot cited by the story:

* `eks_reachability` is served by a `NoopEKSProber` (`main.go:76`) that returns
  Healthy without contacting anything. A cluster that is unreachable reads green.
* `checkVaultSyncStatus` (`heartbeat.go:269-287`) returns `"synced"` on **every**
  branch, including the branch where listing Secrets failed. The one case where
  the answer is genuinely unknown reports the most reassuring value available.

Both are the same bug: a positive reading is representable without the evidence
that would justify it. A blank health surface is recoverable — an operator sees
nothing and goes looking. A *green* health surface for an unreachable cluster is
not: it actively stops the operator looking.

## How the shape prevents it, rather than documenting against it

`CheckResult` refuses to construct in the states that would be lies:

* a positive status or a positive detail (`synced`, `reachable`) with no
  observation time, or carrying an error, raises;
* `NOT_CHECKED` carrying an observation time or an error raises — it is the
  absence of a reading, not a reading;
* `NOT_CHECKED` without a reason raises, so "we did not check" always says why.

So a probe that wants to claim `synced` must supply a time at which it observed
that, and must not simultaneously be reporting a failure. The vault-sync bug is
not a validation failure in this model; it is unconstructible.

## Severity ordering, corrected

The snapshot's ordering (`monitors/monitor.go:27-32`) ranks `Unknown` **above**
`Unreachable`, so a cluster nobody could reach aggregates as less severe than
one whose probe was indeterminate. That is backwards: unreachable is a confirmed
loss of contact, and indeterminate is not. The ordering here is corrected, and
`test_probe_cannot_fake_health.py` asserts the corrected relation directly so a
future edit cannot quietly restore the old one.

`NOT_CHECKED` sits above `HEALTHY` and below `DEGRADED`. Above `HEALTHY` because
the entire point is that an unchecked dimension must not aggregate to healthy;
below `DEGRADED` because a confirmed problem outranks a missing reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CheckStatus(str, Enum):
    """Status of a single observed dimension of a cluster's health.

    `str`-valued so a status serializes to its wire form directly and a receiver
    written in another language compares against a stable string rather than an
    ordinal that shifts when a member is inserted.
    """

    HEALTHY = "healthy"
    NOT_CHECKED = "not_checked"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"


# Severity rank, ascending. Explicit integers rather than declaration order so
# adding a member does not silently renumber the others, and so the corrected
# UNREACHABLE > UNKNOWN relation is stated in one place a test can read.
SEVERITY_RANK: dict[CheckStatus, int] = {
    CheckStatus.HEALTHY: 0,
    CheckStatus.NOT_CHECKED: 1,
    CheckStatus.DEGRADED: 2,
    CheckStatus.UNKNOWN: 3,
    CheckStatus.UNREACHABLE: 4,
}

# Statuses that assert something is well. Only these may be paired with a
# positive detail, and only with evidence.
POSITIVE_STATUSES: frozenset[CheckStatus] = frozenset({CheckStatus.HEALTHY})

# Detail strings that themselves assert a positive outcome. `synced` is here
# because it is the exact string the upstream vault-sync check returns on its
# failure branch; `reachable` because it is the claim the no-op EKS prober makes.
# A detail in this set requires a positive status and evidence, so neither claim
# can be serialized by a check that failed or never ran.
POSITIVE_DETAILS: frozenset[str] = frozenset({"synced", "reachable", "ready", "ok"})


class ContractViolation(ValueError):
    """Raised when a payload cannot be constructed without asserting a falsehood.

    A `ValueError` subclass so a receiver's validation layer can treat it as
    ordinary input rejection, and a named type so a submitter can distinguish
    "this shape is not expressible" from an unrelated `ValueError` in its own
    serialization code.
    """


@dataclass(frozen=True)
class CheckResult:
    """One dimension of one cluster's health, as observed by one probe.

    Frozen because an observation is a historical fact: a receiver that could
    mutate a submitted result after validating it would have validated something
    other than what it stored.
    """

    name: str
    status: CheckStatus
    # When the probe actually observed this. Required for every status except
    # NOT_CHECKED, and forbidden for NOT_CHECKED — the presence or absence of
    # this field is what makes "did a probe run?" answerable from the payload.
    observed_at: datetime | None = None
    # Free-text outcome marker (e.g. "synced"). Constrained only where it makes
    # a positive claim; see POSITIVE_DETAILS.
    detail: str | None = None
    # Why the check failed, or why it was not run. Never paired with a positive
    # status.
    error: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ContractViolation("check name must be a non-empty string")
        if not isinstance(self.status, CheckStatus):
            raise ContractViolation(f"unknown check status: {self.status!r}")

        detail = (self.detail or "").strip().lower()
        claims_positive = self.status in POSITIVE_STATUSES or detail in POSITIVE_DETAILS

        if self.status is CheckStatus.NOT_CHECKED:
            # An absence of a reading, so it carries no reading's attributes.
            if self.observed_at is not None:
                raise ContractViolation(
                    "a not_checked result cannot carry an observation time"
                )
            if self.error is not None:
                raise ContractViolation("a not_checked result cannot carry an error")
            if detail:
                # Any detail at all, positive or otherwise, would be a reading.
                raise ContractViolation(
                    "a not_checked result cannot carry an outcome detail"
                )
            if not self.reason or not self.reason.strip():
                raise ContractViolation("a not_checked result must state a reason")
            return

        if self.observed_at is None:
            # This is the no-op-prober rule: a status other than NOT_CHECKED
            # asserts an observation, so it must say when it observed.
            raise ContractViolation(
                f"check {self.name!r} reports {self.status.value} without an observation time; "
                "use CheckStatus.NOT_CHECKED with a reason instead"
            )
        if self.observed_at.tzinfo is None:
            # A naive timestamp is unorderable against submissions from another
            # zone, and observation ordering is the only thing making a fleet
            # surface's "as of" meaningful.
            raise ContractViolation("observed_at must be timezone-aware")

        if claims_positive and self.error is not None:
            # This is the vault-sync rule, stated once: an error and a positive
            # claim cannot coexist in one result.
            raise ContractViolation(
                f"check {self.name!r} claims a positive outcome while reporting an error"
            )
        if detail in POSITIVE_DETAILS and self.status not in POSITIVE_STATUSES:
            raise ContractViolation(
                f"check {self.name!r} carries positive detail {detail!r} "
                f"with non-positive status {self.status.value}"
            )

    @classmethod
    def not_checked(cls, name: str, reason: str) -> CheckResult:
        """Build the honest result for a probe that did not run.

        This is the constructor a no-op or unconfigured prober is supposed to
        reach for. It exists so that "we have no reading" is as easy to express
        as a fabricated healthy one — the upstream no-op prober returned Healthy
        partly because there was nothing else to return.
        """
        return cls(name=name, status=CheckStatus.NOT_CHECKED, reason=reason)

    @classmethod
    def failed(cls, name: str, observed_at: datetime, error: str) -> CheckResult:
        """Build the result for a check that ran and could not determine an answer.

        Fixed at UNKNOWN and never at a positive status, so a caller cannot pass
        a status through and get a failure recorded as healthy. Distinct from
        `not_checked`: the probe *did* run, which is itself information.
        """
        return cls(
            name=name,
            status=CheckStatus.UNKNOWN,
            observed_at=observed_at,
            error=error,
        )


def aggregate_status(
    results: tuple[CheckResult, ...] | list[CheckResult],
) -> CheckStatus:
    """Reduce many check results to the single most severe status.

    An empty set of results is `NOT_CHECKED`, not `HEALTHY`. A submission that
    checked nothing has established nothing, and the max-of-empty default in
    almost every implementation of this function is the identity element — which
    for a "worst wins" reduction is the *healthiest* value. That default is how a
    cluster with zero probes reports green.
    """
    if not results:
        return CheckStatus.NOT_CHECKED
    return max(results, key=lambda r: SEVERITY_RANK[r.status]).status


def is_more_severe(left: CheckStatus, right: CheckStatus) -> bool:
    """True when `left` is strictly more severe than `right`."""
    return SEVERITY_RANK[left] > SEVERITY_RANK[right]
