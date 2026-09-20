"""Offline regressions for U12's R17 baseline capture; fixtures never go live.

Issue #5289, follow-up to #5040 (U12), evaluated by #5067.

Two jobs, mirroring `spike/tests/test_parity_harness.py`. First, prove the
capture's mechanics work: it validates observations, separates the two criteria,
and applies the cleanup and cost rules. Second, and load-bearing, prove that
satisfying all of that offline still yields **no** live evidence and publishes
nothing.

Every observer here is a fake confined to this file. None is in
``LIVE_OBSERVERS``, so every result is ``SOURCE_FIXTURE`` and
``ParityResult.live_verified`` is False throughout -- which is why a fully green
run of this suite leaves both R17 criteria unsatisfied. That is the intended
outcome, not a limitation to work around.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from spike.parity_matrix import (
    Dimension,
    EvidenceKind,
    all_checks,
    check_by_id,
    dimension_by_name,
)
from superplane_acceptance import live_baseline as lb
from superplane_acceptance import live_observer as lo
from superplane_acceptance.cli_delivery import EvidenceError

ENVIRONMENT = "baseline/selected"
REVISION = "abc1234def5678901234567890abcdef12345678"
SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567"
AUTHORIZATION = "wave-6 retained authorization record"
PROVIDER = "nebius"
REGION = "eu-north1"
CLUSTER = "workspace-eks"
CONTROLLER = "superplane-controller"
RUNTIME = "0.12.0"
# The canonical identity, carrying account and region as well as the name. A bare
# name let a lookalike cluster be accepted as the selected one, so the registered
# target records the ARN and the address the capture expects to be talking to, and
# both are compared whole.
CLUSTER_ARN = f"arn:aws:eks:{REGION}:111122223333:cluster/{CLUSTER}"
API_ENDPOINT = f"https://{CLUSTER}.eks.amazonaws.com"
TARGET_METADATA = {
    "provider": PROVIDER,
    "region": REGION,
    "workspace_cluster": CLUSTER,
    "workspace_cluster_arn": CLUSTER_ARN,
    "workspace_api_endpoint": API_ENDPOINT,
    "skypilot_api": "http://skypilot-api.skypilot.svc.cluster.local:46580",
    "skypilot_runtime_version": RUNTIME,
    "controller": CONTROLLER,
}
# Every scenario in U12's inventory. The capture now requires a check's baseline
# scenario to have been selected, so the mechanics tests select all of them and
# the partial-selection behavior gets its own regressions below.
ALL_SCENARIOS = tuple(sorted(lb.KNOWN_SCENARIOS))
DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def registered_target(monkeypatch):
    """Register a target for the mechanics tests only.

    The shipped registry is empty (no baseline environment is reviewed yet), so
    without this every test would stop at the BLOCKED selection check and none of
    the downstream rules would be exercised. Registering it here does NOT make
    anything publishable: LIVE_OBSERVERS stays empty, which the final section
    asserts.
    """
    monkeypatch.setitem(lb.BASELINE_TARGETS, ENVIRONMENT, dict(TARGET_METADATA))


def config(**overrides) -> dict:
    base = {
        "environment": ENVIRONMENT,
        "revision": REVISION,
        "source_revision": SOURCE_REVISION,
        "source_provenance": {
            "claimed": SOURCE_REVISION,
            "observed_in_checkout": SOURCE_REVISION,
            "verified_against_checkout": True,
            "checkout_dirty": False,
        },
        "scenarios": ALL_SCENARIOS,
        "authorization": AUTHORIZATION,
        "window_start": datetime.now(timezone.utc) - timedelta(hours=1),
        "evidence_file": "/unused/offline-baseline.json",
        "target_metadata": dict(TARGET_METADATA),
    }
    base.update(overrides)
    return base


def identity(**overrides) -> lb.ResourceIdentity:
    values = {
        "provider": PROVIDER,
        "provider_resource_id": "i-0baseline",
        # The whole ARN, matching what the observer now records: a fact carrying a
        # bare cluster name no longer identifies the selected cluster.
        "cluster": CLUSTER_ARN,
        "kubernetes_node": "sky-node-1",
        "controller": CONTROLLER,
        "skypilot_cluster": "sky-baseline-1",
    }
    values.update(overrides)
    return lb.ResourceIdentity(**values)


def fact(check_id: str, **overrides) -> lb.ObservedFact:
    values = {
        "check_id": check_id,
        "environment": ENVIRONMENT,
        "revision": REVISION,
        "observed_at": datetime.now(timezone.utc),
        "resource": identity(),
        "outcome": lb.Outcome.SATISFIED,
        "detail": f"observed {check_id}",
        "evidence_reference": "evidence.json.raw/001-observation.json",
        "evidence_sha256": DIGEST,
    }
    values.update(overrides)
    return lb.ObservedFact(**values)


def inventory(**overrides) -> lb.ServingInventory:
    """A serving listing as an authorized client would return it.

    ``authenticated`` defaults true because the interesting cases here are about
    what the criteria model does with a *listing*, not about attestation -- the
    unauthenticated-listing path is regressed by name in
    :func:`test_an_unauthenticated_empty_listing_cannot_establish_serving_absence`
    and end-to-end in the observer suite.
    """
    values = {
        "enumerated_via": "sky serve status from an authorized client",
        "environment": ENVIRONMENT,
        "revision": REVISION,
        "observed_at": datetime.now(timezone.utc),
        "evidence_reference": "evidence.json.raw/002-serving.json",
        "evidence_sha256": DIGEST,
        "authenticated": True,
    }
    values.update(overrides)
    return lb.ServingInventory(**values)


def observed_identity(**overrides) -> lb.ObservedEnvironmentIdentity:
    """Identity as an environment would report it, matching the selection.

    Written out rather than derived from ``TARGET_METADATA`` on purpose: the defect
    the capture now guards against was exactly the expectation being copied into
    the observed record, so the tests that vary one field (below) vary an
    independent value rather than the selection itself.
    """
    values = {
        "provider": PROVIDER,
        "region": REGION,
        "controller": CONTROLLER,
        "runtime_version": RUNTIME,
        "cluster_arn": CLUSTER_ARN,
        "api_endpoint": API_ENDPOINT,
        "observed_at": datetime.now(timezone.utc),
        "evidence_reference": "evidence.json.raw/000-identity.json",
        "evidence_sha256": DIGEST,
    }
    values.update(overrides)
    return lb.ObservedEnvironmentIdentity(**values)


def state_record(kind: str, **overrides) -> lb.ExistingStateRecord:
    values = {
        "kind": kind,
        "enumerated_via": f"read-only enumeration of {kind}",
        "environment": ENVIRONMENT,
        "revision": REVISION,
        "observed_at": datetime.now(timezone.utc),
        "evidence_reference": f"evidence.json.raw/003-{kind}.json",
        "evidence_sha256": DIGEST,
        "handles": ("sky-baseline-1",),
    }
    values.update(overrides)
    return lb.ExistingStateRecord(**values)


def complete_state() -> tuple[lb.ExistingStateRecord, ...]:
    """Every recorded state class enumerated, which the capture now requires."""
    return tuple(state_record(kind) for kind in sorted(lb.KNOWN_STATE_KINDS))


def _facts_for(dimensions: tuple[Dimension, ...]) -> dict[Dimension, tuple]:
    """A complete, well-formed observation for each check of each dimension."""
    observations: dict[Dimension, tuple] = {}
    for dimension in dimensions:
        entries = []
        for check in dimension_by_name(dimension).checks:
            extra: dict[str, object] = {}
            if check.check_id == lb.PROVIDER_ABSENCE_CHECK:
                extra["provider_absence_confirmed"] = True
            if check.check_id == lb.COST_CHECK:
                extra["hourly_cost"] = 98.32
            entries.append(fact(check.check_id, **extra))
        observations[dimension] = tuple(entries)
    return observations


class Observer:
    """A fake observer, explicitly confined to these offline tests.

    Deliberately NOT in ``LIVE_OBSERVERS``. It satisfies ``BaselineObserver``
    structurally, which is exactly the fake whose output must stay
    ``SOURCE_FIXTURE``.
    """

    def __init__(self, services: tuple[str, ...] = ("qwen35-eu",)) -> None:
        self.observations = _facts_for(lb.BASELINE_DIMENSIONS + lb.SERVING_DIMENSIONS)
        self.services = services
        if not services:
            # Serving facts alongside an empty inventory are now contradictory,
            # so an absent-serving fake observes no serving.
            self.observations[Dimension.SERVING_WORKLOAD] = ()
        self.inventory: lb.ServingInventory | None = inventory(services=services)
        self.state = complete_state()
        self.identity: lb.ObservedEnvironmentIdentity | None = observed_identity()
        self.live = False

    def observe(self, dimension: Dimension) -> tuple:
        return self.observations.get(dimension, ())

    def environment_identity(self):
        return self.identity

    def serving_inventory(self) -> lb.ServingInventory | None:
        return self.inventory

    def existing_state(self) -> tuple:
        return self.state

    def transport_is_live(self) -> bool:
        # Even claiming a live transport cannot make a fake publishable: the type
        # is not registered. test_a_structural_fake_claiming_live_is_still_fixture
        # drives exactly that case.
        return self.live


# ---------------------------------------------------------------------------
# Missing inputs and authority are BLOCKED, never skipped and never passed.
# ---------------------------------------------------------------------------


def test_no_input_at_all_is_blocked():
    with pytest.raises(EvidenceError, match="BLOCKED: missing explicit"):
        lb.settings({})


@pytest.mark.parametrize("omitted", lb.INPUTS)
def test_each_missing_input_is_named_and_blocked(omitted, tmp_path):
    environment = complete_environment(tmp_path)
    environment.pop(omitted)
    with pytest.raises(EvidenceError) as error:
        lb.settings(environment)
    assert "BLOCKED" in str(error.value)
    assert omitted in str(error.value)


def executing_revision() -> str:
    """The revision of the checkout running this suite.

    ``settings`` now requires the claimed maintained-source revision to be the one
    actually executing, so a fixed literal would fail everywhere for the right
    reason and hide the rule each test is about. The disagreement case gets its own
    regression below, which claims a different revision deliberately.
    """
    observed, _ = lb._checkout_revision()
    return observed or SOURCE_REVISION


def complete_environment(tmp_path) -> dict:
    return {
        "SUPERPLANE_LIVE_BASELINE_ENVIRONMENT": ENVIRONMENT,
        "SUPERPLANE_LIVE_BASELINE_REVISION": REVISION,
        "SUPERPLANE_LIVE_BASELINE_SOURCE_REVISION": executing_revision(),
        "SUPERPLANE_LIVE_BASELINE_SCENARIOS": ",".join(ALL_SCENARIOS),
        "SUPERPLANE_LIVE_BASELINE_AUTHORIZATION": AUTHORIZATION,
        "SUPERPLANE_LIVE_BASELINE_WINDOW_START": (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(),
        "SUPERPLANE_LIVE_BASELINE_EVIDENCE_FILE": str(tmp_path / "evidence.json"),
        # The suite runs from this checkout, which has the working changes a
        # development run always has. Verifying the claimed revision against the
        # checkout is the point of the provenance check; acknowledging the dirty
        # tree here is what lets these tests reach the rules they are about.
        lb.ALLOW_DIRTY_VARIABLE: "true",
    }


def test_unreviewed_target_is_blocked(tmp_path, monkeypatch):
    monkeypatch.delitem(lb.BASELINE_TARGETS, ENVIRONMENT)
    with pytest.raises(EvidenceError, match="no reviewed baseline environment"):
        lb.settings(complete_environment(tmp_path))


def test_shipped_registry_is_empty_so_no_target_is_assumed():
    """The selected environment is the supervisor's decision, not a default."""
    module = Path(lb.__file__).read_text()
    assert "BASELINE_TARGETS: dict[str, dict[str, str]] = {}" in module
    assert "LIVE_OBSERVERS: tuple[type, ...] = ()" in module


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "  ",
        "short",
        "not-alnum-xyz",
        # An abbreviation, which reads as a commit but cannot be compared whole
        # against the full head_sha an evidence producer reports. Refused at the
        # input rather than prefix-matched later, because a prefix comparison
        # admits a different commit that happens to share it.
        REVISION[:12],
        # Full length but not a commit at all, and an upper-case rendering of a
        # real one: both would fail an exact comparison downstream, so they are
        # named here instead of surfacing as a mismatch against genuine evidence.
        "z" * 40,
        REVISION.upper(),
    ],
)
def test_missing_or_malformed_revision_is_blocked(revision, tmp_path):
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_BASELINE_REVISION"] = revision
    # Case-insensitive: a blank value is refused as a missing input, naming the
    # variable, while a malformed one is refused by the revision rule itself.
    with pytest.raises(EvidenceError, match="(?i)revision"):
        lb.settings(environment)


