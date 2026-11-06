"""Core authorization of Atlassian observations and one-use tool dispatch."""

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Annotated, Literal

import boto3
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.github_oauth import canonical as receipt_canonical
from src.internal.github_oauth import credential_digest
from src.internal.service_authorization import require_service_operation, service_tenant
from src.internal.tool_adapter_legacy import LEGACY_SCOPES, current_legacy_adapter_binding
from src.internal.tool_adapter_observation import canonical, observation_settings
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.identity.atlassian_readiness import verified_connection
from src.shared.models.base import new_uuid
from src.shared.models.organization import User
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, OAuthToolReceipt, ToolAdapterActivation, UserCredential

router = APIRouter(prefix="/internal/v1/tool-adapters", tags=["tool-adapters"])


class ObservationAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    binding: dict
    purpose: Literal["connection_observe"]
    connector: Literal["atlassian"]
    operation: Literal["observe"]
    signed_assertion: str = Field(min_length=1, max_length=16384, repr=False)
    deadline_at_ms: int


class ToolAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    binding: dict
    purpose: Literal["tool_call"]
    connector: Literal["atlassian", "github", "slack", "jira"]
    operation: str = Field(min_length=1, max_length=512)
    signed_assertion: str = Field(min_length=1, max_length=16384, repr=False)
    deadline_at_ms: int
    context_arguments: dict | None = None


class PolicyAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    binding: dict
    purpose: Literal["policy_observe"]
    connector: Literal["atlassian"]
    operation: Literal["policy_observe"]
    signed_assertion: str = Field(min_length=1, max_length=16384, repr=False)
    deadline_at_ms: int


class ReconcileAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    binding: dict
    purpose: Literal["reconcile"]
    connector: Literal["atlassian"]
    operation: Literal["get_jira_issue"]
    read_arguments: dict
    signed_assertion: str = Field(min_length=1, max_length=16384, repr=False)
    deadline_at_ms: int


class ToolDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    admission_id: str = Field(min_length=1, max_length=512)
    binding: dict
    binding_revision: str = Field(min_length=1, max_length=512)


def decode(value):
    import base64

    if not isinstance(value, str) or not 0 < len(value) <= 16384:
        raise ValueError("invalid proof")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


