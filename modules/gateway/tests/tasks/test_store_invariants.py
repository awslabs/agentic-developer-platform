"""Ordering, fencing, idempotency and budget rules of the event store.

These test ``InMemoryTaskStore``, which is a test implementation — so it is worth
being precise about what this file does and does not establish. It establishes
that the *rules* the routes depend on are stated and enforced somewhere, so a
route test that passes is meaningful rather than passing against a permissive
double. It does **not** establish that DynamoDB enforces them; proving the real
conditional transaction behaves this way is V1's lane, against real storage.

The rules under test all exist to make one guarantee true: the sequence is a
reliable record. A sequence consumed by a failed write, a duplicate report
allocating a second number, or a superseded worker appending out of band each
break it in a way that is invisible to a reading client.
"""

from __future__ import annotations

import pytest

from src.tasks.limits import MAX_EVENTS_PER_TASK, RESERVED_TERMINAL_EVENT_SLOTS
from src.tasks.store import (
    EventBudgetExhaustedError,
    InMemoryTaskStore,
    ReportConflictError,
    SequenceFencedError,
    TaskRecord,
    TaskStoreError,
)

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
INVOCATION = "5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40"
ATTEMPT = "a1b2c3d4-e5f6-4718-9a2b-3c4d5e6f7081"
OTHER_ATTEMPT = "b2c3d4e5-f6a7-4829-ab3c-4d5e6f708192"
NOW = "2026-09-24T14:43:20Z"

PROGRESS = {"message": "Analyzing the failing test", "stage": "investigate"}


def make_store(**overrides) -> InMemoryTaskStore:
    store = InMemoryTaskStore()
    base = {
        "task_id": TASK,
        "invocation_id": INVOCATION,
        "tenant_id": "org-alpha",
        "owner_principal_id": "svc-alpha",
        "persona": "agent-task-investigator",
        "status": "running",
        "version": 3,
        "created_at": "2026-09-24T14:42:03Z",
        "updated_at": "2026-09-24T14:42:03Z",
        "deadline_at": "2026-09-24T15:12:03Z",
        "runtime_attempt_id": ATTEMPT,
    }
    store.put_task(TaskRecord(**{**base, **overrides}))
    return store


def append(store: InMemoryTaskStore, *, report_id=None, event_type="progress.updated", data=None, **kwargs):
    return store.append_event(
        task_id=TASK,
        report_id=report_id,
        event_type=event_type,
        data=PROGRESS if data is None else data,
        producer_timestamp=None,
        timestamp=NOW,
        **kwargs,
    )


# -- ordering --------------------------------------------------------------


def test_sequences_strictly_increase_from_one() -> None:
    """Invariant LC-09. The first event is 1, not 0.

    Zero is the "no events yet" sentinel on the task row, so an event at sequence
    0 would be indistinguishable from an empty stream.
    """
    store = make_store()
    assert [append(store).event.sequence for _ in range(3)] == [1, 2, 3]


def test_high_water_and_oldest_bounds_track_the_appends() -> None:
    """The task row must know the position it handed out.

    A sequence that existed without the row knowing it would let a second writer
    allocate the same number, which is the one way two different events can claim
    one position.
    """
    store = make_store()
    append(store)
    append(store)
    record = store.load_task(task_id=TASK)
    assert record.latest_sequence == 2
    assert record.oldest_sequence == 1
    assert record.latest_event_cursor == f"{TASK}:2"
    assert record.oldest_event_cursor == f"{TASK}:1"


def test_cursors_are_null_before_the_first_event() -> None:
    """``nullable_event_cursor`` exists for exactly this state.

    Reporting ``...:0`` would hand a client a cursor the contract pattern rejects
    and that it could never replay from.
    """
    record = make_store().load_task(task_id=TASK)
    assert record.latest_event_cursor is None
    assert record.oldest_event_cursor is None


def test_read_events_returns_ordered_events_after_a_position() -> None:
    store = make_store()
    for _ in range(5):
        append(store)
    page = store.read_events(task_id=TASK, after_sequence=2, limit=10)
    assert [event.sequence for event in page] == [3, 4, 5]


def test_read_events_respects_the_page_limit() -> None:
    """Paging is what keeps a long history from becoming one unbounded read."""
    store = make_store()
    for _ in range(5):
        append(store)
    assert [event.sequence for event in store.read_events(task_id=TASK, after_sequence=0, limit=2)] == [1, 2]


