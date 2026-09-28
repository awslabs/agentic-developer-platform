"""IAM gateway and single-owner browser consumer; never replay a claimed action."""

import base64
import hashlib
import json
import os
import time
from decimal import Decimal

import boto3
from adp_tools.authority import TaskAuthorityClient, require_worker
from adp_tools.storage import serialize
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import HTTPException
from pydantic import ValidationError

from cyber_tools.handler import response
from cyber_tools.operations import CyberBody
from cyber_tools.task_browser import TaskBrowser
from cyber_tools.url_contract import validate_url_payload

OPERATIONS = {
    "browser_start",
    "browser_step",
    "browser_inspect",
    "browser_close",
    "cancel_jobs",
}
PENDING = {"operation_status": "pending", "result": {"status": "pending"}}


def key(operation_id):
    return {"event_id": "BROWSER#" + operation_id, "arrived_at": "OP"}


def read(table, operation_id):
    item = table.get_item(Key=key(operation_id), ConsistentRead=True).get("Item")
    return item


def session_partition(owner):
    return "BROWSER_SESSIONS#" + "#".join(owner)


def owned_sessions(table, owner):
    response = table.query(
        KeyConditionExpression=Key("event_id").eq(session_partition(owner))
        & Key("arrived_at").begins_with("START#"),
        ConsistentRead=True,
        Limit=5,
    )
    if "LastEvaluatedKey" in response:
        raise HTTPException(503, "Browser session bound exceeded")
    return response["Items"]


def session_reference(session):
    return session.get("session_id", session["arrived_at"])


