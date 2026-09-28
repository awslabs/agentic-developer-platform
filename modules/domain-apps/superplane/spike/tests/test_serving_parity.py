"""Batch success cannot satisfy serving reachability, auth or cleanup.

Issue #5040 (U12), EPIC #4910.

Serving is the dimension most likely to be declared done by accident. A batch
GPU job and a served model both "run on a GPU node", so a green batch result
reads like evidence that serving works. It is not: a served endpoint additionally
has to be reachable, has to refuse unauthenticated callers, and has to be owned
by exactly one controller that can tear its replicas down.

In this baseline the gap is wider than "untested". The SkyServe specs are real
and detailed, but they are operator-run CLI artifacts and the controller's
SkyPilot client has no serve endpoint at all — so nothing reconciles a service.
These tests pin that finding so the serving criteria cannot be quietly closed by
batch evidence or by an adapter that merely launches a cluster.
"""

from __future__ import annotations

from ..baseline_inventory import (
    CHAIN_GAPS,
    EXISTING_STATE_CLASSES,
    SKYPILOT_CLIENT_ENDPOINTS,
    scenario_by_id,
)
from ..harness import fixture_result
from ..parity_matrix import (
    Dimension,
    EvidenceKind,
    ParityResult,
    dimension_by_name,
    outstanding_live_criteria,
)
from ..provenance import EvidenceStatus

SERVING = dimension_by_name(Dimension.SERVING_WORKLOAD)
BATCH = dimension_by_name(Dimension.BATCH_WORKLOAD)

SERVING_CHECK_IDS = {c.check_id for c in SERVING.checks}
BATCH_CHECK_IDS = {c.check_id for c in BATCH.checks}


class TestServingIsSeparateFromBatch:
    def test_serving_and_batch_are_distinct_dimensions(self) -> None:
        assert SERVING.dimension is not BATCH.dimension
        assert not SERVING_CHECK_IDS & BATCH_CHECK_IDS

    def test_all_batch_checks_green_leaves_serving_outstanding(self) -> None:
        """The substitution this test file exists to prevent.

        Every batch check is marked passed — even at LIVE_CAPTURE strength — and
        every serving check must still be reported outstanding.
        """
        results = {
            check_id: ParityResult(
                check_id=check_id,
                passed=True,
                evidence=EvidenceKind.LIVE_CAPTURE,
            )
            for check_id in BATCH_CHECK_IDS
        }
        outstanding = set(outstanding_live_criteria(results))
        assert SERVING_CHECK_IDS <= outstanding

    def test_batch_evidence_does_not_satisfy_any_serving_check(self) -> None:
        """No serving check shares a baseline scenario with a batch check."""
        batch_scenarios = {
            ref for check in BATCH.checks for ref in check.baseline_scenarios
        }
        for check in SERVING.checks:
            assert not set(check.baseline_scenarios) & batch_scenarios, check.check_id

    def test_serving_requires_four_independent_properties(self) -> None:
        """Reachability, authentication, ownership and teardown are separate."""
        assert SERVING_CHECK_IDS == {
            "serving.endpoint-reachable",
            "serving.unauthenticated-request-refused",
            "serving.owning-controller-identified",
            "serving.teardown-removes-replicas",
        }


class TestReachabilityAndAuthenticationAreSeparate:
    def test_reachability_alone_is_not_serving_parity(self) -> None:
        """An endpoint that answers everyone is a finding, not a pass."""
        results = {
            "serving.endpoint-reachable": ParityResult(
                check_id="serving.endpoint-reachable",
                passed=True,
                evidence=EvidenceKind.LIVE_CAPTURE,
            )
        }
        outstanding = set(outstanding_live_criteria(results))
        assert "serving.unauthenticated-request-refused" in outstanding

    def test_authentication_check_refuses_rather_than_permits(self) -> None:
        check = next(
            c
            for c in SERVING.checks
            if c.check_id == "serving.unauthenticated-request-refused"
        )
        assert "refused" in check.assertion
        assert "open endpoint" in check.assertion

    def test_parity_does_not_preserve_an_auth_bypass(self) -> None:
        """Functional parity excludes authentication bypasses by design."""
        assert "not working serving" in next(
            c.assertion
            for c in SERVING.checks
            if c.check_id == "serving.unauthenticated-request-refused"
        )