def test_missing_authorization_reference_is_blocked(tmp_path):
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_BASELINE_AUTHORIZATION"] = "adhoc"
    with pytest.raises(EvidenceError, match="authorization"):
        lb.settings(environment)


@pytest.mark.parametrize(
    "window_start", ["", "yesterday", "2026-09-19T12:00:00", "not-a-date"]
)
def test_missing_or_naive_execution_window_is_blocked(window_start, tmp_path):
    """A window with no UTC offset cannot place evidence in time."""
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_BASELINE_WINDOW_START"] = window_start
    with pytest.raises(EvidenceError):
        lb.settings(environment)


def test_execution_window_cannot_open_in_the_future(tmp_path):
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_BASELINE_WINDOW_START"] = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()
    with pytest.raises(EvidenceError, match="cannot open in the future"):
        lb.settings(environment)


def test_a_long_past_execution_window_is_refused(tmp_path):
    """Stops an old session's receipts being replayed as this run's evidence."""
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_BASELINE_WINDOW_START"] = (
        datetime.now(timezone.utc) - timedelta(hours=lb.MAX_WINDOW_HOURS + 1)
    ).isoformat()
    with pytest.raises(EvidenceError, match="rather than replaying an older session"):
        lb.settings(environment)


def test_evidence_from_before_the_authorized_window_is_rejected():
    """An older receipt for the same environment is still out of window."""
    observer = Observer()
    observer.observations[Dimension.STATUS_AND_LOGS] = (
        fact(
            "status.progress-lines-streamed",
            observed_at=datetime.now(timezone.utc) - timedelta(hours=3),
        ),
    )
    with pytest.raises(EvidenceError, match="outside this run's window"):
        lb.capture(
            config(window_start=datetime.now(timezone.utc) - timedelta(hours=1)),
            observer,
        )


