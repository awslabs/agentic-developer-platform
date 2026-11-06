"""Private FinOps Lambda host; worker payloads never confer work authority."""

import asyncio
import json
import os
import re
from datetime import UTC, datetime

import boto3

from l5_services.adapters.finops.console_executor import (
    ConsoleFinOpsExecutor, FinOpsExecutorEvidence, PlaywrightFinOpsBrowser,
)
from l5_services.models.run import Run
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.setup import SetupIntent, SetupWork
from l5_services.models.work import Work
from l5_services.runtime_support import gateway_transport as _gateway_transport, sessions as _sessions


async def verify_executor_work(authority, sessions, transport):
    if isinstance(authority, dict) and authority.get("work_kind") == "setup":
        return await verify_executor_setup(authority, sessions, transport)
    if (not isinstance(authority, dict) or any(not authority.get(field) for field in (
            "tenant_id", "product_id", "project_id", "operation_id", "attempt_id", "lease_owner",
            "delegation_id", "connection_ref", "provider_identity", "work_kind"))
            or any(type(authority.get(field)) is not int or authority[field] < 1 for field in (
                "lease_fence", "run_revision", "work_generation", "delegation_revision"))):
        raise PermissionError("private executor work identity unavailable")
    async with sessions() as session:
        work = await session.get(Work, (authority["tenant_id"], authority["attempt_id"]))
        run = await session.get(Run, (authority["tenant_id"], authority["operation_id"]))
        now = datetime.now(UTC)
        agent = (run.admission or {}).get("native_agent_ref") if run is not None else None
        if (not isinstance(agent, dict) or agent.get("record_type") != "finops_agent"
                or agent.get("tenant_id") != authority["tenant_id"]
                or agent.get("project_id") != authority["project_id"]
                or not isinstance(agent.get("record_id"), str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", agent["record_id"])):
            raise PermissionError("private executor native agent identity unavailable")
        if (work is None or run is None or run.service_id != "finops"
                or run.revision != authority["run_revision"] or work.run_id != run.run_id
                or work.generation != authority["work_generation"] or work.fence != authority["lease_fence"]
                or work.kind != authority["work_kind"]
                or work.lease_owner != authority["lease_owner"] or work.completed_at or work.blocked_at
                or work.lease_until is None or work.lease_until.replace(
                    tzinfo=work.lease_until.tzinfo or UTC) <= now
                or (run.tenant_id, run.product_id, run.project_id) != (
                    authority["tenant_id"], authority["product_id"], authority["project_id"])
                or run.admission.get("delegation_id") != authority["delegation_id"]
                or run.admission.get("delegation_version") != authority["delegation_revision"]
                or run.admission.get("provider_identity") != authority["provider_identity"]
                or {**run.admission.get("connection_ref", {}), "record_type": "credential"}
                != authority["connection_ref"]):
            raise PermissionError("private executor current scope unavailable")

    request = {"delegation_id": authority["delegation_id"],
               "delegation_revision": authority["delegation_revision"],
               "operation_id": authority["operation_id"], "attempt_id": authority["attempt_id"],
               "lease_owner": authority["lease_owner"], "lease_fence": authority["lease_fence"],
               "connection_ref": authority["connection_ref"]}
    response = await transport.post("/internal/v1/l5/credential-session", json=request)
    if response.status_code in {401, 403, 404, 409, 410}:
        raise PermissionError("private executor broker authority unavailable")
    if response.status_code != 200:
        raise ConnectionError("private executor broker response unavailable")
    verified = response.json()
    try:
        expires = datetime.fromisoformat(verified["expires_at"])
    except (TypeError, KeyError, ValueError) as error:
        raise PermissionError("private executor broker expiry unavailable") from error
    if (expires.tzinfo is None or expires <= datetime.now(UTC)
            or verified.get("operation_id") != authority["operation_id"]
            or verified.get("attempt_id") != authority["attempt_id"]
            or verified.get("delegation_revision") != authority["delegation_revision"]
            or any((verified.get("provider_identity") or {}).get(field) != authority["provider_identity"].get(field)
                   for field in ("account_id", "role_arn", "region"))
            or any(not verified.get(field) for field in (
                "access_key_id", "secret_access_key", "session_token"))):
        raise PermissionError("private executor broker returned another scope")
    return {**verified, "provider_identity": authority["provider_identity"],
            "lease_fence": authority["lease_fence"], "native_agent_id": agent["record_id"]}


async def verify_executor_setup(authority, sessions, transport):
    if (any(not authority.get(field) for field in ("tenant_id", "product_id", "project_id", "operation_id",
            "attempt_id", "lease_owner", "delegation_id", "provider_identity", "connection_ref"))
            or any(type(authority.get(field)) is not int or authority[field] < 1 for field in
                   ("lease_fence", "work_generation", "binding_generation", "delegation_revision"))):
        raise PermissionError("private executor setup identity unavailable")
    async with sessions() as session:
        intent = await session.get(SetupIntent, (authority["tenant_id"], authority["operation_id"]))
        work = await session.get(SetupWork, (authority["tenant_id"], authority["attempt_id"]))
        if (intent is None or work is None or intent.service_id != "finops" or intent.request.get("action") != "link"
                or (intent.product_id, intent.project_id) != (authority["product_id"], authority["project_id"])
                or work.operation_id != intent.operation_id or work.generation != authority["work_generation"]
                or work.fence != authority["lease_fence"] or work.lease_owner != authority["lease_owner"]
                or work.completed_at or work.blocked_at or work.lease_until is None
                or work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC) <= datetime.now(UTC)
                or intent.binding_generation != authority["binding_generation"]
                or intent.authorization.get("delegation_id") != authority["delegation_id"]
                or intent.authorization.get("delegation_revision") != authority["delegation_revision"]):
            raise PermissionError("private executor current setup unavailable")
        configuration = await session.get(Configuration, (intent.tenant_id, intent.configuration_id))
        version = await session.get(ConfigurationVersion, (intent.tenant_id, intent.configuration_id, intent.request["configuration_revision"]))
        reference = intent.request["native_resource_ref"]
        if (configuration is None or version is None or configuration.current_revision != version.revision
                or configuration.service_id != "finops" or version.settings.get("enabled") is not True
                or {**version.settings.get("connection_ref", {}), "record_type": "credential"} != authority["connection_ref"]
                or reference.get("record_id") != (version.settings.get("finops") or {}).get("native_agent_ref")
                or (reference.get("tenant_id"), reference.get("project_id"), reference.get("source_connection_id")) !=
                    (intent.tenant_id, intent.project_id, authority["connection_ref"].get("source_connection_id"))
                or reference.get("record_type") != "finops_agent"
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", reference.get("record_id", ""))):
            raise PermissionError("private executor selected setup scope unavailable")
        request = {field: authority[field] for field in ("delegation_id", "delegation_revision", "operation_id",
                   "attempt_id", "lease_owner", "lease_fence", "connection_ref")}
        response = await transport.post("/internal/v1/l5/credential-session", json=request)
        if response.status_code != 200:
            raise PermissionError("private executor setup broker authority unavailable")
        result = response.json()
        verified = result.get("setup_verified") or {}
        try:
            expires = datetime.fromisoformat(result["expires_at"])
        except (KeyError, TypeError, ValueError):
            raise PermissionError("private executor setup expiry unavailable") from None
        if (expires.tzinfo is None or expires <= datetime.now(UTC)
                or result.get("operation_id") != intent.operation_id or result.get("attempt_id") != work.work_id
                or result.get("delegation_revision") != authority["delegation_revision"]
                or any((result.get("provider_identity") or {}).get(field) != authority["provider_identity"].get(field)
                       for field in ("account_id", "role_arn", "region"))
                or verified.get("scope") != intent.scope or verified.get("configuration_id") != intent.configuration_id
                or verified.get("configuration_revision") != version.revision
                or verified.get("authorization") != intent.authorization
                or verified.get("finops") != version.settings.get("finops")
                or any(not result.get(field) for field in ("access_key_id", "secret_access_key", "session_token"))):
            raise PermissionError("private executor setup broker returned another scope")
        return {**result, "provider_identity": authority["provider_identity"],
                "lease_fence": authority["lease_fence"], "native_agent_id": reference["record_id"],
                "native_resource_ref": reference}


def build_executor(*, sessions=None, transport=None, browser=None, evidence=None):
    if os.environ.get("L5_SERVICE_ID") != "finops" or not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        raise RuntimeError("FinOps executor requires installed private Lambda authority")
    try:
        surface = json.loads(os.environ["L5_FINOPS_SURFACE"])
    except (KeyError, ValueError) as error:
        raise ValueError("qualified FinOps surface unavailable") from error
    region = os.environ["AWS_REGION"]
    bucket = os.environ["L5_ARCHIVE_BUCKET"]
    sessions = sessions or _sessions()
    transport = transport or _gateway_transport()

    async def verify(authority):
        return await verify_executor_work(authority, sessions, transport)

    return ConsoleFinOpsExecutor(browser or PlaywrightFinOpsBrowser(region), verify,
                                 evidence or FinOpsExecutorEvidence(bucket, boto3.client("s3", region_name=region)),
                                 surface)


async def dispatch(event, executor):
    if (not isinstance(event, dict) or set(event) != {
            "schema_version", "action", "authority", "intent", "native_job_ref"}
            or event["schema_version"] != 1
            or event["action"] not in {"submit", "read_submit", "observe", "read_answer", "read_native_cost", "stop", "read_agent"}
            or not isinstance(event["authority"], dict)):
        raise ValueError("private FinOps action is invalid")
    action = event["action"]
    permitted = {"submit": {"submit"}, "read_submit": {"submit", "reconcile"},
                 "observe": {"poll", "reconcile", "cancel"}, "read_answer": {"collect"},
                 "read_native_cost": {"billing"}, "stop": {"cancel", "poll", "reconcile"}, "read_agent": {"setup"}}
    if event["authority"].get("work_kind") not in permitted[action]:
        raise PermissionError("private FinOps action differs from current work")
    if action in {"submit", "read_submit"}:
        if event["native_job_ref"] is not None or not isinstance(event["intent"], dict):
            raise ValueError("private FinOps submission payload is invalid")
        arguments = {"intent": event["intent"]}
    else:
        if event["intent"] is not None or not isinstance(event["native_job_ref"], dict):
            raise ValueError("private FinOps readback payload is invalid")
        arguments = {"native_job_ref": event["native_job_ref"]}
    result = await getattr(executor, action)(**arguments, authority=event["authority"])
    return {"action": action, "operation_id": event["authority"]["operation_id"],
            "attempt_id": event["authority"]["attempt_id"], "result": result}


def lambda_handler(event, context):
    try:
        return asyncio.run(dispatch(event, build_executor()))
    except PermissionError:
        if not isinstance(event, dict) or not isinstance(event.get("authority"), dict):
            raise
        authority = event["authority"]
        return {"action": event.get("action"), "operation_id": authority.get("operation_id"),
                "attempt_id": authority.get("attempt_id"), "error": "authority_revoked"}
