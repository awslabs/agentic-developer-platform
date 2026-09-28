"""R10 acceptance 3 — the alert emission path outlives the agent, and is net-new.

Issue #5050 (U5), EPIC #4910.

Two things get asserted here, matching what the story asks for:

  1. the alert is emitted through a sink whose lifetime is INDEPENDENT of the agent
     process, and a sink bound to the agent's lifetime is refused;
  2. the emission path is NET-NEW rather than a log line — asserted against the repository,
     because the claim "we wired up existing eventing" is the failure mode that ships
     nothing observable, and it can only be checked by looking at what exists.

## What these tests do NOT establish

They do not close R10 acceptance 3. Asserting that emission went through an
independent-lifetime sink is not the same as observing an alert arrive with the agent gone;
the sink here is a fake. That criterion (U5-L1) is deferred, gated on a named
account/environment, the deployed sink and a named owner to receive it — all unresolved.
`test_deferred_live_criterion_is_recorded` pins that the deferral stays written down, so it
cannot quietly disappear and leave these offline tests looking like the whole story.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

import pytest
from _hosting_fakes import RAISED_AT, SESSION_ID, Boom, FakeSink
from superplane_hosting import (
    AlertKind,
    AlertRecord,
    AlertSink,
    AlertSinkRejected,
    LifetimeOwner,
    complete_session,
    emit,
    record_alert,
)

pytest_plugins = ("_hosting_fixtures",)

# tests/[0] agent/[1] superplane/[2] domain-apps/[3] modules/[4] root/[5]
_REPO_ROOT = Path(__file__).resolve().parents[5]
_HOSTING_DIR = Path(__file__).resolve().parent.parent / "hosting"


class TestSinkLifetimeIsIndependentOfTheAgent:
    """The property acceptance 3 turns on: delivery does not depend on the agent living."""

    def test_emits_through_an_independent_lifetime_sink(self, budget_alert):
        sink = FakeSink(lifetime_owner_value=LifetimeOwner.INDEPENDENT, durable=True)

        emit(budget_alert, sink)

        assert sink.delivered == [budget_alert]

    def test_refuses_a_sink_owned_by_the_agent_process(self, budget_alert):
        """An alert delivered from inside the agent dies with the agent.

        That is not a hypothetical: the case an operator most needs to hear about is the
        session that died mid-run, which is precisely when in-process delivery never runs.
        """
        sink = FakeSink(lifetime_owner_value=LifetimeOwner.AGENT, durable=True)

        with pytest.raises(AlertSinkRejected, match="die with the agent"):
            emit(budget_alert, sink)

        assert sink.delivered == []

    def test_refuses_a_non_durable_sink(self, budget_alert):
        """A log line is not an alert sink.

        A non-durable sink loses the alert to the same failure that made it worth sending,
        which satisfies "we emit an alert" on paper while delivering nothing.
        """
        sink = FakeSink(lifetime_owner_value=LifetimeOwner.INDEPENDENT, durable=False)

        with pytest.raises(AlertSinkRejected, match="not an alert sink"):
            emit(budget_alert, sink)

        assert sink.delivered == []

    def test_rejection_is_raised_rather_than_degraded(self, budget_alert):
        """No silent fallback to a weaker sink.

        Falling back to a log line is indistinguishable from having no alerting, so the
        boundary refuses loudly instead of quietly downgrading.
        """
        sink = FakeSink(lifetime_owner_value=LifetimeOwner.AGENT, durable=False)

        with pytest.raises(AlertSinkRejected):
            emit(budget_alert, sink)

    def test_the_sink_contract_exposes_its_lifetime_owner(self):
        """`lifetime_owner` is part of the protocol, not incidental metadata.

        `emit` reads it to make its decision, so a sink that cannot answer it is not usable
        here — the runtime-checkable protocol is what makes that a contract.
        """
        assert isinstance(FakeSink(), AlertSink)


class TestAlertRecordsSurviveTheProcess:
    """The agent's contribution is a durable record, not a delivery."""

    def test_both_acceptance_3_kinds_are_representable(self):
        """Overdue cleanup and budget — the two kinds the criterion names."""
        assert {AlertKind.OVERDUE_CLEANUP.value, AlertKind.BUDGET.value} == {
            "overdue_cleanup",
            "budget",
        }

    def test_record_alert_builds_a_correlatable_record(self):
        record = record_alert(
            kind=AlertKind.OVERDUE_CLEANUP,
            session_id=SESSION_ID,
            summary="Allocation cleanup is overdue.",
            raised_at=RAISED_AT,
        )

        assert record.kind is AlertKind.OVERDUE_CLEANUP
        # The session id is what lets an operator correlate the alert with a session that is
        # no longer running — the normal case for this path.
        assert record.session_id == SESSION_ID

    def test_a_record_is_immutable_once_built(self, budget_alert):
        """A record is evidence; it must not be editable after the durable write."""
        with pytest.raises(FrozenInstanceError):
            budget_alert.summary = "changed"

    def test_an_empty_summary_is_refused(self):
        """An alert that tells an operator nothing is not an alert."""
        with pytest.raises(ValueError, match="no summary"):
            record_alert(
                kind=AlertKind.BUDGET,
                session_id=SESSION_ID,
                summary="   ",
                raised_at=RAISED_AT,
            )

    def test_a_naive_timestamp_is_refused(self):
        """An ambiguous timestamp on an alert that outlives its process is a trap.

        Somebody reconstructing a timeline from an alert whose agent is gone has only the
        record to go on, so the instant has to be unambiguous.
        """
        with pytest.raises(ValueError, match="timezone-aware"):
            AlertRecord(
                kind=AlertKind.BUDGET,
                session_id=SESSION_ID,
                summary="Budget threshold reached.",
                raised_at=datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - the point of the test
            )

    def test_the_record_is_durable_before_the_agent_acknowledges_its_work(
        self, outcome_with_alert, store, handoff, inbox, log
    ):
        """The coupling to acceptance 2, from the alert's side.

        The durable alert write precedes the input-message delete, so a crash cannot lose
        the alert while leaving the work marked as handled.
        """
        complete_session(outcome_with_alert, store, handoff, inbox)

        assert log.index("write_alert") < log.index("delete")
        assert store.alerts[0].kind is AlertKind.BUDGET

    def test_an_alert_survives_a_crash_that_prevents_acknowledgement(
        self, outcome_with_alert, store, handoff, inbox
    ):
        """The alert is durable even though the session never completed."""
        handoff.fail = True

        with pytest.raises(Boom):
            complete_session(outcome_with_alert, store, handoff, inbox)

        assert len(store.alerts) == 1, (
            "The alert was lost by a crash before the handoff."
        )
        assert inbox.deleted == []


