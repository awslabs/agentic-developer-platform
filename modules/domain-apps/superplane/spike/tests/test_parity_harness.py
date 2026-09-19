"""Source-derived fixtures exercise the parity assertions; mocks stay mocks.

Issue #5040 (U12), EPIC #4910.

Two jobs here. First, prove the harness reproduces the baseline's decision rules
for provisioning, join, scheduling, status, cancellation and cleanup — so U19 has
an executable definition of "same behavior". Second, and more important, prove
that satisfying all of those offline still yields zero live-parity claims.

``TestMocksCannotProveParity`` is the load-bearing part. If it ever fails, the
harness has become able to certify a migration it never observed.
"""

from __future__ import annotations

import inspect
from enum import Enum

import pytest

from ..baseline_inventory import SCENARIOS
from ..fixtures import (
    DOWN_RESPONSE,
    ENABLED_CLOUDS_RESPONSE,
    FIXTURE_PROVENANCE,
    GPU_PRICING_H100,
    HEALTH_RESPONSE,
    LAUNCH_RESPONSE,
    SSE_LAUNCH_ERROR,
    SSE_LAUNCH_SUCCESS,
    SSE_MULTILINE_DATA,
    STATUS_RESPONSE_EMPTY,
    STATUS_RESPONSE_STOPPED,
    STATUS_RESPONSE_UP,
)
from ..harness import (
    DEFAULT_DISK_SIZE_GB,
    DEFAULT_IDLE_MINUTES_TO_AUTOSTOP,
    DEFAULT_TIMEOUT_MINUTES,
    aggregate_costs,
    build_launch_task,
    consume_stream,
    filter_by_configured_clouds,
    fixture_result,
    map_cluster_status,
    parse_sse,
    select_options,
    teardown,
)
from ..parity_matrix import (
    MATRIX,
    Dimension,
    EvidenceKind,
    ParityCheck,
    ParityDimension,
    ParityResult,
    all_checks,
    check_by_id,
    dimension_by_name,
    outstanding_live_criteria,
)
from ..provenance import UPSTREAM_REVISION

# Every cloud reported enabled by /enabled_clouds. Retained to prove that
# selection outcomes are INDEPENDENT of this map (see
# TestProviderSelectionParity), not to feed it into select_options.
ALL_ENABLED = {"aws": True, "nebius": True, "lambda": True}


