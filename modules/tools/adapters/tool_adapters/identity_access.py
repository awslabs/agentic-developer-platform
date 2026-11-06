"""Retrieve only the current admitted user's transient access from AgentCore Identity."""

import asyncio
import time

import boto3
from adp_tools.adapter_contracts import Admission
from botocore.config import Config
from fastapi import HTTPException

from tool_adapters.legacy_identity import legacy_identity

RESOURCE = "https://mcp.atlassian.com/v2/mcp"


def _identity_access(admission: Admission, limit_ms: int) -> str:
    remaining = (limit_ms - int(time.time() * 1000)) / 1000
    if remaining <= 0:
        raise ValueError("admission expired")
    identity = boto3.client(
        "bedrock-agentcore",
        config=Config(
            connect_timeout=min(2, remaining),
            read_timeout=min(5, remaining),
            retries={"total_max_attempts": 1},
        ),
    )
    workload = identity.get_workload_access_token_for_user_id(
        workloadName=admission.identity.workload,
        userId=admission.identity.canonical_user_id,
    )
    if (
        not isinstance(workload, dict)
        or not isinstance(workload.get("workloadAccessToken"), str)
        or not workload["workloadAccessToken"]
        or int(time.time() * 1000) >= limit_ms
    ):
        raise ValueError("Identity grant unavailable")
    response = identity.get_resource_oauth2_token(
        workloadIdentityToken=workload["workloadAccessToken"],
        resourceCredentialProviderName=admission.identity.provider,
        scopes=list(admission.context.scopes),
        **({"resources": [RESOURCE]} if admission.connector == "atlassian" else {}),
        oauth2Flow="USER_FEDERATION",
    )
    if not isinstance(response, dict):
        raise ValueError("Identity grant unavailable")
    token = response.get("accessToken")
    if (
        response.get("sessionStatus") in {"IN_PROGRESS", "FAILED"}
        or not isinstance(token, str)
        or not token
        or len(token) > 16384
        or any(char.isspace() for char in token)
        or int(time.time() * 1000) >= limit_ms
    ):
        raise ValueError("Identity grant unavailable")
    return token


async def retrieve_access_token(admission: Admission, configuration) -> str:
    settings = configuration.observation
    now = int(time.time() * 1000)
    if (
        not isinstance(admission, Admission)
        or configuration.mode != "observation"
        or settings is None
        or admission.purpose not in {"connection_observe", "policy_observe", "tool_call", "reconcile"}
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
                or not admission.context.legacy.provider_account_id
                or admission.purpose != "tool_call"
            )
        )
        or admission.identity.canonical_user_id != admission.context.canonical_user_id
        or (
            not admission.context.scopes
            and not (
                admission.connector == "github"
                and admission.context.legacy is not None
                and admission.context.legacy.app_id is not None
                and admission.context.legacy.resources_sha256 is not None
            )
        )
        or admission.issued_at_ms > now
        or min(admission.expires_at_ms, admission.deadline_at_ms) <= now
        or admission.purpose == "tool_call"
        and (
            not admission.context.connection_id
            or not admission.context.binding_revision
            or (admission.connector in {"atlassian", "jira"} and not admission.context.site_id)
        )
        or admission.purpose == "connection_observe"
        and (admission.context.connection_id is not None or admission.context.site_id is not None)
    ):
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    limit_ms = min(admission.expires_at_ms, admission.deadline_at_ms)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_identity_access, admission, limit_ms),
            timeout=(limit_ms - now) / 1000,
        )
    except Exception:
        raise HTTPException(503, detail={"error": "identity_unavailable"}) from None