def test_completed_operation_evidence_predating_the_capture_is_accepted():
    """Cancellation, teardown and provider-absence evidence necessarily precedes
    the capture; requiring observation after it would make R17's lifecycle
    criteria impossible to satisfy."""
    observer = Observer()
    earlier = datetime.now(timezone.utc) - timedelta(minutes=30)
    observer.observations[Dimension.COST_AND_CLEANUP] = tuple(
        fact(
            check.check_id,
            observed_at=earlier,
            provider_absence_confirmed=(check.check_id == lb.PROVIDER_ABSENCE_CHECK),
            hourly_cost=98.32 if check.check_id == lb.COST_CHECK else None,
        )
        for check in dimension_by_name(Dimension.COST_AND_CLEANUP).checks
    )
    report = lb.capture(
        config(window_start=datetime.now(timezone.utc) - timedelta(hours=2)), observer
    )
    checks = report["criteria"]["U12-L1"]["checks"]
    assert checks[lb.PROVIDER_ABSENCE_CHECK]["passed"] is True
    assert checks[lb.PROVIDER_ABSENCE_CHECK]["provider_side_absence_confirmed"] is True
    # Still not live: the observer is a fake, so nothing is live-verified.
    assert checks[lb.PROVIDER_ABSENCE_CHECK]["live_verified"] is False


def test_unknown_scenario_is_refused(tmp_path):
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_BASELINE_SCENARIOS"] = "invented-scenario"
    with pytest.raises(EvidenceError, match="not in U12's recorded inventory"):
        lb.settings(environment)


def test_existing_evidence_path_is_refused(tmp_path):
    existing = tmp_path / "already-there.json"
    existing.write_text("{}")
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_BASELINE_EVIDENCE_FILE"] = str(existing)
    with pytest.raises(EvidenceError, match="absolute new filename"):
        lb.settings(environment)


def test_settings_retains_no_process_environment(tmp_path):
    """A token in the invoking environment must not reach the config or evidence."""
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_ADP_TOKEN"] = "token-sentinel-not-a-credential"
    resolved = lb.settings(environment)
    assert "SUPERPLANE_LIVE_ADP_TOKEN" not in resolved
    assert "token-sentinel-not-a-credential" not in json.dumps(resolved, default=str)


# ---------------------------------------------------------------------------
# Evidence identity: foreign, stale and off-dimension records are rejected.
# ---------------------------------------------------------------------------


def test_foreign_environment_evidence_is_rejected():
    observer = Observer()
    observer.observations[Dimension.STATUS_AND_LOGS] = (
        fact("status.progress-lines-streamed", environment="other/environment"),
    )
    with pytest.raises(EvidenceError, match="belongs to another environment"):
        lb.capture(config(), observer)


def test_wrong_revision_evidence_is_rejected():
    observer = Observer()
    observer.observations[Dimension.STATUS_AND_LOGS] = (
        fact("status.progress-lines-streamed", revision="9999999aaaa"),
    )
    with pytest.raises(EvidenceError, match="another deployed revision"):
        lb.capture(config(), observer)


def test_stale_observation_outside_the_window_is_rejected():
    observer = Observer()
    observer.observations[Dimension.STATUS_AND_LOGS] = (
        fact(
            "status.progress-lines-streamed",
            observed_at=datetime.now(timezone.utc) - timedelta(days=2),
        ),
    )
    with pytest.raises(EvidenceError, match="outside this run's window"):
        lb.capture(config(), observer)


