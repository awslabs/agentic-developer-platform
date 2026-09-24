"""Task API record keys, lifecycle and digests (T1, issue #5794).

Implements the storage layout accepted in
``docs/task-api/implementation-design.md`` section 6 at design revision
``b5761a4a2502aceaa9133afef552b567a19cb46e``, as frozen by T0 in
``docs/task-api/contracts/v1/identity-and-lifecycle.json``.

## Why the existing request table, and what keeps it safe

Task records live in the *existing* ``adp-<env>-webhook-events`` table (D01: reuse
the request table, do not replace it). That table already carries every GitHub
webhook delivery and powers the Agent Activity views, so the design's hard
constraint is that task records must be **invisible** to those views.

Two independent properties give that, and both are structural rather than
conventional:

1. **No legacy GSI attributes.** Activity queries select on ``tenant_id``,
   ``user_id``, ``correlation_id``, ``root_human_id`` and
   ``engine_command_status``. A DynamoDB GSI only indexes items that carry its
   hash key, so an item omitting these attributes cannot appear in any of those
   queries at all. :data:`OMITTED_LEGACY_GSI_ATTRIBUTES` names them and
   :func:`assert_legacy_invisible` enforces the omission on every item this
   module builds. Tenant scope still travels with the record, inside the nested
   ``scope`` map, where no index can reach it.

2. **No bare invocation ID as a partition.** ``activity/service.py::get_invocation``
   resolves a run detail page with ``Query(event_id == invocation_id)`` on the
   base table. Every task partition is prefixed (``TASK#``, ``TASK_EVENTS#`` …),
   so no task record can ever be returned by that lookup. This is why the design
   states ``raw_invocation_id_as_partition: false`` — it is what stops a run
   detail page resolving to a task's internal progress row.

## Fixed-width ordering keys

DynamoDB sorts string sort keys lexicographically, so ordering numbers are
zero-padded to a fixed width: sequences and turn numbers to 20 digits, generations
to 10. Unpadded, ``SEQ#10`` would sort before ``SEQ#9`` and event replay — which
reads a range of sort keys in order — would emit history out of order. The widths
are the design's, not ours; :func:`event_sort_key` and friends are the only places
that pad, so no caller can format one by hand and get it wrong.

## Digests

Idempotency asks one question: "is this the same request as the one I already
accepted?" That comparison is a hash, so the hash must be stable across JSON key
ordering and whitespace, and must not collide for genuinely different payloads.
:func:`canonical_json` implements RFC 8785 canonical JSON (the design's choice)
and :func:`component_digest` composes multi-part keys with **length-delimited**
components. Length delimiting is load-bearing: plain concatenation makes
``("ab", "c")`` and ``("a", "bc")`` hash identically, which for the idempotency
scope ``(tenant, principal, key)`` would let one tenant's key collide with
another's. The design calls this out as "versioned length-delimited components,
not ambiguous concatenation".

``src/agentauth/model_policy.py::canonical_json`` exists but is a sorted-key
``json.dumps``, which is not RFC 8785 (notably for fractional/exponential number
forms and UTF-16 property ordering), carries a policy-snapshot size ceiling and
raises ``ModelPolicyError``. Task digests therefore use the dedicated RFC 8785
implementation rather than bending that module's contract.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import rfc8785

# --------------------------------------------------------------------------
# Record namespaces (design section 6 table)
# --------------------------------------------------------------------------

TASK_NAMESPACE: Final = "TASK"
TASK_RUN_NAMESPACE: Final = "TASK_RUN"
TASK_EVENTS_NAMESPACE: Final = "TASK_EVENTS"
TASK_COMMANDS_NAMESPACE: Final = "TASK_COMMANDS"
TASK_TURNS_NAMESPACE: Final = "TASK_TURNS"
TASK_OPS_NAMESPACE: Final = "TASK_OPS"
TASK_IDEMP_NAMESPACE: Final = "TASK_IDEMP"
TASK_WORK_NAMESPACE: Final = "TASK_WORK"
TASK_REPORT_NAMESPACE: Final = "TASK_REPORT"
TASK_ARTIFACT_NAMESPACE: Final = "TASK_ARTIFACT"

#: Every task partition prefix, in the design's order. The IAM deny on the worker
#: role is generated from this same list (see the additive Terraform change), so a
#: new namespace cannot be added to the code without also being denied.
TASK_NAMESPACES: Final[tuple[str, ...]] = (
    TASK_NAMESPACE,
    TASK_RUN_NAMESPACE,
    TASK_EVENTS_NAMESPACE,
    TASK_COMMANDS_NAMESPACE,
    TASK_TURNS_NAMESPACE,
    TASK_OPS_NAMESPACE,
    TASK_IDEMP_NAMESPACE,
    TASK_WORK_NAMESPACE,
    TASK_REPORT_NAMESPACE,
    TASK_ARTIFACT_NAMESPACE,
)

#: Legacy GSI hash-key attributes a task record must never carry. Carrying any one
#: of them would project the record into an Activity view.
OMITTED_LEGACY_GSI_ATTRIBUTES: Final[frozenset[str]] = frozenset({"tenant_id", "user_id", "correlation_id", "root_human_id", "engine_command_status"})

SCHEMA_VERSION: Final = "1.0"

# Fixed widths from the design's record table.
_SEQUENCE_WIDTH: Final = 20
_TURN_WIDTH: Final = 20
_GENERATION_WIDTH: Final = 10

#: Sparse recovery index. Only work records carry these two attributes, which is
#: what keeps the index small enough to query instead of scanning the table.
WORK_INDEX_NAME: Final = "task-work-index"
WORK_SHARD_ATTRIBUTE: Final = "task_work_shard"
WORK_DUE_ATTRIBUTE: Final = "task_due"
WORK_SHARD_COUNT: Final = 16
WORK_SHARD_PREFIX: Final = "v1#"

# Protected primary-key locator in the existing authority table.  The due GSI is
# discovery only; every dispatch/recovery operation resolves this key and then
# reads the exact request-table key recorded in the binding.
TASK_WORK_LOCATOR_PREFIX: Final = "TASK_WORK_ID#"
TASK_WORK_LOCATOR_SORT_KEY: Final = "BINDING"

#: Epoch-millisecond due times are zero-padded so the index sorts chronologically
#: as strings, for the same reason sequence numbers are padded.
_DUE_WIDTH: Final = 13

_TASK_ID_PATTERN: Final = re.compile(r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_UUID_PATTERN: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_ARTIFACT_ID_PATTERN: Final = re.compile(r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class TaskRecordError(ValueError):
    """A record key or item violates the frozen storage contract.

    A ``ValueError`` subclass because every raise here is a caller passing a
    malformed identifier or a would-be-legacy-visible item: a programming error
    at the boundary, not a transient storage condition worth retrying.
    """


def _require(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise TaskRecordError(f"{label} does not match the frozen contract pattern")
    return value


def validate_task_id(task_id: str) -> str:
    """Return ``task_id`` if it is a contract-shaped ``tsk_<UUIDv4>``.

    Identifiers are validated at the point they enter a key rather than trusted,
    because an unvalidated identifier containing ``#`` could forge a different
    record's sort key — e.g. a "task id" of ``x#META`` landing on another
    partition's metadata row.
    """
    return _require(task_id, _TASK_ID_PATTERN, "task_id")


def validate_uuid(value: str, label: str = "identifier") -> str:
    """Return ``value`` if it is a contract-shaped bare UUIDv4."""
    return _require(value, _UUID_PATTERN, label)


def validate_artifact_id(artifact_id: str) -> str:
    """Return ``artifact_id`` if it is a contract-shaped ``art_<UUIDv4>``."""
    return _require(artifact_id, _ARTIFACT_ID_PATTERN, "artifact_id")


# --------------------------------------------------------------------------
# Partition (event_id) and sort (arrived_at) keys
# --------------------------------------------------------------------------


def task_partition(task_id: str) -> str:
    """``TASK#<task_id>`` — the task snapshot and counter row's partition."""
    return f"{TASK_NAMESPACE}#{validate_task_id(task_id)}"


