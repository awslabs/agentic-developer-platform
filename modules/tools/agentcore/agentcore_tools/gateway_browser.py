"""Durable owner-bound browser FIFO claims and consumer fences."""

import base64
from decimal import Decimal
import hashlib
import json
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from adp_tools.storage import serialize
from fastapi import HTTPException

from agentcore_tools.gateway_search import _json
from agentcore_tools.url_contract import checked_url


def _packet_number(value):
    if isinstance(value, Decimal) and value.is_finite():
        return int(value) if value == value.to_integral_value() else float(value)
    raise ValueError("Invalid browser packet value")


class GatewayBrowser:
    def __init__(self, table=None, queue=None, clock=None):
        self.table = table or boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"]).Table(
            os.environ["BROWSER_OPERATIONS_TABLE"])
        self.queue = queue or boto3.client("sqs", region_name=os.environ["AWS_REGION"])
        self.clock = clock or time.time

    @staticmethod
    def _partition(claims, execution_id):
        principal = claims["principal"]
        owner = [principal["tenant_id"], principal["user_id"], principal["team_id"],
                 claims.get("run_id"), claims.get("workload_id"), execution_id]
        return "GATEWAY_BROWSER#" + hashlib.sha256(_json(owner)).hexdigest()

    @staticmethod
    def _key(partition, sort_key):
        return {"event_id": partition, "arrived_at": sort_key}

    def _read(self, partition, sort_key):
        return self.table.get_item(Key=self._key(partition, sort_key), ConsistentRead=True).get("Item")

    def _transact(self, writes):
        transaction = []
        for entry in writes:
            for action, details in entry.items():
                typed = dict(details)
                for field in ("Item", "Key", "ExpressionAttributeValues"):
                    if field in typed:
                        typed[field] = serialize(typed[field])
                transaction.append({action: typed})
        boto3.client("dynamodb", region_name=os.environ["AWS_REGION"]).transact_write_items(
            TransactItems=transaction)

    def _recover_start_identity(self, partition, session):
        """Recover a returned identity if closing raced its SESSION write."""
        if session.get("provider_id") or not session.get("start_id"):
            return session
        start = self._read(partition, "OP#" + session["start_id"])
        if (not start or start.get("operation") != "start" or
                start.get("execution_id") != session["execution_id"] or
                start.get("request_id") != session["start_id"] or
                start.get("generation") != session["generation"] or
                start.get("provider_deadline_at") != session["deadline_at"] or
                start.get("provider_dispatch") != "sent" or
                not isinstance(start.get("provider_id"), str) or not start["provider_id"] or
                not isinstance(start.get("provider_token"), str) or not start["provider_token"]):
            return session
        self.table.update_item(Key=self._key(partition, "SESSION"),
            UpdateExpression="SET provider_id = :provider, provider_token = :token",
            ConditionExpression=("#state = :closing AND execution_id = :execution AND start_id = :start "
                                 "AND generation = :generation AND deadline_at = :deadline "
                                 "AND attribute_not_exists(provider_id)"),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":closing": "closing", ":execution": session["execution_id"],
                ":start": session["start_id"], ":generation": session["generation"],
                ":deadline": session["deadline_at"],
                ":provider": start["provider_id"], ":token": start["provider_token"]})
        return self._read(partition, "SESSION")

    @staticmethod
    def _digest(operation, arguments, claims):
        return hashlib.sha256(_json([operation, arguments, claims["principal"],
                                     claims.get("run_id"), claims.get("workload_id")])).hexdigest()

    @staticmethod
    def _receipt(row):
        status = {"queued": "pending", "running": "unknown"}.get(row["state"], row["state"])
        result = {"status": status, "execution_id": row["execution_id"],
                  "request_id": row["request_id"], "receipt_id": row["receipt_id"],
                  "generation": int(row["generation"])}
        if "result" in row:
            result["result"] = json.loads(json.dumps(row["result"], default=int))
        return result

    def operation_get(self, arguments, claims):
        partition = self._partition(claims, arguments["execution_id"])
        row = self._read(partition, "OP#" + arguments["request_id"])
        if not row or row["execution_id"] != arguments["execution_id"]:
            raise HTTPException(404, "Browser operation unavailable")
        return self._receipt(row)

    def artifact_read(self, arguments, claims):
        partition = self._partition(claims, arguments["execution_id"])
        artifact = self._read(partition, "ARTIFACT#" + arguments["artifact_id"])
        if not artifact:
            raise HTTPException(404, "Browser artifact unavailable")
        content = bytes(artifact["content"])
        offset = arguments.get("offset", 0)
        if offset > len(content):
            raise HTTPException(416, "Invalid artifact offset")
        chunk = content[offset:offset + 8192]
        return {"artifact_id": arguments["artifact_id"], "content_type": "application/json",
                "offset": offset, "length": len(chunk), "total_length": len(content),
                "sha256": artifact["sha256"], "data_base64": base64.b64encode(chunk).decode()}

    def _close_undispatched_start(self, partition, session):
        """Prove no provider effect with the same atomic fence dispatch uses."""
        if (session.get("state") not in {"closing", "closed"} or
                "provider_id" in session or "provider_token" in session or not session.get("start_id")):
            return False
        start = self._read(partition, "OP#" + session["start_id"])
        if (not start or start.get("operation") != "start" or
                "provider_id" in start or "provider_token" in start or
                start.get("request_id") != session["start_id"] or
                start.get("execution_id") != session.get("execution_id") or
                start.get("generation") != session.get("generation") or
                start.get("execution_deadline_at") != session.get("deadline_at")):
            return False
        if start.get("provider_dispatch") == "cancelled":
            return (session["state"] == "closed" and session.get("cleanup_status") == "confirmed" and
                    session.get("cleanup_proof") == start.get("cleanup_proof") == "start_not_dispatched")
        if "provider_dispatch" in start or session["state"] != "closing":
            return False
        binding = {":execution": session["execution_id"], ":start": session["start_id"],
                   ":generation": session["generation"], ":deadline": session["deadline_at"],
                   ":proof": "start_not_dispatched"}
        self._transact([
            {"Update": {"TableName": self.table.name, "Key": self._key(partition, "OP#" + session["start_id"]),
                "UpdateExpression": "SET provider_dispatch = :cancelled, cleanup_proof = :proof",
                "ConditionExpression": ("#operation = :operation AND #state IN (:queued, :running, :failed, :unknown) "
                    "AND execution_id = :execution AND request_id = :start AND generation = :generation "
                    "AND execution_deadline_at = :deadline AND attribute_not_exists(provider_dispatch) "
                    "AND attribute_not_exists(provider_id) AND attribute_not_exists(provider_token)"),
                "ExpressionAttributeNames": {"#operation": "operation", "#state": "state"},
                "ExpressionAttributeValues": {**binding, ":cancelled": "cancelled", ":operation": "start",
                    ":queued": "queued", ":running": "running", ":failed": "failed", ":unknown": "unknown"}}},
            {"Update": {"TableName": self.table.name, "Key": self._key(partition, "SESSION"),
                "UpdateExpression": ("SET #state = :closed, cleanup_status = :confirmed, cleanup_proof = :proof "
                                     "REMOVE cleanup_partition, next_cleanup_at"),
                "ConditionExpression": ("#state = :closing AND execution_id = :execution AND start_id = :start "
                    "AND generation = :generation AND deadline_at = :deadline "
                    "AND attribute_not_exists(provider_id) AND attribute_not_exists(provider_token)"),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {**binding, ":closing": "closing", ":closed": "closed",
                                              ":confirmed": "confirmed"}}},
        ])
        return True

    def execution_close(self, arguments, claims, revalidate):
        if revalidate() != (claims, arguments, "browser", "execution_close"):
            raise HTTPException(403, "Browser authority changed")
        execution_id = arguments["execution_id"]
        partition = self._partition(claims, execution_id)
        key = self._key(partition, "SESSION")
        try:
            session = self._read(partition, "SESSION")
            if not session or session.get("execution_id") != execution_id:
                raise HTTPException(404, "Browser session unavailable")
            if session["state"] in {"active", "starting"}:
                try:
                    self.table.update_item(Key=key,
                        UpdateExpression="SET #state = :closing, next_cleanup_at = :now",
                        ConditionExpression="#state = :previous AND generation = :generation",
                        ExpressionAttributeNames={"#state": "state"},
                        ExpressionAttributeValues={":closing": "closing", ":previous": session["state"],
                                                   ":generation": session["generation"],
                                                   ":now": int(self.clock())})
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                session = self._read(partition, "SESSION")
            if session["state"] == "closing" and not session.get("provider_id"):
                try:
                    session = self._recover_start_identity(partition, session)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                    session = self._read(partition, "SESSION")
            if self._close_undispatched_start(partition, session):
                return {"execution_id": execution_id, "status": "confirmed",
                        "termination_confirmed": True, "provider_status": "NOT_STARTED",
                        "cleanup_proof": "start_not_dispatched"}
            pending = {"execution_id": execution_id, "status": "pending", "termination_confirmed": False}
            if (session["state"] not in {"closing", "closed"} or
                    not isinstance(session.get("provider_id"), str)):
                return pending
            from agentcore_tools.browser_runtime.isolated_browser import stop_session
            try:
                terminated = stop_session(session["provider_id"])
            except (ClientError, BotoCoreError, ValueError, RuntimeError, TimeoutError):
                return pending
            if not terminated:
                return pending
            if session["state"] == "closed":
                if session.get("cleanup_status") != "confirmed":
                    try:
                        self.table.update_item(Key=key,
                            UpdateExpression="SET cleanup_status = :confirmed REMOVE cleanup_partition, next_cleanup_at",
                            ConditionExpression="#state = :closed AND provider_id = :provider",
                            ExpressionAttributeNames={"#state": "state"},
                            ExpressionAttributeValues={":closed": "closed", ":provider": session["provider_id"],
                                                       ":confirmed": "confirmed"})
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                            raise
                        return pending
                return {"execution_id": execution_id, "status": "confirmed",
                        "termination_confirmed": True, "provider_status": "TERMINATED"}
            try:
                self.table.update_item(Key=key,
                    UpdateExpression="SET #state = :closed, cleanup_status = :confirmed "
                                     "REMOVE cleanup_partition, next_cleanup_at",
                    ConditionExpression="#state = :closing AND provider_id = :provider",
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={":closed": "closed", ":closing": "closing",
                                               ":confirmed": "confirmed", ":provider": session["provider_id"]})
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                current = self._read(partition, "SESSION")
                if current.get("state") != "closed" or current.get("cleanup_status") != "confirmed":
                    return pending
            return {"execution_id": execution_id, "status": "confirmed",
                    "termination_confirmed": True, "provider_status": "TERMINATED"}
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "Browser cleanup unavailable") from None

    def submit(self, operation, arguments, claims, revalidate, assertion):
        if operation not in {"start", "step", "inspect", "close"} or revalidate() != (
                claims, arguments, "browser", operation):
            raise HTTPException(403, "Browser authority changed")
        if not isinstance(assertion, str) or not assertion:
            raise HTTPException(503, "Browser assertion unavailable")
        if operation == "step" and arguments.get("action") == "navigate":
            checked_url(arguments["url"])
        execution_id = arguments["execution_id"]
        partition = self._partition(claims, execution_id)
        request_id = arguments.get("request_id", "close")
        sort_key = "OP#" + request_id
        digest = self._digest(operation, arguments, claims)
        action_digest = self._digest(operation, {key: value for key, value in arguments.items()
                                                  if key != "request_id"}, claims)
        now = int(self.clock())
        try:
            row = self._read(partition, sort_key)
            if row:
                if row["digest"] != digest:
                    raise HTTPException(409, "Browser operation ID conflict")
            else:
                session = self._read(partition, "SESSION")
                if operation == "start":
                    if session:
                        raise HTTPException(409, "Browser session already claimed")
                    generation = 1
                else:
                    if (not session or session["state"] != "active" or
                            session["session_id"] != arguments["session_id"] or
                            (operation in {"step", "inspect"} and session["view_id"] != arguments["view_id"])):
                        raise HTTPException(404, "Browser session or view unavailable")
                    if session["deadline_at"] <= now:
                        raise HTTPException(403, "Browser session expired")
                    if operation == "step" and arguments["action"] in {"follow", "expand"}:
                        packet = session.get("packet")
                        packet = json.loads(packet) if isinstance(packet, str) else packet
                        if not isinstance(packet, dict) or not any(
                                choice.get("id") == arguments.get("candidate_id") and
                                choice.get("action") == arguments["action"]
                                for choice in packet.get("choices", []) if isinstance(choice, dict)):
                            raise HTTPException(404, "Browser candidate unavailable")
                    generation = session["generation"]
                row = {**self._key(partition, sort_key), "execution_id": execution_id,
                       "request_id": request_id, "receipt_id": claims["receipt_id"],
                       "operation": operation, "digest": digest, "generation": generation,
                       "state": "queued", "created": now}
                writes = [{"Put": {"TableName": self.table.name, "Item": row,
                                   "ConditionExpression": "attribute_not_exists(event_id)"}}]
                if operation in {"start", "step", "close"}:
                    writes.append({"Put": {"TableName": self.table.name,
                        "Item": {**self._key(partition, "ALIAS#" + action_digest), "request_id": request_id},
                        "ConditionExpression": "attribute_not_exists(event_id)"}})
                if operation == "start":
                    requested = claims.get("execution_deadline_at", now + 900)
                    if type(requested) is not int or requested > now + 900:
                        raise HTTPException(403, "Browser authority expired")
                    deadline = min(requested, claims.get("run_expires_at") or now + 900)
                    if deadline <= now:
                        raise HTTPException(403, "Browser authority expired")
                    row["execution_deadline_at"] = deadline
                    writes.append({"Put": {"TableName": self.table.name,
                                           "Item": {**self._key(partition, "SESSION"),
                                                    "execution_id": execution_id, "start_id": request_id,
                                                    "generation": generation, "state": "starting",
                                                    "deadline_at": deadline, "cleanup_partition": "due",
                                                    "next_cleanup_at": deadline},
                                           "ConditionExpression": "attribute_not_exists(event_id)"}})
                else:
                    writes.append({"ConditionCheck": {
                        "TableName": self.table.name, "Key": self._key(partition, "SESSION"),
                        "ConditionExpression": "#state = :active AND generation = :generation AND deadline_at > :now"
                            + " AND session_id = :session"
                            + (" AND view_id = :view" if operation in {"step", "inspect"} else ""),
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {":active": "active", ":generation": generation,
                                                      ":now": now, ":session": arguments["session_id"],
                                                      **({":view": arguments["view_id"]}
                                                         if operation in {"step", "inspect"} else {})},
                    }})
                try:
                    self._transact(writes)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                        alias = self._read(partition, "ALIAS#" + action_digest)
                        if alias and alias["request_id"] != request_id:
                            raise HTTPException(409, "Browser action already claimed") from None
                    raise
            if row["state"] == "queued":
                self.queue.send_message(
                    QueueUrl=os.environ["BROWSER_QUEUE_URL"],
                    MessageBody=json.dumps({"kind": "gateway", "operation": operation,
                        "arguments": arguments, "claims": claims, "assertion": assertion,
                        "digest": digest, "generation": int(row["generation"])}),
                    MessageGroupId=partition.removeprefix("GATEWAY_BROWSER#"),
                    MessageDeduplicationId=hashlib.sha256((partition + sort_key).encode()).hexdigest(),
                )
            return self._receipt(row)
        except (BotoCoreError, ClientError) as exc:
            raise HTTPException(503, "Browser queue unavailable") from exc


