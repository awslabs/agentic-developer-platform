"""CLI machine-agent adapter with durable admission before provider creation."""

import base64
import hashlib
import json
import uuid
from typing import Annotated, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.agent_registry_schemas import AgentRegistryCreateRequest, AgentRegistryUpdateRequest
from src.admin.agent_registry_service import AgentRegistryService
from src.admin.agent_schemas import AgentCreateRequest, AgentUpdateRequest
from src.admin.agent_service import AgentService
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.exceptions import ConflictError, NotFoundError
from src.shared.models.audit import AuditLog
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/machine-agents", route_class=AuditedAdminRoute)
Kind = Literal["iam-registry", "cognito-client"]
Db = Annotated[AsyncSession, Depends(get_db)]
Actor = Annotated[TokenContext, Depends(get_current_user)]


class Register(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    org_id: str = Field(min_length=1, max_length=255)
    agent: dict


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str = Field(min_length=1, max_length=255)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent: dict = Field(default_factory=dict)
    deregister: bool = False
    operation_id: uuid.UUID | None = None


def service(kind):
    return AgentRegistryService() if kind == "iam-registry" else AgentService()


async def authorize(db, actor, org, kind, *, write=False):
    if actor.account_type != "human" or actor.auth_source != "jwt" or actor.org_id != org:
        raise HTTPException(403, detail={"error": "human_tenant_required"})
    permission = Permission.AGENT_REGISTER if kind == "iam-registry" and write else Permission.ORG_UPDATE if write else Permission.ORG_READ
    await AccessControl(db).check_permission(actor, permission, target_org_id=org)


async def read(provider, kind, org, key):
    try:
        row = await provider.get_agent(key) if kind == "iam-registry" else await provider.get_agent(key, org)
    except NotFoundError:
        raise HTTPException(404, detail={"error": "agent_not_found"}) from None
    value = row.model_dump(mode="json")
    if value["org_id"] != org:
        raise HTTPException(404, detail={"error": "agent_not_found"})
    value["identity_type"] = kind
    value["id"] = value["agent_id" if kind == "iam-registry" else "client_id"]
    value["revision"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def schema(model, data):
    if set(data) - set(model.model_fields):
        raise HTTPException(422, detail={"error": "unknown_agent_fields"})
    try:
        return model.model_validate(data)
    except ValidationError:
        raise HTTPException(422, detail={"error": "invalid_agent_metadata"}) from None


@router.get("/{kind}/{key}/identity")
async def get_machine_agent(kind: Kind, key: str, org_id: str, db: Db, current_user: Actor):
    await authorize(db, current_user, org_id, kind)
    return await read(service(kind), kind, org_id, key)


@router.get("/cognito-client/page")
async def list_cognito_machine_agents(
    org_id: str, db: Db, current_user: Actor, page_size: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None
):
    await authorize(db, current_user, org_id, "cognito-client")
    provider = service("cognito-client")
    kwargs = {
        "IndexName": "org_id-index",
        "KeyConditionExpression": "org_id = :org",
        "ExpressionAttributeValues": {":org": org_id},
        "Limit": page_size,
    }
    if cursor:
        try:
            key = json.loads(base64.urlsafe_b64decode(cursor))
            if not isinstance(key, dict) or set(key) != {"org_id", "client_id"} or key["org_id"] != org_id or not isinstance(key["client_id"], str):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(422, detail={"error": "invalid_cursor"}) from None
        kwargs["ExclusiveStartKey"] = key
    result = provider.dynamodb.Table(provider.table_name).query(**kwargs)
    # Strict read projection prevents future secret metadata fields escaping.
    fields = ("client_id", "org_id", "name", "team_id", "department_id", "description", "scopes", "created_at", "updated_at", "status")
    rows = [{key: row.get(key) for key in fields} for row in result.get("Items", []) if row.get("org_id") == org_id]
    following = result.get("LastEvaluatedKey")
    return {"items": rows, "org_id": org_id, "next_cursor": base64.urlsafe_b64encode(json.dumps(following).encode()).decode() if following else None}


@router.post("/{kind}/register")
async def register_machine_agent(kind: Kind, request: Register, db: Db, current_user: Actor):
    await authorize(db, current_user, request.org_id, kind, write=True)
    if request.agent.get("org_id", request.org_id) != request.org_id:
        raise HTTPException(422, detail={"error": "agent_scope_mismatch"})
    model = AgentRegistryCreateRequest if kind == "iam-registry" else AgentCreateRequest
    data = schema(model, {**request.agent, "org_id": request.org_id})
    # Avoid a second resource (automatic budget) in an uncertain registration.
    if kind == "iam-registry" and data.budget_monthly_usd is not None:
        raise HTTPException(422, detail={"error": "use_existing_budget_config"})
    natural_key = data.role_arn if kind == "iam-registry" else request.org_id + ":" + data.name.lower()
    receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"adp:machine-agent:{kind}:{natural_key}"))
    completed_id = str(uuid.uuid5(uuid.UUID(receipt_id), "completed"))
    fingerprint = hashlib.sha256(json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    admitted = await db.get(AuditLog, receipt_id)
    if admitted is None:
        try:
            admitted = AuditLog(
                id=receipt_id,
                org_id=request.org_id,
                actor_id=current_user.user_id,
                event_type="machine_agent_registration_started",
                details={"fingerprint": fingerprint},
            )
            db.add(admitted)
            await db.commit()  # Durable before any external provider mutation.
        except IntegrityError:
            await db.rollback()
            admitted = await db.get(AuditLog, receipt_id)
        else:
            admitted = None  # This invocation owns the one permitted provider call.
    if admitted is not None:
        if admitted.org_id != request.org_id or admitted.actor_id != current_user.user_id or admitted.details.get("fingerprint") != fingerprint:
            raise HTTPException(409, detail={"error": "registration_identity_conflict"})
        completed = await db.get(AuditLog, completed_id)
        if completed is None:
            raise HTTPException(409, detail={"error": "registration_pending", "operation_id": str(request.operation_id)})
        await write_admin_audit(
            db, actor=current_user, action="reconcile_machine_agent", target_type="agent", target_id=completed.details["id"], org_id=request.org_id
        )
        return await read(service(kind), kind, request.org_id, completed.details["id"])
    provider = service(kind)
    mark_admin_effects()
    try:
        row = await provider.create_agent(data)
    except ConflictError:
        raise HTTPException(409, detail={"error": "registration_identity_conflict"}) from None
    # If the response is lost before this receipt commits, retries stay pending.
    # No provider credential can be reminted automatically.
    key = row.agent_id if kind == "iam-registry" else row.client_id
    db.add(
        AuditLog(
            id=completed_id,
            org_id=request.org_id,
            actor_id=current_user.user_id,
            event_type="machine_agent_registration_completed",
            details={"id": key, "kind": kind},
        )
    )
    await db.commit()
    await write_admin_audit(db, actor=current_user, action="register_machine_agent", target_type="agent", target_id=key, org_id=request.org_id)
    return await read(provider, kind, request.org_id, key)


@router.patch("/{kind}/{key}/identity")
async def update_machine_agent(kind: Kind, key: str, request: Patch, db: Db, current_user: Actor):
    await authorize(db, current_user, request.org_id, kind, write=True)
    provider = service(kind)
    before = await read(provider, kind, request.org_id, key)
    retiring = kind == "cognito-client" and request.deregister and before.get("status") in {"retiring", "retired"}
    if not retiring and before["revision"] != request.expected_revision:
        raise HTTPException(409, detail={"error": "revision_conflict"})
    if not before.get("updated_at"):
        raise HTTPException(409, detail={"error": "agent_revision_unavailable"})
    if kind == "iam-registry" and "role_arn" in request.agent:
        raise HTTPException(
            422, detail={"error": "role_rebinding_unsupported", "message": "Register a distinct IAM identity; aliases must not transfer attribution."}
        )
    if kind == "cognito-client" and request.deregister:
        if request.operation_id is None or request.agent:
            raise HTTPException(422, detail={"error": "retirement_operation_required"})
        mark_admin_effects()
        try:
            await provider.retire_agent(
                key, request.org_id, expected_updated_at=before["updated_at"].replace("Z", "+00:00"), operation_id=str(request.operation_id)
            )
        except ConflictError:
            raise HTTPException(409, detail={"error": "retirement_conflict"}) from None
        await write_admin_audit(db, actor=current_user, action="retire_machine_agent", target_type="agent", target_id=key, org_id=request.org_id)
        value = await read(provider, kind, request.org_id, key)
        value["effect"] = (
            "Cognito client deleted: future token minting is prevented. Already-issued JWTs expire normally and existing runs are not terminated."
        )
        return value
    data = schema(
        AgentRegistryUpdateRequest if kind == "iam-registry" else AgentUpdateRequest, {"status": "disabled"} if request.deregister else request.agent
    )
    if not data.model_dump(exclude_none=True):
        raise HTTPException(422, detail={"error": "empty_update"})
    # Cognito deletion has no provider conditional-revision contract. Disabling
    # registry metadata is exposed honestly; it does not revoke existing JWTs.
    mark_admin_effects()
    try:
        if kind == "iam-registry":
            await provider.update_agent(key, data, expected_updated_at=before["updated_at"].replace("Z", "+00:00"))
        else:
            await provider.update_agent(key, request.org_id, data, expected_updated_at=before["updated_at"].replace("Z", "+00:00"))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HTTPException(409, detail={"error": "revision_conflict"}) from None
        raise
    await write_admin_audit(db, actor=current_user, action="update_machine_agent", target_type="agent", target_id=key, org_id=request.org_id)
    value = await read(provider, kind, request.org_id, key)
    value["effect"] = (
        "IAM disabled status prevents future authorizer checks; cached authorization and existing runs are not terminated."
        if kind == "iam-registry"
        else "Cognito metadata update only; this does not invalidate JWTs or stop runs."
    )
    return value
