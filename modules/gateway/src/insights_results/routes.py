"""Authenticated gateway route for the Insights-owned business-context view."""

import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from typing import get_args

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from contracts.insights import results
from src.auth.dependencies import get_current_user
from src.products.connector_policy import PolicyScopeError, derive_current_envelope
from src.products.models import (
    ConnectorCell,
    ConnectorEvidenceDependency,
    ConnectorPolicyRealm,
    Product,
    ProductChange,
    ProductProjectBinding,
    ProductSourceBinding,
    Project,
)
from src.products.routes import change_in_project
from src.products.source_routes import KINDS, binding_view, connection_access
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.identity.product_scope import product_member, product_visibility_scope
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, TeamMembership, User
from src.shared.models.vault import ChannelTenantMap, InstallationOwnershipConflict, InstallationRevocation, UserCredential, UserIdentity
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["insights"])
SNAPSHOT_LIMIT = 2 * 1024 * 1024
POLICY_LIMIT = 256 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
AUTHORITY_MODELS = (
    ChannelTenantMap,
    ConnectorCell,
    ConnectorEvidenceDependency,
    ConnectorPolicyRealm,
    InstallationOwnershipConflict,
    InstallationRevocation,
    Organization,
    Product,
    ProductChange,
    ProductProjectBinding,
    ProductSourceBinding,
    Project,
    TeamMembership,
    TenantMembership,
    User,
    UserCredential,
    UserIdentity,
)


@asynccontextmanager
async def release_authority(db: AsyncSession):
    """Guard the final decision, including inserted revocations and dependency versions.

    Storage I/O precedes this short read-only critical section. Shared table locks
    permit concurrent readers but fail closed on writers without waiting. Row locks
    alone cannot protect absent revocations or newly inserted policy revisions.
    """
    await db.rollback()
    try:
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            await db.connection(execution_options={"isolation_level": "READ COMMITTED"})
            tables = ", ".join(sorted(model.__table__.name for model in AUTHORITY_MODELS))
            await db.execute(text(f"LOCK TABLE {tables} IN SHARE MODE NOWAIT"))
        elif dialect == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        else:
            raise results.ReadError(409, "projection_incomplete")
        yield
    finally:
        await db.rollback()


def _read_document(tenant_id: str, project_id: str, suffix: str, limit: int, *, expected_sha256: str | None = None) -> dict | None:
    bucket = os.environ.get("BG_INSIGHTS_RESULTS_BUCKET", "")
    if not bucket or not IDENTIFIER.fullmatch(tenant_id) or not IDENTIFIER.fullmatch(project_id):
        return None
    key = f"results/v1/{tenant_id}/{project_id}/{suffix}"
    try:
        response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        body = response["Body"].read(limit + 1)
        if len(body) > limit or (expected_sha256 is not None and hashlib.sha256(body).hexdigest() != expected_sha256):
            raise results.ReadError(409, "projection_incomplete")
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError("invalid published document")
        return document
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise results.ReadError(409, "projection_incomplete") from None
    except (BotoCoreError, OSError, ValueError, UnicodeError, KeyError, TypeError, RecursionError):
        raise results.ReadError(409, "projection_incomplete") from None


def _versioned(tenant_id: str, project_id: str, pointer: dict, kind: str, limit: int) -> dict:
    if set(pointer) != {"schema_version", "dataset_version", "snapshot_sha256", "policy_sha256", "quality_sha256"}:
        raise results.ReadError(409, "projection_incomplete")
    version = pointer["dataset_version"]
    digest = pointer[{"snapshots": "snapshot_sha256", "policies": "policy_sha256"}[kind]]
    if (
        not isinstance(version, str)
        or not IDENTIFIER.fullmatch(version)
        or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for field, value in pointer.items() if field.endswith("_sha256"))
    ):
        raise results.ReadError(409, "projection_incomplete")
    document = _read_document(tenant_id, project_id, f"datasets/{version}/{kind}/{digest}.json", limit, expected_sha256=digest)
    if document is None:
        raise results.ReadError(409, "projection_incomplete")
    return document


def read_snapshot(tenant_id: str, project_id: str) -> dict | None:
    if not IDENTIFIER.fullmatch(tenant_id) or not IDENTIFIER.fullmatch(project_id):
        raise results.ReadError(409, "projection_incomplete")
    published = _read_document(tenant_id, project_id, "published.json", SNAPSHOT_LIMIT)
    if published and published.get("schema_version") == "results-publication/1.0":
        return _versioned(tenant_id, project_id, published, "snapshots", SNAPSHOT_LIMIT)
    return published


