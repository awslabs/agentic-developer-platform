"""Observe current read permissions through the owner-bound adapter path."""

import asyncio
import hashlib
import json
import os
import re
import time

import boto3
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.internal.service_authorization import require_service_operation, service_tenant
from src.internal.tool_adapter_observation import canonical, observation_settings, signed_observation_request
from src.shared.config import get_settings
from src.shared.models.base import new_uuid
from src.shared.models.oauth_setup import OAuthSetupState
from src.shared.models.organization import User

READS = frozenset({"get_jira_issue", "search_jira", "get_confluence_page", "search_confluence"})


class PolicyProbe(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_revision: str = Field(min_length=1, max_length=512)
    site_id: str = Field(min_length=1, max_length=512)
    probes: dict[str, dict]
    expected_results: dict[str, str]

    @model_validator(mode="after")
    def bounded_reads(self):
        if (
            set(self.probes) != READS
            or set(self.expected_results) != READS
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.expected_results.values())
            or len(canonical(self.probes)) > 8192
            or any(arguments.get("cloudId") != self.site_id for arguments in self.probes.values())
        ):
            raise ValueError("Four bounded reads for the selected site are required")
        return self


async def current_policy_binding(db, *, user_id, tenant_id, connection, config_revision):
    from src.internal.tool_adapter_authority import TOOL_SCOPES, _current_binding

    owner = await db.scalar(
        select(User)
        .where(
            User.id == user_id,
            User.org_id == tenant_id,
            User.user_kind == "human",
            User.is_shadow.is_(False),
        )
        .with_for_update(read=True)
    )
    if owner is None or config_revision != get_settings().atlassian_metadata_sha256:
        raise HTTPException(403, detail={"error": "connection_changed"})
    for operation in sorted(READS):
        credential, binding, site, scopes, workload = await _current_binding(
            db,
            tenant_id=tenant_id,
            owner_id=user_id,
            operation=operation,
            expected_connection=connection,
        )
    state = await db.get(OAuthSetupState, "atlassian-mcp")
    if state is None or state.phase != "prepared" or not re.fullmatch(r"[0-9a-f]{64}", state.config_digest or ""):
        raise HTTPException(503, detail={"error": "policy_observation_unavailable"})
    scopes = {TOOL_SCOPES[operation] for operation in READS}
    scopes.update({"read:me", "read:account", "offline_access"})
    if not scopes <= set(credential.scopes["oauth_scopes"]):
        raise HTTPException(403, detail={"error": "connection_changed"})
    # A read probe may use a grant originally consented for writes as well.
    # Preserve that grant's lookup key after validating all read permissions.
    return {
        "provider_user_id": binding.provider_account_id,
        "scopes": credential.scopes["oauth_scopes"],
        "workload": workload,
        "setup_digest": state.config_digest,
    }


