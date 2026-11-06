"""Observe effective provider permissions, not an administrator's identity."""

import asyncio
import hashlib
import json
import secrets
import time

import boto3
from fastapi import HTTPException

from tool_adapters.atlassian import READ_TOOLS, ReadMCPTransport, discover_reads
from tool_adapters.atlassian_observe import _observation_value, canonical, collect_observation
from tool_adapters.caller_identity import matches_role
from tool_adapters.identity_access import retrieve_access_token
from tool_adapters.observation import authorize_observation, decode


def policy_outcome(packet, operation, expected_result):
    if type(packet) is not dict or operation not in READ_TOOLS:
        return "unknown"
    result = packet.get("result")
    permission = "search" if operation.startswith("search_") else "read"
    denial = f"Access denied: Your organization admin has not authorized the {permission} permission."
    if type(packet.get("error")) is dict and "result" not in packet and packet["error"].get("message") == denial:
        return "blocked"
    if (
        type(result) is dict
        and result.get("isError") is True
        and "structuredContent" not in result
        and result.get("content") == [{"type": "text", "text": denial}]
    ):
        return "blocked"
    if "error" in packet:
        return "unknown"
    try:

        def structured(value):
            if type(value) is not dict or "error" in value:
                raise ValueError("Policy probe did not succeed")
            items = (
                value.get("issues" if operation == "search_jira" else "results")
                if operation.startswith("search_")
                else [value]
            )
            if (
                type(items) is not list
                or not items
                or any(
                    type(item) is not dict or not isinstance(item.get("id"), str) or not item["id"] for item in items
                )
            ):
                raise ValueError("Policy read did not return the expected resource shape")
            return value

        value = _observation_value(result, structured)
        if hashlib.sha256(canonical(value)).hexdigest() != expected_result:
            return "unknown"
    except (ValueError, TypeError):
        return "unknown"
    return "allowed"


def probe_permissions(
    token, settings, *, probes, expected_results, subject, site_id, deadline, transport_factory=ReadMCPTransport
):
    actual_subject, sites, scopes = collect_observation(
        token,
        settings,
        transport_factory=lambda **limits: transport_factory(deadline=min(deadline, limits["deadline"])),
    )
    required = {scope for _, scope in READ_TOOLS.values()}
    if (
        actual_subject != subject
        or not required <= set(scopes)
        or not any(site.site_id == site_id and required <= set(site.scopes) for site in sites)
        or set(probes) != set(READ_TOOLS)
        or set(expected_results) != set(READ_TOOLS)
        or any(arguments.get("cloudId") != site_id for arguments in probes.values())
    ):
        raise ValueError("Policy probe owner, site or scopes differ")
    transport = transport_factory(deadline=deadline)
    catalogue = discover_reads(
        token,
        settings.read_schemas_sha256,
        deadline=deadline,
        transport_factory=lambda **kwargs: transport,
        reviewed_schemas=settings.read_schemas,
    )
    schemas = {tool["name"]: tool["inputSchema"] for tool in catalogue}
    for alias, arguments in probes.items():
        schema = schemas[alias]
        if not set(schema.get("required", [])) <= set(arguments) <= set(schema["properties"]):
            raise ValueError("Policy probe arguments differ from the pinned schema")
    results = {}
    for alias, arguments in probes.items():
        if time.monotonic() >= deadline:
            raise ValueError("Policy observation deadline expired")
        packet = transport.send("tools/call", {"name": READ_TOOLS[alias][0], "arguments": arguments}, token)
        results[alias] = {
            "status": policy_outcome(packet, alias, expected_results[alias]),
            "response_sha256": hashlib.sha256(canonical(packet)).hexdigest(),
        }
    return {"provider_user_id": actual_subject, "scopes": sorted(required), "probes": results}


async def observe_policy(body, request, configuration):
    settings = configuration.observation
    now = int(time.time() * 1000)
    try:
        if (
            configuration.mode != "observation"
            or settings is None
            or settings.read_schemas is None
            or any(len(request.headers.getlist(key)) != 1 for key in ("x-adp-edge-provenance", "x-caller-identity"))
            or not secrets.compare_digest(
                hashlib.sha256(request.headers["x-adp-edge-provenance"].encode()).hexdigest(),
                settings.edge_provenance_sha256,
            )
            or not matches_role(request.headers["x-caller-identity"], settings.core_role_arn)
            or body.config_revision != settings.config_revision
            or body.binding.release != configuration.release
            or not now < body.deadline_at_ms <= now + 20_000
        ):
            raise ValueError("Policy observation authority unavailable")
        envelope = json.loads(decode(body.signed_assertion))
        claims = envelope["claims"]
        if (
            set(envelope) != {"claims", "signature"}
            or claims.get("audience") != "tool-adapters:observe"
            or claims.get("caller_role_arn") != settings.core_role_arn
            or claims.get("purpose") != "policy_observe"
            or claims.get("binding") != body.binding.model_dump()
            or claims.get("deadline_at_ms") != body.deadline_at_ms
            or any(
                claims.get(key) != getattr(body, key)
                for key in ("connection_id", "binding_revision", "site_id", "config_revision")
            )
            or claims.get("probes_sha256")
            != hashlib.sha256(canonical({"probes": body.probes, "expected_results": body.expected_results})).hexdigest()
        ):
            raise ValueError("Policy observation binding differs")
        verified = await asyncio.to_thread(
            boto3.client("kms").verify,
            KeyId=settings.assertion_key_arn,
            Message=canonical(claims),
            MessageType="RAW",
            Signature=decode(envelope["signature"]),
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if verified.get("SignatureValid") is not True:
            raise ValueError("Policy observation signature unavailable")
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None
    admission = await authorize_observation(body, configuration)
    if (
        admission.purpose != "policy_observe"
        or admission.context.connection_id != body.connection_id
        or admission.context.binding_revision != body.binding_revision
        or admission.context.site_id != body.site_id
        or admission.context.canonical_user_id != claims.get("user_id")
        or admission.context.tenant_id != claims.get("tenant_id")
    ):
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    token = await retrieve_access_token(admission, configuration)
    current = await authorize_observation(body, configuration)
    if (
        current.context != admission.context
        or current.identity != admission.identity
        or current.binding != admission.binding
    ):
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    remaining = (min(body.deadline_at_ms, current.expires_at_ms) - int(time.time() * 1000)) / 1000
    if remaining <= 0:
        raise HTTPException(503, detail={"error": "policy_observation_unavailable"})
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                probe_permissions,
                token,
                settings,
                probes=body.probes,
                expected_results=body.expected_results,
                subject=claims["provider_user_id"],
                site_id=body.site_id,
                deadline=time.monotonic() + remaining,
            ),
            remaining,
        )
    except Exception:
        raise HTTPException(503, detail={"error": "policy_observation_unavailable"}) from None
    return {
        "schema_version": "1.0",
        "purpose": "policy_observe",
        "binding": body.binding.model_dump(),
        "tenant_id": claims["tenant_id"],
        "user_id": claims["user_id"],
        "connection_id": body.connection_id,
        "binding_revision": body.binding_revision,
        "site_id": body.site_id,
        "config_revision": body.config_revision,
        "observed_at_ms": int(time.time() * 1000),
        **result,
    }
