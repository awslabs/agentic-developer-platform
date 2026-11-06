"""Human Knowledge content search through the authenticated, scoped Door."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal
from uuid import UUID

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import SQLAlchemyError

from src.agentauth.door_identity import sign_door_identity
from src.agentauth.envelope import EnvelopeError
from src.agentauth.knowledge_service import door_config, forward_door
from src.knowledge.product_access import require_product_access
from src.knowledge.schemas import AssetResponse
from src.knowledge.search_billing import admit_search, finalize_search
from src.shared.identity.product_scope import product_member
from src.shared.identity.resolver import resolve_asset_owner_id
from src.shared.identity.workspaces import login_subject_for_user, primary_team_for_workspace
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Team, TeamMembership, User
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=4096)
    scope: Literal["personal", "product"] = "personal"
    product_id: str | None = Field(default=None, max_length=36)
    limit: int = Field(default=20, ge=1, le=20)

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.scope == "product") != bool(self.product_id):
            raise ValueError("product_id is required only for Product scope")
        if self.product_id and str(UUID(self.product_id)) != self.product_id:
            raise ValueError("product_id must be a canonical UUID")
        return self


class KnowledgeSearchMatch(BaseModel):
    asset: AssetResponse
    excerpt: str
    heading: str


class KnowledgeSearchResponse(BaseModel):
    items: list[KnowledgeSearchMatch]
    query: str


async def _search_identity(gateway_db, current_user, product_id):
    """Hold current user/membership authority until the response is released."""
    if getattr(current_user, "account_type", "human") != "human":
        raise HTTPException(403, "Context search requires a user session")
    access = None
    if product_id:
        access = await require_product_access(gateway_db, current_user, product_id, lock=True)
        member = await gateway_db.get(User, access.member_id)
    else:
        member = await product_member(
            gateway_db,
            user_id=current_user.user_id,
            org_id=current_user.org_id,
            username=getattr(current_user, "cognito_username", ""),
        )
        if member is not None:
            await gateway_db.execute(select(User.id).where(User.id == member.id).with_for_update(read=True))
            await gateway_db.execute(
                select(TenantMembership.id)
                .where(
                    TenantMembership.user_id == member.id,
                    TenantMembership.tenant_id == current_user.org_id,
                )
                .with_for_update(read=True)
            )
            member = await product_member(
                gateway_db,
                user_id=current_user.user_id,
                org_id=current_user.org_id,
                username=getattr(current_user, "cognito_username", ""),
            )
    if member is None:
        raise HTTPException(403, "Context access is no longer available")
    try:
        subject = await login_subject_for_user(gateway_db, member, lock=True)
    except ValueError:
        raise HTTPException(403, "Context identity is unavailable") from None
    if not subject:
        raise HTTPException(403, "Context identity is unavailable")
    owner_id = await resolve_asset_owner_id(gateway_db, subject)
    identity = {"x-owner-sub": subject, "x-asset-owner-id": owner_id, "x-tenant-id": current_user.org_id}
    if product_id:
        identity["x-product-id"] = product_id
    await gateway_db.execute(
        select(TeamMembership.id).where(TeamMembership.user_id == member.id, TeamMembership.org_id == current_user.org_id).with_for_update(read=True)
    )
    team = await primary_team_for_workspace(gateway_db, member, current_user.org_id)
    if team is None or member.is_shadow or member.user_kind != "human":
        raise HTTPException(403, "Context billing identity is unavailable")
    await gateway_db.execute(select(Team.id).where(Team.id == team.id).with_for_update(read=True))
    budget_context = TokenContext(
        # USER budgets and the direct-use ledger are keyed by the verified
        # Cognito subject. Canonical users.id remains the activity/member key.
        user_id=subject,
        org_id=current_user.org_id,
        team_id=team.id,
        department_id=team.department_id,
        account_type="human",
        auth_source="jwt",
        expires_at=current_user.expires_at,
    )
    return identity, budget_context, member.id


async def search_knowledge(body: KnowledgeSearchRequest, db, gateway_db, current_user, request) -> KnowledgeSearchResponse:
    """Search only the collection shown by the UI; never accept identity input."""
    admission = None
    finalized = False
    try:
        async with asyncio.timeout(30):
            identity, budget_context, member_id = await _search_identity(gateway_db, current_user, body.product_id)
            door_url = door_config() + "/call"
            arguments = {"query": body.query, "scope": "docs" if body.product_id else "personal", "limit": body.limit}
            if body.product_id:
                arguments["product_id"] = body.product_id
            payload = json.dumps({"name": "search", "arguments": arguments}).encode()
            if body.product_id:
                from src.products.work_scope import resolve_project

                budget_context._project_attribution = await resolve_project(gateway_db, tenant_id=current_user.org_id, product_id=body.product_id)
            admission = await admit_search(
                request,
                budget_context,
                human_id=member_id,
                subject=identity["x-owner-sub"],
                aliases=(current_user.user_id, getattr(current_user, "cognito_username", "")),
            )
            assertion = sign_door_identity(
                principal=current_user.user_id,
                tenant_id=current_user.org_id,
                identity=identity,
                method="POST",
                path="/call",
                body=payload,
            )
            admission.dispatched = True
            response = await forward_door(
                "POST",
                door_url,
                {
                    "x-adp-door-identity": assertion,
                    "content-type": "application/json",
                },
                payload,
            )
            result = json.loads(response.body)
            admission.result = result
            finalized = True
            if not await finalize_search(admission):
                raise ValueError("Knowledge search accounting is unavailable")
            if isinstance(result, dict) and result.get("code") == "knowledge_scope_limit":
                raise HTTPException(
                    422,
                    detail={
                        "code": "knowledge_scope_limit",
                        "message": (
                            "This collection has too many indexed sources to search at once. Remove unneeded sources or contact an administrator."
                        ),
                    },
                )
            if not isinstance(result, dict) or result.get("error") or not isinstance(result.get("results"), list):
                raise ValueError("Knowledge search did not complete")
            hits = result["results"]
            if len(hits) > body.limit or any(
                not isinstance(hit, dict) or (hit.get("asset_id") and not isinstance(hit["asset_id"], str)) for hit in hits
            ):
                raise ValueError("Invalid search response")

            # Rejoin the live registry before exposing excerpts. This both
            # enriches citations and fences deletion/reindex after vector search.
            ids = {str(UUID(hit["asset_id"])) for hit in hits if hit.get("asset_id")}
            if not ids:
                return KnowledgeSearchResponse(items=[], query=body.query)
            rows = (
                await db.execute(
                    text("""
                SELECT id, asset_type, source_ref, display_name, tags, metadata,
                       tenant_id, owner_sub, project_id, product_id, status, last_error,
                       retry_count, registered_by, created_at, updated_at, ingestion_attempt_id
                FROM knowledge_assets
                WHERE id IN :ids AND status IN ('complete', 'partial')
                  AND tenant_id = :tenant_id
                  AND product_id IS NOT DISTINCT FROM CAST(:product_id AS text)
                  AND owner_sub IS NOT DISTINCT FROM CAST(:owner_id AS text)
                FOR SHARE
            """).bindparams(bindparam("ids", expanding=True)),
                    {
                        "ids": sorted(ids),
                        "tenant_id": current_user.org_id,
                        "product_id": body.product_id,
                        "owner_id": None if body.product_id else identity["x-asset-owner-id"],
                    },
                )
            ).all()
            assets = {str(row.id): row for row in rows}
            items, seen = [], set()
            for hit in hits:
                row = assets.get(hit.get("asset_id"))
                if row is None or str(row.ingestion_attempt_id) != hit.get("ingestion_attempt_id") or str(row.id) in seen:
                    continue
                if (hit.get("product_id") or None) != body.product_id or hit.get("tenant_id") != current_user.org_id:
                    continue
                if body.product_id:
                    if hit.get("visibility") != "product" or hit.get("owner_sub"):
                        continue
                elif hit.get("visibility") != "personal" or hit.get("owner_sub") != identity["x-asset-owner-id"]:
                    continue
                data = dict(row._mapping)
                data.pop("ingestion_attempt_id")
                data["id"] = str(row.id)
                data["project_id"] = str(row.project_id) if row.project_id else None
                items.append(
                    KnowledgeSearchMatch(
                        asset=AssetResponse(**data),
                        excerpt=str(hit.get("chunk_text") or "")[:1600],
                        heading=str(hit.get("section_heading") or "")[:200],
                    )
                )
                seen.add(str(row.id))
            return KnowledgeSearchResponse(items=items, query=body.query)
    except HTTPException as exc:
        if exc.status_code < 500:
            raise
    except (
        httpx.HTTPError,
        TimeoutError,
        SQLAlchemyError,
        EnvelopeError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        RuntimeError,
        BotoCoreError,
        ClientError,
    ):
        logger.warning("Knowledge content search unavailable", exc_info=True)
    finally:
        if admission is not None and not finalized:
            await finalize_search(admission)
    raise HTTPException(503, detail="Content search is unavailable. Please try again.")