class GatewayBrowserConsumer:
    def __init__(self, browser, reauthorize, dispatch):
        self.browser = browser
        self.reauthorize = reauthorize
        self.dispatch = dispatch

    def consume(self, message):
        envelope = json.loads(message)
        if envelope.get("kind") != "gateway":
            return
        claims, arguments, operation = envelope["claims"], envelope["arguments"], envelope["operation"]
        partition = self.browser._partition(claims, arguments["execution_id"])
        request_id = arguments.get("request_id", "close")
        key = self.browser._key(partition, "OP#" + request_id)
        row = self.browser._read(partition, key["arrived_at"])
        if (not row or row["state"] != "queued" or row["operation"] != operation or
                row.get("execution_id") != arguments["execution_id"] or row.get("request_id") != request_id or
                row["digest"] != envelope["digest"] or row["generation"] != envelope["generation"] or
                row["digest"] != self.browser._digest(operation, arguments, claims)):
            return
        session = self.browser._read(partition, "SESSION")
        if (not session or session["generation"] != row["generation"] or
                session.get("execution_id") != arguments["execution_id"] or
                session["deadline_at"] <= int(self.browser.clock()) or
                (operation == "start" and (session["state"] != "starting" or
                                           session.get("start_id") != request_id or
                                           row.get("execution_deadline_at") != session["deadline_at"])) or
                (operation != "start" and (session["state"] != "active" or
                    session["session_id"] != arguments["session_id"])) or
                (operation in {"step", "inspect"} and session["view_id"] != arguments["view_id"])):
            self.browser.table.update_item(Key=key, UpdateExpression="SET #state = :failed",
                ConditionExpression="#state = :queued",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":failed": "failed", ":queued": "queued"})
            return
        try:
            authorized = self.reauthorize(envelope) == claims
        except HTTPException as exc:
            if exc.status_code not in {401, 403, 404}:
                raise
            authorized = False
        if not authorized:
            self.browser.table.update_item(Key=key, UpdateExpression="SET #state = :failed",
                ConditionExpression="#state = :queued",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":failed": "failed", ":queued": "queued"})
            return
        try:
            update = {"TableName": self.browser.table.name, "Key": key,
                "UpdateExpression": "SET #state = :running",
                "ConditionExpression": ("#state = :queued AND digest = :digest AND generation = :generation "
                                        "AND execution_id = :execution AND request_id = :request"),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":running": "running", ":queued": "queued",
                                              ":digest": row["digest"], ":generation": row["generation"],
                                              ":execution": arguments["execution_id"], ":request": request_id}}
            writes = [{"Update": update}]
            if operation == "start":
                writes.append({"ConditionCheck": {
                    "TableName": self.browser.table.name, "Key": self.browser._key(partition, "SESSION"),
                    "ConditionExpression": ("#state = :starting AND start_id = :start AND execution_id = :execution "
                                            "AND generation = :generation AND deadline_at = :deadline "
                                            "AND deadline_at > :now"),
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {":starting": "starting", ":start": request_id,
                        ":execution": arguments["execution_id"], ":generation": row["generation"],
                        ":deadline": session["deadline_at"], ":now": int(self.browser.clock())}}})
            self.browser._transact(writes)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                return
            raise
        try:
            if operation == "close":
                self.browser.table.update_item(Key=self.browser._key(partition, "SESSION"),
                    UpdateExpression="SET #state = :closing, next_cleanup_at = :now, close_id = :request",
                    ConditionExpression="#state = :active AND generation = :generation",
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={":closing": "closing", ":active": "active",
                        ":generation": row["generation"], ":now": int(self.browser.clock()),
                        ":request": request_id})
            result = self.dispatch(operation, arguments, claims)
            if result.get("status") != "confirmed":
                raise ValueError("Browser outcome uncertain")
            if operation in {"start", "step"} and (
                    not isinstance(result.get("session_id"), str) or
                    not isinstance(result.get("view_id"), str) or
                    not 0 < len(result["view_id"]) <= 256 or
                    (operation == "step" and result["session_id"] != arguments["session_id"])):
                raise ValueError("Browser state incomplete")
            if operation == "start":
                if len(result["session_id"]) != 64:
                    raise ValueError("Browser session unavailable")
                self.browser.table.update_item(Key=self.browser._key(partition, "SESSION"),
                    UpdateExpression="SET #state = :active, session_id = :session, view_id = :view",
                    ConditionExpression=("#state = :starting AND generation = :generation AND start_id = :start "
                                         "AND execution_id = :execution AND deadline_at = :deadline AND deadline_at > :now"),
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={":active": "active", ":starting": "starting",
                        ":session": result["session_id"], ":view": result["view_id"],
                        ":generation": row["generation"], ":start": request_id,
                        ":execution": arguments["execution_id"], ":deadline": session["deadline_at"],
                        ":now": int(self.browser.clock())})
            elif operation == "step":
                self.browser.table.update_item(Key=self.browser._key(partition, "SESSION"),
                    UpdateExpression="SET view_id = :new, generation = :next",
                    ConditionExpression="#state = :active AND generation = :generation AND view_id = :previous",
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={":active": "active", ":generation": row["generation"],
                        ":previous": arguments["view_id"], ":new": result["view_id"],
                        ":next": row["generation"] + 1})
            elif operation == "close":
                if (result.get("termination_confirmed") is not True or
                        result.get("session_id") != arguments["session_id"]):
                    raise ValueError("Browser termination unconfirmed")
                self.browser.table.update_item(Key=self.browser._key(partition, "SESSION"),
                    UpdateExpression="SET #state = :closed REMOVE cleanup_partition, next_cleanup_at",
                    ConditionExpression="#state = :closing AND generation = :generation",
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={":closed": "closed", ":closing": "closing",
                                               ":generation": row["generation"]})
            self.browser.table.update_item(Key=key, UpdateExpression="SET #state = :confirmed, #result = :result",
                ConditionExpression="#state = :running AND digest = :digest",
                ExpressionAttributeNames={"#state": "state", "#result": "result"},
                ExpressionAttributeValues={":confirmed": "confirmed", ":running": "running",
                                           ":digest": row["digest"], ":result": result})
        except Exception:
            self.browser.table.update_item(Key=key, UpdateExpression="SET #state = :unknown",
                ConditionExpression="#state = :running",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":unknown": "unknown", ":running": "running"})


