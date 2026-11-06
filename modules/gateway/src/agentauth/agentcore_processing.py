"""Restartable gateway-owned processing for accepted user-memory captures."""

import asyncio
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from types import SimpleNamespace

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError
from starlette.concurrency import run_in_threadpool

from src.agentauth.agentcore_authority import shared_owner_header
from src.agentauth.agentcore_governance import owner_state
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError

RECEIPT = re.compile(r"[a-f0-9]{64}\Z")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 30


class AgentCoreCaptureProcessor:
    def __init__(self, authority, capabilities, provider):
        self.authority = authority
        self.capabilities = capabilities
        self.provider = provider
        self.table = authority.context_table

    def _authorized(self, row, now):
        identity = (row.get("tenantId"), row.get("teamId"), row.get("ownerUserId"))
        if (
            not all(
                isinstance(value, str) and value
                for value in (identity[0], identity[2], row.get("sessionId"), row.get("runId"), row.get("messageId"), row.get("requestDigest"))
            )
            or not isinstance(identity[1], str)
            or not isinstance(row.get("userText"), str)
            or not row["userText"].strip()
            or len(row["userText"].encode()) > 8192
            or isinstance(row.get("capturedAt"), bool)
            or not isinstance(row.get("capturedAt"), int | Decimal)
            or int(row["capturedAt"]) != row["capturedAt"]
            or not isinstance(row.get("attempt"), int | Decimal)
            or int(row["attempt"]) != row["attempt"]
        ):
            raise ChatAuthorizationRefusedError("memory capture invalid")
        digest = hashlib.sha256(
            json.dumps(
                [identity[0], identity[2], row["sessionId"], row["runId"], row["requestDigest"], row["messageId"]],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if digest != row.get("receiptId"):
            raise ChatAuthorizationRefusedError("memory capture invalid")
        owner = SimpleNamespace(tenant_id=identity[0], team_id=identity[1], user_id=identity[2], session_id=row["sessionId"])
        shared_owner_header(self.table, owner)
        epoch = row.get("ownerEpoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int | Decimal) or int(epoch) != epoch or epoch < 0:
            raise ChatAuthorizationRefusedError("memory epoch invalid")
        if owner_state(self.table, owner)["epoch"] != epoch:
            raise ChatAuthorizationRefusedError("memory capture superseded")
        self.capabilities._member(identity[0], identity[2], identity[1])
        grant = self.authority.store.live_grant(
            invocation_id=row["runId"],
            tenant_id=identity[0],
            attempt=row["attempt"],
            now=datetime.fromtimestamp(now, UTC),
        )
        if (
            grant.grant_id != row.get("grantId")
            or grant.revocation_epoch != row.get("grantEpoch")
            or grant.authority.kind != "chat_event"
            or grant.authority.human_id != identity[2]
            or f"chat/{row['sessionId']}" not in grant.repo_scope
        ):
            raise ChatAuthorizationRefusedError("memory grant changed")
        return self.provider.owner_namespace(owner)

    def process(self, receipt_id, *, now):
        if not isinstance(receipt_id, str) or not RECEIPT.fullmatch(receipt_id):
            raise ChatAuthorizationRefusedError("memory receipt invalid")
        key = {"PK": f"memory-capture#{receipt_id}", "SK": "capture"}
        try:
            claimed = self.table.update_item(
                Key=key,
                UpdateExpression="SET #status = :processing, leaseUntil = :lease, attempts = if_not_exists(attempts, :zero) + :one",
                ConditionExpression=(
                    "(#status = :pending AND (attribute_not_exists(nextAttemptAt) OR nextAttemptAt <= :now)) "
                    "OR (#status = :processing AND leaseUntil <= :now)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":pending": "pending",
                    ":lease": now + LEASE_SECONDS,
                    ":now": now,
                    ":zero": 0,
                    ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        if claimed.get("receiptId") != receipt_id or claimed.get("SK") != "capture" or claimed.get("PK") != key["PK"]:
            self._finish(key, claimed, now, status="failed", reason="invalid_capture")
            return {"status": "failed", "receipt_id": receipt_id}
        attempts = int(claimed["attempts"])
        if attempts > MAX_ATTEMPTS:
            self._finish(key, claimed, now, status="failed", reason="processing_exhausted")
            return {"status": "failed", "receipt_id": receipt_id}
        try:
            namespace = self._authorized(claimed, now)
            response = self.provider.client.create_event(
                memoryId=self.provider.memory_id,
                actorId=namespace.rsplit("/", 1)[-1],
                sessionId=claimed["sessionId"],
                eventTimestamp=datetime.fromtimestamp(int(claimed["capturedAt"]), UTC),
                payload=[{"conversational": {"content": {"text": claimed["userText"]}, "role": "USER"}}],
                metadata={"adp_receipt": {"stringValue": receipt_id}, "adp_epoch": {"stringValue": str(int(claimed["ownerEpoch"]))}},
                clientToken=receipt_id,
            )
            event = response.get("event") if isinstance(response, dict) else None
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("eventId"), str)
                or not event["eventId"]
                or event.get("memoryId") != self.provider.memory_id
                or event.get("sessionId") != claimed["sessionId"]
                or event.get("actorId") != namespace.rsplit("/", 1)[-1]
            ):
                raise ChatAuthorizationUnavailableError("memory service receipt unavailable")
            self._authorized(claimed, now)
        except (BootstrapRefusedError, ChatAuthorizationRefusedError):
            self._finish(key, claimed, now, status="failed", reason="authority_refused")
            return {"status": "failed", "receipt_id": receipt_id}
        except (ChatAuthorizationUnavailableError, BotoCoreError, ClientError):
            if attempts >= MAX_ATTEMPTS:
                self._finish(key, claimed, now, status="failed", reason="service_unavailable")
                return {"status": "failed", "receipt_id": receipt_id}
            self._finish(key, claimed, now, status="pending", next_attempt=now + min(2**attempts, 300))
            return {"status": "pending", "receipt_id": receipt_id}
        self._finish(key, claimed, now, status="submitted", service_event_id=event["eventId"])
        return {"status": "pending", "receipt_id": receipt_id}

    def _finish(self, key, claimed, now, *, status, reason=None, next_attempt=None, service_event_id=None):
        updates = {"#status": "status"}
        values = {":processing": "processing", ":status": status, ":lease": claimed["leaseUntil"], ":attempts": claimed["attempts"]}
        expression = "SET #status = :status"
        if reason:
            expression += ", failureCode = :reason"
            values[":reason"] = reason
        if next_attempt:
            expression += ", nextAttemptAt = :retry"
            values[":retry"] = next_attempt
        if status == "submitted":
            expression += ", submittedAt = :now, serviceEventId = :event"
            values[":now"] = now
            values[":event"] = service_event_id
        expression += " REMOVE leaseUntil"
        if next_attempt is None:
            expression += ", nextAttemptAt"
        self.table.update_item(
            Key=key,
            UpdateExpression=expression,
            ConditionExpression="#status = :processing AND leaseUntil = :lease AND attempts = :attempts",
            ExpressionAttributeNames=updates,
            ExpressionAttributeValues=values,
        )

    def poll(self, *, now, cursor=None):
        parameters = {
            "FilterExpression": Attr("PK").begins_with("memory-capture#")
            & Attr("SK").eq("capture")
            & (Attr("status").eq("pending") | Attr("status").eq("processing")),
            "Limit": 25,
        }
        if cursor:
            parameters["ExclusiveStartKey"] = cursor
        page = self.table.scan(**parameters)
        for row in page.get("Items", []):
            receipt_id = row["PK"].removeprefix("memory-capture#")
            if RECEIPT.fullmatch(receipt_id):
                self.process(receipt_id, now=now)
        return page.get("LastEvaluatedKey")


async def run():
    from src.agentauth.chat_data_routes import runtime
    from src.agentauth.chat_model import agentcore_recall

    authority, capabilities = runtime()
    cursor = None
    while True:
        provider = agentcore_recall()
        if provider is None:
            raise ChatAuthorizationUnavailableError("memory target unconfigured")
        cursor = await run_in_threadpool(
            partial(AgentCoreCaptureProcessor(authority, capabilities, provider).poll, now=int(time.time()), cursor=cursor)
        )
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run())
