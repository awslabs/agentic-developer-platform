"""Contract shape and version discipline.

Issue #5043 (U8), EPIC #4910.

The failure mode these tests prevent is the silent one: a submitter and a receiver
that disagree about the contract and never find out. An unversioned or
version-mismatched submission has to be *refused*, because the alternative —
interpreting the fields it recognizes — produces an accepted observation that means
something different at each end, with nothing in the system reporting an error.

Also asserted here: the wire shape is stable and hand-written. `to_wire()` is the
public contract, so these tests pin its keys. If a field is renamed in the
dataclass without a version bump, the wire assertions fail rather than the rename
silently becoming a breaking change for the upstream receiver.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from conftest import OBSERVED_AT, W1
from superplane_contracts import (
    CONTRACT_VERSION,
    OBSERVATION_KINDS,
    SUPPORTED_VERSIONS,
    VERSION_FIELD,
    VERSION_HEADER,
    BudgetUsage,
    CheckResult,
    CheckStatus,
    ClusterRef,
    ContractViolation,
    Observation,
    check_version,
)


class TestVersionDiscipline:
    """A submission's version is checked before anything else is trusted."""

    def test_matching_supported_version_is_accepted(self) -> None:
        result = check_version(CONTRACT_VERSION, CONTRACT_VERSION)
        assert result.accepted
        assert result.version == CONTRACT_VERSION

    def test_missing_version_entirely_is_refused(self) -> None:
        """No version at all: refused, not defaulted.

        Defaulting is the tempting behaviour and the dangerous one — it makes an
        unversioned sender work today and break invisibly at the next bump.
        """
        result = check_version(None, None)
        assert not result.accepted
        assert result.reason == "missing contract version"

    def test_missing_header_alone_is_refused(self) -> None:
        result = check_version(None, CONTRACT_VERSION)
        assert not result.accepted
        assert VERSION_HEADER in result.reason

    def test_missing_payload_field_alone_is_refused(self) -> None:
        """The header is present but the payload copy is not.

        Refused because the payload copy is the one that survives queuing,
        logging and replay; accepting header-only would make a replayed body
        unversioned.
        """
        result = check_version(CONTRACT_VERSION, None)
        assert not result.accepted
        assert VERSION_FIELD in result.reason

    def test_blank_version_is_refused(self) -> None:
        """Whitespace is not a version."""
        assert not check_version("   ", "   ").accepted

    def test_header_and_payload_disagreement_is_refused(self) -> None:
        """Two versions in one submission, disagreeing: refused, not resolved.

        Preferring either would let whichever surface is easier to tamper with
        decide how the body is interpreted.
        """
        result = check_version(CONTRACT_VERSION, "v99")
        assert not result.accepted
        assert result.reason == "contract version mismatch"

    def test_unsupported_version_is_refused(self) -> None:
        result = check_version("v99", "v99")
        assert not result.accepted
        assert "unsupported contract version" in result.reason

    def test_refusal_reason_does_not_echo_payload(self) -> None:
        """A refusal names the problem without quoting arbitrary caller input.

        The mismatch branch is the one that has two caller-supplied values to
        hand, so it is the one worth pinning: it must not become a reflector.
        """
        result = check_version("v1", "attacker-controlled-value")
        assert not result.accepted
        assert "attacker-controlled-value" not in result.reason

    def test_supported_versions_contains_the_current_version(self) -> None:
        """Version discipline is self-consistent.

        A CONTRACT_VERSION absent from SUPPORTED_VERSIONS would refuse every
        submission this package itself produces.
        """
        assert CONTRACT_VERSION in SUPPORTED_VERSIONS