def read_policy(tenant_id: str, project_id: str) -> dict | None:
    if not IDENTIFIER.fullmatch(tenant_id) or not IDENTIFIER.fullmatch(project_id):
        return None
    published = _read_document(tenant_id, project_id, "published.json", SNAPSHOT_LIMIT)
    if published and published.get("schema_version") == "results-publication/1.0":
        return _versioned(tenant_id, project_id, published, "policies", POLICY_LIMIT)
    return _read_document(tenant_id, project_id, "policy.json", POLICY_LIMIT)


async def current_project_access(db: AsyncSession, actor: TokenContext, project_id: str) -> bool:
    if not IDENTIFIER.fullmatch(project_id):
        return False
    member = await product_member(
        db,
        user_id=actor.user_id,
        org_id=actor.org_id,
        username=actor.cognito_username,
        account_type=actor.account_type,
        is_platform_admin=actor.is_admin,
    )
    if member is None:
        return False
    rows = await db.scalars(
        select(Project.id)
        .outerjoin(
            ProductProjectBinding,
            and_(
                Project.tenant_id == ProductProjectBinding.tenant_id,
                Project.id == ProductProjectBinding.project_id,
                ProductProjectBinding.valid_to.is_(None),
            ),
        )
        .outerjoin(Product, and_(Product.tenant_id == ProductProjectBinding.tenant_id, Product.id == ProductProjectBinding.product_id))
        .where(
            Project.tenant_id == actor.org_id,
            Project.id == project_id,
            Project.archived_at.is_(None),
            or_(
                and_(ProductProjectBinding.id.is_(None), Project.owner_user_id == member.id),
                and_(Product.archived_at.is_(None), product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin)),
            ),
        )
        .limit(2)
    )
    return project_id in rows.all()


async def current_profile_access(db: AsyncSession, actor: TokenContext, project_id: str, identities: list[dict]) -> bool:
    member = await product_member(
        db,
        user_id=actor.user_id,
        org_id=actor.org_id,
        username=actor.cognito_username,
        account_type=actor.account_type,
        is_platform_admin=actor.is_admin,
    )
    if member is None:
        return False
    pairs = (
        await db.execute(
            select(Product.id, ProductProjectBinding.id)
            .join(
                ProductProjectBinding,
                and_(
                    ProductProjectBinding.tenant_id == Product.tenant_id,
                    ProductProjectBinding.product_id == Product.id,
                ),
            )
            .join(Project, and_(Project.tenant_id == ProductProjectBinding.tenant_id, Project.id == ProductProjectBinding.project_id))
            .where(
                product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin),
                Project.id == project_id,
                Project.archived_at.is_(None),
                Product.archived_at.is_(None),
                ProductProjectBinding.valid_to.is_(None),
            )
            .limit(2)
        )
    ).all()
    if len(pairs) != 1:
        return False
    product_id, binding_id = pairs[0]
    for identity in identities:
        kind, identifier = identity["record_type"], identity["record_id"]
        if kind in ("product", "project"):
            if identifier != {"product": product_id, "project": project_id}[kind]:
                return False
        elif kind == "relationship":
            if identifier != binding_id and not await db.scalar(
                select(ProductProjectBinding.id).where(
                    ProductProjectBinding.tenant_id == actor.org_id,
                    ProductProjectBinding.product_id == product_id,
                    ProductProjectBinding.project_id == project_id,
                    ProductProjectBinding.id == identifier,
                )
            ):
                return False
        elif kind == "change":
            if not await db.scalar(
                select(ProductChange.id).where(
                    ProductChange.tenant_id == actor.org_id,
                    ProductChange.product_id == product_id,
                    ProductChange.id == identifier,
                    change_in_project(project_id),
                )
            ):
                return False
        else:
            return False
    return True


