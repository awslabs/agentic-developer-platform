"""Private AgentCore MCP target; identity signing stays inside the Gateway.

Human calls use current canonical authority. Memory calls additionally reopen
their original budgeted admission and permanently fence a single dispatch.
Run-bound operations retain their separate authority contract.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse, Response

from src.agentauth.door_identity import sign_door_catalogue, sign_door_identity
from src.agentauth.envelope import EnvelopeError
from src.agentauth.knowledge_service import bounded_body, door_config, forward_door
from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.crew.targets import CrewLeaseFencedError, CrewOwner, CrewTargetMissingError
from src.internal import tools_knowledge_memory as memory
from src.internal.mcp_params import operation_params
from src.internal.tool_entitlements import effective_entitlement
from src.internal.tools_dynamic_catalogue import canonical, publish_catalogue, target_binding, validate_tools
from src.internal.tools_effective_policy import effective_tools_policy
from src.internal.tools_policy import ToolsPolicyStore
from src.shared.database import get_session_factory
from src.shared.identity.resolver import resolve_asset_owner_id
from src.shared.identity.workspaces import login_subject_for_user
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User
from src.shared.models.vault import UserIdentity

PATH = "/internal/v1/tools/knowledge/mcp"
router = APIRouter(tags=["tools-knowledge"])
logger = logging.getLogger(__name__)


def require_transport(request: Request, *, namespace="knowledge") -> None:
    """The edge proof is mandatory; an asserted role header is never enough."""
    if any(len(request.headers.getlist(name)) != 1 for name in ("x-caller-identity", "x-adp-edge-provenance", "x-adp-knowledge-edge-provenance")):
        raise HTTPException(403, "knowledge transport refused")
    private_proof = os.environ.get("ADP_TOOLS_KNOWLEDGE_EDGE_PROVENANCE", "")
    if not private_proof or not secrets.compare_digest(private_proof, request.headers["x-adp-knowledge-edge-provenance"]):
        raise HTTPException(403, "knowledge transport refused")
    caller = verified_caller_identity(request)
    expected = os.environ.get("ADP_TOOLS_GATEWAY_ROLE_ARN", "")
    if not expected or parse_assumed_role_arn(caller or "") != expected:
        raise HTTPException(403, "knowledge transport refused")
    if namespace not in {"knowledge", "github", "jira", "slack"} or os.environ.get(f"ADP_TOOLS_{namespace.upper()}_MEDIATOR_ENABLED") != "true":
        raise HTTPException(503, "knowledge mediator unavailable")


def current_binding(*, metadata_only=False, namespace="knowledge") -> dict:
    if namespace not in {"knowledge", "github", "jira", "slack", "atlassian"}:
        raise ValueError("unsupported private target")
    prefix = f"ADP_TOOLS_{namespace.upper()}"
    gateway_id = os.environ.get(prefix + "_GATEWAY_ID", "")
    target_id = os.environ.get(prefix + "_TARGET_ID", "")
    if not gateway_id or (not target_id and not metadata_only):
        raise ValueError("knowledge deployment unavailable")
    control = boto3.client("bedrock-agentcore-control", config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}))
    gateway = control.get_gateway(gatewayIdentifier=gateway_id)
    if not target_id:
        # Creation assigns the ID before AgentCore fetches the schema. Only
        # metadata bootstrap may resolve that unique server-configured name.
        matches = [
            target["targetId"]
            for page in control.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gateway_id)
            for target in page["items"]
            if target.get("name") == namespace
        ]
        if len(matches) != 1:
            raise ValueError("knowledge bootstrap target unavailable")
        target_id = matches[0]
    target = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    if (
        target.get("name") != namespace
        or gateway.get("roleArn") != os.environ.get("ADP_TOOLS_GATEWAY_ROLE_ARN")
        or gateway.get("status") != "READY"
        or gateway.get("authorizerType") != "AWS_IAM"
        or not gateway.get("interceptorConfigurations")
        or not gateway.get("gatewayArn")
        or target.get("gatewayArn") != gateway["gatewayArn"]
    ):
        raise ValueError("knowledge gateway binding unavailable")
    return target_binding(gateway_id, target, gateway, metadata_only=metadata_only)


def decode_response(response: Response, request_id) -> dict:
    body = response.body
    if not 200 <= response.status_code < 300:
        raise ValueError("Door request failed")
    if response.media_type == "text/event-stream":
        packets = [
            json.loads("\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")))
            for event in body.decode().replace("\r\n", "\n").split("\n\n")
            if any(line.startswith("data:") for line in event.splitlines())
        ]
        if len(packets) != 1:
            raise ValueError("invalid Door event stream")
        packet = packets[0]
    else:
        packet = json.loads(body)
    if not isinstance(packet, dict) or packet.get("jsonrpc") != "2.0" or packet.get("id") != request_id:
        raise ValueError("invalid Door response")
    return packet


async def verify_facade(token: str, operation: str, arguments: dict, request_id: str, *, namespace="knowledge") -> dict:
    if not isinstance(token, str) or not 0 < len(token) <= 16384:
        logger.warning("Private tool assertion refused", extra={"event": "private_tool_assertion_refused", "stage": "assertion_presence"})
        raise HTTPException(403, "knowledge assertion refused")
    stage, binding_checks = "envelope", {}
    try:

        def decode(value):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise ValueError("invalid encoding")
            return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)

        envelope = json.loads(decode(token))
        claims, signature = envelope["claims"], decode(envelope["signature"])
        principal = claims["principal"]
        facade = os.environ.get("ADP_TOOLS_FACADE_ROLE_ARN", "")
        key = os.environ.get("ADP_TOOLS_ASSERTION_KEY_ARN", "")
        # The interceptor binds this signed ingress ID before AgentCore opens
        # a separate MCP request to its server target. That relay may assign a
        # different transport ID; it is not a new source of caller authority.
        upstream_rpc_id = claims.get("rpc_id")
        upstream_rpc_id_valid = type(upstream_rpc_id) in {str, int} and 0 < len(str(upstream_rpc_id)) <= 256
        stage = "request_binding"
        # Fixed field names and booleans only: no assertions, IDs, arguments,
        # identity data, or exception text belong in diagnostic logs.
        binding_checks = {
            "upstream_rpc_id_valid": upstream_rpc_id_valid,
            "arguments_match": claims.get("arguments_sha256") == hashlib.sha256(canonical(arguments)).hexdigest(),
            "tool_matches": claims.get("tool") == namespace + "___" + operation,
            "caller_role_matches": claims.get("caller_role_arn") == facade,
            "principal_bound": isinstance(principal, dict) and claims.get("root_human_id") == principal.get("user_id"),
            "verification_configured": bool(facade and key),
        }
        if (
            not facade
            or not key
            or not isinstance(claims, dict)
            or not isinstance(principal, dict)
            or claims.get("caller_role_arn") != facade
            or claims.get("method") != "tools/call"
            or namespace not in {"knowledge", "github", "jira", "slack"}
            or claims.get("tool") != namespace + "___" + operation
            or type(request_id) not in {str, int}
            or not str(request_id)
            or len(str(request_id)) > 256
            or not upstream_rpc_id_valid
            or claims.get("arguments_sha256") != hashlib.sha256(canonical(arguments)).hexdigest()
            or not all(isinstance(principal.get(field), str) and 0 < len(principal[field]) <= 256 for field in ("user_id", "tenant_id", "team_id"))
            or claims.get("root_human_id") != principal["user_id"]
            or not isinstance(claims.get("receipt_id"), str)
            or not 0 < len(claims["receipt_id"]) <= 256
        ):
            raise ValueError("unbound knowledge authority")
        stage = "run_binding"
        native = claims.get("native_test")
        if native is None:
            if claims.get("run_id") is None and any(claims.get(field) is not None for field in ("workload_id", "run_expires_at", "host_generation")):
                raise ValueError("unbound knowledge authority")
            if claims.get("run_id") is not None and (
                not all(isinstance(claims.get(field), str) and claims[field] for field in ("run_id", "workload_id"))
                or type(claims.get("run_expires_at")) is not int
                or type(claims.get("host_generation")) is not int
                or claims["host_generation"] < 1
            ):
                raise ValueError("unbound knowledge authority")
        elif (
            type(native) is not dict
            or set(native) != {"operation_id", "execution_id", "request_id", "request_sha256"}
            or not all(isinstance(claims.get(field), str) and claims[field] for field in ("run_id", "workload_id"))
            or type(claims.get("run_expires_at")) is not int
            or claims.get("host_generation") is not None
        ):
            raise ValueError("unbound native knowledge authority")
        stage = "freshness"
        check_fresh(claims)
        stage = "signature"
        valid = await asyncio.to_thread(
            boto3.client("kms", config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})).verify,
            KeyId=key,
            Message=canonical(claims),
            MessageType="RAW",
            Signature=signature,
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if valid.get("SignatureValid") is not True:
            raise ValueError("invalid signature")
        if native is None and claims.get("run_id") is not None:
            stage = "host_run"
            await verify_host_run(claims)
        stage = "freshness_after_signature"
        check_fresh(claims)
        return claims
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError):
        logger.warning(
            "Private tool assertion refused",
            extra={"event": "private_tool_assertion_refused", "stage": stage, "binding_checks": binding_checks},
        )
        raise HTTPException(403, "knowledge assertion refused") from None


async def verify_host_run(claims: dict) -> None:
    from src.agentauth.chat_crew_run import current_crew_store
    from src.crew.proxy import get_target_resolver

    now = int(time.time())
    principal = claims["principal"]
    if claims["run_expires_at"] <= now:
        raise ValueError("expired host run")
    row = await asyncio.to_thread(current_crew_store().read_run, claims["run_id"])
    if (
        not isinstance(row, dict)
        or row.get("run_id") != claims["run_id"]
        or row.get("status") != "active"
        or row.get("kind") != "crew"
        or row.get("executor_scope") != "session"
        or row.get("target_kind") != "ec2-instance"
        or not row.get("host_role_arn")
        or row.get("tenant_id") != principal["tenant_id"]
        or row.get("user_id") != principal["user_id"]
        or row.get("root_human_id") != claims["root_human_id"]
        or row.get("lease_expires_at") != claims["run_expires_at"]
    ):
        raise ValueError("unbound host run")
    owner = CrewOwner(principal["tenant_id"], principal["user_id"])
    try:
        target = await get_target_resolver().resolve(owner, now=now)
    except (CrewTargetMissingError, CrewLeaseFencedError):
        raise ValueError("fenced host run") from None
    if (
        target.lease.target_kind != "ec2-instance"
        or target.lease.tenant_id != owner.tenant_id
        or target.lease.user_id != owner.user_id
        or target.lease.expires_at <= now
        or target.lease.expires_at != claims["run_expires_at"]
        or target.lease.generation != row.get("lease_generation")
        or target.lease.generation != claims["host_generation"]
        # The run row names the box in ``session_id`` and the machine in ``target_ref`` (handoff);
        # the lease names the box in ``target_ref``. Dev 2026-10-23: comparing lease.target_ref with
        # row.target_ref refused every real host run as "fenced" while the unit fixture used one id for both.
        or target.lease.target_ref != row.get("session_id")
        or target.host_instance_ref != row.get("target_ref")
        or target.host_run_id != claims["run_id"]
        or target.host_instance_ref != claims["workload_id"]
    ):
        raise ValueError("fenced host run")


def check_fresh(claims):
    issued, expires = claims.get("issued_at"), claims.get("expires_at")
    if type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 30 or not issued <= time.time() < expires:
        raise ValueError("expired knowledge authority")


async def claim_request(claims, runtime):
    digest = hashlib.sha256(canonical(claims)).hexdigest()
    try:
        await asyncio.to_thread(
            runtime.store.client.put_item,
            TableName=runtime.store.table,
            Item={
                "pk": {"S": "TOOLS_KNOWLEDGE_REPLAY#" + digest},
                "sk": {"S": "REQUEST"},
                "knowledge_replay_ttl": {"N": str(claims["expires_at"] + 60)},
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HTTPException(403, "knowledge assertion already used") from None
        raise HTTPException(503, "knowledge replay store unavailable") from None
    except BotoCoreError:
        raise HTTPException(503, "knowledge replay store unavailable") from None


@asynccontextmanager
async def locked_human_identity(principal):
    """Canonical membership and the real linked identity remain locked in use."""
    user_id, tenant_id, team_id = (principal[field] for field in ("user_id", "tenant_id", "team_id"))
    async with get_session_factory()() as db, db.begin():
        user = (
            await db.execute(
                select(User)
                .join(TenantMembership, TenantMembership.user_id == User.id)
                .join(TeamMembership, TeamMembership.user_id == User.id)
                .where(
                    User.id == user_id,
                    User.org_id == tenant_id,
                    User.team_id == team_id,
                    User.user_kind == "human",
                    User.is_shadow.is_(False),
                    TenantMembership.tenant_id == tenant_id,
                    TenantMembership.is_active.is_(True),
                    TenantMembership.revoked_at.is_(None),
                    TeamMembership.org_id == tenant_id,
                    TeamMembership.team_id == team_id,
                )
                .with_for_update(read=True, of=(User, TenantMembership, TeamMembership))
            )
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(403, "knowledge authority refused")
        try:
            subject = await login_subject_for_user(db, user, lock=True)
        except ValueError:
            raise HTTPException(403, "knowledge authority refused") from None
        if not isinstance(subject, str) or not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", subject):
            raise HTTPException(403, "knowledge authority refused")
        linked = (
            await db.execute(
                select(UserIdentity.provider_username)
                .where(UserIdentity.user_id == user_id, UserIdentity.org_id == tenant_id, UserIdentity.provider == "github")
                .order_by(UserIdentity.is_primary.desc(), UserIdentity.provider_user_id.asc())
                .limit(1)
                .with_for_update(read=True)
            )
        ).one_or_none()
        asset_owner_id = await resolve_asset_owner_id(db, subject)
        identity = {"x-owner-sub": subject.lower(), "x-asset-owner-id": asset_owner_id, "x-tenant-id": tenant_id}
        if linked is not None:
            if not isinstance(linked.provider_username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", linked.provider_username):
                raise HTTPException(403, "knowledge authority refused")
            identity["x-github-login"] = linked.provider_username.lower()
        yield identity, db


async def check_policy(claims, operation, db, runtime, *, namespace="knowledge"):
    check_fresh(claims)
    principal = claims["principal"]
    if namespace not in {"knowledge", "github", "jira", "slack", "atlassian"}:
        raise HTTPException(403, "unsupported private authority")
    permission = namespace + "." + operation
    policy = await asyncio.to_thread(
        ToolsPolicyStore(table_name=runtime.store.table, client=runtime.store.client).get,
        tenant_id=principal["tenant_id"],
        owner_id=principal["user_id"],
    )
    if policy is not None and policy.get("status") != "active":
        raise HTTPException(403, "knowledge authority refused")
    if (
        namespace == "atlassian"
        and operation in {"create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"}
        and permission not in (policy or {}).get("consented_tools", [])
    ):
        raise HTTPException(403, "knowledge authority refused")
    try:
        # #8387: consent may come from the tenant posture rather than the owner's own list.
        effective = await effective_tools_policy(
            db, policy=policy, tenant_id=principal["tenant_id"], owner_id=principal["user_id"], team_id=principal["team_id"], include_platform=False
        )
    except HTTPException as error:
        raise HTTPException(403, "knowledge authority refused") from error
    if permission not in effective.allowed_tools or permission not in effective.consented_tools:
        raise HTTPException(403, "knowledge authority refused")
    policy = effective.as_policy()
    try:
        ceiling = Decimal(str(policy.get("tool_max_cost_usd", {}).get(permission)))
        if not ceiling.is_finite() or ceiling <= 0:
            raise ValueError("invalid ceiling")
    except (InvalidOperation, ValueError, AttributeError):
        raise HTTPException(403, "knowledge authority refused") from None
    # A second read must not reuse ORM objects from the pre-forward check.
    db.expire_all()
    entitled, grant_ceiling = await effective_entitlement(
        db,
        tenant_id=principal["tenant_id"],
        team_id=principal["team_id"],
        tool_name=permission,
    )
    if not entitled or (grant_ceiling is not None and (not grant_ceiling.is_finite() or grant_ceiling <= 0)):
        raise HTTPException(403, "knowledge authority refused")
    check_fresh(claims)
    return effective.revision


def require_read_admission(operation, arguments, claims=None):
    # A listing or ordinary signed call alone does not admit memory effects.
    # The signed original-operation reference is reopened before dispatch.
    if operation in {"remember", "experience"}:
        if not claims or not claims.get("knowledge_memory") or claims.get("native_test") is not None:
            raise HTTPException(503, "knowledge_admission_unavailable")
        memory.validate_arguments(operation, arguments)
        return
    if (
        operation == "search"
        and arguments.get("scope", "code") not in ("code", "personal")
        and not (arguments.get("product_id") and arguments.get("scope") == "docs")
    ):
        raise HTTPException(503, "knowledge_admission_unavailable")
    if operation not in {"search", "understand", "impact", "browse", "secure"}:
        raise HTTPException(404, "knowledge tool unavailable")


async def _serve(request: Request, packet: dict, runtime):
    method, request_id = packet["method"], packet.get("id")
    binding = await asyncio.to_thread(current_binding, metadata_only=method != "tools/call")
    params = operation_params(packet)
    if method == "initialize":
        if not isinstance(params, dict) or params.get("protocolVersion") not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
            raise HTTPException(400, "unsupported MCP initialization")
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "adp-private-knowledge", "version": "1.0"},
                },
            }
        )
    if method == "notifications/initialized" and "id" not in packet and params == {}:
        return Response(status_code=202)
    if method == "ping" and params == {}:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
    base = door_config(runtime)
    target = urlsplit(base)
    if target.hostname not in {
        "context-mcp",
        "context-mcp.agent-context",
        "context-mcp.agent-context.svc",
        "context-mcp.agent-context.svc.cluster.local",
    } or target.port not in {None, 80, 443, 5100}:
        raise ValueError("private Door transport unavailable")
    if method == "tools/list" and params == {}:
        body = canonical({"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}})
        assertion = sign_door_catalogue(body=body, env=runtime.env)
        response = await forward_door(
            "POST",
            base + "/mcp/",
            {
                "x-adp-door-catalogue": assertion,
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            body,
        )
        answer = decode_response(response, request_id)
        result = answer.get("result")
        if not isinstance(result, dict) or result.get("nextCursor") or result.get("isError") is True or "error" in answer:
            raise ValueError("knowledge discovery unavailable")
        tools = validate_tools(result.get("tools"))
        if await asyncio.to_thread(current_binding, metadata_only=True) != binding:
            raise ValueError("knowledge deployment changed")
        if binding["target_status"] == "READY" and os.environ.get("ADP_TOOLS_KNOWLEDGE_TARGET_ID"):
            await asyncio.to_thread(publish_catalogue, binding=binding, tools=tools, client=runtime.store.client, table_name=runtime.store.table)
        # The interceptor injects this transport proof after authorization. It
        # is stripped from the public catalogue and never forwarded to Door.
        transported = [
            {
                **tool,
                "inputSchema": {
                    **tool["inputSchema"],
                    "properties": {
                        **tool["inputSchema"].get("properties", {}),
                        "adp_assertion": {"type": "string"},
                    },
                },
            }
            for tool in tools
        ]
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": transported}}, headers={"Cache-Control": "no-store"})
    if method != "tools/call" or not isinstance(params, dict) or set(params) != {"name", "arguments"}:
        raise HTTPException(400, "unsupported knowledge request")
    operation, arguments = params["name"], params["arguments"]
    if not isinstance(operation, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", operation) or not isinstance(arguments, dict):
        raise HTTPException(400, "invalid knowledge request")
    arguments = dict(arguments)
    claims = await verify_facade(arguments.pop("adp_assertion", None), operation, arguments, request_id)
    from src.agentauth.product_knowledge import authorize_product, requested_product

    product_id = requested_product(operation, arguments)
    try:
        require_read_admission(operation, arguments, claims)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "status": "not_ready",
                                    "code": "knowledge_admission_unavailable",
                                    "error": (
                                        "For content search, use scope='personal' or scope='docs' with product_id. "
                                        "General document search is not available."
                                        if operation == "search" and arguments.get("scope") == "docs" and not arguments.get("product_id")
                                        else "Knowledge operation admission is unavailable."
                                    ),
                                }
                            ),
                        }
                    ],
                },
            },
            headers={"Cache-Control": "no-store"},
        )
    async with locked_human_identity(claims["principal"]) as (identity, db):
        if product_id:
            await authorize_product(db, user_id=claims["principal"]["user_id"], tenant_id=claims["principal"]["tenant_id"], product_id=product_id)
            identity["x-product-id"] = product_id
        await check_policy(claims, operation, db, runtime)
        check_fresh(claims)
        native = None
        memory_binding = None
        if claims.get("native_test") is not None:
            from src.agentauth.native_test_knowledge_dispatch import NativeKnowledgeDispatch
            from src.agentauth.test_run_admission_store import AdmissionRefusedError

            try:
                native = await NativeKnowledgeDispatch.prepare(
                    runtime=getattr(request.app.state, "native_test_runtime", None),
                    claims=claims,
                    operation=operation,
                    arguments=arguments,
                    binding=binding,
                )
            except AdmissionRefusedError:
                raise HTTPException(403, "native knowledge authority refused") from None
        elif operation in {"remember", "experience"}:
            memory_binding = await asyncio.to_thread(memory.claim_memory, claims, operation, arguments, runtime)
            check_fresh(claims)
        else:
            await claim_request(claims, runtime)
        body = canonical({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": operation, "arguments": arguments}})
        assertion = sign_door_identity(
            principal=claims["principal"]["user_id"],
            tenant_id=claims["principal"]["tenant_id"],
            identity=identity,
            method="POST",
            path="/mcp/",
            body=body,
            env=runtime.env,
        )
        forward = forward_door if native is None else native.forward
        response = await forward(
            "POST",
            base + "/mcp/",
            {
                "x-adp-door-identity": assertion,
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            body,
        )
        answer = decode_response(response, request_id)
        if "error" in answer:
            raise ValueError("Door tool failed")
        result = answer.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("content"), list) or not result["content"]:
            raise ValueError("Door tool response unavailable")
        if not result.get("isError"):
            for content in result.get("content", []):
                if not isinstance(content, dict) or not isinstance(content.get("text"), str):
                    continue
                try:
                    tool_result = json.loads(content["text"])
                except ValueError:
                    continue
                if isinstance(tool_result, dict) and isinstance(tool_result.get("error"), str):
                    answer = {**answer, "result": {**result, "isError": True}}
                    break
        await check_policy(claims, operation, db, runtime)
        if product_id:
            await authorize_product(db, user_id=claims["principal"]["user_id"], tenant_id=claims["principal"]["tenant_id"], product_id=product_id)
        if await asyncio.to_thread(current_binding) != binding:
            raise ValueError("knowledge deployment changed")
        check_fresh(claims)
        if memory_binding is not None:
            proof = memory.completion(memory_binding, operation, arguments, answer["result"])
            if proof is not None:
                answer = {**answer, "result": {**answer["result"], "_meta": {"adp_knowledge_memory": proof}}}
        if native is not None:
            await native.received(answer)
        return JSONResponse(answer, headers={"Cache-Control": "no-store"})


@router.post(PATH)
async def knowledge_mcp(request: Request):
    require_transport(request)
    if request.url.query or any(
        request.headers.get(name)
        for name in (
            "x-adp-user-authorization",
            "x-adp-run-credential",
            "x-adp-workload-token",
            "x-adp-assertion",
            "x-adp-native-test-capability",
            "x-adp-native-test-operation",
        )
    ):
        # End-user authority has exactly one source: the bound assertion in
        # arguments. The original HTTP Authorization may still contain SigV4.
        raise HTTPException(400, "ambiguous knowledge request")
    from src.agentauth.routes import get_agent_runtime
    from src.agentauth.test_run_admission_store import AdmissionRefusedError

    try:
        async with asyncio.timeout(25):
            packet = json.loads(await bounded_body(request))
            if (
                not isinstance(packet, dict)
                or packet.get("jsonrpc") != "2.0"
                or not isinstance(packet.get("method"), str)
                or not set(packet) <= {"jsonrpc", "id", "method", "params"}
                or (packet["method"] != "notifications/initialized" and (type(packet.get("id")) not in {str, int} or len(str(packet["id"])) > 256))
            ):
                raise HTTPException(400, "invalid knowledge request")
            return await _serve(request, packet, get_agent_runtime())
    except (
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
        EnvelopeError,
        OverflowError,
        httpx.HTTPError,
        TimeoutError,
        SQLAlchemyError,
        BotoCoreError,
        ClientError,
        AdmissionRefusedError,
    ):
        raise HTTPException(503, "knowledge service unavailable") from None
