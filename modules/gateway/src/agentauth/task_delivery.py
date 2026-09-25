"""One durable queue assignment per verified pod; no worker receipt handles.

The gateway owns SQS receive/heartbeat/delete and journals each assignment in
the protected authority table. A pod cannot choose a tenant, task, queue, receipt
or another assignment, and acknowledgement never grants it another task.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.store import AuthorityStoreError

TASK_SOURCE_FLAG = "ADP_RUN_TASKS_ENABLED"
MAX_MESSAGE_BYTES = 200 * 1024  # Below DynamoDB's item limit after metadata.
VISIBILITY_SECONDS = 300
_OP_SECONDS = 20
_LEASE_MARGIN = 30  # Exceeds the service's bounded SQS operation timeout.
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


class TaskDeliveryError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def enabled(env) -> bool:
    import os

    return (os.environ if env is None else env).get(TASK_SOURCE_FLAG, "false").lower() == "true"


class TaskDelivery:
    def __init__(self, *, store, sqs, queue_url: str, clock=time.time, allow_task_api=False, allow_legacy=True, allow_shared_legacy=False):
        if not queue_url:
            raise TaskDeliveryError("unavailable")
        self.store, self.sqs, self.queue_url, self.clock = store, sqs, queue_url, clock
        self.allow_task_api, self.allow_legacy = allow_task_api, allow_legacy
        self.allow_shared_legacy = allow_shared_legacy

    def read(self, pod_uid: str) -> dict | None:
        raw = self.store._read(f"PODTASK#{pod_uid}", "DELIVERY")
        return {k: _DESERIALIZER.deserialize(v) for k, v in raw.items()} if raw else None

    def save(self, pod_uid: str, values: dict, previous: dict | None) -> dict:
        item = {**values, "pk": f"PODTASK#{pod_uid}", "sk": "DELIVERY", "version": uuid.uuid4().hex}
        args = {
            "TableName": self.store.table,
            "Item": {k: _SERIALIZER.serialize(v) for k, v in item.items()},
            "ConditionExpression": "attribute_not_exists(pk)" if previous is None else "#version = :version",
        }
        if previous is not None:
            args.update(ExpressionAttributeNames={"#version": "version"}, ExpressionAttributeValues={":version": {"S": previous["version"]}})
        try:
            self.store.client.put_item(**args)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TaskDeliveryError("busy") from None
            raise AuthorityStoreError("task assignment unavailable") from None
        except BotoCoreError:
            raise AuthorityStoreError("task assignment unavailable") from None
        return item

    def _live(self, assignment: dict) -> None:
        if assignment.get("lease_until", 0) <= int(self.clock()) + _LEASE_MARGIN:
            raise TaskDeliveryError("lease_expired")

    def _cancelled_before_start(self, body):
        if not self.allow_task_api:
            return False
        envelope = json.loads(body)
        if not isinstance(envelope, dict) or envelope.get("kind") != "adp.task":
            return False
        from src.tasks.records import dispatch_sort_key, task_work_partition
        from src.tasks.store import TaskStore
        from src.tasks.task_commands import TaskCommands
        repository = TaskStore(dynamodb_client=self.store.client, authority_table_name=self.store.table,
            clock=lambda: datetime.fromtimestamp(self.clock(), UTC))
        task = repository.read_task(envelope.get("task_id", ""))
        if not task or task.get("dispatch_id") != envelope.get("dispatch_id"):
            return False
        work = repository._get(task_work_partition(task["task_id"]), dispatch_sort_key(task["dispatch_id"]))
        if not work or work.get("envelope") != envelope:
            return False
        return TaskCommands(repository).cancel_unstarted(task["task_id"])

    def acquire(self, pod_uid: str) -> str | None:
        now = int(self.clock())
        previous = self.read(pod_uid)
        if previous is None and self.store._read(f"POD#{pod_uid}", "BINDING") is not None:
            raise TaskDeliveryError("finished")
        if previous:
            if previous["state"] in {"assigned", "heartbeating"}:
                self._live(previous)
                if self._cancelled_before_start(previous["body"]):
                    self.maintain(pod_uid, acknowledge=True)
                    return None
                return previous["body"]
            if previous["state"] == "empty":
                return None
            if previous["state"] != "receiving":
                raise TaskDeliveryError("finished")
            if previous["operation_until"] > now:
                raise TaskDeliveryError("busy")
        # Only an expired, unfinished receive can be retried. Once delivered,
        # this pod's assignment is permanent, including after ack/lease expiry.
        reservation = self.save(pod_uid, {"state": "receiving", "operation_until": now + 30}, previous)
        try:
            response = self.sqs.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=10,
                VisibilityTimeout=VISIBILITY_SECONDS,
            )
        except (ClientError, BotoCoreError):
            raise TaskDeliveryError("unavailable") from None
        messages = response.get("Messages", [])
        if not messages:
            self.save(pod_uid, {"state": "empty"}, reservation)
            return None
        message = messages[0]
        try:
            body, receipt = message["Body"], message["ReceiptHandle"]
            if not isinstance(body, str) or len(body.encode()) > MAX_MESSAGE_BYTES or not isinstance(receipt, str) or not receipt:
                raise TaskDeliveryError("invalid_task")
            envelope = json.loads(body)
            if not isinstance(envelope, dict):
                raise TaskDeliveryError("invalid_task")
            shared_legacy = (self.allow_shared_legacy and "kind" not in envelope and envelope.get("version") == "1.0"
                and all(isinstance(envelope.get(name), str) and envelope[name] for name in ("channel", "tenant_id", "persona"))
                and isinstance(envelope.get("source_ref"), dict))
            journal_id = envelope.get("message_id")
            if not isinstance(journal_id, str) or not journal_id:
                if not shared_legacy or not isinstance(message.get("MessageId"), str):
                    raise TaskDeliveryError("invalid_task")
                journal_id = message["MessageId"]  # Queue journal only; never mints an ADP run grant.
            digest = envelope_digest(envelope)
            if envelope.get("kind") == "adp.task":
                if not self.allow_task_api:
                    raise TaskDeliveryError("invalid_task")
                from src.tasks.store import TaskStore, TaskStoreError, WorkBindingError

                if not self._cancelled_before_start(body):
                    try:
                        work = TaskStore(dynamodb_client=self.store.client, authority_table_name=self.store.table,
                            clock=lambda: datetime.fromtimestamp(self.clock(), UTC)).resolve_work(
                                envelope.get("dispatch_id", ""), expected_kind="dispatch")
                        if work.get("envelope") != envelope:
                            raise TaskDeliveryError("invalid_task")
                    except (TaskStoreError, WorkBindingError):
                        raise TaskDeliveryError("invalid_task") from None
            elif shared_legacy:
                pass  # Same normal legacy body the old shared consumer already received.
            else:
                if "kind" in envelope or not self.allow_legacy:
                    raise TaskDeliveryError("invalid_task")
                pending = self.store._read(f"INVOCATION#{envelope['message_id']}", "DISPATCH")
                if not pending or pending.get("envelope_digest") != {"S": digest}:
                    raise TaskDeliveryError("invalid_task")
            # Count from before receive: an underestimated visibility window is
            # safe; counting from a slow response could authorize an old receipt.
            assigned = {
                "state": "assigned",
                "body": body,
                "receipt": receipt,
                "sqs_message_id": message.get("MessageId"),
                "queue_url": self.queue_url,
                "invocation_id": journal_id,
                "envelope_digest": digest,
                "lease_until": now + VISIBILITY_SECONDS,
            }
            self._live(assigned)
            self.save(pod_uid, assigned, reservation)
            if self._cancelled_before_start(body):
                self.maintain(pod_uid, acknowledge=True)
                return None
            return body
        except AuthorityStoreError:
            # A failed response may hide a committed assignment. Do not release
            # that task while its durable owner may already exist; read/retry or
            # ordinary visibility expiry reconciles it without a second effect.
            raise TaskDeliveryError("unavailable") from None
        except (TaskDeliveryError, ValueError, KeyError, TypeError):
            # No body was delivered. Return the unassigned message for another
            # pod if possible; SQS redelivery is also the crash-recovery fallback.
            try:
                self.sqs.change_message_visibility(QueueUrl=self.queue_url, ReceiptHandle=message.get("ReceiptHandle", ""), VisibilityTimeout=0)
            except (ClientError, BotoCoreError):
                pass
            raise TaskDeliveryError("assignment_failed") from None

    def require_assignment(self, pod_uid: str, invocation_id: str, digest: str) -> None:
        row = self.read(pod_uid)
        if (
            not row
            or row.get("state") not in {"assigned", "heartbeating"}
            or row.get("invocation_id") != invocation_id
            or row.get("envelope_digest") != digest
        ):
            raise TaskDeliveryError("wrong_assignment")
        self._live(row)

    def maintain(self, pod_uid: str, *, acknowledge: bool) -> None:
        now = int(self.clock())
        previous = self.read(pod_uid)
        if previous is None:
            raise TaskDeliveryError("not_assigned")
        if previous["state"] == "acknowledged" and acknowledge:
            return
        self._live(previous)
        if previous["state"] in {"acking", "heartbeating"}:
            if previous["operation_until"] > now:
                raise TaskDeliveryError("busy")
            # A delete may have succeeded before its writer crashed. Never turn
            # that journal entry back into an active task through heartbeat.
            if previous["state"] == "acking" and not acknowledge:
                raise TaskDeliveryError("finished")
        elif previous["state"] != "assigned":
            raise TaskDeliveryError("finished")
        reservation = self.save(
            pod_uid,
            {
                **previous,
                "state": "acking" if acknowledge else "heartbeating",
                "operation_until": now + _OP_SECONDS,
                **({"ack_attempts": int(previous.get("ack_attempts", 0)) + 1} if acknowledge else {}),
            },
            previous,
        )
        # The conditional write may wait. Never use an entry-time lease after it.
        self._live(reservation)
        if reservation["operation_until"] <= int(self.clock()):
            raise TaskDeliveryError("busy")
        try:
            if acknowledge:
                response = self.sqs.delete_message(QueueUrl=previous["queue_url"], ReceiptHandle=previous["receipt"])
                # Retain a tombstone without task body or receipt. No second task
                # may be received by the same workload after acknowledgement.
                metadata = response.get("ResponseMetadata", {})
                self.save(
                    pod_uid,
                    {
                        "state": "acknowledged",
                        "invocation_id": previous["invocation_id"],
                        "sqs_message_id": previous.get("sqs_message_id"),
                        "receipt_handle_sha256": hashlib.sha256(previous["receipt"].encode()).hexdigest(),
                        # Reservations count conservatively: a crash before the SDK
                        # call can increase this count without a transport attempt.
                        "ack_attempts": reservation["ack_attempts"],
                        "acknowledged_at": int(self.clock()),
                        "sqs_request_id": metadata.get("RequestId"),
                        "sqs_http_status": metadata.get("HTTPStatusCode"),
                        "sqs_retry_attempts": metadata.get("RetryAttempts"),
                    },
                    reservation,
                )
            else:
                self.sqs.change_message_visibility(
                    QueueUrl=previous["queue_url"], ReceiptHandle=previous["receipt"], VisibilityTimeout=VISIBILITY_SECONDS
                )
                self.save(pod_uid, {**previous, "state": "assigned", "lease_until": now + VISIBILITY_SECONDS}, reservation)
        except (ClientError, BotoCoreError):
            raise TaskDeliveryError("unavailable") from None
