"""Durable admission identity for ordinary façade tool calls."""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from src.agentauth.test_run_admission_store import canonical


class ToolCallAdmission:
    def __init__(self, *, table, client, tenant_id, user_id, team_id, run_id, workload_id, body):
        self.table = table
        self.client = client
        self.key = {
            "pk": {"S": f"TENANT#{tenant_id}"},
            "sk": {"S": f"TOOLS_CALL#{user_id}#{body.request_id}"},
        }
        self.binding = hashlib.sha256(
            canonical(
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "team_id": team_id,
                    "run_id": run_id,
                    "workload_id": workload_id,
                    "call": body.model_dump(mode="json"),
                }
            ).encode()
        ).hexdigest()
        self.receipt_id = str(uuid4())
        self.status = "claimed"

    def claim(self):
        try:
            self.client.put_item(
                TableName=self.table,
                Item={
                    **self.key,
                    "record_type": {"S": "TOOLS_CALL_ADMISSION"},
                    "binding": {"S": self.binding},
                    "receipt_id": {"S": self.receipt_id},
                    "status": {"S": "claimed"},
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise HTTPException(503, "tool admission unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool admission unavailable") from None
        try:
            existing = self.client.get_item(TableName=self.table, Key=self.key, ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool admission unavailable") from None
        if not existing or existing.get("record_type") != {"S": "TOOLS_CALL_ADMISSION"}:
            raise HTTPException(503, "tool admission unavailable")
        if existing.get("binding") != {"S": self.binding}:
            raise HTTPException(409, "request id payload conflict")
        if existing.get("status") not in ({"S": "claimed"}, {"S": "admitted"}):
            raise HTTPException(503, "tool admission unavailable")
        try:
            original_receipt = existing["receipt_id"]["S"]
            if str(UUID(original_receipt)) != original_receipt:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise HTTPException(503, "tool admission unavailable") from None
        self.receipt_id = original_receipt
        self.status = existing["status"]["S"]
        return False

    def finish(self):
        try:
            self.client.update_item(
                TableName=self.table,
                Key=self.key,
                UpdateExpression="SET #status = :admitted",
                ConditionExpression="#status = :claimed AND binding = :binding AND receipt_id = :receipt",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":admitted": {"S": "admitted"},
                    ":claimed": {"S": "claimed"},
                    ":binding": {"S": self.binding},
                    ":receipt": {"S": self.receipt_id},
                },
            )
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool admission unavailable") from None

    def cancel_before_reservation(self):
        try:
            self.client.delete_item(
                TableName=self.table,
                Key=self.key,
                ConditionExpression="#status = :claimed AND binding = :binding AND receipt_id = :receipt",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":claimed": {"S": "claimed"},
                    ":binding": {"S": self.binding},
                    ":receipt": {"S": self.receipt_id},
                },
            )
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool admission unavailable") from None