class TestOwningControllerLifecycle:
    def test_no_owning_controller_gap_is_recorded(self) -> None:
        gap = next(
            g for g in CHAIN_GAPS if g.gap_id == "no-owning-controller-for-serving"
        )
        assert "No controller reconciles" in gap.description
        assert "would not be reconciled away" in gap.consequence

    def test_every_serving_check_is_blocked_by_the_ownership_gap(self) -> None:
        """Nothing in serving can pass live while no controller owns it."""
        for check in SERVING.checks:
            assert "no-owning-controller-for-serving" in check.blocked_by_gaps, (
                check.check_id
            )

    def test_teardown_requires_provider_side_confirmation(self) -> None:
        check = next(
            c
            for c in SERVING.checks
            if c.check_id == "serving.teardown-removes-replicas"
        )
        assert "confirmed provider-side" in check.assertion
        assert "orphaned GPU capacity" in check.assertion

    def test_serving_live_gates_require_a_named_owner(self) -> None:
        joined = " ".join(SERVING.live_gates).lower()
        assert "owning controller" in joined
        assert "cleanup owner" in joined


class TestServingBaselineIsUncaptured:
    def test_no_serving_check_has_a_captured_baseline(self) -> None:
        """There is nothing to compare a migrated service against yet."""
        for check in SERVING.checks:
            assert check.baseline_unknown, check.check_id

    def test_serving_scenario_is_absent_from_the_controller(self) -> None:
        scenario = scenario_by_id("serving-via-sky-serve-yaml")
        assert scenario.evidence_status is EvidenceStatus.ABSENT

    def test_specs_are_real_but_operator_run(self) -> None:
        """ABSENT describes the controller, not the project's serving specs."""
        scenario = scenario_by_id("serving-via-sky-serve-yaml")
        assert scenario.caveat is not None
        assert "the specs are real" in scenario.caveat
        assert "operator-run CLI artifacts" in scenario.caveat
        assert any("skypilot-models" in c.path for c in scenario.citations)

    def test_client_has_no_serve_endpoint(self) -> None:
        """The mechanical reason serving cannot be controller-attested."""
        methods = {method for _, _, method in SKYPILOT_CLIENT_ENDPOINTS}
        assert "Serve" not in methods
        assert methods == {
            "Health",
            "Launch",
            "Status",
            "Down",
            "EnabledClouds",
            "StreamProgress",
        }

    def test_ordered_fallback_is_recorded_as_the_serving_shape(self) -> None:
        """Serving parity has to account for multi-region ordered fallback."""
        scenario = scenario_by_id("serving-via-sky-serve-yaml")
        assert "ordered" in scenario.expected_outcome
        assert "8000" in scenario.expected_outcome

    def test_serving_services_are_an_existing_state_class(self) -> None:
        """A running service must not be missed during cutover."""
        entry = next(e for e in EXISTING_STATE_CLASSES if e.kind == "skyserve_services")
        assert entry.default_decision == "undecided"
        assert "no controller-side inventory" in entry.how_to_enumerate


class TestOfflineServingRunProvesNothing:
    def test_fixture_results_cannot_close_serving_criteria(self) -> None:
        results = {
            check.check_id: fixture_result(check.check_id, passed=True)
            for check in SERVING.checks
        }
        outstanding = set(outstanding_live_criteria(results))
        assert SERVING_CHECK_IDS <= outstanding

    def test_even_a_live_serving_run_needs_a_baseline_to_compare(self) -> None:
        """Deferred R17 serving-baseline criterion, asserted.

        A live ADP-side serving run still cannot support a parity claim while the
        baseline's serving behavior is uncaptured.
        """
        results = {
            check.check_id: ParityResult(
                check_id=check.check_id,
                passed=True,
                evidence=EvidenceKind.LIVE_CAPTURE,
            )
            for check in SERVING.checks
        }
        for result in results.values():
            assert result.live_verified
            assert not result.supports_parity_claim
        assert SERVING_CHECK_IDS <= set(outstanding_live_criteria(results))
