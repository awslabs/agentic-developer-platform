"""Bounded private invocation of an L5-authorized isolated control host."""

import asyncio
import json
import re
from hashlib import sha256
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from l5_services.workers.execution import AuthorityRevoked


def _contains_credentials(value):
    if isinstance(value, dict):
        return (bool({"access_key_id", "secret_access_key", "session_token",
                      "aws_access_key_id", "aws_secret_access_key", "aws_session_token"} & value.keys())
                or any(_contains_credentials(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return any(_contains_credentials(item) for item in value)
    return False


class TransformControlClient:
    def __init__(self, client, *, function_arn):
        if (not isinstance(function_arn, str) or not re.fullmatch(
                r"arn:aws[a-z-]*:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]{1,64}", function_arn)):
            raise ValueError("Private Transform control function is not pinned")
        config = getattr(getattr(client, "meta", None), "config", None)
        retries = getattr(config, "retries", None)
        if (not isinstance(retries, dict) or retries.get("total_max_attempts") != 1
                or not isinstance(getattr(config, "read_timeout", None), (int, float))
                or not 0 < config.read_timeout <= 5
                or not isinstance(getattr(config, "connect_timeout", None), (int, float))
                or not 0 < config.connect_timeout <= 3):
            raise ValueError("Private Transform control must disable SDK invocation retries")
        self.client = client
        self.function_arn = function_arn

    async def _call(self, operation, intent, authority, *, manifest=None, native_job_ref=None):
        if operation not in {"launch", "read", "stop", "collect"}:
            raise ValueError("Unsupported Transform control operation")
        if (not isinstance(intent, dict) or not isinstance(getattr(authority, "scope", None), dict)
                or not isinstance(getattr(authority, "source_request", None), dict)
                or not callable(getattr(authority, "assert_current", None))):
            raise AuthorityRevoked("Transform control requires current L5 work")
        request = authority.source_request
        scope = authority.scope
        if (not isinstance(request.get("attempt_id"), str) or not request["attempt_id"]
                or request.get("operation_id") != intent.get("run_id")
                or scope.get("tenant_id") != intent.get("tenant_id")
                or scope.get("product_id") != intent.get("product_id")
                or scope.get("project_id") != intent.get("project_id")
                or not isinstance(request.get("delegation_id"), str) or not request["delegation_id"]
                or type(request.get("delegation_revision")) is not int or request["delegation_revision"] < 1
                or not isinstance(request.get("lease_owner"), str) or not request["lease_owner"]
                or type(request.get("lease_fence")) is not int or request["lease_fence"] < 1):
            raise AuthorityRevoked("Transform control work identity changed")
        authority.assert_current()
        if operation == "launch":
            if (not isinstance(manifest, dict) or manifest.get("run_id") != intent["run_id"]
                    or manifest.get("scope") != scope or not isinstance(intent.get("provider_job"), dict)):
                raise ValueError("Transform launch lacks sealed source and finite provider job")
        if operation == "stop" and (not isinstance(native_job_ref, dict)
                                     or native_job_ref.get("executor_name") != intent.get("executor_name")
                                     or not native_job_ref.get("executor_uid")):
            raise AuthorityRevoked("Transform stop lacks committed Pod identity")
        if _contains_credentials((intent, request, scope, manifest, native_job_ref)):
            raise AuthorityRevoked("Transform control request contains provider credentials")
        nonce = str(uuid4())
        envelope = {"schema_version": "1.0", "kind": "l5-transform-control-v1", "operation": operation,
                    "request_id": nonce, "intent": intent, "scope": scope, "work": request,
                    "expires_at": authority.expires_at.isoformat(),
                    "native_job_ref": native_job_ref}
        if manifest is not None:
            envelope["manifest_sha256"] = sha256(json.dumps(
                manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            envelope["snapshot_ids"] = manifest.get("source_snapshot_ids")
        raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > 32768:
            raise ValueError("Transform control request exceeds bound")

        def invoke():
            return self.client.invoke(FunctionName=self.function_arn, InvocationType="RequestResponse", Payload=raw)

        try:
            reply = await asyncio.wait_for(asyncio.to_thread(invoke), timeout=5)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"AccessDeniedException", "ResourceNotFoundException"}:
                raise AuthorityRevoked("Private Transform control unavailable") from None
            raise ConnectionError("Private Transform control outcome unknown") from error
        except (BotoCoreError, OSError, TimeoutError) as error:
            raise ConnectionError("Private Transform control outcome unknown") from error
        try:
            authority.assert_current()
        except AuthorityRevoked as error:
            raise ConnectionError("Private Transform control outcome unknown") from error
        if (not isinstance(reply, dict) or reply.get("StatusCode") != 200
                or reply.get("FunctionError") or not hasattr(reply.get("Payload"), "read")):
            raise ConnectionError("Private Transform control outcome unknown")
        try:
            content = await asyncio.wait_for(asyncio.to_thread(reply["Payload"].read, 65537), timeout=2)
        except (OSError, TimeoutError) as error:
            raise ConnectionError("Private Transform control outcome unknown") from error
        if len(content) > 65536:
            raise ConnectionError("Private Transform control response exceeds bound")
        try:
            result = json.loads(content)
        except (TypeError, ValueError) as error:
            raise ConnectionError("Private Transform control response unavailable") from error
        if (not isinstance(result, dict) or any(result.get(key) != value for key, value in {
                "schema_version": "1.0", "request_id": nonce, "operation": operation,
                "run_id": intent["run_id"], "executor_name": intent["executor_name"],
                "attempt_token": intent["attempt_token"], "work_id": request["attempt_id"],
                "lease_fence": request["lease_fence"]}.items())
                or "result" not in result
                or result["result"] is not None and not isinstance(result["result"], dict)
                or _contains_credentials(result)):
            raise ConnectionError("Private Transform control response differs from work")
        try:
            authority.assert_current()
        except AuthorityRevoked as error:
            raise ConnectionError("Private Transform control outcome unknown") from error
        return result["result"]

    async def launch_provider(self, *, intent, manifest, authority):
        return await self._call("launch", intent, authority, manifest=manifest)

    async def read_provider(self, *, intent, authority):
        return await self._call("read", intent, authority)

    async def request_stop(self, *, intent, native_job_ref, authority):
        return await self._call("stop", intent, authority, native_job_ref=native_job_ref)

    async def collect_provider(self, *, intent, authority):
        return await self._call("collect", intent, authority)
