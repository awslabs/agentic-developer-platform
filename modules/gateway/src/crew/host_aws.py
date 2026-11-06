"""User-owned AWS connections for a live Dev Box; delivery uses the existing STS broker."""

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.responses import JSONResponse

from src.auth.aws_connection_authority import verified_connection_evidence
from src.crew import model_token
from src.crew.host_routes import host_run_route
from src.crew.host_tools import current_host_target
from src.internal.assume_role_routes import AssumeRoleRequestBody, assume_bound_role, get_secrets_manager
from src.internal.credential_binding import BindingResult
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.database import get_session_factory
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserCredential
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["crew"])
NO_STORE = {"Cache-Control": "no-store"}


def refused(code, status=403):
    return HTTPException(status, detail={"error": code}, headers=NO_STORE)


class AwsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(pattern="^(list|assume)$")
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    label: str | None = Field(default=None, min_length=1, max_length=255)


async def owner_connections(db, owner):
    # Every issuance rechecks current membership independently of host enrollment.
    member = await db.scalar(
        select(User.id)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .where(
            User.id == owner.user_id,
            User.org_id == owner.tenant_id,
            User.user_kind == "human",
            User.is_shadow.is_(False),
            TenantMembership.tenant_id == owner.tenant_id,
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
        )
    )
    if member is None:
        raise refused("aws_owner_membership_required")
    return list(
        (
            await db.scalars(
                select(UserCredential)
                .where(
                    UserCredential.org_id == owner.tenant_id,
                    UserCredential.user_id == owner.user_id,
                    UserCredential.team_id.is_(None),
                    UserCredential.domain_app_id.is_(None),
                    UserCredential.service == "aws",
                    UserCredential.credential_type == "aws_role",
                )
                .order_by(UserCredential.created_at, UserCredential.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


def usable(credential):
    if credential.agent_consent is not True:
        raise refused("aws_consent_required")
    if (credential.scopes or {}).get("permission_tier") == "deploy-bootstrap":
        raise refused("aws_bootstrap_connection_requires_explicit_workflow")
    verified_connection_evidence(credential)


@router.post(host_run_route("/crew/aws-credentials"))
async def host_aws_credentials(
    body: AwsRequest,
    request: Request,
    context: Annotated[TokenContext, Depends(get_token_context)],
    run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    token: str | None = Header(default=None, alias="X-Adp-Crew-Model-Token"),
    sm=Depends(get_secrets_manager),
):
    now = int(time.time())
    owner, target = await current_host_target(context, run_id, now=now)
    claims = await model_token.verify_model_token(token or "", target, now=now)
    if claims["run"] != run_id:
        raise refused("aws_host_identity_required")
    if body.connection_id is not None and body.label is not None or body.operation == "list" and (body.connection_id or body.label):
        raise refused("aws_invalid_selection", 400)

    async with get_session_factory()() as db:
        connections = await owner_connections(db, owner)
        if body.operation == "list":
            available = []
            for credential in connections:
                try:
                    usable(credential)
                except HTTPException:
                    continue
                available.append({"id": credential.id, "label": credential.label, "account_id": credential.scopes["account_id"]})
            return JSONResponse({"connections": available}, headers=NO_STORE)

        selected = [
            c for c in connections if (body.connection_id is None or c.id == body.connection_id) and (body.label is None or c.label == body.label)
        ]
        if not selected:
            raise refused("aws_not_connected", 404)
        # No fallback to another connection or a team/org role, including after revocation.
        if len(selected) != 1:
            raise refused("aws_select_connection", 409)
        credential = selected[0]
        usable(credential)
        selected_id = credential.id

        async def validate(current, **_):
            fresh_owner, fresh_target = await current_host_target(context, run_id, now=int(time.time()))
            if (
                fresh_owner != owner
                or fresh_target.host_instance_ref != target.host_instance_ref
                or fresh_target.lease.generation != target.lease.generation
            ):
                raise refused("aws_host_identity_changed")
            fresh = next((c for c in await owner_connections(db, owner) if c.id == selected_id), None)
            if fresh is None or current.id != selected_id:
                raise refused("aws_not_connected", 404)
            usable(fresh)

        result = await assume_bound_role(
            AssumeRoleRequestBody(
                user_id=owner.user_id, agent_id="dev-box", task_id=run_id, label=credential.label, purpose="Owner Dev Box AWS access"
            ),
            request,
            db,
            sm,
            binding=BindingResult(owner.user_id, False, False, owner.user_id, tenant_id=owner.tenant_id),
            validate=validate,
            selected_credential=credential,
            audit_fields={
                "origin_kind": "dev-box",
                "box_id": target.lease.target_ref,
                "instance_id": target.host_instance_ref,
                "generation": target.lease.generation,
                "host_run_id": run_id,
            },
        )
        expiration = datetime.fromisoformat(result.expiration.replace("Z", "+00:00"))
        if expiration.tzinfo is None or not datetime.now(UTC) < expiration:
            raise refused("aws_session_expired", 502)
        return JSONResponse(
            {
                "Version": 1,
                "AccessKeyId": result.access_key_id,
                "SecretAccessKey": result.secret_access_key,
                "SessionToken": result.session_token,
                "Expiration": result.expiration,
            },
            headers=NO_STORE,
        )
