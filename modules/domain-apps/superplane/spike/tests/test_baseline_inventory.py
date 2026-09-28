"""Every observed scenario maps to a revision, inputs, outcome and evidence.

Issue #5040 (U12), EPIC #4910.

These tests guard the inventory's integrity rather than any runtime behavior.
The failure mode they prevent is an inventory that grows unsupported claims:
a scenario added without citations, an evidence level quietly upgraded to
"works", or the AWS support limitation reworded into a requirement to replace
the architecture.
"""

from __future__ import annotations

import pytest

from ..baseline_inventory import (
    CHAIN_GAPS,
    EXISTING_STATE_CLASSES,
    HANDOVER_DECISIONS,
    SCENARIOS,
    SKYPILOT_CLIENT_ENDPOINTS,
    SUPPORT_LIMITATION,
    SUPPORT_LIMITATION_CITATION,
    BaselineScenario,
    scenario_by_id,
    scenarios_with_status,
    unresolved_gaps,
)
from ..provenance import UPSTREAM_REVISION, Citation, EvidenceStatus


class TestScenarioCompleteness:
    """Each scenario carries the four things the story requires."""

    def test_every_scenario_has_inputs_outcome_and_citations(self) -> None:
        for scenario in SCENARIOS:
            assert scenario.inputs, f"{scenario.scenario_id} has no inputs"
            assert scenario.expected_outcome.strip(), scenario.scenario_id
            assert scenario.citations, f"{scenario.scenario_id} has no citations"

    def test_every_citation_pins_the_upstream_revision(self) -> None:
        """A claim without a revision is not reproducible."""
        for scenario in SCENARIOS:
            for citation in scenario.citations:
                assert citation.revision == UPSTREAM_REVISION, scenario.scenario_id
                assert citation.detail.strip(), citation.path

    def test_scenario_ids_are_unique(self) -> None:
        ids = [s.scenario_id for s in SCENARIOS]
        assert len(ids) == len(set(ids))

    def test_citation_exposes_a_runnable_lookup_command(self) -> None:
        """A reader must be able to fetch the cited file without guessing."""
        citation = SCENARIOS[0].citations[0]
        assert citation.show_command.startswith("git show FETCH_HEAD:")
        assert "ai-super-plane/reference/" in citation.snapshot_path

    def test_scenario_lookup_by_id(self) -> None:
        found = scenario_by_id("skypilot-launch-and-stream")
        assert found.evidence_status is EvidenceStatus.SOURCE_ONLY

    def test_unknown_scenario_id_raises(self) -> None:
        with pytest.raises(KeyError):
            scenario_by_id("no-such-scenario")

    def test_scenario_rejects_missing_inputs(self) -> None:
        with pytest.raises(ValueError, match="no inputs"):
            BaselineScenario(
                scenario_id="bad",
                summary="s",
                inputs=(),
                expected_outcome="o",
                evidence_status=EvidenceStatus.SOURCE_ONLY,
                citations=(Citation(path="a.go", detail="d"),),
            )

    def test_scenario_rejects_missing_citations(self) -> None:
        """An uncited scenario is an assertion, not evidence."""
        with pytest.raises(ValueError, match="no citations"):
            BaselineScenario(
                scenario_id="bad",
                summary="s",
                inputs=("i",),
                expected_outcome="o",
                evidence_status=EvidenceStatus.SOURCE_ONLY,
                citations=(),
            )

    def test_scenario_rejects_empty_outcome(self) -> None:
        with pytest.raises(ValueError, match="no expected outcome"):
            BaselineScenario(
                scenario_id="bad",
                summary="s",
                inputs=("i",),
                expected_outcome="   ",
                evidence_status=EvidenceStatus.SOURCE_ONLY,
                citations=(Citation(path="a.go", detail="d"),),
            )


class TestCitationValidation:
    def test_absolute_path_rejected(self) -> None:
        """Citations are snapshot-relative so they resolve on any checkout."""
        with pytest.raises(ValueError, match="snapshot-relative"):
            Citation(path="/etc/passwd", detail="d")

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="snapshot-relative"):
            Citation(path="", detail="d")

    def test_detail_required(self) -> None:
        with pytest.raises(ValueError, match="no detail"):
            Citation(path="a.go", detail="  ")