def task_run_partition(task_id: str) -> str:
    """``TASK_RUN#<task_id>`` — run history, one row per generation."""
    return f"{TASK_RUN_NAMESPACE}#{validate_task_id(task_id)}"


def task_events_partition(task_id: str) -> str:
    """``TASK_EVENTS#<task_id>`` — ordered events, replayed as a sort-key range."""
    return f"{TASK_EVENTS_NAMESPACE}#{validate_task_id(task_id)}"


def task_commands_partition(task_id: str) -> str:
    """``TASK_COMMANDS#<task_id>`` — input and cancellation commands."""
    return f"{TASK_COMMANDS_NAMESPACE}#{validate_task_id(task_id)}"


def task_turns_partition(task_id: str) -> str:
    """``TASK_TURNS#<task_id>`` — the canonical conversation transcript."""
    return f"{TASK_TURNS_NAMESPACE}#{validate_task_id(task_id)}"


def task_ops_partition(task_id: str) -> str:
    """``TASK_OPS#<task_id>`` — model operation claims and receipts."""
    return f"{TASK_OPS_NAMESPACE}#{validate_task_id(task_id)}"


def task_work_partition(task_id: str) -> str:
    """``TASK_WORK#<task_id>`` — publication intent and reconciliation state."""
    return f"{TASK_WORK_NAMESPACE}#{validate_task_id(task_id)}"


