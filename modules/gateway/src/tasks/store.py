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
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.tasks.json_storage import (
    JSON_ENCODING_FIELD,
    decode_record_json,
    encode_json_updates,
    encode_json_value,
    encode_record_json,
)
from src.tasks.records import (
    META_SORT_KEY,
    TASK_WORK_LOCATOR_SORT_KEY,
    WORK_DUE_ATTRIBUTE,
    WORK_INDEX_NAME,
    WORK_SHARD_ATTRIBUTE,
    TaskState,
    TaskTransitionError,
    assert_legacy_invisible,
    base_item,
    canonical_json,
    command_sort_key,
    component_digest,
    dispatch_sort_key,
    event_sort_key,
    idempotency_partition,
    is_terminal,
    payload_digest,
    report_sort_key,
    run_sort_key,
    task_artifact_partition,
    task_authority_partition,
    task_binding_sort_key,
    task_capacity_partition,
    task_commands_partition,
    task_events_partition,
    task_ops_partition,
    task_partition,
    task_policy_sort_key,
    task_report_partition,
    task_run_grant_sort_key,
    task_run_partition,
    task_turns_partition,
    task_work_locator_partition,
    task_work_partition,
    turn_sort_key,
    validate_artifact_id,
    validate_task_id,
    validate_transition,
    validate_uuid,
    work_due_key,
    work_shard,
)

logger = logging.getLogger(__name__)

WEBHOOK_EVENTS_TABLE_ENV: Final = "WEBHOOK_EVENTS_TABLE"
AUTHORITY_TABLE_ENV: Final = "AGENT_AUTHORITY_TABLE"
_DEFAULT_TABLE_NAME: Final = "adp-dev-webhook-events"
_DEFAULT_AUTHORITY_TABLE_NAME: Final = "adp-dev-agent-authority"

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
TTL_EXEMPT_WHILE_ACTIVE: Final = frozenset({"TASK", "TASK_RUN", "TASK_IDEMP", "TASK_COMMANDS", "TASK_ARTIFACT", "TASK_WORK"})

WORK_LEASE_SECONDS: Final = 45
MAX_WORK_PER_CLAIM: Final = 100
MAX_TASK_EVENTS: Final = 10_000
FINAL_EVENT_RESERVE: Final = 100
MAX_COMMAND_BYTES: Final = 16_384
MAX_PENDING_INPUTS: Final = 10
MAX_INPUT_COMMANDS: Final = 100
_REPORT_EVENT_TYPES: Final = frozenset(
    {
        "run.started",
        "progress.updated",
        "artifact.created",
        "input.required",
        "input.accepted",
        "input.consumed",
        "input.rejected",
        "command.updated",
        "cancel.requested",
        "run.completed",
        "run.failed",
        "task.completed",
        "task.failed",
        "task.cancelled",
        "history.gap",
    }
)
_TERMINAL_REPORT_EVENT_TYPES: Final = frozenset({"run.completed", "run.failed", "task.completed", "task.failed", "task.cancelled"})
_ATTEMPT_PROTECTED_TRANSITION_ATTRIBUTES: Final = frozenset({"result", "result_ref", "error", "error_code", "outcome"})
_TRANSITION_MUTABLE_ATTRIBUTES: Final = frozenset(
    {
        "execution_health",
        "queue_ack_status",
        "result",
        "result_ref",
        "error",
        "error_code",
        "outcome",
        "input_request",
        "recovery_required",
        "recovery_reason",
        "started_at",
        "completed_at",
        "heartbeat_at",
    }
)
_ENVELOPE_FIELDS: Final = frozenset(
    {"kind", "schema_version", "task_id", "invocation_id", "message_id", "persona", "dispatch_id", "request_digest", "input_ref", "assignment_ref"}
)

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