def test_future_observation_outside_the_window_is_rejected():
    observer = Observer()
    observer.observations[Dimension.STATUS_AND_LOGS] = (
        fact(
            "status.progress-lines-streamed",
            observed_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    with pytest.raises(EvidenceError, match="outside this run's window"):
        lb.capture(config(), observer)


def test_evidence_for_another_dimension_is_rejected():
    observer = Observer()
    observer.observations[Dimension.STATUS_AND_LOGS] = (
        fact("serving.endpoint-reachable"),
    )
    with pytest.raises(EvidenceError, match="is not a check of dimension"):
        lb.capture(config(), observer)


def test_naive_timestamp_cannot_be_recorded():
    with pytest.raises(ValueError, match="timezone-aware"):
        # A deliberately naive instant: the point of this test is that the
        # contract refuses one, so DTZ001's advice does not apply here.
        fact(
            "status.progress-lines-streamed",
            observed_at=datetime(2026, 9, 19),  # noqa: DTZ001
        )


def test_observation_needs_a_concrete_resource_handle():
    with pytest.raises(ValueError, match="needs a provider resource"):
        lb.ResourceIdentity(provider="nebius", cluster="workspace-eks")


def test_fabricated_json_is_not_an_observation():
    """A dict that looks like evidence is refused; only ObservedFact counts."""
    observer = Observer()
    observer.observations[Dimension.STATUS_AND_LOGS] = (
        {"check_id": "status.progress-lines-streamed", "passed": True},
    )
    with pytest.raises(EvidenceError, match="must be ObservedFact records"):
        lb.capture(config(), observer)


# ---------------------------------------------------------------------------
# Incomplete lifecycle coverage, unknown cleanup and unknown cost cannot pass.
# ---------------------------------------------------------------------------


def test_missing_observation_stays_not_run_rather_than_inferred():
    observer = Observer()
    observer.observations[Dimension.NODE_REGISTRATION] = ()
    report = lb.capture(config(), observer)
    checks = report["criteria"]["U12-L1"]["checks"]
    absent = checks["node.join-produces-ready-node"]
    assert absent["evidence"] == EvidenceKind.NOT_RUN.value
    assert absent["passed"] is False
    assert (
        "node.join-produces-ready-node"
        in (report["criteria"]["U12-L1"]["outstanding_checks"])
    )


def test_purge_success_without_provider_confirmation_fails_cleanup():
    """A successful Down/purge drops local state regardless of provider outcome."""
    observer = Observer()
    observer.observations[Dimension.COST_AND_CLEANUP] = tuple(
        fact(
            check.check_id,
            detail="sky down --purge returned success",
            provider_absence_confirmed=False,
            hourly_cost=98.32 if check.check_id == lb.COST_CHECK else None,
        )
        for check in dimension_by_name(Dimension.COST_AND_CLEANUP).checks
    )
    report = lb.capture(config(), observer)
    cleanup = report["criteria"]["U12-L1"]["checks"][lb.PROVIDER_ABSENCE_CHECK]
    assert cleanup["passed"] is False
    assert "did not independently confirm absence" in cleanup["detail"]


def test_unknown_cost_is_not_zero_spend():
    observer = Observer()
    observer.observations[Dimension.COST_AND_CLEANUP] = tuple(
        fact(
            check.check_id,
            hourly_cost=None,
            provider_absence_confirmed=(check.check_id == lb.PROVIDER_ABSENCE_CHECK),
        )
        for check in dimension_by_name(Dimension.COST_AND_CLEANUP).checks
    )
    report = lb.capture(config(), observer)
    cost = report["criteria"]["U12-L1"]["checks"][lb.COST_CHECK]
    assert cost["passed"] is False
    assert "unknown cost is not zero spend" in cost["detail"]


def test_missing_existing_state_inventory_is_blocked():
    observer = Observer()
    observer.state = ()
    with pytest.raises(EvidenceError, match="live clusters, nodes, services"):
        lb.capture(config(), observer)


def test_existing_state_decisions_stay_undecided_here():
    """This capture records U19's inputs; it does not make U19's decision."""
    report = lb.capture(config(), Observer())
    decisions = [entry["decision"] for entry in report["existing_state_for_u19"]]
    assert decisions == ["undecided"] * len(lb.KNOWN_STATE_KINDS)


def test_unknown_existing_state_class_is_refused():
    with pytest.raises(ValueError, match="not a recorded existing-state class"):
        state_record("invented_resource_class")


# ---------------------------------------------------------------------------
# Serving is a separate criterion. Batch success cannot stand in for it.
# ---------------------------------------------------------------------------


def test_batch_results_do_not_satisfy_the_serving_criterion():
    observer = Observer()
    # A complete batch/baseline observation, and no serving observation at all.
    observer.observations[Dimension.SERVING_WORKLOAD] = ()
    report = lb.capture(config(), observer)
    serving = report["criteria"]["U12-L2"]
    assert serving["satisfied"] is False
    serving_ids = {
        check.check_id for check in dimension_by_name(Dimension.SERVING_WORKLOAD).checks
    }
    assert serving_ids <= set(serving["outstanding_checks"])
    assert all(
        serving["checks"][check_id]["evidence"] == EvidenceKind.NOT_RUN.value
        for check_id in serving_ids
    )


def test_the_two_criteria_never_share_checks():
    baseline = report_checks("U12-L1")
    serving = report_checks("U12-L2")
    assert baseline.isdisjoint(serving)
    assert serving == {
        check.check_id for check in dimension_by_name(Dimension.SERVING_WORKLOAD).checks
    }


def report_checks(criterion: str) -> set[str]:
    report = lb.capture(config(), Observer())
    return set(report["criteria"][criterion]["checks"])


def test_every_parity_check_is_covered_by_exactly_one_criterion():
    """No dimension is silently dropped from the capture."""
    covered = report_checks("U12-L1") | report_checks("U12-L2")
    assert covered == {check.check_id for check in all_checks()}


def test_serving_absence_needs_observed_inventory_not_an_empty_selection():
    observer = Observer()
    observer.inventory = None
    with pytest.raises(EvidenceError, match="observed baseline inventory evidence"):
        lb.capture(config(), observer)


def test_serving_inventory_from_another_environment_is_rejected():
    observer = Observer()
    observer.inventory = inventory(environment="other/environment")
    with pytest.raises(EvidenceError, match="another environment or revision"):
        lb.capture(config(), observer)


def test_observed_absence_of_serving_is_recorded_explicitly():
    report = lb.capture(config(), Observer(services=()))
    inventory = report["serving_inventory"]
    assert inventory["serving_present_in_baseline"] is False
    assert inventory["enumerated_via"]
    # Absence is recorded, and it still does not make the criterion satisfied
    # offline -- the serving checks remain outstanding.
    assert report["criteria"]["U12-L2"]["satisfied"] is False


def test_present_serving_is_recorded_with_its_services():
    report = lb.capture(config(), Observer(services=("qwen35-eu",)))
    assert report["serving_inventory"]["serving_present_in_baseline"] is True
    assert report["serving_inventory"]["services"] == ["qwen35-eu"]


def test_an_unauthenticated_empty_listing_cannot_establish_serving_absence():
    """U12-201. Absence is the claim an unvouched listing most wants to make.

    Serving has no controller-side inventory for the capture to observe
    independently, so the listing is the only authority on which services exist.
    That makes "the listing found none" un-cross-checkable, and therefore the one
    place an unauthenticated file must not be allowed to speak: a forged empty
    listing would otherwise establish that this baseline runs no service, which is
    one of the two criteria the issue requires be evidenced separately.

    Blocked rather than recorded-as-absent, and rather than skipped.
    """
    subject = Observer(services=())
    subject.inventory = inventory(services=(), authenticated=False)
    with pytest.raises(EvidenceError, match="BLOCKED: the serving inventory is unauth"):
        lb.capture(config(), subject)


def test_an_unauthenticated_listing_naming_services_still_records_them():
    """The converse, so the gate is about absence and not about listings at all.

    A listing naming a service is corroborated by the service being there to find;
    an empty one is corroborated by nothing. Blocking both would make the repair a
    blanket refusal rather than a check, so presence is still recorded while the
    report carries the authentication status for the reader.
    """
    subject = Observer(services=("qwen35-eu",))
    subject.inventory = inventory(services=("qwen35-eu",), authenticated=False)
    report = lb.capture(config(), subject)
    assert report["serving_inventory"]["serving_present_in_baseline"] is True
    assert report["criteria"]["U12-L2"]["satisfied"] is False


# ---------------------------------------------------------------------------
# The load-bearing part: offline mechanics can never become live evidence.
# ---------------------------------------------------------------------------


def test_a_fully_observed_offline_run_yields_no_live_evidence():
    """Every mechanic satisfied, and still zero live-verified checks."""
    report = lb.capture(config(), Observer(services=("qwen35-eu",)))
    assert report["evidence_kind"] == "offline-fixture"
    for criterion in report["criteria"].values():
        assert criterion["satisfied"] is False
        assert criterion["outstanding_checks"]
        for check in criterion["checks"].values():
            assert check["live_verified"] is False
            assert check["evidence"] != EvidenceKind.LIVE_CAPTURE.value
    assert set(lb.unsatisfied_criteria(report)) == {"U12-L1", "U12-L2"}


def test_a_structural_fake_is_not_a_live_observer():
    """Satisfying the Protocol must not confer publishability."""
    observer = Observer()
    assert isinstance(observer, lb.BaselineObserver)
    assert type(observer) not in lb.LIVE_OBSERVERS
    assert lb.capture(config(), observer)["evidence_kind"] == "offline-fixture"


def test_fixture_evidence_cannot_be_published(tmp_path):
    report = lb.capture(config(), Observer())
    destination = tmp_path / "must-not-exist.json"
    with pytest.raises(EvidenceError, match="cannot be published as a live"):
        lb.publish(config(evidence_file=str(destination)), report)
    assert not destination.exists()


def test_publishing_refuses_to_overwrite_an_existing_record(tmp_path):
    destination = tmp_path / "already.json"
    destination.write_text('{"evidence_kind": "live"}\n')
    report = dict(lb.capture(config(), Observer()), evidence_kind="live")
    with pytest.raises(EvidenceError, match="could not be published"):
        lb.publish(config(evidence_file=str(destination)), report)
    assert destination.read_text() == '{"evidence_kind": "live"}\n'


def test_published_record_is_private_and_written_once(tmp_path):
    """The publish mechanics, driven with a hand-marked record.

    This is the only place a 'live' label is constructed, and it is constructed
    by this test rather than reachable from any capture run -- which is the
    property the previous tests assert.
    """
    destination = tmp_path / "published.json"
    report = dict(lb.capture(config(), Observer()), evidence_kind="live")
    lb.publish(config(evidence_file=str(destination)), report)
    assert json.loads(destination.read_text())["evidence_kind"] == "live"
    assert destination.stat().st_mode & 0o777 == 0o600
    # No temporary evidence is left behind for a later reader to mistake.
    assert [p.name for p in tmp_path.iterdir()] == ["published.json"]


def test_no_temporary_file_survives_a_failed_publish(tmp_path):
    report = lb.capture(config(), Observer())
    with pytest.raises(EvidenceError):
        lb.publish(config(evidence_file=str(tmp_path / "out.json")), report)
    assert list(tmp_path.iterdir()) == []


def test_run_live_is_blocked_on_a_named_missing_input(tmp_path, monkeypatch):
    """The live path stops on a specific absent input, not on absent code.

    The reviewed observer ships, so the failure is now a named missing access
    input rather than "no observer is registered". It still fails BLOCKED and
    still publishes nothing.
    """
    for name in (
        lo.SKYPILOT_TOKEN_VARIABLE,
        lo.KUBECONFIG_VARIABLE,
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(EvidenceError) as error:
        lb.run_live(complete_environment(tmp_path))
    message = str(error.value)
    assert "BLOCKED" in message
    assert lo.KUBECONFIG_VARIABLE in message or lo.SKYPILOT_TOKEN_VARIABLE in message
    assert not (tmp_path / "evidence.json").exists()


def test_the_observer_contract_exposes_no_mutating_operation():
    """There is no code path by which a capture creates, changes or bills anything."""
    forbidden = (
        "launch",
        "stop",
        "down",
        "purge",
        "delete",
        "terminate",
        "drain",
        "apply",
        "create",
        "scale",
        "exec",
    )
    methods = [name for name in dir(lb.BaselineObserver) if not name.startswith("_")]
    assert methods
    for name in methods:
        assert not any(word in name.lower() for word in forbidden), name


def test_no_live_evidence_claim_appears_in_a_blocked_report():
    """Nothing in an offline record reads as an observed live pass."""
    report = lb.capture(config(), Observer())
    serialized = json.dumps(report)
    assert '"live_verified": true' not in serialized
    assert EvidenceKind.LIVE_CAPTURE.value not in serialized


# ---------------------------------------------------------------------------
# The documented command behaves as documented.
# ---------------------------------------------------------------------------


def test_explicit_live_command_without_inputs_fails_instead_of_skipping():
    """The #5067 command must fail BLOCKED, not report a skipped criterion."""
    module = Path(__file__).parent / "acceptance/test_u12_live.py"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SUPERPLANE_LIVE_")
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(module), "-q", "-p", "no:cacheprovider"],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode != 0, result.stdout
    assert "BLOCKED" in result.stdout
    assert " skipped" not in result.stdout
    assert "1 failed" in result.stdout


def test_every_inventory_scenario_has_expected_evidence_documented():
    """#5067's evaluator gets a scenario-by-scenario mapping, with no gaps."""
    assert lb.unmapped_scenarios() == ()
    for scenario_id, evidence in lb.EXPECTED_EVIDENCE.items():
        assert evidence and all(item.strip() for item in evidence), scenario_id


def test_serving_scenario_evidence_covers_all_four_serving_requirements():
    evidence = " ".join(lb.EXPECTED_EVIDENCE[lb.SERVING_SCENARIO]).lower()
    for requirement in ("inventory", "unauthenticated", "controller", "replica"):
        assert requirement in evidence


# ---------------------------------------------------------------------------
# U12-002. An observation must state its outcome, and only a satisfied outcome
# passes. The first review drove the capture with deliberately negative
# observations and it reported both criteria satisfied; these are those drives.
# ---------------------------------------------------------------------------


def test_an_observation_without_an_outcome_cannot_be_constructed():
    """Recording that someone looked is not recording what they saw."""
    with pytest.raises(TypeError):
        lb.ObservedFact(
            check_id="node.join-produces-ready-node",
            environment=ENVIRONMENT,
            revision=REVISION,
            observed_at=datetime.now(timezone.utc),
            resource=identity(),
            detail="observed",
            evidence_reference="evidence.json.raw/001.json",
            evidence_sha256=DIGEST,
        )


def test_a_non_outcome_value_is_refused():
    with pytest.raises(TypeError, match="outcome must be an Outcome"):
        fact("node.join-produces-ready-node", outcome="satisfied")


NEGATIVE_OBSERVATIONS = (
    (
        Dimension.NODE_REGISTRATION,
        "node.join-produces-ready-node",
        "node condition Ready=False after the join window",
    ),
    (
        Dimension.BATCH_WORKLOAD,
        "batch.gpu-workload-schedules",
        "pod remained Pending: no node satisfied the GPU request",
    ),
    (
        Dimension.SERVING_WORKLOAD,
        "serving.unauthenticated-request-refused",
        "unauthenticated request was answered with HTTP 200",
    ),
    (
        Dimension.COST_AND_CLEANUP,
        lb.PROVIDER_ABSENCE_CHECK,
        "provider still reports the instance running after teardown",
    ),
    (
        Dimension.STOP_CANCELLATION,
        "cancel.in-flight-launch-stops",
        "launch continued to completion after cancellation",
    ),
)


@pytest.mark.parametrize(("dimension", "check_id", "detail"), NEGATIVE_OBSERVATIONS)
def test_a_refuted_observation_fails_its_check(dimension, check_id, detail):
    """The behaviour was observed NOT to happen; that is not a pass."""
    observer = Observer()
    observer.observations[dimension] = tuple(
        fact(
            check.check_id,
            outcome=(
                lb.Outcome.REFUTED
                if check.check_id == check_id
                else lb.Outcome.SATISFIED
            ),
            detail=detail
            if check.check_id == check_id
            else f"observed {check.check_id}",
            provider_absence_confirmed=(check.check_id == lb.PROVIDER_ABSENCE_CHECK),
            hourly_cost=98.32 if check.check_id == lb.COST_CHECK else None,
        )
        for check in dimension_by_name(dimension).checks
    )
    report = lb.capture(config(), observer)
    criterion = "U12-L2" if dimension is Dimension.SERVING_WORKLOAD else "U12-L1"
    record = report["criteria"][criterion]["checks"][check_id]
    assert record["passed"] is False
    assert "refuted" in record["detail"].lower()
    assert check_id in report["criteria"][criterion]["outstanding_checks"]
    assert report["criteria"][criterion]["satisfied"] is False


@pytest.mark.parametrize(("dimension", "check_id", "detail"), NEGATIVE_OBSERVATIONS)
def test_a_refutation_stays_refuted_rather_than_becoming_not_run(
    dimension, check_id, detail
):
    """'Observed not to happen' and 'nobody looked' are different findings."""
    observer = Observer()
    observer.observations[dimension] = (
        fact(check_id, outcome=lb.Outcome.REFUTED, detail=detail),
    )
    report = lb.capture(config(), observer)
    criterion = "U12-L2" if dimension is Dimension.SERVING_WORKLOAD else "U12-L1"
    record = report["criteria"][criterion]["checks"][check_id]
    assert record["evidence"] != EvidenceKind.NOT_RUN.value
    assert [o["outcome"] for o in record["observations"]] == ["refuted"]


def test_an_indeterminate_observation_does_not_satisfy_its_check():
    observer = Observer()
    observer.observations[Dimension.STATUS_AND_LOGS] = (
        fact(
            "status.progress-lines-streamed",
            outcome=lb.Outcome.INDETERMINATE,
            detail="the retained stream for this launch is unavailable",
        ),
    )
    report = lb.capture(config(), observer)
    record = report["criteria"]["U12-L1"]["checks"]["status.progress-lines-streamed"]
    assert record["passed"] is False
    assert "could not establish" in record["detail"]


def test_contradictory_observations_for_one_check_fail_rather_than_pass():
    """A refutation outranks a neighbouring success for the same check."""
    observer = Observer()
    observer.observations[Dimension.NODE_REGISTRATION] = (
        fact("node.join-produces-ready-node", detail="node reached Ready=True"),
        fact(
            "node.join-produces-ready-node",
            outcome=lb.Outcome.REFUTED,
            detail="a second read found Ready=False",
        ),
    )
    report = lb.capture(config(), observer)
    record = report["criteria"]["U12-L1"]["checks"]["node.join-produces-ready-node"]
    assert record["passed"] is False
    assert {o["outcome"] for o in record["observations"]} == {"satisfied", "refuted"}


def test_a_negative_detail_can_no_longer_ride_a_satisfied_outcome():
    """The reviewers' exact drive: a failure narrated in free-form detail.

    Detail is prose and cannot be the gate; the outcome is. A caller claiming
    SATISFIED while describing a failure is a lying observer, not a validation
    hole -- so this asserts the check passes only because the outcome says so,
    and that the contradictory text is retained verbatim for the reader.
    """
    observer = Observer()
    observer.observations[Dimension.NODE_REGISTRATION] = tuple(
        fact(check.check_id, detail="OBSERVED FAILURE: assertion was false")
        for check in dimension_by_name(Dimension.NODE_REGISTRATION).checks
    )
    report = lb.capture(config(), observer)
    record = report["criteria"]["U12-L1"]["checks"]["node.join-produces-ready-node"]
    assert (
        record["observations"][0]["detail"] == "OBSERVED FAILURE: assertion was false"
    )
    assert record["observations"][0]["outcome"] == "satisfied"


# ---------------------------------------------------------------------------
# U12-004. Evidence must be about the selected machine, and the record must
# retain which machine that was.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "aws"),
        ("cluster", "someone-elses-eks"),
        ("controller", "other-controller"),
    ],
)
def test_evidence_about_a_foreign_resource_is_rejected(field, value):
    """The reviewers' reproduction: right labels, wrong machine."""
    observer = Observer()
    observer.observations[Dimension.NODE_REGISTRATION] = (
        fact(
            "node.join-produces-ready-node",
            resource=identity(**{field: value, "provider_resource_id": "FOREIGN-123"}),
        ),
    )
    with pytest.raises(EvidenceError, match=f"observed {field} .* is not the selected"):
        lb.capture(config(), observer)