class GatewayBrowserProvider:
    def __init__(self, browser, request=None):
        self.browser = browser
        if request is None:
            from agentcore_tools.browser_runtime.local_browser import investigation_request
            request = investigation_request
        self.request = request

    def __call__(self, operation, arguments, claims):
        partition = self.browser._partition(claims, arguments["execution_id"])
        session = self.browser._read(partition, "SESSION")
        if not session:
            raise HTTPException(404, "Browser session unavailable")
        if operation == "start":
            start_id = arguments["request_id"]
            start = self.browser._read(partition, "OP#" + start_id)
            digest = self.browser._digest(operation, arguments, claims)
            if (not start or start.get("operation") != "start" or start.get("digest") != digest or
                    start.get("execution_id") != arguments["execution_id"] or start.get("request_id") != start_id or
                    start.get("generation") != session["generation"] or
                    start.get("execution_deadline_at") != session["deadline_at"] or
                    session.get("execution_id") != arguments["execution_id"] or
                    session.get("start_id") != start_id):
                raise HTTPException(404, "Browser start unavailable")
            # The dispatch marker and lifecycle check share one transaction. A
            # close before this point wins; a close after it owns an in-flight
            # start whose returned identity must still be retained.
            self.browser._transact([
                {"Update": {"TableName": self.browser.table.name,
                    "Key": self.browser._key(partition, "OP#" + start_id),
                    "UpdateExpression": "SET provider_dispatch = :sent, provider_deadline_at = :deadline",
                    "ConditionExpression": ("#state = :running AND digest = :digest AND generation = :generation "
                                            "AND request_id = :start AND execution_id = :execution "
                                            "AND execution_deadline_at = :deadline "
                                            "AND attribute_not_exists(provider_dispatch)"),
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {":running": "running", ":digest": digest,
                        ":generation": session["generation"], ":sent": "sent",
                        ":deadline": session["deadline_at"], ":start": start_id,
                        ":execution": arguments["execution_id"]}}},
                {"ConditionCheck": {"TableName": self.browser.table.name,
                    "Key": self.browser._key(partition, "SESSION"),
                    "ConditionExpression": ("#state = :starting AND start_id = :start AND execution_id = :execution "
                                            "AND generation = :generation AND deadline_at = :deadline "
                                            "AND deadline_at > :now"),
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {":starting": "starting", ":start": start_id,
                        ":execution": arguments["execution_id"], ":generation": session["generation"],
                        ":deadline": session["deadline_at"], ":now": int(self.browser.clock())}}},
            ])
            packet = self.request("start", {"url": arguments["url"], "deadline_at": int(session["deadline_at"])})
            token = packet.pop("session_token")
            provider_id = packet.get("manifest", {}).get("session_id", "")
            if not isinstance(token, str) or not token or not isinstance(provider_id, str) or not provider_id:
                raise ValueError("Browser provider identity unavailable")
            persistence_error = None
            # Keep a second, immutable start-owned copy so cleanup can recover
            # even when a conditional SESSION write or its response is lost.
            for sort_key, condition, values in (
                ("OP#" + start_id, "#state = :running AND provider_dispatch = :sent AND digest = :digest",
                 {":running": "running", ":sent": "sent", ":digest": digest}),
                ("SESSION", "(#state = :starting OR #state = :closing) AND start_id = :start "
                 "AND deadline_at = :deadline AND (attribute_not_exists(provider_id) OR provider_id = :provider)",
                 {":starting": "starting", ":closing": "closing", ":start": start_id,
                  ":deadline": session["deadline_at"]}),
            ):
                try:
                    self.browser.table.update_item(Key=self.browser._key(partition, sort_key),
                        UpdateExpression="SET provider_token = :token, provider_id = :provider",
                        ConditionExpression=condition + " AND generation = :generation AND execution_id = :execution",
                        ExpressionAttributeNames={"#state": "state"},
                        ExpressionAttributeValues={**values, ":generation": session["generation"],
                            ":execution": arguments["execution_id"], ":token": token, ":provider": provider_id})
                except (ClientError, BotoCoreError) as exc:
                    persistence_error = exc
            if persistence_error:
                raise persistence_error
            handle = hashlib.sha256(_json([partition, session["start_id"]])).hexdigest()
        else:
            if session.get("session_id") != arguments["session_id"] or not session.get("provider_token"):
                raise HTTPException(404, "Browser session unavailable")
            handle = session["session_id"]
            if operation == "inspect":
                packet = session.get("packet")
                if not packet:
                    raise HTTPException(404, "Browser evidence unavailable")
                packet = json.loads(packet if isinstance(packet, str) else json.dumps(packet, default=_packet_number))
            else:
                payload = {"session_token": session["provider_token"]}
                if operation == "step":
                    payload.update({field: value for field, value in arguments.items()
                                    if field not in {"execution_id", "request_id", "session_id"}})
                packet = self.request("close" if operation == "close" else "step", payload)
        if operation == "close":
            if packet.get("cleanup_status") != "stopped" or packet.get("session_open") is not False:
                raise ValueError("Browser stop unconfirmed")
            return {"status": "confirmed", "session_id": handle, "termination_confirmed": True,
                    "cleanup_status": "stopped"}
        if packet.get("view_id") is None:
            raise ValueError("Browser view unavailable")
        content = _json(packet)
        if len(content) > 128000:
            raise ValueError("Browser evidence exceeds bound")
        artifact_id = arguments["request_id"]
        digest = hashlib.sha256(content).hexdigest()
        self.browser.table.put_item(Item={**self.browser._key(partition, "ARTIFACT#" + artifact_id),
            "content": content, "sha256": digest, "byte_length": len(content)},
            ConditionExpression="attribute_not_exists(event_id)")
        if operation in {"start", "step"}:
            self.browser.table.update_item(Key=self.browser._key(partition, "SESSION"),
                UpdateExpression="SET packet = :packet",
                ConditionExpression="generation = :generation",
                ExpressionAttributeValues={":packet": content.decode(), ":generation": session["generation"]})
        observations = [
            {field: (observation[field][:2000] if isinstance(observation[field], str)
                     else observation[field][:5] if isinstance(observation[field], list) else observation[field])
             for field in ("page_title", "visible_text", "final_url", "http_status", "errors",
                           "screenshot_status", "screenshot_sha256") if field in observation}
            for observation in packet.get("observations", [])[:2] if isinstance(observation, dict)
        ]
        return {"status": "confirmed", "session_id": handle, "view_id": packet["view_id"],
                "choices": packet.get("choices", [])[:12], "observations": observations,
                "artifact": {"artifact_id": artifact_id, "content_sha256": digest,
                             "byte_length": len(content), "content_type": "application/json"}}


