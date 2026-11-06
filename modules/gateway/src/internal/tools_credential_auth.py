"""Authorize an OpenAPI broker call against a signed tools decision and current consent."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.internal.credential_binding import GatewayCredentialAuthority
from src.internal.tool_entitlements import effective_entitlement
from src.internal.tools_effective_policy import effective_tools_policy
from src.internal.tools_policy import ToolsPolicyStore
from src.shared.database import get_session_factory
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User
from src.shared.models.vault import UserCredential

OPERATIONS = {
    ("GET", "/internal/v1/user-credentials"): "list",
    ("POST", "/internal/v1/proxy-request"): "proxy_request",
    ("POST", "/internal/v1/credential-assume-role"): "assume_role",
    ("POST", "/internal/v1/credential-materialize"): "materialize",
}
PUBLIC_ARGUMENTS = {
    "list": {"service"},
    "proxy_request": {"service", "label", "method", "url", "headers", "body"},
    "assume_role": {"service", "label", "purpose"},
    "materialize": {"service", "label"},
}


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def assert_tools_credential_fresh(request: Request) -> None:
    authority = getattr(request.state, "agent_tools_credential_authority", None)
    if authority is not None and (
        not isinstance(authority, GatewayCredentialAuthority)
        or type(authority.assertion_expires_at) is not int
        or time.time() >= authority.assertion_expires_at
    ):
        raise HTTPException(403, "expired tools assertion")


async def verify_tools_credential_request(request: Request) -> None:
    """Only an edge-verified gateway role with a valid façade assertion may call."""
    request.state.agent_tools_credential_authority = None
    role = os.environ.get("ADP_TOOLS_GATEWAY_ROLE_ARN", "")
    signing_key = os.environ.get("ADP_TOOLS_ASSERTION_KEY_ARN", "")
    facade = os.environ.get("ADP_TOOLS_FACADE_ROLE_ARN", "")
    if any(len(request.headers.getlist(header)) > 1 for header in ("x-caller-identity", "x-adp-edge-provenance")):
        raise HTTPException(403, "ambiguous tools transport")
    arn = verified_caller_identity(request)
    if not role or not signing_key or not facade or parse_assumed_role_arn(arn or "") != role:
        raise HTTPException(403, "tools gateway identity required")
    operation = OPERATIONS.get((request.method, request.url.path))
    if operation in {"assume_role", "materialize"}:
        raise HTTPException(403, "private credential delivery unavailable")
    headers = request.headers.getlist("x-adp-assertion")
    if operation is None or len(headers) != 1 or not 0 < len(headers[0]) <= 4096:
        raise HTTPException(404, "not found")
    try:
        envelope = json.loads(_decode(headers[0]))
        claims = envelope["claims"]
        signature = _decode(envelope["signature"])
        principal = claims["principal"]
        issued, expires = claims["issued_at"], claims["expires_at"]
        expected_tool = f"credentials___{operation}"
        arguments = dict(request.query_params) if request.method == "GET" else await request.json()
        if (
            not isinstance(claims, dict)
            or not isinstance(arguments, dict)
            or arguments.keys() - PUBLIC_ARGUMENTS[operation]
            or (request.method == "GET" and len(request.query_params.multi_items()) != len(arguments))
            or not isinstance(principal, dict)
            or not signature
            or claims.get("caller_role_arn") != facade
            or claims.get("tool") != expected_tool
            or claims.get("method") != "tools/call"
            or claims.get("arguments_sha256") != hashlib.sha256(_canonical(arguments)).hexdigest()
            or not isinstance(claims.get("receipt_id"), str)
            or not claims["receipt_id"]
            or type(issued) is not int
            or type(expires) is not int
            or not issued <= int(time.time()) < expires
            or expires - issued > 30
            or not all(isinstance(principal.get(field), str) and principal[field] for field in ("user_id", "tenant_id", "team_id"))
            or claims.get("root_human_id") != principal["user_id"]
            or any(claims.get(field) is not None for field in ("run_id", "workload_id", "run_expires_at"))
        ):
            raise ValueError("unbound broker request")
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError):
        raise HTTPException(403, "invalid tools assertion") from None
    try:
        valid = await asyncio.to_thread(
            boto3.client("kms").verify,
            KeyId=signing_key,
            Message=_canonical(claims),
            MessageType="RAW",
            Signature=signature,
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "tools authority unavailable") from None
    if valid.get("SignatureValid") is not True:
        raise HTTPException(403, "invalid tools assertion")
    if time.time() >= expires:
        raise HTTPException(403, "expired tools assertion")
    from src.agentauth.routes import get_agent_runtime

    runtime = get_agent_runtime()
    policy = await asyncio.to_thread(
        ToolsPolicyStore(table_name=runtime.store.table, client=runtime.store.client).get,
        tenant_id=principal["tenant_id"],
        owner_id=principal["user_id"],
    )
    if time.time() >= expires:
        raise HTTPException(403, "expired tools assertion")
    permission = f"credentials.{operation}"
    if policy is not None and policy.get("status") != "active":
        raise HTTPException(403, {"error": "not_consented"})
    async with get_session_factory()() as policy_session:
        # #8387: consent may come from the tenant posture rather than the owner's own list.
        effective = await effective_tools_policy(
            policy_session,
            policy=policy,
            tenant_id=principal["tenant_id"],
            owner_id=principal["user_id"],
            team_id=principal["team_id"],
            include_platform=False,
        )
    if permission not in effective.allowed_tools or permission not in effective.consented_tools:
        raise HTTPException(403, {"error": "not_consented"})
    policy = effective.as_policy()
    async with get_session_factory()() as session:
        try:
            current_user = await session.scalar(
                select(User.id)
                .join(TenantMembership, TenantMembership.user_id == User.id)
                .join(TeamMembership, TeamMembership.user_id == User.id)
                .where(
                    User.id == principal["user_id"],
                    User.org_id == principal["tenant_id"],
                    User.team_id == principal["team_id"],
                    User.user_kind == "human",
                    User.is_shadow.is_(False),
                    TenantMembership.tenant_id == principal["tenant_id"],
                    TenantMembership.is_active.is_(True),
                    TenantMembership.revoked_at.is_(None),
                    TeamMembership.org_id == principal["tenant_id"],
                    TeamMembership.team_id == principal["team_id"],
                )
                .with_for_update(read=True, of=(User, TenantMembership, TeamMembership))
            )
            if current_user != principal["user_id"] or time.time() >= expires:
                raise HTTPException(403, "current human membership required")
            entitled, _ = await effective_entitlement(session, tenant_id=principal["tenant_id"], team_id=principal["team_id"], tool_name=permission)
        except SQLAlchemyError:
            raise HTTPException(503, "tool entitlement unavailable") from None
        if not entitled:
            raise HTTPException(403, {"error": "not_entitled"})
        rows = await session.scalars(
            select(UserCredential).where(
                UserCredential.org_id == principal["tenant_id"],
                UserCredential.user_id == principal["user_id"],
            )
        )
        consented = frozenset(row.id for row in rows if getattr(row, "agent_consent", False) is True)
        if time.time() >= expires:
            raise HTTPException(403, "expired tools assertion")
    request.state.agent_tools_credential_authority = GatewayCredentialAuthority(
        user_id=principal["user_id"],
        tenant_id=principal["tenant_id"],
        agent_id="tools-facade",
        credential_ids=consented,
        assertion_expires_at=expires,
    )