def test_one_handle_cannot_denote_two_different_machines():
    observer = Observer()
    observer.observations[Dimension.NODE_REGISTRATION] = (
        fact("node.join-produces-ready-node", resource=identity()),
        fact(
            "node.status-links-to-k8s-node",
            resource=identity(kubernetes_node="a-different-node"),
        ),
    )
    with pytest.raises(
        EvidenceError, match="observed with a different kubernetes_node"
    ):
        lb.capture(config(), observer)


def test_lifecycle_evidence_must_concern_a_provisioned_resource():
    """A machine cleaned up but never seen provisioned has an unrecorded origin."""
    observer = Observer()
    observer.observations[Dimension.COST_AND_CLEANUP] = tuple(
        fact(
            check.check_id,
            resource=identity(
                provider_resource_id="i-0never-provisioned",
                kubernetes_node="",
                skypilot_cluster="",
            ),
            provider_absence_confirmed=(check.check_id == lb.PROVIDER_ABSENCE_CHECK),
            hourly_cost=98.32 if check.check_id == lb.COST_CHECK else None,
        )
        for check in dimension_by_name(Dimension.COST_AND_CLEANUP).checks
    )
    with pytest.raises(EvidenceError, match="never observed being provisioned"):
        lb.capture(config(), observer)


def test_each_check_retains_an_auditable_receipt():
    """Identity, instant, provenance and evidence location survive into the record."""
    report = lb.capture(config(), Observer())
    record = report["criteria"]["U12-L1"]["checks"]["node.join-produces-ready-node"]
    assert len(record["observations"]) == 1
    receipt = record["observations"][0]
    assert receipt["resource"]["provider_resource_id"] == "i-0baseline"
    assert receipt["resource"]["kubernetes_node"] == "sky-node-1"
    assert receipt["deployed_revision"] == REVISION
    assert receipt["evidence_sha256"] == DIGEST
    assert receipt["evidence_reference"].endswith(".json")
    assert datetime.fromisoformat(receipt["observed_at"]).tzinfo is not None
    assert record["baseline_scenarios"] == list(
        check_by_id("node.join-produces-ready-node").baseline_scenarios
    )


