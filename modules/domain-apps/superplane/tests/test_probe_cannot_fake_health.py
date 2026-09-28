"""A probe cannot report health it did not check.

Issue #5043 (U8), EPIC #4910. R11 acceptance 4.

Three properties, each traceable to a specific upstream behaviour the story cites:

1. **"Not checked" is expressible and distinct from healthy.** Today
   `eks_reachability` is served by a `NoopEKSProber` that returns Healthy without
   contacting anything, partly because there was no honest value to return. There
   is one now, and `CheckResult.not_checked()` is it.
2. **A failed check cannot serialize as `synced` or healthy.** `checkVaultSyncStatus`
   returns `"synced"` on every branch, including the branch where listing Secrets
   failed. Under this contract that combination does not validate — it is
   unconstructible, not merely discouraged.
3. **`Unreachable` outranks `Unknown`.** The upstream severity ordering ranks
   `Unknown` *above* `Unreachable`, so a cluster nobody could reach aggregates as
   less severe than one whose probe was indeterminate. Corrected, and asserted
   directly so a future edit cannot quietly restore the old order.

The tests assert on construction failures rather than on a validator's return
value, because making the dishonest states unrepresentable is stronger than
detecting them: a probe author cannot forget to run the check that would have
caught it.
"""

from __future__ import annotations

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from conftest import OBSERVED_AT, W1
from superplane_contracts import (
    POSITIVE_DETAILS,
    SEVERITY_RANK,
    CheckResult,
    CheckStatus,
    ClusterRef,
    ContractViolation,
    Observation,
    aggregate_status,
    is_more_severe,
)


class TestNotCheckedIsExpressible:
    """The honest answer for a probe that did not run exists and is distinct."""

    def test_not_checked_is_its_own_status(self) -> None:
        assert CheckStatus.NOT_CHECKED != CheckStatus.HEALTHY
        assert CheckStatus.NOT_CHECKED.value == "not_checked"

    def test_not_checked_requires_a_reason(self) -> None:
        """ "We did not check" always says why.

        Without this, `not_checked` becomes a shrug that no operator can act on.
        """
        with pytest.raises(ContractViolation, match="must state a reason"):
            CheckResult(name="eks_reachability", status=CheckStatus.NOT_CHECKED)

    def test_the_noop_prober_case_has_an_honest_shape(self) -> None:
        """What `NoopEKSProber` should return instead of Healthy.

        The upstream no-op prober reports Healthy without contacting anything.
        This is the constructor it should reach for, and it is no harder to use.
        """
        result = CheckResult.not_checked(
            "eks_reachability", reason="no EKS prober configured"
        )
        assert result.status is CheckStatus.NOT_CHECKED
        assert result.observed_at is None
        assert result.reason == "no EKS prober configured"

    def test_not_checked_cannot_carry_an_observation_time(self) -> None:
        """An absence of a reading has no observation time.

        Allowing one would let a probe claim it looked at a moment it did not.
        """
        with pytest.raises(ContractViolation, match="cannot carry an observation time"):
            CheckResult(
                name="eks_reachability",
                status=CheckStatus.NOT_CHECKED,
                observed_at=OBSERVED_AT,
                reason="no prober",
            )

    def test_not_checked_cannot_carry_an_outcome_detail(self) -> None:
        """Any detail at all would be a reading."""
        with pytest.raises(ContractViolation, match="cannot carry an outcome detail"):
            CheckResult(
                name="vault_sync",
                status=CheckStatus.NOT_CHECKED,
                detail="synced",
                reason="not run",
            )

    def test_not_checked_cannot_carry_an_error(self) -> None:
        """A probe that errored *did* run — that is `failed`, not `not_checked`.

        Keeping them distinct preserves real information: an erroring probe tells
        an operator the path is exercised and broken, while an unrun one tells
        them it is unconfigured.
        """
        with pytest.raises(ContractViolation, match="cannot carry an error"):
            CheckResult(
                name="vault_sync",
                status=CheckStatus.NOT_CHECKED,
                error="list secrets failed",
                reason="not run",
            )

    def test_a_status_other_than_not_checked_requires_an_observation_time(self) -> None:
        """This is the no-op-prober rule, enforced.

        Claiming any real status asserts an observation, so it must say when it
        observed. A prober that contacts nothing has no time to supply.
        """
        with pytest.raises(ContractViolation, match="without an observation time"):
            CheckResult(name="eks_reachability", status=CheckStatus.HEALTHY)

    def test_naive_observation_time_is_refused(self) -> None:
        """Observation ordering is what makes a fleet surface's "as of" meaningful.

        A naive timestamp is unorderable against a submission from another zone.
        """
        with pytest.raises(ContractViolation, match="timezone-aware"):
            CheckResult(
                name="api_server",
                status=CheckStatus.HEALTHY,
                observed_at=OBSERVED_AT.replace(tzinfo=None),
            )

    def test_blank_check_name_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="non-empty"):
            CheckResult.not_checked("  ", reason="not run")

    def test_a_raw_string_status_is_refused(self) -> None:
        """Status must be a `CheckStatus`, not a lookalike string.

        `CheckStatus` is a `str` enum, so `"healthy"` compares equal to
        `CheckStatus.HEALTHY` and would pass every equality check in this
        package — while `SEVERITY_RANK[...]` would raise a `KeyError` deep inside
        aggregation for any value not spelled exactly like a member. Refusing at
        construction turns that latent crash into an immediate, attributable
        refusal, and rejects `"not-checked"`-style near-misses outright.
        """
        with pytest.raises(ContractViolation, match="unknown check status"):
            CheckResult(name="api_server", status="healthy", observed_at=OBSERVED_AT)  # type: ignore[arg-type]