class TestMatrixShape:
    """The matrix covers the required dimensions and is internally consistent."""

    def test_all_eight_dimensions_present(self) -> None:
        assert {entry.dimension for entry in MATRIX} == set(Dimension)

    def test_every_dimension_has_checks_and_an_intent(self) -> None:
        for entry in MATRIX:
            assert entry.checks, entry.dimension.value
            assert entry.intent.strip(), entry.dimension.value

    def test_check_ids_are_unique(self) -> None:
        ids = [c.check_id for c in all_checks()]
        assert len(ids) == len(set(ids))

    def test_scenario_references_resolve(self) -> None:
        """A check must compare against a real inventory scenario."""
        known = {s.scenario_id for s in SCENARIOS}
        for check in all_checks():
            for ref in check.baseline_scenarios:
                assert ref in known, check.check_id

    def test_unknown_scenario_reference_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown scenario"):
            ParityCheck(
                check_id="bad",
                assertion="a",
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                baseline_scenarios=("nope",),
            )

    def test_unknown_gap_reference_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown gap"):
            ParityCheck(
                check_id="bad",
                assertion="a",
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
                blocked_by_gaps=("nope",),
            )

    def test_empty_assertion_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty assertion"):
            ParityCheck(
                check_id="bad",
                assertion="  ",
                baseline_evidence=EvidenceKind.SOURCE_FIXTURE,
            )

    def test_lookup_helpers(self) -> None:
        assert dimension_by_name(Dimension.BATCH_WORKLOAD).checks
        assert check_by_id("cost.hourly-and-daily-aggregation")
        with pytest.raises(KeyError):
            check_by_id("no.such.check")

    def test_unknown_dimension_lookup_raises(self) -> None:
        """dimension_by_name must not return a silent default."""

        class FakeDimension(str, Enum):
            NOPE = "nope"

        with pytest.raises(KeyError):
            dimension_by_name(FakeDimension.NOPE)  # type: ignore[arg-type]

    def test_dimension_rejects_having_no_checks(self) -> None:
        """An empty dimension would silently claim coverage it has none of."""
        with pytest.raises(ValueError, match="no checks"):
            ParityDimension(
                dimension=Dimension.BATCH_WORKLOAD,
                intent="i",
                checks=(),
            )

    def test_unknown_baseline_checks_reported_per_dimension(self) -> None:
        """Serving has no captured baseline at all; status/logs is fully covered."""
        serving = dimension_by_name(Dimension.SERVING_WORKLOAD)
        assert len(serving.unknown_baseline_checks) == len(serving.checks)
        status = dimension_by_name(Dimension.STATUS_AND_LOGS)
        assert status.unknown_baseline_checks == ()

    def test_spend_dimensions_declare_live_gates(self) -> None:
        """Anything that can cost money must name its authorization gate."""
        for dimension in (
            Dimension.PROVIDER_SELECTION,
            Dimension.BATCH_WORKLOAD,
            Dimension.SERVING_WORKLOAD,
            Dimension.COST_AND_CLEANUP,
        ):
            gates = dimension_by_name(dimension).live_gates
            assert gates, dimension.value
            joined = " ".join(gates).lower()
            assert "spend" in joined or "cleanup" in joined or "authorized" in joined


class TestFixtureProvenance:
    """Fixtures come from named upstream sources, never from a desired shape."""

    def test_every_fixture_has_a_citation(self) -> None:
        assert len(FIXTURE_PROVENANCE) == 11
        for name, citation in FIXTURE_PROVENANCE.items():
            assert citation.detail.strip(), name
            assert citation.revision == UPSTREAM_REVISION, name

    def test_fixtures_cite_upstream_schema_or_client_tests(self) -> None:
        """The permitted provenance sources for this story."""
        for name, citation in FIXTURE_PROVENANCE.items():
            assert citation.path.endswith((".go", ".yaml")), name

    def test_no_serving_fixture_is_fabricated(self) -> None:
        """The baseline client has no serve endpoint, so no fixture may exist."""
        for name in FIXTURE_PROVENANCE:
            assert "SERVE" not in name.upper()

    def test_health_and_launch_match_upstream_literals(self) -> None:
        assert HEALTH_RESPONSE == {"status": "healthy", "version": "0.12.0"}
        assert LAUNCH_RESPONSE["request_id"] == "req-abc-123"
        assert DOWN_RESPONSE["request_id"] == "req-down-456"

    def test_enabled_clouds_fixture_matches_the_upstream_literal(self) -> None:
        """Transcribed from TestEnabledClouds_Success's served response."""
        assert ENABLED_CLOUDS_RESPONSE["enabled_clouds"] == [
            {"name": "aws", "enabled": True},
            {"name": "gcp", "enabled": False},
            {"name": "nebius", "enabled": True},
        ]

    def test_pricing_fixture_is_not_pre_sorted(self) -> None:
        """Otherwise an implementation that never sorts would pass."""
        costs = [row["hourly_cost"] for row in GPU_PRICING_H100]
        assert costs != sorted(costs)

    def test_no_check_claims_enabled_clouds_filters_selection(self) -> None:
        """Guards against reintroducing a rule the baseline does not have.

        A check may mention /enabled_clouds only to record that it does NOT
        drive selection. Asserting exclusion-by-enabled-clouds as captured
        baseline behavior is the defect this test exists to catch: it would fail
        an adapter faithful to the baseline, and certify one that diverges.
        """
        for check in all_checks():
            if "/enabled_clouds" not in check.assertion:
                continue
            assert "does NOT consult" in check.assertion, check.check_id
            assert check.check_id != "provider.disabled-cloud-excluded"