class TestTheEmissionPathIsNetNew:
    """Asserted against the repository, because "we wired up existing eventing" is the
    claim that under-sizes this work and ships nothing observable.

    These read the tree as text. They are checking a property OF THE REPOSITORY, not of an
    import, and they are the only kind of check that can catch the description drifting away
    from what exists.
    """

    def test_no_production_put_events_caller_exists(self):
        """`put_events`/`putEvents` has zero production callers.

        Occurrences under `.github/scripts/tests/` are assertions that ops dispatch does NOT
        use it, so they are excluded — they are evidence for this claim, not against it.
        """
        hits: list[str] = []
        for path in _REPO_ROOT.rglob("*.py"):
            parts = path.parts
            if "node_modules" in parts or ".git" in parts:
                continue
            relative = path.relative_to(_REPO_ROOT).as_posix()
            if relative.startswith(".github/scripts/tests/"):
                continue
            # This module's own docstrings discuss the absence; they are not callers.
            if relative.startswith("modules/domain-apps/superplane/agent/"):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line in source.splitlines():
                if ".put_events(" in line or ".putEvents(" in line:
                    hits.append(f"{relative}: {line.strip()}")

        assert hits == [], (
            "A production put_events caller now exists, so the 'net-new sink' premise has "
            f"changed and this module's README needs revisiting: {hits}"
        )

    def test_the_hosting_module_does_not_present_a_log_line_as_an_alert(self):
        """No `print`/`logging` call stands in for emission in the hosting package.

        The whole failure mode is an alert that is really a log line, so the module that
        owns the emission path must not contain one masquerading as delivery.
        """
        offenders: list[str] = []
        for path in (_HOSTING_DIR / "superplane_hosting").glob("*.py"):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith(("#", '"')):
                    continue
                if (
                    "print(" in stripped
                    or "logging." in stripped
                    or "logger." in stripped
                ):
                    offenders.append(f"{path.name}:{number}: {stripped}")

        assert offenders == [], f"A log call appears in the emission path: {offenders}"

    def test_the_readme_records_the_sink_as_net_new(self):
        """The README states the premise the tests above check.

        Pinned so the reasoning cannot be deleted while the code stays — a future reader
        needs the "why" as much as the assertions.
        """
        readme = (_HOSTING_DIR / "README.md").read_text(encoding="utf-8")

        assert "net-new" in readme
        assert "zero production callers" in readme.lower()

    def test_deferred_live_criterion_is_recorded(self):
        """U5-L1 stays visible as unresolved.

        Without this, the offline suite above reads as if it closed acceptance 3.
        """
        readme = (_HOSTING_DIR / "README.md").read_text(encoding="utf-8")

        assert "U5-L1" in readme
        assert "unresolved" in readme.lower()
        # No account id or credential label is invented anywhere in this module.
        assert "adp-cred" not in readme

    def test_no_account_identifier_is_hardcoded_in_the_hosting_module(self):
        """The gate on the live criterion is unresolved, so nothing may name an account.

        A 12-digit literal here would be an invented deployment target — the specific thing
        the approved plan says must not be guessed.
        """
        import re

        twelve_digits = re.compile(r"(?<!\d)\d{12}(?!\d)")
        offenders: list[str] = []
        for path in _HOSTING_DIR.rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".md",
                ".yaml",
                ".yml",
                ".tf",
            }:
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if twelve_digits.search(line):
                    offenders.append(f"{path.name}:{number}")

        assert offenders == [], (
            f"An account-shaped literal appears in the hosting module: {offenders}"
        )


@pytest.mark.parametrize(
    "owner,durable",
    [(None, True), ("independent_of_agent", True), (LifetimeOwner.INDEPENDENT, "yes")],
)
def test_unknown_sink_guarantees_are_refused(owner, durable):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from datetime import datetime, timezone
    from superplane_hosting.alerts import (
        AlertKind,
        AlertRecord,
        AlertSinkRejected,
        emit,
    )

    sink = SimpleNamespace(lifetime_owner=owner, is_durable=durable, deliver=Mock())
    record = AlertRecord(
        AlertKind.BUDGET, "session", "budget observation", datetime.now(timezone.utc)
    )
    with pytest.raises(AlertSinkRejected):
        emit(record, sink)
    sink.deliver.assert_not_called()