class TestFailedCheckCannotClaimHealth:
    """The `checkVaultSyncStatus` bug class, made unconstructible."""

    def test_healthy_with_an_error_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="claims a positive outcome"):
            CheckResult(
                name="vault_sync",
                status=CheckStatus.HEALTHY,
                observed_at=OBSERVED_AT,
                error="list secrets failed",
            )

    def test_synced_detail_with_an_error_is_refused(self) -> None:
        """The exact upstream shape: `synced` returned on the failure branch.

        `synced` is in POSITIVE_DETAILS precisely because it is the string that
        function returns when listing Secrets failed.
        """
        with pytest.raises(ContractViolation, match="claims a positive outcome"):
            CheckResult(
                name="vault_sync",
                status=CheckStatus.HEALTHY,
                observed_at=OBSERVED_AT,
                detail="synced",
                error="list secrets failed",
            )

    def test_synced_detail_cannot_ride_on_a_non_positive_status(self) -> None:
        """A positive detail requires a positive status.

        Otherwise `status=unknown, detail=synced` would render as "synced" on any
        surface that reads the detail — which is how the reassuring string
        survives a failure.
        """
        with pytest.raises(ContractViolation, match="positive detail"):
            CheckResult(
                name="vault_sync",
                status=CheckStatus.UNKNOWN,
                observed_at=OBSERVED_AT,
                detail="synced",
            )

    def test_positive_detail_matching_is_case_insensitive(self) -> None:
        """`Synced` is the same claim as `synced`.

        Case-sensitive matching would leave the rule bypassable by capitalization.
        """
        with pytest.raises(ContractViolation, match="positive detail"):
            CheckResult(
                name="vault_sync",
                status=CheckStatus.DEGRADED,
                observed_at=OBSERVED_AT,
                detail="Synced",
            )

    def test_reachable_detail_is_also_constrained(self) -> None:
        """The no-op prober's claim gets the same treatment as the vault one."""
        assert "reachable" in POSITIVE_DETAILS
        with pytest.raises(ContractViolation, match="positive detail"):
            CheckResult(
                name="eks_reachability",
                status=CheckStatus.UNREACHABLE,
                observed_at=OBSERVED_AT,
                detail="reachable",
            )

    def test_failed_constructor_records_unknown_not_healthy(self) -> None:
        """The honest shape for a check that ran and could not determine an answer.

        Status is fixed at UNKNOWN inside the constructor, so a caller cannot
        pass a status through and have a failure recorded as healthy.
        """
        result = CheckResult.failed(
            "vault_sync", observed_at=OBSERVED_AT, error="list secrets failed"
        )
        assert result.status is CheckStatus.UNKNOWN
        assert result.error == "list secrets failed"
        assert result.observed_at == OBSERVED_AT

    def test_a_genuinely_healthy_check_is_still_easy_to_build(self) -> None:
        """The honest positive path is not made awkward by the rules above.

        Worth asserting: a contract that made truthful reporting inconvenient
        would be worked around.
        """
        result = CheckResult(
            name="api_server",
            status=CheckStatus.HEALTHY,
            observed_at=OBSERVED_AT,
            detail="ok",
        )
        assert result.status is CheckStatus.HEALTHY
        assert result.error is None


