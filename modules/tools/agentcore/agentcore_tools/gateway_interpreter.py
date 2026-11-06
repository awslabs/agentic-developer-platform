"""Owner-bound Gateway Code Interpreter starts, executions and read-only polling."""

import base64
from contextlib import contextmanager
import hashlib
import json
import os
import shlex
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from agentcore_tools.code_interpreter import CodeInterpreter, PATH, ProviderExecutionRefused
from agentcore_tools.gateway_search import _json


# Bounded in-call wait for a submitted execution before answering ``pending`` (#8223 Dev Box tool latency).
EXECUTE_WAIT_SECONDS = float(os.environ.get("ADP_TOOLS_EXECUTE_WAIT_SECONDS", "6"))
EXECUTE_POLL_INTERVAL_SECONDS = 0.25
_sleep = time.sleep
_monotonic = time.monotonic


class GatewayInterpreter:
    def __init__(self, client=None, provider=None):
        self.table = os.environ.get("ADP_TOOLS_TABLE", "")
        self.identifier = os.environ.get("ADP_CODE_INTERPRETER_ID", "")
        if not self.table:
            raise HTTPException(503, "Interpreter unavailable")
        self.client = client or boto3.client("dynamodb", region_name=os.environ["AWS_REGION"], config=Config(
            connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}))
        self.provider = provider

    def _provider(self):
        if self.provider is None:
            self.provider = boto3.client("bedrock-agentcore", config=Config(
                connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 1}))
        return self.provider

    def _settle_task(self, key, row, session):
        """Read the provider task once; record its outcome on ``row``'s operation when it is final.

        Returns the refreshed row. Shared by ``execute`` (bounded wait right after submission) and
        ``result`` (the owner's later poll). A non-final status or a provider error leaves the row as it
        was; the caller decides whether to wait again.
        """
        interpreter = CodeInterpreter(None, None, self.identifier, provider=self._provider())
        try:
            data = interpreter.safe_result(interpreter.invoke(session["provider_id"]["S"],
                "getTask", {"taskId": row["provider_task_id"]["S"]}, native="native_interpreter" in session))
            data.pop("taskId", None)
            if "taskStatus" in data:
                data["status"] = data.pop("taskStatus")
            if data.get("status") not in {"completed", "failed", "cancelled"}:
                return row
            if data["status"] == "completed" and type(data.get("exitCode")) is int and data["exitCode"] != 0:
                data["status"] = "failed"
            content = _json(data)
            if len(content) > 24000:
                raise ValueError("Interpreter result exceeds bound")
            state = "confirmed" if data["status"] == "completed" else "failed"
            self._update(key, {"result_json": {"S": content.decode()}, "state": {"S": state}},
                         "attribute_not_exists(result_json)")
        except (BotoCoreError, ClientError, ValueError, HTTPException, KeyError, TimeoutError):
            pass
        return self._read_key(key)

    def _wait_for_task(self, key, row, session):
        if EXECUTE_WAIT_SECONDS <= 0:
            return row
        deadline = _monotonic() + EXECUTE_WAIT_SECONDS
        while row.get("state", {}).get("S") == "pending":
            row = self._settle_task(key, row, session)
            if row.get("state", {}).get("S") != "pending" or _monotonic() >= deadline:
                break
            _sleep(min(EXECUTE_POLL_INTERVAL_SECONDS, max(0.0, deadline - _monotonic())))
        return row

    @staticmethod
    def _key(claims, execution_id, sort_key):
        principal = claims["principal"]
        identity = [principal["tenant_id"], principal["user_id"], principal["team_id"],
                    claims.get("run_id"), claims.get("workload_id"), execution_id]
        return {"event_id": {"S": "GATEWAY_CODE#" + hashlib.sha256(_json(identity)).hexdigest()},
                "arrived_at": {"S": sort_key}}

    def _read(self, claims, execution_id, sort_key):
        return self._read_key(self._key(claims, execution_id, sort_key))

    def _read_key(self, key):
        try:
            return self.client.get_item(TableName=self.table, Key=key, ConsistentRead=True).get("Item")
        except (BotoCoreError, ClientError):
            raise HTTPException(503, "Interpreter state unavailable") from None

    def _put(self, key, row):
        try:
            self.client.put_item(TableName=self.table, Item={**key, **row},
                                 ConditionExpression="attribute_not_exists(event_id)")
            return True
        except (BotoCoreError, ClientError):
            return False

    def _update(self, key, values, condition="attribute_exists(event_id)"):
        updates = ", ".join(f"#{field} = :{field}" for field in values)
        try:
            self.client.update_item(TableName=self.table, Key=key, UpdateExpression="SET " + updates,
                                    ConditionExpression=condition,
                                    ExpressionAttributeNames={"#" + field: field for field in values},
                                    ExpressionAttributeValues={":" + field: value for field, value in values.items()})
        except (BotoCoreError, ClientError):
            raise HTTPException(503, "Interpreter outcome unavailable") from None

    @staticmethod
    def _receipt(row):
        receipt = {"status": row["state"]["S"], "execution_id": row["execution_id"]["S"],
                   "request_id": row["request_id"]["S"], "receipt_id": row["receipt_id"]["S"]}
        if "result_json" in row:
            receipt["result"] = json.loads(row["result_json"]["S"])
            receipt["content_sha256"] = hashlib.sha256(row["result_json"]["S"].encode()).hexdigest()
        if row.get("provider_task_id"):
            receipt["provider_id"] = row["provider_task_id"]["S"]
        if row.get("session_handle") and (row["state"]["S"] == "confirmed" or
                                          row.get("provider_task_id")):
            receipt["session_id"] = row["session_handle"]["S"]
        if row.get("cleanup_state"):
            receipt["cleanup_status"] = "confirmed" if row["cleanup_state"]["S"] == "closed" else "pending"
            receipt["termination_confirmed"] = row["cleanup_state"]["S"] == "closed"
        if row.get("artifact_sha256"):
            receipt["artifact"] = {"artifact_id": row["request_id"]["S"],
                                   "content_type": "application/octet-stream",
                                   "content_sha256": row["artifact_sha256"]["S"],
                                   "byte_length": int(row["artifact_length"]["N"])}
        return receipt

    def _session(self, arguments, claims):
        row = self._read(claims, arguments["execution_id"], "CODE_SESSION")
        if not row or row.get("session_handle", {}).get("S") != arguments["session_id"]:
            raise HTTPException(404, "Interpreter session not found")
        self._native_session(row, claims)
        return row

    @staticmethod
    def _native_effect(session, arguments, claims, operation):
        if "native_interpreter" not in session:
            return
        binding = claims.get("native_interpreter", {})
        if (binding.get("kind") != "effect" or binding.get("tool") != "code_interpreter___" + operation
                or binding.get("request_id") != arguments["request_id"]):
            raise HTTPException(403, "Original native Interpreter effect required")

    @staticmethod
    def _native_session(row, claims):
        binding = claims.get("native_interpreter")
        original = row.get("native_interpreter", {}).get("S")
        if original is None and binding is None:
            return None
        try:
            original = json.loads(original)
            if (not isinstance(binding, dict) or type(binding.get("version")) is not int or binding["version"] != 1
                    or not isinstance(binding.get("run_id"), str) or not binding["run_id"]
                    or binding.get("run_id") != original["run_id"] or binding["run_id"] != claims.get("run_id")
                    or binding.get("execution_id") != row["execution_id"]["S"]
                    or not isinstance(binding.get("operation_id"), str) or not binding["operation_id"]):
                raise ValueError
            if binding.get("kind") == "effect":
                if (binding.get("reservation_id") != claims.get("receipt_id")
                        or any(binding.get(field) != original[field] for field in
                               ("session_deadline_at", "cleanup_deadline_at", "cleanup_attempts"))):
                    raise ValueError
            elif binding.get("kind") == "cleanup":
                if (binding.get("original_operation_id") != original["operation_id"]
                        or binding.get("reservation_id") != original["reservation_id"]
                        or claims.get("receipt_id") != original["reservation_id"]
                        or type(binding.get("attempt")) is not int or not 1 <= binding["attempt"] <= original["cleanup_attempts"]
                        or type(binding.get("dispatch_deadline_at")) not in (int, float)
                        or not time.time() < binding["dispatch_deadline_at"] <= original["cleanup_deadline_at"]):
                    raise ValueError
            else:
                raise ValueError
            return original
        except (ValueError, TypeError, KeyError):
            raise HTTPException(403, "Original native Interpreter binding required") from None

    def start(self, arguments, claims, revalidate):
        execution_id, request_id = arguments["execution_id"], arguments["request_id"]
        key = self._key(claims, execution_id, "CODE_SESSION")
        prior = self._read(claims, execution_id, "CODE_SESSION")
        if prior:
            self._native_session(prior, claims)
            if prior.get("request_id", {}).get("S") != request_id:
                raise HTTPException(409, "Interpreter session already claimed")
            return self._receipt(prior)
        handle = hashlib.sha256(_json([key["event_id"]["S"], request_id])).hexdigest()
        now = int(time.time())
        requested = claims.get("execution_deadline_at", now + 900)
        if type(requested) is not int or requested > now + 900:
            raise HTTPException(403, "Interpreter authority expired")
        deadline = min(requested, claims.get("run_expires_at") or now + 900)
        if not self.identifier:
            raise HTTPException(503, "Interpreter unavailable")
        if deadline <= now:
            raise HTTPException(403, "Interpreter authority expired")
        row = {"execution_id": {"S": execution_id}, "request_id": {"S": request_id},
               "receipt_id": {"S": claims["receipt_id"]}, "session_handle": {"S": handle},
               "provider_identifier": {"S": self.identifier},
               "state": {"S": "unknown"}, "deadline_at": {"N": str(deadline)},
               "cleanup_partition": {"S": "due"}, "next_cleanup_at": {"N": str(deadline)}}
        native = claims.get("native_interpreter")
        if native is None and str(claims.get("workload_id", "")).startswith("native-test-"):
            raise HTTPException(403, "Original native Interpreter start required")
        if native is not None:
            if (not isinstance(native, dict) or native.get("kind") != "effect"
                    or native.get("tool") != "code_interpreter___start" or native.get("request_id") != request_id
                    or native.get("reservation_id") != claims["receipt_id"]
                    or type(native.get("session_deadline_at")) is not int or native["session_deadline_at"] != deadline
                    or not now < deadline <= now + 300 or native.get("cleanup_attempts") != 2
                    or type(native.get("cleanup_deadline_at")) is not int
                    or not deadline <= native["cleanup_deadline_at"] <= now + 600):
                raise HTTPException(403, "Original native Interpreter start required")
            row["native_interpreter"] = {"S": _json(native).decode()}
            self._native_session(row, claims)
            # The facade's original funded cleanup/reconciler owns this root.
            # Never enroll it in the provider's independent unbounded worker.
            del row["cleanup_partition"]
            del row["next_cleanup_at"]
        if not self._put(key, row):
            prior = self._read(claims, execution_id, "CODE_SESSION")
            if not prior:
                raise HTTPException(503, "Interpreter state unavailable")
            if prior.get("request_id", {}).get("S") != request_id:
                raise HTTPException(409, "Interpreter session already claimed")
            return self._receipt(prior)
        if revalidate() != (claims, arguments):
            raise HTTPException(403, "Interpreter authority changed")
        if self._read(claims, execution_id, "CODE_SESSION").get("cleanup_state"):
            return self._receipt(self._read(claims, execution_id, "CODE_SESSION"))
        try:
            remaining = int(deadline - time.time())
            if remaining <= 0:
                raise HTTPException(403, "Interpreter authority expired")
            response = self._provider().start_code_interpreter_session(
                codeInterpreterIdentifier=self.identifier, clientToken=handle, name="adp-" + handle,
                sessionTimeoutSeconds=remaining)
            provider_id = response["sessionId"]
            if not isinstance(provider_id, str) or not 0 < len(provider_id) <= 256:
                raise ValueError("Missing provider session")
            self._update(key, {"provider_id": {"S": provider_id}, "state": {"S": "confirmed"}},
                         "attribute_not_exists(provider_id)")
        except (BotoCoreError, ClientError, ValueError, KeyError, HTTPException, TimeoutError):
            return self._receipt(self._read(claims, execution_id, "CODE_SESSION"))
        return self._receipt(self._read(claims, execution_id, "CODE_SESSION"))

    @contextmanager
    def _mutation(self, key, request_id, operation_key=None):
        try:
            self.client.update_item(TableName=self.table, Key=key,
                UpdateExpression="SET active_mutation = :request",
                ConditionExpression=("attribute_exists(provider_id) AND attribute_not_exists(cleanup_state) "
                                     "AND attribute_not_exists(active_mutation) AND deadline_at > :now"),
                ExpressionAttributeValues={":request": {"S": request_id},
                                           ":now": {"N": str(int(time.time()))}})
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise HTTPException(503, "Interpreter state unavailable") from None
            yield False
            return
        except BotoCoreError:
            raise HTTPException(503, "Interpreter state unavailable") from None
        try:
            yield True
        finally:
            release = operation_key is None
            if operation_key is not None:
                try:
                    operation = self.client.get_item(TableName=self.table, Key=operation_key,
                        ConsistentRead=True).get("Item", {})
                    release = operation.get("state", {}).get("S") in {"pending", "confirmed", "failed"}
                except (BotoCoreError, ClientError):
                    pass
            if release:
                try:
                    self.client.update_item(TableName=self.table, Key=key,
                        UpdateExpression="REMOVE active_mutation",
                        ConditionExpression="active_mutation = :request",
                        ExpressionAttributeValues={":request": {"S": request_id}})
                except (BotoCoreError, ClientError):
                    pass

    def execute(self, arguments, claims, revalidate):
        session = self._session(arguments, claims)
        self._native_effect(session, arguments, claims, "execute")
        execution_id, request_id = arguments["execution_id"], arguments["request_id"]
        digest = hashlib.sha256(_json({"session_id": arguments["session_id"],
                                       "code": arguments["code"], "language": arguments["language"]})).hexdigest()
        key = self._key(claims, execution_id, "CODE_OP#" + request_id)
        prior = self._read(claims, execution_id, "CODE_OP#" + request_id)
        if prior:
            if prior.get("request_digest", {}).get("S") != digest:
                raise HTTPException(409, "Interpreter request ID conflict")
            return self._receipt(prior)
        if (not self.identifier or "provider_id" not in session or session.get("cleanup_state")
                or int(session["deadline_at"]["N"]) <= time.time()):
            raise HTTPException(409, "Interpreter session unavailable")
        row = {"execution_id": {"S": execution_id}, "request_id": {"S": request_id},
               "receipt_id": {"S": claims["receipt_id"]}, "session_handle": {"S": arguments["session_id"]},
               "request_digest": {"S": digest}, "state": {"S": "unknown"}}
        if not self._put(key, row):
            prior = self._read(claims, execution_id, "CODE_OP#" + request_id)
            if not prior:
                raise HTTPException(503, "Interpreter state unavailable")
            if prior.get("request_digest", {}).get("S") != digest:
                raise HTTPException(409, "Interpreter request ID conflict")
            return self._receipt(prior)
        if revalidate() != (claims, arguments):
            raise HTTPException(403, "Interpreter authority changed")
        with self._mutation(self._key(claims, execution_id, "CODE_SESSION"), request_id, key) as acquired:
            if not acquired:
                return self._receipt(self._read(claims, execution_id, "CODE_OP#" + request_id))
            if revalidate() != (claims, arguments):
                raise HTTPException(403, "Interpreter authority changed")
            if self._session(arguments, claims).get("cleanup_state"):
                return self._receipt(self._read(claims, execution_id, "CODE_OP#" + request_id))
            try:
                interpreter = CodeInterpreter(None, None, self.identifier, provider=self._provider())
                result = interpreter.safe_result(interpreter.invoke(session["provider_id"]["S"],
                    "startCommandExecution", {"command": "python -c " + shlex.quote(arguments["code"])},
                    native="native_interpreter" in session))
                provider_id = result["taskId"]
                if not isinstance(provider_id, str) or not 0 < len(provider_id) <= 256:
                    raise ValueError("Missing provider task")
                self._update(key, {"provider_task_id": {"S": provider_id}, "state": {"S": "pending"}},
                             "attribute_not_exists(provider_task_id)")
            except ProviderExecutionRefused:
                self._update(key, {"result_json": {"S": _json({"status": "failed", "error": "provider_refused"}).decode()},
                                   "state": {"S": "failed"}}, "attribute_not_exists(result_json)")
            except (BotoCoreError, ClientError, HTTPException, ValueError, KeyError, TimeoutError):
                return self._receipt(self._read(claims, execution_id, "CODE_OP#" + request_id))
            row = self._read(claims, execution_id, "CODE_OP#" + request_id)
            if row.get("state", {}).get("S") == "pending" and "native_interpreter" not in session:
                # Most executions finish in well under a second, yet a bare ``pending`` costs the caller a
                # whole extra model turn plus a status read (#8223 Dev Box tool latency: ~8 s per execute).
                # Wait a bounded moment for the task here, inside the budgets of every hop above us (Lambda
                # 28 s, façade 30 s, relay 25 s, API Gateway 29 s). Native sessions keep the explicit poll:
                # their status reads are individually admitted and accounted.
                row = self._wait_for_task(key, row, session)
            return self._receipt(row)

    def result(self, arguments, claims):
        session = self._session(arguments, claims)
        self._native_effect(session, arguments, claims, "result")
        row = self._read(claims, arguments["execution_id"], "CODE_OP#" + arguments["request_id"])
        if (not row or row.get("session_handle", {}).get("S") != arguments["session_id"]
                or row.get("provider_task_id", {}).get("S") != arguments["provider_id"]):
            raise HTTPException(404, "Interpreter operation not found")
        if row["state"]["S"] in {"confirmed", "failed"} or "provider_id" not in session:
            return self._receipt(row)
        if "native_interpreter" in session and not self._put(
                self._key(claims, arguments["execution_id"], "CODE_NATIVE_POLL#" + arguments["request_id"]),
                {"operation_id": {"S": claims["native_interpreter"]["operation_id"]}, "state": {"S": "claimed"}}):
            # Managed redelivery and a lost claim acknowledgement cannot poll
            # again. An unresolved first observation retains the original hold.
            return self._receipt(row)
        return self._receipt(self._settle_task(
            self._key(claims, arguments["execution_id"], "CODE_OP#" + arguments["request_id"]), row, session))

    def operation_get(self, arguments, claims):
        row = self._read(claims, arguments["execution_id"], "CODE_OP#" + arguments["request_id"])
        if not row:
            row = self._read(claims, arguments["execution_id"], "CODE_SESSION")
            if not row or row["request_id"]["S"] != arguments["request_id"]:
                raise HTTPException(404, "Interpreter operation not found")
        return self._receipt(row)

    def file(self, arguments, claims, revalidate):
        if not isinstance(arguments.get("path"), str) or not PATH.fullmatch(arguments["path"]):
            raise HTTPException(422, "Invalid interpreter path")
        session = self._session(arguments, claims)
        self._native_effect(session, arguments, claims, "file")
        execution_id, request_id = arguments["execution_id"], arguments["request_id"]
        digest = hashlib.sha256(_json({"session_id": arguments["session_id"],
                                       "path": arguments["path"]})).hexdigest()
        key = self._key(claims, execution_id, "CODE_OP#" + request_id)
        prior = self._read(claims, execution_id, "CODE_OP#" + request_id)
        if prior:
            if prior.get("request_digest", {}).get("S") != digest or prior.get("operation", {}).get("S") != "file":
                raise HTTPException(409, "Interpreter request ID conflict")
            return self._receipt(prior)
        if (not self.identifier or "provider_id" not in session or session.get("cleanup_state")
                or int(session["deadline_at"]["N"]) <= time.time()):
            raise HTTPException(409, "Interpreter session unavailable")
        row = {"execution_id": {"S": execution_id}, "request_id": {"S": request_id},
               "receipt_id": {"S": claims["receipt_id"]}, "session_handle": {"S": arguments["session_id"]},
               "operation": {"S": "file"}, "request_digest": {"S": digest}, "state": {"S": "unknown"}}
        if not self._put(key, row):
            prior = self._read(claims, execution_id, "CODE_OP#" + request_id)
            if not prior:
                raise HTTPException(503, "Interpreter state unavailable")
            if prior.get("request_digest", {}).get("S") != digest or prior.get("operation", {}).get("S") != "file":
                raise HTTPException(409, "Interpreter request ID conflict")
            return self._receipt(prior)
        if revalidate() != (claims, arguments):
            raise HTTPException(403, "Interpreter authority changed")
        with self._mutation(self._key(claims, execution_id, "CODE_SESSION"), request_id) as acquired:
            if not acquired:
                return self._receipt(self._read(claims, execution_id, "CODE_OP#" + request_id))
            if revalidate() != (claims, arguments):
                raise HTTPException(403, "Interpreter authority changed")
            if self._session(arguments, claims).get("cleanup_state"):
                return self._receipt(self._read(claims, execution_id, "CODE_OP#" + request_id))
            code = (
                "import base64,json,os,stat; "
                f"fd=os.open({arguments['path']!r},os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK); "
                "assert stat.S_ISREG(os.fstat(fd).st_mode), 'Not a regular file'; "
                "data=os.read(fd,8193); os.close(fd); "
                "assert len(data)<=8192, 'File exceeds 8192 bytes'; "
                "print(json.dumps({'encoding':'base64','data':base64.b64encode(data).decode()}))"
            )
            try:
                interpreter = CodeInterpreter(None, None, self.identifier, provider=self._provider())
                found = interpreter.safe_result(interpreter.invoke(session["provider_id"]["S"],
                    "executeCommand", {"command": "python -c " + shlex.quote(code)}, native="native_interpreter" in session))
                output = found.get("stdout", found.get("content"))
                if isinstance(output, list) and len(output) == 1 and output[0].get("type") == "text":
                    output = output[0].get("text")
                payload = json.loads(output) if isinstance(output, str) else found
                if (not isinstance(payload, dict) or payload.get("encoding") != "base64"
                        or not isinstance(payload.get("data"), str) or len(payload["data"]) > 10924):
                    raise ValueError("Invalid interpreter file response")
                content = base64.b64decode(payload["data"], validate=True)
                if len(content) > 8192:
                    raise ValueError("Interpreter file exceeds bound")
                result = _json({"path": arguments["path"], "data_base64": base64.b64encode(content).decode(),
                                "byte_length": len(content), "content_sha256": hashlib.sha256(content).hexdigest()})
                self._update(key, {"result_json": {"S": result.decode()}, "state": {"S": "confirmed"},
                                   "artifact_sha256": {"S": hashlib.sha256(content).hexdigest()},
                                   "artifact_length": {"N": str(len(content))}}, "attribute_not_exists(result_json)")
            except HTTPException as error:
                if error.status_code == 502:
                    try:
                        self._update(key, {"result_json": {"S": _json({"status": "failed", "error": "provider_refused"}).decode()},
                                           "state": {"S": "failed"}}, "attribute_not_exists(result_json)")
                    except HTTPException:
                        pass
                return self._receipt(self._read(claims, execution_id, "CODE_OP#" + request_id))
            except (BotoCoreError, ClientError, ValueError, KeyError, TypeError, TimeoutError):
                return self._receipt(self._read(claims, execution_id, "CODE_OP#" + request_id))
            return self._receipt(self._read(claims, execution_id, "CODE_OP#" + request_id))

    def artifact_read(self, arguments, claims):
        row = self._read(claims, arguments["execution_id"], "CODE_OP#" + arguments["artifact_id"])
        if not row or row["state"]["S"] != "confirmed":
            raise HTTPException(404, "Interpreter artifact not found")
        if "artifact_sha256" in row:
            content = base64.b64decode(json.loads(row["result_json"]["S"])["data_base64"], validate=True)
            content_type = "application/octet-stream"
        else:
            content = row["result_json"]["S"].encode()
            content_type = "application/json"
        offset = arguments.get("offset", 0)
        if offset > len(content):
            raise HTTPException(416, "Invalid artifact offset")
        chunk = content[offset:offset + 8192]
        return {"artifact_id": arguments["artifact_id"], "content_type": content_type,
                "offset": offset, "length": len(chunk), "total_length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "data_base64": base64.b64encode(chunk).decode()}

    def _stop_fenced(self, key, session):
        pending = {**self._receipt(session), "status": "pending", "cleanup_status": "pending",
                   "termination_confirmed": False}
        if "active_mutation" in session and int(session["deadline_at"]["N"]) > time.time():
            return pending
        if "provider_id" not in session:
            return pending
        provider_identifier = session.get("provider_identifier", {}).get("S", self.identifier)
        provider_id = session["provider_id"]["S"]
        try:
            observed = self._provider().get_code_interpreter_session(
                codeInterpreterIdentifier=provider_identifier, sessionId=provider_id)
        except (ClientError, BotoCoreError, TimeoutError):
            return pending
        if observed.get("status") != "TERMINATED":
            try:
                self._provider().stop_code_interpreter_session(
                    codeInterpreterIdentifier=provider_identifier, sessionId=provider_id,
                    clientToken=hashlib.sha256((session["session_handle"]["S"] + ":stop").encode()).hexdigest())
            except (ClientError, BotoCoreError, TimeoutError):
                pass
            try:
                observed = self._provider().get_code_interpreter_session(
                    codeInterpreterIdentifier=provider_identifier, sessionId=provider_id)
            except (ClientError, BotoCoreError, TimeoutError):
                return pending
            if observed.get("status") != "TERMINATED":
                return pending
        if session.get("cleanup_state", {}).get("S") == "closed":
            return {**self._receipt(session), "status": "confirmed"}
        try:
            self.client.update_item(TableName=self.table, Key=key,
                UpdateExpression="SET cleanup_state = :closed REMOVE cleanup_partition, next_cleanup_at",
                ConditionExpression="cleanup_state = :closing AND provider_id = :provider",
                ExpressionAttributeValues={":closed": {"S": "closed"}, ":closing": {"S": "closing"},
                                           ":provider": session["provider_id"]})
        except (BotoCoreError, ClientError):
            return pending
        session = self.client.get_item(TableName=self.table, Key=key, ConsistentRead=True)["Item"]
        return {**self._receipt(session), "status": "confirmed"}

    def close(self, arguments, claims, revalidate, expected_arguments=None):
        execution_id = arguments["execution_id"]
        session = self._session(arguments, claims)
        if "native_interpreter" in session:
            binding = claims["native_interpreter"]
            if binding.get("kind") != "cleanup":
                raise HTTPException(403, "Original native cleanup required")
            if session.get("cleanup_state", {}).get("S") == "closed":
                return {**self._receipt(session), "status": "confirmed"}
            if not self._put(self._key(claims, execution_id, "CODE_NATIVE_CLEANUP#" + str(binding["attempt"])),
                             {"operation_id": {"S": binding["operation_id"]}, "state": {"S": "claimed"}}):
                return {**self._receipt(session), "status": "pending", "termination_confirmed": False}
        key = self._key(claims, execution_id, "CODE_SESSION")
        if session.get("cleanup_state", {}).get("S") == "closed":
            return self._stop_fenced(key, session)
        if revalidate() != (claims, expected_arguments or arguments):
            raise HTTPException(403, "Interpreter authority changed")
        if "cleanup_state" not in session:
            try:
                native = "native_interpreter" in session
                self.client.update_item(TableName=self.table, Key=key,
                    UpdateExpression=("SET cleanup_state = :closing" if native else
                                      "SET cleanup_state = :closing, cleanup_partition = :due, next_cleanup_at = :now"),
                    ConditionExpression="attribute_not_exists(cleanup_state)",
                    ExpressionAttributeValues={":closing": {"S": "closing"}, **({} if native else {
                        ":due": {"S": "due"}, ":now": {"N": str(int(time.time()))}})})
            except (BotoCoreError, ClientError):
                session = self._read(claims, execution_id, "CODE_SESSION")
                if "cleanup_state" not in session:
                    raise HTTPException(503, "Interpreter close outcome unavailable") from None
        session = self._read(claims, execution_id, "CODE_SESSION")
        if "cleanup_partition" not in session and "native_interpreter" not in session:
            try:
                self.client.update_item(TableName=self.table, Key=key,
                    UpdateExpression="SET cleanup_partition = :due, next_cleanup_at = :now",
                    ConditionExpression="cleanup_state = :closing AND attribute_not_exists(cleanup_partition)",
                    ExpressionAttributeValues={":due": {"S": "due"}, ":now": {"N": str(int(time.time()))},
                                               ":closing": {"S": "closing"}})
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise HTTPException(503, "Interpreter close outcome unavailable") from None
            except BotoCoreError:
                raise HTTPException(503, "Interpreter close outcome unavailable") from None
            session = self._read(claims, execution_id, "CODE_SESSION")
        if revalidate() != (claims, expected_arguments or arguments):
            raise HTTPException(403, "Interpreter authority changed")
        return self._stop_fenced(key, session)

    def execution_close(self, arguments, claims, revalidate):
        session = self._read(claims, arguments["execution_id"], "CODE_SESSION")
        if not session:
            raise HTTPException(404, "Interpreter session not found")
        return self.close({**arguments, "session_id": session["session_handle"]["S"]}, claims,
                          revalidate, expected_arguments=arguments)


class GatewayInterpreterReconciler:
    def __init__(self, interpreter=None, clock=None):
        self.interpreter = interpreter or GatewayInterpreter()
        self.clock = clock or time.time

    def run_once(self):
        now = int(self.clock())
        client = self.interpreter.client
        cursor = None
        while True:
            options = {"TableName": self.interpreter.table, "IndexName": "cleanup-due",
                "KeyConditionExpression": "cleanup_partition = :due AND next_cleanup_at <= :now",
                "ExpressionAttributeValues": {":due": {"S": "due"}, ":now": {"N": str(now)}},
                "Limit": 25}
            if cursor:
                options["ExclusiveStartKey"] = cursor
            try:
                page = client.query(**options)
            except (BotoCoreError, ClientError):
                raise HTTPException(503, "Interpreter cleanup unavailable") from None
            for candidate in page["Items"]:
                if candidate["arrived_at"]["S"] != "CODE_SESSION" or "native_interpreter" in candidate:
                    continue
                key = {field: candidate[field] for field in ("event_id", "arrived_at")}
                try:
                    client.update_item(TableName=self.interpreter.table, Key=key,
                        UpdateExpression="SET cleanup_state = :closing, next_cleanup_at = :retry",
                        ConditionExpression=("cleanup_partition = :due AND next_cleanup_at <= :now "
                                             "AND attribute_not_exists(native_interpreter) "
                                             "AND (attribute_not_exists(cleanup_state) OR cleanup_state = :closing)"),
                        ExpressionAttributeValues={":due": {"S": "due"}, ":now": {"N": str(now)},
                            ":closing": {"S": "closing"}, ":retry": {"N": str(now + 60)}})
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                        continue
                    raise HTTPException(503, "Interpreter cleanup unavailable") from None
                except BotoCoreError:
                    raise HTTPException(503, "Interpreter cleanup unavailable") from None
                session = client.get_item(TableName=self.interpreter.table, Key=key, ConsistentRead=True).get("Item")
                if not session or "native_interpreter" in session:
                    continue
                if "provider_id" not in session and int(session["deadline_at"]["N"]) + 120 <= now:
                    try:
                        client.update_item(TableName=self.interpreter.table, Key=key,
                            UpdateExpression="REMOVE cleanup_partition, next_cleanup_at",
                            ConditionExpression="cleanup_state = :closing AND attribute_not_exists(provider_id)",
                            ExpressionAttributeValues={":closing": {"S": "closing"}})
                    except ClientError as error:
                        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                            raise HTTPException(503, "Interpreter cleanup unavailable") from None
                    continue
                self.interpreter._stop_fenced(key, session)
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                return
