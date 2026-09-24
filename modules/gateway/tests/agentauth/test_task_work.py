"""The recovery fault matrix: interrupt at each point, assert nothing is lost.

Structured after the interruption table in the accepted design (section 7). Each
row there names a point where the process can die and what recovery must then do,
and each gets a test here that injects the failure and asserts the work is still
discoverable, still bounded, and still single-owner.

Run against moto rather than mocks: the properties under test ARE the DynamoDB
condition expressions (version CAS, lease-token equality, attribute_not_exists),
and a MagicMock would happily accept a condition that real DynamoDB rejects --
proving only that the test agrees with itself.

Covers T3-AC01 (injected failures recover with no further client request) and
T3-AC02 (no competing owners; transport IDs never become run IDs).
"""

from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from src.agentauth.task_work import (
    DISPATCH_SORT_PREFIX,
    MAX_PUBLICATION_TRIES,
    MAX_WORK_RECORDS_PER_INVOCATION,
    PUBLICATION_TRY_WINDOW_MINUTES,
    SHARD_ATTRIBUTE,
    SHARD_COUNT,
    TASK_WORK_INDEX,
    WORK_LEASE_SECONDS,
    TaskWorkError,
    TaskWorkStore,
    decode_cursor,
    due_key,
    encode_cursor,
    work_shard,
)

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
DISPATCH = "b5e9835b-fc24-4231-96f2-e8b8ca3681be"
SECOND_DISPATCH = "c7f0946c-0d35-4342-a703-f9c9db4792cf"
TENANT = "t-4821"
DIGEST = "a" * 64
START = 1_760_000_000  # Fixed clock: a real clock makes lease tests flaky.
HOUR = 3600