async def authorize_policy(body, request, db):
    from src.internal.tool_adapter_authority import decode

    require_service_operation(request, "tools:adapter:observe")
    adapter = os.environ.get("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN")
    if not adapter or parse_assumed_role_arn(verified_caller_identity(request) or "") != adapter:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    _, key, role, release = observation_settings()
    try:
        envelope = json.loads(decode(body.signed_assertion))
        claims = envelope["claims"]
        binding = body.binding
        if (
            set(envelope) != {"claims", "signature"}
            or type(claims) is not dict
            or set(binding) != {"operation_id", "receipt_id", "payload_sha256", "release"}
            or binding["release"] != release
            or claims.get("binding") != binding
            or claims.get("audience") != "tool-adapters:observe"
            or claims.get("caller_role_arn") != role
            or claims.get("purpose") != "policy_observe"
            or claims.get("deadline_at_ms") != body.deadline_at_ms
            or not int(time.time() * 1000) < body.deadline_at_ms <= int(time.time() * 1000) + 20_000
            or not re.fullmatch(r"[0-9a-f]{64}", claims.get("probes_sha256", ""))
            or binding["payload_sha256"] != claims["probes_sha256"]
            or any(
                not isinstance(claims.get(field), str) or not claims[field]
                for field in (
                    "user_id",
                    "tenant_id",
                    "connection_id",
                    "binding_revision",
                    "site_id",
                    "config_revision",
                    "provider_user_id",
                    "setup_digest",
                )
            )
        ):
            raise ValueError("Unbound policy observation")
        valid = await asyncio.to_thread(
            boto3.client("kms").verify,
            KeyId=key,
            Message=canonical(claims),
            MessageType="RAW",
            Signature=decode(envelope["signature"]),
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if valid.get("SignatureValid") is not True:
            raise ValueError("Invalid policy signature")
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None
    if service_tenant(request, claims["tenant_id"], "tools:adapter:observe") != claims["tenant_id"]:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    current = await current_policy_binding(
        db,
        user_id=claims["user_id"],
        tenant_id=claims["tenant_id"],
        connection={"id": claims["connection_id"], "revision": claims["binding_revision"], "site_id": claims["site_id"]},
        config_revision=claims["config_revision"],
    )
    if any(current[field] != claims[field] for field in ("provider_user_id", "setup_digest")):
        raise HTTPException(403, detail={"error": "connection_changed"})
    issued = int(time.time() * 1000)
    if issued >= body.deadline_at_ms:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    return {
        "schema_version": "1.0",
        "admission_id": new_uuid(),
        "binding": body.binding,
        "purpose": "policy_observe",
        "connector": "atlassian",
        "operation": "policy_observe",
        "identity": {
            "workload": current["workload"],
            "provider": current["workload"].removesuffix("-oauth-user") + "-user",
            "canonical_user_id": claims["user_id"],
        },
        "context": {
            "tenant_id": claims["tenant_id"],
            "canonical_user_id": claims["user_id"],
            "connection_id": claims["connection_id"],
            "binding_revision": claims["binding_revision"],
            "site_id": claims["site_id"],
            "scopes": current["scopes"],
        },
        "issued_at_ms": issued,
        "expires_at_ms": body.deadline_at_ms,
        "deadline_at_ms": body.deadline_at_ms,
    }


async def observe_policy(*, caller, body, connection_id, db):
    from src.internal.tool_adapter_authority import TOOL_SCOPES

    url, key, role, release = observation_settings()
    config_revision = get_settings().atlassian_metadata_sha256
    connection = {"id": connection_id, "revision": body.connection_revision, "site_id": body.site_id}
    current = await current_policy_binding(
        db, user_id=caller.user_id, tenant_id=caller.org_id, connection=connection, config_revision=config_revision
    )
    await db.commit()
    started = int(time.time() * 1000)
    deadline = started + 20_000
    identifier = new_uuid()
    binding = {
        "operation_id": identifier,
        "receipt_id": identifier,
        "payload_sha256": hashlib.sha256(canonical({"probes": body.probes, "expected_results": body.expected_results})).hexdigest(),
        "release": release,
    }
    payload = {
        "schema_version": "1.0",
        "purpose": "policy_observe",
        "binding": binding,
        "connector": "atlassian",
        "connection_id": connection_id,
        "binding_revision": body.connection_revision,
        "site_id": body.site_id,
        "config_revision": config_revision,
        "probes": body.probes,
        "expected_results": body.expected_results,
        "deadline_at_ms": deadline,
    }
    claims = {
        **{
            field: payload[field]
            for field in ("purpose", "binding", "connection_id", "binding_revision", "site_id", "config_revision", "deadline_at_ms")
        },
        "audience": "tool-adapters:observe",
        "caller_role_arn": role,
        "user_id": caller.user_id,
        "tenant_id": caller.org_id,
        "provider_user_id": current["provider_user_id"],
        "setup_digest": current["setup_digest"],
        "probes_sha256": binding["payload_sha256"],
    }
    try:
        request = await asyncio.wait_for(
            asyncio.to_thread(signed_observation_request, url=url, key=key, claims=claims, payload=payload, deadline_at_ms=deadline), 20
        )
        remaining = (deadline - int(time.time() * 1000)) / 1000
        if remaining <= 0:
            raise ValueError("Policy observation expired")
        async with httpx.AsyncClient(timeout=remaining, follow_redirects=False, trust_env=False) as client:
            response = await asyncio.wait_for(client.post(url, content=request.body, headers=dict(request.headers)), remaining)
        if response.status_code != 200 or len(response.content) > 16384:
            raise ValueError("Policy observation unavailable")
        result = response.json()
        now = int(time.time() * 1000)
        expected = {
            field: claims[field]
            for field in (
                "purpose",
                "binding",
                "connection_id",
                "binding_revision",
                "site_id",
                "config_revision",
                "user_id",
                "tenant_id",
                "provider_user_id",
            )
        }
        if (
            type(result) is not dict
            or set(result) != {*expected, "schema_version", "observed_at_ms", "scopes", "probes"}
            or result["schema_version"] != "1.0"
            or any(result[field] != value for field, value in expected.items())
            or type(result["observed_at_ms"]) is not int
            or not started <= result["observed_at_ms"] <= now < deadline
            or set(result["probes"]) != READS
            or set(result["scopes"]) != {TOOL_SCOPES[operation] for operation in READS}
        ):
            raise ValueError("Unbound policy observation")
        for probe in result["probes"].values():
            if (
                type(probe) is not dict
                or set(probe) != {"status", "response_sha256"}
                or probe["status"] not in {"allowed", "blocked", "unknown"}
                or not re.fullmatch(r"[0-9a-f]{64}", probe["response_sha256"])
            ):
                raise ValueError("Incomplete policy observation")
    except Exception:
        raise HTTPException(503, detail={"error": "policy_observation_unavailable"}) from None
    await db.rollback()
    refreshed = await current_policy_binding(
        db, user_id=caller.user_id, tenant_id=caller.org_id, connection=connection, config_revision=config_revision
    )
    if refreshed != current or observation_settings()[3] != release:
        raise HTTPException(409, detail={"error": "connection_changed"})
    statuses = {probe["status"] for probe in result["probes"].values()}
    status = "blocked" if "blocked" in statuses else "allowed" if statuses == {"allowed"} else "unknown"
    evidence = hashlib.sha256(canonical(result)).hexdigest()
    await db.rollback()
    return {
        "status": status,
        "evidence_sha256": evidence,
        "observed_at_ms": result["observed_at_ms"],
        "configuration_sha256": current["setup_digest"],
        "connection_revision": body.connection_revision,
        "site_id": body.site_id,
        "owner_id": caller.user_id,
        "tenant_id": caller.org_id,
        "permission": "read-search",
    }