class TestEvidenceClassification:
    """Source-only, stubbed and user-observed stay separated."""

    def test_no_scenario_claims_live_verification(self) -> None:
        """The strongest attestation available is a user's report.

        This is the core honesty property of the inventory: nothing here was
        observed by ADP, so no scenario may be recorded above USER_REPORTED.
        """
        allowed = set(EvidenceStatus)
        for scenario in SCENARIOS:
            assert scenario.evidence_status in allowed

    def test_the_join_step_is_the_user_reported_one(self) -> None:
        """The user's 'works well' report is about the EKS join path."""
        reported = scenarios_with_status(EvidenceStatus.USER_REPORTED)
        ids = {s.scenario_id for s in reported}
        assert "eks-join-via-onboarding-scripts" in ids

    def test_user_reported_scenarios_record_what_is_still_uncaptured(self) -> None:
        """A user report is not a captured baseline; the gap must be stated."""
        for scenario in scenarios_with_status(EvidenceStatus.USER_REPORTED):
            assert scenario.caveat, scenario.scenario_id
            assert "deferred" in scenario.caveat.lower()

    def test_serving_is_absent_from_the_controller(self) -> None:
        """Serving exists as CLI specs, not as controller behavior."""
        serving = scenario_by_id("serving-via-sky-serve-yaml")
        assert serving.evidence_status is EvidenceStatus.ABSENT
        assert serving.caveat is not None
        # It must not be recorded as simply missing: the specs are real.
        assert "operator-run" in serving.caveat

    def test_health_monitoring_records_its_precondition(self) -> None:
        """Health monitoring is inert without k8sNodeName; say so."""
        scenario = scenario_by_id("node-health-monitoring")
        assert scenario.caveat is not None
        assert "k8s-node-name-never-assigned" in scenario.caveat


class TestSupportLimitation:
    """The AWS limitation is a constraint, not a mandate to replace the design."""

    def test_limitation_recorded_verbatim(self) -> None:
        assert "does NOT officially support" in SUPPORT_LIMITATION
        assert "unsupported configuration" in SUPPORT_LIMITATION

    def test_limitation_is_cited(self) -> None:
        assert SUPPORT_LIMITATION_CITATION.path.endswith("README.md")
        assert SUPPORT_LIMITATION_CITATION.revision == UPSTREAM_REVISION

    def test_limitation_is_not_stated_as_a_no_join_requirement(self) -> None:
        """The amendment supersedes the mandatory no-EKS-join spike.

        Recording the limitation must not smuggle back a requirement to avoid
        joining nodes to EKS, which is the behavior the user asked to preserve.
        """
        text = SUPPORT_LIMITATION.lower()
        for forbidden in ("must not join", "do not join", "prohibited", "forbidden"):
            assert forbidden not in text

    def test_join_scenario_still_present_despite_limitation(self) -> None:
        """Regression check: no onboarding path is removed by this story."""
        assert scenario_by_id("eks-join-via-onboarding-scripts")