def task_report_partition(task_id: str) -> str:
    """``TASK_REPORT#<task_id>`` — producer report deduplication."""
    return f"{TASK_REPORT_NAMESPACE}#{validate_task_id(task_id)}"


def task_artifact_partition(artifact_id: str) -> str:
    """``TASK_ARTIFACT#<artifact_id>`` — immutable artifact binding.

    Partitioned by artifact rather than by task: an artifact is uploaded before
    any task references it (the design allows unclaimed uploads that expire after
    24 hours), so at write time there is no task to key it under.
    """
    return f"{TASK_ARTIFACT_NAMESPACE}#{validate_artifact_id(artifact_id)}"


def task_work_locator_partition(work_id: str) -> str:
    """``TASK_WORK_ID#<work_uuid>`` in the protected authority table."""
    return f"{TASK_WORK_LOCATOR_PREFIX}{validate_uuid(work_id, 'work_id')}"


def task_authority_partition(tenant: str) -> str:
    """Tenant partition used by protected task bindings and run grants."""
    if not isinstance(tenant, str) or not tenant or "#" in tenant:
        raise TaskRecordError("tenant must be a non-empty identifier without '#'")
    return f"TENANT#{tenant}"


def task_binding_sort_key(task_id: str) -> str:
    """Protected immutable task binding key."""
    return f"TASK#{validate_task_id(task_id)}"


def task_run_grant_sort_key(*, invocation_id: str, generation: int) -> str:
    """Protected run-grant key, sharing the request-table generation width."""
    return f"TASK_RUN#{validate_uuid(invocation_id, 'invocation_id')}#GEN#{_pad(generation, _GENERATION_WIDTH, 'generation')}"


def task_policy_sort_key(canonical_principal: str) -> str:
    """Protected service policy key used as an acceptance condition."""
    if not isinstance(canonical_principal, str) or not canonical_principal or "#" in canonical_principal:
        raise TaskRecordError("canonical_principal must be a non-empty identifier without '#'")
    return f"TASK_POLICY#{canonical_principal}"


