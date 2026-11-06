"""Private, IAM-authorized transport to the isolated FinOps browser executor."""

import asyncio
import json
import re
from datetime import UTC, datetime

from l5_services.workers.execution import AuthorityRevoked


class PrivateFinOpsBridge:
    def __init__(self, client, function_arn, region):
        if (not isinstance(function_arn, str)
                or not re.fullmatch(
                    rf"arn:aws[a-z-]*:lambda:{re.escape(region)}:[0-9]{{12}}:function:[A-Za-z0-9_-]+-l5-finops-executor",
                    function_arn)):
            raise ValueError("select the private FinOps executor function")
        self.client = client
        self.function_arn = function_arn

    async def _invoke(self, action, authority, *, intent=None, native_job_ref=None):
        if (not isinstance(authority, dict) or not isinstance(authority.get("provider_identity"), dict)
                or not isinstance(authority.get("operation_id"), str)
                or not isinstance(authority.get("attempt_id"), str)
                or not isinstance(authority.get("lease_fence"), int)
                or any(not authority.get(field) for field in (
                    "access_key_id", "secret_access_key", "session_token"))):
            raise AuthorityRevoked("private executor lacks selected work authority")
        try:
            expiry = datetime.fromisoformat(authority["expires_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise AuthorityRevoked("private executor authority expiry unavailable") from error
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise AuthorityRevoked("private executor authority expired")
        scoped = {key: value for key, value in authority.items()
                  if key not in {"access_key_id", "secret_access_key", "session_token"}}
        request = {"schema_version": 1, "action": action, "authority": scoped,
                   "intent": intent, "native_job_ref": native_job_ref}
        result = await asyncio.to_thread(self.client.invoke, FunctionName=self.function_arn,
                                         InvocationType="RequestResponse",
                                         Payload=json.dumps(request, sort_keys=True).encode())
        if result.get("StatusCode") != 200 or result.get("FunctionError"):
            raise ConnectionError("private FinOps executor action outcome is uncertain")
        content = await asyncio.to_thread(result["Payload"].read, 2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ConnectionError("private FinOps executor response is too large to reconcile")
        try:
            answer = json.loads(content)
        except (ValueError, TypeError) as error:
            raise ConnectionError("private FinOps executor response is invalid") from error
        if (not isinstance(answer, dict) or set(answer) not in (
                {"action", "operation_id", "attempt_id", "result"},
                {"action", "operation_id", "attempt_id", "error"})
                or answer["action"] != action
                or answer["operation_id"] != authority["operation_id"]
                or answer["attempt_id"] != authority["attempt_id"]):
            raise ConnectionError("private FinOps executor response belongs to another work item")
        if "error" in answer:
            if answer["error"] != "authority_revoked":
                raise ConnectionError("private FinOps executor refusal is not qualified")
            raise AuthorityRevoked("private FinOps executor current work revoked")
        return answer["result"]

    async def submit(self, *, intent, authority):
        if intent["run_id"] != authority["operation_id"]:
            raise PermissionError("FinOps submission differs from selected work")
        return await self._invoke("submit", authority, intent=intent)

    async def read_agent(self, *, native_job_ref, authority):
        if authority.get("work_kind") != "setup":
            raise PermissionError("FinOps adoption requires current setup work")
        return await self._invoke("read_agent", authority, native_job_ref=native_job_ref)

    async def read_submit(self, *, intent, authority):
        if intent["run_id"] != authority["operation_id"]:
            raise PermissionError("FinOps recovery differs from selected work")
        return await self._invoke("read_submit", authority, intent=intent)

    async def observe(self, *, native_job_ref, authority):
        return await self._invoke("observe", authority, native_job_ref=native_job_ref)

    async def read_answer(self, *, native_job_ref, authority):
        return await self._invoke("read_answer", authority, native_job_ref=native_job_ref)

    async def read_native_cost(self, *, native_job_ref, authority):
        return await self._invoke("read_native_cost", authority, native_job_ref=native_job_ref)

    async def stop(self, *, native_job_ref, authority):
        return await self._invoke("stop", authority, native_job_ref=native_job_ref)
