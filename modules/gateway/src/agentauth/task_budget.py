"""Task admission leases and strict per-Task model reservations."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from src.agentauth.bootstrap import BootstrapStore
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationTarget


class TaskBudgetError(Exception):
    pass


def _restore_target(value):
    return ReservationTarget(
        **{
            **value,
            "headroom_usd": Decimal(str(value["headroom_usd"])),
            "ttl_seconds": int(value["ttl_seconds"]) if value.get("ttl_seconds") is not None else None,
        }
    )


class TaskBudget:
    def __init__(self, authority, *, reservations=None, qualification_id=None, clock=None):
        self.authority = authority
        self.reservations = reservations or BudgetEnforcementService()._get_reservations()
        # Retained only for compatibility with stored admission records/callers.
        self.qualification_id = qualification_id or "per-task-v1"
        self.clock = clock or (lambda: datetime.now(UTC))

    def _target(self, *, scope, cap):
        # Retain the existing per-Task ledger namespace so an upgrade cannot
        # reset spending already recorded by running Tasks.
        return ReservationTarget(
            org_id="task-qualification",
            entity_type="task_pilot",
            entity_id=scope,
            period_type="qualification",
            period_start="task-api-v1",
            headroom_usd=Decimal(str(cap)),
            ttl_seconds=90 * 86400,
            require_initialization=True,
        )

    async def _initialize(self, target):
        marker = "task-budget:" + hashlib.sha256(target.key().encode()).hexdigest()
        first = self.authority.claim_policy_budget_initialization(tenant_id="task-qualification", flow_id=marker, allow_create=True)
        if first:
            result = await self.reservations.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])
            if result is None or not result.admitted:
                raise TaskBudgetError("task budget initialization unavailable")
        snapshot = await self.reservations.snapshot(target)
        if snapshot is None:
            raise TaskBudgetError("task budget state unavailable")

    async def reserve_admission(self, *, tenant, principal, idempotency_key, max_usd, request_digest):
        reservation_id = "task-admit:" + hashlib.sha256((tenant + "\0" + principal + "\0" + idempotency_key).encode()).hexdigest()
        # Admission keeps its fenced lease, but does not reserve pilot budgets.
        # Real hierarchy and Task limits are reserved before provider dispatch.
        key = {"pk": {"S": "TASK_CAPACITY#" + hashlib.sha256(reservation_id.encode()).hexdigest()}, "sk": {"S": "RESERVATION"}}
        owner = str(uuid.uuid4())
        now = int(self.clock().timestamp())
        record = {
            "reservation_id": reservation_id,
            "amount_usd": str(max_usd),
            "request_digest": request_digest,
            "qualification_id": self.qualification_id,
            "targets": [],
            "status": "reserved",
            "owner_token": owner,
            "lease_expires_at": now + 120,
            "authority_pk": key["pk"]["S"],
            "authority_sk": "RESERVATION",
        }
        serializer, deserializer = TypeSerializer(), TypeDeserializer()
        previous_raw = self.authority._read(key["pk"]["S"], "RESERVATION")
        previous = {name: deserializer.deserialize(value) for name, value in previous_raw.items()} if previous_raw else None
        condition = "attribute_not_exists(pk)"
        values = None
        if previous:
            if previous["request_digest"] != request_digest:
                raise TaskBudgetError("idempotency conflict")
            if previous.get("state") not in {"released", "preparing"} or (
                previous.get("state") == "preparing" and int(previous["lease_expires_at"]) >= now
            ):
                raise TaskBudgetError("task admission already pending")
            # Release an expired, uncommitted legacy pilot hold before takeover.
            # abort_admission's owner CAS cannot release an accepted Task's hold.
            if previous.get("targets") and previous.get("state") == "preparing":
                await self.abort_admission(previous)
                raw = self.authority._read(key["pk"]["S"], "RESERVATION")
                previous = {name: deserializer.deserialize(value) for name, value in raw.items()}
                if previous.get("state") != "released":
                    raise TaskBudgetError("task admission already pending")
            condition = "owner_token = :old AND #state = :state"
            values = {":old": {"S": previous["owner_token"]}, ":state": {"S": previous["state"]}}
        put = {
            "TableName": self.authority.table,
            "Item": {**key, **{name: serializer.serialize(value) for name, value in record.items()}, "state": {"S": "preparing"}},
            "ConditionExpression": condition,
        }
        if values:
            put.update(ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues=values)
        try:
            shard = int(hashlib.sha256(reservation_id.encode()).hexdigest()[:2], 16) % 16
            cleanup = {
                "pk": {"S": f"TASK_ADMISSION_CLEANUP#v1#{shard:02d}"},
                "sk": {"S": f"{now + 120:012d}#{owner}"},
                "authority_pk": key["pk"],
                "owner_token": {"S": owner},
            }
            self.authority.client.transact_write_items(
                TransactItems=[
                    {"Put": put},
                    {"Put": {"TableName": self.authority.table, "Item": cleanup, "ConditionExpression": "attribute_not_exists(pk)"}},
                ]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
                raise TaskBudgetError("task admission already pending") from None
            raise
        return record

    async def abort_admission(self, reservation):
        key = {"pk": {"S": reservation["authority_pk"]}, "sk": {"S": reservation["authority_sk"]}}
        try:
            self.authority.client.update_item(
                TableName=self.authority.table,
                Key=key,
                UpdateExpression="SET #state = :failed",
                ConditionExpression="(#state = :preparing OR #state = :failed) AND owner_token = :owner",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":failed": {"S": "failed"}, ":preparing": {"S": "preparing"}, ":owner": {"S": reservation["owner_token"]}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return  # Accepted or taken over: this loser cannot release its hold.
            raise
        await self.settle_admission(reservation, actual_usd=0, uncommitted=True)
        self.authority.client.update_item(
            TableName=self.authority.table,
            Key=key,
            UpdateExpression="SET #state = :released",
            ConditionExpression="#state = :failed AND owner_token = :owner",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":released": {"S": "released"}, ":failed": {"S": "failed"}, ":owner": {"S": reservation["owner_token"]}},
        )

    async def reap_abandoned(self, *, shard, limit=16):
        if shard not in {f"v1#{value:02d}" for value in range(16)}:
            raise TaskBudgetError("invalid cleanup shard")
        now = int(self.clock().timestamp())
        page = self.authority.client.query(
            TableName=self.authority.table,
            ConsistentRead=True,
            KeyConditionExpression="pk = :pk AND sk < :due",
            Limit=min(limit, 16),
            ExpressionAttributeValues={":pk": {"S": "TASK_ADMISSION_CLEANUP#" + shard}, ":due": {"S": f"{now:012d}#"}},
        )
        decoder = TypeDeserializer()
        for work in page.get("Items", []):
            raw = self.authority._read(work["authority_pk"]["S"], "RESERVATION")
            record = {key: decoder.deserialize(value) for key, value in raw.items()} if raw else None
            if record and record.get("owner_token") == work["owner_token"]["S"] and record.get("state") in {"preparing", "failed"}:
                if int(record["lease_expires_at"]) >= now:
                    continue
                # Expired lease and owner CAS fence acceptance and takeover.
                # A failed Redis verification leaves this work indexed to retry.
                await self.abort_admission(record)
            self.authority.client.delete_item(TableName=self.authority.table, Key={"pk": work["pk"], "sk": work["sk"]})

    async def settle_admission(self, reservation, *, actual_usd, uncommitted=False):
        targets = [_restore_target(value) for value in reservation["targets"]]
        amount = Decimal(str(actual_usd))
        if not amount.is_finite() or amount < 0 or amount > Decimal(reservation["amount_usd"]):
            raise TaskBudgetError("task settlement exceeds reservation")
        if not targets:
            return
        # Legacy accepted Tasks settle their original recorded pilot targets.
        # Existing ledger reconciliation retains actual usage, releasing only
        # unspent headroom. A backend error preserves the original upper bound.
        await self.reservations.reconcile(reservation["reservation_id"], amount, targets)
        await self.verify_settlement(reservation["reservation_id"], amount, targets, allow_missing_zero=uncommitted)

    async def verify_settlement(self, operation_id, amount, targets, allow_missing_zero=False):
        # The shared best-effort reconcile swallows backend failures. Task
        # terminalization must inspect the real strict ledger before releasing.
        client = await self.reservations._get_client()
        for target in targets:
            values = await client.hmget(target.key(), [operation_id, "pending:" + operation_id, "unbounded:" + operation_id])
            if allow_missing_zero and Decimal(str(amount)) == 0 and not any(values):
                continue  # A preparing admission never authorized any spend.
            if not values[0] or values[1] or values[2]:
                raise TaskBudgetError("task budget settlement unconfirmed")
            value = values[0].decode() if isinstance(values[0], bytes) else values[0]
            settled, expires = value.split(":", 1)
            if Decimal(settled) != Decimal(str(amount)) or float(expires) <= self.clock().timestamp():
                raise TaskBudgetError("task budget settlement unconfirmed")

    async def reserve_model(self, *, task_id, operation_id, cap, amount):
        target = self._target(scope="task:" + task_id, cap=cap)
        await self._initialize(target)
        result = await self.reservations.reserve(operation_id, Decimal(str(amount)), [target])
        if result is None or not result.admitted:
            raise TaskBudgetError("task model budget reservation refused")
        return target

    async def settle_model(self, *, operation_id, target, actual_usd):
        await self.reservations.reconcile(operation_id, Decimal(str(actual_usd)), [target])
        await self.verify_settlement(operation_id, actual_usd, [target])


def task_budget(repository):
    return TaskBudget(BootstrapStore(table_name=repository.authority_table_name, dynamodb_client=repository._client))