class TestSeverityOrdering:
    """The corrected ordering, asserted directly."""

    def test_unreachable_outranks_unknown(self) -> None:
        """The upstream ordering ranks these backwards; this is the correction.

        Unreachable is a confirmed loss of contact. Unknown is not. A fleet
        surface that sorted or alerted by the old ordering would put the
        confirmed problem below the indeterminate one.
        """
        assert is_more_severe(CheckStatus.UNREACHABLE, CheckStatus.UNKNOWN)
        assert (
            SEVERITY_RANK[CheckStatus.UNREACHABLE] > SEVERITY_RANK[CheckStatus.UNKNOWN]
        )

    def test_not_checked_outranks_healthy(self) -> None:
        """An unchecked dimension must not aggregate to healthy."""
        assert is_more_severe(CheckStatus.NOT_CHECKED, CheckStatus.HEALTHY)

    def test_degraded_outranks_not_checked(self) -> None:
        """A confirmed problem outranks a missing reading."""
        assert is_more_severe(CheckStatus.DEGRADED, CheckStatus.NOT_CHECKED)

    def test_full_ordering_is_ascending_and_total(self) -> None:
        """Every status has a distinct rank.

        Two statuses sharing a rank would make `max()` order-dependent, so the
        aggregate of the same checks could differ by list order.
        """
        expected = [
            CheckStatus.HEALTHY,
            CheckStatus.NOT_CHECKED,
            CheckStatus.DEGRADED,
            CheckStatus.UNKNOWN,
            CheckStatus.UNREACHABLE,
        ]
        ranks = [SEVERITY_RANK[s] for s in expected]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)

    def test_every_status_has_a_rank(self) -> None:
        """A status added without a rank would raise inside aggregation."""
        assert set(SEVERITY_RANK) == set(CheckStatus)


class TestAggregation:
    """Reducing many checks to one status never invents good news."""

    def test_worst_status_wins(self) -> None:
        results = [
            CheckResult(name="a", status=CheckStatus.HEALTHY, observed_at=OBSERVED_AT),
            CheckResult(
                name="b",
                status=CheckStatus.UNREACHABLE,
                observed_at=OBSERVED_AT,
                error="connection refused",
            ),
            CheckResult(name="c", status=CheckStatus.DEGRADED, observed_at=OBSERVED_AT),
        ]
        assert aggregate_status(results) is CheckStatus.UNREACHABLE

    def test_empty_results_are_not_checked_not_healthy(self) -> None:
        """The identity-element trap, asserted.

        For a "worst wins" reduction the natural empty-set default is the
        *healthiest* value — which is how a cluster with zero probes reports
        green. A submission that checked nothing has established nothing.
        """
        assert aggregate_status([]) is CheckStatus.NOT_CHECKED

    def test_one_unchecked_dimension_prevents_a_healthy_aggregate(self) -> None:
        """A cluster is not healthy while a dimension is unverified.

        This is the whole-observation version of the no-op prober bug: three
        real green checks plus one unrun probe must not render as green.
        """
        results = [
            CheckResult(name="a", status=CheckStatus.HEALTHY, observed_at=OBSERVED_AT),
            CheckResult(name="b", status=CheckStatus.HEALTHY, observed_at=OBSERVED_AT),
            CheckResult.not_checked("eks_reachability", reason="no prober configured"),
        ]
        assert aggregate_status(results) is CheckStatus.NOT_CHECKED

    def test_all_healthy_aggregates_to_healthy(self) -> None:
        """Honest green still reads green."""
        results = [
            CheckResult(name="a", status=CheckStatus.HEALTHY, observed_at=OBSERVED_AT),
            CheckResult(name="b", status=CheckStatus.HEALTHY, observed_at=OBSERVED_AT),
        ]
        assert aggregate_status(results) is CheckStatus.HEALTHY

    def test_aggregation_is_order_independent(self) -> None:
        """Same checks, any order, same answer."""
        healthy = CheckResult(
            name="a", status=CheckStatus.HEALTHY, observed_at=OBSERVED_AT
        )
        unreachable = CheckResult(
            name="b",
            status=CheckStatus.UNREACHABLE,
            observed_at=OBSERVED_AT,
            error="refused",
        )
        assert aggregate_status([healthy, unreachable]) is aggregate_status(
            [unreachable, healthy]
        )

    def test_observation_status_uses_the_same_reduction(self) -> None:
        """The status on the wire is the aggregate, not a submitter's opinion."""
        observation = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="c1", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(
                CheckResult(
                    name="a", status=CheckStatus.HEALTHY, observed_at=OBSERVED_AT
                ),
                CheckResult.not_checked("eks_reachability", reason="no prober"),
            ),
        )
        assert observation.status is CheckStatus.NOT_CHECKED
        assert observation.to_wire()["status"] == "not_checked"