class TestObservationShape:
    """The payload refuses to construct in states that establish nothing."""

    def test_fleet_health_observation_carries_its_version(self, w1_observation) -> None:
        assert w1_observation.contract_version == CONTRACT_VERSION
        assert w1_observation.to_wire()[VERSION_FIELD] == CONTRACT_VERSION

    def test_unknown_kind_is_refused(self, healthy_check) -> None:
        """An unnameable kind cannot be scoped or interpreted, so it is refused."""
        with pytest.raises(ContractViolation, match="unknown observation kind"):
            Observation(
                kind="something_new",
                subject=ClusterRef(cluster_id="c1", workspace=W1),
                reported_at=OBSERVED_AT,
                reporter="platform-monitor",
                checks=(healthy_check,),
            )

    def test_known_kinds_are_exactly_fleet_health_and_budget_usage(self) -> None:
        assert OBSERVATION_KINDS == frozenset({"fleet_health", "budget_usage"})

    def test_fleet_health_with_no_checks_is_refused(self) -> None:
        """An empty health report would occupy the cluster's state slot for free.

        Refused so a reporter cannot overwrite a real reading with one that
        observed nothing.
        """
        with pytest.raises(ContractViolation, match="at least one check"):
            Observation(
                kind="fleet_health",
                subject=ClusterRef(cluster_id="c1", workspace=W1),
                reported_at=OBSERVED_AT,
                reporter="platform-monitor",
            )

    def test_naive_reported_at_is_refused(self, healthy_check) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            Observation(
                kind="fleet_health",
                subject=ClusterRef(cluster_id="c1", workspace=W1),
                reported_at=OBSERVED_AT.replace(tzinfo=None),
                reporter="platform-monitor",
                checks=(healthy_check,),
            )

    def test_blank_reporter_is_refused(self, healthy_check) -> None:
        with pytest.raises(ContractViolation, match="reporter"):
            Observation(
                kind="fleet_health",
                subject=ClusterRef(cluster_id="c1", workspace=W1),
                reported_at=OBSERVED_AT,
                reporter="  ",
                checks=(healthy_check,),
            )

    def test_subject_requires_both_cluster_and_workspace(self) -> None:
        """A bare UUID is not a sufficient subject — that is the forgeable shape."""
        with pytest.raises(ContractViolation, match="workspace"):
            ClusterRef(cluster_id="c1", workspace="")
        with pytest.raises(ContractViolation, match="cluster_id"):
            ClusterRef(cluster_id="", workspace=W1)

    def test_observation_is_immutable(self, w1_observation) -> None:
        """A receiver cannot mutate a payload after validating it.

        Otherwise what was validated is not what gets stored.
        """
        with pytest.raises(dataclasses.FrozenInstanceError):
            w1_observation.reporter = "someone-else"  # type: ignore[misc]


