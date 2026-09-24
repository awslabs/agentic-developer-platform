"""The T0 fixtures, consumed unchanged, as the manifest requires.

``docs/task-api/evaluation-manifest.json`` names specific fixture files against
T6-AC01, AC02, AC04 and AC05 and records the obligation as "T0 supplies the
fixtures; the owner consumes them unchanged". Validating emitted bodies against the
*schemas* — which the other test modules do — is a weaker statement than that, and
the difference is not academic:

* A schema says a frame is *permissible*. A fixture says what the frame the design
  reviewers agreed on actually looks like. An implementation can satisfy every
  schema and still disagree with the contract's own example on a field the schema
  leaves optional — ``producer_timestamp`` is exactly such a field.
* These files are the shared artifact. If T7's client probe and T6's emitter each
  only validate against schemas, they can pass independently and still fail to
  interoperate; the fixture is the fixed point that makes those two claims
  comparable.

So the assertions here are equality against the fixture body, not validity. Each
test reconstructs the frame from the fixture's own inputs and requires the result to
equal the fixture in full — which also means an emitter that silently *added* a field
fails, where a schema check would only catch it if the schema happened to close the
object.

The fixtures are read from ``docs/`` at test time rather than copied into the test
tree. A copy is a fork: it would keep passing after T0 amended the contract, which
inverts the purpose. The gateway image does not ship ``docs/``, so these tests skip
rather than fail where the contract is absent — the same convention as
``test_limits_match_contract.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.tasks import events, snapshot
from src.tasks.store import TaskRecord

from .conftest import SCHEMA_DIR, make_record

FIXTURES = SCHEMA_DIR.parent / "fixtures"
TRACES = SCHEMA_DIR.parent / "traces"

pytestmark = pytest.mark.skipif(not FIXTURES.is_dir(), reason="contract fixtures are not present in this build context")

#: Fixture files this module consumes, from ``evaluation-manifest.json``'s
#: ``t0_artifacts`` for the T6 criteria. Listed explicitly so a fixture the manifest
#: assigns to T6 that no test here reads is visible as a gap.
CONSUMED = (
    "valid/event-progress.json",
    "valid/event-task-completed.json",
    "valid/event-input-required.json",
    "valid/event-history-gap.json",
    "valid/sse-snapshot-frame.json",
    "valid/sse-heartbeat-frame.json",
    "valid/task-snapshot-running.json",
    "valid/error-history-expired.json",
    "invalid/sse-snapshot-frame-advances-cursor.json",
    "invalid/sse-heartbeat-counts-as-progress.json",
)


def load(name: str) -> dict:
    """A fixture body with its ``$fixture`` metadata block removed.

    The metadata is the fixture's own description of itself — schema, expectation,
    owner, criteria — and is not part of the instance under test. Removing it here
    rather than in each test keeps the comparisons below equality against the whole
    remaining document, so a field the implementation adds cannot hide in a subset.
    """
    document = json.loads((FIXTURES / name).read_text())
    document.pop("$fixture", None)
    return document


def metadata(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())["$fixture"]


def event_from(fixture: dict) -> events.TaskEvent:
    """Rebuild a ``TaskEvent`` from a fixture's own field values.

    Every input comes from the fixture, so the only thing the comparison can be
    testing is the serialization — not a set of values retyped from the fixture into
    the test, which would make the test pass by construction.
    """
    return events.TaskEvent(
        task_id=fixture["task_id"],
        invocation_id=fixture["invocation_id"],
        generation=fixture["generation"],
        runtime_attempt_id=fixture["runtime_attempt_id"],
        sequence=fixture["sequence"],
        type=fixture["type"],
        timestamp=fixture["timestamp"],
        data=fixture["data"],
        producer_timestamp=fixture.get("producer_timestamp"),
    )


# ---------------------------------------------------------------------------
# The fixtures exist and say what this module assumes they say
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", CONSUMED)
def test_every_consumed_fixture_is_present_and_owned_by_this_story(name) -> None:
    """The manifest's T6 fixtures exist, and each declares its own expectation.

    Reading ``expect`` from the fixture rather than hardcoding it per test means a
    fixture whose expectation T0 later flips cannot leave a test asserting the old
    polarity — it would assert the new one, or fail the parity check below.
    """
    assert (FIXTURES / name).is_file()
    assert metadata(name)["expect"] == ("valid" if name.startswith("valid/") else "invalid")


@pytest.mark.parametrize("name", CONSUMED)
def test_each_fixture_matches_its_declared_expectation(contract, name) -> None:
    """Valid fixtures validate and invalid ones do not, against their own schema.

    This is T0's check, repeated here deliberately. Every equality assertion below
    rests on the fixture being the thing the contract says it is; if a valid fixture
    stopped validating, those comparisons would be pinning the implementation to a
    body the contract rejects, and they would keep passing while doing it.
    """
    fixture = metadata(name)
    errors = contract(load(name), fixture["schema"])

    if fixture["expect"] == "valid":
        assert errors == [], f"{name} should validate against {fixture['schema']}"
    else:
        assert errors, f"{name} must be rejected: {fixture['reason']}"


# ---------------------------------------------------------------------------
# T6-AC01, AC04: authored events serialize to the fixture exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["valid/event-progress.json", "valid/event-input-required.json", "valid/event-history-gap.json"],
)
def test_an_authored_event_serializes_to_the_fixture_exactly(name) -> None:
    """T6-AC01/AC04: the wire form is the contract's, field for field.

    ``history.gap`` is in this set because it is the honesty guarantee AC04 names:
    the gateway reports a discontinuity it knows about rather than renumbering
    around it, and the fixture fixes the shape of that admission — which cursors were
    skipped and which reports were lost.
    """
    fixture = load(name)

    assert event_from(fixture).to_contract() == fixture


def test_a_gateway_authored_event_omits_the_producer_timestamp_the_fixture_nulls(contract) -> None:
    """The one place this implementation and its fixture differ, stated openly.

    ``event-task-completed.json`` spells the absent producer time as an explicit
    ``null``; ``to_contract`` omits the key. Both validate — the contract's
    ``nullable_timestamp`` permits either — so this is a T0 fixture inconsistency
    rather than a defect: ``event-history-gap.json`` is also gateway-authored and
    omits the key, so the two fixtures do not agree with each other.

    The omission is the deliberate reading. An explicit null asserts "the producer
    supplied no timestamp", which presumes a producer; a terminal event the gateway
    authored has none, and the two-marker latency threshold is measured at emitter
    and receiver, so a null producer time on a server-authored event is a data point
    that does not exist rather than a missing one.

    Asserted rather than glossed: every other field must still match exactly, so
    this test fails if the difference ever widens beyond the one key.
    """
    fixture = load("valid/event-task-completed.json")
    assert fixture["producer_timestamp"] is None

    emitted = event_from(fixture).to_contract()

    assert "producer_timestamp" not in emitted
    assert emitted == {k: v for k, v in fixture.items() if k != "producer_timestamp"}
    assert contract(emitted, "events.schema.json#/$defs/event") == [], "the omission must still validate"


def test_the_terminal_fixture_is_the_one_that_closes_a_stream(contract) -> None:
    """The fixture's kind is terminal, and a progress event of the same shape is not.

    Pinned against the fixture because "which events end a stream" is the rule a
    subscriber's whole reconnect strategy depends on: closing on a non-terminal kind
    would make a client resume forever, and failing to close on a terminal one would
    hold a socket open past the task's own end.
    """
    terminal = event_from(load("valid/event-task-completed.json"))
    progress = event_from(load("valid/event-progress.json"))

    assert terminal.closes_stream
    assert not progress.closes_stream


@pytest.mark.parametrize("name", ["valid/event-task-completed.json", "valid/event-history-gap.json"])
def test_the_fixtures_a_bounded_buffer_must_never_drop_are_protected(name) -> None:
    """T6-AC04: the kinds whose loss cannot be recovered survive backpressure.

    Design section 9 reserves capacity for "terminal/error" evidence, and the test
    of membership is recoverability, not importance. A terminal outcome and an
    admitted gap are statements nothing else reproduces: drop the first and a
    subscriber waits forever on a task that ended; drop the second and it believes
    it has continuous history it does not.
    """
    assert event_from(load(name)).is_protected


@pytest.mark.parametrize("name", ["valid/event-progress.json", "valid/event-input-required.json"])
def test_the_recoverable_fixtures_are_droppable_under_pressure(name) -> None:
    """The complement, without which "protected" would be an empty label.

    If every kind were protected the bounded buffer could not shed anything and a
    slow subscriber would apply backpressure into the agent's execution — the exact
    failure T6-AC04 forbids. So the set is as narrow as honesty allows.

    ``input.required`` is the interesting member and it belongs here, though the
    first version of this test asserted the opposite. A dropped frame does not
    silently vanish: the buffer overflowing *disconnects* the subscriber with a
    resume cursor, and the reconnect's snapshot carries ``input_request`` for as long
    as the task is ``waiting_for_input``. The pending clarification is therefore
    recoverable from state, which is what distinguishes it from a terminal event —
    there is no snapshot field that reconstructs "the task ended while you were
    away". Protecting it anyway would widen the unbounded set for no gain in honesty.
    """
    assert not event_from(load(name)).is_protected


def test_a_dropped_clarification_is_recoverable_from_the_snapshot() -> None:
    """The guarantee that makes ``input.required`` safe to drop, asserted directly.

    Without this the reasoning above would be an unverified claim in a comment. A
    change that stopped carrying ``input_request`` on ``waiting_for_input`` would
    turn a droppable event into an unrecoverable one, and the only thing that would
    fail is this test.
    """
    fixture = load("valid/event-input-required.json")
    request = fixture["data"]

    waiting = make_record(status="waiting_for_input", input_request=request)
    rendered = snapshot.render(waiting, request_id="req-recovery")

    assert rendered["input_request"] == request, "a reconnecting client must still see the pending prompt"


@pytest.mark.parametrize("name", ["valid/event-progress.json", "valid/event-task-completed.json", "valid/event-history-gap.json"])
def test_fixture_event_data_passes_the_mirrored_validation(name) -> None:
    """The contract's own examples survive this implementation's data checks.

    ``validate_event_data`` restates closed vocabularies that live in ``docs/``,
    because the gateway image does not ship the schemas. A mirror that is stricter
    than the contract is as much a defect as one that is looser — it would refuse
    exactly the evidence T0 published as correct — so the fixtures are run through it.
    """
    fixture = load(name)

    events.validate_event_data(fixture["type"], fixture["data"])


# ---------------------------------------------------------------------------
# T6-AC02: the frames that govern cursor discipline
# ---------------------------------------------------------------------------


def test_the_snapshot_frame_is_built_exactly_as_the_fixture_shows() -> None:
    """T6-AC02: the opening frame matches, including the no-advance declaration."""
    fixture = load("valid/sse-snapshot-frame.json")

    built = events.snapshot_frame(
        task_id=fixture["task_id"],
        version=fixture["version"],
        status=fixture["status"],
        high_water_cursor=fixture["high_water_cursor"],
        oldest_event_cursor=fixture["oldest_event_cursor"],
    )

    assert built == fixture


def test_the_heartbeat_frame_is_built_exactly_as_the_fixture_shows() -> None:
    """T6-AC01: the keepalive matches, including that it disclaims being progress."""
    fixture = load("valid/sse-heartbeat-frame.json")
    moment = datetime.strptime(fixture["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    assert events.heartbeat_frame(moment) == fixture


def test_the_rejected_snapshot_variant_is_unreachable_from_this_implementation(contract) -> None:
    """T6-AC02: the emitter cannot produce the frame the contract rejects.

    The rejected fixture differs from the accepted one in exactly one field —
    ``advances_last_event_id: true`` — and the manifest calls it "the subtle one": a
    client that treated the snapshot as a position would resume past events it never
    received, and the loss would be invisible because the stream looked continuous.

    Stated as unreachability rather than as a validation result. The schema already
    rejects the body; what this asserts is that no argument to ``snapshot_frame``
    produces it, because the field is not a parameter at all. A future signature that
    accepted it would fail here even if every caller still passed ``False``.
    """
    rejected = load("invalid/sse-snapshot-frame-advances-cursor.json")
    assert contract(rejected, "events.schema.json#/$defs/sse_snapshot_frame")

    built = events.snapshot_frame(
        task_id=rejected["task_id"],
        version=rejected["version"],
        status=rejected["status"],
        high_water_cursor=rejected["high_water_cursor"],
        oldest_event_cursor=rejected["oldest_event_cursor"],
    )

    assert built["advances_last_event_id"] is False
    assert built != rejected
    assert contract(built, "events.schema.json#/$defs/sse_snapshot_frame") == []


def test_the_rejected_heartbeat_variant_is_unreachable_from_this_implementation(contract) -> None:
    """T6-AC01: a keepalive cannot claim to be progress.

    The criterion fails on "heartbeat-only", so a heartbeat that counted as progress
    would mask a stalled task for as long as the socket survived. As above,
    ``counts_as_progress`` is not a parameter, so the rejected frame has no
    construction path.
    """
    rejected = load("invalid/sse-heartbeat-counts-as-progress.json")
    assert contract(rejected, "events.schema.json#/$defs/sse_heartbeat_frame")

    moment = datetime.strptime(rejected["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    built = events.heartbeat_frame(moment)

    assert built["counts_as_progress"] is False
    assert built != rejected
    assert contract(built, "events.schema.json#/$defs/sse_heartbeat_frame") == []


def test_the_snapshot_carries_no_id_so_the_transport_cannot_advance_a_cursor() -> None:
    """The payload's claim is also enforced at the wire level.

    ``advances_last_event_id: false`` is a statement in the body, and a conforming
    SSE client does not read it — it sets Last-Event-ID from the ``id:`` line. So the
    encoder omits ``id:`` for a snapshot, which is what makes the rule binding rather
    than advisory, and emits it for a real event, whose cursor the client *must*
    advance to.
    """
    fixture = load("valid/sse-snapshot-frame.json")

    encoded = events.encode_snapshot(fixture).decode()
    assert "\nid: " not in f"\n{encoded}"

    event = event_from(load("valid/event-progress.json"))
    assert f"id: {event.event_id}" in events.encode_event(event).decode()


def test_the_heartbeat_is_a_comment_so_it_structurally_cannot_carry_an_id() -> None:
    """Stronger than omitting ``id:``: a comment line cannot hold one at all.

    A conforming client discards comment frames, so a heartbeat cannot be mistaken
    for an event or move a position even by a client that ignores the payload. The
    timestamp still rides inside it, so a diagnostic consumer can see liveness.
    """
    fixture = load("valid/sse-heartbeat-frame.json")

    encoded = events.encode_heartbeat(fixture).decode()

    assert encoded.startswith(": ")
    assert "id:" not in encoded
    assert encoded.endswith("\n\n")
    assert json.loads(encoded[2:].strip()) == fixture


def test_the_replay_trace_cursor_discipline_holds_step_by_step() -> None:
    """``traces/reconnect-and-replay.json``, checked against the emitters it names.

    The trace is the manifest's T6-AC02 artifact and its subject is one property:
    which frames move ``Last-Event-ID``. Each step records the position *after* it, so
    the trace itself states that the snapshot and the heartbeat leave the cursor where
    it was and only the event advances it. Driving it from the file means a step T0
    adds is covered rather than silently skipped.
    """
    trace = json.loads((TRACES / "reconnect-and-replay.json").read_text())
    steps = {step["step"]: step for step in trace["steps"]}

    assert steps[1]["last_event_id_after"] is None, "an opening snapshot establishes no position"

    advanced = steps[2]["last_event_id_after"]
    event = event_from(load("valid/event-progress.json"))
    assert advanced.startswith(f"{event.task_id}:"), "the cursor is this task's, not a global position"

    assert steps[3]["last_event_id_after"] == advanced, "a heartbeat must not move the cursor"
    assert steps[4]["last_event_id_after"] == advanced, "nor may a dropped connection"

    resume = events.parse_cursor(advanced, task_id=event.task_id)
    assert steps[5]["expected_replay_from_sequence"] == resume + 1, "replay resumes strictly after the cursor, with no duplicate and no gap"
    assert steps[6]["expected_status"] == 410, "a cursor older than retention is refused, not silently advanced"


def test_the_expired_history_response_is_rendered_as_the_fixture_shows(contract) -> None:
    """T6-AC02/AC04: 410 carries both bounds and admits the gap.

    Built from the fixture's own details and compared to the fixture body, so the
    ``history_gap: true`` flag is pinned as emitted rather than merely permitted. The
    rejected variant omits it, and the omission is what would let a client read the
    response as "start here and you have everything" — which is the silent loss AC02
    forbids.
    """
    fixture = load("valid/error-history-expired.json")
    details = fixture["details"]

    from src.tasks import errors as task_errors

    error = task_errors.history_expired(
        task_id=details["task_id"],
        current_status=details["current_status"],
        oldest_event_cursor=details["oldest_event_cursor"],
        latest_event_cursor=details["latest_event_cursor"],
    )
    body = error.body(fixture["request_id"])

    assert error.status == 410
    assert body == fixture
    assert contract(body, "errors.schema.json#/$defs/history_expired_response") == []


def test_the_rejected_expired_history_variant_is_unreachable() -> None:
    """The gap flag is not optional in this implementation.

    ``errors.history_expired`` sets it unconditionally, so the rejected fixture —
    identical but for the missing flag — has no construction path. A caller cannot
    build the dishonest variant by omitting an argument.
    """
    rejected = load("invalid/error-history-expired-without-gap.json")
    assert "history_gap" not in rejected["details"]

    from src.tasks import errors as task_errors

    details = rejected["details"]
    body = task_errors.history_expired(
        task_id=details["task_id"],
        current_status=details["current_status"],
        oldest_event_cursor=details["oldest_event_cursor"],
        latest_event_cursor=details["latest_event_cursor"],
    ).body(rejected["request_id"])

    assert body["details"]["history_gap"] is True
    assert body != rejected


# ---------------------------------------------------------------------------
# T6-AC05: the snapshot a polling client reads
# ---------------------------------------------------------------------------


def test_the_running_snapshot_renders_exactly_as_the_fixture_shows(contract) -> None:
    """T6-AC05: the full snapshot body equals the contract's example.

    The cursors are the part worth noting: they are *derived* from the record's
    sequence bounds rather than stored as strings, so this comparison also pins the
    cursor format a client feeds back as ``Last-Event-ID``. A snapshot whose cursor
    the event stream would not accept is a snapshot a client cannot stream from.
    """
    fixture = load("valid/task-snapshot-running.json")
    latest = events.parse_cursor(fixture["latest_event_cursor"], task_id=fixture["task_id"])
    oldest = events.parse_cursor(fixture["oldest_event_cursor"], task_id=fixture["task_id"])

    record = TaskRecord(
        task_id=fixture["task_id"],
        invocation_id=fixture["invocation_id"],
        # Server-owned and absent from the snapshot by design: a caller learns
        # neither its own tenant nor the owning principal from a read.
        tenant_id="org-alpha",
        owner_principal_id="svc-alpha",
        persona=fixture["persona"],
        status=fixture["status"],
        version=fixture["version"],
        created_at=fixture["created_at"],
        updated_at=fixture["updated_at"],
        deadline_at=fixture["deadline_at"],
        generation=fixture["generation"],
        runtime_attempt_id=fixture["runtime_attempt_id"],
        execution_health=fixture["execution_health"],
        recovery_required=fixture["recovery_required"],
        external_reference=fixture["external_reference"],
        queue_ack_status=fixture["queue_ack_status"],
        latest_sequence=latest,
        oldest_sequence=oldest,
    )

    rendered = snapshot.render(record, request_id=fixture["request_id"])

    assert rendered == fixture
    assert contract(rendered, "public-api.schema.json#/$defs/task_snapshot") == []


def test_the_snapshot_never_reveals_the_owning_principal_or_tenant() -> None:
    """T6-AC03/AC05: authorization inputs are not disclosed by a read.

    The record carries tenant and owner because they are what the authorization
    decision is made from; the snapshot omits them. Echoing them would publish the
    tenant's internal principal naming to anyone who can read one task, and would
    hand a caller the exact strings to attempt in a forged report.
    """
    fixture = load("valid/task-snapshot-running.json")

    assert "tenant_id" not in fixture
    assert "owner_principal_id" not in fixture


def test_the_snapshot_forbids_a_presigned_download_url(contract) -> None:
    """T6-AC05: the rejected fixture's field cannot be rendered.

    ``submit-response-presigned-download-url.json`` is a snapshot carrying a
    presigned URL for a result artifact. The reason it is rejected is the reason the
    artifact download route streams bytes instead: a presigned URL outlives the
    credential that obtained it, survives revocation, and records no reader.
    """
    rejected = load("invalid/submit-response-presigned-download-url.json")
    assert contract(rejected, "public-api.schema.json#/$defs/task_snapshot")

    assert "download_url" in json.dumps(rejected)
    assert "download_url" not in json.dumps(load("valid/task-snapshot-running.json"))