def task_capacity_partition(scope_hash: str) -> str:
    """Protected nonterminal-capacity partition."""
    if not isinstance(scope_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", scope_hash):
        raise TaskRecordError("capacity scope hash must be a lowercase SHA-256 digest")
    return f"TASK_CAPACITY#{scope_hash}"


def idempotency_partition(*, tenant: str, canonical_principal: str, idempotency_key: str) -> str:
    """``TASK_IDEMP#<SHA256(tenant, principal, key)>``.

    The scope is tenant + canonical principal + key, so one tenant's key choice
    can never collide with another's, and a caller cannot reach another
    principal's task by guessing a key. Hashed with length-delimited components
    (see :func:`component_digest`) precisely so that a tenant containing a
    delimiter cannot be made to collide with a different tenant/key split.
    """
    if not tenant or not canonical_principal or not idempotency_key:
        raise TaskRecordError("tenant, canonical_principal and idempotency_key are required")
    digest = component_digest("task-idemp-v1", tenant, canonical_principal, idempotency_key)
    return f"{TASK_IDEMP_NAMESPACE}#{digest}"


META_SORT_KEY: Final = "META"
RECONCILE_SORT_KEY: Final = "RECONCILE"


def run_sort_key(*, invocation_id: str, generation: int) -> str:
    """``RUN#<invocation_id>#GEN#<10-digit generation>``."""
    return f"RUN#{validate_uuid(invocation_id, 'invocation_id')}#GEN#{_pad(generation, _GENERATION_WIDTH, 'generation')}"


def event_sort_key(sequence: int) -> str:
    """``SEQ#<20-digit sequence>`` — padded so a range read returns events in order."""
    return f"SEQ#{_pad(sequence, _SEQUENCE_WIDTH, 'sequence')}"


def command_sort_key(command_id: str) -> str:
    """``CMD#<command_id>``.

    Keyed by the caller's command UUID, not by arrival order, because that is
    what makes "apply this command at most once" a single conditional insert.
    FIFO ordering is carried by a separate monotonic command sequence attribute —
    random UUIDs sort arbitrarily, so the key cannot supply order.
    """
    return f"CMD#{validate_uuid(command_id, 'command_id')}"


def turn_sort_key(turn_number: int) -> str:
    """``TURN#<20-digit turn number>``."""
    return f"TURN#{_pad(turn_number, _TURN_WIDTH, 'turn_number')}"


def model_operation_sort_key(turn_id: str) -> str:
    """``MODEL#<turn_id>`` — one model operation per turn."""
    return f"MODEL#{validate_uuid(turn_id, 'turn_id')}"


def dispatch_sort_key(dispatch_id: str) -> str:
    """``DISPATCH#<dispatch_id>`` — the stable FIFO deduplication identity."""
    return f"DISPATCH#{validate_uuid(dispatch_id, 'dispatch_id')}"


def report_sort_key(*, generation: int, report_id: str) -> str:
    """``REPORT#<generation>#<report_id>``.

    Generation is part of the key so a superseded worker's retried report lands on
    its own row instead of overwriting the live generation's.
    """
    return f"REPORT#{_pad(generation, _GENERATION_WIDTH, 'generation')}#{validate_uuid(report_id, 'report_id')}"


def _pad(value: int, width: int, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskRecordError(f"{label} must be an integer")
    if value < 1:
        raise TaskRecordError(f"{label} must be a positive integer")
    text = str(value)
    if len(text) > width:
        raise TaskRecordError(f"{label} exceeds its {width}-digit fixed width")
    return text.zfill(width)


# --------------------------------------------------------------------------
# Sparse work index
# --------------------------------------------------------------------------


def work_shard(task_id: str) -> str:
    """``v1#<00..15>`` derived from the task hash.

    Sharding spreads recovery writes across partitions so a burst of due work does
    not concentrate on one. It is derived from the task ID rather than assigned by
    a counter so that any component can recompute a task's shard without a read.
    The ``v1#`` prefix makes a future reshard a new key space instead of a silent
    reinterpretation of existing rows.
    """
    digest = hashlib.sha256(validate_task_id(task_id).encode("utf-8")).digest()
    return f"{WORK_SHARD_PREFIX}{digest[0] % WORK_SHARD_COUNT:02d}"


def work_due_key(*, due_at: datetime, work_id: str) -> str:
    """Fixed-width epoch milliseconds plus the work ID.

    The work ID suffix breaks ties: two records due in the same millisecond would
    otherwise collide in a sparse index whose sort key must stay unique per item.
    """
    if due_at.tzinfo is None:
        raise TaskRecordError("due_at must be timezone-aware")
    millis = int(due_at.astimezone(UTC).timestamp() * 1000)
    if millis < 0:
        raise TaskRecordError("due_at must not precede the epoch")
    if len(str(millis)) > _DUE_WIDTH:
        raise TaskRecordError("due_at exceeds the fixed-width millisecond encoding")
    if not work_id:
        raise TaskRecordError("work_id is required")
    return f"{str(millis).zfill(_DUE_WIDTH)}#{work_id}"


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


class TaskState(StrEnum):
    """The eight task states in the frozen lifecycle contract."""

    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: Final[frozenset[TaskState]] = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})

