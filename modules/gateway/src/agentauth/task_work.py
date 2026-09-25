"""Dispatch HTTP adapter over the task repository's acceptance and lease contract.

T1 owns persistence, authority validation and fencing. Keeping those operations
in one repository prevents acceptance and publication from inventing different
representations of the same protected work record.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from boto3.dynamodb.types import TypeSerializer

from src.tasks.records import work_shard as work_shard
from src.tasks.store import TaskStore, TaskStoreError, WorkBindingError, WorkLeaseConflictError

MAX_WORK_RECORDS_PER_INVOCATION = 100
_SERIALIZER = TypeSerializer()


class TaskWorkError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TaskWorkUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class BoundWork:
    work_id: str
    task_id: str
    tenant_id: str
    kind: str
    work: dict
    task: dict
    envelope: dict | None


def encode_cursor(key: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(key, sort_keys=True).encode()).decode()


def decode_cursor(cursor: str) -> dict:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, TypeError, binascii.Error):
        raise TaskWorkError("invalid_cursor") from None
    if not isinstance(value, dict) or set(value) != {"event_id", "arrived_at", "task_work_shard", "task_due"}:
        raise TaskWorkError("invalid_cursor")
    if not all(isinstance(item, str) and item for item in value.values()):
        raise TaskWorkError("invalid_cursor")
    return value


class TaskWorkStore:
    def __init__(self, *, dynamodb_client, table_name=None, authority_table_name=None, clock=time.time):
        self.clock = clock
        self.repository = TaskStore(
            dynamodb_client=dynamodb_client,
            table_name=table_name,
            authority_table_name=authority_table_name,
            clock=lambda: datetime.fromtimestamp(clock(), UTC),
        )

    @staticmethod
    def _uuid(work_id):
        try:
            parsed = uuid.UUID(work_id)
            valid = parsed.version == 4 and str(parsed) == work_id
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise TaskWorkError("invalid_work_id")

    @staticmethod
    def _call(operation, **kwargs):
        try:
            return operation(**kwargs)
        except WorkLeaseConflictError:
            raise TaskWorkError("stale_lease") from None
        except WorkBindingError:
            raise TaskWorkError("binding_mismatch") from None
        except TaskStoreError:
            raise TaskWorkUnavailableError("task work store unavailable") from None

    def _bound(self, record):
        task = self._call(self.repository.read_task, task_id=record["task_id"])
        if task is None:
            raise TaskWorkError("not_found")
        # Only the HTTP adapter's in-memory projection has these names. Stored
        # records and all writes retain the single T1 representation.
        projected = dict(record)
        due_at = record["created_at"]
        if record.get("task_due"):
            due_at = datetime.fromtimestamp(int(record["task_due"].split("#")[0]) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        projected.setdefault("due_at", due_at)
        projected["queue_ack_status"] = task.get("queue_ack_status", "pending")
        return BoundWork(
            work_id=record["work_id"],
            task_id=record["task_id"],
            tenant_id=task["scope"]["tenant"],
            kind=record["work_kind"],
            work={key: _SERIALIZER.serialize(value) for key, value in projected.items()},
            task=task,
            envelope=record.get("envelope"),
        )

    def resolve(self, work_id: str, *, expected_kind=None):
        self._uuid(work_id)
        record = self._call(self.repository.resolve_work, work_id=work_id, expected_kind=expected_kind)
        return self._bound(record)

    def claim_publication(self, dispatch_id):
        self._uuid(dispatch_id)
        self._call(self.repository.claim_dispatch, dispatch_id=dispatch_id)
        return self.resolve(dispatch_id, expected_kind="dispatch")

    def settle_publication(self, *, dispatch_id, lease_token, publication_outcome, sqs_message_id):
        self._uuid(dispatch_id)
        if publication_outcome not in {"confirmed", "unknown", "failed"} or not lease_token:
            raise TaskWorkError("invalid_settlement")
        if (publication_outcome == "confirmed") != bool(sqs_message_id):
            raise TaskWorkError("invalid_settlement")
        self._call(
            self.repository.settle_dispatch,
            dispatch_id=dispatch_id,
            lease_token=lease_token,
            publication_outcome=publication_outcome,
            sqs_message_id=sqs_message_id,
        )
        return self.resolve(dispatch_id, expected_kind="dispatch")

    def claim_recovery(self, *, shard, cursor=None, limit=MAX_WORK_RECORDS_PER_INVOCATION):
        if shard not in {f"v1#{value:02d}" for value in range(16)} or not 1 <= limit <= MAX_WORK_RECORDS_PER_INVOCATION:
            raise TaskWorkError("invalid_claim")
        key = decode_cursor(cursor) if cursor else None
        if key and key["task_work_shard"] != shard:
            raise TaskWorkError("invalid_cursor")
        page = self._call(
            self.repository.claim_due_work_page, shard=shard, now=datetime.fromtimestamp(self.clock(), UTC), limit=limit, exclusive_start_key=key
        )
        return [self.resolve(item["work_id"]) for item in page["work"]], encode_cursor(page["next_key"]) if page["next_key"] else None

    def settle_recovery(self, *, work_id, lease_token, evidence_kind, observed, observed_at):
        self.resolve(work_id)
        # A caller's observation is never authoritative evidence. The repository
        # validates both the persisted evidence and the current lease before writes.
        self._call(
            self.repository.settle_recovery,
            work_id=work_id,
            lease_token=lease_token,
            evidence_kind=evidence_kind,
            observed=observed,
            observed_at=observed_at,
        )
        return "confirmed", self.resolve(work_id)

    def task_status(self, task_id):
        task = self._call(self.repository.read_task, task_id=task_id)
        if task is None:
            raise TaskWorkError("not_found")
        return task["state"]