def lambda_handler(event, context):
    authority = None
    try:
        if (
            event.get("httpMethod") != "POST"
            or event.get("resource") != "/tools/browser"
        ):
            raise HTTPException(404, "Not found")
        require_worker(
            event,
            set(
                filter(None, os.environ.get("CYBER_TOOLS_WORKER_ROLES", "").split(","))
            ),
        )
        raw = event.get("body", "")
        if not isinstance(raw, str) or len(raw) > 90000:
            raise HTTPException(413, "Tool request exceeds bound")
        raw = (
            base64.b64decode(raw, validate=True)
            if event.get("isBase64Encoded")
            else raw.encode()
        )
        if len(raw) > 65536:
            raise HTTPException(413, "Tool request exceeds bound")
        body = CyberBody.model_validate_json(raw)
        if body.operation not in OPERATIONS or (
            body.operation == "cancel_jobs" and body.payload
        ):
            raise HTTPException(422, "Unsupported browser operation")
        if body.operation != "cancel_jobs":
            validate_url_payload(body.operation, body.payload)
        headers = event.get("headers") or {}
        authority = TaskAuthorityClient(
            os.environ["ADP_TASK_AUTHORITY_ENDPOINT"],
            headers,
            region=os.environ["AWS_REGION"],
        )
        attempt = body.attempt.model_dump()
        verified = authority.authorize(
            attempt=attempt,
            tool="cyber." + body.operation,
            cleanup=body.operation == "cancel_jobs",
        )
        table = boto3.resource("dynamodb").Table(os.environ["BROWSER_OPERATIONS_TABLE"])
        owner = verified.identity.model_dump()
        digest = hashlib.sha256(
            json.dumps([owner, body.operation, body.payload], sort_keys=True).encode()
        ).hexdigest()
        existing = read(table, body.operation_id)
        if existing:
            if existing["digest"] != digest:
                raise HTTPException(409, "Tool operation ID reused")
        else:
            if (
                body.operation != "cancel_jobs"
                and os.environ.get("ADP_TASK_BROWSER_HTTP_ENABLED") != "true"
            ):
                raise HTTPException(503, "Browser HTTP tools unavailable")
            claim = {
                "event_id": "BROWSER#" + body.operation_id,
                "arrived_at": "OP",
                "digest": digest,
                "state": "queued",
                "created": int(time.time()),
            }
            try:
                if body.operation in {"browser_start", "browser_step", "browser_close"}:
                    boto3.client("dynamodb").transact_write_items(
                        TransactItems=[
                            {
                                "Put": {
                                    "TableName": table.name,
                                    "Item": serialize(item),
                                    "ConditionExpression": "attribute_not_exists(event_id)",
                                }
                            }
                            for item in (
                                claim,
                                {
                                    "event_id": "BROWSER_DIGEST#" + digest,
                                    "arrived_at": "OP",
                                    "operation_id": body.operation_id,
                                },
                            )
                        ]
                    )
                else:
                    table.put_item(
                        Item=claim, ConditionExpression="attribute_not_exists(event_id)"
                    )
            except ClientError as error:
                if error.response["Error"]["Code"] not in {
                    "ConditionalCheckFailedException",
                    "TransactionCanceledException",
                }:
                    raise
                existing = read(table, body.operation_id)
                if not existing and body.operation in {
                    "browser_start",
                    "browser_step",
                    "browser_close",
                }:
                    alias = table.get_item(
                        Key={
                            "event_id": "BROWSER_DIGEST#" + digest,
                            "arrived_at": "OP",
                        },
                        ConsistentRead=True,
                    ).get("Item")
                    if alias:
                        raise HTTPException(
                            409,
                            "Browser action already claimed; retain original operation ID",
                        ) from None
                if not existing:
                    raise HTTPException(503, "Browser claim unavailable") from None
                if existing["digest"] != digest:
                    raise HTTPException(409, "Tool operation ID reused") from None
        existing = existing or read(table, body.operation_id)
        retry_cleanup = (
            body.operation == "cancel_jobs"
            and existing
            and existing.get("receipt", {}).get("operation_status") != "confirmed"
        )
        if existing and (existing["state"] == "queued" or retry_cleanup):
            proofs = {
                name: value
                for name in ("x-adp-run-credential", "x-adp-workload-token")
                for header, value in headers.items()
                if header.lower() == name
            }
            # Cleanup is idempotent and may need another pass after an uncertain
            # stop or provider expiry. Mutations always keep their original ID.
            dedup = body.operation_id + (
                "-" + str(int(time.time()) // 2) if retry_cleanup else ""
            )
            boto3.client("sqs").send_message(
                QueueUrl=os.environ["BROWSER_QUEUE_URL"],
                MessageBody=json.dumps({"body": body.model_dump(), "proofs": proofs}),
                MessageGroupId="browser",
                MessageDeduplicationId=dedup,
            )
        result = existing.get("receipt") if existing else None
        if not result:
            result = {
                "schema_version": "1.0",
                "task_id": owner["task_id"],
                "operation_id": body.operation_id,
                **PENDING,
            }
            if existing and time.time() - int(existing["created"]) > 240:
                result["operation_status"] = "unknown"
                result["result"] = {
                    "status": "unknown",
                    "reason": "browser_outcome_unavailable",
                }
        return response(200, result)
    except HTTPException as error:
        return response(
            error.status_code,
            {
                "code": "invalid_request"
                if error.status_code == 422
                else "tool_refused",
                "message": error.detail,
            },
        )
    except (ValidationError, ValueError, TypeError):
        return response(
            422, {"code": "invalid_request", "message": "Invalid browser tool request"}
        )
    except Exception:
        return response(
            503,
            {
                "code": "outcome_unavailable",
                "message": "Tool outcome unavailable; retain operation identity",
            },
        )
    finally:
        if authority is not None:
            authority.close()


class BrowserConsumer:
    def __init__(self, table, request=None):
        self.table = table
        self.request = request
        self.tools = {}

    def consume(self, message):
        envelope = json.loads(message)
        body = CyberBody.model_validate(envelope["body"])
        row = read(self.table, body.operation_id)
        if not row:
            return
        cleanup_retry = (
            body.operation == "cancel_jobs"
            and row.get("receipt", {}).get("operation_status") != "confirmed"
        )
        eligible = row["state"] == "queued" or (
            cleanup_retry
            and (
                row["state"] == "done"
                or time.time() - int(row.get("updated", row["created"])) > 240
            )
        )
        if not eligible:
            return
        if body.operation not in OPERATIONS or (
            body.operation == "cancel_jobs" and body.payload
        ):
            return
        if body.operation != "cancel_jobs":
            validate_url_payload(body.operation, body.payload)
        if body.operation != "cancel_jobs" and time.time() - int(row["created"]) > 225:
            self.table.update_item(
                Key=key(body.operation_id),
                UpdateExpression="SET #state = :done",
                ConditionExpression="#state = :queued",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":done": "done", ":queued": "queued"},
            )
            return
        try:
            self.table.update_item(
                Key=key(body.operation_id),
                UpdateExpression="SET #state = :running, updated = :updated",
                ConditionExpression="#state = :prior",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":running": "running",
                    ":prior": row["state"],
                    ":updated": int(time.time()),
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return
            raise
        attempt = body.attempt.model_dump()
        owner = (attempt["run"]["task_id"], attempt["runtime_attempt_id"])
        authority = None
        try:
            authority = TaskAuthorityClient(
                os.environ["ADP_TASK_AUTHORITY_ENDPOINT"],
                envelope["proofs"],
                region=os.environ["AWS_REGION"],
            )
            verified = authority.authorize(
                attempt=attempt,
                tool="cyber." + body.operation,
                cleanup=body.operation == "cancel_jobs",
            )
            digest = hashlib.sha256(
                json.dumps(
                    [
                        verified.identity.model_dump(),
                        body.operation,
                        body.payload,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if row["digest"] != digest:
                raise HTTPException(403, "Task authority changed")
            fence = {"event_id": session_partition(owner), "arrived_at": "CLOSED"}
            if body.operation == "cancel_jobs":
                self.table.put_item(Item={**fence, "closed": True})
            elif self.table.get_item(Key=fence, ConsistentRead=True).get("Item"):
                raise HTTPException(409, "Browser cleanup has started")
            if owner not in self.tools:
                tool = TaskBrowser(None, request=self.request)
                self.tools[owner] = tool
            else:
                tool = self.tools[owner]
            tool.authority = authority
            sessions = owned_sessions(self.table, owner)
            provisional = {
                "event_id": session_partition(owner),
                "arrived_at": "START#" + body.operation_id,
            }
            if body.operation == "browser_start" and len(sessions) >= 4:
                receipt = {
                    "schema_version": "1.0",
                    "task_id": owner[0],
                    "operation_id": body.operation_id,
                    "operation_status": "rejected",
                    "result": {
                        "status": "refused",
                        "reason": "Browser session bound exceeded",
                    },
                }
            else:
                original_request = tool.request
                if body.operation == "browser_start":
                    lease_seconds = int(
                        os.environ.get("CYBER_BROWSER_SESSION_SECONDS", "600")
                    )
                    if not 1 <= lease_seconds <= 1800:
                        raise ValueError("Invalid browser lease")
                    created = int(time.time())
                    self.table.put_item(
                        Item={
                            **provisional,
                            "provider_id": "",
                            "created": created,
                            "expires_at": created + lease_seconds + 120,
                        },
                        ConditionExpression="attribute_not_exists(event_id)",
                    )

                def guarded_request(operation, payload):
                    packet = original_request(operation, payload)
                    if operation == "start":
                        self.table.update_item(
                            Key=provisional,
                            UpdateExpression="SET provider_id = :provider_id",
                            ConditionExpression="attribute_exists(event_id)",
                            ExpressionAttributeValues={
                                ":provider_id": packet.get("manifest", {}).get(
                                    "session_id", ""
                                )
                            },
                        )
                    return packet

                tool.request = guarded_request
                try:
                    receipt = tool.invoke(body.model_dump())
                finally:
                    tool.request = original_request
            if body.operation == "browser_start":
                provisional_row = self.table.get_item(
                    Key=provisional, ConsistentRead=True
                ).get("Item")
                if (
                    receipt["operation_status"] == "rejected"
                    and provisional_row
                    and not provisional_row.get("provider_id")
                ):
                    self.table.delete_item(Key=provisional)
                    provisional_row = None
                session_id = receipt.get("result", {}).get("session_id")
                if (
                    not session_id
                    and provisional_row
                    and provisional_row.get("provider_id")
                ):
                    session_id = next(
                        (
                            sid
                            for sid, session in tool.sessions.items()
                            if session["packet"].get("manifest", {}).get("session_id")
                            == provisional_row["provider_id"]
                        ),
                        None,
                    )
                if session_id and provisional_row:
                    self.table.update_item(
                        Key=provisional,
                        UpdateExpression="SET session_id = :session_id",
                        ConditionExpression="attribute_exists(event_id)",
                        ExpressionAttributeValues={":session_id": session_id},
                    )
                elif receipt["operation_status"] == "confirmed" and not any(
                    session_reference(session) == session_id for session in sessions
                ):
                    raise HTTPException(503, "Browser session ownership unavailable")
            elif body.operation in {"browser_step", "browser_close"}:
                session_id = body.payload["session_id"]
                if (
                    receipt["operation_status"] == "rejected"
                    and session_id not in tool.sessions
                    and any(
                        session_reference(session) == session_id for session in sessions
                    )
                ):
                    receipt = {
                        "schema_version": "1.0",
                        "task_id": owner[0],
                        "operation_id": body.operation_id,
                        "operation_status": "unknown",
                        "result": {
                            "status": "unknown",
                            "reason": "browser_session_owner_lost",
                        },
                    }
                if receipt.get("result", {}).get("cleanup_status") == "stopped":
                    for session in sessions:
                        if session_reference(session) == session_id:
                            self.table.delete_item(
                                Key={
                                    "event_id": session_partition(owner),
                                    "arrived_at": session["arrived_at"],
                                }
                            )
            elif body.operation == "cancel_jobs":
                pending = []
                for session in sessions:
                    session_id = session_reference(session)
                    if time.time() >= int(session.get("expires_at", 2**63)):
                        self.table.delete_item(
                            Key={
                                "event_id": session_partition(owner),
                                "arrived_at": session["arrived_at"],
                            }
                        )
                        continue
                    if (
                        session_id in tool.sessions
                        and tool.sessions[session_id].get("cleanup") == "stopped"
                    ):
                        self.table.delete_item(
                            Key={
                                "event_id": session_partition(owner),
                                "arrived_at": session["arrived_at"],
                            }
                        )
                        continue
                    if session_id not in tool.sessions and session.get("provider_id"):
                        from isolated_browser import stop_session

                        try:
                            stopped = stop_session(session["provider_id"])
                        except Exception:
                            stopped = False
                        if stopped:
                            self.table.delete_item(
                                Key={
                                    "event_id": session_partition(owner),
                                    "arrived_at": session["arrived_at"],
                                }
                            )
                            continue
                    pending.append(session_id)
                pending = sorted(set(pending))
                receipt["operation_status"] = "pending" if pending else "confirmed"
                receipt["result"] = {
                    "status": receipt["operation_status"],
                    "pending_jobs": pending,
                }
        except HTTPException:
            receipt = {
                "schema_version": "1.0",
                "task_id": owner[0],
                "operation_id": body.operation_id,
                "operation_status": "rejected",
                "result": {
                    "status": "refused",
                    "reason": "Task authority refused browser operation",
                },
            }
        except Exception:
            receipt = {
                "schema_version": "1.0",
                "task_id": owner[0],
                "operation_id": body.operation_id,
                "operation_status": "unknown",
                "result": {
                    "status": "unknown",
                    "reason": "browser_outcome_unavailable",
                },
            }
        finally:
            if authority is not None:
                authority.close()
        if (
            body.operation == "cancel_jobs"
            and receipt.get("operation_status") == "confirmed"
        ):
            self.tools.pop(owner, None)
        receipt = json.loads(json.dumps(receipt), parse_float=Decimal)
        self.table.update_item(
            Key=key(body.operation_id),
            UpdateExpression="SET #state = :done, receipt = :receipt",
            ConditionExpression="#state = :running",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":done": "done",
                ":running": "running",
                ":receipt": receipt,
            },
        )


def serve():
    table = boto3.resource("dynamodb").Table(os.environ["BROWSER_OPERATIONS_TABLE"])
    queue = boto3.client("sqs")
    consumer = BrowserConsumer(table)
    while True:
        batch = queue.receive_message(
            QueueUrl=os.environ["BROWSER_QUEUE_URL"],
            WaitTimeSeconds=20,
            MaxNumberOfMessages=1,
        ).get("Messages", [])
        for message in batch:
            consumer.consume(message["Body"])
            queue.delete_message(
                QueueUrl=os.environ["BROWSER_QUEUE_URL"],
                ReceiptHandle=message["ReceiptHandle"],
            )


if __name__ == "__main__":
    serve()