async def current_policy_access(db: AsyncSession, actor: TokenContext, project_id: str, policy: dict) -> bool:
    try:
        dependency_keys = results.policy_dependencies(policy, actor.org_id, project_id)
    except results.ReadError:
        return False
    cells = (
        await db.scalars(
            select(ConnectorCell)
            .where(
                ConnectorCell.tenant_id == actor.org_id,
                ConnectorCell.project_id == project_id,
                ConnectorCell.state == "active",
                ConnectorCell.audience == "shared",
                ConnectorCell.routing_revision == policy["policy_epoch"],
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(cells) != 1:
        return False
    cell = cells[0]
    realm = await db.get(ConnectorPolicyRealm, (actor.org_id, project_id, cell.realm_id, cell.realm_revision), populate_existing=True)
    if realm is None or realm.state != "active":
        return False
    if not isinstance(realm.descriptor, dict) or realm.descriptor.get("audience_expression") != "current:project-readers":
        return False
    rows = (
        await db.scalars(
            select(ConnectorEvidenceDependency)
            .where(
                ConnectorEvidenceDependency.tenant_id == actor.org_id,
                ConnectorEvidenceDependency.project_id == project_id,
                ConnectorEvidenceDependency.cell_id == cell.id,
            )
            .order_by(ConnectorEvidenceDependency.identity_hash, ConnectorEvidenceDependency.revision.desc())
            .limit(101)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(rows) > 100:
        return False
    latest = {}
    for row in rows:
        if row.identity_hash not in latest:
            latest[row.identity_hash] = row
    for envelope in policy["envelopes"]:
        if (envelope["realm_id"], envelope["realm_revision"], envelope["descriptor_digest"]) != (realm.id, realm.revision, realm.descriptor_digest):
            return False
        try:
            current = derive_current_envelope(realm, envelope["identity"], latest, "shared")
        except (PolicyScopeError, KeyError, ValueError):
            return False
        if current != envelope:
            return False
    identities = [dict(zip(results.projection.REFERENCE_FIELDS, key)) for key in dependency_keys]
    profile_identities = [identity for identity in identities if identity["source_connection_id"] == "adp.profile"]
    if profile_identities and not await current_profile_access(db, actor, project_id, profile_identities):
        return False
    sources = {identity["source_connection_id"] for identity in identities} - {"adp.profile"}
    if not sources and profile_identities:
        return True
    if not sources or len(sources) > 100:
        return False
    bindings = (
        await db.scalars(
            select(ProductSourceBinding)
            .where(
                ProductSourceBinding.tenant_id == actor.org_id,
                ProductSourceBinding.project_id == project_id,
                ProductSourceBinding.unlinked_at.is_(None),
                or_(ProductSourceBinding.installation_id.in_(sources), ProductSourceBinding.credential_id.in_(sources)),
            )
            .limit(101)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(bindings) > 100:
        return False
    for source in sources:
        matching = [binding for binding in bindings if (binding.installation_id or binding.credential_id) == source]
        if not matching:
            return False
        selections = {
            (identity["record_type"], identity["record_id"])
            for identity in identities
            if identity["source_connection_id"] == source and identity["record_type"] in get_args(KINDS)
        }
        if selections:
            matching = [binding for binding in matching if (binding.kind, binding.native_id) in selections]
            if selections != {(binding.kind, binding.native_id) for binding in matching}:
                return False
        for binding in matching:
            if binding_view(binding, True)["status"] != "healthy" or await connection_access(db, actor, binding) is None:
                return False
    return True


@router.get("/engineering-insights/v1/projects/{project_id}/results", operation_id="readBusinessContextResults")
async def read_business_context_results(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    def denied(code, status):
        return Response(
            content=json.dumps({"error": {"code": code, "message": "Result unavailable"}}),
            status_code=status,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    try:
        project_allowed = await current_project_access(db, actor, project_id)
        if not project_allowed:
            return denied("not_found", 404)
        policy = await run_in_threadpool(read_policy, actor.org_id, project_id)
        policy_allowed = policy is not None and await current_policy_access(db, actor, project_id, policy)
    except Exception:
        return denied("projection_incomplete", 409)
    reader = results.ResultsReader(
        read_snapshot,
        lambda tenant, project, viewer: tenant == actor.org_id and viewer == actor.user_id and project == project_id and project_allowed,
        get_settings().token_secret_key,
        read_policy=lambda tenant, project: policy,
        can_read_policy=lambda tenant, project, viewer, descriptor: (
            tenant == actor.org_id and project == project_id and viewer == actor.user_id and descriptor == policy and policy_allowed
        ),
    )
    adapter = results.ResultsApp(reader, lambda environ: (actor.org_id, actor.user_id))
    status = []
    headers = []

    def start_response(value, extra):
        status.append(value)
        headers.extend(extra)

    body = await run_in_threadpool(
        lambda: b"".join(
            adapter({"PATH_INFO": request.url.path, "QUERY_STRING": request.url.query, "REQUEST_METHOD": request.method}, start_response)
        )
    )
    if status[0].startswith("200"):
        try:
            latest_policy = await run_in_threadpool(read_policy, actor.org_id, project_id)
            if latest_policy != policy:
                return denied("not_found", 404)
            async with release_authority(db):
                if not await current_project_access(db, actor, project_id):
                    return denied("not_found", 404)
                if not await current_policy_access(db, actor, project_id, policy):
                    return denied("not_found", 404)
                return Response(content=body, status_code=200, headers=dict(headers))
        except Exception:
            return denied("projection_incomplete", 409)
    return Response(content=body, status_code=int(status[0].split(" ", 1)[0]), headers=dict(headers))
