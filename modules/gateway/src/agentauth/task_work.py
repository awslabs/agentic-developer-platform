"""Protected task publication and recovery work.

The due-time GSI only discovers candidates. Every claim resolves an opaque work
UUID through the protected authority-table locator, consistently validates the
work/task/grant bindings, and then applies a version-fenced lease. Publication
and recovery leases are intentionally independent.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.bootstrap import envelope_digest

logger = logging.getLogger("bedrockgateway.agentauth.task_work")

WEBHOOK_EVENTS_TABLE_ENV = "WEBHOOK_EVENTS_TABLE"
AUTHORITY_TABLE_ENV = "AGENT_AUTHORITY_TABLE"
_DEFAULT_WEBHOOK_EVENTS_TABLE = "adp-dev-webhook-events"
_DEFAULT_AUTHORITY_TABLE = "adp-dev-agent-authority"

TASK_WORK_PREFIX = "TASK_WORK#"
TASK_LOCATOR_PREFIX = "TASK_WORK_ID#"
DISPATCH_SORT_PREFIX = "DISPATCH#"
RECONCILE_SORT_KEY = "RECONCILE"
LOCATOR_SORT_KEY = "BINDING"
TASK_WORK_INDEX = "task-work-index"
SHARD_ATTRIBUTE = "task_work_shard"
DUE_ATTRIBUTE = "task_due"
SHARD_COUNT = 16
SHARD_VERSION = "v1"
WORK_LEASE_SECONDS = 45
MAX_WORK_RECORDS_PER_INVOCATION = 100
MAX_INVOCATION_SECONDS = 30
MAX_PUBLICATION_TRIES = 5
PUBLICATION_TRY_WINDOW_MINUTES = 10
DISPATCH_KIND = "dispatch"
WORK_KINDS = (DISPATCH_KIND, "execution", "queue_ack", "cleanup")
PUBLICATION_OUTCOMES = ("confirmed", "unknown", "failed")
TASK_STATUSES = {
    "accepted", "queued", "running", "waiting_for_input", "cancel_requested",
    "completed", "failed", "cancelled",
}
_GRANT_SK = re.compile(r"^TASK_RUN#([0-9a-f-]{36})#GEN#([0-9]{10})$")
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


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
    locator: dict
    work: dict
    task: dict
    envelope: dict | None


def work_shard(task_id: str) -> str:
    if not task_id:
        raise TaskWorkError("invalid_task_id")
    digest = hashlib.sha256(task_id.encode()).digest()
    return f"{SHARD_VERSION}#{digest[0] % SHARD_COUNT:02d}"


def due_key(due_at: datetime, work_id: str) -> str:
    millis = int(due_at.astimezone(UTC).timestamp() * 1000)
    return f"{millis:013d}#{work_id}"


def _due_upper_bound(now: datetime) -> str:
    return f"{int(now.astimezone(UTC).timestamp() * 1000):013d}#\uffff"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def encode_cursor(key: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(key, sort_keys=True).encode()).decode()


def decode_cursor(cursor: str) -> dict:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, TypeError, binascii.Error):
        raise TaskWorkError("invalid_cursor") from None
    if not isinstance(value, dict):
        raise TaskWorkError("invalid_cursor")
    return value


def _string(item: dict, name: str) -> str:
    value = item.get(name, {}).get("S")
    if not isinstance(value, str) or not value:
        raise TaskWorkError("corrupt_binding")
    return value


def _number(item: dict, name: str) -> int:
    try:
        return int(item[name]["N"])
    except (KeyError, TypeError, ValueError):
        raise TaskWorkError("corrupt_binding") from None


def _scope_tenant(item: dict) -> str:
    try:
        value = item["scope"]["M"]["tenant_id"]["S"]
    except (KeyError, TypeError):
        raise TaskWorkError("corrupt_binding") from None
    if not value:
        raise TaskWorkError("corrupt_binding")
    return value


def _is_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _plain_json(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value


def _is_revoked(item: dict, now: datetime) -> bool:
    expires = _parse_iso(item.get("expires_at", {}).get("S"))
    return (
        item.get("revoked", {}).get("BOOL") is True
        or item.get("status", {}).get("S") in {"revoked", "expired", "superseded", "disabled"}
        or (expires is not None and expires <= now)
    )


class TaskWorkStore:
    """Repository consumed by T1 acceptance and the four T3 adapters."""

    def __init__(
        self, *, dynamodb_client, table_name: str | None = None,
        authority_table_name: str | None = None, authority_client=None,
        clock=time.time,
    ):
        self.table = table_name or os.environ.get(WEBHOOK_EVENTS_TABLE_ENV, _DEFAULT_WEBHOOK_EVENTS_TABLE)
        self.authority_table = authority_table_name or os.environ.get(AUTHORITY_TABLE_ENV, _DEFAULT_AUTHORITY_TABLE)
        if not self.table or not self.authority_table:
            raise TaskWorkUnavailableError("task work tables are required")
        self.client = dynamodb_client
        self.authority_client = authority_client or dynamodb_client
        self.clock = clock

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=UTC)

    @staticmethod
    def _work_key(task_id: str, arrived_at: str) -> dict:
        return {"event_id": {"S": f"{TASK_WORK_PREFIX}{task_id}"}, "arrived_at": {"S": arrived_at}}

    @staticmethod
    def _task_key(task_id: str) -> dict:
        return {"event_id": {"S": f"TASK#{task_id}"}, "arrived_at": {"S": "META"}}

    @staticmethod
    def _authority_key(pk: str, sk: str) -> dict:
        return {"pk": {"S": pk}, "sk": {"S": sk}}

    def _get(self, client, table: str, key: dict) -> dict | None:
        try:
            return client.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError):
            raise TaskWorkUnavailableError("task work store unavailable") from None

    def transaction_items(
        self, *, task_id: str, kind: str, tenant_id: str,
        deadline_at: datetime, envelope: dict | None = None,
        work_id: str | None = None, invocation_id: str | None = None,
        generation: int | None = None, due_at: datetime | None = None,
    ) -> tuple[str, list[dict]]:
        """Build locator/work puts for T1's all-or-nothing acceptance transaction."""
        if kind not in WORK_KINDS or not task_id or not tenant_id:
            raise TaskWorkError("invalid_work_binding")
        if kind == DISPATCH_KIND:
            if not isinstance(envelope, dict):
                raise TaskWorkError("envelope_required")
            dispatch_id = envelope.get("dispatch_id")
            if not _is_uuid4(dispatch_id) or work_id not in (None, dispatch_id):
                raise TaskWorkError("invalid_dispatch_id")
            work_id = dispatch_id
            invocation_id = envelope.get("invocation_id")
            generation = (envelope.get("assignment_ref") or {}).get("generation")
            if envelope.get("task_id") != task_id or envelope.get("message_id") != invocation_id:
                raise TaskWorkError("envelope_binding_mismatch")
        elif work_id is None:
            work_id = str(uuid.uuid4())
        if not _is_uuid4(work_id):
            raise TaskWorkError("invalid_work_id")

        now = self._now()
        due = due_at or now
        arrived_at = f"{DISPATCH_SORT_PREFIX}{work_id}" if kind == DISPATCH_KIND else RECONCILE_SORT_KEY
        protected_digest = envelope_digest(envelope) if envelope is not None else hashlib.sha256(
            json.dumps(
                {"kind": kind, "task_id": task_id, "work_id": work_id,
                 "invocation_id": invocation_id, "generation": generation},
                sort_keys=True, separators=(",", ":"),
            ).encode()
        ).hexdigest()
        common = {
            "schema_version": {"S": "1.0"}, "work_id": {"S": work_id},
            "task_id": {"S": task_id}, "kind": {"S": kind},
            "scope": {"M": {"tenant_id": {"S": tenant_id}}},
            "protected_digest": {"S": protected_digest},
        }
        if invocation_id:
            common["invocation_id"] = {"S": invocation_id}
        if generation is not None:
            common["generation"] = {"N": str(generation)}
        work = {
            **self._work_key(task_id, arrived_at), **common,
            "record_type": {"S": "TASK_WORK"},
            "publication_outcome": {"S": "pending"},
            "queue_ack_status": {"S": "pending"},
            "publication_tries": {"N": "0"},
            "deadline_at": {"S": _iso(deadline_at)}, "due_at": {"S": _iso(due)},
            "created_at": {"S": _iso(now)}, "version": {"S": secrets.token_hex(16)},
            SHARD_ATTRIBUTE: {"S": work_shard(task_id)},
            DUE_ATTRIBUTE: {"S": due_key(due, work_id)},
        }
        if envelope is not None:
            work["envelope"] = _SERIALIZER.serialize(envelope)
            work["envelope_digest"] = {"S": protected_digest}
        locator = {
            **self._authority_key(f"{TASK_LOCATOR_PREFIX}{work_id}", LOCATOR_SORT_KEY),
            **common, "record_type": {"S": "TASK_WORK_LOCATOR"},
            "work_event_id": work["event_id"], "work_arrived_at": work["arrived_at"],
            "created_at": {"S": _iso(now)},
        }
        return work_id, [
            {"Put": {"TableName": self.authority_table, "Item": locator,
                     "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
            {"Put": {"TableName": self.table, "Item": work,
                     "ConditionExpression": "attribute_not_exists(event_id) AND attribute_not_exists(arrived_at)"}},
        ]

    def replacement_transaction_items(self, *, previous_work_id: str, **kwargs) -> tuple[str, list[dict]]:
        """Build an atomic RECONCILE replacement without retargeting the old locator."""
        previous = self.resolve(previous_work_id)
        if previous.kind == DISPATCH_KIND or _string(previous.work, "arrived_at") != RECONCILE_SORT_KEY:
            raise TaskWorkError("invalid_replacement")
        kwargs.pop("work_id", None)
        new_work_id, items = self.transaction_items(work_id=str(uuid.uuid4()), **kwargs)
        replacement = items[1]["Put"]
        replacement["ConditionExpression"] = "work_id = :previous_work_id AND #version = :previous_version"
        replacement["ExpressionAttributeNames"] = {"#version": "version"}
        replacement["ExpressionAttributeValues"] = {
            ":previous_work_id": {"S": previous_work_id},
            ":previous_version": previous.work["version"],
        }
        return new_work_id, items

    def put_work(self, **kwargs) -> str:
        """Atomically create a work record and immutable protected locator."""
        work_id, items = self.transaction_items(**kwargs)
        try:
            self.client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "TransactionCanceledException", "ConditionalCheckFailedException"
            }:
                raise TaskWorkError("already_exists") from None
            raise TaskWorkUnavailableError("task work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("task work store unavailable") from None
        return work_id

    def resolve(self, work_id: str, *, expected_kind: str | None = None) -> BoundWork:
        """Resolve an opaque work UUID and validate every immutable binding."""
        if not _is_uuid4(work_id):
            raise TaskWorkError("invalid_work_id")
        locator = self._get(
            self.authority_client, self.authority_table,
            self._authority_key(f"{TASK_LOCATOR_PREFIX}{work_id}", LOCATOR_SORT_KEY),
        )
        if locator is None:
            raise TaskWorkError("not_found")
        if _string(locator, "work_id") != work_id or _string(locator, "record_type") != "TASK_WORK_LOCATOR":
            raise TaskWorkError("binding_mismatch")
        task_id, tenant_id, kind = _string(locator, "task_id"), _scope_tenant(locator), _string(locator, "kind")
        if expected_kind is not None and kind != expected_kind:
            raise TaskWorkError("binding_mismatch")
        event_id, arrived_at = _string(locator, "work_event_id"), _string(locator, "work_arrived_at")
        if event_id != f"{TASK_WORK_PREFIX}{task_id}":
            raise TaskWorkError("binding_mismatch")
        if kind == DISPATCH_KIND and arrived_at != f"{DISPATCH_SORT_PREFIX}{work_id}":
            raise TaskWorkError("binding_mismatch")
        if kind != DISPATCH_KIND and arrived_at != RECONCILE_SORT_KEY:
            raise TaskWorkError("binding_mismatch")
        work = self._get(self.client, self.table, {
            "event_id": {"S": event_id}, "arrived_at": {"S": arrived_at}
        })
        task = self._get(self.client, self.table, self._task_key(task_id))
        if work is None or task is None:
            raise TaskWorkError("not_found")
        for item in (work, task):
            if _string(item, "task_id") != task_id or _scope_tenant(item) != tenant_id:
                raise TaskWorkError("binding_mismatch")
        if (
            _string(work, "work_id") != work_id
            or _string(work, "kind") != kind
            or _string(work, "protected_digest") != _string(locator, "protected_digest")
            or _string(task, "status") not in TASK_STATUSES
        ):
            raise TaskWorkError("binding_mismatch")

        locator_invocation = locator.get("invocation_id", {}).get("S")
        work_invocation = work.get("invocation_id", {}).get("S")
        locator_generation = locator.get("generation", {}).get("N")
        work_generation = work.get("generation", {}).get("N")
        if locator_invocation != work_invocation or locator_generation != work_generation:
            raise TaskWorkError("binding_mismatch")
        if (locator_invocation is None) != (locator_generation is None):
            raise TaskWorkError("binding_mismatch")

        now = self._now()
        binding = self._get(
            self.authority_client, self.authority_table,
            self._authority_key(f"TENANT#{tenant_id}", f"TASK#{task_id}"),
        )
        if binding is None or _is_revoked(binding, now):
            raise TaskWorkError("authority_refused")
        if _string(binding, "task_id") != task_id or _scope_tenant(binding) != tenant_id:
            raise TaskWorkError("binding_mismatch")
        if locator_invocation is not None:
            try:
                generation = int(locator_generation)
            except (TypeError, ValueError):
                raise TaskWorkError("corrupt_binding") from None
            for item in (task, binding):
                if _string(item, "invocation_id") != locator_invocation or _number(item, "generation") != generation:
                    raise TaskWorkError("binding_mismatch")
            grant = self._get(
                self.authority_client,
                self.authority_table,
                self._authority_key(
                    f"TENANT#{tenant_id}",
                    f"TASK_RUN#{locator_invocation}#GEN#{generation:010d}",
                ),
            )
            if grant is None or _is_revoked(grant, now):
                raise TaskWorkError("authority_refused")
            if (
                _string(grant, "task_id") != task_id
                or _scope_tenant(grant) != tenant_id
                or _string(grant, "invocation_id") != locator_invocation
                or _number(grant, "generation") != generation
            ):
                raise TaskWorkError("binding_mismatch")

        envelope = None
        if kind == DISPATCH_KIND:
            try:
                envelope = _plain_json(_DESERIALIZER.deserialize(work["envelope"]))
            except (KeyError, TypeError, ValueError):
                raise TaskWorkError("corrupt_binding") from None
            if not isinstance(envelope, dict) or envelope_digest(envelope) != _string(work, "envelope_digest"):
                raise TaskWorkError("digest_mismatch")
            assignment = envelope.get("assignment_ref") or {}
            grant_pk, grant_sk = assignment.get("grant_pk"), assignment.get("grant_sk")
            match = _GRANT_SK.fullmatch(grant_sk or "")
            generation, invocation_id = assignment.get("generation"), envelope.get("invocation_id")
            if (
                envelope.get("dispatch_id") != work_id
                or envelope.get("task_id") != task_id
                or envelope.get("message_id") != invocation_id
                or grant_pk != f"TENANT#{tenant_id}"
                or match is None or match.group(1) != invocation_id
                or int(match.group(2)) != generation
                or any(_string(item, "invocation_id") != invocation_id for item in (locator, work, task))
                or any(_number(item, "generation") != generation for item in (locator, work, task))
            ):
                raise TaskWorkError("binding_mismatch")
            canonical_principal_id = _string(grant, "canonical_principal_id")
            policy_sk = _string(grant, "task_policy_sk")
            policy_version = _number(grant, "task_policy_version")
            if policy_sk != f"TASK_POLICY#{canonical_principal_id}":
                raise TaskWorkError("binding_mismatch")
            policy = self._get(
                self.authority_client, self.authority_table, self._authority_key(grant_pk, policy_sk)
            )
            if (
                policy is None
                or _scope_tenant(policy) != tenant_id
                or _string(policy, "canonical_principal_id") != canonical_principal_id
                or _number(policy, "version") != policy_version
                or policy.get("record_type") != {"S": "TASK_SERVICE_POLICY"}
                or policy.get("status") != {"S": "active"}
            ):
                raise TaskWorkError("authority_refused")
            try:
                allowed_personas = _plain_json(_DESERIALIZER.deserialize(policy["allowed_personas"]))
                task_scopes = _plain_json(_DESERIALIZER.deserialize(policy["task_scopes"]))
            except (KeyError, TypeError, ValueError):
                raise TaskWorkError("corrupt_binding") from None
            if envelope.get("persona") not in allowed_personas or "submit" not in task_scopes:
                raise TaskWorkError("authority_refused")
        return BoundWork(work_id, task_id, tenant_id, kind, locator, work, task, envelope)

    @staticmethod
    def _live_lease(work: dict, prefix: str, now: datetime) -> bool:
        expires = _parse_iso(work.get(f"{prefix}_lease_expires_at", {}).get("S"))
        return expires is not None and expires > now

    def _deadline_check(self, bound: BoundWork, now: datetime) -> None:
        deadline = _parse_iso(bound.work.get("deadline_at", {}).get("S"))
        if deadline is None:
            raise TaskWorkError("corrupt_binding")
        if now >= deadline:
            self._mark_exhausted(bound, now)
            raise TaskWorkError("exhausted")

    def _mark_exhausted(self, bound: BoundWork, now: datetime) -> None:
        work_update = {
            "TableName": self.table,
            "Key": self._work_key(bound.task_id, _string(bound.work, "arrived_at")),
            "UpdateExpression": (
                "SET recovery_status = :exhausted, exhausted_at = :now, #version = :new_version "
                f"REMOVE {SHARD_ATTRIBUTE}, {DUE_ATTRIBUTE}, publication_lease_token, "
                "publication_lease_expires_at, recovery_lease_token, recovery_lease_expires_at"
            ),
            "ConditionExpression": "#version = :version AND attribute_not_exists(recovery_settled_at)",
            "ExpressionAttributeNames": {"#version": "version"},
            "ExpressionAttributeValues": {
                ":version": bound.work["version"], ":new_version": {"S": secrets.token_hex(16)},
                ":now": {"S": _iso(now)}, ":exhausted": {"S": "exhausted"},
            },
        }
        transaction = [{"Update": work_update}]
        task_status = _string(bound.task, "status")
        if task_status in {"accepted", "queued"}:
            transaction.append({"Update": {
                "TableName": self.table, "Key": self._task_key(bound.task_id),
                "UpdateExpression": "SET #status = :failed, updated_at = :now, #version = :new_version",
                "ConditionExpression": "#status = :current AND #version = :version",
                "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                "ExpressionAttributeValues": {
                    ":failed": {"S": "failed"}, ":current": {"S": task_status},
                    ":now": {"S": _iso(now)}, ":version": bound.task["version"],
                    ":new_version": {"S": secrets.token_hex(16)},
                },
            }})
        try:
            self.client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise TaskWorkError("lost_claim_race") from None
            raise TaskWorkUnavailableError("task work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("task work store unavailable") from None

    def claim_publication(self, dispatch_id: str) -> BoundWork:
        bound, now = self.resolve(dispatch_id, expected_kind=DISPATCH_KIND), self._now()
        self._deadline_check(bound, now)
        if bound.work.get("publication_confirmed_at", {}).get("S"):
            raise TaskWorkError("already_settled")
        if _string(bound.task, "status") != "accepted":
            raise TaskWorkError("task_not_publishable")
        if self._live_lease(bound.work, "publication", now):
            raise TaskWorkError("leased")
        tries = int(bound.work.get("publication_tries", {}).get("N", "0"))
        window = _parse_iso(bound.work.get("publication_try_window_started_at", {}).get("S"))
        if tries >= MAX_PUBLICATION_TRIES and (window is None or now < window + timedelta(minutes=PUBLICATION_TRY_WINDOW_MINUTES)):
            raise TaskWorkError("throttled")
        if window and now >= window + timedelta(minutes=PUBLICATION_TRY_WINDOW_MINUTES):
            tries, window = 0, now
        token, expires = secrets.token_hex(16), now + timedelta(seconds=WORK_LEASE_SECONDS)
        try:
            response = self.client.update_item(
                TableName=self.table,
                Key=self._work_key(bound.task_id, _string(bound.work, "arrived_at")),
                UpdateExpression=(
                    "SET publication_lease_token = :token, publication_lease_expires_at = :expires, "
                    "publication_tries = :tries, publication_try_window_started_at = :window, #version = :new_version"
                ),
                ConditionExpression="#version = :version AND attribute_not_exists(publication_confirmed_at)",
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={
                    ":token": {"S": token}, ":expires": {"S": _iso(expires)},
                    ":tries": {"N": str(tries + 1)}, ":window": {"S": _iso(window or now)},
                    ":version": bound.work["version"], ":new_version": {"S": secrets.token_hex(16)},
                }, ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TaskWorkError("lost_claim_race") from None
            raise TaskWorkUnavailableError("task work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("task work store unavailable") from None
        return BoundWork(**{**bound.__dict__, "work": response.get("Attributes", {})})

    def _query_due(self, *, shard: str, cursor: str | None, limit: int) -> tuple[list[dict], str | None]:
        if not re.fullmatch(r"v1#(0[0-9]|1[0-5])", shard or ""):
            raise TaskWorkError("invalid_shard")
        arguments = {
            "TableName": self.table, "IndexName": TASK_WORK_INDEX,
            "KeyConditionExpression": f"{SHARD_ATTRIBUTE} = :shard AND {DUE_ATTRIBUTE} <= :now",
            "ExpressionAttributeValues": {
                ":shard": {"S": shard}, ":now": {"S": _due_upper_bound(self._now())},
            }, "Limit": max(1, min(int(limit), MAX_WORK_RECORDS_PER_INVOCATION)),
        }
        if cursor:
            arguments["ExclusiveStartKey"] = decode_cursor(cursor)
        try:
            response = self.client.query(**arguments)
        except (ClientError, BotoCoreError):
            raise TaskWorkUnavailableError("task work store unavailable") from None
        last = response.get("LastEvaluatedKey")
        return response.get("Items", []), encode_cursor(last) if last else None

    def claim_recovery(
        self, *, shard: str, cursor: str | None = None,
        limit: int = MAX_WORK_RECORDS_PER_INVOCATION,
    ) -> tuple[list[BoundWork], str | None]:
        candidates, next_cursor = self._query_due(shard=shard, cursor=cursor, limit=limit)
        claimed = []
        for candidate in candidates:
            work_id = candidate.get("work_id", {}).get("S", "")
            try:
                bound, now = self.resolve(work_id), self._now()
                if bound.work.get(SHARD_ATTRIBUTE) != {"S": shard}:
                    raise TaskWorkError("binding_mismatch")
                self._deadline_check(bound, now)
                if bound.work.get("recovery_settled_at", {}).get("S") or self._live_lease(bound.work, "recovery", now):
                    continue
                response = self.client.update_item(
                    TableName=self.table,
                    Key=self._work_key(bound.task_id, _string(bound.work, "arrived_at")),
                    UpdateExpression=(
                        "SET recovery_lease_token = :token, recovery_lease_expires_at = :expires, #version = :new_version"
                    ), ConditionExpression="#version = :version AND attribute_not_exists(recovery_settled_at)",
                    ExpressionAttributeNames={"#version": "version"},
                    ExpressionAttributeValues={
                        ":token": {"S": secrets.token_hex(16)},
                        ":expires": {"S": _iso(now + timedelta(seconds=WORK_LEASE_SECONDS))},
                        ":version": bound.work["version"], ":new_version": {"S": secrets.token_hex(16)},
                    }, ReturnValues="ALL_NEW",
                )
                claimed.append(BoundWork(**{**bound.__dict__, "work": response.get("Attributes", {})}))
            except TaskWorkError as exc:
                logger.info("task recovery candidate refused work_id=%s reason=%s", work_id, exc.code)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise TaskWorkUnavailableError("task work store unavailable") from None
            except BotoCoreError:
                raise TaskWorkUnavailableError("task work store unavailable") from None
        return claimed, next_cursor

    def _update_work(self, bound: BoundWork, expression: str, values: dict, condition: str) -> BoundWork:
        try:
            response = self.client.update_item(
                TableName=self.table,
                Key=self._work_key(bound.task_id, _string(bound.work, "arrived_at")),
                UpdateExpression=expression, ConditionExpression=condition,
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues=values, ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TaskWorkError("stale_lease") from None
            raise TaskWorkUnavailableError("task work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("task work store unavailable") from None
        return BoundWork(**{**bound.__dict__, "work": response.get("Attributes", {})})

    def settle_publication(
        self, *, dispatch_id: str, lease_token: str, publication_outcome: str,
        sqs_message_id: str | None,
    ) -> BoundWork:
        if publication_outcome not in PUBLICATION_OUTCOMES or not lease_token:
            raise TaskWorkError("invalid_settlement")
        if publication_outcome == "confirmed" and not sqs_message_id:
            raise TaskWorkError("confirmed_without_message_id")
        bound, now = self.resolve(dispatch_id, expected_kind=DISPATCH_KIND), self._now()
        expires = _parse_iso(bound.work.get("publication_lease_expires_at", {}).get("S"))
        if bound.work.get("publication_lease_token") != {"S": lease_token} or expires is None or expires <= now:
            raise TaskWorkError("stale_lease")
        values = {
            ":token": {"S": lease_token}, ":now": {"S": _iso(now)},
            ":version": bound.work["version"], ":new_version": {"S": secrets.token_hex(16)},
            ":outcome": {"S": publication_outcome},
        }
        if publication_outcome != "confirmed":
            values[":due"] = {"S": due_key(now, dispatch_id)}
            return self._update_work(
                bound,
                f"SET publication_outcome = :outcome, observed_at = :now, {DUE_ATTRIBUTE} = :due, "
                "due_at = :now, #version = :new_version REMOVE publication_lease_token, publication_lease_expires_at",
                values, "publication_lease_token = :token AND publication_lease_expires_at > :now AND #version = :version",
            )
        values[":message_id"] = {"S": sqs_message_id}
        remove = "publication_lease_token, publication_lease_expires_at"
        if not self._live_lease(bound.work, "recovery", now):
            remove += f", {SHARD_ATTRIBUTE}, {DUE_ATTRIBUTE}"
        work_update = {
            "TableName": self.table,
            "Key": self._work_key(bound.task_id, _string(bound.work, "arrived_at")),
            "UpdateExpression": (
                "SET publication_outcome = :outcome, sqs_message_id = :message_id, publication_confirmed_at = :now, "
                f"observed_at = :now, #version = :new_version REMOVE {remove}"
            ),
            "ConditionExpression": "publication_lease_token = :token AND publication_lease_expires_at > :now AND #version = :version",
            "ExpressionAttributeNames": {"#version": "version"}, "ExpressionAttributeValues": values,
        }
        transaction = [{"Update": work_update}]
        if _string(bound.task, "status") == "accepted":
            transaction.append({"Update": {
                "TableName": self.table, "Key": self._task_key(bound.task_id),
                "UpdateExpression": "SET #status = :queued, updated_at = :now, #version = :new_version",
                "ConditionExpression": "#status = :accepted AND #version = :version",
                "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                "ExpressionAttributeValues": {
                    ":queued": {"S": "queued"}, ":accepted": {"S": "accepted"},
                    ":now": {"S": _iso(now)}, ":version": bound.task["version"],
                    ":new_version": {"S": secrets.token_hex(16)},
                },
            }})
        try:
            self.client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise TaskWorkError("stale_lease") from None
            raise TaskWorkUnavailableError("task work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("task work store unavailable") from None
        return self.resolve(dispatch_id, expected_kind=DISPATCH_KIND)

    def settle_recovery(
        self, *, work_id: str, lease_token: str, evidence_kind: str,
        observed: bool, observed_at: datetime,
    ) -> tuple[str, BoundWork]:
        bound, now = self.resolve(work_id), self._now()
        expires = _parse_iso(bound.work.get("recovery_lease_expires_at", {}).get("S"))
        if bound.work.get("recovery_lease_token") != {"S": lease_token} or expires is None or expires <= now:
            raise TaskWorkError("stale_lease")
        expected = {
            "dispatch": "publication", "execution": "workload_termination",
            "queue_ack": "queue_ack", "cleanup": "retention",
        }[bound.kind]
        if evidence_kind != expected:
            raise TaskWorkError("evidence_mismatch")
        if bound.kind == DISPATCH_KIND:
            committed = (
                bound.work.get("publication_outcome") == {"S": "confirmed"}
                and bool(bound.work.get("sqs_message_id", {}).get("S"))
                and bool(bound.work.get("publication_confirmed_at", {}).get("S"))
            )
        else:
            committed = (
                bound.work.get("committed_evidence_kind") == {"S": evidence_kind}
                and bool(bound.work.get("committed_evidence_at", {}).get("S"))
            )
        values = {
            ":token": {"S": lease_token}, ":now": {"S": _iso(now)},
            ":version": bound.work["version"], ":new_version": {"S": secrets.token_hex(16)},
        }
        condition = "recovery_lease_token = :token AND recovery_lease_expires_at > :now AND #version = :version"
        if observed and committed:
            values.update({":observed_at": {"S": _iso(observed_at)}, ":confirmed": {"S": "confirmed"}})
            updated = self._update_work(
                bound,
                "SET recovery_status = :confirmed, recovery_settled_at = :now, recovery_observed_at = :observed_at, "
                f"#version = :new_version REMOVE recovery_lease_token, recovery_lease_expires_at, {SHARD_ATTRIBUTE}, {DUE_ATTRIBUTE}",
                values, condition,
            )
            return "confirmed", updated
        values.update({":due": {"S": due_key(now, work_id)}, ":rejected": {"S": "rejected"}})
        updated = self._update_work(
            bound,
            f"SET recovery_status = :rejected, recovery_observed_at = :now, due_at = :now, {DUE_ATTRIBUTE} = :due, "
            "#version = :new_version REMOVE recovery_lease_token, recovery_lease_expires_at",
            values, condition,
        )
        return "rejected", updated

    def task_status(self, task_id: str) -> str:
        task = self._get(self.client, self.table, self._task_key(task_id))
        if task is None:
            raise TaskWorkError("not_found")
        status = _string(task, "status")
        if status not in TASK_STATUSES:
            raise TaskWorkError("corrupt_binding")
        return status