class GatewayBrowserReconciler:
    def __init__(self, browser, request):
        self.browser = browser
        self.request = request

    def run_once(self):
        now = int(self.browser.clock())
        page = self.browser.table.query(IndexName="cleanup-due",
            KeyConditionExpression="cleanup_partition = :due AND next_cleanup_at <= :now",
            ExpressionAttributeValues={":due": "due", ":now": now}, Limit=25)
        for candidate in page["Items"]:
            key = self.browser._key(candidate["event_id"], "SESSION")
            try:
                self.browser.table.update_item(Key=key,
                    UpdateExpression="SET #state = :closing, next_cleanup_at = :retry",
                    ConditionExpression="cleanup_partition = :due AND next_cleanup_at <= :now AND #state <> :closed",
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={":due": "due", ":now": now, ":closing": "closing",
                                               ":closed": "closed", ":retry": now + 60})
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    continue
                raise
            session = self.browser._read(candidate["event_id"], "SESSION")
            try:
                if self.browser._close_undispatched_start(candidate["event_id"], session):
                    continue
                session = self.browser._recover_start_identity(candidate["event_id"], session)
            except (ClientError, BotoCoreError):
                continue
            if not session.get("provider_token"):
                continue
            try:
                packet = self.request("close", {"session_token": session["provider_token"]})
            except (RuntimeError, ValueError, ClientError, BotoCoreError):
                continue
            if not isinstance(packet, dict) or packet.get("cleanup_status") != "stopped" or packet.get("session_open") is not False:
                continue
            if session.get("close_id"):
                operation = self.browser._key(candidate["event_id"], "OP#" + session["close_id"])
                result = {"status": "confirmed", "session_id": session["session_id"],
                          "termination_confirmed": True, "cleanup_status": "stopped"}
                try:
                    self.browser.table.update_item(Key=operation,
                        UpdateExpression="SET #state = :confirmed, #result = :result",
                        ConditionExpression="#state = :unknown OR #state = :running",
                        ExpressionAttributeNames={"#state": "state", "#result": "result"},
                        ExpressionAttributeValues={":confirmed": "confirmed", ":unknown": "unknown",
                                                   ":running": "running", ":result": result})
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
            self.browser.table.update_item(Key=key,
                UpdateExpression="SET #state = :closed, cleanup_status = :confirmed REMOVE cleanup_partition, next_cleanup_at",
                ConditionExpression="#state = :closing AND provider_token = :token",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":closed": "closed", ":closing": "closing", ":confirmed": "confirmed",
                                           ":token": session["provider_token"]})