class TestBudgetObservation:
    """Budget usage reports observed spend and confers no authority."""

    def _usage(self, **overrides) -> BudgetUsage:
        kwargs = {
            "workspace": W1,
            "window_start": OBSERVED_AT,
            "window_end": OBSERVED_AT + timedelta(hours=1),
            "observed_spend_usd": 12.5,
        }
        kwargs.update(overrides)
        return BudgetUsage(**kwargs)  # type: ignore[arg-type]

    def test_budget_observation_round_trips_to_wire(self) -> None:
        observation = Observation(
            kind="budget_usage",
            subject=ClusterRef(cluster_id="c1", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="cost-monitor",
            budget=self._usage(),
        )
        wire = observation.to_wire()
        assert wire["kind"] == "budget_usage"
        assert wire["budget"]["observed_spend_usd"] == 12.5
        assert wire["budget"]["currency"] == "USD"

    def test_budget_payload_has_no_enforcement_field(self) -> None:
        """M6 stays out of scope, structurally.

        No `budget_exceeded`, `limit` or `enforce` field exists to be read as a
        local verdict. This asserts the absence, because the absence is the
        design decision: enforcement is B's admission-time concern, and a
        verdict field here would be the first step to a local one.
        """
        wire = self._usage()
        forbidden = {"budget_exceeded", "enforce", "limit", "quota", "blocked"}
        assert forbidden.isdisjoint(vars(wire).keys())

    def test_blank_budget_workspace_is_refused(self) -> None:
        """Spend has to be attributable to a workspace to be usable at all.

        A blank workspace here would also slip past scoping's subject check by
        never being compared to anything, so the guard belongs on the payload.
        """
        with pytest.raises(ContractViolation, match="workspace"):
            self._usage(workspace="   ")

    def test_negative_spend_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="negative"):
            self._usage(observed_spend_usd=-1.0)

    def test_inverted_window_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="after its start"):
            self._usage(window_end=OBSERVED_AT - timedelta(hours=1))

    def test_naive_window_bounds_are_refused(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            self._usage(window_start=OBSERVED_AT.replace(tzinfo=None))

    def test_budget_workspace_must_match_the_subject(self) -> None:
        """Two disagreeing workspace assertions in one payload are refused.

        Reconciling them would make whichever field a receiver preferred the
        tamperable one.
        """
        with pytest.raises(ContractViolation, match="does not match"):
            Observation(
                kind="budget_usage",
                subject=ClusterRef(cluster_id="c1", workspace="ws-other"),
                reported_at=OBSERVED_AT,
                reporter="cost-monitor",
                budget=self._usage(),
            )

    def test_budget_observation_requires_budget_usage(self) -> None:
        with pytest.raises(ContractViolation, match="must carry budget usage"):
            Observation(
                kind="budget_usage",
                subject=ClusterRef(cluster_id="c1", workspace=W1),
                reported_at=OBSERVED_AT,
                reporter="cost-monitor",
            )

    def test_kinds_do_not_mix_payloads(self, healthy_check) -> None:
        """A single submission is one kind of observation, not both."""
        with pytest.raises(ContractViolation, match="cannot carry budget usage"):
            Observation(
                kind="fleet_health",
                subject=ClusterRef(cluster_id="c1", workspace=W1),
                reported_at=OBSERVED_AT,
                reporter="platform-monitor",
                checks=(healthy_check,),
                budget=self._usage(),
            )
        with pytest.raises(ContractViolation, match="cannot carry health checks"):
            Observation(
                kind="budget_usage",
                subject=ClusterRef(cluster_id="c1", workspace=W1),
                reported_at=OBSERVED_AT,
                reporter="cost-monitor",
                checks=(healthy_check,),
                budget=self._usage(),
            )


class TestWireShape:
    """The wire keys are the contract, so they are pinned.

    Written by hand in `to_wire()` rather than derived from the dataclass, so a
    field rename cannot become a breaking change without a version bump. These
    assertions are what make that guarantee real.
    """

    def test_required_top_level_keys(self, w1_observation) -> None:
        wire = w1_observation.to_wire()
        assert set(wire) >= {
            VERSION_FIELD,
            "kind",
            "subject",
            "reported_at",
            "reporter",
            "status",
        }

    def test_subject_keys(self, w1_observation) -> None:
        assert set(w1_observation.to_wire()["subject"]) == {"cluster_id", "workspace"}

    def test_check_keys(self, w1_observation) -> None:
        check = w1_observation.to_wire()["checks"][0]
        assert set(check) == {
            "name",
            "status",
            "observed_at",
            "detail",
            "error",
            "reason",
        }

    def test_timestamps_serialize_as_iso8601_with_offset(self, w1_observation) -> None:
        """A receiver in another language parses these, so the offset must be present."""
        assert w1_observation.to_wire()["reported_at"].endswith("+00:00")

    def test_aggregate_status_is_on_the_wire(self, healthy_check) -> None:
        """The reduced status travels with the submission.

        So a receiver storing only the summary cannot disagree with the
        submitter about what the checks reduced to.
        """
        observation = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="c1", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(
                healthy_check,
                CheckResult.not_checked(
                    "eks_reachability", reason="no prober configured"
                ),
            ),
        )
        assert observation.to_wire()["status"] == CheckStatus.NOT_CHECKED.value

    def test_labels_are_omitted_when_empty_and_present_when_set(
        self, healthy_check
    ) -> None:
        bare = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="c1", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(healthy_check,),
        )
        assert "labels" not in bare.to_wire()

        labelled = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="c1", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(healthy_check,),
            labels=(("region", "us-east-1"),),
        )
        assert labelled.to_wire()["labels"] == {"region": "us-east-1"}