class AcceptanceConditionError(TaskStoreError):
    """A durable policy, capacity, or artifact acceptance condition refused."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


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


class WorkBindingError(Exception):
    """A work locator, immutable envelope, or authority binding disagrees."""


class WorkLeaseConflictError(Exception):
    """A work claim/settlement lost its independent lease fence."""


class StaleAttemptError(Exception):
    """A worker-originated write did not match the active runtime attempt."""


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
    envelope: dict[str, Any]
    immutable_input: dict[str, Any]
    model_binding: dict[str, Any]
    run_limits: dict[str, Any]
    policy_version: int
    capacity_scope_hash: str
    capacity_limit: int
    capacity_reservation_id: str
    generation: int = 1
    input_reference: dict[str, Any] | None = None
    artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    budget_reservation: dict[str, Any] | None = None
    submit_rate_scope_hash: str | None = None
    submit_rate_window_end: int | None = None


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

    def __init__(self, *, table_name: str | None = None, authority_table_name: str | None = None, dynamodb_client=None, clock=None) -> None:
        self._table_name = table_name or os.environ.get(WEBHOOK_EVENTS_TABLE_ENV, _DEFAULT_TABLE_NAME)
        self._authority_table_name = authority_table_name or os.environ.get(AUTHORITY_TABLE_ENV, _DEFAULT_AUTHORITY_TABLE_NAME)
        self._client = dynamodb_client or boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        # Injected so retention and deadline behaviour is testable without sleeping.
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def authority_table_name(self) -> str:
        return self._authority_table_name

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
        if request.policy_version < 1:
            raise TaskStoreError("policy_version must be a positive integer")
        if request.capacity_limit < 1:
            raise TaskStoreError("capacity_limit must be a positive integer")
        validate_uuid(request.capacity_reservation_id, "capacity_reservation_id")
        if request.deadline_at.tzinfo is None:
            raise TaskStoreError("deadline_at must be timezone-aware")
        if len(canonical_json(request.request_payload)) > 65_536:
            raise TaskStoreError("task request payload exceeds 65536 bytes")
        _validate_run_bindings(request)

        digest = payload_digest(request.request_payload)
        now = self._clock()
        now_iso = _iso(now)
        idempotency_pk = idempotency_partition(
            tenant=request.tenant,
            canonical_principal=request.canonical_principal,
            idempotency_key=request.idempotency_key,
        )

        try:
            self._client.transact_write_items(
                TransactItems=self._acceptance_items(request, digest, idempotency_pk, now),
                ClientRequestToken=component_digest("task-acceptance-token-v1", idempotency_pk, digest)[:36],
            )
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

    def _acceptance_items(self, request: AcceptanceRequest, digest: str, idempotency_pk: str, now: datetime) -> list[dict[str, Any]]:
        """Compose the acceptance transaction.

        This is the complete cross-table transaction.  T3 supplies already
        authenticated values through :class:`AcceptanceRequest`; it must not append
        a second transaction after this one.
        """
        now_iso = _iso(now)
        scope = {"tenant": request.tenant, "canonical_principal": request.canonical_principal}
        deadline_iso = _iso(request.deadline_at)
        envelope = _validated_envelope(request, digest)
        envelope_digest = payload_digest(envelope)
        authority_pk = task_authority_partition(request.tenant)
        run_grant_sk = task_run_grant_sort_key(invocation_id=request.invocation_id, generation=request.generation)

        metadata = base_item(
            partition=task_partition(request.task_id),
            sort_key=META_SORT_KEY,
            record_type="TASK",
            scope=scope,
        ) | {
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "dispatch_id": request.dispatch_id,
            "state": TaskState.ACCEPTED.value,
            # Starts at 1 and is the compare-and-swap fence for every later
            # transition. Never reset, so a stale writer's expected version can
            # never coincidentally match again.
            "version": 1,
            "generation": request.generation,
            "persona": request.persona,
            "policy_version": request.policy_version,
            "request_digest": digest,
            "input_payload": request.request_payload,
            "idempotency_partition": idempotency_pk,
            "grant_reference": request.grant_reference,
            "capacity_scope_hash": request.capacity_scope_hash,
            "capacity_reservation_id": request.capacity_reservation_id,
            # Sequence 1 is consumed by the task.accepted event in this same
            # transaction, so the counter starts consistent with durable history.
            "event_sequence": 1,
            "command_sequence": 0,
            "pending_input_count": 0,
            "input_command_count": 0,
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
            "generation": request.generation,
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
            data={"status": TaskState.ACCEPTED.value, "version": 1},
        )

        dispatch = base_item(
            partition=task_work_partition(request.task_id),
            sort_key=dispatch_sort_key(request.dispatch_id),
            record_type="TASK_WORK",
            scope=scope,
        ) | {
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "generation": request.generation,
            "dispatch_id": request.dispatch_id,
            "work_id": request.dispatch_id,
            "work_kind": "dispatch",
            "request_digest": digest,
            "envelope": envelope,
            "envelope_digest": envelope_digest,
            "publication_state": "pending",
            "publication_tries": 0,
            "publication_deadline_at": _iso(min(request.deadline_at, now + timedelta(minutes=10))),
            "created_at": now_iso,
            "updated_at": now_iso,
            # Sparse recovery index attributes. Only work records carry them, so
            # the index holds outstanding work rather than the whole table.
            WORK_SHARD_ATTRIBUTE: work_shard(request.task_id),
            WORK_DUE_ATTRIBUTE: work_due_key(due_at=now, work_id=request.dispatch_id),
            "due_at": now_iso,
        }

        locator = {
            "pk": task_work_locator_partition(request.dispatch_id),
            "sk": TASK_WORK_LOCATOR_SORT_KEY,
            "record_type": "TASK_WORK_BINDING",
            "schema_version": "1.0",
            "work_id": request.dispatch_id,
            "work_kind": "dispatch",
            "tenant": request.tenant,
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "generation": request.generation,
            "work_event_id": dispatch["event_id"],
            "work_arrived_at": dispatch["arrived_at"],
            "request_digest": digest,
            "envelope_digest": envelope_digest,
            "binding_state": "active",
            "created_at": now_iso,
        }
        task_binding = {
            "pk": authority_pk,
            "sk": task_binding_sort_key(request.task_id),
            "record_type": "TASK_BINDING",
            "schema_version": "1.0",
            "tenant": request.tenant,
            "canonical_principal": request.canonical_principal,
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "generation": request.generation,
            "request_digest": digest,
            "grant_sk": run_grant_sk,
            "policy_version": request.policy_version,
            "persona": request.persona,
            "status": "active",
            "created_at": now_iso,
        }
        run_grant = {
            "pk": authority_pk,
            "sk": run_grant_sk,
            "record_type": "TASK_RUN_GRANT",
            "schema_version": "1.0",
            "tenant": request.tenant,
            "canonical_principal": request.canonical_principal,
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "generation": request.generation,
            "request_digest": digest,
            "status": "active",
            "persona": request.persona,
            "input": request.immutable_input,
            "model_binding": request.model_binding,
            "limits": request.run_limits,
            "capabilities": ["input", "cancel"],
            "runtime_attempt_id": None,
            "created_at": now_iso,
        }
        if request.budget_reservation is not None:
            if request.budget_reservation.get("status") != "reserved" or not request.budget_reservation.get("reservation_id"):
                raise TaskStoreError("acceptance requires a confirmed budget reservation")
            run_grant["budget_reservation"] = request.budget_reservation
            metadata["budget_reservation"] = request.budget_reservation

        grant_digest = _protected_grant_digest(run_grant)
        metadata["grant_digest"] = grant_digest
        dispatch["grant_digest"] = grant_digest
        locator["grant_digest"] = grant_digest
        task_binding["grant_digest"] = grant_digest

        transaction = [
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
            {
                "Put": {
                    "TableName": self._authority_table_name,
                    "Item": _serialize_authority(locator),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Put": {
                    "TableName": self._authority_table_name,
                    "Item": _serialize_authority(task_binding),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Put": {
                    "TableName": self._authority_table_name,
                    "Item": _serialize_authority(run_grant),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "ConditionCheck": {
                    "TableName": self._authority_table_name,
                    "Key": _serialize_authority({"pk": authority_pk, "sk": task_policy_sort_key(request.canonical_principal)}),
                    "ConditionExpression": "#status = :active AND #version = :version AND contains(personas, :persona)",
                    "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                    "ExpressionAttributeValues": _serialize_authority(
                        {":active": "active", ":version": request.policy_version, ":persona": request.persona}
                    ),
                }
            },
            {
                "Update": {
                    "TableName": self._authority_table_name,
                    "Key": _serialize_authority({"pk": task_capacity_partition(request.capacity_scope_hash), "sk": "ACTIVE"}),
                    "UpdateExpression": "SET reservations.#task = :reservation, updated_at = :now ADD active_count :one",
                    "ConditionExpression": (
                        "attribute_exists(pk) AND capacity_limit = :limit AND "
                        "attribute_not_exists(reservations.#task) AND active_count < capacity_limit"
                    ),
                    "ExpressionAttributeNames": {"#task": request.task_id},
                    "ExpressionAttributeValues": _serialize_authority(
                        {
                            ":reservation": request.capacity_reservation_id,
                            ":now": now_iso,
                            ":one": 1,
                            ":limit": request.capacity_limit,
                        }
                    ),
                }
            },
        ]

        if request.submit_rate_scope_hash is not None:
            if request.submit_rate_window_end is None:
                raise TaskStoreError("submit rate window is missing")
            transaction.append(
                {
                    "Update": {
                        "TableName": self._authority_table_name,
                        "Key": _serialize_authority({"pk": task_capacity_partition(request.submit_rate_scope_hash), "sk": "ACTIVE"}),
                        "UpdateExpression": "SET expires_at = :expires ADD request_count :one",
                        "ConditionExpression": "attribute_not_exists(request_count) OR request_count < :max",
                        "ExpressionAttributeValues": _serialize_authority({":expires": request.submit_rate_window_end, ":one": 1, ":max": 10}),
                    }
                }
            )

        transaction.extend(self._artifact_claim_items(request=request, now_iso=now_iso))
        if request.budget_reservation and request.budget_reservation.get("authority_pk"):
            reservation = request.budget_reservation
            transaction.append(
                {
                    "Update": {
                        "TableName": self._authority_table_name,
                        "Key": _serialize_authority({"pk": reservation["authority_pk"], "sk": reservation["authority_sk"]}),
                        "UpdateExpression": "SET #state = :committed, task_id = :task",
                        "ConditionExpression": "#state = :preparing AND owner_token = :owner AND lease_expires_at >= :now",
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": _serialize_authority(
                            {
                                ":committed": "committed",
                                ":preparing": "preparing",
                                ":task": request.task_id,
                                ":owner": reservation["owner_token"],
                                ":now": int(self._clock().timestamp()),
                            }
                        ),
                    }
                }
            )
        return transaction

    def _artifact_claim_items(self, *, request: AcceptanceRequest, now_iso: str) -> list[dict[str, Any]]:
        refs = request.envelope.get("input_ref", {}).get("artifact_refs", [])
        if not isinstance(refs, list) or len(refs) > 4:
            raise TaskStoreError("an acceptance may reference at most four artifacts")
        ref_ids = [ref.get("artifact_id") for ref in refs if isinstance(ref, dict)]
        if len(ref_ids) != len(refs) or set(ref_ids) != set(request.artifact_ids) or len(ref_ids) != len(set(ref_ids)):
            raise TaskStoreError("artifact_ids must exactly match unique immutable envelope references")

        claims: list[dict[str, Any]] = []
        now_epoch = int(datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        for ref in refs:
            artifact_id = str(ref["artifact_id"])
            claims.append(
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": _serialize_authority({"event_id": task_artifact_partition(artifact_id), "arrived_at": META_SORT_KEY}),
                        "UpdateExpression": "SET task_id = :task, binding_state = :bound, bound_at = :now REMOVE expires_at",
                        "ConditionExpression": (
                            "attribute_exists(event_id) AND attribute_not_exists(task_id) AND "
                            "binding_state = :unclaimed AND expires_at > :now_epoch AND "
                            "#scope.#tenant = :tenant AND #scope.#principal = :principal AND "
                            "#version = :version AND content_sha256 = :digest AND content_type = :content_type"
                        ),
                        "ExpressionAttributeNames": {
                            "#tenant": "tenant",
                            "#principal": "canonical_principal",
                            "#version": "version",
                            "#scope": "scope",
                        },
                        "ExpressionAttributeValues": _serialize_authority(
                            {
                                ":task": request.task_id,
                                ":bound": "bound",
                                ":unclaimed": "unclaimed",
                                ":now": now_iso,
                                ":now_epoch": now_epoch,
                                ":tenant": request.tenant,
                                ":principal": request.canonical_principal,
                                ":version": int(ref["version"]),
                                ":digest": str(ref["content_sha256"]),
                                ":content_type": str(
                                    next(
                                        artifact["content_type"]
                                        for artifact in request.immutable_input.get("artifacts", [])
                                        if artifact["artifact_id"] == artifact_id
                                    )
                                ),
                            }
                        ),
                    }
                }
            )
        return claims

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
            reasons = _cancellation_reasons(exc) if isinstance(exc, ClientError) else []
            if len(reasons) > 8 and reasons[8] == "ConditionalCheckFailed":
                raise AcceptanceConditionError("task policy no longer permits acceptance") from None
            if len(reasons) > 9 and reasons[9] == "ConditionalCheckFailed":
                raise AcceptanceConditionError("task capacity is exhausted or changed") from None
            artifact_start = 10
            if request.submit_rate_scope_hash is not None:
                if len(reasons) > 10 and reasons[10] == "ConditionalCheckFailed":
                    raise AcceptanceConditionError("task submission rate is exhausted") from None
                artifact_start = 11
            if any(reason == "ConditionalCheckFailed" for reason in reasons[artifact_start:]):
                raise AcceptanceConditionError("artifact ownership, digest, version, or expiry was refused") from None
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

        try:
            self.resolve_work(str(existing.get("dispatch_id", "")), expected_kind="dispatch")
        except (TaskStoreError, WorkBindingError):
            raise TaskStoreError("task acceptance binding is incomplete or inconsistent") from None

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

    def _get_authority(self, partition: str, sort_key: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_item(
                TableName=self._authority_table_name,
                Key=_serialize_authority({"pk": partition, "sk": sort_key}),
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError) as exc:
            raise TaskStoreError("task authority store unavailable") from exc
        item = response.get("Item")
        return _deserialize(item) if item else None

    def resolve_work(self, work_id: str, *, expected_kind: str | None = None) -> dict[str, Any]:
        """Resolve a work UUID through its protected locator and verify all bindings."""
        validate_uuid(work_id, "work_id")
        locator = self._get_authority(task_work_locator_partition(work_id), TASK_WORK_LOCATOR_SORT_KEY)
        required_locator = {
            "work_id",
            "work_kind",
            "tenant",
            "task_id",
            "invocation_id",
            "generation",
            "grant_digest",
            "work_event_id",
            "work_arrived_at",
            "binding_state",
        }
        if locator is None or not required_locator.issubset(locator) or locator.get("binding_state") != "active":
            raise WorkBindingError("work locator is missing, incomplete, stale, or revoked")
        if locator.get("work_id") != work_id or (expected_kind is not None and locator.get("work_kind") != expected_kind):
            raise WorkBindingError("work locator kind or identity mismatch")

        task_id = str(locator["task_id"])
        invocation_id = str(locator["invocation_id"])
        generation = int(locator["generation"])
        tenant = str(locator["tenant"])
        validate_task_id(task_id)
        validate_uuid(invocation_id, "invocation_id")
        expected_partition = task_work_partition(task_id)
        expected_sort_key = dispatch_sort_key(work_id) if locator["work_kind"] == "dispatch" else "RECONCILE"
        if locator["work_event_id"] != expected_partition or locator["work_arrived_at"] != expected_sort_key:
            raise WorkBindingError("work locator target does not match its immutable key layout")

        work = self._get(expected_partition, expected_sort_key)
        required_work = {"record_type", "work_id", "work_kind", "task_id", "invocation_id", "generation", "grant_digest", "scope"}
        if work is None or not required_work.issubset(work) or work.get("record_type") != "TASK_WORK":
            raise WorkBindingError("located work record is missing or incomplete")
        for attribute_name in ("work_id", "work_kind", "task_id", "invocation_id", "generation", "grant_digest"):
            if work.get(attribute_name) != locator.get(attribute_name):
                raise WorkBindingError("work record disagrees with its protected locator")
        scope = work.get("scope")
        if not isinstance(scope, dict) or scope.get("tenant") != tenant or not scope.get("canonical_principal"):
            raise WorkBindingError("work scope disagrees with its protected locator")

        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise WorkBindingError("work task metadata is missing")
        for attribute_name, expected in {
            "task_id": task_id,
            "invocation_id": invocation_id,
            "generation": generation,
        }.items():
            if snapshot.get(attribute_name) != expected:
                raise WorkBindingError("task metadata disagrees with work locator")
        if snapshot.get("scope") != scope:
            raise WorkBindingError("task ownership disagrees with work scope")

        authority_pk = task_authority_partition(tenant)
        binding = self._get_authority(authority_pk, task_binding_sort_key(task_id))
        required_binding = {
            "tenant",
            "canonical_principal",
            "task_id",
            "invocation_id",
            "generation",
            "request_digest",
            "policy_version",
            "persona",
            "status",
            "grant_digest",
        }
        if binding is None or not required_binding.issubset(binding) or binding.get("status") != "active":
            raise WorkBindingError("task binding is missing, incomplete, or inactive")
        for attribute_name, expected in {
            "tenant": tenant,
            "canonical_principal": scope["canonical_principal"],
            "task_id": task_id,
            "invocation_id": invocation_id,
            "generation": generation,
            "request_digest": snapshot.get("request_digest"),
            "policy_version": snapshot.get("policy_version"),
            "persona": snapshot.get("persona"),
            "grant_digest": snapshot.get("grant_digest"),
        }.items():
            if binding.get(attribute_name) != expected:
                raise WorkBindingError("task binding disagrees with work or task metadata")

        policy = self._get_authority(authority_pk, task_policy_sort_key(str(binding["canonical_principal"])))
        if (
            policy is None
            or policy.get("status") != "active"
            or int(policy.get("version", 0)) != int(binding["policy_version"])
            or binding["persona"] not in policy.get("personas", set())
        ):
            raise WorkBindingError("current task policy no longer permits this work")

        grant_sk = task_run_grant_sort_key(invocation_id=invocation_id, generation=generation)
        grant = self._get_authority(authority_pk, grant_sk)
        required_grant = {
            "tenant",
            "canonical_principal",
            "task_id",
            "invocation_id",
            "generation",
            "request_digest",
            "persona",
            "input",
            "status",
        }
        if grant is None or not required_grant.issubset(grant) or grant.get("status") != "active":
            raise WorkBindingError("task run grant is missing, incomplete, or inactive")
        for attribute_name in ("tenant", "canonical_principal", "task_id", "invocation_id", "generation", "request_digest", "persona"):
            if grant.get(attribute_name) != binding.get(attribute_name):
                raise WorkBindingError("task run grant disagrees with task binding")
        grant_digest = _protected_grant_digest(grant)
        if grant_digest != work["grant_digest"] or grant_digest != locator["grant_digest"] or grant_digest != binding["grant_digest"]:
            raise WorkBindingError("protected run grant digest disagrees with work binding")

        if work["work_kind"] == "dispatch":
            required_dispatch = {"dispatch_id", "request_digest", "envelope", "envelope_digest"}
            if not required_dispatch.issubset(work) or work.get("dispatch_id") != work_id:
                raise WorkBindingError("dispatch work identity is incomplete or mismatched")
            if locator.get("request_digest") != work.get("request_digest") or locator.get("envelope_digest") != work.get("envelope_digest"):
                raise WorkBindingError("dispatch locator digest disagrees with work")
            envelope = work["envelope"]
            if not isinstance(envelope, dict) or payload_digest(envelope) != work["envelope_digest"]:
                raise WorkBindingError("dispatch envelope digest mismatch")
            expected_envelope = {
                "task_id": task_id,
                "invocation_id": invocation_id,
                "message_id": invocation_id,
                "dispatch_id": work_id,
                "request_digest": binding["request_digest"],
                "persona": binding["persona"],
            }
            if any(envelope.get(attribute_name) != value for attribute_name, value in expected_envelope.items()):
                raise WorkBindingError("dispatch envelope disagrees with protected identity")
            assignment = envelope.get("assignment_ref")
            expected_assignment = {"grant_pk": authority_pk, "grant_sk": grant_sk, "generation": generation}
            if assignment != expected_assignment:
                raise WorkBindingError("dispatch envelope assignment is stale or mismatched")
            if envelope.get("input_ref", {}).get("input_digest") != grant.get("input", {}).get("input_digest"):
                raise WorkBindingError("dispatch envelope input disagrees with protected run input")
        return work

    def _authority_condition_checks(
        self,
        *,
        snapshot: dict[str, Any],
        runtime_attempt_id: str | None = None,
        required_capability: str | None = None,
        required_turn_number: int | None = None,
    ) -> list[dict[str, Any]]:
        """Build exact current-policy/task/run fences for a cross-table mutation."""
        scope = snapshot.get("scope")
        if not isinstance(scope, dict) or not scope.get("tenant") or not scope.get("canonical_principal"):
            raise WorkBindingError("task ownership scope is incomplete")
        tenant = str(scope["tenant"])
        principal = str(scope["canonical_principal"])
        task_id = str(snapshot["task_id"])
        invocation_id = str(snapshot["invocation_id"])
        generation = int(snapshot["generation"])
        authority_pk = task_authority_partition(tenant)
        grant_sk = task_run_grant_sort_key(invocation_id=invocation_id, generation=generation)
        grant = self._get_authority(authority_pk, grant_sk)
        if grant is None or _protected_grant_digest(grant) != snapshot.get("grant_digest"):
            raise WorkBindingError("protected run grant digest disagrees with task metadata")
        common_values = {
            ":active": "active",
            ":tenant": tenant,
            ":principal": principal,
            ":task": task_id,
            ":invocation": invocation_id,
            ":generation": generation,
            ":digest": str(snapshot["request_digest"]),
            ":persona": str(snapshot["persona"]),
            ":policy_version": int(snapshot["policy_version"]),
            ":grant_digest": str(snapshot["grant_digest"]),
        }
        grant_condition = (
            "#status = :active AND tenant = :tenant AND canonical_principal = :principal AND "
            "task_id = :task AND invocation_id = :invocation AND generation = :generation AND "
            "request_digest = :digest AND persona = :persona AND #input = :grant_input AND "
            "#model_binding = :grant_model_binding AND #limits = :grant_limits AND #capabilities = :grant_capabilities"
        )
        grant_values = {key: value for key, value in common_values.items() if key not in {":policy_version", ":grant_digest"}} | {
            ":grant_input": grant["input"],
            ":grant_model_binding": grant["model_binding"],
            ":grant_limits": grant["limits"],
            ":grant_capabilities": grant["capabilities"],
        }
        if runtime_attempt_id is not None:
            validate_uuid(runtime_attempt_id, "runtime_attempt_id")
            grant_condition += " AND runtime_attempt_id = :attempt"
            grant_values[":attempt"] = runtime_attempt_id
        if required_capability is not None:
            if required_capability not in {"input", "cancel"}:
                raise WorkBindingError("required task capability is invalid")
            grant_condition += " AND contains(#capabilities, :required_capability)"
            grant_values[":required_capability"] = required_capability
        grant_names = {
            "#status": "status",
            "#input": "input",
            "#model_binding": "model_binding",
            "#limits": "limits",
            "#capabilities": "capabilities",
        }
        if required_turn_number is not None:
            limits = grant.get("limits")
            maximum_turns = limits.get("max_turns") if isinstance(limits, dict) else None
            if not isinstance(required_turn_number, int) or isinstance(required_turn_number, bool) or required_turn_number < 1:
                raise TaskStoreError("turn_number must be a positive integer")
            if not isinstance(maximum_turns, int) or isinstance(maximum_turns, bool) or required_turn_number > maximum_turns:
                raise TaskStoreError("turn_number exceeds the protected max_turns limit")
            grant_condition += " AND #limits.#max_turns >= :required_turn_number"
            grant_values[":required_turn_number"] = required_turn_number
            grant_names["#max_turns"] = "max_turns"
        return [
            {
                "ConditionCheck": {
                    "TableName": self._authority_table_name,
                    "Key": _serialize_authority({"pk": authority_pk, "sk": task_binding_sort_key(task_id)}),
                    "ConditionExpression": (
                        "#status = :active AND tenant = :tenant AND canonical_principal = :principal AND "
                        "task_id = :task AND invocation_id = :invocation AND generation = :generation AND "
                        "request_digest = :digest AND persona = :persona AND policy_version = :policy_version AND "
                        "grant_digest = :grant_digest"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": _serialize_authority(common_values),
                }
            },
            {
                "ConditionCheck": {
                    "TableName": self._authority_table_name,
                    "Key": _serialize_authority({"pk": authority_pk, "sk": grant_sk}),
                    "ConditionExpression": grant_condition,
                    "ExpressionAttributeNames": grant_names,
                    "ExpressionAttributeValues": _serialize_authority(grant_values),
                }
            },
            {
                "ConditionCheck": {
                    "TableName": self._authority_table_name,
                    "Key": _serialize_authority({"pk": authority_pk, "sk": task_policy_sort_key(principal)}),
                    "ConditionExpression": "#status = :active AND #version = :policy_version AND contains(personas, :persona)",
                    "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                    "ExpressionAttributeValues": _serialize_authority(
                        {
                            ":active": "active",
                            ":policy_version": int(snapshot["policy_version"]),
                            ":persona": str(snapshot["persona"]),
                        }
                    ),
                }
            },
        ]

    def bind_runtime_attempt(
        self,
        *,
        task_id: str,
        invocation_id: str,
        generation: int,
        runtime_attempt_id: str,
        expected_version: int,
        expected_runtime_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically bind the task, run row and protected grant to one attempt."""
        validate_uuid(invocation_id, "invocation_id")
        validate_uuid(runtime_attempt_id, "runtime_attempt_id")
        if expected_runtime_attempt_id is not None:
            validate_uuid(expected_runtime_attempt_id, "expected_runtime_attempt_id")
        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)
        if snapshot.get("invocation_id") != invocation_id or int(snapshot.get("generation", 0)) != generation:
            raise StaleGenerationError(task_id=task_id, supplied=generation, current=int(snapshot.get("generation", 0)))
        if is_terminal(TaskState(str(snapshot["state"]))) or snapshot.get("state") == TaskState.CANCEL_REQUESTED.value:
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=str(snapshot["state"]),
                current_version=int(snapshot["version"]),
            )

        now_iso = _iso(self._clock())
        prior_condition = "runtime_attempt_id = :prior"
        values: dict[str, Any] = {
            ":attempt": runtime_attempt_id,
            ":prior": expected_runtime_attempt_id,
            ":version": expected_version,
            ":next_version": expected_version + 1,
            ":invocation": invocation_id,
            ":generation": generation,
            ":now": now_iso,
        }
        if expected_runtime_attempt_id is None:
            prior_condition = "(attribute_not_exists(runtime_attempt_id) OR attribute_type(runtime_attempt_id, :null_type))"
            values[":null_type"] = "NULL"

        task_binding_check, grant_check, policy_check = self._authority_condition_checks(snapshot=snapshot)
        grant_fence = grant_check["ConditionCheck"]

        def expression_values(*names: str) -> dict[str, Any]:
            return _serialize_authority({name: values[name] for name in names})

        prior_names = (":prior",) if expected_runtime_attempt_id is not None else (":null_type",)
        run_key = {
            "event_id": task_run_partition(task_id),
            "arrived_at": run_sort_key(invocation_id=invocation_id, generation=generation),
        }
        grant_key = {
            "pk": task_authority_partition(str(snapshot["scope"]["tenant"])),
            "sk": task_run_grant_sort_key(invocation_id=invocation_id, generation=generation),
        }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": task_partition(task_id), "arrived_at": META_SORT_KEY}),
                            "UpdateExpression": "SET runtime_attempt_id = :attempt, #version = :next_version, updated_at = :now",
                            "ConditionExpression": (
                                "#version = :version AND invocation_id = :invocation AND generation = :generation AND " + prior_condition
                            ),
                            "ExpressionAttributeNames": {"#version": "version"},
                            "ExpressionAttributeValues": expression_values(
                                ":attempt", ":version", ":next_version", ":invocation", ":generation", ":now", *prior_names
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority(run_key),
                            "UpdateExpression": "SET runtime_attempt_id = :attempt, updated_at = :now",
                            "ConditionExpression": "invocation_id = :invocation AND generation = :generation AND " + prior_condition,
                            "ExpressionAttributeValues": expression_values(":attempt", ":invocation", ":generation", ":now", *prior_names),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._authority_table_name,
                            "Key": _serialize_authority(grant_key),
                            "UpdateExpression": "SET runtime_attempt_id = :attempt, updated_at = :now",
                            "ConditionExpression": f"({grant_fence['ConditionExpression']}) AND {prior_condition}",
                            "ExpressionAttributeNames": grant_fence["ExpressionAttributeNames"],
                            "ExpressionAttributeValues": {
                                **grant_fence["ExpressionAttributeValues"],
                                **expression_values(":attempt", ":now", *prior_names),
                            },
                        }
                    },
                    task_binding_check,
                    policy_check,
                    *self._execution_recovery_items(snapshot),
                ]
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise TaskStoreError("task attempt binding unavailable") from exc
            live = self.read_task(task_id)
            if live is not None and int(live.get("generation", 0)) != generation:
                raise StaleGenerationError(task_id=task_id, supplied=generation, current=int(live.get("generation", 0))) from None
            raise StaleAttemptError("runtime attempt binding is stale or authority was revoked") from None
        except BotoCoreError as exc:
            raise TaskStoreError("task attempt binding unavailable") from exc
        return {
            "task_id": task_id,
            "invocation_id": invocation_id,
            "generation": generation,
            "runtime_attempt_id": runtime_attempt_id,
            "version": expected_version + 1,
        }

    def _execution_recovery_items(self, snapshot):
        if self._get(task_work_partition(snapshot["task_id"]), "RECONCILE") is not None:
            return []
        now = self._clock()
        due = now + timedelta(seconds=60)
        work_id = str(uuid.uuid4())
        work = base_item(
            partition=task_work_partition(snapshot["task_id"]), sort_key="RECONCILE", record_type="TASK_WORK", scope=snapshot["scope"]
        ) | {
            "task_id": snapshot["task_id"],
            "invocation_id": snapshot["invocation_id"],
            "generation": int(snapshot["generation"]),
            "grant_digest": snapshot["grant_digest"],
            "work_id": work_id,
            "work_kind": "execution",
            "work_version": 1,
            "recovery_state": "pending",
            "created_at": _iso(now),
            "updated_at": _iso(now),
            WORK_SHARD_ATTRIBUTE: work_shard(snapshot["task_id"]),
            WORK_DUE_ATTRIBUTE: work_due_key(due_at=due, work_id=work_id),
            "due_at": _iso(due),
        }
        locator = self._locator_for_work(work=work, tenant=snapshot["scope"]["tenant"], now_iso=_iso(now))
        return [
            {"Put": {"TableName": self._table_name, "Item": _serialize(work), "ConditionExpression": "attribute_not_exists(event_id)"}},
            {
                "Put": {
                    "TableName": self._authority_table_name,
                    "Item": _serialize_authority(locator),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]

    # -- state transitions --------------------------------------------------

    def transition(
        self,
        *,
        task_id: str,
        expected_version: int,
        target_state: TaskState,
        generation: int | None = None,
        invocation_id: str | None = None,
        runtime_attempt_id: str | None = None,
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
        attempt_bound = invocation_id is not None or runtime_attempt_id is not None
        if attempt_bound:
            if invocation_id is None or generation is None or runtime_attempt_id is None:
                raise TaskStoreError("worker transition requires the complete invocation/generation/attempt binding")
            validate_uuid(invocation_id, "invocation_id")
            validate_uuid(runtime_attempt_id, "runtime_attempt_id")
            if snapshot.get("invocation_id") != invocation_id or int(snapshot.get("generation", 0)) != generation:
                raise StaleGenerationError(task_id=task_id, supplied=generation, current=int(snapshot.get("generation", 0)))
            if snapshot.get("runtime_attempt_id") != runtime_attempt_id:
                raise StaleAttemptError("runtime attempt is not current")

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

        if _ATTEMPT_PROTECTED_TRANSITION_ATTRIBUTES.intersection(attributes or {}) and not attempt_bound:
            raise TaskStoreError("result and outcome evidence require a current runtime attempt binding")
        for key, value in (attributes or {}).items():
            if key not in _TRANSITION_MUTABLE_ATTRIBUTES:
                raise TaskStoreError(f"attribute {key!r} cannot be set through a transition")
            placeholder = f":attr_{len(values)}"
            attribute_name = f"#attr_{len(values)}"
            names[attribute_name] = key
            set_parts.append(f"{attribute_name} = {placeholder}")
            values[placeholder] = encode_json_updates(snapshot, {key: value})[key]

        json_updates = encode_json_updates(snapshot, attributes or {})
        if JSON_ENCODING_FIELD in json_updates:
            names["#json_encoding"] = JSON_ENCODING_FIELD
            set_parts.append("#json_encoding = :json_encoding")
            values[":json_encoding"] = json_updates[JSON_ENCODING_FIELD]

        condition = "version = :expected_version AND attribute_exists(event_id)"
        if generation is not None:
            condition += " AND generation = :generation"
            values[":generation"] = generation
        if attempt_bound:
            condition += " AND invocation_id = :invocation_id AND runtime_attempt_id = :runtime_attempt_id"
            values[":invocation_id"] = invocation_id
            values[":runtime_attempt_id"] = runtime_attempt_id

        # Terminal state records both retention boundaries.  The snapshot and
        # idempotency row are tombstones through day 90; task content becomes
        # logically unavailable at day 30 and is compacted asynchronously.
        if is_terminal(target_state):
            condition += " AND pending_input_count = :zero_pending"
            values[":zero_pending"] = 0
            set_parts.append("content_expires_at = :content_expires")
            values[":content_expires"] = int((self._clock() + timedelta(days=CONTENT_RETENTION_DAYS)).timestamp())
            set_parts.append(f"{TTL_ATTRIBUTE} = :tombstone_expires")
            values[":tombstone_expires"] = int((self._clock() + timedelta(days=TOMBSTONE_RETENTION_DAYS)).timestamp())
            set_parts.append("terminal_at = :now")

        transact: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": {"event_id": {"S": task_partition(task_id)}, "arrived_at": {"S": META_SORT_KEY}},
                    "UpdateExpression": "SET " + ", ".join(set_parts),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": _serialize_authority(values),
                }
            }
        ]

        if is_terminal(target_state):
            transact.append(
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": _serialize_authority({"event_id": str(snapshot["idempotency_partition"]), "arrived_at": META_SORT_KEY}),
                        "UpdateExpression": "SET terminal_at = :now, expires_at = :tombstone_expires",
                        "ConditionExpression": "task_id = :task_id AND request_digest = :request_digest",
                        "ExpressionAttributeValues": _serialize_authority(
                            {
                                ":now": now_iso,
                                ":tombstone_expires": values[":tombstone_expires"],
                                ":task_id": task_id,
                                ":request_digest": snapshot["request_digest"],
                            }
                        ),
                    }
                }
            )
            for artifact_id in snapshot.get("artifact_ids", []):
                transact.append(
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": task_artifact_partition(str(artifact_id)), "arrived_at": META_SORT_KEY}),
                            "UpdateExpression": "SET terminal_at = :now, expires_at = :content_expires",
                            "ConditionExpression": "task_id = :task_id AND binding_state = :bound",
                            "ExpressionAttributeValues": _serialize_authority(
                                {
                                    ":now": now_iso,
                                    ":content_expires": values[":content_expires"],
                                    ":task_id": task_id,
                                    ":bound": "bound",
                                }
                            ),
                        }
                    }
                )
            transact.append(
                {
                    "Update": {
                        "TableName": self._authority_table_name,
                        "Key": _serialize_authority({"pk": task_capacity_partition(str(snapshot["capacity_scope_hash"])), "sk": "ACTIVE"}),
                        "UpdateExpression": "SET updated_at = :now REMOVE reservations.#task ADD active_count :minus_one",
                        "ConditionExpression": "reservations.#task = :reservation AND active_count > :zero",
                        "ExpressionAttributeNames": {"#task": task_id},
                        "ExpressionAttributeValues": _serialize_authority(
                            {
                                ":now": now_iso,
                                ":reservation": snapshot["capacity_reservation_id"],
                                ":zero": 0,
                                ":minus_one": -1,
                            }
                        ),
                    }
                }
            )

        if attempt_bound:
            transact.extend(self._authority_condition_checks(snapshot=snapshot, runtime_attempt_id=runtime_attempt_id))

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
                                runtime_attempt_id=runtime_attempt_id,
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
            self._raise_transition_failure(
                exc,
                task_id=task_id,
                target_state=target_state,
                generation=generation,
                runtime_attempt_id=runtime_attempt_id,
            )

        return {"task_id": task_id, "state": target_state.value, "version": next_version, "updated_at": now_iso}

    def _raise_transition_failure(
        self,
        exc: Exception,
        *,
        task_id: str,
        target_state: TaskState,
        generation: int | None,
        runtime_attempt_id: str | None,
    ) -> None:
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
        if runtime_attempt_id is not None and snapshot.get("runtime_attempt_id") != runtime_attempt_id:
            raise StaleAttemptError("runtime attempt is stale") from None
        if is_terminal(target_state) and int(snapshot.get("pending_input_count", 0)) > 0:
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=str(snapshot.get("state")),
                current_version=int(snapshot.get("version", 0)),
                reason="pending_input",
            ) from None
        if runtime_attempt_id is not None:
            raise StaleAttemptError("runtime attempt authority was revoked or changed") from None

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
                            "ExpressionAttributeValues": _serialize_authority(values),
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
                    *self._authority_condition_checks(snapshot=snapshot),
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
        runtime_attempt_id: str | None = None,
        producer_timestamp: str | None = None,
    ) -> dict[str, Any]:
        return (
            base_item(
                partition=task_events_partition(task_id),
                sort_key=event_sort_key(sequence),
                record_type="TASK_EVENTS",
                scope=scope,
            )
            | {
                "task_id": task_id,
                "invocation_id": invocation_id,
                "generation": generation,
                "runtime_attempt_id": runtime_attempt_id,
                "sequence": sequence,
                # The public cursor form from the contract: <task_id>:<sequence>.
                "task_event_id": f"{task_id}:{sequence}",
                "type": kind,
                "timestamp": timestamp,
                "data": data,
            }
            | ({"producer_timestamp": producer_timestamp} if producer_timestamp is not None else {})
        )

    def append_report(
        self,
        *,
        task_id: str,
        invocation_id: str,
        generation: int,
        runtime_attempt_id: str,
        report_id: str,
        kind: str,
        data: dict[str, Any],
        producer_timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Deduplicate and append one worker report under the current-attempt fence."""
        validate_task_id(task_id)
        validate_uuid(invocation_id, "invocation_id")
        validate_uuid(runtime_attempt_id, "runtime_attempt_id")
        validate_uuid(report_id, "report_id")
        if kind not in _REPORT_EVENT_TYPES:
            raise TaskStoreError("report event type is not permitted")
        if not isinstance(data, dict):
            raise TaskStoreError("report data must be an object")
        data_limit = 8_192 if kind == "progress.updated" else 65_536
        if len(canonical_json(data)) > data_limit:
            raise TaskStoreError("report data exceeds the fixed event limit")
        if producer_timestamp is not None and producer_timestamp.tzinfo is None:
            raise TaskStoreError("producer_timestamp must be timezone-aware")

        producer_iso = _iso(producer_timestamp) if producer_timestamp is not None else None
        report_digest = payload_digest(
            {
                "task_id": task_id,
                "invocation_id": invocation_id,
                "generation": generation,
                "runtime_attempt_id": runtime_attempt_id,
                "event_type": kind,
                "producer_timestamp": producer_iso,
                "data": data,
            }
        )
        report_key = report_sort_key(generation=generation, report_id=report_id)
        existing = self._get(task_report_partition(task_id), report_key)
        if existing is not None:
            if existing.get("report_digest") != report_digest:
                raise IdempotencyConflictError(
                    task_id=task_id,
                    stored_digest=str(existing.get("report_digest", "")),
                    supplied_digest=report_digest,
                )
            return {
                "report_id": report_id,
                "sequence": int(existing["sequence"]),
                "event_id": str(existing["task_event_id"]),
            }

        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)
        if snapshot.get("invocation_id") != invocation_id or int(snapshot.get("generation", 0)) != generation:
            raise StaleGenerationError(task_id=task_id, supplied=generation, current=int(snapshot.get("generation", 0)))
        if snapshot.get("runtime_attempt_id") != runtime_attempt_id:
            raise StaleAttemptError("runtime attempt is not current")
        current_state = TaskState(str(snapshot["state"]))
        if is_terminal(current_state) or current_state is TaskState.CANCEL_REQUESTED:
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=current_state.value,
                current_version=int(snapshot["version"]),
            )
        current_sequence = int(snapshot.get("event_sequence", 0))
        maximum = MAX_TASK_EVENTS if kind in _TERMINAL_REPORT_EVENT_TYPES else MAX_TASK_EVENTS - FINAL_EVENT_RESERVE
        starts_run = current_state in {TaskState.ACCEPTED, TaskState.QUEUED}
        allocated = 2 if starts_run else 1
        if current_sequence + allocated > maximum:
            raise TaskStoreError("task event budget is exhausted")

        sequence = current_sequence + allocated
        now_iso = _iso(self._clock())
        scope = dict(snapshot["scope"])
        report = base_item(
            partition=task_report_partition(task_id),
            sort_key=report_key,
            record_type="TASK_REPORT",
            scope=scope,
        ) | {
            "task_id": task_id,
            "invocation_id": invocation_id,
            "generation": generation,
            "runtime_attempt_id": runtime_attempt_id,
            "report_id": report_id,
            "report_digest": report_digest,
            "event_type": kind,
            "sequence": sequence,
            "task_event_id": f"{task_id}:{sequence}",
            "created_at": now_iso,
        }
        event = self._event_item(
            task_id=task_id,
            scope=scope,
            sequence=sequence,
            kind=kind,
            invocation_id=invocation_id,
            generation=generation,
            timestamp=now_iso,
            data=data,
            runtime_attempt_id=runtime_attempt_id,
            producer_timestamp=producer_iso,
        )
        meta_expression = "SET event_sequence = :next, updated_at = :now"
        meta_names = {"#state": "state", "#version": "version"}
        meta_values = {
            ":next": sequence,
            ":current": current_sequence,
            ":now": now_iso,
            ":state": current_state.value,
            ":invocation": invocation_id,
            ":generation": generation,
            ":attempt": runtime_attempt_id,
            ":version": int(snapshot["version"]),
        }
        if starts_run or kind == "input.required":
            meta_expression += ", #state = :running, #version = :next_version"
            meta_values.update(
                {":running": "waiting_for_input" if kind == "input.required" else "running", ":next_version": int(snapshot["version"]) + 1}
            )
        if kind == "input.required":
            validate_uuid(data.get("input_request_id"), "input_request_id")
            if not isinstance(data.get("prompt"), str) or not 1 <= len(data["prompt"]) <= 4000:
                raise TaskStoreError("clarification prompt is invalid")
            meta_expression += ", input_request = :input_request"
            meta_values[":input_request"] = {"input_request_id": data["input_request_id"], "prompt": data["prompt"], "requested_at": now_iso}
        transaction = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(report),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize_authority({"event_id": task_partition(task_id), "arrived_at": META_SORT_KEY}),
                    "UpdateExpression": meta_expression,
                    "ConditionExpression": (
                        "event_sequence = :current AND #state = :state AND invocation_id = :invocation AND "
                        "generation = :generation AND runtime_attempt_id = :attempt AND #version = :version"
                    ),
                    "ExpressionAttributeNames": meta_names,
                    "ExpressionAttributeValues": _serialize_authority(meta_values),
                }
            },
            *self._authority_condition_checks(snapshot=snapshot, runtime_attempt_id=runtime_attempt_id),
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(event),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
        ]
        if starts_run:
            # A current-attempt authored report proves the runtime started. The
            # gateway derives lifecycle state; the producer cannot supply it.
            started = self._event_item(
                task_id=task_id,
                scope=scope,
                sequence=current_sequence + 1,
                kind="run.started",
                invocation_id=invocation_id,
                generation=generation,
                runtime_attempt_id=runtime_attempt_id,
                timestamp=now_iso,
                data={"status": "running", "version": int(snapshot["version"]) + 1},
            )
            transaction.append(
                {"Put": {"TableName": self._table_name, "Item": _serialize(started), "ConditionExpression": "attribute_not_exists(event_id)"}}
            )
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise TaskStoreError("task report store unavailable") from exc
            committed = self._get(task_report_partition(task_id), report_key)
            if committed is not None:
                if committed.get("report_digest") != report_digest:
                    raise IdempotencyConflictError(
                        task_id=task_id,
                        stored_digest=str(committed.get("report_digest", "")),
                        supplied_digest=report_digest,
                    ) from None
                return {
                    "report_id": report_id,
                    "sequence": int(committed["sequence"]),
                    "event_id": str(committed["task_event_id"]),
                }
            live = self.read_task(task_id)
            if live is not None and int(live.get("generation", 0)) != generation:
                raise StaleGenerationError(task_id=task_id, supplied=generation, current=int(live.get("generation", 0))) from None
            if (
                live is not None
                and live.get("runtime_attempt_id") == runtime_attempt_id
                and live.get("state") == current_state.value
                and int(live.get("version", 0)) != int(snapshot["version"])
            ):
                raise TaskStoreError("task report version changed; retry the same report ID") from None
            raise StaleAttemptError("runtime attempt is stale or authority was revoked") from None
        except BotoCoreError as exc:
            raise TaskStoreError("task report store unavailable") from exc
        return {"report_id": report_id, "sequence": sequence, "event_id": f"{task_id}:{sequence}"}

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
        """Persist one schema-valid command under task, authority, and limit fences."""
        validate_task_id(task_id)
        validate_uuid(command_id, "command_id")
        _validate_command_payload(command_id=command_id, kind=kind, payload=payload)
        if authority_expires_at.tzinfo is None:
            raise TaskStoreError("command authority expiry must be timezone-aware")

        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)
        scope = snapshot.get("scope")
        if not isinstance(scope, dict) or author != scope.get("canonical_principal"):
            raise WorkBindingError("command author does not match the protected task owner")

        digest = payload_digest({"kind": kind, "payload": payload})
        existing = self._get(task_commands_partition(task_id), command_sort_key(command_id))
        if existing is not None:
            if str(existing.get("command_digest")) != digest or existing.get("author") != author:
                raise IdempotencyConflictError(
                    task_id=task_id,
                    stored_digest=str(existing.get("command_digest", "")),
                    supplied_digest=digest,
                )
            return existing

        state = TaskState(str(snapshot["state"]))
        if is_terminal(state) or state is TaskState.CANCEL_REQUESTED:
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=state.value,
                current_version=int(snapshot.get("version", 0)),
            )

        now = self._clock()
        if authority_expires_at <= now:
            raise WorkBindingError("command authority is expired")
        current_event_sequence = int(snapshot.get("event_sequence", 0))
        maximum_events = MAX_TASK_EVENTS if kind == "cancel" else MAX_TASK_EVENTS - FINAL_EVENT_RESERVE
        if current_event_sequence >= maximum_events:
            raise TaskStoreError("task event budget is exhausted")
        if kind == "input":
            if int(snapshot.get("pending_input_count", 0)) >= MAX_PENDING_INPUTS:
                raise TaskStoreError("pending input command limit is exhausted")
            if int(snapshot.get("input_command_count", 0)) >= MAX_INPUT_COMMANDS:
                raise TaskStoreError("input command lifetime limit is exhausted")

        now_iso = _iso(now)
        sequence = int(snapshot.get("command_sequence", 0)) + 1
        event_sequence = current_event_sequence + 1
        command = base_item(
            partition=task_commands_partition(task_id),
            sort_key=command_sort_key(command_id),
            record_type="TASK_COMMANDS",
            scope=dict(scope),
        ) | {
            "task_id": task_id,
            "command_id": command_id,
            "kind": kind,
            "command_digest": digest,
            "payload": payload,
            "author": author,
            "policy_version": int(snapshot["policy_version"]),
            "authority_expires_at": _iso(authority_expires_at),
            "command_sequence": sequence,
            "status": "accepted",
            "handoff": "not_started",
            "turn_id": None,
            "turn_number": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        event = self._event_item(
            task_id=task_id,
            scope=dict(scope),
            sequence=event_sequence,
            kind="cancel.requested" if kind == "cancel" else "input.accepted",
            invocation_id=str(snapshot.get("invocation_id", "")),
            generation=int(snapshot.get("generation", 1)),
            timestamp=now_iso,
            data={"command_id": command_id},
        )

        update_expression = "SET command_sequence = :sequence, event_sequence = :event_sequence, #version = :next_version, updated_at = :now"
        condition_expression = "#version = :expected_version AND command_sequence = :prior AND event_sequence = :prior_event"
        expression_names = {"#version": "version"}
        expression_values: dict[str, Any] = {
            ":sequence": sequence,
            ":prior": sequence - 1,
            ":event_sequence": event_sequence,
            ":prior_event": current_event_sequence,
            ":next_version": expected_version + 1,
            ":expected_version": expected_version,
            ":now": now_iso,
        }
        if kind == "input":
            update_expression += ", pending_input_count = :pending_inputs, input_command_count = :input_commands"
            condition_expression += " AND pending_input_count < :max_pending AND input_command_count < :max_inputs"
            expression_values |= {
                ":pending_inputs": int(snapshot.get("pending_input_count", 0)) + 1,
                ":input_commands": int(snapshot.get("input_command_count", 0)) + 1,
                ":max_pending": MAX_PENDING_INPUTS,
                ":max_inputs": MAX_INPUT_COMMANDS,
            }
        else:
            update_expression += ", #state = :cancel_requested"
            condition_expression += " AND #state = :current_state"
            expression_names["#state"] = "state"
            expression_values |= {":cancel_requested": TaskState.CANCEL_REQUESTED.value, ":current_state": state.value}

        transaction = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(command),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": {"event_id": {"S": task_partition(task_id)}, "arrived_at": {"S": META_SORT_KEY}},
                    "UpdateExpression": update_expression,
                    "ConditionExpression": condition_expression,
                    "ExpressionAttributeNames": expression_names,
                    "ExpressionAttributeValues": _serialize_authority(expression_values),
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(event),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
            *self._authority_condition_checks(snapshot=snapshot, required_capability=kind),
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except (ClientError, BotoCoreError) as exc:
            if not _is_conditional_failure(exc):
                raise TaskStoreError("task store unavailable") from exc
            committed = self._get(task_commands_partition(task_id), command_sort_key(command_id))
            if committed is not None and str(committed.get("command_digest")) == digest and committed.get("author") == author:
                return committed
            live = self.read_task(task_id)
            if kind == "input" and live is not None:
                if int(live.get("pending_input_count", 0)) >= MAX_PENDING_INPUTS:
                    raise TaskStoreError("pending input command limit is exhausted") from None
                if int(live.get("input_command_count", 0)) >= MAX_INPUT_COMMANDS:
                    raise TaskStoreError("input command lifetime limit is exhausted") from None
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
        invocation_id: str,
        generation: int,
        runtime_attempt_id: str,
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
        validate_task_id(task_id)
        validate_uuid(invocation_id, "invocation_id")
        validate_uuid(runtime_attempt_id, "runtime_attempt_id")
        validate_uuid(turn_id, "turn_id")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise TaskStoreError("generation must be a positive integer")
        if not command_ids:
            raise TaskStoreError("a turn must contain at least one command")
        if len(command_ids) > MAX_PENDING_INPUTS or len(command_ids) != len(set(command_ids)):
            raise TaskStoreError("a turn may contain at most ten unique input commands")
        for command_id in command_ids:
            validate_uuid(command_id, "command_id")

        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)
        if snapshot.get("invocation_id") != invocation_id or int(snapshot.get("generation", 0)) != generation:
            raise StaleGenerationError(task_id=task_id, supplied=generation, current=int(snapshot.get("generation", 0)))
        if snapshot.get("runtime_attempt_id") != runtime_attempt_id:
            raise StaleAttemptError("runtime attempt is not current")
        authority_checks = self._authority_condition_checks(
            snapshot=snapshot,
            runtime_attempt_id=runtime_attempt_id,
            required_capability="input",
            required_turn_number=turn_number,
        )

        existing = self._get(task_turns_partition(task_id), turn_sort_key(turn_number))
        if existing is not None:
            # Idempotent recovery. Returning the committed turn is what stops a
            # retry from re-inserting the same commands.
            return existing
        if turn_number != int(snapshot.get("turn_count", 0)) + 1:
            raise TaskStoreError("turn_number must be the next sequential turn")

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
                    "UpdateExpression": (
                        "SET turn_count = :turn, #version = :next_version, updated_at = :now ADD pending_input_count :pending_delta"
                    ),
                    "ConditionExpression": (
                        "#version = :expected_version AND invocation_id = :invocation_id AND generation = :generation AND "
                        "runtime_attempt_id = :runtime_attempt_id AND turn_count = :prior_turn AND "
                        "pending_input_count >= :command_count"
                    ),
                    "ExpressionAttributeNames": {"#version": "version"},
                    "ExpressionAttributeValues": {
                        k: _SERIALIZER.serialize(v)
                        for k, v in {
                            ":turn": turn_number,
                            ":prior_turn": turn_number - 1,
                            ":invocation_id": invocation_id,
                            ":generation": generation,
                            ":runtime_attempt_id": runtime_attempt_id,
                            ":next_version": expected_version + 1,
                            ":expected_version": expected_version,
                            ":pending_delta": -len(command_ids),
                            ":command_count": len(command_ids),
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
                        "UpdateExpression": (
                            "SET #status = :consumed, turn_id = :turn_id, turn_number = :turn, consumed_at = :now, updated_at = :now"
                        ),
                        # Only a command still in `accepted` may be consumed, so a
                        # command already consumed by an earlier turn fails here
                        # and cannot be assigned to a second turn. This is the
                        # durable half of "each command appears in exactly one
                        # turn, once".
                        "ConditionExpression": ("#status = :accepted AND kind = :input AND author = :author AND authority_expires_at >= :now"),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":consumed": {"S": "consumed"},
                            ":accepted": {"S": "accepted"},
                            ":input": {"S": "input"},
                            ":author": {"S": str(snapshot["scope"]["canonical_principal"])},
                            ":turn_id": {"S": turn_id},
                            ":turn": _SERIALIZER.serialize(turn_number),
                            ":now": {"S": now_iso},
                        },
                    }
                }
            )

        transact.extend(authority_checks)

        try:
            self._client.transact_write_items(TransactItems=transact)
        except (ClientError, BotoCoreError) as exc:
            if not _is_conditional_failure(exc):
                raise TaskStoreError("task store unavailable") from exc
            committed = self._get(task_turns_partition(task_id), turn_sort_key(turn_number))
            if committed is not None:
                return committed
            live = self.read_task(task_id)
            if live is not None and int(live.get("generation", 0)) != generation:
                raise StaleGenerationError(task_id=task_id, supplied=generation, current=int(live.get("generation", 0))) from None
            if live is not None and live.get("runtime_attempt_id") != runtime_attempt_id:
                raise StaleAttemptError("runtime attempt is stale") from None
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

    def create_artifact_binding(
        self,
        *,
        artifact_id: str,
        tenant: str,
        canonical_principal: str,
        version: int,
        content_sha256: str,
        content_type: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        """Persist an immutable, owner-bound external artifact reference."""
        if version < 1:
            raise TaskStoreError("artifact version must be positive")
        if content_type not in {"text/plain", "application/json"}:
            raise TaskStoreError("artifact content type is not permitted")
        if size_bytes < 1 or size_bytes > 262_144:
            raise TaskStoreError("artifact exceeds the 256 KiB input limit")
        if len(content_sha256) != 64 or any(character not in "0123456789abcdef" for character in content_sha256):
            raise TaskStoreError("artifact digest must be lowercase SHA-256")
        partition = task_artifact_partition(artifact_id)
        now = self._clock()
        object_key = (
            f"tasks/{component_digest('task-artifact-tenant-v1', tenant)}/"
            f"{component_digest('task-artifact-principal-v1', canonical_principal)}/{artifact_id}/{version}"
        )
        item = base_item(
            partition=partition,
            sort_key=META_SORT_KEY,
            record_type="TASK_ARTIFACT",
            scope={"tenant": tenant, "canonical_principal": canonical_principal},
        ) | {
            "artifact_id": artifact_id,
            "version": version,
            "content_sha256": content_sha256,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "object_key": object_key,
            "binding_state": "unclaimed",
            "created_at": _iso(now),
            TTL_ATTRIBUTE: int((now + timedelta(hours=24)).timestamp()),
        }
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_serialize(item),
                ConditionExpression="attribute_not_exists(event_id)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = self._get(partition, META_SORT_KEY)
                if (
                    existing
                    and all(
                        existing.get(key) == value
                        for key, value in {
                            "artifact_id": artifact_id,
                            "version": version,
                            "content_sha256": content_sha256,
                            "content_type": content_type,
                            "size_bytes": size_bytes,
                            "object_key": object_key,
                        }.items()
                    )
                    and existing.get("scope") == {"tenant": tenant, "canonical_principal": canonical_principal}
                ):
                    if existing.get("binding_state") == "bound" or int(existing.get(TTL_ATTRIBUTE, 0)) > int(now.timestamp()):
                        return existing
                raise IdempotencyConflictError(
                    task_id="",
                    stored_digest=str(existing.get("content_sha256", "")) if existing else "",
                    supplied_digest=content_sha256,
                ) from None
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc
        return item

    # -- recovery and retention --------------------------------------------

    def create_recovery_work(
        self,
        *,
        task_id: str,
        kind: str,
        due_at: datetime,
        expected_task_version: int,
        work_id: str | None = None,
    ) -> str:
        """Atomically create non-dispatch work and its protected UUID locator."""
        if kind not in {"execution", "queue_ack", "cleanup"}:
            raise TaskStoreError("non-dispatch recovery kind is invalid")
        work_id = work_id or str(uuid.uuid4())
        validate_uuid(work_id, "work_id")
        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)
        now_iso = _iso(self._clock())
        scope = dict(snapshot["scope"])
        work = base_item(
            partition=task_work_partition(task_id),
            sort_key="RECONCILE",
            record_type="TASK_WORK",
            scope=scope,
        ) | {
            "task_id": task_id,
            "invocation_id": snapshot["invocation_id"],
            "generation": int(snapshot["generation"]),
            "grant_digest": snapshot["grant_digest"],
            "work_id": work_id,
            "work_kind": kind,
            "work_version": 1,
            "recovery_state": "pending",
            "created_at": now_iso,
            "updated_at": now_iso,
            WORK_SHARD_ATTRIBUTE: work_shard(task_id),
            WORK_DUE_ATTRIBUTE: work_due_key(due_at=due_at, work_id=work_id),
            "due_at": _iso(due_at),
        }
        locator = self._locator_for_work(work=work, tenant=str(scope["tenant"]), now_iso=now_iso)
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": task_partition(task_id), "arrived_at": META_SORT_KEY}),
                            "ConditionExpression": (
                                "#version = :version AND invocation_id = :invocation AND generation = :generation AND request_digest = :digest"
                            ),
                            "ExpressionAttributeNames": {"#version": "version"},
                            "ExpressionAttributeValues": _serialize_authority(
                                {
                                    ":version": expected_task_version,
                                    ":invocation": snapshot["invocation_id"],
                                    ":generation": int(snapshot["generation"]),
                                    ":digest": snapshot["request_digest"],
                                }
                            ),
                        }
                    },
                    *self._authority_condition_checks(snapshot=snapshot),
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _serialize(work),
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._authority_table_name,
                            "Item": _serialize_authority(locator),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise TaskStateConflictError(
                    task_id=task_id,
                    current_state=str(snapshot.get("state")),
                    current_version=int(snapshot.get("version", 0)),
                ) from None
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc
        return work_id

    def replace_recovery_work(self, *, old_work_id: str, due_at: datetime, new_work_id: str | None = None) -> str:
        """Replace non-dispatch work without ever retargeting its old locator."""
        old = self.resolve_work(old_work_id)
        if old.get("work_kind") == "dispatch":
            raise TaskStoreError("dispatch work identity is stable and cannot be replaced")
        new_work_id = new_work_id or str(uuid.uuid4())
        validate_uuid(new_work_id, "new_work_id")
        if new_work_id == old_work_id:
            raise TaskStoreError("replacement work must receive a new UUID")
        now_iso = _iso(self._clock())
        scope = dict(old["scope"])
        replacement = old | {
            "work_id": new_work_id,
            "work_version": int(old.get("work_version", 1)) + 1,
            "recovery_state": "pending",
            "updated_at": now_iso,
            WORK_DUE_ATTRIBUTE: work_due_key(due_at=due_at, work_id=new_work_id),
            "due_at": _iso(due_at),
        }
        for attribute_name in (
            "recovery_lease_token",
            "recovery_lease_expires_at",
            "recovery_lease_taken_at",
            "publication_lease_token",
            "publication_lease_expires_at",
        ):
            replacement.pop(attribute_name, None)
        locator = self._locator_for_work(work=replacement, tenant=str(scope["tenant"]), now_iso=now_iso)
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": old["event_id"], "arrived_at": old["arrived_at"]}),
                            "UpdateExpression": (
                                "SET work_id = :new, work_version = :version, recovery_state = :pending, "
                                "updated_at = :now, task_due = :due, due_at = :due_at "
                                "REMOVE recovery_lease_token, recovery_lease_expires_at, recovery_lease_taken_at"
                            ),
                            "ConditionExpression": "work_id = :old AND work_version = :old_version",
                            "ExpressionAttributeValues": _serialize_authority(
                                {
                                    ":new": new_work_id,
                                    ":version": replacement["work_version"],
                                    ":pending": "pending",
                                    ":now": now_iso,
                                    ":due": replacement[WORK_DUE_ATTRIBUTE],
                                    ":due_at": replacement["due_at"],
                                    ":old": old_work_id,
                                    ":old_version": int(old.get("work_version", 1)),
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._authority_table_name,
                            "Key": _serialize_authority({"pk": task_work_locator_partition(old_work_id), "sk": TASK_WORK_LOCATOR_SORT_KEY}),
                            "UpdateExpression": "SET binding_state = :replaced, replaced_at = :now, replacement_work_id = :new",
                            "ConditionExpression": "binding_state = :active AND work_id = :old",
                            "ExpressionAttributeValues": _serialize_authority(
                                {":replaced": "replaced", ":now": now_iso, ":new": new_work_id, ":active": "active", ":old": old_work_id}
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._authority_table_name,
                            "Item": _serialize_authority(locator),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    *self._authority_condition_checks(snapshot=self.read_task(str(old["task_id"])) or {}),
                ]
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise WorkLeaseConflictError("recovery work was concurrently replaced") from None
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc
        return new_work_id

    @staticmethod
    def _locator_for_work(*, work: dict[str, Any], tenant: str, now_iso: str) -> dict[str, Any]:
        return {
            "pk": task_work_locator_partition(str(work["work_id"])),
            "sk": TASK_WORK_LOCATOR_SORT_KEY,
            "record_type": "TASK_WORK_BINDING",
            "schema_version": "1.0",
            "work_id": work["work_id"],
            "work_kind": work["work_kind"],
            "tenant": tenant,
            "task_id": work["task_id"],
            "invocation_id": work["invocation_id"],
            "generation": work["generation"],
            "grant_digest": work["grant_digest"],
            "work_event_id": work["event_id"],
            "work_arrived_at": work["arrived_at"],
            "binding_state": "active",
            "created_at": now_iso,
        }

    def claim_due_work(
        self,
        *,
        shard: str,
        now: datetime,
        limit: int = MAX_WORK_PER_CLAIM,
        lease_seconds: int = WORK_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        """Compatibility list view over :meth:`claim_due_work_page`."""
        return self.claim_due_work_page(shard=shard, now=now, limit=limit, lease_seconds=lease_seconds)["work"]

    def claim_due_work_page(
        self,
        *,
        shard: str,
        now: datetime,
        limit: int = MAX_WORK_PER_CLAIM,
        lease_seconds: int = WORK_LEASE_SECONDS,
        exclusive_start_key: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            raise TaskStoreError("limit must be between 1 and 100")
        if shard not in {f"v1#{index:02d}" for index in range(16)}:
            raise TaskStoreError("shard must be one of v1#00 through v1#15")
        try:
            query = {
                "TableName": self._table_name,
                "IndexName": WORK_INDEX_NAME,
                "KeyConditionExpression": f"{WORK_SHARD_ATTRIBUTE} = :shard AND {WORK_DUE_ATTRIBUTE} <= :due",
                "ExpressionAttributeValues": {
                    ":shard": {"S": shard},
                    ":due": {"S": work_due_key(due_at=now, work_id=_WORK_ID_MAX)},
                },
                "Limit": limit,
            }
            if exclusive_start_key is not None:
                query["ExclusiveStartKey"] = _serialize_authority(exclusive_start_key)
            response = self._client.query(
                **query,
            )
        except (ClientError, BotoCoreError) as exc:
            raise TaskStoreError("task store unavailable") from exc

        claimed: list[dict[str, Any]] = []
        for projected in response.get("Items", []):
            item = _deserialize(projected)
            partition, sort_key = item.get("event_id"), item.get("arrived_at")
            if not partition or not sort_key:
                continue
            try:
                record = self.resolve_work(str(item.get("work_id", "")))
            except (WorkBindingError, ValueError):
                continue
            if record.get("event_id") != partition or record.get("arrived_at") != sort_key:
                continue
            leased = self._lease_recovery(record=record, now=now, lease_seconds=lease_seconds)
            if leased is not None:
                claimed.append(leased)
        last_key = response.get("LastEvaluatedKey")
        return {"work": claimed, "next_key": _deserialize(last_key) if last_key else None}

    def _lease_recovery(self, *, record: dict[str, Any], now: datetime, lease_seconds: int) -> dict[str, Any] | None:
        """Take the independent recovery lease under current authority fences."""
        if lease_seconds < 1 or lease_seconds > WORK_LEASE_SECONDS:
            raise TaskStoreError("recovery lease must be between 1 and 45 seconds")
        due_at = record.get("due_at")
        if not isinstance(due_at, str):
            raise WorkBindingError("work record has no immutable due time")
        try:
            due_time = datetime.fromisoformat(due_at)
        except ValueError:
            return None
        if due_time.tzinfo is None or due_time > now:
            return None
        expected_due_key = work_due_key(due_at=due_time, work_id=str(record["work_id"]))
        stored_due_key = record.get(WORK_DUE_ATTRIBUTE)
        # due_at is persisted at second precision; the sparse index retains
        # milliseconds. Validate that both identify the same due second and
        # work, then fence the write with the exact stored index key.
        stored_millis, separator, stored_work_id = str(stored_due_key).partition("#")
        expected_millis = int(expected_due_key.partition("#")[0])
        if (
            not separator
            or not stored_millis.isdigit()
            or len(stored_millis) != len(expected_due_key.partition("#")[0])
            or stored_work_id != str(record["work_id"])
            or int(stored_millis) // 1000 != expected_millis // 1000
        ):
            logger.warning("Task recovery due key refused", extra={"work_id": record["work_id"], "error_code": "invalid_due_key"})
            return None
        if int(stored_millis) > int(now.timestamp() * 1000):
            return None
        expected_due_key = stored_due_key
        expiry = _iso(now + timedelta(seconds=lease_seconds))
        token = str(uuid.uuid4())
        snapshot = self.read_task(str(record["task_id"]))
        if snapshot is None:
            raise WorkBindingError("work task metadata is missing")
        transaction = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize_authority({"event_id": record["event_id"], "arrived_at": record["arrived_at"]}),
                    "UpdateExpression": (
                        "SET recovery_lease_token = :token, recovery_lease_expires_at = :expiry, "
                        "recovery_lease_taken_at = :now, recovery_lease_version = if_not_exists(recovery_lease_version, :zero) + :one"
                    ),
                    "ConditionExpression": (
                        "work_id = :work_id AND task_due = :due_key AND due_at = :due_at AND "
                        "(attribute_not_exists(recovery_lease_expires_at) OR recovery_lease_expires_at < :now)"
                    ),
                    "ExpressionAttributeValues": _serialize_authority(
                        {
                            ":token": token,
                            ":expiry": expiry,
                            ":now": _iso(now),
                            ":work_id": record["work_id"],
                            ":due_key": expected_due_key,
                            ":due_at": due_at,
                            ":zero": 0,
                            ":one": 1,
                        }
                    ),
                }
            },
            *self._authority_condition_checks(snapshot=snapshot),
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return None
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc
        return {
            "work_id": str(record["work_id"]),
            "task_id": str(record["task_id"]),
            "kind": str(record["work_kind"]),
            "due_at": due_at,
            "lease_token": token,
            "lease_expires_at": expiry,
        }

    def claim_dispatch(self, *, dispatch_id: str, now: datetime | None = None, lease_seconds: int = WORK_LEASE_SECONDS) -> dict[str, Any]:
        """Claim the independent publication lease and return the exact envelope."""
        if lease_seconds < 1 or lease_seconds > WORK_LEASE_SECONDS:
            raise TaskStoreError("publication lease must be between 1 and 45 seconds")
        claimed_at = now or self._clock()
        work = self.resolve_work(dispatch_id, expected_kind="dispatch")
        snapshot = self.read_task(str(work["task_id"]))
        if snapshot is None or snapshot.get("state") != TaskState.ACCEPTED.value:
            raise WorkLeaseConflictError("dispatch task is no longer accepted")
        if work.get("publication_state") == "confirmed":
            raise WorkLeaseConflictError("dispatch is already confirmed")
        token = str(uuid.uuid4())
        expiry = _iso(claimed_at + timedelta(seconds=lease_seconds))
        transaction = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize_authority({"event_id": work["event_id"], "arrived_at": work["arrived_at"]}),
                    "UpdateExpression": (
                        "SET publication_lease_token = :token, publication_lease_expires_at = :expiry, "
                        "publication_lease_taken_at = :now, publication_lease_version = if_not_exists(publication_lease_version, :zero) + :one "
                        "ADD publication_tries :one"
                    ),
                    "ConditionExpression": (
                        "work_id = :work_id AND envelope_digest = :digest AND publication_state <> :confirmed AND "
                        "publication_tries < :max_tries AND publication_deadline_at >= :now AND "
                        "(attribute_not_exists(publication_lease_expires_at) OR publication_lease_expires_at < :now)"
                    ),
                    "ExpressionAttributeValues": _serialize_authority(
                        {
                            ":token": token,
                            ":expiry": expiry,
                            ":now": _iso(claimed_at),
                            ":work_id": dispatch_id,
                            ":digest": work["envelope_digest"],
                            ":confirmed": "confirmed",
                            ":zero": 0,
                            ":one": 1,
                            ":max_tries": 5,
                        }
                    ),
                }
            },
            *self._authority_condition_checks(snapshot=snapshot),
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise WorkLeaseConflictError("publication lease is held or dispatch authority changed") from None
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc
        return {"envelope": work["envelope"], "lease_token": token, "lease_expires_at": expiry}

    def _queue_transition_items(self, *, snapshot: dict[str, Any], now_iso: str) -> list[dict[str, Any]]:
        sequence = int(snapshot.get("event_sequence", 0)) + 1
        next_version = int(snapshot["version"]) + 1
        task_id = str(snapshot["task_id"])
        return [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize_authority({"event_id": task_partition(task_id), "arrived_at": META_SORT_KEY}),
                    "UpdateExpression": (
                        "SET #state = :queued, #version = :next_version, event_sequence = :sequence, queue_ack_status = :pending, updated_at = :now"
                    ),
                    "ConditionExpression": (
                        "#state = :accepted AND #version = :version AND invocation_id = :invocation AND "
                        "generation = :generation AND request_digest = :digest"
                    ),
                    "ExpressionAttributeNames": {"#state": "state", "#version": "version"},
                    "ExpressionAttributeValues": _serialize_authority(
                        {
                            ":queued": TaskState.QUEUED.value,
                            ":accepted": TaskState.ACCEPTED.value,
                            ":next_version": next_version,
                            ":version": int(snapshot["version"]),
                            ":sequence": sequence,
                            ":pending": "pending",
                            ":now": now_iso,
                            ":invocation": snapshot["invocation_id"],
                            ":generation": int(snapshot["generation"]),
                            ":digest": snapshot["request_digest"],
                        }
                    ),
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(
                        self._event_item(
                            task_id=task_id,
                            scope=dict(snapshot["scope"]),
                            sequence=sequence,
                            kind="task.queued",
                            invocation_id=str(snapshot["invocation_id"]),
                            generation=int(snapshot["generation"]),
                            timestamp=now_iso,
                            data={"status": TaskState.QUEUED.value, "version": next_version},
                        )
                    ),
                    "ConditionExpression": "attribute_not_exists(event_id)",
                }
            },
        ]

    def settle_dispatch(
        self,
        *,
        dispatch_id: str,
        lease_token: str,
        publication_outcome: str,
        sqs_message_id: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist actual send evidence and any queued transition atomically."""
        settled_at = now or self._clock()
        if publication_outcome not in {"confirmed", "unknown", "failed"}:
            raise TaskStoreError("invalid publication outcome")
        if publication_outcome == "confirmed" and not sqs_message_id:
            raise TaskStoreError("confirmed publication requires the actual SQS message ID")
        if publication_outcome != "confirmed" and sqs_message_id is not None:
            raise TaskStoreError("an unconfirmed publication cannot record an SQS message ID")
        work = self.resolve_work(dispatch_id, expected_kind="dispatch")
        snapshot = self.read_task(str(work["task_id"]))
        if snapshot is None:
            raise WorkBindingError("dispatch task metadata is missing")
        now_iso = _iso(settled_at)
        transaction: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize_authority({"event_id": work["event_id"], "arrived_at": work["arrived_at"]}),
                    "UpdateExpression": (
                        "SET publication_state = :outcome, sqs_message_id = :message, publication_settled_at = :now "
                        "REMOVE publication_lease_token, publication_lease_expires_at"
                    ),
                    "ConditionExpression": (
                        "work_id = :work_id AND publication_lease_token = :token AND "
                        "publication_lease_expires_at >= :now AND publication_state <> :confirmed"
                    ),
                    "ExpressionAttributeValues": _serialize_authority(
                        {
                            ":outcome": publication_outcome,
                            ":message": sqs_message_id,
                            ":now": now_iso,
                            ":work_id": dispatch_id,
                            ":token": lease_token,
                            ":confirmed": "confirmed",
                        }
                    ),
                }
            }
        ]
        if publication_outcome == "confirmed" and snapshot.get("state") == TaskState.ACCEPTED.value:
            transaction.extend(self._queue_transition_items(snapshot=snapshot, now_iso=now_iso))
        transaction.extend(self._authority_condition_checks(snapshot=snapshot))
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise WorkLeaseConflictError("publication token is stale, expired, settled, or authority changed") from None
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc

        current = self.read_task(str(work["task_id"]))
        return {
            "dispatch_id": dispatch_id,
            "queue_ack_status": str(current.get("queue_ack_status", "unknown")) if current else "unknown",
            "task_status": current.get("state") if current else None,
        }

    def settle_recovery(
        self,
        *,
        work_id: str,
        lease_token: str,
        evidence_kind: str,
        observed: bool,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Settle recovery only under a live token and persisted evidence fence."""
        if observed_at.tzinfo is None:
            raise TaskStoreError("observed_at must be timezone-aware")
        checked_at = self._clock()
        work = self.resolve_work(work_id)
        if evidence_kind != "publication":
            raise WorkBindingError("this storage interface has no authoritative evidence validator for that recovery kind")
        if not observed or work.get("publication_state") != "confirmed" or not work.get("sqs_message_id"):
            raise WorkBindingError("committed publication evidence is absent")
        snapshot = self.read_task(str(work["task_id"]))
        if snapshot is None:
            raise WorkBindingError("recovery task metadata is missing")
        now_iso = _iso(checked_at)
        transaction: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize_authority({"event_id": work["event_id"], "arrived_at": work["arrived_at"]}),
                    "UpdateExpression": (
                        "SET recovery_state = :settled, recovery_evidence_kind = :kind, recovery_settled_at = :now, "
                        "recovery_observed_at = :observed_at "
                        "REMOVE recovery_lease_token, recovery_lease_expires_at, task_work_shard, task_due"
                    ),
                    "ConditionExpression": (
                        "work_id = :work_id AND recovery_lease_token = :token AND recovery_lease_expires_at >= :now AND "
                        "publication_state = :confirmed AND attribute_exists(sqs_message_id)"
                    ),
                    "ExpressionAttributeValues": _serialize_authority(
                        {
                            ":settled": "settled",
                            ":kind": evidence_kind,
                            ":now": now_iso,
                            ":observed_at": _iso(observed_at),
                            ":work_id": work_id,
                            ":token": lease_token,
                            ":confirmed": "confirmed",
                        }
                    ),
                }
            }
        ]
        if snapshot.get("state") == TaskState.ACCEPTED.value:
            transaction.extend(self._queue_transition_items(snapshot=snapshot, now_iso=now_iso))
        transaction.extend(self._authority_condition_checks(snapshot=snapshot))
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise WorkLeaseConflictError("recovery token is stale, expired, settled, or authority changed") from None
            raise TaskStoreError("task store unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task store unavailable") from exc
        current = self.read_task(str(work["task_id"]))
        return {
            "work_id": work_id,
            "operation_status": "confirmed",
            "task_status": current.get("state") if current else None,
        }

    def expire_content_page(
        self,
        *,
        task_id: str,
        record_type: str,
        now: datetime,
        limit: int = 100,
        exclusive_start_key: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stamp one bounded partition page under the live recovery-retention fence."""
        partitions = dict(_task_content_partitions(task_id))
        if record_type not in partitions:
            raise TaskStoreError("record type is not eligible for content-page cleanup")
        if limit < 1 or limit > 100:
            raise TaskStoreError("limit must be between 1 and 100")
        snapshot = self.read_task(task_id)
        if snapshot is None or not is_terminal(TaskState(str(snapshot.get("state")))):
            raise TaskStateConflictError(task_id=task_id, current_state=str(snapshot.get("state")) if snapshot else None)
        content_expires_at = int(snapshot.get("content_expires_at", 0))
        if snapshot.get("recovery_required") or int(now.timestamp()) < content_expires_at:
            raise TaskStoreError("task content is not eligible for cleanup")

        query: dict[str, Any] = {
            "TableName": self._table_name,
            "KeyConditionExpression": "event_id = :partition",
            "ExpressionAttributeValues": {":partition": {"S": partitions[record_type]}},
            "Limit": limit,
            "ConsistentRead": True,
        }
        if exclusive_start_key is not None:
            query["ExclusiveStartKey"] = _serialize_authority(exclusive_start_key)
        try:
            response = self._client.query(**query)
            raw_items = response.get("Items", [])
            for offset in range(0, len(raw_items), 99):
                transaction = [
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": task_partition(task_id), "arrived_at": META_SORT_KEY}),
                            "ConditionExpression": (
                                "#version = :version AND recovery_required = :false AND terminal_at = :terminal AND "
                                "content_expires_at = :content_expires AND content_expires_at <= :now_epoch"
                            ),
                            "ExpressionAttributeNames": {"#version": "version"},
                            "ExpressionAttributeValues": _serialize_authority(
                                {
                                    ":version": int(snapshot["version"]),
                                    ":false": False,
                                    ":terminal": snapshot["terminal_at"],
                                    ":content_expires": content_expires_at,
                                    ":now_epoch": int(now.timestamp()),
                                }
                            ),
                        }
                    }
                ]
                for raw_item in raw_items[offset : offset + 99]:
                    item = _deserialize(raw_item)
                    transaction.append(
                        {
                            "Update": {
                                "TableName": self._table_name,
                                "Key": _serialize_authority({"event_id": item["event_id"], "arrived_at": item["arrived_at"]}),
                                "UpdateExpression": "SET terminal_at = :terminal, expires_at = :expires",
                                "ConditionExpression": "task_id = :task_id AND record_type = :record_type",
                                "ExpressionAttributeValues": _serialize_authority(
                                    {
                                        ":terminal": snapshot["terminal_at"],
                                        ":expires": content_expires_at,
                                        ":task_id": task_id,
                                        ":record_type": record_type,
                                    }
                                ),
                            }
                        }
                    )
                self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise TaskStoreError("task recovery retention changed during content cleanup") from None
            raise TaskStoreError("task content cleanup unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task content cleanup unavailable") from exc
        last_key = response.get("LastEvaluatedKey")
        return {"updated": len(response.get("Items", [])), "next_key": _deserialize(last_key) if last_key else None}

    def compact_terminal_task(self, *, task_id: str, now: datetime) -> None:
        """Remove retained input at day 30 while preserving the day-90 tombstone."""
        snapshot = self.read_task(task_id)
        if snapshot is None or not is_terminal(TaskState(str(snapshot.get("state")))):
            raise TaskStateConflictError(task_id=task_id, current_state=str(snapshot.get("state")) if snapshot else None)
        if snapshot.get("recovery_required") or int(now.timestamp()) < int(snapshot.get("content_expires_at", 0)):
            raise TaskStoreError("task content is not eligible for compaction")
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=_serialize_authority({"event_id": task_partition(task_id), "arrived_at": META_SORT_KEY}),
                UpdateExpression=("SET history_expired = :true, compacted_at = :now REMOVE input_payload, input_reference, artifact_ids"),
                ConditionExpression=(
                    "version = :version AND terminal_at = :terminal AND content_expires_at <= :now_epoch AND recovery_required = :false"
                ),
                ExpressionAttributeValues=_serialize_authority(
                    {
                        ":true": True,
                        ":false": False,
                        ":now": _iso(now),
                        ":now_epoch": int(now.timestamp()),
                        ":version": int(snapshot["version"]),
                        ":terminal": snapshot["terminal_at"],
                    }
                ),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TaskStateConflictError(
                    task_id=task_id,
                    current_state=str(snapshot.get("state")),
                    current_version=int(snapshot.get("version", 0)),
                ) from None
            raise TaskStoreError("task content cleanup unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task content cleanup unavailable") from exc

    def compact_settled_work(self, *, work_id: str, now: datetime) -> None:
        """Drop a settled envelope at day 30 and retain its locator to day 90."""
        locator = self._get_authority(task_work_locator_partition(work_id), TASK_WORK_LOCATOR_SORT_KEY)
        if locator is None or locator.get("binding_state") != "active" or locator.get("work_id") != work_id:
            raise WorkBindingError("work locator is missing, stale, or revoked")
        work = self._get(str(locator.get("work_event_id", "")), str(locator.get("work_arrived_at", "")))
        snapshot = self.read_task(str(locator.get("task_id", "")))
        if work is None or snapshot is None or not is_terminal(TaskState(str(snapshot.get("state")))):
            raise TaskStoreError("work is not attached to a terminal task")
        if snapshot.get("recovery_required") or int(now.timestamp()) < int(snapshot.get("content_expires_at", 0)):
            raise TaskStoreError("settled work is not eligible for compaction")
        tombstone_expiry = int(snapshot[TTL_ATTRIBUTE])
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": task_partition(str(snapshot["task_id"])), "arrived_at": META_SORT_KEY}),
                            "ConditionExpression": (
                                "#version = :version AND recovery_required = :false AND terminal_at = :terminal AND content_expires_at <= :now_epoch"
                            ),
                            "ExpressionAttributeNames": {"#version": "version"},
                            "ExpressionAttributeValues": _serialize_authority(
                                {
                                    ":version": int(snapshot["version"]),
                                    ":false": False,
                                    ":terminal": snapshot["terminal_at"],
                                    ":now_epoch": int(now.timestamp()),
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": work["event_id"], "arrived_at": work["arrived_at"]}),
                            "UpdateExpression": ("SET envelope_compacted = :true, terminal_at = :terminal, expires_at = :expires REMOVE envelope"),
                            "ConditionExpression": ("work_id = :work_id AND recovery_state = :settled AND attribute_not_exists(task_due)"),
                            "ExpressionAttributeValues": _serialize_authority(
                                {
                                    ":true": True,
                                    ":terminal": snapshot["terminal_at"],
                                    ":expires": tombstone_expiry,
                                    ":work_id": work_id,
                                    ":settled": "settled",
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._authority_table_name,
                            "Key": _serialize_authority({"pk": task_work_locator_partition(work_id), "sk": TASK_WORK_LOCATOR_SORT_KEY}),
                            "UpdateExpression": "SET binding_state = :retained, delete_after = :expires, compacted_at = :now",
                            "ConditionExpression": "binding_state = :active AND work_id = :work_id",
                            "ExpressionAttributeValues": _serialize_authority(
                                {
                                    ":retained": "retained",
                                    ":expires": tombstone_expiry,
                                    ":now": _iso(now),
                                    ":active": "active",
                                    ":work_id": work_id,
                                }
                            ),
                        }
                    },
                ]
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise WorkBindingError("work is active, unconfirmed, or already compacted") from None
            raise TaskStoreError("task work cleanup unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task work cleanup unavailable") from exc

    def delete_retained_work_locator(self, *, work_id: str, now: datetime) -> None:
        """Delete only a compacted locator whose day-90 deadline has passed."""
        try:
            self._client.delete_item(
                TableName=self._authority_table_name,
                Key=_serialize_authority({"pk": task_work_locator_partition(work_id), "sk": TASK_WORK_LOCATOR_SORT_KEY}),
                ConditionExpression="work_id = :work_id AND binding_state = :retained AND delete_after <= :now",
                ExpressionAttributeValues=_serialize_authority({":work_id": work_id, ":retained": "retained", ":now": int(now.timestamp())}),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise WorkBindingError("work locator is active, unconfirmed, or still retained") from None
            raise TaskStoreError("task locator cleanup unavailable") from exc
        except BotoCoreError as exc:
            raise TaskStoreError("task locator cleanup unavailable") from exc

    def mark_recovery_required(self, *, task_id: str, reason: str, expected_version: int) -> None:
        """Retain every unresolved task record until explicit recovery settlement."""
        if not isinstance(reason, str) or not reason or len(reason) > 1_000:
            raise TaskStoreError("recovery reason must contain between 1 and 1000 characters")
        snapshot = self.read_task(task_id)
        if snapshot is None:
            raise TaskStateConflictError(task_id=task_id, current_state=None)
        already_marked = snapshot.get("recovery_required") is True and int(snapshot.get("version", 0)) == expected_version + 1
        if not already_marked and int(snapshot.get("version", 0)) != expected_version:
            raise TaskStateConflictError(
                task_id=task_id,
                current_state=str(snapshot.get("state")),
                current_version=int(snapshot.get("version", 0)),
            )

        if not already_marked:
            now_iso = _iso(self._clock())
            work_records = []
            dispatch_id = snapshot.get("dispatch_id")
            if dispatch_id:
                dispatch = self._get(task_work_partition(task_id), dispatch_sort_key(str(dispatch_id)))
                if dispatch is not None:
                    work_records.append(dispatch)
            reconcile = self._get(task_work_partition(task_id), "RECONCILE")
            if reconcile is not None:
                work_records.append(reconcile)
            if snapshot.get("history_expired") or any(work.get("envelope_compacted") for work in work_records):
                raise TaskStoreError("recovery evidence has already been compacted")

            transaction: list[dict[str, Any]] = [
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": _serialize_authority({"event_id": task_partition(task_id), "arrived_at": META_SORT_KEY}),
                        "UpdateExpression": (
                            "SET recovery_required = :true, recovery_reason = :reason, execution_health = :unknown, "
                            "#version = :next, updated_at = :now REMOVE expires_at"
                        ),
                        "ConditionExpression": "#version = :expected",
                        "ExpressionAttributeNames": {"#version": "version"},
                        "ExpressionAttributeValues": _serialize_authority(
                            {
                                ":true": True,
                                ":reason": reason,
                                ":unknown": "unknown",
                                ":next": expected_version + 1,
                                ":expected": expected_version,
                                ":now": now_iso,
                            }
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": _serialize_authority({"event_id": str(snapshot["idempotency_partition"]), "arrived_at": META_SORT_KEY}),
                        "UpdateExpression": "REMOVE expires_at",
                        "ConditionExpression": "task_id = :task AND request_digest = :digest",
                        "ExpressionAttributeValues": _serialize_authority({":task": task_id, ":digest": snapshot["request_digest"]}),
                    }
                },
            ]
            for artifact_id in snapshot.get("artifact_ids", []):
                transaction.append(
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": task_artifact_partition(str(artifact_id)), "arrived_at": META_SORT_KEY}),
                            "UpdateExpression": "REMOVE expires_at",
                            "ConditionExpression": "task_id = :task AND binding_state = :bound",
                            "ExpressionAttributeValues": _serialize_authority({":task": task_id, ":bound": "bound"}),
                        }
                    }
                )
            for work in work_records:
                transaction.append(
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize_authority({"event_id": work["event_id"], "arrived_at": work["arrived_at"]}),
                            "UpdateExpression": "REMOVE expires_at",
                            "ConditionExpression": ("task_id = :task AND work_id = :work AND attribute_not_exists(envelope_compacted)"),
                            "ExpressionAttributeValues": _serialize_authority({":task": task_id, ":work": work["work_id"]}),
                        }
                    }
                )
            try:
                self._client.transact_write_items(TransactItems=transaction)
            except ClientError as exc:
                if _is_conditional_failure(exc):
                    live = self.read_task(task_id)
                    raise TaskStateConflictError(
                        task_id=task_id,
                        current_state=str(live.get("state")) if live else None,
                        current_version=int(live.get("version", 0)) if live else None,
                    ) from None
                raise TaskStoreError("task recovery retention unavailable") from exc
            except BotoCoreError as exc:
                raise TaskStoreError("task recovery retention unavailable") from exc

        self._clear_recovery_content_ttls(task_id)

    def _clear_recovery_content_ttls(self, task_id: str) -> None:
        """Idempotently clear previously stamped child TTLs after recovery latches."""
        for _, partition in _task_content_partitions(task_id):
            start_key = None
            while True:
                query: dict[str, Any] = {
                    "TableName": self._table_name,
                    "KeyConditionExpression": "event_id = :partition",
                    "ExpressionAttributeValues": {":partition": {"S": partition}},
                    "ProjectionExpression": "event_id, arrived_at, expires_at",
                    "Limit": 100,
                    "ConsistentRead": True,
                }
                if start_key is not None:
                    query["ExclusiveStartKey"] = start_key
                try:
                    response = self._client.query(**query)
                    for item in response.get("Items", []):
                        if TTL_ATTRIBUTE not in item:
                            continue
                        self._client.update_item(
                            TableName=self._table_name,
                            Key={"event_id": item["event_id"], "arrived_at": item["arrived_at"]},
                            UpdateExpression="REMOVE expires_at",
                            ConditionExpression="attribute_exists(event_id)",
                        )
                except (ClientError, BotoCoreError) as exc:
                    raise TaskStoreError("task recovery evidence retention unavailable") from exc
                start_key = response.get("LastEvaluatedKey")
                if not start_key:
                    break

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
    unclaimed_artifact = item.get("record_type") == "TASK_ARTIFACT" and item.get("binding_state") == "unclaimed"
    if TTL_ATTRIBUTE in item and item.get("record_type") in TTL_EXEMPT_WHILE_ACTIVE and not item.get("terminal_at") and not unclaimed_artifact:
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
    return {key: _SERIALIZER.serialize(_dynamodb_number(value)) for key, value in encode_record_json(item).items()}