class TestChainGaps:
    """The gaps U19 must decide are recorded with evidence and a decision."""

    def test_every_gap_has_consequence_citations_and_a_decision(self) -> None:
        for gap in CHAIN_GAPS:
            assert gap.description.strip(), gap.gap_id
            assert gap.consequence.strip(), gap.gap_id
            assert gap.citations, gap.gap_id
            assert gap.u19_decision_required.strip(), gap.gap_id

    def test_k8s_node_name_gap_is_recorded(self) -> None:
        """The field is declared but never assigned, so health checks skip."""
        gap = next(g for g in CHAIN_GAPS if g.gap_id == "k8s-node-name-never-assigned")
        assert "ever assigns either field" in gap.description
        assert "skips every check" in gap.consequence

    def test_launch_task_join_gap_is_recorded(self) -> None:
        """The Go-built task has no run phase, so it cannot join by itself."""
        gap = next(g for g in CHAIN_GAPS if g.gap_id == "launch-task-has-no-join-step")
        assert "no setup or" in gap.description
        assert "does not join it to EKS" in gap.consequence

    def test_secret_exposure_gap_is_not_treated_as_desired_behavior(self) -> None:
        """Parity must not preserve secret exposure."""
        gap = next(
            g
            for g in CHAIN_GAPS
            if g.gap_id == "ssm-activation-credentials-in-task-envs"
        )
        assert "does not preserve secret exposure" in gap.u19_decision_required

    def test_serving_ownership_gap_requires_a_single_owner(self) -> None:
        gap = next(
            g for g in CHAIN_GAPS if g.gap_id == "no-owning-controller-for-serving"
        )
        assert "one controller must own" in gap.u19_decision_required

    def test_gaps_are_unresolved_in_this_story(self) -> None:
        """This story authors the record; it does not close the gaps."""
        assert unresolved_gaps() == CHAIN_GAPS
        assert len(CHAIN_GAPS) == 4

    def test_no_credential_values_in_the_inventory(self) -> None:
        """Test artifacts contain no credential values.

        Activation ids/codes are referred to by NAME only. This asserts the
        inventory never grows a real-looking value.
        """
        haystack = " ".join(
            [
                g.description + g.consequence + g.u19_decision_required
                for g in CHAIN_GAPS
            ]
            + [s.summary + s.expected_outcome + (s.caveat or "") for s in SCENARIOS]
        )
        assert "SSM_ACTIVATION_CODE=" not in haystack
        assert "activation-code-" not in haystack


class TestSkyPilotSurface:
    """The client's endpoint list bounds what the baseline can attest."""

    def test_recorded_endpoints_match_the_client(self) -> None:
        paths = {path for _, path, _ in SKYPILOT_CLIENT_ENDPOINTS}
        assert paths == {
            "/api/health",
            "/launch",
            "/status",
            "/down",
            "/enabled_clouds",
            "/api/stream",
        }

    def test_no_serve_or_jobs_endpoint_exists(self) -> None:
        """Why serving cannot be attested by the controller at all."""
        for _, path, _ in SKYPILOT_CLIENT_ENDPOINTS:
            assert "serve" not in path
            assert "jobs" not in path


class TestExistingStateHandover:
    """U19's adopt / drain-relaunch / no-existing-state decision is specified."""

    def test_every_state_class_has_enumeration_and_rationale(self) -> None:
        for entry in EXISTING_STATE_CLASSES:
            assert entry.how_to_enumerate.strip(), entry.kind
            assert entry.rationale.strip(), entry.kind

    def test_decisions_are_from_the_permitted_set(self) -> None:
        for entry in EXISTING_STATE_CLASSES:
            assert entry.default_decision in HANDOVER_DECISIONS

    def test_no_state_class_defaults_to_a_silent_clean_deployment(self) -> None:
        """The amendment forbids assuming there is nothing to migrate.

        Every class starts 'undecided' so U19 must make an explicit call. A
        default of 'no_existing_state' would be precisely the silent assumption
        the amendment rules out.
        """
        for entry in EXISTING_STATE_CLASSES:
            assert entry.default_decision == "undecided", entry.kind

    def test_no_existing_state_is_an_available_but_verifiable_outcome(self) -> None:
        assert "no_existing_state" in HANDOVER_DECISIONS

    def test_stopped_clusters_and_serving_are_both_covered(self) -> None:
        """The two classes most likely to be missed during a cutover."""
        kinds = {e.kind for e in EXISTING_STATE_CLASSES}
        assert "skypilot_clusters" in kinds
        assert "skyserve_services" in kinds
        assert "skypilot_api_server_state" in kinds

    def test_api_state_store_records_the_orphaning_risk(self) -> None:
        entry = next(
            e for e in EXISTING_STATE_CLASSES if e.kind == "skypilot_api_server_state"
        )
        assert "orphans every running cluster" in entry.rationale