def test_events_inherit_the_task_generation_and_attempt() -> None:
    """The event records which attempt authored it, not which one is current.

    Without this, output from a replaced worker would be indistinguishable from
    the current attempt's in the durable history.
    """
    store = make_store()
    event = append(store).event
    assert event.generation == 1
    assert event.runtime_attempt_id == ATTEMPT


# -- fencing ---------------------------------------------------------------


def test_a_superseded_generation_cannot_append() -> None:
    """Invariant LC-10: late output from a replaced worker alters no outcome."""
    store = make_store(generation=2)
    with pytest.raises(SequenceFencedError):
        append(store, expect_generation=1)


def test_a_stale_runtime_attempt_cannot_append() -> None:
    """An in-process attempt change is fenced without faking a new generation."""
    store = make_store()
    with pytest.raises(SequenceFencedError):
        append(store, expect_runtime_attempt_id=OTHER_ATTEMPT)


def test_a_fenced_append_consumes_no_sequence() -> None:
    """Invariant LC-09: a failed transaction consumes no sequence.

    This is the property that makes a gap meaningful. If a refused append burned a
    number, the durable history would contain holes that a reading client could
    not distinguish from genuine data loss — and every such gap would trigger a
    spurious loss report.
    """
    store = make_store()
    append(store)
    with pytest.raises(SequenceFencedError):
        append(store, expect_runtime_attempt_id=OTHER_ATTEMPT)
    assert append(store).event.sequence == 2
    assert store.load_task(task_id=TASK).latest_sequence == 2


def test_a_fenced_append_reserves_no_report_slot() -> None:
    """Fencing precedes idempotency bookkeeping, deliberately.

    If a fenced producer's report ID were recorded, the *legitimate* current
    attempt reusing that ID (a plausible retry after a handover) would be answered
    with a replay of an event that was never committed.
    """
    store = make_store()
    with pytest.raises(SequenceFencedError):
        append(store, report_id="r1", expect_runtime_attempt_id=OTHER_ATTEMPT)
    result = append(store, report_id="r1")
    assert result.replayed is False
    assert result.event.sequence == 1


def test_matching_fences_are_accepted() -> None:
    store = make_store()
    result = append(store, expect_generation=1, expect_runtime_attempt_id=ATTEMPT)
    assert result.event.sequence == 1


# -- idempotency -----------------------------------------------------------


def test_a_repeated_report_replays_its_original_position() -> None:
    """A retry after a lost response must not double-report.

    The producer cannot tell a lost response from a lost request, so it retries.
    Returning the original sequence with ``replayed=True`` is what lets it
    distinguish "already committed" from "committed now" instead of choosing
    between duplicating progress and dropping it.
    """
    store = make_store()
    first = append(store, report_id="r1")
    second = append(store, report_id="r1")
    assert second.replayed is True
    assert second.event.sequence == first.event.sequence
    assert store.load_task(task_id=TASK).latest_sequence == 1


def test_a_repeated_report_with_different_content_conflicts() -> None:
    """Honouring it would either duplicate an event or rewrite committed history.

    Both make the sequence an unreliable record, so this is a producer defect
    surfaced as a conflict rather than silently resolved in either direction.
    """
    store = make_store()
    append(store, report_id="r1")
    with pytest.raises(ReportConflictError):
        append(store, report_id="r1", data={"message": "something else", "stage": "investigate"})


def test_report_idempotency_tolerates_key_reordering() -> None:
    """The same intent reserialized differently is the same report.

    A producer that rebuilds its JSON on retry may emit keys in another order.
    Treating a formatting difference as a conflict would strand a legitimate retry
    with no way forward.
    """
    store = make_store()
    first = append(store, report_id="r1", data={"message": "m", "stage": "s"})
    second = append(store, report_id="r1", data={"stage": "s", "message": "m"})
    assert second.replayed is True
    assert second.event.sequence == first.event.sequence


def test_reports_without_an_id_are_not_deduplicated() -> None:
    """Gateway-authored events have no producer report to be idempotent about.

    They must each get their own sequence; collapsing them would silently drop
    distinct state transitions.
    """
    store = make_store()
    assert append(store).event.sequence == 1
    assert append(store).event.sequence == 2