@pytest.fixture
def work():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="events",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
                {"AttributeName": "task_work_shard", "AttributeType": "S"},
                {"AttributeName": "task_due", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": TASK_WORK_INDEX,
                    "KeySchema": [
                        {"AttributeName": "task_work_shard", "KeyType": "HASH"},
                        {"AttributeName": "task_due", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        now = [START]
        store = TaskWorkStore(dynamodb_client=ddb, table_name="events", clock=lambda: now[0])
        yield SimpleNamespace(store=store, ddb=ddb, now=now)


def _deadline(now: list[int], *, seconds: int = HOUR):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(now[0] + seconds, tz=UTC)


def accept(work, *, task=TASK, dispatch=DISPATCH):
    """The acceptance transaction's durable intent, as T1 will write it."""
    return work.store.put_work(
        task_id=task,
        kind="dispatch",
        tenant_id=TENANT,
        dispatch_id=dispatch,
        envelope_digest=DIGEST,
        deadline_at=_deadline(work.now),
    )


def sort_key(dispatch=DISPATCH):
    return f"{DISPATCH_SORT_PREFIX}{dispatch}"


# --- Interruption: transaction commits, HTTP response lost -------------------


def test_accepted_work_is_discoverable_without_another_client_request(work):
    """T3-AC01: the 202 is lost, nobody retries, the sweep still finds the work."""
    accept(work)

    found, cursor = work.store.due_work(shard=work_shard(TASK))

    assert [item["dispatch_id"]["S"] for item in found] == [DISPATCH]
    assert cursor is None
    assert found[0]["publication_outcome"]["S"] == "pending"


def test_replayed_acceptance_cannot_create_a_second_attempt_series(work):
    """An at-least-once acceptance path must not double-dispatch one task."""
    accept(work)

    with pytest.raises(TaskWorkError) as raised:
        accept(work)

    assert raised.value.code == "already_exists"


# --- Interruption: SQS send fails or the response is lost --------------------


def test_unknown_publication_stays_due_for_the_next_sweep(work):
    """The send may have landed. The record must neither settle nor be lost.

    This is the row of the design's table that most easily becomes a bug: an
    ambiguous send recorded as failed authorizes a fresh dispatch, and a fresh
    dispatch of a message that did land is a second execution.
    """
    accept(work)
    claimed = work.store.claim(task_id=TASK, sort_key=sort_key())

    settled = work.store.settle_publication(
        task_id=TASK,
        dispatch_id=DISPATCH,
        lease_token=claimed["lease_token"]["S"],
        publication_outcome="unknown",
    )

    assert settled["publication_outcome"]["S"] == "unknown"
    assert "settled_at" not in settled
    assert "lease_token" not in settled, "the lease must be released for the next sweep"
    found, _ = work.store.due_work(shard=work_shard(TASK))
    assert len(found) == 1, "unknown work must remain discoverable"


def test_crash_after_claim_before_settle_recovers_when_the_lease_expires(work):
    """A publisher that dies mid-send holds no lock anyone has to clear."""
    accept(work)
    work.store.claim(task_id=TASK, sort_key=sort_key())  # Then the process dies.

    with pytest.raises(TaskWorkError) as raised:
        work.store.claim(task_id=TASK, sort_key=sort_key())
    assert raised.value.code == "leased", "a live lease is respected"

    work.now[0] += WORK_LEASE_SECONDS + 1
    reclaimed = work.store.claim(task_id=TASK, sort_key=sort_key())

    assert reclaimed["lease_token"]["S"]
    assert int(reclaimed["tries"]["N"]) == 2, "the retry is counted, not hidden"


def test_confirmed_publication_leaves_the_sparse_index(work):
    """Settled work must stop being rediscovered, or the sweep never drains."""
    accept(work)
    claimed = work.store.claim(task_id=TASK, sort_key=sort_key())

    settled = work.store.settle_publication(
        task_id=TASK,
        dispatch_id=DISPATCH,
        lease_token=claimed["lease_token"]["S"],
        publication_outcome="confirmed",
        sqs_message_id="transport-message-id",
    )

    assert settled["settled_at"]["S"]
    assert settled["queue_ack_status"]["S"] == "confirmed"
    assert SHARD_ATTRIBUTE not in settled
    found, _ = work.store.due_work(shard=work_shard(TASK))
    assert found == [], "confirmed work is gone from the index"


def test_confirmed_requires_a_transport_message_id(work):
    """ "The call returned" is not "the message is on the queue"."""
    accept(work)
    claimed = work.store.claim(task_id=TASK, sort_key=sort_key())

    with pytest.raises(TaskWorkError) as raised:
        work.store.settle_publication(
            task_id=TASK,
            dispatch_id=DISPATCH,
            lease_token=claimed["lease_token"]["S"],
            publication_outcome="confirmed",
            sqs_message_id=None,
        )

    assert raised.value.code == "confirmed_without_message_id"


def test_transport_id_is_stored_for_diagnostics_and_is_not_the_work_identity(work):
    """T3-AC02: the SQS message ID never replaces the ADP identifiers."""
    accept(work)
    claimed = work.store.claim(task_id=TASK, sort_key=sort_key())

    settled = work.store.settle_publication(
        task_id=TASK,
        dispatch_id=DISPATCH,
        lease_token=claimed["lease_token"]["S"],
        publication_outcome="confirmed",
        sqs_message_id="transport-message-id",
    )

    assert settled["sqs_message_id"]["S"] == "transport-message-id"
    assert settled["work_id"]["S"] == DISPATCH
    assert settled["task_id"]["S"] == TASK
    assert settled["event_id"]["S"].endswith(TASK)


# --- Interruption: duplicate delivery while an owner is live -----------------


def test_two_publishers_cannot_both_own_one_record(work):
    """T3-AC02: the second claimant is refused, not merged.

    Both read the same version, so the version CAS decides. Asserted against
    real DynamoDB because the condition expression IS the mechanism.
    """
    accept(work)
    first = work.store.claim(task_id=TASK, sort_key=sort_key())

    with pytest.raises(TaskWorkError):
        work.store.claim(task_id=TASK, sort_key=sort_key())

    record = work.store.read(TASK, sort_key())
    assert record["lease_token"]["S"] == first["lease_token"]["S"]


def test_a_stale_claimant_cannot_settle_the_newer_claim(work):
    """A late observation about an older attempt must not overwrite the new one.

    Otherwise a publisher that stalled past its lease could report `failed` for
    its own dead attempt and wipe out the successful publication that replaced
    it -- the task would look unpublished while its message sat on the queue.
    """
    accept(work)
    stale = work.store.claim(task_id=TASK, sort_key=sort_key())["lease_token"]["S"]
    work.now[0] += WORK_LEASE_SECONDS + 1
    fresh = work.store.claim(task_id=TASK, sort_key=sort_key())["lease_token"]["S"]
    assert stale != fresh

    with pytest.raises(TaskWorkError) as raised:
        work.store.settle_publication(
            task_id=TASK,
            dispatch_id=DISPATCH,
            lease_token=stale,
            publication_outcome="failed",
        )

    assert raised.value.code == "stale_lease"
    assert work.store.read(TASK, sort_key())["lease_token"]["S"] == fresh


def test_settled_work_cannot_be_claimed_or_resettled(work):
    """A redelivery observes terminal state and cannot rerun the task."""
    accept(work)
    token = work.store.claim(task_id=TASK, sort_key=sort_key())["lease_token"]["S"]
    work.store.settle_publication(
        task_id=TASK,
        dispatch_id=DISPATCH,
        lease_token=token,
        publication_outcome="confirmed",
        sqs_message_id="transport-message-id",
    )

    with pytest.raises(TaskWorkError) as claim_refused:
        work.store.claim(task_id=TASK, sort_key=sort_key())
    assert claim_refused.value.code == "already_settled"

    with pytest.raises(TaskWorkError) as settle_refused:
        work.store.settle_publication(
            task_id=TASK,
            dispatch_id=DISPATCH,
            lease_token=token,
            publication_outcome="failed",
        )
    assert settle_refused.value.code == "stale_lease"


def test_recovery_republish_is_a_new_dispatch_series_for_the_same_task(work):
    """Recovery keeps the run identity and changes only the dispatch identity."""
    accept(work)
    accept(work, dispatch=SECOND_DISPATCH)

    found, _ = work.store.due_work(shard=work_shard(TASK))

    assert sorted(item["dispatch_id"]["S"] for item in found) == sorted([DISPATCH, SECOND_DISPATCH])
    assert {item["task_id"]["S"] for item in found} == {TASK}


# --- Interruption: recovery bound exhausted ---------------------------------


def test_the_try_budget_is_throttled_not_abandoned_inside_the_window(work):
    """Spending the window's tries must not look like giving up.

    `throttled` and `exhausted` are different answers: throttled work is still
    coming back, exhausted work never is. Reporting exhausted here would abandon
    a task that still had most of its deadline left.
    """
    accept(work)
    for _ in range(MAX_PUBLICATION_TRIES):
        token = work.store.claim(task_id=TASK, sort_key=sort_key())["lease_token"]["S"]
        work.store.settle_publication(
            task_id=TASK,
            dispatch_id=DISPATCH,
            lease_token=token,
            publication_outcome="unknown",
        )
        work.now[0] += 1

    with pytest.raises(TaskWorkError) as raised:
        work.store.claim(task_id=TASK, sort_key=sort_key())

    assert raised.value.code == "throttled"
    assert int(work.store.read(TASK, sort_key())["tries"]["N"]) == MAX_PUBLICATION_TRIES


def test_a_new_try_window_reopens_the_budget(work):
    """5 tries per 10 minutes is a rate, not a lifetime cap."""
    accept(work)
    for _ in range(MAX_PUBLICATION_TRIES):
        token = work.store.claim(task_id=TASK, sort_key=sort_key())["lease_token"]["S"]
        work.store.settle_publication(
            task_id=TASK,
            dispatch_id=DISPATCH,
            lease_token=token,
            publication_outcome="unknown",
        )
        work.now[0] += 1

    work.now[0] += PUBLICATION_TRY_WINDOW_MINUTES * 60
    work.store.reset_try_window(task_id=TASK, sort_key=sort_key())
    claimed = work.store.claim(task_id=TASK, sort_key=sort_key())

    assert int(claimed["tries"]["N"]) == 1


def test_work_past_its_deadline_is_exhausted_and_visibly_so(work):
    """Never quietly abandon and never invent an exit: the caller must see this."""
    accept(work)
    work.now[0] += HOUR + 1

    with pytest.raises(TaskWorkError) as raised:
        work.store.claim(task_id=TASK, sort_key=sort_key())

    assert raised.value.code == "exhausted"
    record = work.store.read(TASK, sort_key())
    assert record is not None, "exhausted work is retained for an operator to find"
    assert record["publication_outcome"]["S"] == "pending"


def test_deadline_beats_a_remaining_try_budget(work):
    """A task past its deadline is exhausted no matter how many tries are left."""
    accept(work)
    work.now[0] += HOUR + 1

    with pytest.raises(TaskWorkError) as raised:
        work.store.claim(task_id=TASK, sort_key=sort_key())

    assert raised.value.code == "exhausted", "not 'throttled': no retry is coming"


def test_counted_tries_with_an_unreadable_window_refuse_rather_than_reset(work):
    """A missing window start must not hand out an unbounded try budget."""
    accept(work)
    work.ddb.update_item(
        TableName="events",
        Key={
            "event_id": {"S": f"TASK_WORK#{TASK}"},
            "arrived_at": {"S": sort_key()},
        },
        UpdateExpression="SET tries = :tries",
        ExpressionAttributeValues={":tries": {"N": str(MAX_PUBLICATION_TRIES)}},
    )

    with pytest.raises(TaskWorkError) as raised:
        work.store.claim(task_id=TASK, sort_key=sort_key())

    assert raised.value.code == "throttled"


# --- Bounded discovery ------------------------------------------------------


def test_discovery_is_bounded_and_its_continuation_is_retained(work):
    """Bounded pages with a usable cursor, never a table scan.

    An unbounded sweep would cost in proportion to total task history rather
    than to outstanding work, and would eventually exceed its own invocation
    budget and stop making progress at all.

    Three dispatch series on ONE task, so every row is guaranteed to share a
    shard -- pagination is what is under test here, not the hash distribution.
    """
    dispatches = [f"{DISPATCH[:-2]}{n:02d}" for n in range(3)]
    for dispatch in dispatches:
        accept(work, dispatch=dispatch)
        work.now[0] += 1

    seen = []
    cursor = None
    cursors = []
    for _ in dispatches:
        page, cursor = work.store.due_work(shard=work_shard(TASK), cursor=cursor, limit=1)
        assert len(page) == 1, "the page limit is honoured"
        seen.append(page[0]["dispatch_id"]["S"])
        cursors.append(cursor)

    assert sorted(seen) == sorted(dispatches), "every record is reached exactly once"
    assert len(set(seen)) == len(seen), "no record is served twice in one sweep"
    # Exhaustion is signalled by the absent cursor, and that is the sweep's stop
    # condition. A caller that treated it as "start again" would re-publish the
    # whole shard every invocation, which is why it must be None and not the
    # last key.
    assert cursors[-1] is None
    assert all(cursors[:-1]), "earlier pages must carry a continuation"


def test_the_page_limit_cannot_exceed_the_contract_bound(work):
    """A caller asking for more than the invocation budget is clamped, not obeyed.

    Asserted on the request sent to DynamoDB: the returned row count would look
    identical whether the limit was clamped or ignored, so it proves nothing.
    """
    accept(work)
    requests = []
    original = work.store.client.query

    def recording(**kwargs):
        requests.append(kwargs)
        return original(**kwargs)

    work.store.client.query = recording
    work.store.due_work(shard=work_shard(TASK), limit=MAX_WORK_RECORDS_PER_INVOCATION * 10)
    work.store.due_work(shard=work_shard(TASK), limit=0)

    assert requests[0]["Limit"] == MAX_WORK_RECORDS_PER_INVOCATION
    assert requests[1]["Limit"] == 1, "a zero limit would return nothing, forever"
    assert "ScanIndexForward" not in requests[0] or requests[0]["ScanIndexForward"]


def test_an_empty_shard_is_refused_rather_than_queried(work):
    """An empty partition key would query nothing and report an empty sweep."""
    with pytest.raises(TaskWorkError) as raised:
        work.store.due_work(shard="")

    assert raised.value.code == "invalid_shard"


def test_work_not_yet_due_is_not_returned(work):
    """Future-dated work must not be dragged forward by the sweep."""
    from datetime import UTC, datetime

    work.store.put_work(
        task_id=TASK,
        kind="dispatch",
        tenant_id=TENANT,
        dispatch_id=DISPATCH,
        envelope_digest=DIGEST,
        deadline_at=_deadline(work.now),
        due_at=datetime.fromtimestamp(work.now[0] + 120, tz=UTC),
    )

    assert work.store.due_work(shard=work_shard(TASK))[0] == []

    work.now[0] += 121
    assert len(work.store.due_work(shard=work_shard(TASK))[0]) == 1


def test_work_due_in_the_current_millisecond_is_included(work):
    """An exclusive bound would strand work due exactly now until the next sweep."""
    accept(work)

    found, _ = work.store.due_work(shard=work_shard(TASK))

    assert len(found) == 1


def test_a_corrupt_cursor_is_refused_rather_than_silently_restarting(work):
    """Restarting the page would re-send messages already published."""
    with pytest.raises(TaskWorkError) as raised:
        work.store.due_work(shard=work_shard(TASK), cursor="not-base64-at-all!!")

    assert raised.value.code == "invalid_cursor"


def test_cursor_round_trips(work):
    key = {"event_id": {"S": f"TASK_WORK#{TASK}"}, "arrived_at": {"S": sort_key()}}

    assert decode_cursor(encode_cursor(key)) == key


# --- Keys and shards --------------------------------------------------------


def test_shards_are_stable_and_within_the_contract_range(work):
    """A record must land in the same shard on every write or it is never found."""
    assert work_shard(TASK) == work_shard(TASK)
    shards = {work_shard(f"tsk_{n}") for n in range(200)}
    assert shards <= {f"v1#{n:02d}" for n in range(SHARD_COUNT)}
    assert len(shards) > 1, "a single shard would serialize all recovery"


def test_due_keys_sort_chronologically_as_strings(work):
    """Fixed width is load-bearing: unpadded, "9" would sort after "10"."""
    from datetime import UTC, datetime

    early = due_key(datetime.fromtimestamp(9, tz=UTC), "w")
    late = due_key(datetime.fromtimestamp(10, tz=UTC), "w")

    assert early < late


def test_work_rows_omit_the_legacy_gsi_attributes(work):
    """Task rows must stay invisible to the legacy Activity indexes.

    The design keeps tenant_id out of the top level for exactly this reason: a
    top-level tenant_id would project every work record into tenant-index and
    show internal plumbing as if it were agent activity.
    """
    item = accept(work)

    for legacy in ("tenant_id", "user_id", "correlation_id", "root_human_id"):
        assert legacy not in item
    assert item["scope"]["M"]["tenant_id"]["S"] == TENANT


def test_invalid_work_is_refused_before_it_reaches_the_table(work):
    with pytest.raises(TaskWorkError) as kind:
        work.store.put_work(
            task_id=TASK,
            kind="not-a-kind",
            tenant_id=TENANT,
            deadline_at=_deadline(work.now),
        )
    assert kind.value.code == "invalid_work_kind"

    with pytest.raises(TaskWorkError) as dispatch:
        work.store.put_work(
            task_id=TASK,
            kind="dispatch",
            tenant_id=TENANT,
            deadline_at=_deadline(work.now),
        )
    assert dispatch.value.code == "dispatch_id_required"

    with pytest.raises(TaskWorkError) as scope:
        work.store.put_work(
            task_id=TASK,
            kind="dispatch",
            tenant_id="",
            dispatch_id=DISPATCH,
            deadline_at=_deadline(work.now),
        )
    assert scope.value.code == "invalid_scope"


def test_an_invalid_outcome_is_refused(work):
    accept(work)
    token = work.store.claim(task_id=TASK, sort_key=sort_key())["lease_token"]["S"]

    with pytest.raises(TaskWorkError) as raised:
        work.store.settle_publication(
            task_id=TASK,
            dispatch_id=DISPATCH,
            lease_token=token,
            publication_outcome="probably-fine",
        )

    assert raised.value.code == "invalid_outcome"


def test_reads_are_consistent(work, monkeypatch):
    """The index can lag; an authorization-relevant read cannot.

    A claim that acted on an eventually-consistent read could publish for a task
    another writer already settled.
    """
    calls = []
    original = work.store.client.get_item

    def recording(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(work.store.client, "get_item", recording)
    accept(work)
    work.store.read(TASK, sort_key())

    assert calls and all(call["ConsistentRead"] for call in calls)


def test_missing_work_is_distinguishable_from_a_refusal(work):
    with pytest.raises(TaskWorkError) as raised:
        work.store.claim(task_id=TASK, sort_key=sort_key())

    assert raised.value.code == "not_found"
