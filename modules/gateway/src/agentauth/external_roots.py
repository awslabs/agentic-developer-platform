"""Protected model-policy roots from explicitly registered ingress identities.

Workers cannot call this surface. A producer's STS proof covers the entire
request digest, and deployment-owned bindings restrict the producer to a source
and tenant. Human ownership is resolved in the tenant's canonical directory.
The returned envelope is final: its digest is persisted before publication.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, envelope_digest
from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.agentauth.model_policy import ModelPolicyError, canonical_json, ensure_snapshot_report_only, registered_compatibility_class
from src.agentauth.store import AuthorityStoreError
from src.agentauth.work_routes import PROOF_HEADER, verify_producer
from src.shared.database import get_db
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity


class RootBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["chat", "gitlab"]
    producer_role: str = Field(pattern=r"^arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    tenant_id: str = Field(min_length=1, max_length=255)
    # GitLab registration is instance AND immutable project qualified. Neither
    # project path nor username is an identity. Chat has no external project.
    instance: str = ""
    project_id: int = Field(default=0, ge=0)
    repo: str = ""
    personas: frozenset[str]


class RootAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["chat", "gitlab"]
    envelope: dict
    # Chat supplies its authenticated subject, GitLab its immutable numeric ID.
    subject: str = Field(min_length=1, max_length=255)
    instance: str = ""
    project_id: int = Field(default=0, ge=0)


def root_bindings() -> list[RootBinding]:
    try:
        raw = os.environ.get("ADP_MODEL_ROOT_BINDINGS", "[]")
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError()
        return [RootBinding.model_validate(row) for row in value]
    except (ValueError, TypeError):
        raise HTTPException(503, "model root registration unavailable") from None


def root_store() -> BootstrapStore:
    import boto3

    table = os.environ.get("AGENT_AUTHORITY_TABLE")
    if not table:
        raise HTTPException(503, "model root registration unavailable")
    return BootstrapStore(table_name=table, dynamodb_client=boto3.client("dynamodb"))


async def canonical_human(session: AsyncSession, binding: RootBinding, subject: str) -> str:
    query = select(User).where(User.org_id == binding.tenant_id, User.user_kind == "human")
    if binding.source == "chat":
        # WebSocket/Slack ingestion already authenticated and resolved this
        # subject. There is no username or arbitrary external-ID fallback.
        query = query.where(or_(User.id == subject, User.cognito_sub == subject))
    else:
        if not subject.isascii() or not subject.isdecimal() or int(subject) < 1:
            raise HTTPException(403, "model root refused")
        query = query.join(UserIdentity, UserIdentity.user_id == User.id).where(
            UserIdentity.org_id == binding.tenant_id,
            UserIdentity.provider == "gitlab",
            UserIdentity.provider_user_id == f"{binding.instance}#{subject}",
            UserIdentity.verified_at.is_not(None),
        )
    users = (await session.scalars(query)).all()
    if len(users) != 1:
        raise HTTPException(403, "model root refused")
    return users[0].id


def provision_root(store: BootstrapStore, envelope: dict, *, source: str, human_id: str, now: datetime, service_identity: str | None = None) -> None:
    """Self-monitoring model authority; this never grants dispatch or control."""
    invocation, tenant = envelope["message_id"], envelope["tenant_id"]
    repo = envelope["source_ref"]["repo"]
    created = datetime.fromisoformat(envelope["arrived_at"].replace("Z", "+00:00"))
    if created.tzinfo is None or not now - timedelta(minutes=15) <= created <= now + timedelta(seconds=30):
        raise BootstrapRefusedError("stale root event")
    expiry = created + timedelta(hours=2)
    reference = f"{source}-event:{envelope_digest(envelope)}"
    kind = "service_policy" if service_identity else f"{source}_event"
    authority = {
        "pk": {"S": f"TENANT#{tenant}"},
        "sk": {"S": f"AUTHORITY#{reference}"},
        "authority_kind": {"S": kind},
        "human_id": {"S": human_id},
        "actor_kind": {"S": "human"},
        "status": {"S": "active"},
        "expires_at": {"S": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")},
    }
    if service_identity:
        authority["service_identity"] = {"S": service_identity}
    try:
        store.client.put_item(TableName=store.table, Item=authority, ConditionExpression="attribute_not_exists(pk)")
    except (ClientError, BotoCoreError):
        if store._read(f"TENANT#{tenant}", f"AUTHORITY#{reference}") != authority:
            raise BootstrapRefusedError("root authority unavailable") from None
    grant = DelegatedGrant(
        grant_id=f"root-{invocation}",
        tenant_id=tenant,
        principal=f"{invocation}#1",
        authority=AuthorityReference(kind, reference, human_id, tenant),
        allowed_actions=frozenset({AgentAction.MONITOR}),
        target_relationships=frozenset({TargetRelationship.SELF}),
        repo_scope=frozenset({repo}),
        flow_id=invocation,
        expires_at=expiry,
    )
    store.provision_pending(envelope=envelope, grant=grant, now=now)


router = APIRouter(prefix="/internal/v1/agent/roots", tags=["agent-authority"])


@router.post("/admit")
async def admit_root(
    body: RootAdmission,
    request: Request,
    session: AsyncSession = Depends(get_db),
    store: BootstrapStore = Depends(root_store),
):
    if len(await request.body()) > 65536:
        raise HTTPException(413, "root request too large")
    bindings = root_bindings()
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest(body.model_dump(mode="json")),
        allowed_roles={binding.producer_role for binding in bindings},
    )
    envelope = dict(body.envelope)
    persona = envelope.get("persona") or envelope.get("agent_type")
    candidates = [
        binding
        for binding in bindings
        if binding.source == body.source
        and binding.producer_role == role
        and binding.instance == body.instance
        and binding.project_id == body.project_id
        and (body.source != "chat" or binding.tenant_id == envelope.get("tenant_id"))
        and persona in binding.personas
    ]
    if len(candidates) != 1:
        raise HTTPException(403, "model root refused")
    binding = candidates[0]
    try:
        compatibility = registered_compatibility_class(persona)
        if body.source == "chat" and compatibility != "claude-agent-sdk":
            raise ModelPolicyError("persona_incompatible")
        if body.source == "gitlab":
            from src.gitlab.service import require_managed_association

            await require_managed_association(session, binding)
        human_id = await canonical_human(session, binding, body.subject)
        invocation = envelope["message_id"]
        if not isinstance(invocation, str) or not 1 <= len(invocation) <= 128:
            raise ValueError()
        if body.source == "chat":
            session_id = envelope["session_id"]
            if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
                raise ValueError()
            repo = f"chat/{session_id}"
        else:
            repo = binding.repo
            if not repo:
                raise ValueError()
        envelope.update(tenant_id=binding.tenant_id, persona=persona)
        envelope["source_ref"] = {**envelope.get("source_ref", {}), "repo": repo}
        envelope["correlation"] = {"correlation_id": invocation, "root_human_id": human_id, "is_human_rooted": True}
        if body.source == "gitlab":
            envelope["actor"] = {**envelope.get("actor", {}), "user_id": human_id, "org_id": binding.tenant_id, "is_bot": False}
        # A root cannot claim to inherit somebody else's snapshot or authority.
        envelope.pop("parent_principal", None)
        await run_in_threadpool(provision_root, store, envelope, source=body.source, human_id=human_id, now=datetime.now(UTC))
        snapshot = await ensure_snapshot_report_only(session, store=store, invocation_id=invocation)
        # TS consumers hash these exact canonical bytes; reparsing arbitrary
        # metadata would lose distinctions such as Python's 1.0 spelling.
        from starlette.responses import JSONResponse

        return JSONResponse(
            {"envelope": envelope, "envelope_json": canonical_json(envelope).decode(), "model_policy_snapshot": snapshot},
            headers={"Cache-Control": "no-store"},
        )
    except HTTPException:
        raise
    except (BootstrapRefusedError, AuthorityStoreError, ModelPolicyError, ValueError, KeyError, TypeError):
        raise HTTPException(403, "model root refused") from None