def test_observed_resources_are_listed_for_u19():
    report = lb.capture(config(), Observer())
    assert {"provider_resource_id": "i-0baseline"}.items() <= report[
        "observed_resources"
    ][0].items()


def test_provenance_records_both_revisions():
    """The deployed revision and the source revision are different facts."""
    report = lb.capture(config(), Observer())
    assert report["target"]["deployed_revision"] == REVISION
    assert report["target"]["maintained_source_revision"] == SOURCE_REVISION
    assert report["target"]["verifier_sha256"]


def test_an_observation_needs_a_retained_evidence_digest():
    with pytest.raises(EvidenceError, match="must be the sha256"):
        fact("node.join-produces-ready-node", evidence_sha256="not-a-digest")


# ---------------------------------------------------------------------------
# U12-003. Claimed scenario coverage is enforced, and serving branches on the
# observed inventory.
# ---------------------------------------------------------------------------


def test_an_unselected_scenarios_check_cannot_be_satisfied():
    """One selected scenario must not satisfy every other scenario's checks."""
    observer = Observer()
    selected = ("eks-join-via-onboarding-scripts",)
    observer.observations = {
        dimension: tuple(
            entry
            for entry in facts
            if selected[0] in check_by_id(entry.check_id).baseline_scenarios
        )
        for dimension, facts in observer.observations.items()
    }
    observer.inventory = inventory(services=())
    report = lb.capture(config(scenarios=selected), observer)
    baseline = report["criteria"]["U12-L1"]
    assert baseline["coverage_complete"] is False
    assert baseline["satisfied"] is False
    # A check of a scenario nobody selected is not_run, not passed.
    unselected = baseline["checks"]["cost.hourly-and-daily-aggregation"]
    assert unselected["scenario_selected"] is False
    assert unselected["passed"] is False
    assert unselected["evidence"] == EvidenceKind.NOT_RUN.value
    assert "cost.hourly-and-daily-aggregation" in baseline["unselected_checks"]


