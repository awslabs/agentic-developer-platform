"""Eligible-capacity filtering, freshness at allocation, approval and relocation.

Issue #5061 (U19), EPIC #4910. One of the three suites the story names.

Every test here asserts a property the story requires, and the assertions are written
against the *reason* rather than the mechanism where the two differ — a test that only
checked "returns empty" would pass for a filter that rejected everything.

No AWS, no provider, no network. Quotes are constructed values and `now` is a fixed
aware instant, for the reason `conftest.py` gives for `OBSERVED_AT`: these tests assert
ordering and validation branches, never "now", so a constant keeps them deterministic.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import _migration_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from migration.placement import (
    DEFAULT_QUOTE_MAX_AGE,
    ApprovalRequired,
    CapacityRequest,
    EligibilityFailure,
    PlacementDecision,
    PriceQuote,
    PricingMode,
    RelocationRecord,
    eligible_candidates,
    place,
    recheck_freshness,
    relocate,
    requires_approval,
)
from superplane_contracts import ContractViolation

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
FRESH_AT = NOW - timedelta(minutes=1)
STALE_AT = NOW - timedelta(minutes=30)


def make_request(**overrides: object) -> CapacityRequest:
    """A minimal well-formed request, with named overrides per test."""
    kwargs: dict[str, object] = {
        "workspace": "ws-w1",
        "gpu_type": "H100",
        "gpu_count": 1,
    }
    kwargs.update(overrides)
    return CapacityRequest(**kwargs)  # type: ignore[arg-type]


def make_quote(**overrides: object) -> PriceQuote:
    """A minimal well-formed quote that satisfies `make_request()` as-is."""
    kwargs: dict[str, object] = {
        "cloud": "aws",
        "region": "us-east-1",
        "instance_type": "p5.48xlarge",
        "gpu_type": "H100",
        "gpu_count": 8,
        "hourly_cost": 10.0,
        "quoted_at": FRESH_AT,
    }
    kwargs.update(overrides)
    return PriceQuote(**kwargs)  # type: ignore[arg-type]


class TestCapacityRequestValidation:
    """The request refuses input that would make a later decision meaningless."""

    @pytest.mark.parametrize("field_name", ["workspace", "gpu_type"])
    def test_blank_required_string_is_refused(self, field_name: str) -> None:
        """A blank workspace or GPU type cannot be filtered against.

        Whitespace, not just empty, because `" "` is the value a template or an env
        var supplies when it was never filled in.
        """
        with pytest.raises(ContractViolation, match=field_name):
            make_request(**{field_name: "   "})

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_gpu_count_is_refused(self, bad: int) -> None:
        """Zero GPUs is not a GPU request; it would make every quote eligible."""
        with pytest.raises(ContractViolation, match="gpu_count"):
            make_request(gpu_count=bad)

    @pytest.mark.parametrize("field_name", ["min_vcpus", "min_memory_gb"])
    def test_negative_floor_is_refused(self, field_name: str) -> None:
        with pytest.raises(ContractViolation, match=field_name):
            make_request(**{field_name: -1})

    def test_non_positive_cost_ceiling_is_refused(self) -> None:
        """A zero ceiling would reject every real quote while reading as 'no limit'."""
        with pytest.raises(ContractViolation, match="max_hourly_cost"):
            make_request(max_hourly_cost=0.0)

    def test_naive_deadline_is_refused(self) -> None:
        """A naive deadline compares wrongly against an aware clock rather than loudly.

        This is the rule every contract in `superplane_contracts` applies, and the
        deadline is the field where getting it wrong silently shifts a placement
        decision by hours.
        """
        with pytest.raises(ContractViolation, match="timezone-aware"):
            make_request(deadline=datetime(2026, 9, 17, 18, 0, 0))  # noqa: DTZ001 - deliberately naive

    def test_is_frozen(self) -> None:
        """Frozen like every contract type, so a validated request stays validated.

        Without this, a caller could relax `gpu_count` after construction and every
        eligibility answer computed from it would be about a request that no longer
        exists.
        """
        request = make_request()
        with pytest.raises(FrozenInstanceError):
            request.gpu_count = 4  # type: ignore[misc]


class TestPriceQuoteValidation:
    """A quote must be recheckable and must not encode a bad reading as a low price."""

    def test_naive_quoted_at_is_refused(self) -> None:
        """A quote whose age cannot be computed would bypass the freshness gate."""
        with pytest.raises(ContractViolation, match="timezone-aware"):
            make_quote(quoted_at=datetime(2026, 9, 17, 11, 59, 0))  # noqa: DTZ001 - deliberately naive

    def test_non_positive_hourly_cost_is_refused(self) -> None:
        """A zero on-demand price sorts first and is never a real quote."""
        with pytest.raises(ContractViolation, match="hourly_cost"):
            make_quote(hourly_cost=0.0)

    def test_negative_spot_cost_is_refused(self) -> None:
        """A negative spot price is a bad reading, not the cheapest option available."""
        with pytest.raises(ContractViolation, match="spot_cost"):
            make_quote(spot_cost=-1.0)

    @pytest.mark.parametrize("field_name", ["vcpus", "memory_gb"])
    def test_negative_capacity_reading_is_refused(self, field_name: str) -> None:
        with pytest.raises(ContractViolation, match=field_name):
            make_quote(**{field_name: -1})

    @pytest.mark.parametrize(
        "field_name", ["cloud", "region", "instance_type", "gpu_type"]
    )
    def test_blank_identity_field_is_refused(self, field_name: str) -> None:
        """Every identity field feeds the rejection key and the provenance record."""
        with pytest.raises(ContractViolation, match=field_name):
            make_quote(**{field_name: ""})

    def test_non_positive_gpu_count_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="gpu_count"):
            make_quote(gpu_count=0)

    def test_age_requires_aware_now(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            make_quote().age(datetime(2026, 9, 17, 12, 0, 0))  # noqa: DTZ001 - deliberately naive

    def test_is_fresh_refuses_non_positive_max_age(self) -> None:
        """A zero window makes every quote stale, which reads as a broken feed."""
        with pytest.raises(ContractViolation, match="max_age"):
            make_quote().is_fresh(NOW, timedelta(0))

    def test_quote_from_the_future_is_not_fresh(self) -> None:
        """Clock skew is a reason to re-quote, not a way past the freshness gate.

        If a negative age counted as fresh, skew between the pricing source and this
        process would be the easiest route to allocating against an unchecked price.
        """
        future = make_quote(quoted_at=NOW + timedelta(minutes=5))
        assert future.is_fresh(NOW) is False

    def test_hardware_provenance_records_the_machine_class(self) -> None:
        """Provenance is what lets a reader tell whether two costs are comparable."""
        quote = make_quote()
        assert quote.hardware_provenance == (
            ("cloud", "aws"),
            ("region", "us-east-1"),
            ("instance_type", "p5.48xlarge"),
            ("gpu_type", "H100"),
            ("gpu_count", "8"),
        )

    def test_as_pricing_row_uses_the_baselines_key_names(self) -> None:
        """The row shape is `spike.harness.select_options`'s, not this module's.

        Pinned because the coupling is real: `select_options` subscripts these keys
        and `filter_by_configured_clouds` subscripts `cloud`. If a rename here went
        unnoticed, ordering would silently fall back to insertion order.
        """
        row = make_quote(spot_cost=4.0, available=False).as_pricing_row()
        assert row["cloud"] == "aws"
        assert row["hourly_cost"] == 10.0
        assert row["spot_cost"] == 4.0
        assert row["available"] is False


class TestEligibilityFiltering:
    """Every axis the story names, and the two places silence is treated differently."""

    def test_a_satisfying_quote_is_eligible_with_no_rejections(self) -> None:
        eligible, rejected = eligible_candidates(make_request(), (make_quote(),), NOW)
        assert len(eligible) == 1
        assert rejected == {}

    def test_wrong_gpu_type_is_rejected(self) -> None:
        _, rejected = eligible_candidates(
            make_request(), (make_quote(gpu_type="A100"),), NOW
        )
        assert rejected["aws/us-east-1/p5.48xlarge"] == (EligibilityFailure.GPU_TYPE,)

    def test_too_few_gpus_is_rejected(self) -> None:
        _, rejected = eligible_candidates(
            make_request(gpu_count=8), (make_quote(gpu_count=4),), NOW
        )
        assert EligibilityFailure.GPU_COUNT in rejected["aws/us-east-1/p5.48xlarge"]

    def test_cpu_and_memory_below_the_floor_are_rejected(self) -> None:
        _, rejected = eligible_candidates(
            make_request(min_vcpus=64, min_memory_gb=512),
            (make_quote(vcpus=8, memory_gb=32),),
            NOW,
        )
        failures = rejected["aws/us-east-1/p5.48xlarge"]
        assert EligibilityFailure.CPU in failures
        assert EligibilityFailure.MEMORY in failures

    def test_unreported_cpu_and_memory_do_not_reject(self) -> None:
        """A zero CPU/RAM reading means 'not reported', not 'zero vCPUs'.

        This is the asymmetry the module documents. The baseline's pricing rows carry
        no CPU/RAM columns at all, so treating missing as zero would make every
        baseline row ineligible the moment a request named a CPU floor — a migration
        that silently placed nothing.
        """
        eligible, rejected = eligible_candidates(
            make_request(min_vcpus=64, min_memory_gb=512),
            (make_quote(vcpus=0, memory_gb=0),),
            NOW,
        )
        assert len(eligible) == 1
        assert rejected == {}

    def test_unreported_image_and_network_do_reject(self) -> None:
        """Silence is not evidence a requirement is met — the other half of the pair.

        "This provider did not say which images it offers" does not mean it offers the
        one the workload needs. Guessing yes produces a node that provisions, bills,
        and then cannot run the workload.
        """
        _, rejected = eligible_candidates(
            make_request(required_image="adp-gpu-node-v3", required_network="ws-vpc"),
            (make_quote(),),
            NOW,
        )
        failures = rejected["aws/us-east-1/p5.48xlarge"]
        assert EligibilityFailure.IMAGE in failures
        assert EligibilityFailure.NETWORK in failures

    def test_offered_image_and_network_satisfy_the_requirement(self) -> None:
        eligible, _ = eligible_candidates(
            make_request(required_image="adp-gpu-node-v3", required_network="ws-vpc"),
            (
                make_quote(
                    images=frozenset({"adp-gpu-node-v3"}),
                    networks=frozenset({"ws-vpc"}),
                ),
            ),
            NOW,
        )
        assert len(eligible) == 1

    def test_region_outside_the_permitted_set_is_a_locality_failure(self) -> None:
        _, rejected = eligible_candidates(
            make_request(permitted_regions=frozenset({"us-west-2"})),
            (make_quote(region="us-east-1"),),
            NOW,
        )
        assert EligibilityFailure.LOCALITY in rejected["aws/us-east-1/p5.48xlarge"]

    def test_cloud_outside_the_permitted_set_reports_as_locality(self) -> None:
        """Cloud restriction is the same question as region from the caller's side.

        Reported as LOCALITY rather than a fourth axis, so a caller handling "we
        cannot place you where you asked" handles both.
        """
        _, rejected = eligible_candidates(
            make_request(permitted_clouds=frozenset({"gcp"})),
            (make_quote(cloud="aws"),),
            NOW,
        )
        assert EligibilityFailure.LOCALITY in rejected["aws/us-east-1/p5.48xlarge"]

    def test_empty_permitted_regions_means_no_locality_restriction(self) -> None:
        """Matching `filter_by_configured_clouds`'s treatment of an empty tuple.

        Deliberate consistency with the baseline rather than a second convention.
        """
        eligible, _ = eligible_candidates(
            make_request(), (make_quote(region="ap-southeast-1"),), NOW
        )
        assert len(eligible) == 1

    def test_quota_below_the_request_is_rejected(self) -> None:
        _, rejected = eligible_candidates(
            make_request(gpu_count=8), (make_quote(quota_remaining=2),), NOW
        )
        assert EligibilityFailure.QUOTA in rejected["aws/us-east-1/p5.48xlarge"]

    def test_unreported_quota_does_not_reject(self) -> None:
        """`None` quota is 'not consulted'. Rejecting on it would place nothing at all
        against a provider that does not expose quota."""
        eligible, _ = eligible_candidates(
            make_request(), (make_quote(quota_remaining=None),), NOW
        )
        assert len(eligible) == 1

    def test_eta_past_the_deadline_is_rejected(self) -> None:
        _, rejected = eligible_candidates(
            make_request(deadline=NOW + timedelta(minutes=10)),
            (make_quote(provisioning_eta=timedelta(hours=2)),),
            NOW,
        )
        assert EligibilityFailure.DEADLINE in rejected["aws/us-east-1/p5.48xlarge"]

    def test_missing_eta_against_a_deadline_fails_safely(self) -> None:
        """No ETA cannot be shown to fit, so it does not pass.

        This is the direction that fails safely: an unbounded provisioning time
        admitted against a deadline produces a workload that misses it after paying
        for the attempt.
        """
        _, rejected = eligible_candidates(
            make_request(deadline=NOW + timedelta(hours=1)),
            (make_quote(provisioning_eta=None),),
            NOW,
        )
        assert EligibilityFailure.DEADLINE in rejected["aws/us-east-1/p5.48xlarge"]

    def test_eta_within_the_deadline_is_eligible(self) -> None:
        eligible, _ = eligible_candidates(
            make_request(deadline=NOW + timedelta(hours=2)),
            (make_quote(provisioning_eta=timedelta(minutes=15)),),
            NOW,
        )
        assert len(eligible) == 1

    def test_no_deadline_means_no_eta_requirement(self) -> None:
        eligible, _ = eligible_candidates(
            make_request(), (make_quote(provisioning_eta=None),), NOW
        )
        assert len(eligible) == 1

    def test_price_above_the_ceiling_is_rejected(self) -> None:
        _, rejected = eligible_candidates(
            make_request(max_hourly_cost=5.0), (make_quote(hourly_cost=10.0),), NOW
        )
        assert EligibilityFailure.COST in rejected["aws/us-east-1/p5.48xlarge"]

    def test_a_real_spot_price_is_what_the_ceiling_compares_against(self) -> None:
        """The ceiling checks the rate the workload would actually be billed at."""
        eligible, _ = eligible_candidates(
            make_request(max_hourly_cost=5.0, allow_spot=True),
            (make_quote(hourly_cost=10.0, spot_cost=3.0),),
            NOW,
        )
        assert len(eligible) == 1

    def test_zero_spot_price_is_unknown_and_not_free(self) -> None:
        """`select_options` treats a zero spot cost as unknown; the ceiling agrees.

        If it did not, a row with no spot reading would look like it costs nothing and
        would pass every ceiling — the cheapest possible way to blow a budget.
        """
        _, rejected = eligible_candidates(
            make_request(max_hourly_cost=5.0, allow_spot=True),
            (make_quote(hourly_cost=10.0, spot_cost=0.0),),
            NOW,
        )
        assert EligibilityFailure.COST in rejected["aws/us-east-1/p5.48xlarge"]

    def test_spot_price_is_ignored_when_spot_is_forbidden(self) -> None:
        _, rejected = eligible_candidates(
            make_request(max_hourly_cost=5.0, allow_spot=False),
            (make_quote(hourly_cost=10.0, spot_cost=3.0),),
            NOW,
        )
        assert EligibilityFailure.COST in rejected["aws/us-east-1/p5.48xlarge"]

    def test_unavailable_capacity_is_rejected(self) -> None:
        _, rejected = eligible_candidates(
            make_request(), (make_quote(available=False),), NOW
        )
        assert EligibilityFailure.UNAVAILABLE in rejected["aws/us-east-1/p5.48xlarge"]

    def test_every_applicable_failure_is_recorded_not_just_the_first(self) -> None:
        """A caller told only 'ineligible' cannot tell which remedy applies.

        An under-specified request and an exhausted region need opposite actions, so
        the decision records every reason that applies.
        """
        _, rejected = eligible_candidates(
            make_request(
                gpu_count=8,
                permitted_regions=frozenset({"us-west-2"}),
                max_hourly_cost=1.0,
            ),
            (make_quote(gpu_type="A100", gpu_count=2, available=False),),
            NOW,
        )
        failures = rejected["aws/us-east-1/p5.48xlarge"]
        assert set(failures) == {
            EligibilityFailure.GPU_TYPE,
            EligibilityFailure.GPU_COUNT,
            EligibilityFailure.LOCALITY,
            EligibilityFailure.COST,
            EligibilityFailure.UNAVAILABLE,
        }

    def test_failures_are_recorded_in_enum_declaration_order(self) -> None:
        """Stable ordering so two candidates' failure lists are comparable."""
        _, rejected = eligible_candidates(
            make_request(
                gpu_count=8,
                permitted_regions=frozenset({"us-west-2"}),
                required_network="workspace-vpc",
            ),
            (make_quote(gpu_type="A100", gpu_count=2, available=False),),
            NOW,
        )
        failures = rejected["aws/us-east-1/p5.48xlarge"]
        assert failures == (
            EligibilityFailure.GPU_TYPE,
            EligibilityFailure.GPU_COUNT,
            EligibilityFailure.LOCALITY,
            EligibilityFailure.NETWORK,
            EligibilityFailure.UNAVAILABLE,
        )

    def test_rejections_are_returned_so_an_empty_result_is_explainable(self) -> None:
        """The useful half when nothing is eligible.

        A function returning only survivors collapses "no H100 in your permitted
        regions" and "your CPU floor excluded everything" into the same empty list.
        """
        eligible, rejected = eligible_candidates(
            make_request(gpu_type="H200"),
            (make_quote(instance_type="p5.48xlarge"), make_quote(instance_type="p4d")),
            NOW,
        )
        assert eligible == ()
        assert len(rejected) == 2

    def test_requires_aware_now(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            eligible_candidates(
                make_request(),
                (make_quote(),),
                datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            )


class TestFreshnessRecheck:
    """Freshness is a property of when you looked, so it is asked at allocation."""

    def test_partitions_into_fresh_and_stale(self) -> None:
        fresh_quote = make_quote(instance_type="p5.48xlarge", quoted_at=FRESH_AT)
        stale_quote = make_quote(instance_type="p4d.24xlarge", quoted_at=STALE_AT)
        fresh, stale = recheck_freshness((fresh_quote, stale_quote), NOW)
        assert fresh == (fresh_quote,)
        assert stale == (stale_quote,)

    def test_stale_quotes_are_returned_not_dropped(self) -> None:
        """So the caller can re-quote exactly those providers.

        Dropping them would leave "no candidates" and "all prices are old"
        indistinguishable, and only one of those is fixed by waiting a second.
        """
        stale_quote = make_quote(quoted_at=STALE_AT)
        fresh, stale = recheck_freshness((stale_quote,), NOW)
        assert fresh == ()
        assert stale == (stale_quote,)

    def test_the_default_window_is_five_minutes(self) -> None:
        """A default, not a policy — every entry point takes `max_age`."""
        assert DEFAULT_QUOTE_MAX_AGE == timedelta(minutes=5)

    def test_a_quote_exactly_at_the_window_is_still_fresh(self) -> None:
        """The boundary is inclusive, so the window means what it says."""
        boundary = make_quote(quoted_at=NOW - DEFAULT_QUOTE_MAX_AGE)
        fresh, stale = recheck_freshness((boundary,), NOW)
        assert fresh == (boundary,)
        assert stale == ()

    def test_a_caller_can_demand_a_tighter_window(self) -> None:
        quote = make_quote(quoted_at=NOW - timedelta(minutes=2))
        fresh, stale = recheck_freshness((quote,), NOW, timedelta(minutes=1))
        assert fresh == ()
        assert stale == (quote,)


class TestApprovalRequirements:
    """Approval is reported, never granted — A owns no approval store."""

    def test_a_conforming_placement_needs_no_approval(self) -> None:
        assert requires_approval(make_request(), make_quote()) == ()

    def test_a_region_change_needs_location_approval(self) -> None:
        needed = requires_approval(
            make_request(permitted_regions=frozenset({"us-west-2"})),
            make_quote(region="eu-central-1"),
        )
        assert needed == (ApprovalRequired.LOCATION,)

    def test_a_cloud_change_needs_location_approval(self) -> None:
        needed = requires_approval(
            make_request(permitted_clouds=frozenset({"aws"})),
            make_quote(cloud="gcp"),
        )
        assert needed == (ApprovalRequired.LOCATION,)

    def test_an_unmet_pinned_image_needs_settings_approval(self) -> None:
        needed = requires_approval(
            make_request(required_image="adp-gpu-node-v3"), make_quote()
        )
        assert ApprovalRequired.SETTINGS in needed

    def test_an_unmet_pinned_network_needs_settings_approval(self) -> None:
        needed = requires_approval(
            make_request(required_network="ws-vpc"), make_quote()
        )
        assert ApprovalRequired.SETTINGS in needed

    def test_a_spot_quote_uses_on_demand_when_the_request_forbids_spot(self) -> None:
        """A preemptible node is a different failure model, not a cheaper price.

        Substituting it silently is the adapter deciding the workload can tolerate
        interruption, which is the caller's decision.
        """
        needed = requires_approval(
            make_request(allow_spot=False), make_quote(hourly_cost=10.0, spot_cost=3.0)
        )
        assert needed == ()

    def test_a_price_above_the_approved_rate_needs_spend_approval(self) -> None:
        needed = requires_approval(
            make_request(), make_quote(hourly_cost=12.0), approved_hourly_cost=10.0
        )
        assert needed == (ApprovalRequired.SPEND,)

    def test_a_price_below_the_approved_rate_needs_no_approval(self) -> None:
        """Only an increase needs authorizing; gating a price drop blocks for nothing."""
        needed = requires_approval(
            make_request(), make_quote(hourly_cost=8.0), approved_hourly_cost=10.0
        )
        assert needed == ()

    def test_a_price_equal_to_the_approved_rate_needs_no_approval(self) -> None:
        needed = requires_approval(
            make_request(), make_quote(hourly_cost=10.0), approved_hourly_cost=10.0
        )
        assert needed == ()

    def test_no_approved_rate_means_no_spend_gate(self) -> None:
        needed = requires_approval(make_request(), make_quote(hourly_cost=999.0))
        assert needed == ()

    def test_all_three_classes_can_be_required_at_once(self) -> None:
        needed = requires_approval(
            make_request(
                permitted_regions=frozenset({"us-west-2"}),
                required_image="adp-gpu-node-v3",
            ),
            make_quote(region="eu-central-1", hourly_cost=20.0),
            approved_hourly_cost=10.0,
        )
        assert set(needed) == {
            ApprovalRequired.LOCATION,
            ApprovalRequired.SETTINGS,
            ApprovalRequired.SPEND,
        }


class TestPlace:
    """Eligibility, then freshness, then the baseline's cost ordering — in that order."""

    def test_selects_the_cheapest_eligible_fresh_quote(self) -> None:
        cheap = make_quote(instance_type="cheap", hourly_cost=5.0)
        dear = make_quote(instance_type="dear", hourly_cost=50.0)
        decision = place(make_request(), (dear, cheap), NOW)
        assert decision.selected is cheap
        assert decision.allocatable is True

    def test_ordering_is_delegated_to_the_baseline_harness(self) -> None:
        """Ranking order is `spike.harness.select_options`'s, not a second sort.

        Asserted on a full ordering rather than just the winner, because a
        reimplementation that happened to agree on the minimum would still drift on
        the tail — and the tail is what a caller falls back to.
        """
        a = make_quote(instance_type="a", hourly_cost=30.0)
        b = make_quote(instance_type="b", hourly_cost=10.0)
        c = make_quote(instance_type="c", hourly_cost=20.0)
        decision = place(make_request(), (a, b, c), NOW)
        assert [quote.instance_type for quote in decision.ranked] == ["b", "c", "a"]

    def test_spot_substitution_follows_the_baseline_when_spot_is_allowed(self) -> None:
        """`prefer_spot` is passed through, so spot pricing reorders as upstream does."""
        on_demand = make_quote(instance_type="ondemand", hourly_cost=8.0)
        spot = make_quote(instance_type="spot", hourly_cost=20.0, spot_cost=2.0)
        decision = place(make_request(allow_spot=True), (on_demand, spot), NOW)
        assert decision.selected is spot
        assert decision.pricing_mode is PricingMode.SPOT
        assert decision.effective_hourly_cost == 2.0

    def test_spot_is_not_substituted_when_the_request_forbids_it(self) -> None:
        on_demand = make_quote(instance_type="ondemand", hourly_cost=8.0)
        spot = make_quote(instance_type="spot", hourly_cost=20.0, spot_cost=2.0)
        decision = place(make_request(allow_spot=False), (on_demand, spot), NOW)
        assert decision.selected is on_demand

    def test_distinct_quotes_with_the_same_four_fields_do_not_collapse(self) -> None:
        cheap = make_quote(spot_cost=1.0, provisioning_eta=timedelta(minutes=5))
        dear = make_quote(spot_cost=5.0, provisioning_eta=timedelta(hours=10))
        decision = place(make_request(allow_spot=True), (cheap, dear), NOW)
        assert decision.selected is cheap
        assert decision.ranked == (cheap, dear)
        assert decision.ranked[0] is not decision.ranked[1]

    def test_spend_approval_uses_the_selected_spot_rate(self) -> None:
        decision = place(
            make_request(allow_spot=True),
            (make_quote(hourly_cost=20.0, spot_cost=2.0),),
            NOW,
            approved_hourly_cost=10.0,
        )
        assert decision.approvals_required == ()
        assert decision.effective_hourly_cost == 2.0

    def test_float_noise_does_not_require_spend_approval(self) -> None:
        decision = place(
            make_request(),
            (make_quote(hourly_cost=0.1 + 0.2),),
            NOW,
            approved_hourly_cost=0.3,
        )
        assert decision.approvals_required == ()

    def test_ineligible_quotes_never_reach_the_ranking(self) -> None:
        """Ranking ineligible candidates can surface one as the winner."""
        wrong = make_quote(instance_type="wrong", gpu_type="A100", hourly_cost=1.0)
        right = make_quote(instance_type="right", hourly_cost=9.0)
        decision = place(make_request(), (wrong, right), NOW)
        assert decision.ranked == (right,)
        assert decision.selected is right

    def test_a_stale_quote_is_removed_before_ranking(self) -> None:
        """So a placement's cost does not depend on which quotes happened to be old.

        The cheapest quote here is stale. It is reported for re-quoting and the winner
        is the cheapest *fresh* quote — not the stale one with a warning attached.
        """
        stale_cheap = make_quote(
            instance_type="stale", hourly_cost=1.0, quoted_at=STALE_AT
        )
        fresh_dear = make_quote(instance_type="fresh", hourly_cost=9.0)
        decision = place(make_request(), (stale_cheap, fresh_dear), NOW)
        assert decision.selected is fresh_dear
        assert decision.stale == (stale_cheap,)

    def test_all_quotes_stale_is_not_allocatable_and_says_to_re_quote(self) -> None:
        """A stale price feeds the relocation record and C's reservation.

        Allocating against it would make both inherit a number no observation
        supports, so this refuses rather than warns.
        """
        decision = place(make_request(), (make_quote(quoted_at=STALE_AT),), NOW)
        assert decision.selected is None
        assert decision.allocatable is False
        assert "re-quote" in decision.refusal_reason

    def test_no_eligible_capacity_is_not_allocatable(self) -> None:
        decision = place(make_request(gpu_type="H200"), (make_quote(),), NOW)
        assert decision.allocatable is False
        assert decision.refusal_reason == "no eligible capacity for this request"

    def test_no_quotes_at_all_is_not_allocatable(self) -> None:
        decision = place(make_request(), (), NOW)
        assert decision.allocatable is False
        assert decision.selected is None

    def test_an_outstanding_approval_blocks_allocation(self) -> None:
        """The gate, not a comment.

        A decision that reported the approval while staying allocatable would let a
        spend increase through with a log line, which is what 'approval for forbidden
        spend changes' exists to prevent.
        """
        decision = place(
            make_request(),
            (make_quote(hourly_cost=50.0),),
            NOW,
            approved_hourly_cost=10.0,
        )
        assert decision.selected is not None
        assert decision.allocatable is False
        assert decision.approvals_required == (ApprovalRequired.SPEND,)
        assert "approval required" in decision.refusal_reason

    def test_changed_pinned_settings_block_the_operational_entry_point(self) -> None:
        """A supported replacement setting is eligible, but not self-authorizing."""
        incumbent_request = make_request(
            required_image="adp-gpu-node-v2", required_network="ws-vpc-v1"
        )
        request = make_request(
            required_image="adp-gpu-node-v3", required_network="ws-vpc-v2"
        )
        replacement = make_quote(
            images=frozenset({"adp-gpu-node-v3"}),
            networks=frozenset({"ws-vpc-v2"}),
        )

        decision = place(
            request,
            (replacement,),
            NOW,
            incumbent=make_quote(),
            incumbent_request=incumbent_request,
            incumbent_pricing_mode=PricingMode.ON_DEMAND,
        )

        assert decision.selected is replacement
        assert decision.approvals_required == (ApprovalRequired.SETTINGS,)
        assert decision.allocatable is False

    def test_rejections_are_carried_on_the_decision(self) -> None:
        decision = place(
            make_request(),
            (make_quote(instance_type="nope", gpu_type="A100"), make_quote()),
            NOW,
        )
        assert decision.rejected["aws/us-east-1/nope"] == (EligibilityFailure.GPU_TYPE,)

    def test_decided_at_records_the_instant_the_decision_was_made(self) -> None:
        decision = place(make_request(), (make_quote(),), NOW)
        assert decision.decided_at == NOW

    def test_a_tighter_max_age_is_honoured(self) -> None:
        quote = make_quote(quoted_at=NOW - timedelta(minutes=3))
        decision = place(make_request(), (quote,), NOW, max_age=timedelta(minutes=1))
        assert decision.selected is None
        assert decision.stale == (quote,)

    def test_an_allocatable_decision_has_no_refusal_reason(self) -> None:
        decision = place(make_request(), (make_quote(),), NOW)
        assert decision.refusal_reason == ""


class TestPlacementDecisionInvariants:
    """`allocatable` is derived, so no caller can assert its way past the gate."""

    def test_allocatable_is_not_a_settable_field(self) -> None:
        """The same rule as `ParityResult.live_verified`.

        A field can be set True by a caller who wants the answer to be True, and this
        one gates a provider allocation.
        """
        decision = place(make_request(), (make_quote(),), NOW)
        with pytest.raises(AttributeError):
            decision.allocatable = False  # type: ignore[misc]

    def test_a_selection_must_be_one_of_the_ranked_candidates(self) -> None:
        """Otherwise a decision could name a quote that passed neither gate."""
        unranked = make_quote(instance_type="never-ranked")
        with pytest.raises(ContractViolation, match="ranked"):
            PlacementDecision(
                request=make_request(), selected=unranked, ranked=(make_quote(),)
            )

    def test_naive_decided_at_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            PlacementDecision(
                request=make_request(),
                selected=None,
                decided_at=datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            )

    def test_no_selection_with_ranked_candidates_reports_generically(self) -> None:
        """The last-resort branch: candidates exist but none was chosen."""
        decision = PlacementDecision(
            request=make_request(), selected=None, ranked=(make_quote(),)
        )
        assert decision.refusal_reason == "no placement selected"

    def test_stale_alongside_ranked_candidates_is_not_the_staleness_refusal(
        self,
    ) -> None:
        """Some stale quotes plus a usable winner is an allocatable decision."""
        decision = place(
            make_request(),
            (make_quote(instance_type="ok"), make_quote(quoted_at=STALE_AT)),
            NOW,
        )
        assert decision.stale
        assert decision.allocatable is True


class TestRelocation:
    """Old and new attempts, both costs, both provenances — and not 'live migration'."""

    def test_records_both_attempts_and_is_never_called_live_migration(self) -> None:
        """The story: relocation "is not silently called live migration".

        Asked of the record directly rather than inferred from an absent field, so a
        later change that started claiming a live migration fails here.
        """
        interrupted = make_quote(instance_type="spot-preempted", hourly_cost=4.0)
        replacement = make_quote(instance_type="on-demand", hourly_cost=10.0)
        record = RelocationRecord(
            workload_id="job-1",
            workspace="ws-w1",
            interrupted=interrupted,
            replacement=replacement,
            interrupted_at=NOW - timedelta(minutes=40),
            relocated_at=NOW,
            interrupted_runtime=timedelta(minutes=40),
            reason="spot preemption",
        )
        assert record.is_live_migration is False
        assert record.interrupted is interrupted
        assert record.replacement is replacement

    def test_the_interrupted_attempts_cost_is_retained(self) -> None:
        """A spot node billed for forty minutes was really billed for forty minutes.

        Dropping that cost is the 'declare zero' failure `accounting.py` refuses:
        zero is the expensive claim to get wrong, so zero is the one needing evidence.
        """
        record = RelocationRecord(
            workload_id="job-1",
            workspace="ws-w1",
            interrupted=make_quote(hourly_cost=6.0),
            replacement=make_quote(hourly_cost=10.0),
            interrupted_at=NOW - timedelta(minutes=30),
            relocated_at=NOW,
            interrupted_runtime=timedelta(minutes=30),
            reason="spot preemption",
        )
        assert record.interrupted_cost == pytest.approx(3.0)

    def test_the_interrupted_cost_uses_its_own_rate_not_the_replacements(self) -> None:
        """The interruption happened on that hardware at that price.

        The two attempts may not be comparable machines at all, which is exactly what
        the retained provenance is for.
        """
        record = RelocationRecord(
            workload_id="job-1",
            workspace="ws-w1",
            interrupted=make_quote(hourly_cost=2.0),
            replacement=make_quote(hourly_cost=100.0),
            interrupted_at=NOW - timedelta(hours=1),
            relocated_at=NOW,
            interrupted_runtime=timedelta(hours=1),
            reason="spot preemption",
        )
        assert record.interrupted_cost == pytest.approx(2.0)

    def test_hardware_change_is_visible_in_the_record(self) -> None:
        record = RelocationRecord(
            workload_id="job-1",
            workspace="ws-w1",
            interrupted=make_quote(cloud="aws", instance_type="p5.48xlarge"),
            replacement=make_quote(cloud="gcp", instance_type="a3-highgpu-8g"),
            interrupted_at=NOW - timedelta(minutes=5),
            relocated_at=NOW,
            interrupted_runtime=timedelta(minutes=5),
            reason="capacity",
        )
        assert record.hardware_changed is True

    def test_identical_hardware_is_recorded_as_unchanged(self) -> None:
        same = make_quote()
        record = RelocationRecord(
            workload_id="job-1",
            workspace="ws-w1",
            interrupted=same,
            replacement=same,
            interrupted_at=NOW - timedelta(minutes=5),
            relocated_at=NOW,
            interrupted_runtime=timedelta(minutes=5),
            reason="node failure",
        )
        assert record.hardware_changed is False

    @pytest.mark.parametrize("field_name", ["workload_id", "workspace", "reason"])
    def test_blank_required_field_is_refused(self, field_name: str) -> None:
        """A relocation with no recorded reason is an unexplained cost."""
        kwargs: dict[str, object] = {
            "workload_id": "job-1",
            "workspace": "ws-w1",
            "interrupted": make_quote(),
            "replacement": make_quote(),
            "interrupted_at": NOW - timedelta(minutes=5),
            "relocated_at": NOW,
            "interrupted_runtime": timedelta(minutes=5),
            "reason": "spot preemption",
        }
        kwargs[field_name] = "  "
        with pytest.raises(ContractViolation, match=field_name):
            RelocationRecord(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize("field_name", ["interrupted_at", "relocated_at"])
    def test_naive_timestamps_are_refused(self, field_name: str) -> None:
        kwargs: dict[str, object] = {
            "workload_id": "job-1",
            "workspace": "ws-w1",
            "interrupted": make_quote(),
            "replacement": make_quote(),
            "interrupted_at": NOW - timedelta(minutes=5),
            "relocated_at": NOW,
            "interrupted_runtime": timedelta(minutes=5),
            "reason": "spot preemption",
        }
        kwargs[field_name] = datetime(2026, 9, 17, 12, 0, 0)  # noqa: DTZ001 - deliberately naive
        with pytest.raises(ContractViolation, match="timezone-aware"):
            RelocationRecord(**kwargs)  # type: ignore[arg-type]

    def test_relocation_cannot_precede_the_interruption(self) -> None:
        """An out-of-order pair means the timestamps came from different clocks."""
        with pytest.raises(ContractViolation, match="cannot precede"):
            RelocationRecord(
                workload_id="job-1",
                workspace="ws-w1",
                interrupted=make_quote(),
                replacement=make_quote(),
                interrupted_at=NOW,
                relocated_at=NOW - timedelta(minutes=5),
                interrupted_runtime=timedelta(minutes=5),
                reason="spot preemption",
            )

    def test_negative_runtime_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="runtime"):
            RelocationRecord(
                workload_id="job-1",
                workspace="ws-w1",
                interrupted=make_quote(),
                replacement=make_quote(),
                interrupted_at=NOW - timedelta(minutes=5),
                relocated_at=NOW,
                interrupted_runtime=timedelta(seconds=-1),
                reason="spot preemption",
            )


class TestRelocate:
    """The entry point returns a decision always, and a record only when real."""

    def test_an_allocatable_replacement_produces_a_record(self) -> None:
        decision, record = relocate(
            "job-1",
            make_request(),
            make_quote(instance_type="preempted", hourly_cost=4.0),
            NOW - timedelta(minutes=20),
            timedelta(minutes=20),
            (make_quote(instance_type="replacement", hourly_cost=9.0),),
            NOW,
            "spot preemption",
            interrupted_pricing_mode=PricingMode.ON_DEMAND,
        )
        assert decision.allocatable is True
        assert record is not None
        assert record.replacement.instance_type == "replacement"
        assert record.workspace == "ws-w1"
        assert record.is_live_migration is False

    def test_no_allocatable_replacement_produces_no_record(self) -> None:
        """A record naming an unallocated replacement would put a fictional attempt
        into the accounting — and the record *is* the cost evidence."""
        decision, record = relocate(
            "job-1",
            make_request(gpu_type="H200"),
            make_quote(),
            NOW - timedelta(minutes=20),
            timedelta(minutes=20),
            (make_quote(),),
            NOW,
            "spot preemption",
            interrupted_pricing_mode=PricingMode.ON_DEMAND,
        )
        assert record is None
        assert decision.allocatable is False

    def test_the_decision_is_returned_even_when_no_record_is(self) -> None:
        """So the caller can tell re-quote from seek-approval from change-the-request."""
        decision, record = relocate(
            "job-1",
            make_request(),
            make_quote(),
            NOW - timedelta(minutes=20),
            timedelta(minutes=20),
            (make_quote(quoted_at=STALE_AT),),
            NOW,
            "spot preemption",
            interrupted_pricing_mode=PricingMode.ON_DEMAND,
        )
        assert record is None
        assert "re-quote" in decision.refusal_reason

    def test_an_approval_requirement_blocks_the_record(self) -> None:
        """Relocating into a spend increase without approval is the case this covers."""
        decision, record = relocate(
            "job-1",
            make_request(),
            make_quote(hourly_cost=4.0),
            NOW - timedelta(minutes=20),
            timedelta(minutes=20),
            (make_quote(hourly_cost=40.0),),
            NOW,
            "spot preemption",
            interrupted_pricing_mode=PricingMode.ON_DEMAND,
            approved_hourly_cost=10.0,
        )
        assert record is None
        assert decision.approvals_required == (ApprovalRequired.SPEND,)

    def test_cross_cloud_relocation_requires_location_approval(self) -> None:
        decision, record = relocate(
            "job-1",
            make_request(),
            make_quote(cloud="aws", region="us-east-1"),
            NOW - timedelta(minutes=20),
            timedelta(minutes=20),
            (make_quote(cloud="gcp", region="eu-central-1"),),
            NOW,
            "capacity",
            interrupted_pricing_mode=PricingMode.ON_DEMAND,
        )
        assert decision.approvals_required == (ApprovalRequired.LOCATION,)
        assert record is None

    def test_interrupted_spot_cost_uses_the_retained_mode(self) -> None:
        decision, record = relocate(
            "job-1",
            make_request(allow_spot=True),
            make_quote(hourly_cost=20.0, spot_cost=2.0),
            NOW - timedelta(hours=1),
            timedelta(hours=1),
            (make_quote(instance_type="replacement", hourly_cost=9.0, spot_cost=1.0),),
            NOW,
            "spot preemption",
            interrupted_pricing_mode=PricingMode.SPOT,
        )
        assert decision.allocatable is True
        assert record is not None
        assert record.interrupted_pricing_mode is PricingMode.SPOT
        assert record.interrupted_cost == pytest.approx(2.0)

    def test_pricing_mode_change_requires_settings_approval(self) -> None:
        decision, record = relocate(
            "job-1",
            make_request(allow_spot=True),
            make_quote(hourly_cost=20.0, spot_cost=2.0),
            NOW - timedelta(hours=1),
            timedelta(hours=1),
            (make_quote(instance_type="replacement", hourly_cost=9.0),),
            NOW,
            "spot capacity exhausted",
            interrupted_pricing_mode=PricingMode.SPOT,
        )

        assert decision.approvals_required == (ApprovalRequired.SETTINGS,)
        assert decision.allocatable is False
        assert record is None

    def test_pinned_image_change_requires_settings_approval(self) -> None:
        incumbent_request = make_request(required_image="adp-gpu-node-v2")
        request = make_request(required_image="adp-gpu-node-v3")
        decision, record = relocate(
            "job-1",
            request,
            make_quote(images=frozenset({"adp-gpu-node-v2"})),
            NOW - timedelta(hours=1),
            timedelta(hours=1),
            (
                make_quote(
                    instance_type="replacement",
                    images=frozenset({"adp-gpu-node-v3"}),
                ),
            ),
            NOW,
            "image update",
            interrupted_pricing_mode=PricingMode.ON_DEMAND,
            incumbent_request=incumbent_request,
        )

        assert decision.approvals_required == (ApprovalRequired.SETTINGS,)
        assert decision.allocatable is False
        assert record is None
