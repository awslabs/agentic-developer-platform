"""Private observation boundary; the adapter never stores or returns Identity tokens."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time

import boto3
import httpx
from adp_tools.adapter_contracts import Admission, AuthorizeRequest, ConnectionObservation, ObserveRequest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from fastapi import HTTPException, Request

from tool_adapters.atlassian_observe import observe_provider
from tool_adapters.caller_identity import matches_role
from tool_adapters.identity_access import retrieve_access_token


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def decode(value):
    if not isinstance(value, str) or not 0 < len(value) <= 16384:
        raise ValueError("invalid assertion")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


async def verified_observation(request: Request, body: ObserveRequest, configuration):
    settings = configuration.observation
    if configuration.mode != "observation" or settings is None:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    now = int(time.time() * 1000)
    try:
        if (
            any(len(request.headers.getlist(key)) != 1 for key in ("x-adp-edge-provenance", "x-caller-identity"))
            or not 0 < len(request.headers["x-adp-edge-provenance"]) <= 256
            or not secrets.compare_digest(
                hashlib.sha256(request.headers["x-adp-edge-provenance"].encode()).hexdigest(),
                settings.edge_provenance_sha256,
            )
            or not matches_role(request.headers["x-caller-identity"], settings.core_role_arn)
            or body.purpose != "connection_observe"
            or body.connector != "atlassian"
            or body.config_revision != settings.config_revision
            or body.binding.release != configuration.release
            or not now < body.deadline_at_ms <= now + 20_000
        ):
            raise ValueError("observation authority unavailable")
        envelope = json.loads(decode(body.signed_assertion))
        if type(envelope) is not dict or set(envelope) != {"claims", "signature"}:
            raise ValueError("invalid assertion")
        claims = envelope["claims"]
        if type(claims) is not dict or (
            claims.get("audience") != "tool-adapters:observe"
            or claims.get("caller_role_arn") != settings.core_role_arn
            or claims.get("purpose") != "connection_observe"
            or claims.get("attempt_id") != body.attempt_id
            or claims.get("config_revision") != body.config_revision
            or claims.get("binding") != body.binding.model_dump()
            or claims.get("deadline_at_ms") != body.deadline_at_ms
            or not isinstance(claims.get("user_id"), str)
            or not isinstance(claims.get("tenant_id"), str)
            or not claims["user_id"]
            or not claims["tenant_id"]
        ):
            raise ValueError("invalid observation proof")
        valid = await asyncio.to_thread(
            boto3.client("kms").verify,
            KeyId=settings.assertion_key_arn,
            Message=canonical(claims),
            MessageType="RAW",
            Signature=decode(envelope["signature"]),
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if valid.get("SignatureValid") is not True or int(time.time() * 1000) >= body.deadline_at_ms:
            raise ValueError("invalid observation proof")
        return claims
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None


async def authorize_observation(body, configuration):
    settings = configuration.observation
    payload = AuthorizeRequest(
        schema_version="1.0",
        binding=body.binding,
        purpose=body.purpose,
        connector="atlassian",
        operation="policy_observe" if body.purpose == "policy_observe" else "observe",
        signed_assertion=body.signed_assertion,
        deadline_at_ms=body.deadline_at_ms,
    )
    try:
        credentials = boto3.Session().get_credentials()
        region = os.environ.get("AWS_REGION", "")
        if not credentials or not region:
            raise ValueError("missing adapter service credentials")
        aws_request = AWSRequest(
            method="POST",
            url=settings.core_authorize_url,
            data=payload.model_dump_json().encode(),
            headers={"content-type": "application/json"},
        )
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", region).add_auth(aws_request)
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                settings.core_authorize_url, content=aws_request.body, headers=dict(aws_request.headers)
            )
        if response.status_code != 200 or len(response.content) > 16384:
            raise ValueError("core refused observation")
        admission = Admission.model_validate(response.json())
        if (
            admission.purpose != payload.purpose
            or admission.binding != body.binding
            or admission.connector != "atlassian"
            or admission.operation != payload.operation
            or admission.deadline_at_ms != body.deadline_at_ms
            or admission.identity.workload != settings.identity_workload
            or admission.identity.provider != settings.identity_provider
            or admission.context.connection_id != getattr(body, "connection_id", None)
            or admission.context.binding_revision != getattr(body, "binding_revision", None)
            or admission.context.site_id != getattr(body, "site_id", None)
            or admission.expires_at_ms <= int(time.time() * 1000)
        ):
            raise ValueError("core admission changed")
        return admission
    except Exception:
        raise HTTPException(503, detail={"error": "authority_unavailable"}) from None


async def observe(body: ObserveRequest, request: Request, configuration):
    claims = await verified_observation(request, body, configuration)
    admission = await authorize_observation(body, configuration)
    if admission.context.tenant_id != claims["tenant_id"] or admission.context.canonical_user_id != claims["user_id"]:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    token = await retrieve_access_token(admission, configuration)
    current = await authorize_observation(body, configuration)
    if (
        current.binding != admission.binding
        or current.identity != admission.identity
        or current.context != admission.context
    ):
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    subject, sites, granted_scopes = await observe_provider(token, configuration.observation)
    return ConnectionObservation(
        schema_version="1.0",
        purpose="connection_observe",
        binding=body.binding,
        attempt_id=body.attempt_id,
        config_revision=body.config_revision,
        provider_user_id=subject,
        sites=sites,
        granted_scopes=granted_scopes,
        observed_at_ms=int(time.time() * 1000),
    )