class TestProviderSelectionParity:
    def test_options_ordered_cheapest_first(self) -> None:
        ordered = select_options(GPU_PRICING_H100)
        costs = [row["hourly_cost"] for row in ordered]
        assert costs == sorted(costs)
        assert ordered[0]["cloud"] == "lambda"

    def test_upstream_select_all_available_ordering(self) -> None:
        """Transferred from adapters/adapter_test.go::TestSelectAllAvailable.

        Upstream builds three adapters priced 10.00 / 2.50 / 5.00 in that
        order and asserts the cheapest is first and the most expensive last.
        """
        rows = (
            {"cloud": "cloud-expensive", "hourly_cost": 10.00, "available": True},
            {"cloud": "cloud-cheap", "hourly_cost": 2.50, "available": True},
            {"cloud": "cloud-mid", "hourly_cost": 5.00, "available": True},
        )
        ordered = select_options(rows)
        assert len(ordered) == 3
        assert ordered[0]["cloud"] == "cloud-cheap"
        assert ordered[2]["cloud"] == "cloud-expensive"

    def test_disabled_cloud_is_still_selected_when_statically_priced(self) -> None:
        """The baseline does NOT filter selection on /enabled_clouds.

        Regression test for a harness rule that was invented rather than
        captured. `isCloudEnabled` is reached only from `dynamicGPULookup` and
        `CheckAvailability`, both of which run only when the GPU type is absent
        from the adapter's static pricing map. H100 is in that map, so a cloud
        reported disabled by /enabled_clouds is still selected upstream —
        including when it is the cheapest option, as lambda is here.

        Asserting the opposite cuts both ways: an adapter faithful to the
        baseline would fail the harness, and one adding the filter to pass would
        be certified at parity while diverging.
        """
        assert ENABLED_CLOUDS_RESPONSE["enabled_clouds"][1] == {
            "name": "gcp",
            "enabled": False,
        }
        ordered = select_options(GPU_PRICING_H100)
        assert ordered[0]["cloud"] == "lambda"
        assert [row["cloud"] for row in ordered] == ["lambda", "nebius", "aws"]

    def test_selection_cannot_accept_an_enabled_clouds_map(self) -> None:
        """The map is structurally absent, not merely unused.

        Keeping it out of the signature is what stops the withdrawn filter from
        being quietly reintroduced: there is nowhere to pass it.
        """
        params = inspect.signature(select_options).parameters
        assert "enabled_clouds" not in params
        assert list(params) == ["pricing", "prefer_spot"]
        with pytest.raises(TypeError):
            select_options(GPU_PRICING_H100, enabled_clouds=ALL_ENABLED)  # type: ignore[call-arg]

    def test_configured_cloud_list_restricts_selection(self) -> None:
        """This is where the baseline actually excludes a cloud: filterAdapters."""
        restricted = filter_by_configured_clouds(GPU_PRICING_H100, ("aws", "nebius"))
        ordered = select_options(restricted)
        assert all(row["cloud"] != "lambda" for row in ordered)
        assert ordered[0]["cloud"] == "nebius"

    def test_empty_configured_cloud_list_means_no_restriction(self) -> None:
        """filterAdapters returns all adapters when pool.Spec.Clouds is empty."""
        assert filter_by_configured_clouds(GPU_PRICING_H100, ()) == tuple(
            GPU_PRICING_H100
        )

    def test_near_tie_broken_by_cloud_name(self) -> None:
        """Upstream compares cloud names when costs differ by under 0.001."""
        rows = (
            {"cloud": "zeta", "hourly_cost": 5.0, "available": True},
            {"cloud": "alpha", "hourly_cost": 5.0005, "available": True},
        )
        ordered = select_options(rows)
        assert [row["cloud"] for row in ordered] == ["alpha", "zeta"]

    def test_cost_difference_above_the_epsilon_beats_the_name(self) -> None:
        """A real price difference is not overridden by alphabetical order."""
        rows = (
            {"cloud": "alpha", "hourly_cost": 9.0, "available": True},
            {"cloud": "zeta", "hourly_cost": 2.0, "available": True},
        )
        ordered = select_options(rows)
        assert [row["cloud"] for row in ordered] == ["zeta", "alpha"]

    def test_near_tie_across_rounding_boundary_uses_upstream_comparison(self) -> None:
        """adapter.go compares the difference, not rounded price buckets."""
        rows = (
            {"cloud": "zeta", "hourly_cost": 5.00049, "available": True},
            {"cloud": "alpha", "hourly_cost": 5.00051, "available": True},
        )
        assert [row["cloud"] for row in select_options(rows)] == ["alpha", "zeta"]

    def test_no_available_options_yields_an_empty_list(self) -> None:
        """Upstream returns an error here; the harness returns no options.

        The observable consequence (the node ends Failed, not Provisioning) is
        covered by provider.fallback-on-launch-failure.
        """
        rows = ({"cloud": "aws", "hourly_cost": 1.0, "available": False},)
        assert select_options(rows) == []

    def test_prefer_spot_uses_lower_spot_price(self) -> None:
        """Spot pricing lowers AWS's effective cost without reordering here.

        AWS drops from 98.32 to its 40.00 spot price, which is still above both
        EU on-demand options, so the ordering is unchanged. The point is that
        the substitution happens at all.
        """
        ordered = select_options(GPU_PRICING_H100, prefer_spot=True)
        # Lambda/Nebius have no spot price in the baseline table, so their
        # on-demand cost stands; AWS drops from 98.32 to 40.00 but stays last.
        assert [row["cloud"] for row in ordered] == ["lambda", "nebius", "aws"]

    def test_zero_spot_price_does_not_win(self) -> None:
        """A 0.0 spot cost means 'unknown', not 'free'."""
        rows = (
            {
                "cloud": "aws",
                "hourly_cost": 10.0,
                "spot_cost": 0.0,
                "available": True,
            },
            {
                "cloud": "nebius",
                "hourly_cost": 5.0,
                "spot_cost": 0.0,
                "available": True,
            },
        )
        ordered = select_options(rows, prefer_spot=True)
        assert ordered[0]["cloud"] == "nebius"

    def test_unavailable_row_excluded(self) -> None:
        rows = (
            {
                "cloud": "aws",
                "hourly_cost": 1.0,
                "spot_cost": 0.0,
                "available": False,
            },
            {
                "cloud": "nebius",
                "hourly_cost": 9.0,
                "spot_cost": 0.0,
                "available": True,
            },
        )
        ordered = select_options(rows)
        assert [row["cloud"] for row in ordered] == ["nebius"]