def test_evidence_for_an_unselected_scenario_is_refused():
    observer = Observer()
    with pytest.raises(EvidenceError, match="was not selected for observation"):
        lb.capture(config(scenarios=("eks-join-via-onboarding-scripts",)), observer)


def test_scenario_coverage_is_reported_per_scenario():
    report = lb.capture(config(), Observer())
    coverage = report["scenario_coverage"]
    assert set(coverage) == lb.KNOWN_SCENARIOS
    for scenario, record in coverage.items():
        assert record["selected"] is True, scenario
        assert record["checks"]


def test_serving_facts_contradicting_an_empty_inventory_are_refused():
    """An empty inventory and serving results cannot both be true."""
    observer = Observer(services=())
    observer.observations[Dimension.SERVING_WORKLOAD] = tuple(
        fact(check.check_id)
        for check in dimension_by_name(Dimension.SERVING_WORKLOAD).checks
    )
    with pytest.raises(EvidenceError, match="inventory enumerated no service"):
        lb.capture(config(), observer)


def test_absent_serving_is_not_satisfied_when_serving_was_never_selected():
    """'We never looked at serving' must not pass as 'serving is absent'."""
    observer = Observer(services=())
    selected = tuple(s for s in ALL_SCENARIOS if s != lb.SERVING_SCENARIO)
    observer.observations = {
        dimension: tuple(
            entry
            for entry in facts
            if any(
                scenario in selected
                for scenario in check_by_id(entry.check_id).baseline_scenarios
            )
        )
        for dimension, facts in observer.observations.items()
    }
    report = lb.capture(config(scenarios=selected), observer)
    serving = report["criteria"]["U12-L2"]
    assert serving["serving_scenario_selected"] is False
    assert serving["satisfied"] is False


def test_the_serving_criterion_records_which_branch_it_took():
    present = lb.capture(config(), Observer(services=("qwen35-eu",)))["criteria"][
        "U12-L2"
    ]
    assert present["basis"] == "serving_lifecycle_observed"
    absent = lb.capture(config(), Observer(services=()))["criteria"]["U12-L2"]
    assert absent["basis"] == "serving_absent_by_observed_inventory"


# ---------------------------------------------------------------------------
# U12-005. Every existing-state class is enumerated explicitly, and every U19
# decision is left open.
# ---------------------------------------------------------------------------


def test_a_partial_existing_state_inventory_is_blocked():
    """Omitting a class is how a cutover meets a cluster it never planned for."""
    observer = Observer()
    observer.state = tuple(
        record for record in complete_state() if record.kind != "skyserve_services"
    )
    with pytest.raises(EvidenceError, match="omitted: skyserve_services"):
        lb.capture(config(), observer)


def test_every_state_class_is_present_in_the_record():
    report = lb.capture(config(), Observer())
    kinds = {entry["kind"] for entry in report["existing_state_for_u19"]}
    assert kinds == lb.KNOWN_STATE_KINDS


def test_a_duplicated_state_class_is_refused():
    observer = Observer()
    observer.state = (*complete_state(), state_record("skypilot_clusters"))
    with pytest.raises(EvidenceError, match="enumerated twice"):
        lb.capture(config(), observer)


def test_a_state_record_from_another_environment_is_refused():
    observer = Observer()
    observer.state = tuple(
        state_record(record.kind, environment="other/environment")
        if record.kind == "joined_eks_hybrid_nodes"
        else record
        for record in complete_state()
    )
    with pytest.raises(EvidenceError, match="another environment or revision"):
        lb.capture(config(), observer)


@pytest.mark.parametrize("decision", ["adopt", "drain_relaunch", "no_existing_state"])
def test_an_already_decided_state_record_is_refused(decision):
    """U19 owns the handover decision; this capture only records its inputs."""
    observer = Observer()
    observer.state = tuple(
        state_record(record.kind, decision=decision)
        if record.kind == "skypilot_clusters"
        else record
        for record in complete_state()
    )
    with pytest.raises(EvidenceError, match="must leave U19's handover decision"):
        lb.capture(config(), observer)


def test_an_empty_enumeration_must_be_explicitly_verified():
    """No handles is a claim to verify, not a silence."""
    with pytest.raises(ValueError, match="verified empty result"):
        state_record("skyserve_services", handles=())
    record = state_record("skyserve_services", handles=(), empty_verified=True)
    assert record.empty_verified is True


