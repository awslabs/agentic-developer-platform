"""GitHub-signed workflow identity selects a protected ARC model-policy root.

Deployment registrations pin immutable repository ID, workflow ref and runner
role. A username, installation token, workflow input or caller-supplied persona
cannot select the preference owner. Every launch re-proves both AWS and GitHub
identity before the gateway signs its short-lived decision.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.external_roots import provision_root, root_store
from src.agentauth.model_policy import ModelPolicyError, _resolve_principal, ensure_snapshot_report_only, registered_compatibility_class
from src.agentauth.routes import AgentRuntime, ModelDecisionRequest, resolved_model_response
from src.agentauth.store import AuthorityStoreError
from src.agentauth.work_routes import PROOF_HEADER, verify_producer
from src.agentauth.workload import VerifiedPod
from src.shared.database import get_db
from src.shared.models.organization import User
from src.shared.models.persona_models import ServicePrincipalAlias
from src.shared.models.vault import UserIdentity

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "adp-agent-model-policy"


class ArcBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repository_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    # Exact trusted workflow ref, e.g. org/repo/.github/workflows/agent-pm.yml@refs/heads/main.
    workflow_ref: str = Field(min_length=1, max_length=512)
    job_workflow_ref: str | None = Field(default=None, max_length=512)
    runner_role: str = Field(pattern=r"^arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    tenant_id: str = Field(min_length=1, max_length=255)
    persona: str = Field(min_length=1, max_length=64)
    # Required for service-triggered runs; this is a registered alias, never the preference owner itself.
    service_identity: str | None = Field(default=None, pattern=r"^github_actions:.{1,230}$")


class ArcModelRequest(ModelDecisionRequest):
    github_oidc_token: str = Field(min_length=1, max_length=16384)


def bindings() -> list[ArcBinding]:
    try:
        raw = os.environ.get("ADP_ARC_MODEL_BINDINGS", "[]")
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError()
        return [ArcBinding.model_validate(item) for item in value]
    except (ValueError, TypeError):
        raise HTTPException(503, "ARC model registration unavailable") from None


async def github_claims(token: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError()
        async with httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False) as client:
            response = await client.get(ISSUER + "/.well-known/jwks")
            response.raise_for_status()
            if len(response.content) > 65536:
                raise ValueError()
            keys = [key for key in response.json()["keys"] if key.get("kid") == header["kid"] and key.get("kty") == "RSA"]
        if len(keys) != 1:
            raise ValueError()
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(keys[0], algorithm="RS256").key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={
                "require": [
                    "exp",
                    "iat",
                    "nbf",
                    "sub",
                    "repository",
                    "repository_id",
                    "workflow_ref",
                    "run_id",
                    "run_attempt",
                    "actor_id",
                    "event_name",
                ]
            },
        )
        if claims["exp"] - claims["iat"] > 600:
            raise ValueError()
        for name in ("repository_id", "run_id", "run_attempt", "actor_id"):
            value = claims[name]
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or not 1 <= len(value) <= 20 or int(value) < 1:
                raise ValueError()
        return claims
    except (ValueError, TypeError, KeyError, jwt.PyJWTError, httpx.HTTPError):
        raise HTTPException(403, "ARC identity refused") from None


async def human_owner(session, *, tenant, actor_id) -> str:
    owners = (
        await session.scalars(
            select(User.id)
            .join(UserIdentity, UserIdentity.user_id == User.id)
            .where(
                User.org_id == tenant,
                User.user_kind == "human",
                UserIdentity.org_id == tenant,
                UserIdentity.provider == "github",
                UserIdentity.provider_user_id == actor_id,
                UserIdentity.verified_at.is_not(None),
            )
        )
    ).all()
    if len(owners) != 1:
        raise HTTPException(403, "ARC human identity unavailable")
    return owners[0]


router = APIRouter(prefix="/internal/v1/agent/arc", tags=["agent-authority"])


@router.post("/model-decision")
async def arc_model_decision(body: ArcModelRequest, request: Request, db: AsyncSession = Depends(get_db), store=Depends(root_store)):
    registrations = bindings()
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""), envelope_digest(body.model_dump()), allowed_roles={entry.runner_role for entry in registrations}
    )
    claims = await github_claims(body.github_oidc_token)
    matches = [
        entry
        for entry in registrations
        if entry.runner_role == role
        and entry.repository_id == claims["repository_id"]
        and entry.repository == claims["repository"]
        and entry.workflow_ref == claims["workflow_ref"]
        and entry.job_workflow_ref == claims.get("job_workflow_ref")
    ]
    if len(matches) != 1:
        raise HTTPException(403, "ARC workflow not registered")
    binding = matches[0]
    try:
        if registered_compatibility_class(binding.persona) != "claude-agent-sdk":
            raise ModelPolicyError("persona_incompatible")
    except ModelPolicyError:
        raise HTTPException(403, "ARC harness incompatible") from None
    # Reusable workflows are service-triggered even when the caller's original
    # event was human. An App/worker actor never stands in for that principal.
    human = claims["event_name"] in {"issues", "issue_comment", "workflow_dispatch"} and not claims.get("job_workflow_ref")
    if human:
        owner = await human_owner(db, tenant=binding.tenant_id, actor_id=claims["actor_id"])
    else:
        if not binding.service_identity:
            raise HTTPException(403, "ARC service identity unavailable")
        # Preserve the canonical registration actor for audit only. The active
        # service alias below supplies the preference owner, never this human.
        owner = await db.scalar(
            select(User.id)
            .join(ServicePrincipalAlias, ServicePrincipalAlias.registered_by == User.id)
            .where(
                User.org_id == binding.tenant_id,
                User.user_kind == "human",
                ServicePrincipalAlias.org_id == binding.tenant_id,
                ServicePrincipalAlias.alias_source == "github_actions",
                ServicePrincipalAlias.alias_id == binding.service_identity,
                ServicePrincipalAlias.is_active.is_(True),
            )
        )
        if not owner:
            raise HTTPException(403, "ARC service registration unavailable")
    identity = {
        name: claims.get(name) for name in ("repository_id", "run_id", "run_attempt", "workflow_ref", "job_workflow_ref", "actor_id", "event_name")
    }
    invocation = "arc-" + hashlib.sha256(canonical_identity(identity)).hexdigest()[:48]
    try:
        now = datetime.now(UTC)
        existing = await run_in_threadpool(store._read, f"TENANT#{binding.tenant_id}", f"EXEC#{invocation}")
        envelope = {
            "message_id": invocation,
            "tenant_id": binding.tenant_id,
            "persona": binding.persona,
            "arrived_at": existing["arrived_at"]["S"] if existing else now.isoformat(),
            "source_ref": {"repo": binding.repository},
            "github_actions": identity,
            "correlation": {"correlation_id": invocation},
        }
        if not existing:
            await run_in_threadpool(
                provision_root,
                store,
                envelope,
                source="github_actions",
                human_id=owner,
                now=now,
                service_identity=None if human else binding.service_identity,
            )
            # Resolve the registered service alias before accepting even a
            # report-only root; failed identity is not a model proposal failure.
            grant = await run_in_threadpool(store.live_grant, invocation_id=invocation, tenant_id=binding.tenant_id, attempt=1, now=now)
            authority = await run_in_threadpool(store._read, f"TENANT#{binding.tenant_id}", f"AUTHORITY#{grant.authority.reference_id}")
            await _resolve_principal(db, tenant_id=binding.tenant_id, grant=grant, authority=authority)
            await ensure_snapshot_report_only(db, store=store, invocation_id=invocation)
        grant = await run_in_threadpool(store.live_grant, invocation_id=invocation, tenant_id=binding.tenant_id, attempt=1, now=now)
        if grant.authority.human_id != owner or grant.authority.kind != ("github_actions_event" if human else "service_policy"):
            raise BootstrapRefusedError("ARC root changed")
        authority = await run_in_threadpool(store._read, f"TENANT#{binding.tenant_id}", f"AUTHORITY#{grant.authority.reference_id}")
        if not human and authority.get("service_identity") != {"S": binding.service_identity}:
            raise BootstrapRefusedError("ARC service changed")
        await _resolve_principal(db, tenant_id=binding.tenant_id, grant=grant, authority=authority)
        # This is a GitHub-verified job identity, not a caller-selected pod ID.
        job = VerifiedPod(invocation, invocation, "github-actions", role, "github-actions")
        record = await run_in_threadpool(store.bind, invocation_id=invocation, digest=envelope_digest(envelope), pod=job, now=now)
        runtime = AgentRuntime(store=store, workloads=None)
        result = await resolved_model_response(
            db=db,
            runtime=runtime,
            record=record,
            grant=grant,
            nonce=body.nonce,
            client_contract=body.model_policy_contract,
            response_context={"persona": binding.persona, "github_actions": identity},
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (BootstrapRefusedError, AuthorityStoreError, ModelPolicyError, ValueError, KeyError, TypeError):
        raise HTTPException(403, "ARC model authority refused") from None


def canonical_identity(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