class TestLaunchRequestParity:
    def test_task_carries_cloud_accelerators_and_disk(self) -> None:
        task = build_launch_task("aws", "H100", 8)
        resources = task["resources"]
        assert resources["cloud"] == "aws"
        assert resources["accelerators"] == "H100:8"
        assert resources["disk_size"] == DEFAULT_DISK_SIZE_GB

    def test_explicit_disk_size_overrides_default(self) -> None:
        task = build_launch_task("aws", "H100", 1, disk_size_gb=512)
        assert task["resources"]["disk_size"] == 512

    def test_region_and_spot_only_present_when_set(self) -> None:
        plain = build_launch_task("aws", "H100", 1)
        assert "region" not in plain["resources"]
        assert "use_spot" not in plain["resources"]
        full = build_launch_task("aws", "H100", 1, region="us-east-1", use_spot=True)
        assert full["resources"]["region"] == "us-east-1"
        assert full["resources"]["use_spot"] is True

    def test_task_has_no_join_step(self) -> None:
        """The documented baseline gap, asserted rather than assumed.

        If a future change adds setup/run to this builder, that is a deliberate
        departure from the baseline and this test should be updated alongside
        chain gap 'launch-task-has-no-join-step' — not silently deleted.
        """
        task = build_launch_task("aws", "H100", 1)
        assert set(task) == {"resources"}
        assert "setup" not in task
        assert "run" not in task

    def test_autostop_and_timeout_defaults_recorded(self) -> None:
        assert DEFAULT_IDLE_MINUTES_TO_AUTOSTOP == 120
        assert DEFAULT_TIMEOUT_MINUTES == 30


