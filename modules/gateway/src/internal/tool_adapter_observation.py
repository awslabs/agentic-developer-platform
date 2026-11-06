"""Sign owner-bound completed attempts for the separate adapter; no OAuth token leaves Identity."""

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.shared.config import get_settings


class ObservedSite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_id: str = Field(min_length=1, max_length=512)
    scopes: tuple[str, ...]


class ConnectionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    purpose: str
    binding: dict
    attempt_id: str
    config_revision: str
    provider_user_id: str = Field(min_length=1, max_length=512)
    sites: tuple[ObservedSite, ...]
    granted_scopes: tuple[str, ...]
    observed_at_ms: int


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def observation_settings():
    url = os.environ.get("ADP_TOOL_ADAPTER_OBSERVE_URL", "")
    key = os.environ.get("ADP_TOOL_ADAPTER_ASSERTION_KEY_ARN", "")
    role = os.environ.get("ADP_TOOL_ADAPTER_CORE_ROLE_ARN", "")
    release = os.environ.get("ADP_TOOL_ADAPTER_RELEASE", "")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or re.fullmatch(r"(?:/[A-Za-z0-9_-]+)?/internal/v1/tool-adapters/observe", parsed.path) is None
        or not re.fullmatch(r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[0-9a-f-]+", key)
        or not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[a-zA-Z0-9/_+=,.@-]+", role)
    ):
        raise HTTPException(503, detail={"error": "adapter_unavailable"})
    try:
        binding_release = json.loads(release)
        if (
            type(binding_release) is not dict
            or set(binding_release) != {"contract_version", "source_revision", "image_digest", "group", "group_generation", "backend_generation"}
            or binding_release["contract_version"] != "1.0"
            or not re.fullmatch(r"[0-9a-f]{40}", binding_release["source_revision"])
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding_release["image_digest"])
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", binding_release["group"])
            or any(type(binding_release[field]) is not int or binding_release[field] < 1 for field in ("group_generation", "backend_generation"))
        ):
            raise ValueError("invalid release")
    except (TypeError, ValueError, KeyError):
        raise HTTPException(503, detail={"error": "adapter_unavailable"}) from None
    return url, key, role, binding_release


def signed_observation_request(*, url, key, claims, payload, deadline_at_ms):
    remaining = (deadline_at_ms - int(time.time() * 1000)) / 1000
    if remaining <= 0:
        raise ValueError("observation deadline expired")
    kms = boto3.client(
        "kms",
        config=Config(
            connect_timeout=min(2, remaining),
            read_timeout=min(5, remaining),
            retries={"total_max_attempts": 1},
        ),
    )
    signed = kms.sign(KeyId=key, Message=canonical(claims), MessageType="RAW", SigningAlgorithm="RSASSA_PSS_SHA_256")
    if int(time.time() * 1000) >= deadline_at_ms:
        raise ValueError("observation deadline expired")
    assertion = encoded(canonical({"claims": claims, "signature": encoded(signed["Signature"])}))
    request = AWSRequest(
        method="POST",
        url=url,
        data=canonical({**payload, "signed_assertion": assertion}),
        headers={"content-type": "application/json"},
    )
    credentials = boto3.Session().get_credentials()
    if credentials is None or int(time.time() * 1000) >= deadline_at_ms:
        raise ValueError("service credentials unavailable")
    SigV4Auth(credentials.get_frozen_credentials(), "execute-api", get_settings().aws_region).add_auth(request)
    return request


async def observe_connection(*, attempt, user_id, tenant_id):
    url, key, role, release = observation_settings()
    deadline_at_ms = min(int(attempt.expires_at.timestamp() * 1000), int(time.time() * 1000) + 20_000)
    if deadline_at_ms <= int(time.time() * 1000):
        raise HTTPException(403, detail={"error": "invalid_consent"})
    binding = {
        "operation_id": attempt.state_digest,
        "receipt_id": attempt.state_digest,
        "payload_sha256": hashlib.sha256(canonical({"owner": user_id, "tenant": tenant_id, "attempt": attempt.state_digest})).hexdigest(),
        "release": release,
    }
    claims = {
        "audience": "tool-adapters:observe",
        "caller_role_arn": role,
        "purpose": "connection_observe",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "attempt_id": attempt.state_digest,
        "config_revision": attempt.connection_snapshot["config_revision"],
        "binding": binding,
        "deadline_at_ms": deadline_at_ms,
    }
    try:
        payload = {
            "schema_version": "1.0",
            "purpose": "connection_observe",
            "binding": binding,
            "connector": "atlassian",
            "attempt_id": attempt.state_digest,
            "config_revision": attempt.connection_snapshot["config_revision"],
            "deadline_at_ms": deadline_at_ms,
        }
        timeout = (deadline_at_ms - int(time.time() * 1000)) / 1000
        if timeout <= 0:
            raise ValueError("observation deadline expired")
        request = await asyncio.wait_for(
            asyncio.to_thread(signed_observation_request, url=url, key=key, claims=claims, payload=payload, deadline_at_ms=deadline_at_ms),
            timeout=timeout,
        )
        remaining = (deadline_at_ms - int(time.time() * 1000)) / 1000
        if remaining <= 0:
            raise ValueError("observation deadline expired")
        async with httpx.AsyncClient(timeout=min(8, remaining), follow_redirects=False, trust_env=False) as client:
            response = await asyncio.wait_for(
                client.post(url, content=request.body, headers=dict(request.headers)),
                timeout=remaining,
            )
        if int(time.time() * 1000) >= deadline_at_ms or response.status_code != 200 or len(response.content) > 65536:
            raise ValueError("adapter observation unavailable")
        result = response.json()
        expected_fields = {
            "schema_version",
            "purpose",
            "binding",
            "attempt_id",
            "config_revision",
            "provider_user_id",
            "sites",
            "granted_scopes",
            "observed_at_ms",
        }
        if type(result) is not dict or set(result) != expected_fields or result["schema_version"] != "1.0" or result["binding"] != binding:
            raise ValueError("invalid observation")
        return ConnectionObservation.model_validate(result)
    except Exception:
        raise HTTPException(503, detail={"error": "adapter_unavailable"}) from None