@router.post("/authorize", dependencies=[Depends(verify_internal_or_irsa)])
async def authorize_observation(
    body: Annotated[ObservationAuthorization | ToolAuthorization | ReconcileAuthorization | PolicyAuthorization, Body(discriminator="purpose")],
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if isinstance(body, PolicyAuthorization):
        from src.internal.tool_adapter_policy import authorize_policy

        return await authorize_policy(body, request, db)
    if isinstance(body, ToolAuthorization):
        return await authorize_tool(body, request, db)
    if isinstance(body, ReconcileAuthorization):
        return await authorize_reconciliation(body, request, db)
    require_service_operation(request, "tools:adapter:observe")
    adapter_role = os.environ.get("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", "")
    if not adapter_role or parse_assumed_role_arn(verified_caller_identity(request) or "") != adapter_role:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    _, key, role, release = observation_settings()
    now = int(time.time() * 1000)
    try:
        envelope = json.loads(decode(body.signed_assertion))
        claims = envelope["claims"]
        binding = body.binding
        if (
            type(claims) is not dict
            or type(binding) is not dict
            or set(binding) != {"operation_id", "receipt_id", "payload_sha256", "release"}
            or binding["release"] != release
            or claims.get("audience") != "tool-adapters:observe"
            or claims.get("caller_role_arn") != role
            or claims.get("purpose") != body.purpose
            or claims.get("binding") != binding
            or claims.get("deadline_at_ms") != body.deadline_at_ms
            or not now < body.deadline_at_ms <= now + 20_000
            or binding["operation_id"] != claims.get("attempt_id")
            or binding["receipt_id"] != claims.get("attempt_id")
            or binding["payload_sha256"]
            != hashlib.sha256(canonical({"owner": claims["user_id"], "tenant": claims["tenant_id"], "attempt": claims["attempt_id"]})).hexdigest()
        ):
            raise ValueError("unbound observation")
        valid = await asyncio.to_thread(
            boto3.client("kms").verify,
            KeyId=key,
            Message=canonical(claims),
            MessageType="RAW",
            Signature=decode(envelope["signature"]),
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if valid.get("SignatureValid") is not True:
            raise ValueError("invalid signature")
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None
    if (
        service_tenant(request, claims["tenant_id"], "tools:adapter:observe") != claims["tenant_id"]
        or body.connector != "atlassian"
        and os.environ.get("ADP_TOOL_ADAPTER_LEGACY_ENABLED") != "true"
        or int(time.time() * 1000) >= body.deadline_at_ms
    ):
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    owner = await db.scalar(
        select(User)
        .where(
            User.id == claims["user_id"],
            User.org_id == claims["tenant_id"],
            User.user_kind == "human",
            User.is_shadow.is_(False),
        )
        .with_for_update(read=True)
    )
    if owner is None:
        raise HTTPException(403, detail={"error": "invalid_consent"})
    attempt = await db.scalar(
        select(OAuthConnectionAttempt)
        .where(
            OAuthConnectionAttempt.state_digest == claims["attempt_id"],
            OAuthConnectionAttempt.org_id == claims["tenant_id"],
            OAuthConnectionAttempt.user_id == claims["user_id"],
            OAuthConnectionAttempt.provider == "atlassian-mcp",
        )
        .with_for_update()
    )
    if (
        attempt is None
        or attempt.identity_completed_at is None
        or attempt.consumed_at is not None
        or attempt.superseded_at is not None
        or attempt.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        or attempt.connection_snapshot is None
        or attempt.connection_snapshot.get("config_revision") != claims.get("config_revision")
        or not isinstance(attempt.requested_scopes, list)
        or not attempt.requested_scopes
    ):
        raise HTTPException(403, detail={"error": "invalid_consent"})
    existing = await db.scalar(
        select(UserCredential).where(
            UserCredential.org_id == claims["tenant_id"],
            UserCredential.user_id == claims["user_id"],
            UserCredential.service == "atlassian-mcp",
            UserCredential.provider == "agentcore-identity",
        )
    )
    previous = await db.get(OAuthConnectionBinding, existing.id) if existing else None
    if attempt.connection_snapshot != {
        "credential_sha256": credential_digest(existing) if existing else None,
        "binding_revision": previous.revision if previous else None,
        "config_revision": claims["config_revision"],
    }:
        raise HTTPException(403, detail={"error": "connection_changed"})
    workload = f"{get_settings().oauth_workload_name.removesuffix('-oauth-user')}-atlassian-mcp-oauth-user"
    if not workload.startswith("adp-tools-"):
        raise HTTPException(503, detail={"error": "identity_unavailable"})
    issued = int(time.time() * 1000)
    if issued >= body.deadline_at_ms:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    return {
        "schema_version": "1.0",
        "admission_id": new_uuid(),
        "binding": binding,
        "purpose": "connection_observe",
        "connector": "atlassian",
        "operation": "observe",
        "identity": {"workload": workload, "provider": workload.removesuffix("-oauth-user") + "-user", "canonical_user_id": owner.id},
        "context": {
            "tenant_id": owner.org_id,
            "canonical_user_id": owner.id,
            "connection_id": None,
            "binding_revision": None,
            "site_id": None,
            "scopes": attempt.requested_scopes,
        },
        "issued_at_ms": issued,
        "expires_at_ms": min(issued + 5000, body.deadline_at_ms),
        "deadline_at_ms": body.deadline_at_ms,
    }


async def authorize_reconciliation(body: ReconcileAuthorization, request: Request, db: AsyncSession):
    from src.internal.tools_human_authority import current_tools_human

    require_service_operation(request, "tools:adapter:authorize")
    adapter_role = os.environ.get("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", "")
    if not adapter_role or parse_assumed_role_arn(verified_caller_identity(request) or "") != adapter_role:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    _, _, _, release = observation_settings()
    now = int(time.time() * 1000)
    try:
        envelope = json.loads(decode(body.signed_assertion))
        claims = envelope["claims"]
        principal = claims["principal"]
        connection = claims["connection"]
        original = claims["atlassian_operation"]
        binding = body.binding
        arguments = body.read_arguments
        role = os.environ["ADP_TOOLS_RECONCILER_ROLE_ARN"]
        key = os.environ["ADP_TOOLS_RECONCILER_KEY_ARN"]
        if (
            type(envelope) is not dict
            or set(envelope) != {"claims", "signature"}
            or type(claims) is not dict
            or type(principal) is not dict
            or type(original) is not dict
            or type(connection) is not dict
            or set(binding) != {"operation_id", "receipt_id", "payload_sha256", "release"}
            or type(arguments) is not dict
            or set(arguments) not in ({"cloudId", "issueIdOrKey"}, {"cloudId", "issueId"}, {"cloudId", "id"}, {"cloudId", "key"})
            or any(type(value) is not str or not 0 < len(value) <= 512 for value in arguments.values())
            or arguments["cloudId"] != connection.get("site_id")
            or claims.get("purpose") != "reconcile"
            or claims.get("audience") != "tool-adapters:reconcile"
            or claims.get("caller_role_arn") != role
            or claims.get("read_arguments_sha256") != hashlib.sha256(canonical(arguments)).hexdigest()
            or claims.get("deadline_at_ms") != body.deadline_at_ms
            or not now < body.deadline_at_ms <= now + 20_000
            or original.get("tool") not in {"atlassian___" + name for name in WRITE_OPERATIONS}
            or original.get("operation_id") != (f"OP#{principal['tenant_id']}#{principal['user_id']}#{original['tool']}#{original['request_id']}")
            or original.get("connection") != connection
            or original.get("release") != release
            or original.get("payload_sha256") != binding["payload_sha256"]
            or original.get("operation_id") != binding["operation_id"]
            or original.get("receipt_id") != binding["receipt_id"]
            or binding["release"] != release
            or connection.get("id") is None
            or claims.get("reconcile_tool") != "get_jira_issue"
        ):
            raise ValueError("unbound Jira readback")
        valid = await asyncio.to_thread(
            boto3.client("kms").verify,
            KeyId=key,
            Message=canonical(claims),
            MessageType="RAW",
            Signature=decode(envelope["signature"]),
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if valid.get("SignatureValid") is not True:
            raise ValueError("invalid readback signature")
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None
    if service_tenant(request, principal["tenant_id"], "tools:adapter:authorize") != principal["tenant_id"]:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    activation = await db.get(ToolAdapterActivation, release["group"])
    if activation is None or activation.status != "active" or activation.release != release:
        raise HTTPException(403, detail={"error": "adapter_release_fenced"})
    owner = await current_tools_human(db, principal)
    credential, current, site, _, workload = await _current_binding(
        db,
        tenant_id=owner.org_id,
        owner_id=owner.id,
        operation="get_jira_issue",
        expected_connection=connection,
    )
    issued = int(time.time() * 1000)
    if issued >= body.deadline_at_ms:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    return {
        "schema_version": "1.0",
        "admission_id": new_uuid(),
        "binding": binding,
        "purpose": "reconcile",
        "connector": "atlassian",
        "operation": "get_jira_issue",
        "identity": {"workload": workload, "provider": workload.removesuffix("-oauth-user") + "-user", "canonical_user_id": owner.id},
        "context": {
            "tenant_id": owner.org_id,
            "canonical_user_id": owner.id,
            "connection_id": credential.id,
            "binding_revision": current.revision,
            "site_id": site,
            "scopes": credential.scopes["oauth_scopes"],
        },
        "issued_at_ms": issued,
        "expires_at_ms": min(issued + 5000, body.deadline_at_ms),
        "deadline_at_ms": body.deadline_at_ms,
    }


TOOL_SCOPES = {
    "get_project_context": "read:jira:agent-interface",
    "search_jira": "search:jira:agent-interface",
    "get_jira_issue": "read:jira:agent-interface",
    "search_confluence": "search:confluence:agent-interface",
    "get_confluence_page": "read:confluence:agent-interface",
    "create_jira_issue": "write:jira:agent-interface",
    "update_jira_issue": "write:jira:agent-interface",
    "comment_jira_issue": "write:jira:agent-interface",
    "transition_jira_issue": "write:jira:agent-interface",
}
WRITE_OPERATIONS = frozenset({"create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"})


def require_recorded_write(claims, release):
    operation = claims.get("atlassian_operation")
    approval = claims.get("atlassian_approval")
    principal = claims.get("principal", {})
    if (
        type(operation) is not dict
        or set(operation)
        != {
            "tool",
            "operation_id",
            "execution_id",
            "request_id",
            "receipt_id",
            "payload_sha256",
            "dispatch_intent",
            "connection",
            "release",
            "approval",
        }
        or type(approval) is not dict
        or set(approval) != {"tool", "request_id", "payload_sha256", "policy_revision", "consent_source"}
        or type(approval["policy_revision"]) is not int
        or approval["policy_revision"] < 0
        or approval["consent_source"] != "explicit"
        or approval["tool"] != claims["tool"]
        or approval["request_id"] != operation["request_id"]
        or approval["payload_sha256"] != claims["arguments_sha256"]
        or operation["approval"] != approval
        or operation["tool"] != claims["tool"]
        or operation["operation_id"] != (f"OP#{principal['tenant_id']}#{principal['user_id']}#{claims['tool']}#{operation['request_id']}")
        or not isinstance(operation["execution_id"], str)
        or not operation["execution_id"]
        or operation["receipt_id"] != claims["receipt_id"]
        or operation["payload_sha256"] != claims["arguments_sha256"]
        or operation["dispatch_intent"] != "dispatched"
        or operation["connection"] != claims["connection"]
        or operation["release"] != release
        or os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED") != "true"
        or re.fullmatch(r"[a-f0-9]{64}", os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", "")) is None
    ):
        raise HTTPException(403, detail={"error": "adapter_authority_required"})


async def authorize_tool(
    body: ToolAuthorization,
    request: Request,
    db: AsyncSession,
):
    from src.agentauth.routes import get_agent_runtime
    from src.internal.tools_human_authority import current_tools_human
    from src.internal.tools_knowledge import check_fresh, check_policy, verify_host_run

    require_service_operation(request, "tools:adapter:authorize")
    adapter_role = os.environ.get("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", "")
    if not adapter_role or parse_assumed_role_arn(verified_caller_identity(request) or "") != adapter_role:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    _, _, _, release = observation_settings()
    now = int(time.time() * 1000)
    try:
        envelope = json.loads(decode(body.signed_assertion))
        if type(envelope) is not dict or set(envelope) != {"claims", "signature"}:
            raise ValueError("invalid assertion")
        claims = envelope["claims"]
        principal = claims["principal"]
        binding = body.binding
        operation_id = claims["rpc_id"]
        if (
            type(claims) is not dict
            or type(principal) is not dict
            or any(type(principal.get(field)) is not str or not principal[field] for field in ("user_id", "tenant_id", "team_id"))
            or type(binding) is not dict
            or set(binding) != {"operation_id", "receipt_id", "payload_sha256", "release"}
            or any(type(binding[field]) is not str or not 0 < len(binding[field]) <= 512 for field in ("operation_id", "receipt_id"))
            or type(binding["payload_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", binding["payload_sha256"]) is None
            or type(claims.get("expires_at")) is not int
            or binding["release"] != release
            or claims.get("adapter_release") != release
            or claims.get("audience") != "tool-adapters:authorize"
            or claims.get("purpose") != body.purpose
            or claims.get("connector") != body.connector
            or claims.get("caller_role_arn") != os.environ.get("ADP_TOOLS_FACADE_ROLE_ARN")
            or not os.environ.get("ADP_TOOLS_FACADE_ROLE_ARN")
            or claims.get("method") != "tools/call"
            or claims.get("tool") != body.connector + "___" + body.operation
            or (body.connector == "atlassian" and body.operation not in TOOL_SCOPES)
            or (body.connector != "atlassian" and body.operation not in LEGACY_SCOPES.get(body.connector, {}))
            or type(operation_id) not in (str, int)
            or binding["operation_id"] != str(operation_id)
            or binding["receipt_id"] != claims.get("receipt_id")
            or binding["payload_sha256"] != claims.get("arguments_sha256")
            or claims.get("root_human_id") != principal["user_id"]
            or claims.get("native_test") is not None
            or type(body.deadline_at_ms) is not int
            or not now < body.deadline_at_ms <= now + 20_000
            or body.deadline_at_ms > claims["expires_at"] * 1000
        ):
            raise ValueError("unbound tool call")
        check_fresh(claims)
        key = os.environ.get("ADP_TOOLS_ASSERTION_KEY_ARN", "")
        if not key:
            raise ValueError("missing assertion key")
        valid = await asyncio.to_thread(
            boto3.client("kms").verify,
            KeyId=key,
            Message=canonical(claims),
            MessageType="RAW",
            Signature=decode(envelope["signature"]),
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if valid.get("SignatureValid") is not True:
            raise ValueError("invalid signature")
        if claims.get("native_atlassian") is not None:
            if body.connector != "atlassian" or claims.get("host_generation") is not None:
                raise ValueError("unbound native adapter run")
        elif claims.get("run_id") is not None:
            if (
                not all(type(claims.get(field)) is str and claims[field] for field in ("run_id", "workload_id"))
                or type(claims.get("run_expires_at")) is not int
                or type(claims.get("host_generation")) is not int
                or claims["host_generation"] < 1
            ):
                raise ValueError("unbound run")
            await verify_host_run(claims)
        elif any(claims.get(field) is not None for field in ("workload_id", "run_expires_at", "host_generation")):
            raise ValueError("unbound run")
        check_fresh(claims)
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None
    if (
        service_tenant(request, principal["tenant_id"], "tools:adapter:authorize") != principal["tenant_id"]
        or (body.connector != "atlassian" and os.environ.get("ADP_TOOL_ADAPTER_LEGACY_ENABLED") != "true")
        or int(time.time() * 1000) >= body.deadline_at_ms
    ):
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    if body.connector == "atlassian" and body.operation in WRITE_OPERATIONS:
        require_recorded_write(claims, release)
    activation = await db.scalar(select(ToolAdapterActivation).where(ToolAdapterActivation.group == release["group"]).with_for_update(read=True))
    if activation is None or activation.status != "active" or activation.release != release:
        raise HTTPException(403, detail={"error": "adapter_release_fenced"})
    owner = await current_tools_human(db, principal)
    owner_id, tenant_id = owner.id, owner.org_id
    policy_revision = await check_policy(claims, body.operation, db, get_agent_runtime(), namespace=body.connector)
    if body.connector == "atlassian" and body.operation == "get_project_context":
        from src.products.jira_context import read_context, tool_context_connection
        from src.products.jira_context_contract import JiraContextQuery

        if body.context_arguments is None or hashlib.sha256(canonical(body.context_arguments)).hexdigest() != binding["payload_sha256"]:
            raise HTTPException(403, detail={"error": "adapter_authority_required"})
        try:
            query = JiraContextQuery.model_validate(body.context_arguments)
        except ValueError:
            raise HTTPException(422, detail={"error": "invalid_jira_context_query"}) from None
        actor, connection = await tool_context_connection(db, await db.get(User, owner_id, populate_existing=True), query)
        if claims["connection"] != {"id": connection.credential_id, "revision": connection.revision, "site_id": connection.site_id}:
            raise HTTPException(403, detail={"error": "connection_changed"})
        result = await read_context(db, actor, query, deadline_at_ms=body.deadline_at_ms)
        await db.commit()
        return {"jira_context_result": result}
    if body.context_arguments is not None:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    if body.connector == "atlassian" and body.operation in WRITE_OPERATIONS:
        if policy_revision != claims["atlassian_approval"]["policy_revision"]:
            raise HTTPException(403, detail={"error": "approval_changed"})
    legacy = None
    if body.connector == "atlassian":
        credential, current, site, site_scopes, workload = await _current_binding(
            db,
            tenant_id=tenant_id,
            owner_id=owner_id,
            operation=body.operation,
            expected_connection=claims["connection"],
        )
        revision = current.revision
        identity_provider = workload.removesuffix("-oauth-user") + "-user"
    else:
        credential, protected = await current_legacy_adapter_binding(
            db,
            principal=principal,
            connector=body.connector,
            operation=body.operation,
            claims=claims,
        )
        if not isinstance(claims.get("connection_scopes"), list) or LEGACY_SCOPES[body.connector][body.operation] not in claims["connection_scopes"]:
            raise HTTPException(403, detail={"error": "adapter_authority_required"})
        revision = claims["connection"]["revision"]
        site, site_scopes, workload = protected["site_id"], protected["scopes"], protected["workload"]
        identity_provider = protected["provider"]
        legacy = {key: protected[key] for key in ("provider_account_id", "observed_scopes", "resources_sha256", "app_id", "site_scopes")}
    if claims.get("native_atlassian") is not None:
        from src.agentauth.native_test_atlassian_dispatch import NativeAtlassianDispatch

        try:
            await NativeAtlassianDispatch.verify(
                runtime=getattr(request.app.state, "native_test_runtime", None),
                claims=claims,
                operation=body.operation,
                binding=binding,
            )
        except Exception:
            raise HTTPException(403, detail={"error": "native_test_atlassian_dispatch_refused"}) from None
    issued = int(time.time() * 1000)
    if issued >= body.deadline_at_ms:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    admission_id = new_uuid()
    expires = min(issued + 5000, body.deadline_at_ms)
    db.add(
        OAuthToolReceipt(
            id=admission_id,
            expires_at=datetime.fromtimestamp(expires / 1000, UTC),
            authorization={
                "binding": binding,
                "operation": body.operation,
                "connector": body.connector,
                "site_id": site,
                "legacy": protected if legacy is not None else None,
                "run_marker": claims.get("run_marker") if body.connector == "slack" and body.operation == "post_message" else None,
                "principal": principal,
                "connection": claims["connection"],
                **(
                    {"atlassian_approval": claims["atlassian_approval"]}
                    if body.connector == "atlassian" and body.operation in WRITE_OPERATIONS
                    else {}
                ),
                "issued_at": claims["issued_at"],
                "expires_at": claims["expires_at"],
                "deadline_at_ms": body.deadline_at_ms,
                **{
                    field: claims[field]
                    for field in ("root_human_id", "run_id", "workload_id", "run_expires_at", "host_generation", "native_atlassian")
                    if field in claims
                },
            },
        )
    )
    await db.commit()
    # Site and operation permissions were independently checked above. Identity
    # finds the saved grant using the original consent scope set; substituting
    # site scopes starts a different OAuth flow instead of retrieving that grant.
    identity_scopes = credential.scopes["oauth_scopes"] if body.connector == "atlassian" else site_scopes
    return {
        "schema_version": "1.0",
        "admission_id": admission_id,
        "binding": binding,
        "purpose": "tool_call",
        "connector": body.connector,
        "operation": body.operation,
        "identity": {"workload": workload, "provider": identity_provider, "canonical_user_id": owner_id},
        "context": {
            "tenant_id": tenant_id,
            "canonical_user_id": owner_id,
            "connection_id": credential.id,
            "binding_revision": revision,
            "site_id": site,
            "scopes": identity_scopes,
            **({"legacy": legacy} if legacy is not None else {}),
        },
        "issued_at_ms": issued,
        "expires_at_ms": expires,
        "deadline_at_ms": body.deadline_at_ms,
    }


async def _current_binding(db, *, tenant_id, owner_id, operation, expected_connection):
    credential = await db.scalar(
        select(UserCredential)
        .where(
            UserCredential.org_id == tenant_id,
            UserCredential.user_id == owner_id,
            UserCredential.team_id.is_(None),
            UserCredential.domain_app_id.is_(None),
            UserCredential.service == "atlassian-mcp",
            UserCredential.provider == "agentcore-identity",
        )
        .with_for_update(read=True)
    )
    current = await db.get(OAuthConnectionBinding, credential.id) if credential else None
    workload = f"{get_settings().oauth_workload_name.removesuffix('-oauth-user')}-atlassian-mcp-oauth-user"
    site = current.selected_site_id if current else None
    site_scopes = (
        next(
            (resource.get("scopes") for resource in current.observed_resources if isinstance(resource, dict) and resource.get("site_id") == site),
            None,
        )
        if current and isinstance(current.observed_resources, list)
        else None
    )
    required = TOOL_SCOPES[operation]
    if (
        credential is None
        or current is None
        or credential.credential_type != "oauth_token"
        or credential.strict is not True
        or credential.agent_consent is not True
        or credential.expires_at is not None
        or credential.secret_arn != f"agentcore-identity:{workload}:atlassian-mcp"
        or not isinstance(credential.scopes, dict)
        or set(credential.scopes) != {"oauth_scopes"}
        or not isinstance(credential.scopes["oauth_scopes"], list)
        or required not in credential.scopes["oauth_scopes"]
        or current.org_id != tenant_id
        or current.user_id != owner_id
        or current.provider != "atlassian-mcp"
        or current.credential_digest != credential_digest(credential)
        or not site
        or not isinstance(site_scopes, list)
        or not isinstance(current.observed_scopes, list)
        or required not in site_scopes
        or required not in current.observed_scopes
        or expected_connection != {"id": credential.id, "revision": current.revision, "site_id": site}
    ):
        raise HTTPException(403, detail={"error": "connection_changed"})
    if not await verified_connection(
        db, credential=credential, binding=current, config_revision=getattr(get_settings(), "atlassian_metadata_sha256", None)
    ):
        raise HTTPException(403, detail={"error": "atlassian_compatibility_unverified"})
    return credential, current, site, site_scopes, workload


@router.post("/dispatch", dependencies=[Depends(verify_internal_or_irsa)])
async def dispatch_tool(body: ToolDispatch, request: Request, db: AsyncSession = Depends(get_db)):
    from src.agentauth.routes import get_agent_runtime
    from src.internal.tools_human_authority import current_tools_human
    from src.internal.tools_knowledge import check_policy, verify_host_run

    require_service_operation(request, "tools:adapter:dispatch")
    adapter_role = os.environ.get("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", "")
    if not adapter_role or parse_assumed_role_arn(verified_caller_identity(request) or "") != adapter_role:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    _, _, _, release = observation_settings()
    row = await db.scalar(select(OAuthToolReceipt).where(OAuthToolReceipt.id == body.admission_id).with_for_update())
    authority = row.authorization if row else None
    now = int(time.time() * 1000)
    if (
        not isinstance(authority, dict)
        or not isinstance(authority.get("principal"), dict)
        or not isinstance(authority.get("connection"), dict)
        or not isinstance(authority.get("operation"), str)
        or type(authority.get("issued_at")) is not int
        or type(authority.get("expires_at")) is not int
        or type(authority.get("deadline_at_ms")) is not int
        or row.consumed_at is not None
        or row.expires_at.replace(tzinfo=UTC).timestamp() * 1000 <= now
        or body.binding != authority.get("binding")
        or body.binding.get("release") != release
        or body.binding_revision != authority.get("connection", {}).get("revision")
        or authority.get("deadline_at_ms", 0) <= now
    ):
        raise HTTPException(403, detail={"error": "adapter_dispatch_refused"})
    activation = await db.scalar(select(ToolAdapterActivation).where(ToolAdapterActivation.group == release["group"]).with_for_update(read=True))
    if activation is None or activation.status != "active" or activation.release != release:
        raise HTTPException(403, detail={"error": "adapter_release_fenced"})
    expires_at_ms = int(row.expires_at.replace(tzinfo=UTC).timestamp() * 1000)
    principal = authority["principal"]
    if service_tenant(request, principal["tenant_id"], "tools:adapter:dispatch") != principal["tenant_id"]:
        raise HTTPException(403, detail={"error": "adapter_dispatch_refused"})
    owner = await current_tools_human(db, principal)
    owner_id, tenant_id = owner.id, owner.org_id
    if authority.get("run_id") is not None and authority.get("native_atlassian") is None:
        try:
            await verify_host_run(authority)
        except Exception:
            raise HTTPException(403, detail={"error": "adapter_dispatch_refused"}) from None
    connector = authority.get("connector", "atlassian")
    if connector != "atlassian" and (
        connector not in LEGACY_SCOPES
        or authority["operation"] not in LEGACY_SCOPES[connector]
        or os.environ.get("ADP_TOOL_ADAPTER_LEGACY_ENABLED") != "true"
    ):
        raise HTTPException(403, detail={"error": "adapter_dispatch_refused"})
    policy_revision = await check_policy(authority, authority["operation"], db, get_agent_runtime(), namespace=connector)
    if connector == "atlassian" and authority["operation"] in WRITE_OPERATIONS:
        if not isinstance(authority.get("atlassian_approval"), dict) or policy_revision != authority["atlassian_approval"].get("policy_revision"):
            raise HTTPException(403, detail={"error": "approval_changed"})
    if connector == "atlassian":
        await _current_binding(
            db,
            tenant_id=tenant_id,
            owner_id=owner_id,
            operation=authority["operation"],
            expected_connection=authority["connection"],
        )
    else:
        connection = authority["connection"]
        _, current = await current_legacy_adapter_binding(
            db,
            principal=principal,
            connector=connector,
            operation=authority["operation"],
            claims={
                "connection": connection,
                connector + "_connection": connection,
                "connection_id": connection["connection_id"],
                "site_id": authority.get("site_id"),
            },
        )
        expected = authority.get("legacy")
        if not isinstance(expected, dict) or any(
            current[key] != expected.get(key)
            for key in (
                "provider_account_id",
                "scopes",
                "observed_scopes",
                "resources_sha256",
                "app_id",
                "site_scopes",
                "workload",
                "provider",
                "site_id",
            )
        ):
            raise HTTPException(403, detail={"error": "connection_changed"})
    if authority.get("native_atlassian") is not None:
        from src.agentauth.native_test_atlassian_dispatch import NativeAtlassianDispatch

        if connector != "atlassian":
            raise HTTPException(403, detail={"error": "native_test_atlassian_dispatch_refused"})
        try:
            native = await NativeAtlassianDispatch.verify(
                runtime=getattr(request.app.state, "native_test_runtime", None),
                claims=authority,
                operation=authority["operation"],
                binding=body.binding,
            )
            await asyncio.to_thread(native.claim, body.admission_id)
        except Exception:
            raise HTTPException(403, detail={"error": "native_test_atlassian_dispatch_refused"}) from None
    consumed = int(time.time() * 1000)
    if consumed >= authority["deadline_at_ms"] or consumed >= expires_at_ms:
        raise HTTPException(403, detail={"error": "adapter_dispatch_refused"})
    receipt_digest = hashlib.sha256(
        receipt_canonical(
            {
                "principal": principal,
                "receipt": body.binding["receipt_id"],
                "tool": connector + "___" + authority["operation"],
            }
        )
    ).hexdigest()
    db.add(OAuthToolReceipt(id=receipt_digest, expires_at=datetime.fromtimestamp(authority["expires_at"], UTC)))
    if connector == "slack" and authority["operation"] == "post_message":
        from src.internal.slack_fixture import approved_post_fixture

        fixture = approved_post_fixture(owner_id=owner_id, team_id=current["provider_account_id"].split(":", 1)[0])
        if authority.get("run_marker") != fixture.run_marker:
            raise HTTPException(403, detail={"error": "slack_fixture_denied"})
        marker_digest = hashlib.sha256(
            receipt_canonical(
                {
                    "tool": "slack___post_message",
                    "owner_id": owner_id,
                    "run_marker": fixture.run_marker,
                }
            )
        ).hexdigest()
        db.add(OAuthToolReceipt(id=marker_digest, expires_at=datetime.max.replace(tzinfo=UTC)))
    row.consumed_at = datetime.fromtimestamp(consumed / 1000, UTC)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"error": "adapter_dispatch_reused"}) from None
    return {
        "schema_version": "1.0",
        "admission_id": body.admission_id,
        "binding": body.binding,
        "binding_revision": body.binding_revision,
        "consumed_at_ms": consumed,
        "deadline_at_ms": authority["deadline_at_ms"],
    }


class AtlassianReadCatalogue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    release: dict
    tools: list[dict] = Field(min_length=4, max_length=9)


@router.post("/catalogue", dependencies=[Depends(verify_internal_or_irsa)])
async def publish_atlassian_reads(body: AtlassianReadCatalogue, request: Request):
    from src.internal.tools_dynamic_catalogue import atlassian_tool_names, publish_catalogue, validate_tools
    from src.internal.tools_dynamic_catalogue import canonical as catalogue_canonical
    from src.internal.tools_knowledge import current_binding

    require_service_operation(request, "tools:adapter:observe")
    adapter_role = os.environ.get("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", "")
    if not adapter_role or parse_assumed_role_arn(verified_caller_identity(request) or "") != adapter_role:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    _, _, _, release = observation_settings()
    writes = os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED") == "true"
    expected = os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_SHA256" if writes else "ADP_TOOLS_ATLASSIAN_READS_SHA256", "")
    names = atlassian_tool_names()
    try:
        tools = validate_tools(body.tools, namespace="atlassian")
        if (
            body.release != release
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or {tool["name"] for tool in tools} != names
            or hashlib.sha256(catalogue_canonical(tools)).hexdigest() != expected
            or release["source_revision"] != os.environ.get("ADP_TOOLS_ATLASSIAN_SOURCE_REVISION")
        ):
            raise ValueError("catalogue not approved")
        binding = await asyncio.to_thread(current_binding, namespace="atlassian", metadata_only=True)
        table = os.environ["AGENT_AUTHORITY_TABLE"]
        await asyncio.to_thread(
            publish_catalogue,
            binding=binding,
            tools=tools,
            client=boto3.client("dynamodb"),
            table_name=table,
        )
    except Exception:
        raise HTTPException(503, detail={"error": "catalogue_incomplete"}) from None
    return {"status": "ready", "tools_sha256": expected}


class LegacyReadCatalogue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    release: dict
    connector: Literal["github", "jira", "slack"]
    tools: list[dict] = Field(min_length=1, max_length=2)


@router.post("/catalogue/legacy", dependencies=[Depends(verify_internal_or_irsa)])
async def publish_legacy_reads(body: LegacyReadCatalogue, request: Request):
    from src.internal.tools_dynamic_catalogue import publish_catalogue, validate_tools
    from src.internal.tools_knowledge import current_binding

    require_service_operation(request, "tools:adapter:observe")
    adapter_role = os.environ.get("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", "")
    if not adapter_role or parse_assumed_role_arn(verified_caller_identity(request) or "") != adapter_role:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    _, _, _, release = observation_settings()
    if os.environ.get("ADP_TOOL_ADAPTER_LEGACY_ENABLED") != "true" or body.release != release:
        raise HTTPException(403, detail={"error": "adapter_release_fenced"})
    try:
        from src.internal.slack_fixture import configured_post_fixture
        from src.internal.tools_github import TOOLS as GITHUB_TOOLS
        from src.internal.tools_jira import TOOLS as JIRA_TOOLS
        from src.internal.tools_slack import TOOLS as SLACK_TOOLS

        expected = {"github": GITHUB_TOOLS, "jira": JIRA_TOOLS, "slack": SLACK_TOOLS}[body.connector]
        if body.connector == "slack" and len(body.tools) == 1:
            try:
                configured_post_fixture()
            except HTTPException as error:
                if error.status_code != 403:
                    raise
            else:
                raise ValueError("approved write missing from catalogue")
            expected = expected[:1]
        elif body.connector == "slack":
            configured_post_fixture()
        tools = validate_tools(body.tools, namespace=body.connector)
        if tools != expected or release["source_revision"] != os.environ.get(f"ADP_TOOLS_{body.connector.upper()}_SOURCE_REVISION"):
            raise ValueError("catalogue changed")
        binding = await asyncio.to_thread(current_binding, namespace=body.connector, metadata_only=True)
        await asyncio.to_thread(
            publish_catalogue,
            binding=binding,
            tools=tools,
            client=boto3.client("dynamodb"),
            table_name=os.environ["AGENT_AUTHORITY_TABLE"],
        )
    except Exception:
        raise HTTPException(503, detail={"error": "catalogue_incomplete"}) from None
    return {"status": "ready"}


async def current_atlassian_read(db, *, tenant_id, owner_id, operation, arguments=None):
    if operation == "get_project_context":
        from src.products.jira_context import tool_context_connection
        from src.products.jira_context_contract import JiraContextQuery

        user = await db.get(User, owner_id, populate_existing=True)
        if user is None or user.org_id != tenant_id:
            raise HTTPException(403, detail={"error": "adapter_authority_required"})
        try:
            _, connection = await tool_context_connection(db, user, JiraContextQuery.model_validate(arguments) if arguments is not None else None)
        except (HTTPException, ValueError):
            raise HTTPException(403, detail={"error": "jira_context_source_unavailable"}) from None
        return {"id": connection.credential_id, "revision": connection.revision, "site_id": connection.site_id}, [
            "read:jira:agent-interface",
            "search:jira:agent-interface",
        ]
    if operation not in TOOL_SCOPES or operation in WRITE_OPERATIONS and os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED") != "true":
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    credential = await db.scalar(
        select(UserCredential).where(
            UserCredential.org_id == tenant_id,
            UserCredential.user_id == owner_id,
            UserCredential.team_id.is_(None),
            UserCredential.domain_app_id.is_(None),
            UserCredential.service == "atlassian-mcp",
            UserCredential.provider == "agentcore-identity",
        )
    )
    binding = await db.get(OAuthConnectionBinding, credential.id) if credential else None
    if binding is None or not binding.selected_site_id:
        raise HTTPException(403, detail={"error": "connection_changed"})
    connection = {"id": credential.id, "revision": binding.revision, "site_id": binding.selected_site_id}
    _, _, _, site_scopes, _ = await _current_binding(
        db,
        tenant_id=tenant_id,
        owner_id=owner_id,
        operation=operation,
        expected_connection=connection,
    )
    return connection, list(site_scopes)
