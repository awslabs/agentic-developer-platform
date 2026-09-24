"""Durable task storage: acceptance, state fencing and event allocation (T1, #5794).

Implements design section 6 at revision
``b5761a4a2502aceaa9133afef552b567a19cb46e``. The gateway is the sole writer of
every record here (``sole_task_writer: gateway`` in the frozen contract); worker
containers reach this state only through authenticated gateway routes, and are
denied direct DynamoDB writes to the ``TASK*`` namespaces by IAM.

## Acceptance is one transaction, or it did not happen

A submitted task must become durable in several places at once: the idempotency
record that makes a retry return the same task, the task metadata, the run
history row, the first event, and the dispatch intent the publisher later reads.
If those could land separately, two failures become possible, and the design
forbids both:

* a caller receiving ``202 Accepted`` for a task that is not fully recorded, and
* a dispatch intent existing without the metadata and authority that bind it —
  "an executable unbound assignment" a worker could pick up and run.

:meth:`TaskStore.accept` therefore writes all of them in a single
``TransactWriteItems``. DynamoDB commits a transaction atomically, so there is no
interleaving where some rows exist and others do not. The caller is told
"accepted" only after that call returns successfully.

## Idempotency is decided by a condition, not by a read-then-write

"Has this key been used?" answered with a read followed by a write is a race:
two concurrent submissions both read "no" and both create a task. Instead the
idempotency row is written with ``attribute_not_exists``, inside the same
transaction. DynamoDB evaluates that condition atomically, so of N concurrent
same-key submissions exactly one transaction commits and the rest are cancelled.

The losers then do a **strongly consistent** read of the committed idempotency
row and return the task it names. That single path serves three cases the design
lists separately:

* genuine concurrency (two clients, same key, same instant),
* a retry after the caller lost the HTTP response, and
* a deliberate duplicate submission.

The read must be strongly consistent. DynamoDB's default eventually-consistent
read can miss a write that has just committed, which here would mean concluding
"no existing task" and creating a second run for one idempotency key — the exact
duplicate-execution outcome the key exists to prevent.

If the stored request digest differs from the incoming one, the key is being
reused with a different payload: that is :class:`IdempotencyConflictError`, surfaced
by T2 as ``409 idempotency_conflict``. Note the asymmetry — same digest replays,
different digest conflicts — which is why the digest is stored on the
idempotency row rather than recomputed from the task.

## Ambiguity resolves by reading committed state, never by trusting the error

A ``TransactionCanceledException`` can mean "your condition failed" or can arrive
alongside a genuinely uncertain outcome (a timeout, a dropped connection). This
module never reports acceptance or failure from the error alone: on any ambiguous
write failure it re-reads the committed idempotency row and decides from what is
actually durable. This matches the confirm-read pattern in
``agentauth/service_authority.py`` and ``agentauth/bootstrap.py``, and it is what
keeps a lost response from turning into either a false ``202`` or a false failure.

## State changes are fenced by version, not by the state they read

Two writers race constantly here: a worker reporting completion while the caller
requests cancellation, and a superseded worker writing after replacement. Every
transition is conditional on the task's current ``version`` (compare-and-swap), so
exactly one of a racing pair commits and the loser is told the actual current
state. The design's rule "if completion commits before a cancel request,
cancellation returns the existing completion; if cancellation commits first,
completion is refused" is precisely this fence, not an ordering assumption.

Generation fencing is separate and additive: a write from generation N is refused
once the task has advanced past it, so a replaced worker cannot overwrite the live
run's state even while holding otherwise-valid credentials.

## Event sequence numbers cannot be duplicated or gapped

Replay depends on a total order per task. The counter on the ``TASK#`` metadata
row and the new ``TASK_EVENTS#`` row are written in one transaction, with the
counter conditioned on its expected prior value. So two concurrent reporters
cannot claim the same number, and a failed transaction consumes no number at all
("failed transactions do not consume a sequence" in the design).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.tasks.records import (
    META_SORT_KEY,
    WORK_DUE_ATTRIBUTE,
    WORK_INDEX_NAME,
    WORK_SHARD_ATTRIBUTE,
    TaskState,
    TaskTransitionError,
    assert_legacy_invisible,
    base_item,
    command_sort_key,
    dispatch_sort_key,
    event_sort_key,
    idempotency_partition,
    is_terminal,
    payload_digest,
    run_sort_key,
    task_commands_partition,
    task_events_partition,
    task_partition,
    task_run_partition,
    task_turns_partition,
    task_work_partition,
    turn_sort_key,
    validate_task_id,
    validate_transition,
    validate_uuid,
    work_due_key,
    work_shard,
)

logger = logging.getLogger(__name__)

WEBHOOK_EVENTS_TABLE_ENV: Final = "WEBHOOK_EVENTS_TABLE"
_DEFAULT_TABLE_NAME: Final = "adp-dev-webhook-events"

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()

#: Retention windows from the design's limits table (section 10). Content is kept
#: 30 days past terminal state; a content-free tombstone survives to day 90 so a
#: reused idempotency key returns 410 rather than silently creating a new task.
CONTENT_RETENTION_DAYS: Final = 30
TOMBSTONE_RETENTION_DAYS: Final = 90

TTL_ATTRIBUTE: Final = "expires_at"

#: Record kinds that must never carry a TTL attribute while the task is live.
#: TTL deletion is asynchronous and unordered, so an expiry stamp on any of these
#: could remove the record that authorises or deduplicates an in-flight task —
#: leaving a running task with no metadata, or freeing an idempotency key while
#: its task still runs so a retry starts a second execution.
TTL_EXEMPT_WHILE_ACTIVE: Final = frozenset({"TASK", "TASK_RUN", "TASK_IDEMP", "TASK_COMMANDS", "TASK_ARTIFACT"})

#: Upper-bound sentinel for the ``<millis>#<work_id>`` sort key. Sorts at or after
#: any real work ID sharing the same millisecond, so ``task_due <= <millis>#<max>``
#: includes every record due at that instant. Without it, work due in the current
#: millisecond would be excluded and only picked up on a later pass.
_WORK_ID_MAX: Final = "￿"

#: The ``SEQ#`` prefix of an event sort key, derived rather than written literally
#: so a change to the key form cannot leave this paging cursor behind. Sorts before
#: every real event key, so it means "from the beginning of history".
_EVENT_KEY_PREFIX: Final = event_sort_key(1).split("#")[0] + "#"


class TaskStoreError(Exception):
    """The storage layer is unavailable or returned an unusable response.

    Distinct from the conflict types below: this means "unknown, retry may
    succeed" and must surface as ``503``, never as a definitive answer. Conflating
    it with "no such task" is how a storage outage turns into a false ``404`` — or
    worse, into a second execution.
    """


class IdempotencyConflictError(Exception):
    """An idempotency key was reused with a different request payload.

    Carries the existing task so the caller can be told what the key already
    refers to. Surfaced as ``409 idempotency_conflict``.
    """

    def __init__(self, *, task_id: str, stored_digest: str, supplied_digest: str) -> None:
        self.task_id = task_id
        self.stored_digest = stored_digest
        self.supplied_digest = supplied_digest
        super().__init__("idempotency_conflict")


class TaskStateConflictError(Exception):
    """A state change was refused because it does not apply to the actual state.

    Raised for every refusal from the store, whether the cause was a lost version
    race or a transition that is not permitted from the current state. Those are
    the same event from a caller's point of view — "the task is not where you
    thought it was" — and collapsing them into one type with ``current_state``
    attached is what lets a caller report reality rather than guess.

    That matters most in a race. If completion commits first, the cancellation
    request must report the existing completion; carrying the state here is what
    makes that possible. A bare "not permitted" error would leave the caller
    knowing only that it failed, not what the outcome was.

    ``reason`` distinguishes a fence loss from an impermissible transition for
    logging and for choosing a response message; it is not needed to act.
    """

    def __init__(
        self,
        *,
        task_id: str,
        current_state: str | None,
        current_version: int | None = None,
        reason: str = "version_conflict",
    ) -> None:
        self.task_id = task_id
        self.current_state = current_state
        self.current_version = current_version
        self.reason = reason
        super().__init__(f"task_state_conflict:{reason}:{current_state}")


class StaleGenerationError(Exception):
    """A write arrived from a superseded worker generation and was refused."""

    def __init__(self, *, task_id: str, supplied: int, current: int | None) -> None:
        self.task_id = task_id
        self.supplied = supplied
        self.current = current
        super().__init__("stale_generation")


@dataclass(frozen=True)
class AcceptanceRequest:
    """Everything needed to make one task durable.

    Every field is server-derived or server-validated. The design's contract lists
    tenant, owner, executable, model, generation, attempt, queue, grant and
    dispatch identity as fields a public caller cannot supply, so these arrive
    from the authenticated context and the admission adapter — not from the body.
    """

    task_id: str
    invocation_id: str
    dispatch_id: str
    tenant: str
    canonical_principal: str
    idempotency_key: str
    persona: str
    request_payload: dict[str, Any]
    deadline_at: datetime
    #: Opaque reference to the run grant committed in the authority table by T3's
    #: transaction. Stored so a reader can tell an authorised task from a
    #: half-prepared one without a second table round trip.
    grant_reference: str
    generation: int = 1
    input_reference: dict[str, Any] | None = None
    artifact_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AcceptedTask:
    """The committed outcome of :meth:`TaskStore.accept`."""

    task_id: str
    invocation_id: str
    dispatch_id: str
    state: TaskState
    version: int
    request_digest: str
    created_at: str
    #: False when this call created the records, True when an equivalent request
    #: was already durable. T2 returns the same 202 body either way, which is what
    #: makes a lost response safe to retry.
    replayed: bool


class TaskStore:
    """Task records in the existing request table.

    Uses the low-level DynamoDB client rather than the resource-level ``Table``
    abstraction, for the reason stated in ``agentauth/store.py``: the security
    properties here *are* properties of the request shape (condition expressions,
    ``ConsistentRead``, transaction composition), and the resource abstraction
    hides exactly that. The tests assert those shapes and behaviours.
    """

    def __init__(self, *, table_name: str | None = None, dynamodb_client=None, clock=None) -> None:
        self._table_name = table_name or os.environ.get(WEBHOOK_EVENTS_TABLE_ENV, _DEFAULT_TABLE_NAME)
        self._client = dynamodb_client or boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        # Injected so retention and deadline behaviour is testable without sleeping.
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def table_name(self) -> str:
        return self._table_name

    # -- acceptance ---------------------------------------------------------

    def accept(self, request: AcceptanceRequest) -> AcceptedTask:
        """Make one task durable, or return the task an equivalent request created.

        Returns a task with ``replayed=False`` when this call committed the
        records, or ``replayed=True`` when an equivalent request was already
        durable. Raises :class:`IdempotencyConflictError` when the key was used with a
        different payload, and :class:`TaskStoreError` when the outcome is unknown.

        The transaction contains the idempotency row, task metadata, run history,
        the first ``task.accepted`` event and the dispatch intent. Capacity
        reservations and the run grant live in the authority table and are
        committed by T3 in the same cross-table transaction; the ``items``
        fragments below are shaped so that composition needs no change here.
        """
        validate_task_id(request.task_id)
        validate_uuid(request.invocation_id, "invocation_id")
        validate_uuid(request.dispatch_id, "dispatch_id")
        if request.generation < 1:
            raise TaskStoreError("generation must be a positive integer")

        digest = payload_digest(request.request_payload)
        now = self._clock()
        now_iso = _iso(now)
        idempotency_pk = idempotency_partition(
            tenant=request.tenant,
            canonical_principal=request.canonical_principal,
            idempotency_key=request.idempotency_key,
        )

        try:
            self._client.transact_write_items(TransactItems=self._acceptance_items(request, digest, idempotency_pk, now_iso))
        except (ClientError, BotoCoreError) as exc:
            # Never decide from the exception: re-read what is actually durable.
            # A cancelled transaction may mean "key already used" (replay or
            # conflict) or may accompany a genuinely uncertain outcome.
            return self._resolve_ambiguous_acceptance(exc, idempotency_pk=idempotency_pk, supplied_digest=digest, request=request)

        return AcceptedTask(
            task_id=request.task_id,
            invocation_id=request.invocation_id,
            dispatch_id=request.dispatch_id,
            state=TaskState.ACCEPTED,
            version=1,
            request_digest=digest,
            created_at=now_iso,
            replayed=False,
        )

    def _acceptance_items(self, request: AcceptanceRequest, digest: str, idempotency_pk: str, now_iso: str) -> list[dict[str, Any]]:
        """Compose the acceptance transaction.

        Deliberately returns fragments rather than executing, so T3 can extend the
        same transaction with the authority-table grant and capacity reservation.
        Splitting acceptance across two transactions would reintroduce the
        prepared-but-unauthorised state the design forbids.
        """
        scope = {"tenant": request.tenant, "canonical_principal": request.canonical_principal}
        deadline_iso = _iso(request.deadline_at)

        metadata = base_item(
            partition=task_partition(request.task_id),
            sort_key=META_SORT_KEY,
            record_type="TASK",
            scope=scope,
        ) | {
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "state": TaskState.ACCEPTED.value,
            # Starts at 1 and is the compare-and-swap fence for every later
            # transition. Never reset, so a stale writer's expected version can
            # never coincidentally match again.
            "version": 1,
            "generation": request.generation,
            "persona": request.persona,
            "request_digest": digest,
            "idempotency_partition": idempotency_pk,
            "grant_reference": request.grant_reference,
            # Sequence 1 is consumed by the task.accepted event in this same
            # transaction, so the counter starts consistent with durable history.
            "event_sequence": 1,
            "command_sequence": 0,
            "turn_count": 0,
            "created_at": now_iso,
            "updated_at": now_iso,
            "deadline_at": deadline_iso,
            # Health and acknowledgement start as explicit unknowns. The design
            # requires unknown evidence to be nullable and "never zero/success",
            # so absence of a heartbeat must not read as healthy.
            "execution_health": "unknown",
            "queue_ack_status": "unknown",
            "recovery_required": False,
            "artifact_ids": list(request.artifact_ids),
        }
        if request.input_reference is not None:
            metadata["input_reference"] = request.input_reference

        idempotency = base_item(
            partition=idempotency_pk,
            sort_key=META_SORT_KEY,
            record_type="TASK_IDEMP",
            scope=scope,
        ) | {
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "dispatch_id": request.dispatch_id,
            # The digest lives here, not only on the task: replay-versus-conflict
            # is decided against the request that created the key.
            "request_digest": digest,
            "created_at": now_iso,
        }

        run = base_item(
            partition=task_run_partition(request.task_id),
            sort_key=run_sort_key(invocation_id=request.invocation_id, generation=request.generation),
            record_type="TASK_RUN",
            scope=scope,
        ) | {
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "generation": request.generation,
            "grant_reference": request.grant_reference,
            "created_at": now_iso,
            "runtime_attempt_id": None,
        }

        first_event = self._event_item(
            task_id=request.task_id,
            scope=scope,
            sequence=1,
            kind="task.accepted",
            invocation_id=request.invocation_id,
            generation=request.generation,
            timestamp=now_iso,
            data={"persona": request.persona},
        )

        dispatch = base_item(
            partition=task_work_partition(request.task_id),
            sort_key=dispatch_sort_key(request.dispatch_id),
            record_type="TASK_WORK",
            scope=scope,
        ) | {
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "dispatch_id": request.dispatch_id,
            "request_digest": digest,
            "publication_state": "pending",
            "publication_tries": 0,
            "created_at": now_iso,
            "updated_at": now_iso,
            # Sparse recovery index attributes. Only work records carry them, so
            # the index holds outstanding work rather than the whole table.
            WORK_SHARD_ATTRIBUTE: work_shard(request.task_id),
            WORK_DUE_ATTRIBUTE: work_due_key(due_at=self._clock(), work_id=request.dispatch_id),
        }

        return [
            # Ordering matters for decoding a cancellation: index 0 failing means
            # the idempotency key already exists, which is the replay/conflict
            # path rather than a storage problem.
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(idempotency),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(metadata),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(run),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(first_event),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(dispatch),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
        ]

    def _resolve_ambiguous_acceptance(self, exc: Exception, *, idempotency_pk: str, supplied_digest: str, request: AcceptanceRequest) -> AcceptedTask:
        """Decide the outcome of a failed acceptance from committed state.

        Three outcomes, all read from storage rather than inferred:

        * A committed idempotency row with a matching digest means an equivalent
          request is already durable — return it as a replay. This covers both
          concurrent submissions and a retry after a lost response.
        * A matching row with a different digest is a genuine conflict.
        * No row means nothing was committed. Raise :class:`TaskStoreError` so the
          caller retries. Critically, this is *not* reported as accepted: the
          design requires that a failed or partial write can never be reported as
          accepted, nor leave an executable unbound assignment. Because acceptance
          is one transaction, "no idempotency row" implies no metadata and no
          dispatch intent either.
        """
        existing = self._read_idempotency(idempotency_pk)
        if existing is None:
            logger.warning(
                "Task acceptance did not commit",
                extra={"task_id": request.task_id, "error": type(exc).__name__},
            )
            raise TaskStoreError("task acceptance not confirmed; retry the same idempotency key") from None

        stored_digest = str(existing.get("request_digest", ""))
        if stored_digest != supplied_digest:
            raise IdempotencyConflictError(
                task_id=str(existing.get("task_id", "")),
                stored_digest=stored_digest,
                supplied_digest=supplied_digest,
            ) from None

        # Equivalent request already durable. Return the ORIGINAL task's
        # identifiers, not this attempt's: the caller must converge on one task,
        # and the freshly generated IDs in `request` were never committed.
        task_id = str(existing.get("task_id", ""))
        snapshot = self.read_task(task_id)
        if snapshot is None:
            # The idempotency row exists but its task does not. Acceptance is one
            # transaction so this should be unreachable; treat it as unknown
            # rather than inventing a task, which is what "no partial prepared
            # task is publicly accepted" requires.
            raise TaskStoreError("task acceptance not confirmed; retry the same idempotency key") from None

        return AcceptedTask(
            task_id=task_id,
            invocation_id=str(existing.get("invocation_id", "")),
            dispatch_id=str(existing.get("dispatch_id", "")),
            state=TaskState(str(snapshot.get("state", TaskState.ACCEPTED.value))),
            version=int(snapshot.get("version", 1)),
            request_digest=stored_digest,
            created_at=str(existing.get("created_at", "")),
            replayed=True,
        )

    # -- reads --------------------------------------------------------------

    def read_task(self, task_id: str) -> dict[str, Any] | None:
        """Strongly consistent task snapshot, or None when there is no such task.

        Consistent because the design specifies a strongly consistent snapshot for
        the status route, and because an authorisation or state decision made on a
        stale read can honour a superseded state.
        """
        return self._get(task_partition(task_id), META_SORT_KEY)

    def _read_idempotency(self, idempotency_pk: str) -> dict[str, Any] | None:
        return self._get(idempotency_pk, META_SORT_KEY)

    def _get(self, partition: str, sort_key: str) -> dict[str, Any] | None:
        """Exact-key consistent read.

        ``GetItem`` rather than a query: the row must be the one the key names.
        ``agentauth/registration.py`` documents the same rule — "query the newest
        row" would let a second row planted under one partition decide what is
        read.
        """
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={"event_id": {"S": partition}, "arrived_at": {"S": sort_key}},
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError) as exc:
            raise TaskStoreError("task store unavailable") from exc
        item = response.get("Item")
        return _deserialize(item) if item else None

    # -- state transitions --------------------------------------------------

    def transition(
        self,
        *,
        task_id: str,
        expected_version: int,
        target_state: TaskState,
        generation: int | None = None,
        event_kind: str | None = None,
        event_data: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Change a task's state and append its event in one transaction.

        Conditional on ``expected_version`` — the compare-and-swap fence. Of two
        racing writers exactly one commits; the loser gets
        :class:`TaskStateConflictError` carrying the state that actually won, so it can
        report reality (cancellation returning an existing completion, or
        completion refused after cancellation committed).

        The state change and its event share the transaction because the design
        requires it: an event without its state change would let a reader see
        ``task.completed`` on a running task, and a state change without its event
        would leave a gap in the replayable history.

        ``generation`` fences a replaced worker. When supplied, the write also
        requires the task's current generation to match, so a superseded worker's
        late write is refused even if it guessed the version.
        """
        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)

        current = TaskState(str(snapshot["state"]))
        # In-process guard first, so an impermissible transition fails early rather
        # than as an opaque condition failure. It is not the fence — the version
        # condition below is what decides a race — and a refusal here is reported
        # as a state conflict carrying the actual state, because in a race this is
        # exactly how the loser finds out the task already reached a terminal
        # outcome (e.g. cancellation arriving after completion committed).
        try:
            validate_transition(current, target_state)
        except TaskTransitionError as exc:
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=current.value,
                current_version=int(snapshot.get("version", 0)),
                reason="transition_not_permitted",
            ) from exc

        now_iso = _iso(self._clock())
        next_version = expected_version + 1
        scope = dict(snapshot.get("scope") or {})

        set_parts = ["#state = :state", "version = :next_version", "updated_at = :now"]
        values: dict[str, Any] = {
            ":state": target_state.value,
            ":next_version": next_version,
            ":now": now_iso,
            ":expected_version": expected_version,
        }
        names = {"#state": "state"}

        for key, value in (attributes or {}).items():
            if key in {"state", "version", "event_id", "arrived_at", "scope", "task_id", "record_type"}:
                # Refuse to let a caller rewrite identity, scope or the fence
                # itself through the generic attribute channel.
                raise TaskStoreError(f"attribute {key!r} cannot be set through a transition")
            placeholder = f":attr_{len(values)}"
            set_parts.append(f"{key} = {placeholder}")
            values[placeholder] = value

        condition = "version = :expected_version AND attribute_exists(event_id)"
        if generation is not None:
            condition += " AND generation = :generation"
            values[":generation"] = generation

        # Terminal state stamps the content retention window. Doing it in the
        # same write as terminalisation means there is no moment where a finished
        # task is missing its retention stamp.
        if is_terminal(target_state):
            set_parts.append(f"{TTL_ATTRIBUTE} = :expires")
            values[":expires"] = int((self._clock() + timedelta(days=CONTENT_RETENTION_DAYS)).timestamp())
            set_parts.append("terminal_at = :now")

        transact: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": {"event_id": {"S": task_partition(task_id)}, "arrived_at": {"S": META_SORT_KEY}},
                    "UpdateExpression": "SET " + ", ".join(set_parts),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": {k: _SERIALIZER.serialize(v) for k, v in values.items()},
                }
            }
        ]

        if event_kind is not None:
            sequence = int(snapshot.get("event_sequence", 0)) + 1
            transact[0]["Update"]["UpdateExpression"] += ", event_sequence = :sequence"
            transact[0]["Update"]["ExpressionAttributeValues"][":sequence"] = _SERIALIZER.serialize(sequence)
            transact.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize(
                            self._event_item(
                                task_id=task_id,
                                scope=scope,
                                sequence=sequence,
                                kind=event_kind,
                                invocation_id=str(snapshot.get("invocation_id", "")),
                                generation=int(snapshot.get("generation", 1)),
                                timestamp=now_iso,
                                data=event_data or {},
                            )
                        ),
                        # A sequence number must not be reusable: if this row
                        # exists the whole transition fails rather than
                        # overwriting committed history.
                        "ConditionExpression": "attribute_not_exists(event_id)",
                    }
                }
            )

        try:
            self._client.transact_write_items(TransactItems=transact)
        except (ClientError, BotoCoreError) as exc:
            self._raise_transition_failure(exc, task_id=task_id, generation=generation)

        return {"task_id": task_id, "state": target_state.value, "version": next_version, "updated_at": now_iso}

    def _raise_transition_failure(self, exc: Exception, *, task_id: str, generation: int | None) -> None:
        """Translate a failed transition into a conflict carrying committed state.

        Re-reads rather than trusting the cancellation reasons, so the caller is
        told what is actually durable. A storage failure must stay
        :class:`TaskStoreError` — reporting it as a state conflict would invite a
        caller to act on a state that was never read.
        """
        if not _is_conditional_failure(exc):
            raise TaskStoreError("task store unavailable") from exc

        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None) from None

        current_generation = int(snapshot.get("generation", 1))
        if generation is not None and current_generation != generation:
            raise StaleGenerationError(task_id=task_id, supplied=generation, current=current_generation) from None

        raise TaskStateConflictError(
            task_id=task_id,
            current_state=str(snapshot.get("state")),
            current_version=int(snapshot.get("version", 0)),
        ) from None

    # -- events -------------------------------------------------------------

    def append_event(
        self,
        *,
        task_id: str,
        kind: str,
        data: dict[str, Any] | None = None,
        expected_sequence: int | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        """Allocate the next sequence number and persist one event atomically.

        The counter update and the event row are one transaction, with the counter
        conditioned on its prior value, so concurrent reporters cannot claim the
        same number and a failed attempt consumes none. ``expected_sequence`` lets
        a caller assert which number it believes is next; omitted, the current
        counter is read and used.
        """
        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)

        current_sequence = int(snapshot.get("event_sequence", 0))
        if expected_sequence is not None and expected_sequence != current_sequence:
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=str(snapshot.get("state")),
                current_version=int(snapshot.get("version", 0)),
            )

        sequence = current_sequence + 1
        now_iso = _iso(self._clock())
        values = {":next": sequence, ":current": current_sequence, ":now": now_iso}
        condition = "event_sequence = :current"
        if generation is not None:
            condition += " AND generation = :generation"
            values[":generation"] = generation

        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {"event_id": {"S": task_partition(task_id)}, "arrived_at": {"S": META_SORT_KEY}},
                            "UpdateExpression": "SET event_sequence = :next, updated_at = :now",
                            "ConditionExpression": condition,
                            "ExpressionAttributeValues": {k: _SERIALIZER.serialize(v) for k, v in values.items()},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _serialize(
                                self._event_item(
                                    task_id=task_id,
                                    scope=dict(snapshot.get("scope") or {}),
                                    sequence=sequence,
                                    kind=kind,
                                    invocation_id=str(snapshot.get("invocation_id", "")),
                                    generation=int(snapshot.get("generation", 1)),
                                    timestamp=now_iso,
                                    data=data or {},
                                )
                            ),
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                ]
            )
        except (ClientError, BotoCoreError) as exc:
            if not _is_conditional_failure(exc):
                raise TaskStoreError("task store unavailable") from exc
            live = self.read_task(task_id)
            if generation is not None and live is not None and int(live.get("generation", 1)) != generation:
                raise StaleGenerationError(task_id=task_id, supplied=generation, current=int(live.get("generation", 1))) from None
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=str(live.get("state")) if live else None,
                current_version=int(live.get("version", 0)) if live else None,
            ) from None

        return {"task_id": task_id, "sequence": sequence, "event_id": f"{task_id}:{sequence}", "timestamp": now_iso}

    def read_events(self, *, task_id: str, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Read up to ``limit`` events after ``after_sequence``, in order.

        A bounded sort-key range query on one partition. No scan, and no index:
        the design forbids a table scan, and events are already colocated under
        ``TASK_EVENTS#<task_id>`` precisely so replay is a cheap ordered read.
        Fixed-width sequence keys are what make the string range comparison agree
        with numeric order.
        """
        if limit < 1:
            raise TaskStoreError("limit must be a positive integer")
        # Sequence numbers start at 1, so `event_sort_key(0)` is invalid by
        # construction; cursor 0 means "from the beginning". Derived from
        # event_sort_key(1) rather than a literal prefix so the two cannot drift.
        after = event_sort_key(after_sequence) if after_sequence >= 1 else _EVENT_KEY_PREFIX
        try:
            response = self._client.query(
                TableName=self._table_name,
                KeyConditionExpression="event_id = :partition AND arrived_at > :after",
                ExpressionAttributeValues={
                    ":partition": {"S": task_events_partition(task_id)},
                    ":after": {"S": after},
                },
                Limit=limit,
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError) as exc:
            raise TaskStoreError("task store unavailable") from exc
        return [_deserialize(item) for item in response.get("Items", [])]

    def _event_item(
        self,
        *,
        task_id: str,
        scope: dict[str, Any],
        sequence: int,
        kind: str,
        invocation_id: str,
        generation: int,
        timestamp: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        return base_item(
            partition=task_events_partition(task_id),
            sort_key=event_sort_key(sequence),
            record_type="TASK_EVENTS",
            scope=scope,
        ) | {
            "task_id": task_id,
            "invocation_id": invocation_id,
            "generation": generation,
            "runtime_attempt_id": None,
            "sequence": sequence,
            # The public cursor form from the contract: <task_id>:<sequence>.
            "task_event_id": f"{task_id}:{sequence}",
            "type": kind,
            "timestamp": timestamp,
            "data": data,
        }

    # -- commands and turns -------------------------------------------------

    def insert_command(
        self,
        *,
        task_id: str,
        command_id: str,
        kind: str,
        payload: dict[str, Any],
        author: str,
        authority_expires_at: datetime,
        expected_version: int,
    ) -> dict[str, Any]:
        """Insert one command exactly once, with its event, in one transaction.

        Keyed by the caller's command UUID under ``attribute_not_exists``, so a
        retry of the same command cannot enqueue it twice — the durable half of
        "one command is appended once and included once in its assigned turn".

        Reusing a command ID with a different kind or payload is a conflict, not a
        silent no-op: the stored digest is compared so a caller cannot smuggle new
        content under an already-accepted ID.

        Commands carry a monotonic ``command_sequence`` because FIFO order cannot
        come from the key — command IDs are random UUIDs and sort arbitrarily.
        """
        validate_uuid(command_id, "command_id")
        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)

        state = TaskState(str(snapshot["state"]))
        if is_terminal(state) or state is TaskState.CANCEL_REQUESTED:
            # The design: after cancellation or a terminal state, new input is
            # refused (409). Cancellation latching blocks new admission.
            raise TaskStateConflictError(task_id=task_id, current_state=state.value, current_version=int(snapshot.get("version", 0)))

        digest = payload_digest({"kind": kind, "payload": payload})
        existing = self._get(task_commands_partition(task_id), command_sort_key(command_id))
        if existing is not None:
            if str(existing.get("command_digest")) != digest:
                raise IdempotencyConflictError(task_id=task_id, stored_digest=str(existing.get("command_digest", "")), supplied_digest=digest)
            return existing  # Already durable; return the committed receipt.

        now_iso = _iso(self._clock())
        sequence = int(snapshot.get("command_sequence", 0)) + 1
        event_sequence = int(snapshot.get("event_sequence", 0)) + 1
        scope = dict(snapshot.get("scope") or {})

        command = base_item(
            partition=task_commands_partition(task_id),
            sort_key=command_sort_key(command_id),
            record_type="TASK_COMMANDS",
            scope=scope,
        ) | {
            "task_id": task_id,
            "command_id": command_id,
            "kind": kind,
            "command_digest": digest,
            "payload": payload,
            "author": author,
            "authority_expires_at": _iso(authority_expires_at),
            "command_sequence": sequence,
            # status/handoff are independent fields per the design: a command can
            # be accepted while its model handoff is still not_started, and an
            # unknown handoff must never read as confirmed.
            "status": "accepted",
            "handoff": "not_started",
            "turn_number": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        event = self._event_item(
            task_id=task_id,
            scope=scope,
            sequence=event_sequence,
            kind="cancel.requested" if kind == "cancel" else "input.accepted",
            invocation_id=str(snapshot.get("invocation_id", "")),
            generation=int(snapshot.get("generation", 1)),
            timestamp=now_iso,
            data={"command_id": command_id},
        )

        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _serialize(command),
                            # Exactly-once insert for this command ID. A retry of
                            # the same command fails here rather than enqueuing it
                            # a second time.
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {"event_id": {"S": task_partition(task_id)}, "arrived_at": {"S": META_SORT_KEY}},
                            "UpdateExpression": (
                                "SET command_sequence = :sequence, event_sequence = :event_sequence, version = :next_version, updated_at = :now"
                            ),
                            # Same META version fence as a state transition, which
                            # is what makes "completion cannot bypass an input
                            # already committed to the next turn" hold.
                            "ConditionExpression": "version = :expected_version AND command_sequence = :prior",
                            "ExpressionAttributeValues": {
                                k: _SERIALIZER.serialize(v)
                                for k, v in {
                                    ":sequence": sequence,
                                    ":prior": sequence - 1,
                                    ":event_sequence": event_sequence,
                                    ":next_version": expected_version + 1,
                                    ":expected_version": expected_version,
                                    ":now": now_iso,
                                }.items()
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _serialize(event),
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                ]
            )
        except (ClientError, BotoCoreError) as exc:
            if not _is_conditional_failure(exc):
                raise TaskStoreError("task store unavailable") from exc
            # Confirm from storage: a concurrent identical insert may have won.
            committed = self._get(task_commands_partition(task_id), command_sort_key(command_id))
            if committed is not None and str(committed.get("command_digest")) == digest:
                return committed
            live = self.read_task(task_id)
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=str(live.get("state")) if live else None,
                current_version=int(live.get("version", 0)) if live else None,
            ) from None

        return command

    def commit_turn(
        self,
        *,
        task_id: str,
        turn_number: int,
        turn_id: str,
        command_ids: list[str],
        expected_version: int,
    ) -> dict[str, Any]:
        """Create one immutable turn and mark its commands consumed, atomically.

        This is the design's explicit consumption boundary: each command appears
        in exactly one turn, once. The turn row is written under
        ``attribute_not_exists``, so recovery after a crash **reads the existing
        turn** instead of inserting those commands into a second turn.

        Marking each command consumed in the same transaction is what makes the
        boundary checkable: there is no window where a turn exists but its
        commands still look pending, or where a command is consumed twice.
        """
        validate_uuid(turn_id, "turn_id")
        if not command_ids:
            raise TaskStoreError("a turn must contain at least one command")

        existing = self._get(task_turns_partition(task_id), turn_sort_key(turn_number))
        if existing is not None:
            # Idempotent recovery. Returning the committed turn is what stops a
            # retry from re-inserting the same commands.
            return existing

        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)

        now_iso = _iso(self._clock())
        scope = dict(snapshot.get("scope") or {})
        turn = base_item(
            partition=task_turns_partition(task_id),
            sort_key=turn_sort_key(turn_number),
            record_type="TASK_TURNS",
            scope=scope,
        ) | {
            "task_id": task_id,
            "turn_number": turn_number,
            "turn_id": turn_id,
            "command_ids": list(command_ids),
            "created_at": now_iso,
        }

        transact: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(turn),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": {"event_id": {"S": task_partition(task_id)}, "arrived_at": {"S": META_SORT_KEY}},
                    "UpdateExpression": "SET turn_count = :turn, version = :next_version, updated_at = :now",
                    "ConditionExpression": "version = :expected_version",
                    "ExpressionAttributeValues": {
                        k: _SERIALIZER.serialize(v)
                        for k, v in {
                            ":turn": turn_number,
                            ":next_version": expected_version + 1,
                            ":expected_version": expected_version,
                            ":now": now_iso,
                        }.items()
                    },
                }
            },
        ]

        for command_id in command_ids:
            transact.append(
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": {
                            "event_id": {"S": task_commands_partition(task_id)},
                            "arrived_at": {"S": command_sort_key(command_id)},
                        },
                        "UpdateExpression": "SET #status = :consumed, turn_number = :turn, updated_at = :now",
                        # Only a command still in `accepted` may be consumed, so a
                        # command already consumed by an earlier turn fails here
                        # and cannot be assigned to a second turn. This is the
                        # durable half of "each command appears in exactly one
                        # turn, once".
                        "ConditionExpression": "#status = :accepted",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":consumed": {"S": "consumed"},
                            ":accepted": {"S": "accepted"},
                            ":turn": _SERIALIZER.serialize(turn_number),
                            ":now": {"S": now_iso},
                        },
                    }
                }
            )

        try:
            self._client.transact_write_items(TransactItems=transact)
        except (ClientError, BotoCoreError) as exc:
            if not _is_conditional_failure(exc):
                raise TaskStoreError("task store unavailable") from exc
            committed = self._get(task_turns_partition(task_id), turn_sort_key(turn_number))
            if committed is not None:
                return committed
            live = self.read_task(task_id)
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=str(live.get("state")) if live else None,
                current_version=int(live.get("version", 0)) if live else None,
            ) from None

        return turn

    def read_commands(self, *, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """All commands for a task, bounded. Callers order by ``command_sequence``."""
        try:
            response = self._client.query(
                TableName=self._table_name,
                KeyConditionExpression="event_id = :partition",
                ExpressionAttributeValues={":partition": {"S": task_commands_partition(task_id)}},
                Limit=limit,
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError) as exc:
            raise TaskStoreError("task store unavailable") from exc
        return [_deserialize(item) for item in response.get("Items", [])]

    # -- recovery and retention --------------------------------------------

    def claim_due_work(self, *, shard: str, now: datetime, limit: int = 100, lease_seconds: int = 45) -> list[dict[str, Any]]:
        """Query one shard of the sparse index for due work and lease each item.

        Two deliberate properties:

        * **A query, never a scan.** Only work records carry the index attributes,
          so the index holds outstanding work rather than 30 days of webhook
          deliveries. The design forbids a table scan outright.
        * **Index lag cannot authorise a mutation.** The index is
          eventually consistent, so each hit is re-read consistently from the base
          table and claimed with a conditional lease. A stale index entry
          therefore wastes a read instead of double-dispatching work.
        """
        if limit < 1 or limit > 100:
            # The design bounds recovery at 100 work records per invocation.
            raise TaskStoreError("limit must be between 1 and 100")
        try:
            response = self._client.query(
                TableName=self._table_name,
                IndexName=WORK_INDEX_NAME,
                KeyConditionExpression=f"{WORK_SHARD_ATTRIBUTE} = :shard AND {WORK_DUE_ATTRIBUTE} <= :due",
                ExpressionAttributeValues={
                    ":shard": {"S": shard},
                    ":due": {"S": work_due_key(due_at=now, work_id=_WORK_ID_MAX)},
                },
                Limit=limit,
            )
        except (ClientError, BotoCoreError) as exc:
            raise TaskStoreError("task store unavailable") from exc

        claimed: list[dict[str, Any]] = []
        for projected in response.get("Items", []):
            item = _deserialize(projected)
            partition, sort_key = item.get("event_id"), item.get("arrived_at")
            if not partition or not sort_key:
                continue
            record = self._get(str(partition), str(sort_key))
            if record is None:
                continue  # Index lag: the record is gone. Nothing to claim.
            if self._lease(partition=str(partition), sort_key=str(sort_key), now=now, lease_seconds=lease_seconds):
                claimed.append(record)
        return claimed

    def _lease(self, *, partition: str, sort_key: str, now: datetime, lease_seconds: int) -> bool:
        """Take a time-bounded lease, or return False if another holder is live.

        The condition permits a claim only when no lease exists or the existing one
        has expired, so two concurrent recovery invocations cannot both act on one
        work record. A lease expiring is not by itself evidence that the previous
        holder stopped — the design is explicit that replacing a worker needs
        proven termination, which is T3/T4's fence, not this lease.
        """
        expiry = _iso(now + timedelta(seconds=lease_seconds))
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"event_id": {"S": partition}, "arrived_at": {"S": sort_key}},
                UpdateExpression="SET lease_expires_at = :expiry, lease_taken_at = :now",
                ConditionExpression="attribute_not_exists(lease_expires_at) OR lease_expires_at < :now",
                ExpressionAttributeValues={":expiry": {"S": expiry}, ":now": {"S": _iso(now)}},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc
        return True

    def mark_recovery_required(self, *, task_id: str, reason: str, expected_version: int) -> None:
        """Flag a task as needing intervention and remove any expiry stamp.

        ``REMOVE expires_at`` is the load-bearing half. The design requires that
        recovery-required tasks are retained until explicit settlement, and TTL
        deletion is asynchronous — so a task that became uncertain after
        terminalisation must have its expiry cleared, or the evidence needed to
        settle it can be deleted while the question is still open.
        """
        now_iso = _iso(self._clock())
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"event_id": {"S": task_partition(task_id)}, "arrived_at": {"S": META_SORT_KEY}},
                UpdateExpression=(
                    f"SET recovery_required = :true, recovery_reason = :reason, "
                    f"execution_health = :unknown, version = :next, updated_at = :now REMOVE {TTL_ATTRIBUTE}"
                ),
                ConditionExpression="version = :expected",
                ExpressionAttributeValues={
                    ":true": {"BOOL": True},
                    ":reason": {"S": reason},
                    ":unknown": {"S": "unknown"},
                    ":next": _SERIALIZER.serialize(expected_version + 1),
                    ":expected": _SERIALIZER.serialize(expected_version),
                    ":now": {"S": now_iso},
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                live = self.read_task(task_id)
                raise TaskStateConflictError(
                    task_id=task_id,
                    current_state=str(live.get("state")) if live else None,
                    current_version=int(live.get("version", 0)) if live else None,
                ) from None
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc

    def tombstone_expiry(self, terminal_at: datetime) -> int:
        """Epoch seconds at which a content-free tombstone may be deleted.

        Content goes at day 30 but the tombstone survives to day 90, so a key
        replayed in between returns ``410 history_expired`` rather than creating a
        fresh task. Deleting the tombstone at day 30 would silently turn a replay
        into a new run.
        """
        return int((terminal_at + timedelta(days=TOMBSTONE_RETENTION_DAYS)).timestamp())


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def assert_ttl_permitted(item: dict[str, Any]) -> dict[str, Any]:
    """Refuse an expiry stamp on a record that must outlive the active task.

    Enforced on every write rather than left to reviewer discipline, because the
    failure it prevents is silent and delayed: an ``expires_at`` on a live task's
    metadata or idempotency row does nothing visible for days, then deletes the
    record mid-flight — a running task losing its metadata, or an idempotency key
    freed while its task still runs so a retry starts a second execution.
    ``terminal_at`` is the marker that a record has reached a terminal outcome and
    may therefore carry a retention deadline.
    """
    if TTL_ATTRIBUTE in item and item.get("record_type") in TTL_EXEMPT_WHILE_ACTIVE and not item.get("terminal_at"):
        raise TaskStoreError(f"{item.get('record_type')} records must not carry {TTL_ATTRIBUTE} before a terminal outcome")
    return item


def _serialize(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a plain dict to DynamoDB descriptors, re-checking both invariants.

    The legacy-attribute and TTL checks run here as well as at construction
    because this is the last point before the bytes reach DynamoDB: a caller that
    merged extra attributes onto a built item would otherwise bypass the earlier
    check and either project a task record into an Activity view or schedule a live
    record for deletion.
    """
    assert_legacy_invisible(item)
    assert_ttl_permitted(item)
    return {key: _SERIALIZER.serialize(value) for key, value in item.items()}


def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
    """Read an item back, normalising DynamoDB's ``Decimal`` numbers to ``int``.

    DynamoDB returns every number as ``Decimal``. Every number this module stores
    is a whole number — sequences, versions, generations, turn numbers, epoch
    seconds — and they are fed straight back into fixed-width key builders and
    conditional writes that require ``int``. Normalising once here keeps a value
    read from storage usable as a key or a condition operand, rather than failing
    at the point of use. Non-integral values are left as ``Decimal`` so a caller's
    payload is never silently coerced.
    """
    return {key: _normalise_numbers(_DESERIALIZER.deserialize(value)) for key, value in item.items()}


def _normalise_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else value
    if isinstance(value, dict):
        return {key: _normalise_numbers(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_normalise_numbers(inner) for inner in value]
    return value


def _is_conditional_failure(exc: Exception) -> bool:
    """True when a failure is a refused condition rather than a storage fault.

    The distinction decides whether a caller gets a definitive conflict or a
    retryable ``503``, so a transport error must never be read as "your condition
    failed" — that would report a conflict that was never actually observed.
    """
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code")
    if code == "ConditionalCheckFailedException":
        return True
    if code != "TransactionCanceledException":
        return False
    return any(reason == "ConditionalCheckFailed" for reason in _cancellation_reasons(exc))


def _cancellation_reasons(exc: ClientError) -> list[str]:
    """Per-item cancellation codes, positionally parallel to ``TransactItems``."""
    reasons = exc.response.get("CancellationReasons")
    if not isinstance(reasons, list):
        return []
    return [str(reason.get("Code", "")) if isinstance(reason, dict) else "" for reason in reasons]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CONTENT_RETENTION_DAYS",
    "TOMBSTONE_RETENTION_DAYS",
    "TTL_ATTRIBUTE",
    "TTL_EXEMPT_WHILE_ACTIVE",
    "AcceptanceRequest",
    "AcceptedTask",
    "IdempotencyConflictError",
    "StaleGenerationError",
    "TaskStateConflictError",
    "TaskStore",
    "TaskStoreError",
    "assert_ttl_permitted",
]
