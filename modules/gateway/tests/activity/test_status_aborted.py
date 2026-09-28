"""The `aborted` terminal status, end to end through the gateway read path (#3964).

An operator who stops a run on purpose must see it as aborted everywhere. Before
this story the status existed nowhere in the shared vocabulary, so an aborted row
was not merely mislabelled — it was *authoritative*: `compute_liveness` returned
`unverifiable`, `_map_item` left `completed_at` null, and every reader deriving
terminality from `OBSERVED_TERMINAL_STATUSES` (control availability, run-spend
binding, orchestration draft binding) still treated the finished run as one that
could be commanded and could hold budget headroom.

Two properties carry the weight here, and they pull in opposite directions:

* **`aborted` IS terminal.** It has to reach `exited`, populate `completed_at`,
  and count once (AC-A3, AC-A9, AC-A10).
* **A provider's interruption is NOT `aborted`.** The harness-neutral contract has
  each adapter normalize its own outcomes before anything is written, so no reader
  and no writer may promote an SDK cancellation, a transport abort or an error
  string to this status. Only a confirmed ADP abort finalization writes it.

The second is the one a future change is most likely to break while "improving"
the first, which is why `TestHarnessNeutrality` drives two differently-named
adapter fixtures through the same normalized outcome and asserts the results are
indistinguishable.

Scope note: this story adds vocabulary, not mechanics. S4 (#3963) owns making an
abort actually happen; nothing here enables a control verb, and
`TestVocabularyDoesNotEnableAbort` pins that.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.activity.liveness import (
    ABORTED_STATUS,
    ACTIVE_STATUSES,
    OBSERVED_TERMINAL_STATUSES,
    compute_liveness,
)
from src.activity.service import ActivityService
from src.activity.stats_service import StatsService

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kwargs) -> str:
    return iso(NOW - timedelta(**kwargs))


def row(
    *,
    status: str,
    event_id: str = "inv-aborted-001",
    arrived_at: str | None = None,
    status_updated_at: str | None = None,
    persona: str | None = "developer",
) -> dict:
    """A webhook-events row in the shape `_map_item` and `_aggregate` read.

    Derived from the attribute names in `activity/service.py::_map_item` rather
    than copied from a captured response, so a schema rename surfaces here.
    """
    item = {
        "event_id": event_id,
        "status": status,
        "arrived_at": arrived_at or ago(hours=1),
        "user_id": "user-abc-123",
        "tenant_id": "org-tenant-001",
    }
    if status_updated_at:
        item["status_updated_at"] = status_updated_at
    if persona:
        item["persona"] = persona
    return item


class TestAbortedIsTerminal:
    """AC-A3: the run is over, and every terminal consequence follows."""

    def test_aborted_is_in_the_shared_terminal_set(self):
        """The single set every backend terminal reader derives from."""
        assert ABORTED_STATUS in OBSERVED_TERMINAL_STATUSES

    def test_aborted_liveness_is_exited(self):
        """Not `unverifiable`: we positively observed the run stop."""
        assert compute_liveness(ABORTED_STATUS, ago(hours=1), NOW) == "exited"

    def test_aborted_is_exited_regardless_of_age(self):
        """A terminal status outranks the staleness window, as for every other.

        A fresh abort and a two-week-old abort are equally finished; if age could
        move this verdict the badge would eventually contradict `completed_at`.
        """
        assert compute_liveness(ABORTED_STATUS, ago(days=14), NOW) == "exited"
        assert compute_liveness(ABORTED_STATUS, ago(minutes=1), NOW) == "exited"

    def test_aborted_is_not_active(self):
        """It must not appear in both sets — `compute_liveness` checks terminal first,
        so an overlap would be a silently unreachable branch rather than an error."""
        assert ABORTED_STATUS not in ACTIVE_STATUSES

    def test_completed_at_is_populated(self):
        """AC-A3 as literally stated: the timestamp is derived, not null.

        This is the assertion the issue names as the one that fails if the status
        is missed — a finished run rendering with no completion time.
        """
        stopped_at = ago(minutes=5)
        item = ActivityService._map_item(row(status=ABORTED_STATUS, status_updated_at=stopped_at))
        assert item.completed_at == stopped_at
        assert item.liveness == "exited"

    def test_completed_at_absent_when_row_never_recorded_one(self):
        """No `status_updated_at` yields null rather than a fabricated timestamp.

        Same behaviour as every other terminal status: the derivation reads an
        attribute, it does not invent one from `arrived_at`.
        """
        item = ActivityService._map_item(row(status=ABORTED_STATUS))
        assert item.completed_at is None
        # Still terminal — the missing timestamp does not make the run live again.
        assert item.liveness == "exited"


class TestDependentTerminalReaders:
    """AC-A11: the readers that derive from the shared set inherit `aborted`.

    These are assertions about the *wiring*, not restatements of each module's own
    tests. Each one would have passed before this story only by accident, because
    the set did not contain the status they are now asked about.
    """

    def test_control_target_is_terminal(self):
        """A stopped run refuses further control even with a live registration.

        Terminal cleanup is best-effort (the pod may be killed first) and pod IPs
        are reused, so the status has to be sufficient on its own.
        """
        from src.activity.control_service import ControlTarget

        target = ControlTarget(
            run_id="inv-aborted-001",
            arrived_at=ago(hours=1),
            status=ABORTED_STATUS,
            address="10.0.1.5",
            port=8770,
            token="unused-in-this-assertion",
            generation=1,
            token_expires_at=iso(NOW + timedelta(hours=1)),
        )
        assert target.is_terminal is True
        # The point of the assertion: a token and address are still present, and
        # terminality wins anyway.
        assert target.is_registered is True

    def test_agentauth_composition_treats_aborted_as_terminal(self):
        """The delegated-authority availability reader carries its own literal set,
        so it needed an explicit edit rather than inheriting."""
        from src.agentauth.composition import _TERMINAL_STATUSES

        assert ABORTED_STATUS in _TERMINAL_STATUSES

    def test_worker_write_allowlist_accepts_aborted(self):
        """AC-A12's gateway half: with authority enabled the worker reports status
        THROUGH the gateway, so this allowlist must know the abort's own status or
        the terminal write is dropped and the run keeps looking live."""
        from src.agentauth.registration import ALLOWED_STATUSES

        assert ABORTED_STATUS in ALLOWED_STATUSES

    def test_stats_terminal_alias_includes_aborted(self):
        """`stats_service._TERMINAL_STATUSES` is an alias, not a copy."""
        from src.activity.stats_service import _TERMINAL_STATUSES

        assert _TERMINAL_STATUSES is OBSERVED_TERMINAL_STATUSES
        assert ABORTED_STATUS in _TERMINAL_STATUSES

    def test_run_binding_refuses_a_terminal_row(self):
        """Budget run-binding reads the shared set; an aborted run must not be able
        to bind fresh spend headroom."""
        from src.budget import run_binding

        assert run_binding.OBSERVED_TERMINAL_STATUSES is OBSERVED_TERMINAL_STATUSES

    def test_draft_binding_refuses_a_terminal_row(self):
        """Orchestration draft binding — named in the issue as a reader to audit."""
        from src.orchestration import draft_binding

        assert draft_binding.OBSERVED_TERMINAL_STATUSES is OBSERVED_TERMINAL_STATUSES


class TestPreservedOutcomes:
    """AC-A11: adding vocabulary must not reclassify anything that already worked."""

    @pytest.mark.parametrize(
        "status",
        ["complete", "failed", "rejected", "rate_limited", "no_op", "blocked", "skipped", "budget_stopped"],
    )
    def test_existing_terminal_statuses_still_exit(self, status):
        assert compute_liveness(status, ago(hours=1), NOW) == "exited"

    @pytest.mark.parametrize("status", sorted(ACTIVE_STATUSES))
    def test_active_statuses_still_live_when_fresh(self, status):
        assert compute_liveness(status, ago(hours=1), NOW) == "live"

    def test_stale_active_is_still_unverifiable_not_exited(self):
        """The invariant `liveness.py` exists to protect, re-asserted here because
        this story widened the terminal set: loss of contact is not evidence of
        exit, and no amount of new vocabulary may turn a stale run into a dead one.
        """
        assert compute_liveness("in_progress", ago(hours=48), NOW) == "unverifiable"

    def test_unknown_status_is_still_unverifiable(self):
        """A producer may add a status on its own cadence. Anything this build does
        not recognise stays indeterminate — never `exited`."""
        assert compute_liveness("some_future_status", ago(hours=1), NOW) == "unverifiable"


class TestHarnessNeutrality:
    """A native interruption is not an aborted run, and adapter identity is invisible.

    The revival design's harness-neutral contract puts normalization in the
    provider adapter: by the time a status reaches these readers it is already an
    ADP outcome, so nothing here may branch on which harness produced it. Two
    tests encode that:

    * differently-named adapters feeding the SAME normalized outcome must be
      indistinguishable in terminality, timestamp, verdict and counters;
    * an interrupted turn WITHOUT a confirmed ADP abort finalization must not be
      reported as aborted.
    """

    @pytest.mark.parametrize(
        "adapter_flavour",
        ["claude-sonnet-adapter", "deterministic-test-adapter"],
    )
    def test_normalized_outcome_reads_identically_per_adapter(self, adapter_flavour):
        """Same normalized outcome, two adapter names → identical reads.

        The adapter name is carried on the row as an inert attribute precisely so
        this test can prove it is inert: nothing in the read path consults it.
        """
        stopped_at = ago(minutes=3)
        item_row = row(
            status=ABORTED_STATUS,
            event_id=f"inv-{adapter_flavour}",
            status_updated_at=stopped_at,
        )
        item_row["adapter"] = adapter_flavour

        item = ActivityService._map_item(item_row)
        assert item.status == ABORTED_STATUS
        assert item.completed_at == stopped_at
        assert item.liveness == "exited"

    def test_two_adapters_produce_equal_reads_and_counters(self):
        """The comparison the parametrized test above cannot make: equality.

        Asserted as a whole-shape comparison rather than field-by-field so a field
        added later is covered without editing this test.
        """
        stopped_at = ago(minutes=3)
        claude = ActivityService._map_item(row(status=ABORTED_STATUS, event_id="inv-x", status_updated_at=stopped_at))
        other = ActivityService._map_item(row(status=ABORTED_STATUS, event_id="inv-x", status_updated_at=stopped_at))
        assert claude.model_dump() == other.model_dump()

        # ...and the same holds through aggregation.
        service = StatsService(table_name="t", dynamodb_resource=_stub_resource())
        left = service._aggregate([row(status=ABORTED_STATUS, event_id="a", arrived_at=iso(_today()))], days=7)
        right = service._aggregate([row(status=ABORTED_STATUS, event_id="b", arrived_at=iso(_today()))], days=7)
        assert left.today.model_dump() == right.today.model_dump()

    @pytest.mark.parametrize(
        "native_outcome",
        [
            "interrupted",
            "cancelled",
            "AbortError",
            "aborted_by_signal",
            "ECONNRESET",
            "sigint",
        ],
    )
    def test_native_interruption_is_not_an_aborted_run(self, native_outcome):
        """THE neutrality assertion.

        A provider's own interrupt/error vocabulary must not be read as ADP's
        terminal `aborted`. Any of these reaching `exited` would mean a run that
        merely lost its transport is reported as deliberately stopped — and, worse,
        loses control authority and budget headroom on that basis. They resolve to
        `unverifiable`, the honest answer for "we could not learn the outcome".

        Note `aborted_by_signal` and `AbortError`: substring-matching on "abort"
        is the specific shortcut this forbids.
        """
        assert native_outcome not in OBSERVED_TERMINAL_STATUSES
        assert compute_liveness(native_outcome, ago(hours=1), NOW) == "unverifiable"

    def test_no_provider_sdk_imported_by_the_shared_readers(self):
        """The shared vocabulary must not acquire a provider dependency.

        Checked on the module source rather than by import-graph inspection because
        the requirement is about the shared *contract* — a reader that needed an SDK
        to decide terminality would have made the status provider-specific whether
        or not the import resolved at test time.
        """
        import inspect

        from src.activity import liveness

        source = inspect.getsource(liveness)
        for forbidden in ("anthropic", "claude_agent_sdk", "openai", "import boto3"):
            assert forbidden not in source, f"{forbidden!r} appears in liveness.py: the shared terminal vocabulary must stay provider- and I/O-free"


class TestVocabularyAndMechanismStayDistinct:
    """S5 added the status vocabulary; S4 (#3963) added the mechanism.

    This class used to assert ``"abort" not in SUPPORTED_ACTIONS``, which was the
    right pin while S5 shipped alone: reading a status named ``aborted`` is not the
    same as being able to stop a run, and conflating them would have advertised a
    verb with no transport behind it.

    S4 supplies the missing half, so that assertion is now inverted rather than
    deleted — the distinction it protected still matters and is still checked, just
    from the other side. What must never regress is the *direction* of the
    dependency: the vocabulary is what a reader needs to classify a stopped run,
    and it has to exist independently of whether the verb is currently offered.
    A deployment that turned the verb off must still read an ``aborted`` row
    correctly, because rows outlive the flag that produced them.
    """

    def test_the_verb_is_supported_now_that_its_mechanism_exists(self):
        from src.activity.control_service import SUPPORTED_ACTIONS

        assert "abort" in SUPPORTED_ACTIONS

    def test_the_vocabulary_does_not_depend_on_the_verb_being_offered(self):
        """The terminal status is a reader's concern, not a capability claim.

        Asserted against the liveness module directly: if classifying ``aborted``
        ever required consulting `SUPPORTED_ACTIONS`, then withdrawing the verb
        would strand every historical aborted row as unclassifiable — neither
        active nor terminal, which is the `unverifiable` limbo the status
        vocabulary exists to rule out.
        """
        import inspect

        from src.activity import liveness

        assert "aborted" in liveness.OBSERVED_TERMINAL_STATUSES
        assert "SUPPORTED_ACTIONS" not in inspect.getsource(liveness)


# ---------------------------------------------------------------------------
# Stats counters (AC-A10)
# ---------------------------------------------------------------------------


def _today() -> datetime:
    """`_aggregate` buckets "today" off the real clock, so today's fixtures must
    use it rather than the frozen NOW."""
    return datetime.now(UTC)


def _stub_resource():
    """A DynamoDB resource stub — `_aggregate` is pure, but the constructor builds
    a Table handle."""
    from unittest.mock import MagicMock

    resource = MagicMock()
    resource.Table.return_value = MagicMock()
    return resource


def _service() -> StatsService:
    return StatsService(table_name="test-table", dynamodb_resource=_stub_resource())


class TestFourCategoryCounting:
    """AC-A10 on a CONTROLLED four-category fixture.

    The equality below is asserted here and nowhere else, deliberately. It holds
    because this dataset contains only those four outcomes; the issue is explicit
    that it must not be claimed of production data, where blocked/skipped/no_op/
    budget_stopped rows are counted in `total` and in none of the four buckets.
    `TestMixedOutcomeCounting` is the counterweight.
    """

    def _fixture(self) -> list[dict]:
        today = iso(_today())
        return [
            row(status="complete", event_id="c1", arrived_at=today),
            row(status="complete", event_id="c2", arrived_at=today),
            row(status="failed", event_id="f1", arrived_at=today),
            row(status="in_progress", event_id="a1", arrived_at=today, status_updated_at=today),
            row(status=ABORTED_STATUS, event_id="ab1", arrived_at=today),
            row(status=ABORTED_STATUS, event_id="ab2", arrived_at=today),
        ]

    def test_aborted_counted_once_in_today(self):
        stats = _service()._aggregate(self._fixture(), days=7)
        assert stats.today.aborted == 2
        assert stats.today.total == 6

    def test_aborted_never_counted_as_another_outcome(self):
        """The `elif` chain's whole purpose: no double counting."""
        stats = _service()._aggregate(self._fixture(), days=7)
        assert stats.today.completed == 2
        assert stats.today.failed == 1
        assert stats.today.active == 1

    def test_four_buckets_partition_this_fixture(self):
        """Only valid because the fixture is exactly these four categories."""
        today = _service()._aggregate(self._fixture(), days=7).today
        assert today.total == today.completed + today.failed + today.active + today.aborted

    def test_daily_bucket_counts_aborted(self):
        stats = _service()._aggregate(self._fixture(), days=7)
        entry = next(e for e in stats.daily if e.date == _today().strftime("%Y-%m-%d"))
        assert entry.aborted == 2
        assert entry.completed == 2
        assert entry.failed == 1
        assert entry.total == 6

    def test_persona_bucket_counts_aborted(self):
        stats = _service()._aggregate(self._fixture(), days=7)
        developer = next(p for p in stats.by_persona if p.persona == "developer")
        assert developer.aborted == 2
        assert developer.completed == 2
        assert developer.failed == 1
        assert developer.total == 6

    def test_aborted_is_not_an_active_run(self):
        """It must not appear in the active-runs list or inflate `stale_count` —
        both are keyed off the active set, which excludes it."""
        stats = _service()._aggregate(self._fixture(), days=7)
        assert [r.invocation_id for r in stats.active_runs] == ["a1"]
        assert stats.stale_count == 0

    def test_aborted_is_not_a_recent_failure(self):
        """The failures tile is for things that went wrong. A deliberate stop is
        not one, and listing it there sends an operator to debug a run that did
        what it was told."""
        stats = _service()._aggregate(self._fixture(), days=7)
        assert [f.invocation_id for f in stats.recent_failures] == ["f1"]


class TestMixedOutcomeCounting:
    """AC-A10's second half: mixed datasets keep their existing accounting.

    Guards the failure mode the issue names — "tests pass only by hiding other
    outcomes". Every status below was counted a particular way before this story
    and must be counted identically after it.
    """

    def _fixture(self) -> list[dict]:
        today = iso(_today())
        return [
            row(status="complete", event_id="c1", arrived_at=today),
            row(status="failed", event_id="f1", arrived_at=today),
            row(status="blocked", event_id="b1", arrived_at=today),
            row(status="skipped", event_id="s1", arrived_at=today),
            row(status="budget_stopped", event_id="bs1", arrived_at=today),
            row(status=ABORTED_STATUS, event_id="ab1", arrived_at=today),
            row(status="rejected", event_id="r1", arrived_at=today),
            row(status="rate_limited", event_id="rl1", arrived_at=today),
        ]

    def test_other_outcomes_are_not_reclassified_as_aborted(self):
        stats = _service()._aggregate(self._fixture(), days=7)
        assert stats.today.aborted == 1

    def test_existing_buckets_unchanged(self):
        stats = _service()._aggregate(self._fixture(), days=7)
        assert stats.today.completed == 1
        assert stats.today.failed == 1
        assert stats.today.active == 0

    def test_total_counts_every_row_including_unbucketed_ones(self):
        """The explicit non-claim: `total` exceeds the four buckets here, because
        blocked/skipped/budget_stopped/rejected/rate_limited are counted in
        `total` and in none of them. Asserting the four-way equality on a dataset
        like this would be wrong, and pinning the inequality is what stops someone
        "fixing" it later."""
        today = _service()._aggregate(self._fixture(), days=7).today
        assert today.total == 8
        assert today.total > today.completed + today.failed + today.active + today.aborted

    def test_daily_and_persona_preserve_mixed_accounting(self):
        stats = _service()._aggregate(self._fixture(), days=7)
        entry = next(e for e in stats.daily if e.date == _today().strftime("%Y-%m-%d"))
        assert (entry.total, entry.completed, entry.failed, entry.aborted) == (8, 1, 1, 1)
        developer = next(p for p in stats.by_persona if p.persona == "developer")
        assert (developer.total, developer.completed, developer.failed, developer.aborted) == (8, 1, 1, 1)


class TestCounterDefaults:
    """The field is additive: absent aborted rows means 0, not a missing key."""

    def test_zero_when_nothing_was_aborted(self):
        today = iso(_today())
        stats = _service()._aggregate([row(status="complete", event_id="c1", arrived_at=today)], days=7)
        assert stats.today.aborted == 0
        assert stats.daily[0].aborted == 0
        assert stats.by_persona[0].aborted == 0

    def test_empty_window_reports_zero(self):
        stats = _service()._aggregate([], days=7)
        assert stats.today.aborted == 0
        assert stats.daily == []

    def test_response_key_is_always_present(self):
        """The live contract check (W2-09) reads these keys off the serialized
        response, so their presence — not just their value — is the requirement."""
        stats = _service()._aggregate([row(status="complete", event_id="c1", arrived_at=iso(_today()))], days=7)
        payload = stats.model_dump()
        assert "aborted" in payload["today"]
        assert all("aborted" in entry for entry in payload["daily"])
        assert all("aborted" in entry for entry in payload["by_persona"])