def test_report_ids_are_scoped_per_generation() -> None:
    """A new generation gets a clean idempotency namespace.

    A replacement worker starting its own report numbering must not collide with
    the replaced worker's IDs — that would answer its first genuine report with a
    replay of the previous attempt's event.
    """
    store = make_store()
    first = append(store, report_id="r1")
    store.set_status(TASK, generation=2, runtime_attempt_id=OTHER_ATTEMPT)
    second = append(store, report_id="r1", expect_generation=2, expect_runtime_attempt_id=OTHER_ATTEMPT)
    assert second.replayed is False
    assert second.event.sequence == first.event.sequence + 1


def test_a_replayed_report_whose_event_aged_out_conflicts() -> None:
    """Re-allocating would duplicate an event the client may have deduplicated.

    The original sequence is the truthful answer and it is gone, so the honest
    response is a conflict rather than a fresh event pretending to be the old one.
    """
    store = make_store()
    append(store, report_id="r1")
    append(store)
    store.prune_before(TASK, 2)
    with pytest.raises(ReportConflictError):
        append(store, report_id="r1")


# -- budget ----------------------------------------------------------------


def test_progress_stops_at_the_reserved_tail() -> None:
    """A task that floods progress must still be able to record how it ended.

    Progress is capped below the hard ceiling so the final slots remain available
    for terminal evidence. Without the reservation, a runaway producer could make
    its own outcome unrecordable — and an unrecorded outcome is exactly the
    "missing terminal evidence" T6-AC04 requires to be explicit.
    """
    store = make_store(events_allocated=MAX_EVENTS_PER_TASK - RESERVED_TERMINAL_EVENT_SLOTS, latest_sequence=MAX_EVENTS_PER_TASK)
    with pytest.raises(EventBudgetExhaustedError):
        append(store)


def test_protected_events_may_use_the_reserved_tail() -> None:
    store = make_store(events_allocated=MAX_EVENTS_PER_TASK - RESERVED_TERMINAL_EVENT_SLOTS, latest_sequence=MAX_EVENTS_PER_TASK)
    result = append(store, event_type="task.failed", data={"status": "failed", "version": 4, "outcome": "failed", "error_code": "internal"})
    assert result.event.is_protected


def test_even_terminal_events_stop_at_the_hard_ceiling() -> None:
    store = make_store(events_allocated=MAX_EVENTS_PER_TASK, latest_sequence=MAX_EVENTS_PER_TASK)
    with pytest.raises(EventBudgetExhaustedError):
        append(store, event_type="task.failed", data={"status": "failed", "version": 4, "outcome": "failed", "error_code": "internal"})


def test_pruning_history_does_not_grant_fresh_budget() -> None:
    """Budget is measured against allocation, not against what is still stored.

    If retention removing history refunded budget, a long-running task could emit
    unbounded events by outliving its own retention window — the cap would read as
    enforced while bounding nothing.
    """
    store = make_store(events_allocated=MAX_EVENTS_PER_TASK - RESERVED_TERMINAL_EVENT_SLOTS, latest_sequence=MAX_EVENTS_PER_TASK)
    store.prune_before(TASK, MAX_EVENTS_PER_TASK)
    with pytest.raises(EventBudgetExhaustedError):
        append(store)


# -- storage failure -------------------------------------------------------


def test_a_store_outage_is_never_a_missing_task() -> None:
    """An outage that presented as absence would become anonymous access.

    Authorization cannot be decided without reading protected state, so the store
    must distinguish "I could not read" from "there is nothing there". Routes turn
    the former into 503 and the latter into 404.
    """
    store = make_store()
    store.fail = True
    with pytest.raises(TaskStoreError):
        store.load_task(task_id=TASK)


def test_a_store_outage_fails_appends_and_reads() -> None:
    store = make_store()
    store.fail = True
    with pytest.raises(TaskStoreError):
        append(store)
    with pytest.raises(TaskStoreError):
        store.read_events(task_id=TASK, after_sequence=0, limit=10)


def test_appending_to_an_absent_task_is_a_store_error_not_a_silent_create() -> None:
    """An event for a task that does not exist has no owner and no tenant.

    Creating the row here would manufacture an unowned task — state no
    authorization check could evaluate.
    """
    store = InMemoryTaskStore()
    with pytest.raises(TaskStoreError):
        append(store)