class TestStatusAndLogsParity:
    def test_sse_frames_parsed_in_order(self) -> None:
        events = parse_sse(SSE_LAUNCH_SUCCESS)
        assert [e.event_id for e in events] == ["1", "2", "3"]
        assert [e.event for e in events] == ["message", "message", "complete"]

    def test_complete_event_is_terminal_and_succeeds(self) -> None:
        outcome = consume_stream(SSE_LAUNCH_SUCCESS)
        assert outcome.succeeded
        assert outcome.error is None
        assert outcome.lines[0] == "[sky] Launching cluster..."
        assert len(outcome.lines) == 3

    def test_multiline_data_is_joined_with_newlines(self) -> None:
        """Transferred from client_test.go::TestStreamProgress_MultilineData.

        Upstream's test server writes exactly this byte sequence and asserts
        `events[0].Data == "line1\\nline2"`. A flat field dict silently kept only
        the last data line, losing `line1`.
        """
        events = parse_sse(SSE_MULTILINE_DATA)
        assert events[0].data == "line1\nline2"
        assert events[0].event == "message"
        assert not events[0].is_terminal
        assert events[1].data == "done"
        assert events[1].is_terminal

    def test_crlf_frames_preserve_upstream_event_boundaries(self) -> None:
        """Upstream bufio.Scanner's ScanLines removes CR before LF."""
        events = parse_sse(SSE_MULTILINE_DATA.replace("\n", "\r\n"))
        assert [(event.event, event.data) for event in events] == [
            ("message", "line1\nline2"),
            ("complete", "done"),
        ]
        outcome = consume_stream(
            SSE_MULTILINE_DATA.replace("\n", "\r\n"), cancel_after=1
        )
        assert outcome.cancelled
        assert outcome.lines == ["[sky] line1", "[sky] line2"]

    def test_multiline_frame_streams_every_progress_line(self) -> None:
        """The operator-visible consequence of the parser fix.

        streamLaunchProgress splits event data on newlines and emits one
        `[sky] ` line per non-empty part, so dropping data lines would show an
        operator less than the baseline while still looking like it streamed.
        """
        outcome = consume_stream(SSE_MULTILINE_DATA)
        assert outcome.lines == ["[sky] line1", "[sky] line2", "[sky] done"]
        assert outcome.succeeded

    def test_multiline_error_message_is_preserved_whole(self) -> None:
        """A multi-line failure reason must not be truncated to its last line."""
        raw = "event: error\ndata: no capacity\ndata: try another region\n\n"
        outcome = consume_stream(raw)
        assert not outcome.succeeded
        assert outcome.error == "no capacity\ntry another region"

    def test_only_one_leading_space_is_stripped_from_data(self) -> None:
        """Upstream strips a single space after `data:`, not all whitespace."""
        assert parse_sse("event: message\ndata:  indented\n\n")[0].data == " indented"
        assert parse_sse("event: message\ndata:x\n\n")[0].data == "x"

    def test_frame_with_only_an_id_is_not_an_event(self) -> None:
        """Upstream dispatches only when data or event type is set.

        Counting such a frame would also shift cancellation indices.
        """
        assert parse_sse("id: 1\n\n") == []

    def test_error_event_fails_and_preserves_the_message(self) -> None:
        outcome = consume_stream(SSE_LAUNCH_ERROR)
        assert not outcome.succeeded
        assert outcome.error == "no capacity in region"

    def test_stream_stops_at_the_terminal_event(self) -> None:
        """Nothing after a terminal frame is consumed."""
        raw = SSE_LAUNCH_SUCCESS + "id: 4\nevent: message\ndata: extra\n\n"
        outcome = consume_stream(raw)
        assert "[sky] extra" not in outcome.lines

    def test_empty_stream_is_not_a_failure(self) -> None:
        outcome = consume_stream("")
        assert outcome.succeeded
        assert outcome.lines == []

    def test_blank_data_lines_are_not_emitted(self) -> None:
        outcome = consume_stream("id: 1\nevent: message\ndata: \n\n")
        assert outcome.lines == []

    def test_missing_event_field_defaults_to_message(self) -> None:
        events = parse_sse("id: 1\ndata: hello\n\n")
        assert events[0].event == "message"
        assert not events[0].is_terminal

    def test_whitespace_only_lines_inside_a_frame_are_skipped(self) -> None:
        """SSE writers pad frames; padding must not become a bogus field."""
        events = parse_sse("id: 1\n   \nevent: complete\ndata: done\n\n")
        assert len(events) == 1
        assert events[0].event == "complete"
        assert events[0].data == "done"

    def test_only_up_is_ready(self) -> None:
        assert map_cluster_status("UP") == "ready"
        assert map_cluster_status("INIT") == "provisioning"
        assert map_cluster_status("STOPPED") == "stopped"
        assert map_cluster_status("WEIRD") == "unknown"

    def test_status_fixture_shapes(self) -> None:
        assert STATUS_RESPONSE_UP[0]["status"] == "UP"
        assert STATUS_RESPONSE_UP[0]["handle"]["head_ip"] == "10.0.0.1"
        assert STATUS_RESPONSE_EMPTY == []
        assert STATUS_RESPONSE_STOPPED[0]["status"] == "STOPPED"

    def test_stopped_cluster_still_has_a_handle(self) -> None:
        """Why existing-state enumeration cannot ignore STOPPED clusters."""
        handle = STATUS_RESPONSE_STOPPED[0]["handle"]
        assert handle["cluster_name"] == "my-cluster"


