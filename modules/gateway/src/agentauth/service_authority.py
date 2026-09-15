"""Human approval of bounded standing delegation for a registered service rule."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapStore, _iso, _key, envelope_digest
from src.agentauth.dispatch import AgentPersona
from src.agentauth.store import AuthorityStoreError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.agentauth.service_authority")


def audit(*, user, action, reference, status):
    logger.info(
        "service_authority %s",
        json.dumps(
            {
                "caller": user.user_id,
                "actor_kind": user.account_type,
                "tenant": user.org_id,
                "action": action,
                "authority_reference_id": reference,
                "status": status,
            },
            sort_keys=True,
        ),
    )


class ServiceApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    service_identity: str = Field(pattern=r"^eventbridge:[A-Za-z0-9_.-]{1,128}$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    root_personas: set[AgentPersona] = Field(min_length=1, max_length=10)
    child_personas: set[AgentPersona] = Field(default_factory=set, max_length=10)
    child_issue_scope: Literal["same_issue", "repository"] = "same_issue"
    max_total_dispatches: int = Field(default=4, ge=1, le=64)
    max_dispatch_concurrency: int = Field(default=2, ge=1, le=8)
    max_chain_depth: int = Field(default=4, ge=1, le=8)
    expires_at: AwareDatetime
    replaces: str | None = Field(default=None, max_length=128)


class ServiceAuthorityStore:
    def __init__(self, *, store: BootstrapStore, identity_table: str):
        self.store, self.identity_table = store, identity_table

    def approve(self, *, body: ServiceApproval, user: TokenContext, now=None):
        now = now or datetime.now(UTC)
        if not now < body.expires_at <= now + timedelta(days=30):
            raise HTTPException(422, "approval must expire within 30 days")
        try:
            identity = self.store.client.get_item(
                TableName=self.identity_table,
                Key={"identity_type": {"S": "service_account"}, "identity_value": {"S": body.service_identity}},
                ConsistentRead=True,
            ).get("Item", {})
            allowed = {item["S"] for item in identity.get("allowed_personas", {}).get("L", [])}
            allowed.update(identity.get("allowed_personas", {}).get("SS", []))
            child_scope = identity.get("allowed_child_personas", {})
            allowed_children = {item["S"] for item in child_scope.get("L", [])}
            allowed_children.update(child_scope.get("SS", []))
            if (
                identity.get("tenant_id") != {"S": user.org_id}
                or identity.get("org_id") != {"S": user.org_id}
                or identity.get("repo") != {"S": body.repo}
                or not identity.get("rule_arn", {}).get("S")
                or not body.root_personas <= allowed
                or not body.child_personas <= allowed_children
            ):
                raise HTTPException(404, "registered service scope not found")
            pk = f"TENANT#{user.org_id}"
            reference = f"service-approval:{body.request_id}"
            document = body.model_dump(mode="json")
            document["root_personas"], document["child_personas"] = sorted(body.root_personas), sorted(body.child_personas)
            digest = envelope_digest({"approval": document, "human_id": user.user_id, "tenant_id": user.org_id})
            authority = {
                **_key(pk, f"AUTHORITY#{reference}"),
                "authority_kind": {"S": "service_policy"},
                "human_id": {"S": user.user_id},
                "actor_kind": {"S": "human"},
                "status": {"S": "active"},
                "repo": {"S": body.repo},
                "service_identity": {"S": body.service_identity},
                "rule_arn": identity["rule_arn"],
                "root_personas": {"SS": sorted(body.root_personas)},
                "child_issue_scope": {"S": body.child_issue_scope},
                "max_total_dispatches": {"N": str(body.max_total_dispatches)},
                "max_dispatch_concurrency": {"N": str(body.max_dispatch_concurrency)},
                "max_chain_depth": {"N": str(body.max_chain_depth)},
                "created_at": {"S": _iso(now)},
                "expires_at": {"S": _iso(body.expires_at)},
                "intent_digest": {"S": digest},
            }
            if body.child_personas:
                authority["child_personas"] = {"SS": sorted(body.child_personas)}
            binding = {**_key(pk, f"SERVICE#{body.service_identity}"), "authority_reference_id": {"S": reference}}
            condition = "authority_reference_id = :previous" if body.replaces else "attribute_not_exists(pk)"
            put = {"TableName": self.store.table, "Item": binding, "ConditionExpression": condition}
            if body.replaces:
                put["ExpressionAttributeValues"] = {":previous": {"S": body.replaces}}
            transaction = [self.store._put(authority), {"Put": put}]
            transaction.append(
                {
                    "ConditionCheck": {
                        "TableName": self.identity_table,
                        "Key": {"identity_type": {"S": "service_account"}, "identity_value": {"S": body.service_identity}},
                        "ConditionExpression": (
                            "tenant_id = :tenant AND org_id = :tenant AND repo = :repo AND rule_arn = :rule AND allowed_personas = :personas"
                        ),
                        "ExpressionAttributeValues": {
                            ":tenant": {"S": user.org_id},
                            ":repo": {"S": body.repo},
                            ":rule": identity["rule_arn"],
                            ":personas": identity["allowed_personas"],
                        },
                    }
                }
            )
            identity_check = transaction[-1]["ConditionCheck"]
            # Pin both the value and its absence. A concurrent registration
            # change cannot grant an approval the scope that was read earlier.
            if "allowed_child_personas" in identity:
                identity_check["ConditionExpression"] += " AND allowed_child_personas = :children"
                identity_check["ExpressionAttributeValues"][":children"] = identity["allowed_child_personas"]
            else:
                identity_check["ConditionExpression"] += " AND attribute_not_exists(allowed_child_personas)"
            if body.replaces:
                transaction.append(
                    {
                        "Update": {
                            "TableName": self.store.table,
                            "Key": _key(pk, f"AUTHORITY#{body.replaces}"),
                            "UpdateExpression": (
                                "SET #s = :revoked, revoked_by = if_not_exists(revoked_by, :human), revoked_at = if_not_exists(revoked_at, :now)"
                            ),
                            "ConditionExpression": "service_identity = :service",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":revoked": {"S": "revoked"},
                                ":human": {"S": user.user_id},
                                ":now": {"S": _iso(now)},
                                ":service": {"S": body.service_identity},
                            },
                        }
                    }
                )
            try:
                self.store.client.transact_write_items(TransactItems=transaction)
            except (ClientError, BotoCoreError) as error:
                committed = self.store._read(pk, f"AUTHORITY#{reference}") or {}
                if (
                    committed.get("intent_digest") != {"S": digest}
                    or committed.get("status") != {"S": "active"}
                    or self.store._read(pk, f"SERVICE#{body.service_identity}") != binding
                ):
                    conflict = (
                        isinstance(error, ClientError)
                        and error.response["Error"]["Code"] == "TransactionCanceledException"
                        and any(reason.get("Code") == "ConditionalCheckFailed" for reason in error.response.get("CancellationReasons", []))
                    )
                    raise HTTPException(409 if conflict else 503, "approval not confirmed; retry the same request") from None
            return {"authority_reference_id": reference, "service_identity": body.service_identity, "expires_at": _iso(body.expires_at)}
        except (ClientError, BotoCoreError, AuthorityStoreError, KeyError, TypeError):
            raise HTTPException(503, "service authority unavailable") from None

    def revoke(self, *, reference: str, user: TokenContext):
        pk, sk = f"TENANT#{user.org_id}", f"AUTHORITY#{reference}"
        try:
            self.store.client.update_item(
                TableName=self.store.table,
                Key=_key(pk, sk),
                UpdateExpression="SET #s = :revoked, revoked_by = :human, revoked_at = :now",
                ConditionExpression="authority_kind = :kind AND #s = :active",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":kind": {"S": "service_policy"},
                    ":active": {"S": "active"},
                    ":revoked": {"S": "revoked"},
                    ":human": {"S": user.user_id},
                    ":now": {"S": _iso(datetime.now(UTC))},
                },
            )
        except (ClientError, BotoCoreError):
            # A lost reply and a repeated revocation have the same terminal
            # outcome. Read it without replacing the original actor/time.
            try:
                current = self.store._read(pk, sk) or {}
            except AuthorityStoreError:
                raise HTTPException(503, "revocation not confirmed; retry the same reference") from None
            if current.get("authority_kind") != {"S": "service_policy"}:
                raise HTTPException(404, "service approval not found") from None
            if current.get("status") != {"S": "revoked"}:
                raise HTTPException(503, "revocation not confirmed; retry the same reference") from None
        return {"authority_reference_id": reference, "status": "revoked"}


def get_service_authorities():
    table, identities = os.environ.get("AGENT_AUTHORITY_TABLE", ""), os.environ.get("IDENTITY_INDEX_TABLE", "")
    if not table or not identities:
        raise HTTPException(503, "service authority is not configured")
    return ServiceAuthorityStore(store=BootstrapStore(table_name=table, dynamodb_client=boto3.client("dynamodb")), identity_table=identities)


async def approving_human(request: Request, user: Annotated[TokenContext, Depends(get_current_user)], db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        if user.account_type != "human" or user.auth_source != "jwt" or not user.org_id or not user.user_id:
            raise HTTPException(403, "human approval required")
        await AccessControl(db).check_permission(user, Permission.PLAN_APPROVE, target_org_id=user.org_id)
    except HTTPException as error:
        audit(
            user=user,
            action="revoke" if "reference" in request.path_params else "approve",
            reference=request.path_params.get("reference"),
            status=error.status_code,
        )
        raise
    return user


router = APIRouter(prefix="/agent-authorities/service-grants", tags=["agent-authority"])


@router.post("")
async def approve_service(body: ServiceApproval, user=Depends(approving_human), service=Depends(get_service_authorities)):
    return await audited_action(service.approve, action="approve", reference=f"service-approval:{body.request_id}", user=user, body=body)


async def audited_action(function, *, action, reference, user, **kwargs):
    status = 503
    try:
        result = await run_in_threadpool(function, user=user, **kwargs)
        status = 200
        return result
    except HTTPException as error:
        status = error.status_code
        raise
    finally:
        audit(user=user, action=action, reference=reference, status=status)


@router.post("/{reference}/revoke")
async def revoke_service(reference: str, user=Depends(approving_human), service=Depends(get_service_authorities)):
    if not reference.startswith("service-approval:") or len(reference) > 128:
        raise HTTPException(404, "service approval not found")
    return await audited_action(lambda **kwargs: service.revoke(reference=reference, **kwargs), action="revoke", reference=reference, user=user)
