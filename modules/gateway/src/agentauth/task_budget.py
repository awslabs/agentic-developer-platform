"""Strict Task pilot reservations on the existing platform reservation ledger."""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from src.agentauth.bootstrap import BootstrapStore
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationTarget


class TaskBudgetError(Exception):
    pass


class TaskBudget:
    def __init__(self, authority, *, reservations=None, qualification_id=None, clock=None):
        self.authority = authority
        self.reservations = reservations or BudgetEnforcementService()._get_reservations()
        self.qualification_id = qualification_id or os.environ.get("ADP_TASK_QUALIFICATION_ID", "")
        self.clock = clock or (lambda: datetime.now(UTC))
        if not self.qualification_id:
            raise TaskBudgetError("task qualification budget is not configured")

    def _target(self, *, scope, cap):
        # All pilot counters share a Redis cluster hash tag. Tenant identity is
        # still explicitly part of each tenant counter's server-derived scope.
        return ReservationTarget(org_id="task-qualification", entity_type="task_pilot", entity_id=scope,
            period_type="qualification", period_start="task-api-v1", headroom_usd=Decimal(str(cap)),
            ttl_seconds=90 * 86400, require_initialization=True)

    async def _initialize(self, target):
        marker = "task-budget:" + hashlib.sha256(target.key().encode()).hexdigest()
        first = self.authority.claim_policy_budget_initialization(
            tenant_id="task-qualification", flow_id=marker, allow_create=True)
        if first:
            result = await self.reservations.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])
            if result is None or not result.admitted:
                raise TaskBudgetError("task budget initialization unavailable")
        snapshot = await self.reservations.snapshot(target)
        if snapshot is None:
            raise TaskBudgetError("task budget state unavailable")

    async def reserve_admission(self, *, tenant, principal, idempotency_key, max_usd, request_digest):
        reservation_id = "task-admit:" + hashlib.sha256(
            (tenant + "\0" + principal + "\0" + idempotency_key).encode()).hexdigest()
        targets = [self._target(scope="qualification:" + self.qualification_id, cap=25),
                   self._target(scope="tenant-day:" + tenant + ":" + self.clock().strftime("%Y-%m-%d"), cap=10)]
        key = {"pk": {"S": "TASK_CAPACITY#" + hashlib.sha256(reservation_id.encode()).hexdigest()}, "sk": {"S": "RESERVATION"}}
        owner = str(uuid.uuid4())
        now = int(self.clock().timestamp())
        record = {"reservation_id": reservation_id, "amount_usd": str(max_usd), "request_digest": request_digest,
                  "qualification_id": self.qualification_id,
                  "targets": [{**asdict(target), "headroom_usd": str(target.headroom_usd)} for target in targets],
                  "status": "reserved", "owner_token": owner, "lease_expires_at": now + 120,
                  "authority_pk": key["pk"]["S"], "authority_sk": "RESERVATION"}
        serializer, deserializer = TypeSerializer(), TypeDeserializer()
        previous_raw = self.authority._read(key["pk"]["S"], "RESERVATION")
        previous = {name: deserializer.deserialize(value) for name, value in previous_raw.items()} if previous_raw else None
        condition = "attribute_not_exists(pk)"
        values = None
        if previous:
            if previous["request_digest"] != request_digest:
                raise TaskBudgetError("idempotency conflict")
            if previous.get("state") not in {"released", "preparing"} or (
                    previous.get("state") == "preparing" and int(previous["lease_expires_at"]) >= now):
                raise TaskBudgetError("task admission already pending")
            # A takeover reuses the exact hold and its original budget periods.
            # The original owner's acceptance is fenced by owner_token+lease.
            record.update({name: previous[name] for name in ("amount_usd", "qualification_id", "targets")})
            targets = [ReservationTarget(**{**value, "headroom_usd": Decimal(value["headroom_usd"])}) for value in record["targets"]]
            condition = "owner_token = :old AND #state = :state"
            values = {":old": {"S": previous["owner_token"]}, ":state": {"S": previous["state"]}}
        put = {"TableName": self.authority.table, "Item": {**key, **{name: serializer.serialize(value) for name, value in record.items()},
                "state": {"S": "preparing"}}, "ConditionExpression": condition}
        if values:
            put.update(ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues=values)
        try:
            self.authority.client.put_item(**put)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise TaskBudgetError("task admission already pending") from None
            raise
        for target in targets:
            await self._initialize(target)
        result = await self.reservations.reserve(reservation_id, Decimal(record["amount_usd"]), targets)
        if result is None or not result.admitted:
            raise TaskBudgetError("task budget reservation refused")
        return record

    async def abort_admission(self, reservation):
        key = {"pk": {"S": reservation["authority_pk"]}, "sk": {"S": reservation["authority_sk"]}}
        try:
            self.authority.client.update_item(TableName=self.authority.table, Key=key,
                UpdateExpression="SET #state = :failed", ConditionExpression="#state = :preparing AND owner_token = :owner",
                ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues={":failed": {"S": "failed"},
                    ":preparing": {"S": "preparing"}, ":owner": {"S": reservation["owner_token"]}})
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return  # Accepted or taken over: this loser cannot release its hold.
            raise
        await self.settle_admission(reservation, actual_usd=0)
        self.authority.client.update_item(TableName=self.authority.table, Key=key,
            UpdateExpression="SET #state = :released", ConditionExpression="#state = :failed AND owner_token = :owner",
            ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues={":released": {"S": "released"},
                ":failed": {"S": "failed"}, ":owner": {"S": reservation["owner_token"]}})

    async def settle_admission(self, reservation, *, actual_usd):
        if reservation["qualification_id"] != self.qualification_id:
            raise TaskBudgetError("task qualification budget changed")
        targets = [ReservationTarget(**{**value, "headroom_usd": Decimal(str(value["headroom_usd"]))}) for value in reservation["targets"]]
        amount = Decimal(str(actual_usd))
        if amount < 0 or amount > Decimal(reservation["amount_usd"]):
            raise TaskBudgetError("task settlement exceeds reservation")
        # Existing ledger reconciliation retains actual usage, releasing only
        # unspent headroom. A backend error preserves the original upper bound.
        await self.reservations.reconcile(reservation["reservation_id"], amount, targets)

    async def reserve_model(self, *, task_id, operation_id, cap, amount):
        target = self._target(scope="task:" + task_id, cap=cap)
        await self._initialize(target)
        result = await self.reservations.reserve(operation_id, Decimal(str(amount)), [target])
        if result is None or not result.admitted:
            raise TaskBudgetError("task model budget reservation refused")
        return target

    async def settle_model(self, *, operation_id, target, actual_usd):
        await self.reservations.reconcile(operation_id, Decimal(str(actual_usd)), [target])


def task_budget(repository):
    return TaskBudget(BootstrapStore(table_name=repository.authority_table_name, dynamodb_client=repository._client))