def _serialize_authority(item: dict[str, Any]) -> dict[str, Any]:
    physical = encode_record_json(item)
    for name in (":grant_input", ":grant_model_binding", ":grant_limits"):
        if name in physical:
            physical[name] = encode_json_value(physical[name])[0]
    return {key: _SERIALIZER.serialize(_dynamodb_number(value)) for key, value in physical.items()}


def _dynamodb_number(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _dynamodb_number(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_dynamodb_number(inner) for inner in value]
    if isinstance(value, tuple):
        return tuple(_dynamodb_number(inner) for inner in value)
    return value


def _task_content_partitions(task_id: str) -> tuple[tuple[str, str], ...]:
    return (
        ("TASK_RUN", task_run_partition(task_id)),
        ("TASK_EVENTS", task_events_partition(task_id)),
        ("TASK_COMMANDS", task_commands_partition(task_id)),
        ("TASK_TURNS", task_turns_partition(task_id)),
        ("TASK_OPS", task_ops_partition(task_id)),
        ("TASK_REPORT", task_report_partition(task_id)),
    )


def _validate_command_payload(*, command_id: str, kind: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise TaskStoreError("command payload must be an object")
    if kind == "input":
        if not set(payload).issubset({"text", "reply_to"}) or "text" not in payload:
            raise TaskStoreError("input command does not match the closed v1 schema")
        text = payload["text"]
        if not isinstance(text, str) or not text or len(text) > 4_000:
            raise TaskStoreError("input command text must contain between 1 and 4000 characters")
        if "reply_to" in payload:
            validate_uuid(payload["reply_to"], "reply_to")
    elif kind == "cancel":
        if not set(payload).issubset({"reason"}):
            raise TaskStoreError("cancel command does not match the closed v1 schema")
        reason = payload.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 1_000:
            raise TaskStoreError("cancel reason exceeds 1000 characters")
    else:
        raise TaskStoreError("command kind must be input or cancel")

    public_body = {"schema_version": "1.0", "command_id": command_id, **payload}
    if len(canonical_json(public_body)) > MAX_COMMAND_BYTES:
        raise TaskStoreError("command exceeds the 16384-byte limit")


def _protected_grant_digest(grant: dict[str, Any]) -> str:
    fields = (
        "tenant",
        "canonical_principal",
        "task_id",
        "invocation_id",
        "generation",
        "request_digest",
        "persona",
        "input",
        "model_binding",
        "limits",
        "capabilities",
    )
    if any(field_name not in grant for field_name in fields):
        raise WorkBindingError("protected run grant is incomplete")
    if "budget_reservation" in grant:
        fields += ("budget_reservation",)
    return payload_digest({field_name: grant[field_name] for field_name in fields})


def _validate_run_bindings(request: AcceptanceRequest) -> None:
    immutable_input = request.immutable_input
    allowed_input = {"instructions", "inputs", "acceptance_criteria", "artifacts", "input_digest"}
    if not isinstance(immutable_input, dict) or not {"instructions", "input_digest"}.issubset(immutable_input):
        raise TaskStoreError("immutable run input is incomplete")
    if not set(immutable_input).issubset(allowed_input):
        raise TaskStoreError("immutable run input contains unknown fields")
    instructions = immutable_input.get("instructions")
    if not isinstance(instructions, str) or not instructions or len(instructions) > 16_000:
        raise TaskStoreError("immutable run instructions are invalid")
    if request.request_payload.get("instructions") != instructions:
        raise TaskStoreError("immutable run instructions disagree with the accepted request")
    if request.request_payload.get("inputs", {}) != immutable_input.get("inputs", {}):
        raise TaskStoreError("immutable run inputs disagree with the accepted request")
    if request.request_payload.get("acceptance_criteria", []) != immutable_input.get("acceptance_criteria", []):
        raise TaskStoreError("immutable acceptance criteria disagree with the accepted request")
    input_digest = immutable_input.get("input_digest")
    if input_digest != payload_digest(request.request_payload):
        raise TaskStoreError("immutable run input digest disagrees with the accepted request")
    if input_digest != request.envelope.get("input_ref", {}).get("input_digest"):
        raise TaskStoreError("immutable run input disagrees with the dispatch envelope")
    criteria = immutable_input.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or len(criteria) > 10 or any(not isinstance(value, str) or len(value) > 1_000 for value in criteria):
        raise TaskStoreError("immutable acceptance criteria exceed the fixed limits")

    payload_artifact_ids = request.request_payload.get("artifact_ids", [])
    if not isinstance(payload_artifact_ids, list) or any(not isinstance(value, str) for value in payload_artifact_ids):
        raise TaskStoreError("artifact bindings disagree with the accepted request")
    if len(payload_artifact_ids) != len(set(payload_artifact_ids)) or set(payload_artifact_ids) != set(request.artifact_ids):
        raise TaskStoreError("artifact bindings disagree with the accepted request")
    immutable_artifacts = immutable_input.get("artifacts", [])
    if not isinstance(immutable_artifacts, list) or len(immutable_artifacts) != len(request.artifact_ids):
        raise TaskStoreError("immutable artifact bindings are incomplete")
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    for artifact in immutable_artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"artifact_id", "version", "content_sha256", "content_type"}:
            raise TaskStoreError("immutable artifact binding does not match the closed v1 schema")
        artifact_id = str(artifact["artifact_id"])
        validate_artifact_id(artifact_id)
        if artifact_id in artifacts_by_id or artifact_id not in request.artifact_ids:
            raise TaskStoreError("immutable artifact identity is duplicated or unrequested")
        if not isinstance(artifact["version"], int) or isinstance(artifact["version"], bool) or artifact["version"] < 1:
            raise TaskStoreError("immutable artifact version is invalid")
        artifact_digest = artifact["content_sha256"]
        if (
            not isinstance(artifact_digest, str)
            or len(artifact_digest) != 64
            or any(character not in "0123456789abcdef" for character in artifact_digest)
        ):
            raise TaskStoreError("immutable artifact digest is invalid")
        if artifact["content_type"] not in {"text/plain", "application/json"}:
            raise TaskStoreError("immutable artifact content type is invalid")
        artifacts_by_id[artifact_id] = artifact
    envelope_refs = request.envelope.get("input_ref", {}).get("artifact_refs", [])
    if not isinstance(envelope_refs, list) or any(not isinstance(value, dict) for value in envelope_refs):
        raise TaskStoreError("dispatch envelope artifact references are invalid")
    expected_refs = [
        {
            "artifact_id": artifact["artifact_id"],
            "version": artifact["version"],
            "content_sha256": artifact["content_sha256"],
        }
        for artifact in immutable_artifacts
    ]
    if sorted(envelope_refs, key=lambda value: value.get("artifact_id", "")) != sorted(expected_refs, key=lambda value: value["artifact_id"]):
        raise TaskStoreError("immutable artifacts disagree with the dispatch envelope")

    model = request.model_binding
    required_model = {
        "model_id",
        "transport",
        "model_policy_version",
        "request_shape_version",
        "pricing_evidence_version",
        "invocability_verified",
    }
    if not isinstance(model, dict) or set(model) != required_model:
        raise TaskStoreError("model binding does not match the closed v1 schema")
    if model.get("transport") != "anthropic_messages" or model.get("invocability_verified") is not True:
        raise TaskStoreError("model binding is not verified for the supported transport")
    for field_name in required_model - {"transport", "invocability_verified"}:
        value = model.get(field_name)
        maximum = 128 if field_name == "model_id" else 64
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise TaskStoreError(f"model binding field {field_name} is invalid")

    limits = request.run_limits
    required_limits = {"max_turns", "max_output_tokens_per_turn", "max_usd", "deadline_at"}
    allowed_limits = required_limits | {
        "max_provider_operation_seconds",
        "max_events",
        "max_result_artifact_bytes",
        "heartbeat_interval_seconds",
    }
    if not isinstance(limits, dict) or not required_limits.issubset(limits) or not set(limits).issubset(allowed_limits):
        raise TaskStoreError("run limits do not match the closed v1 schema")
    bounded = {
        "max_turns": (1, 8),
        "max_output_tokens_per_turn": (1, 4_096),
        "max_usd": (0, 1),
        "max_provider_operation_seconds": (1, 120),
        "max_events": (1, 10_000),
        "max_result_artifact_bytes": (1, 1_048_576),
        "heartbeat_interval_seconds": (1, 30),
    }
    for field_name, (minimum, maximum) in bounded.items():
        if field_name not in limits:
            continue
        value = limits[field_name]
        if isinstance(value, bool) or not isinstance(value, int | float) or value < minimum or value > maximum:
            raise TaskStoreError(f"run limit {field_name} is invalid")
    if limits.get("deadline_at") != _iso(request.deadline_at):
        raise TaskStoreError("run deadline disagrees with task acceptance")


def _validated_envelope(request: AcceptanceRequest, request_digest: str) -> dict[str, Any]:
    envelope = request.envelope
    if not isinstance(envelope, dict) or frozenset(envelope) != _ENVELOPE_FIELDS:
        raise TaskStoreError("dispatch envelope does not match the closed v1 schema")
    expected = {
        "kind": "adp.task",
        "schema_version": "1.0",
        "task_id": request.task_id,
        "invocation_id": request.invocation_id,
        "message_id": request.invocation_id,
        "persona": request.persona,
        "dispatch_id": request.dispatch_id,
        "request_digest": request_digest,
    }
    if any(envelope.get(key) != value for key, value in expected.items()):
        raise TaskStoreError("dispatch envelope identity or request digest mismatch")
    input_ref = envelope.get("input_ref")
    if input_ref != request.input_reference or not isinstance(input_ref, dict):
        raise TaskStoreError("dispatch envelope input_ref is not the committed immutable input")
    if not {"record_type", "input_digest"}.issubset(input_ref) or not set(input_ref).issubset({"record_type", "input_digest", "artifact_refs"}):
        raise TaskStoreError("dispatch envelope input_ref does not match the closed v1 schema")
    input_digest = input_ref.get("input_digest")
    if input_ref.get("record_type") != "TASK":
        raise TaskStoreError("dispatch envelope input record type is invalid")
    if not isinstance(input_digest, str) or len(input_digest) != 64 or any(character not in "0123456789abcdef" for character in input_digest):
        raise TaskStoreError("dispatch envelope input digest is invalid")
    artifact_refs = input_ref.get("artifact_refs", [])
    if not isinstance(artifact_refs, list) or len(artifact_refs) > 4:
        raise TaskStoreError("dispatch envelope artifact references are invalid")
    for ref in artifact_refs:
        if not isinstance(ref, dict) or set(ref) != {"artifact_id", "version", "content_sha256"}:
            raise TaskStoreError("dispatch envelope artifact reference does not match the closed v1 schema")
        task_artifact_partition(str(ref["artifact_id"]))
        if not isinstance(ref["version"], int) or isinstance(ref["version"], bool) or ref["version"] < 1:
            raise TaskStoreError("dispatch envelope artifact version is invalid")
        digest = ref["content_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise TaskStoreError("dispatch envelope artifact digest is invalid")
    assignment = envelope.get("assignment_ref")
    if not isinstance(assignment, dict) or frozenset(assignment) != {"grant_pk", "grant_sk", "generation"}:
        raise TaskStoreError("dispatch envelope assignment_ref is invalid")
    expected_assignment = {
        "grant_pk": task_authority_partition(request.tenant),
        "grant_sk": task_run_grant_sort_key(invocation_id=request.invocation_id, generation=request.generation),
        "generation": request.generation,
    }
    if assignment != expected_assignment or request.grant_reference != assignment["grant_sk"]:
        raise TaskStoreError("dispatch envelope assignment binding mismatch")
    return dict(envelope)


def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
    """Restore JSON-compatible numbers from DynamoDB's Decimal representation.

    Safe integral numbers become integers for counters and version fences.
    Fractions and larger integral JSON floats remain floats, preserving the
    RFC8785 numeric domain when stored payloads are hashed again.
    """
    return decode_record_json({key: _normalise_numbers(_DESERIALIZER.deserialize(value)) for key, value in item.items()})


def _normalise_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Preserve integral JSON floats outside RFC8785's safe-integer domain.
        # Otherwise e.g.1e20 accepts but cannot be rehashed after a DynamoDB read.
        return int(value) if value == value.to_integral_value() and abs(value) <= 9007199254740991 else float(value)
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
    "WORK_LEASE_SECONDS",
    "AcceptanceRequest",
    "AcceptanceConditionError",
    "AcceptedTask",
    "IdempotencyConflictError",
    "StaleGenerationError",
    "TaskStateConflictError",
    "TaskStore",
    "TaskStoreError",
    "WorkBindingError",
    "WorkLeaseConflictError",
    "StaleAttemptError",
    "assert_ttl_permitted",
]