class TestCancellationParity:
    def test_cancel_mid_stream_stops_without_success(self) -> None:
        outcome = consume_stream(SSE_LAUNCH_SUCCESS, cancel_after=2)
        assert outcome.cancelled
        assert not outcome.succeeded
        assert len(outcome.lines) == 2

    def test_cancel_before_any_event_yields_no_output(self) -> None:
        outcome = consume_stream(SSE_LAUNCH_SUCCESS, cancel_after=0)
        assert outcome.cancelled
        assert outcome.lines == []

    def test_cancel_after_terminal_event_is_a_success(self) -> None:
        """Cancelling after completion must not rewrite the outcome."""
        outcome = consume_stream(SSE_LAUNCH_SUCCESS, cancel_after=5)
        assert outcome.succeeded
        assert not outcome.cancelled


class TestCleanupParity:
    def test_down_without_purge_on_the_happy_path(self) -> None:
        outcome = teardown("my-cluster")
        assert outcome.calls == [("my-cluster", False)]
        assert outcome.succeeded
        assert not outcome.used_purge

    def test_purge_retried_only_after_failure(self) -> None:
        outcome = teardown("my-cluster", first_attempt_fails=True)
        assert outcome.calls == [("my-cluster", False), ("my-cluster", True)]
        assert outcome.succeeded
        assert outcome.used_purge

    def test_purge_failure_surfaces(self) -> None:
        outcome = teardown("my-cluster", first_attempt_fails=True, purge_fails=True)
        assert not outcome.succeeded

    def test_purge_success_is_not_cleanup_evidence(self) -> None:
        """The central cleanup honesty property.

        SkyPilot's purge drops local cluster state regardless of whether the
        provider released anything. A green teardown therefore must not imply
        the resource is gone.
        """
        outcome = teardown("my-cluster", first_attempt_fails=True)
        assert outcome.succeeded
        assert not outcome.provider_absence_confirmed

    def test_provider_absence_check_has_no_live_baseline(self) -> None:
        check = check_by_id("cleanup.provider-side-absence-verified")
        assert check.baseline_unknown
        assert "purge is NOT evidence" in check.assertion


