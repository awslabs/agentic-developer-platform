"""Consume one core dispatch receipt before any adapter provider effect."""

import asyncio
import os
import time

import boto3
import httpx
from adp_tools.adapter_contracts import Admission, DispatchReceipt, DispatchRequest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from fastapi import HTTPException

from tool_adapters.legacy_identity import legacy_identity


def _sign_dispatch(url, payload):
    credentials = boto3.Session().get_credentials()
    region = os.environ.get("AWS_REGION", "")
    if not credentials or not region:
        raise ValueError("missing service credentials")
    request = AWSRequest(method="POST", url=url, data=payload, headers={"content-type": "application/json"})
    SigV4Auth(credentials.get_frozen_credentials(), "execute-api", region).add_auth(request)
    return dict(request.headers)


async def consume_dispatch(admission: Admission, configuration):
    settings = configuration.observation
    now = int(time.time() * 1000)
    if (
        configuration.mode != "observation"
        or settings is None
        or admission.purpose != "tool_call"
        or admission.connector not in {"atlassian", "github", "jira", "slack"}
        or admission.binding.release != configuration.release
        or (
            admission.connector == "atlassian"
            and (
                admission.identity.workload != settings.identity_workload
                or admission.identity.provider != settings.identity_provider
                or admission.context.legacy is not None
            )
        )
        or (
            admission.connector != "atlassian"
            and (
                settings.legacy_identity_workload is None
                or (admission.identity.workload, admission.identity.provider)
                != legacy_identity(admission.connector, settings)
                or admission.context.legacy is None
            )
        )
        or admission.context.connection_id is None
        or admission.context.binding_revision is None
        or admission.expires_at_ms <= now
        or admission.deadline_at_ms <= now
    ):
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    body = DispatchRequest(
        schema_version="1.0",
        admission_id=admission.admission_id,
        binding=admission.binding,
        binding_revision=admission.context.binding_revision,
    )
    url = settings.core_authorize_url.removesuffix("/authorize") + "/dispatch"
    try:
        payload = body.model_dump_json().encode()
        headers = await asyncio.to_thread(_sign_dispatch, url, payload)
        if int(time.time() * 1000) >= min(admission.expires_at_ms, admission.deadline_at_ms):
            raise ValueError("expired admission")
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.post(url, content=payload, headers=headers)
        if response.status_code != 200 or len(response.content) > 16384:
            raise ValueError("core refused dispatch")
        receipt = DispatchReceipt.model_validate(response.json())
        if (
            receipt.admission_id != admission.admission_id
            or receipt.binding != admission.binding
            or receipt.binding_revision != admission.context.binding_revision
            or receipt.deadline_at_ms != admission.deadline_at_ms
            or receipt.consumed_at_ms >= min(admission.expires_at_ms, admission.deadline_at_ms)
            or int(time.time() * 1000) >= admission.deadline_at_ms
        ):
            raise ValueError("core dispatch changed")
        return receipt
    except Exception:
        raise HTTPException(503, detail={"error": "authority_unavailable"}) from None
