"""Durable, owner-bound Gateway Web Search claims and read-only recovery."""

import base64
import hashlib
import json
import os
import re

import boto3
import requests
from adp_tools.contracts import UUID4
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from agentcore_tools.websearch import SearchInput


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class GatewaySearch:
    def __init__(self, client=None):
        self.client = client or boto3.client("dynamodb", region_name=os.environ["AWS_REGION"], config=Config(
            connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}))
        self.table = os.environ.get("ADP_TOOLS_TABLE", "")
        if not self.table:
            raise HTTPException(503, "Search operation store unavailable")

    @staticmethod
    def _key(claims, request_id):
        principal = claims["principal"]
        binding = [principal["tenant_id"], principal["user_id"], principal["team_id"],
                   claims.get("run_id"), claims.get("workload_id")]
        digest = hashlib.sha256(_json([binding, request_id])).hexdigest()
        return {"event_id": {"S": "GATEWAY_SEARCH#" + digest}, "arrived_at": {"S": "OP"}}

    def _read(self, claims, request_id):
        try:
            row = self.client.get_item(TableName=self.table, Key=self._key(claims, request_id),
                                       ConsistentRead=True).get("Item")
        except (BotoCoreError, ClientError):
            raise HTTPException(503, "Search operation store unavailable") from None
        return row

    def _owned(self, claims, execution_id, request_id):
        row = self._read(claims, request_id)
        if not row or row.get("execution_id", {}).get("S") != execution_id:
            raise HTTPException(404, "Search operation not found")
        return row

    @staticmethod
    def _receipt(row):
        result = {
            "status": row["state"]["S"], "execution_id": row["execution_id"]["S"],
            "request_id": row["request_id"]["S"], "receipt_id": row["receipt_id"]["S"],
        }
        if result["status"] == "confirmed":
            content = row["result_json"]["S"].encode("utf-8")
            result["result"] = json.loads(content)
            result["artifact"] = {
                "artifact_id": result["request_id"], "content_type": "application/json",
                "content_sha256": hashlib.sha256(content).hexdigest(), "byte_length": len(content),
            }
        elif result["status"] == "unknown":
            result["potential_query_count"] = 1
        if "retry_after_seconds" in row:
            result["retry_after_seconds"] = int(row["retry_after_seconds"]["N"])
        return result

    def search(self, arguments, claims, run_search, revalidate):
        if set(arguments) != {"execution_id", "request_id", "query"}:
            raise HTTPException(422, "Invalid search fields")
        execution_id, request_id = arguments["execution_id"], arguments["request_id"]
        if not all(isinstance(value, str) and re.fullmatch(UUID4, value)
                   for value in (execution_id, request_id)):
            raise HTTPException(422, "Invalid search identity")
        query = SearchInput.model_validate({"query": arguments["query"]}).model_dump(
            exclude_none=True, exclude_defaults=True, by_alias=True,
        )
        key = self._key(claims, request_id)
        digest = hashlib.sha256(_json(query)).hexdigest()
        row = {**key, "execution_id": {"S": execution_id}, "request_id": {"S": request_id},
               "receipt_id": {"S": claims["receipt_id"]}, "request_digest": {"S": digest},
               "state": {"S": "pending"}}
        try:
            self.client.put_item(TableName=self.table, Item=row,
                                 ConditionExpression="attribute_not_exists(event_id)")
        except (BotoCoreError, ClientError):
            prior = self._read(claims, request_id)
            if not prior:
                raise HTTPException(503, "Search operation store unavailable") from None
            if (prior.get("execution_id", {}).get("S") != execution_id
                    or prior.get("request_digest", {}).get("S") != digest):
                raise HTTPException(409, "Search request ID conflict") from None
            return self._receipt(prior)

        def before_search():
            if revalidate() != (claims, arguments):
                raise HTTPException(403, "Search authority changed")
            self.client.update_item(
                TableName=self.table, Key=key,
                UpdateExpression="SET #state = :unknown",
                ConditionExpression="#state = :pending",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":unknown": {"S": "unknown"}, ":pending": {"S": "pending"}},
            )

        try:
            result = run_search(query, before_search=before_search)
            if not isinstance(result, dict) or result.get("status") not in {"completed", "empty"} or not isinstance(result.get("results"), list):
                return self._receipt(self._owned(claims, execution_id, request_id))
            content = _json(result)
            if len(content) > 64000:
                raise ValueError("Search result exceeds durable artifact bound")
            self.client.update_item(
                TableName=self.table, Key=key,
                UpdateExpression="SET #state = :confirmed, result_json = :result",
                ConditionExpression="#state = :unknown",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":confirmed": {"S": "confirmed"}, ":unknown": {"S": "unknown"},
                                           ":result": {"S": content.decode("utf-8")}},
            )
        except HTTPException as exc:
            if exc.status_code == 403:
                raise
            if exc.status_code == 429:
                retry_after = (exc.headers or {}).get("Retry-After", "")
                if (retry_after.isascii() and retry_after.isdecimal() and
                        len(retry_after) <= 5 and 0 < int(retry_after) <= 86400):
                    self.client.update_item(
                        TableName=self.table, Key=key,
                        UpdateExpression="SET retry_after_seconds = :retry",
                        ExpressionAttributeValues={":retry": {"N": retry_after}},
                    )
            return self._receipt(self._owned(claims, execution_id, request_id))
        except (BotoCoreError, ClientError, ValueError, TypeError, TimeoutError, requests.RequestException):
            return self._receipt(self._owned(claims, execution_id, request_id))
        return self._receipt(self._owned(claims, execution_id, request_id))

    def operation_get(self, arguments, claims):
        if set(arguments) != {"execution_id", "request_id"} or not all(
            isinstance(arguments[key], str) and re.fullmatch(UUID4, arguments[key])
            for key in ("execution_id", "request_id")
        ):
            raise HTTPException(422, "Invalid search operation reference")
        return self._receipt(self._owned(claims, arguments["execution_id"], arguments["request_id"]))

    def artifact_read(self, arguments, claims):
        if (set(arguments) - {"execution_id", "artifact_id", "offset"} or
                not {"execution_id", "artifact_id"} <= arguments.keys() or
                not all(isinstance(arguments[key], str) and re.fullmatch(UUID4, arguments[key])
                        for key in ("execution_id", "artifact_id"))):
            raise HTTPException(422, "Invalid search artifact reference")
        offset = arguments.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise HTTPException(416, "Invalid search artifact offset")
        row = self._owned(claims, arguments["execution_id"], arguments["artifact_id"])
        if row["state"]["S"] != "confirmed":
            raise HTTPException(409, "Search artifact not yet available")
        content = row["result_json"]["S"].encode("utf-8")
        if offset > len(content):
            raise HTTPException(416, "Invalid search artifact offset")
        chunk = content[offset:offset + 8192]
        return {"artifact_id": arguments["artifact_id"], "content_type": "application/json",
                "offset": offset, "length": len(chunk), "total_length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "data_base64": base64.b64encode(chunk).decode("ascii")}