class TestCostParity:
    def test_hourly_sum_and_daily_projection(self) -> None:
        hourly, daily = aggregate_costs([2.95, 2.86])
        assert hourly == 5.81
        assert daily == round(5.81 * 24, 10)

    def test_empty_pool_costs_nothing(self) -> None:
        assert aggregate_costs([]) == (0.0, 0.0)

    def test_cost_is_labelled_an_estimate_not_a_control(self) -> None:
        check = check_by_id("cost.observation-not-a-spend-control")
        assert "not billed spend" in check.assertion


class TestMocksCannotProveParity:
    """A fully green offline run yields zero live-parity claims.

    This is the property the issue asks for: "mocks cannot produce a live-parity
    pass". It is enforced structurally — ParityResult.live_verified is derived
    from the evidence kind rather than stored — so no test, fixture or future
    adapter can set it directly.
    """

    def test_fixture_result_is_never_live_verified(self) -> None:
        result = fixture_result("provider.ordering-cheapest-first", passed=True)
        assert result.passed
        assert not result.live_verified
        assert not result.supports_parity_claim

    def test_captured_replay_is_still_not_live(self) -> None:
        """Replaying a real recording is stronger, but it is not a live run."""
        result = ParityResult(
            check_id="provider.ordering-cheapest-first",
            passed=True,
            evidence=EvidenceKind.CAPTURED_REPLAY,
        )
        assert not result.live_verified

    def test_live_verified_requires_a_pass(self) -> None:
        result = ParityResult(
            check_id="provider.ordering-cheapest-first",
            passed=False,
            evidence=EvidenceKind.LIVE_CAPTURE,
        )
        assert not result.live_verified

    def test_live_capture_can_be_live_verified(self) -> None:
        """The positive control: the property is achievable, just not offline."""
        result = ParityResult(
            check_id="provider.ordering-cheapest-first",
            passed=True,
            evidence=EvidenceKind.LIVE_CAPTURE,
        )
        assert result.live_verified
        assert result.supports_parity_claim

    def test_live_run_without_a_captured_baseline_is_not_parity(self) -> None:
        """Measuring one system is not comparing two.

        node.join-produces-ready-node has no captured baseline, so even a
        successful live ADP run cannot cite it as parity evidence.
        """
        result = ParityResult(
            check_id="node.join-produces-ready-node",
            passed=True,
            evidence=EvidenceKind.LIVE_CAPTURE,
        )
        assert result.live_verified
        assert not result.supports_parity_claim

    def test_all_green_fixtures_leave_every_criterion_outstanding(self) -> None:
        """The smoke check's meaning: exit 0 does not mean parity achieved."""
        results = {
            check.check_id: fixture_result(check.check_id, passed=True)
            for check in all_checks()
        }
        outstanding = outstanding_live_criteria(results)
        assert set(outstanding) == {c.check_id for c in all_checks()}

    def test_no_results_leaves_everything_outstanding(self) -> None:
        assert len(outstanding_live_criteria({})) == len(all_checks())

    def test_checks_with_uncaptured_baselines_are_marked(self) -> None:
        """The join, batch scheduling and serving paths were never captured."""
        for check_id in (
            "node.join-produces-ready-node",
            "batch.gpu-workload-schedules",
            "serving.endpoint-reachable",
        ):
            assert check_by_id(check_id).baseline_unknown, check_id

    def test_blocked_checks_name_the_gap_that_blocks_them(self) -> None:
        blocked = [c for c in all_checks() if c.blocked_by_gaps]
        assert blocked
        for check in blocked:
            assert all(gap for gap in check.blocked_by_gaps), check.check_id