#: Exactly the design's transition table. Encoded as data rather than scattered
#: ``if`` branches so that the permitted set is checkable against the frozen
#: contract file by a test, and so an illegal transition has one place to fail.
#:
#: Two entries carry the honesty rules the design names explicitly:
#: ``cancel_requested`` cannot reach ``completed`` (a cancelled task must never be
#: reported as success), and every terminal state permits nothing at all.
PERMITTED_TRANSITIONS: Final[dict[TaskState, frozenset[TaskState]]] = {
    TaskState.ACCEPTED: frozenset({TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCEL_REQUESTED, TaskState.FAILED}),
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.CANCEL_REQUESTED, TaskState.FAILED}),
    TaskState.RUNNING: frozenset({TaskState.WAITING_FOR_INPUT, TaskState.CANCEL_REQUESTED, TaskState.COMPLETED, TaskState.FAILED}),
    TaskState.WAITING_FOR_INPUT: frozenset({TaskState.RUNNING, TaskState.CANCEL_REQUESTED, TaskState.FAILED}),
    TaskState.CANCEL_REQUESTED: frozenset({TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class TaskTransitionError(TaskRecordError):
    """An attempted state change is not permitted from the current state."""


def is_terminal(state: TaskState) -> bool:
    """True when no further transition is permitted from ``state``."""
    return state in TERMINAL_STATES


def validate_transition(current: TaskState, target: TaskState) -> None:
    """Raise :class:`TaskTransitionError` unless ``current -> target`` is permitted.

    This is the in-process guard. It is *not* the concurrency fence — two callers
    can both pass this check against the same read state. The fence is the
    version-conditional write in the store, so this guard exists to reject a
    caller's logic error with a clear error rather than to serialise writers.
    """
    permitted = PERMITTED_TRANSITIONS.get(current)
    if permitted is None:
        raise TaskTransitionError(f"unknown current task state {current!r}")
    if target not in permitted:
        if is_terminal(current):
            raise TaskTransitionError(f"{current.value} is a terminal outcome and cannot transition to {target.value}")
        raise TaskTransitionError(f"{current.value} -> {target.value} is not a permitted transition")


# --------------------------------------------------------------------------
# Canonical JSON and digests
# --------------------------------------------------------------------------


def canonical_json(value: Any) -> bytes:
    """Serialise ``value`` to RFC 8785 canonical JSON bytes.

    Public task inputs permit every finite JSON number, not only integers. The
    RFC implementation supplies ECMAScript number formatting, UTF-16 property
    ordering and the I-JSON numeric-domain checks needed for cross-language
    request and command digests.
    """
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError) as exc:
        raise TaskRecordError(f"value is not canonicalisable JSON: {exc}") from None


def payload_digest(value: Any) -> str:
    """SHA-256 of the canonical JSON form of ``value``, hex encoded.

    Used for the request digest that decides idempotent replay versus conflict, so
    two logically identical submissions must produce the same digest and two
    different ones must not.
    """
    return hashlib.sha256(canonical_json(value)).hexdigest()


def component_digest(version: str, *components: str) -> str:
    """SHA-256 over length-delimited components, hex encoded.

    Each component is encoded as its UTF-8 byte length, a colon, then its bytes —
    so ``("ab", "c")`` and ``("a", "bc")`` produce different digests. Plain
    concatenation or a delimiter character would not: a delimiter can appear
    inside a tenant ID or an idempotency key (the contract permits any printable
    ASCII there), and the moment it does, two distinct scopes collide and one
    caller reads another's task.

    ``version`` is a domain-separation label, so the same inputs hashed for a
    different purpose cannot produce a matching digest, and so a future change to
    the composition rule can be introduced without reinterpreting stored keys.
    """
    if not version:
        raise TaskRecordError("a digest version label is required")
    hasher = hashlib.sha256()
    for component in (version, *components):
        if not isinstance(component, str):
            raise TaskRecordError("digest components must be strings")
        encoded = component.encode("utf-8")
        hasher.update(f"{len(encoded)}:".encode("ascii"))
        hasher.update(encoded)
    return hasher.hexdigest()


# --------------------------------------------------------------------------
# Item construction
# --------------------------------------------------------------------------


def assert_legacy_invisible(item: dict[str, Any]) -> dict[str, Any]:
    """Return ``item`` unless it carries a legacy GSI attribute.

    Called on every task item before it is written. The check is cheap and the
    failure it prevents is expensive and silent: a task record carrying
    ``tenant_id`` would be projected into ``tenant-index`` and start appearing in
    an operator's Agent Activity list as a phantom run, which no task-side test
    would notice. Failing the write is strictly better than that.
    """
    present = OMITTED_LEGACY_GSI_ATTRIBUTES.intersection(item)
    if present:
        raise TaskRecordError(f"task records must omit legacy GSI attributes: {sorted(present)}")
    return item


def base_item(
    *,
    partition: str,
    sort_key: str,
    record_type: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Build the attributes every task record carries.

    ``scope`` is a nested map holding tenant and canonical principal. Nesting is
    the point: the same values as top-level attributes would be indexable, and
    ``tenant_id`` at the top level is exactly what ``tenant-index`` projects. A
    nested map is unreachable by any GSI on this table, so scope can travel with
    the record without becoming legacy-visible.
    """
    if record_type not in TASK_NAMESPACES:
        raise TaskRecordError(f"unknown task record_type {record_type!r}")
    if not scope.get("tenant") or not scope.get("canonical_principal"):
        raise TaskRecordError("scope must carry tenant and canonical_principal")
    return assert_legacy_invisible(
        {
            "event_id": partition,
            "arrived_at": sort_key,
            "record_type": record_type,
            "schema_version": SCHEMA_VERSION,
            "scope": dict(scope),
        }
    )