@pytest.mark.parametrize(
    "combination",
    [
        {"handles": ("a",), "empty_verified": True},
        {"handles": ("a",), "unresolved_reason": "no correlation source"},
        {"handles": (), "empty_verified": True, "unresolved_reason": "no source"},
    ],
    ids=["handles-and-empty", "handles-and-unresolved", "empty-and-unresolved"],
)
def test_a_class_records_exactly_one_of_the_three_results(combination):
    """Enumerated, verified-empty and unresolved are three distinct claims.

    The third was added because "no services found" and "nothing authoritative to
    ask" had been collapsed into one, which is how a live service became invisible.
    Claiming two of them at once is a contradiction, not a richer record.
    """
    with pytest.raises(ValueError, match="record exactly one of"):
        state_record("skypilot_clusters", **combination)


def test_a_verified_empty_class_is_recorded_as_such():
    observer = Observer()
    observer.state = tuple(
        state_record(record.kind, handles=(), empty_verified=True)
        if record.kind == "skyserve_services"
        else record
        for record in complete_state()
    )
    report = lb.capture(config(), observer)
    entry = next(
        e for e in report["existing_state_for_u19"] if e["kind"] == "skyserve_services"
    )
    assert entry["verified_empty"] is True
    assert entry["handles"] == []


# ---------------------------------------------------------------------------
# U12-006. Nothing credential-shaped or body-shaped reaches the artifact, on
# any observer- or operator-controlled field.
# ---------------------------------------------------------------------------

UNSAFE_VALUES = (
    "Bearer abcdefghijklmnopqrstuvwxyz012345",
    "-----BEGIN RSA PRIVATE KEY-----",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "ghp_0123456789abcdefghijklmnopqrstuvwx",
    "xoxb-12345-abcdefg",
    "password=hunter2",
    "activation_code=abc123",
    '{"status": "UP", "handle": {"head_ip": "10.0.0.1"}}',
    "<html><body>error</body></html>",
    "HTTP/1.1 200 OK",
    "set-cookie: session=abc",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
)


@pytest.mark.parametrize("value", UNSAFE_VALUES)
def test_an_unsafe_observation_detail_is_refused(value):
    with pytest.raises(EvidenceError):
        fact("node.join-produces-ready-node", detail=value)


@pytest.mark.parametrize("value", UNSAFE_VALUES)
def test_an_unsafe_resource_handle_is_refused(value):
    with pytest.raises(EvidenceError):
        identity(provider_resource_id=value)


@pytest.mark.parametrize("value", UNSAFE_VALUES)
def test_an_unsafe_enumerated_via_is_refused(value):
    with pytest.raises(EvidenceError):
        inventory(enumerated_via=value)


@pytest.mark.parametrize("value", UNSAFE_VALUES)
def test_an_unsafe_service_name_is_refused(value):
    with pytest.raises(EvidenceError):
        inventory(services=(value,))


@pytest.mark.parametrize("value", UNSAFE_VALUES)
def test_an_unsafe_state_handle_is_refused(value):
    with pytest.raises(EvidenceError):
        state_record("skypilot_clusters", handles=(value,))


@pytest.mark.parametrize("value", UNSAFE_VALUES)
def test_an_unsafe_request_reference_is_refused(value):
    with pytest.raises(EvidenceError):
        fact("node.join-produces-ready-node", request_reference=value)


@pytest.mark.parametrize(
    "value",
    [
        "Bearer abcdefghijklmnopqrstuvwxyz012345",
        "ghp_0123456789abcdefghijklmnopqrstuvwx",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "token=abc",
    ],
)
def test_a_credential_pasted_as_the_authorization_reference_is_refused(value, tmp_path):
    """The operator-controlled field, which was previously only length-checked."""
    environment = complete_environment(tmp_path)
    environment["SUPERPLANE_LIVE_BASELINE_AUTHORIZATION"] = value
    with pytest.raises(EvidenceError):
        lb.settings(environment)


@pytest.mark.parametrize(
    "value",
    [
        "A" * 40,
        "ZmFrZWJhc2U2NHBheWxvYWRmYWtlYmFzZTY0cGF5bG9hZA==",
    ],
    ids=["unbroken-run", "base64-blob"],
)
def test_an_opaque_run_pasted_as_a_reference_is_refused(value):
    with pytest.raises(EvidenceError, match="opaque high-entropy run"):
        fact("node.join-produces-ready-node", evidence_reference=value)


def test_a_long_readable_reference_is_not_mistaken_for_a_token():
    """The screen tests opacity, not length.

    Separators break a run. They previously did not -- `-` and `_` were inside the
    character class -- so a readable kebab-case reference that crossed the length
    threshold was refused as though it were a pasted token. The retained-receipt
    references are exactly that shape, since they carry the check id.
    """
    readable = (
        "evidence.json.raw/007-receipt-cleanup-provider-side-absence-verified.body"
    )
    assert len(readable) > 40
    assert (
        fact(
            "node.join-produces-ready-node", evidence_reference=readable
        ).evidence_reference
        == readable
    )


def test_an_unbounded_detail_is_refused():
    with pytest.raises(EvidenceError, match="published limit"):
        fact("node.join-produces-ready-node", detail="a" * (lb.MAX_DETAIL + 1))


def test_a_multiline_detail_is_refused():
    """A captured body arrives as multiple lines; a reference does not."""
    with pytest.raises(EvidenceError, match="control characters"):
        fact("node.join-produces-ready-node", detail="line one\nline two")


def test_no_unsafe_content_reaches_a_published_record(tmp_path):
    report = dict(lb.capture(config(), Observer()), evidence_kind="live")
    destination = tmp_path / "published.json"
    lb.publish(config(evidence_file=str(destination)), report)
    serialized = destination.read_text()
    for marker in ("Bearer ", "BEGIN RSA", "set-cookie", "password="):
        assert marker not in serialized


# ---------------------------------------------------------------------------
# U12-001. The reviewed observer ships, and being it is not enough to publish.
# ---------------------------------------------------------------------------


def test_the_reviewed_observer_is_the_only_publishable_type():
    assert lb.LIVE_OBSERVERS == (lo.LiveBaselineObserver,)


def test_a_structural_fake_claiming_live_is_still_fixture_evidence():
    """An unregistered type cannot promote itself by answering yes."""
    observer = Observer()
    observer.live = True
    assert isinstance(observer, lb.BaselineObserver)
    assert lb.capture(config(), observer)["evidence_kind"] == "offline-fixture"


def test_the_reviewed_observer_satisfies_the_read_only_contract():
    assert isinstance(
        lo.LiveBaselineObserver.__new__(lo.LiveBaselineObserver), lb.BaselineObserver
    )


def test_registering_an_observer_is_idempotent():
    lb.register_live_observer(lo.LiveBaselineObserver)
    assert lb.LIVE_OBSERVERS == (lo.LiveBaselineObserver,)
