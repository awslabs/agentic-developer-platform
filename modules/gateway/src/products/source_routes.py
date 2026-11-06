"""Scoped Product source selections and read-only verification."""

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.installations.resolver import InstallationOwnershipError, assert_installation_owned_by
from src.auth.aws_connection_authority import verified_connection_evidence
from src.auth.dependencies import get_current_user
from src.auth.vault_routes import get_secrets_manager
from src.auth.vault_service import _visible_credential_filter
from src.product_onboarding.routes import fingerprint, headers, preconditions
from src.product_onboarding.source_discovery import connection_actor
from src.products.configuration import normalize_environments
from src.products.models import Product, ProductChange, ProductOperation, ProductSourceBinding, Project
from src.products.routes import product_github_bindings, profile, require_profile_admission, scoped_product, scoped_snapshots
from src.products.verification import observe_binding
from src.products.verification_operations import _aware, operation_view, tombstone_binding
from src.shared.database import get_db
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation, UserCredential
from src.shared.schemas.auth import TokenContext
from src.shared.services.secrets_manager import SecretsManagerHelper

router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}", tags=["products"])
bindings_router = APIRouter(prefix="/bindings")
KINDS = Literal[
    "repository",
    "tracker",
    "document",
    "environment",
    "pipeline",
    "ci_job",
    "support",
    "communication",
    "finance",
    "quality_project",
    "deployment_application",
    "incident_service",
    "tracker_team",
    "communication_channel",
]


class ConnectionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["github_installation", "credential"]
    id: str = Field(min_length=1, max_length=36)


class EnvironmentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    environment_class: Literal["development", "test", "staging", "production", "other"]
    production: bool


class BindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: KINDS
    provider: str = Field(min_length=1, max_length=64)
    connection_ref: ConnectionRef
    namespace: str = Field(min_length=1, max_length=512)
    native_id: str = Field(min_length=1, max_length=512)
    environment: EnvironmentSelection | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    repository_id: str | None = Field(default=None, max_length=20)
    source_revision: str | None = Field(default=None, max_length=512)

    @field_validator("source_revision", mode="before")
    @classmethod
    def validate_raw_repository_pin(cls, value, info: ValidationInfo):
        if info.data.get("kind") == "repository" and info.data.get("provider") == "github" and isinstance(value, str):
            if value != value.strip():
                raise ValueError("GitHub repository source_revision must be a full lowercase commit SHA without padding")
        return value

    @model_validator(mode="after")
    def validate_selection(self):
        kinds = {
            "sonarqube": "quality_project",
            "argocd": "deployment_application",
            "pagerduty": "incident_service",
            "linear": "tracker_team",
            "slack": "communication_channel",
        }
        if self.kind in kinds.values() and kinds.get(self.provider) != self.kind:
            raise ValueError("Connector scope kind requires its matching provider")
        if self.provider in kinds and (
            self.kind != kinds[self.provider]
            or self.connection_ref.kind != "credential"
            or (self.provider in ("sonarqube", "pagerduty", "linear") and self.namespace != "/")
            or (self.provider == "slack" and self.namespace != "/" and re.fullmatch(r"T[A-Z0-9]{2,127}", self.namespace) is None)
            or (
                self.provider == "argocd"
                and (
                    len(self.namespace.split("/")) != 2
                    or any(segment in ("", ".", "..") for segment in self.namespace.split("/"))
                    or "/" in self.native_id
                )
            )
        ):
            raise ValueError("Connector selection requires its provider's native scope and credential")
        if self.provider == "jenkins" or self.kind == "ci_job":
            from src.product_onboarding.jenkins import selection

            if (
                self.provider != "jenkins"
                or self.kind != "ci_job"
                or self.connection_ref.kind != "credential"
                or not selection(self.namespace, self.native_id)
            ):
                raise ValueError("Jenkins selection requires its parent folder and full job name")
        if self.kind == "environment" and self.environment is None:
            raise ValueError("environment selection requires environment metadata")
        if self.kind == "pipeline" and (not self.repository_id or not self.source_revision):
            raise ValueError("pipeline selection requires a discovered repository and revision")
        if self.kind == "repository" and self.provider == "github" and self.source_revision is not None:
            if re.fullmatch(r"[a-f0-9]{40}", self.source_revision) is None:
                raise ValueError("GitHub repository source_revision must be a full lowercase commit SHA")
        if any(not value or value != value.strip() for value in (self.namespace, self.native_id)):
            raise ValueError("native identities cannot be blank or padded")
        return self


async def connection_access(db: AsyncSession, actor: TokenContext, binding: ProductSourceBinding, *, lock=False):
    if actor.account_type != "human":
        return None
    if binding.provider in ("jenkins", "sonarqube", "argocd", "pagerduty", "linear", "slack"):
        from src.product_onboarding.discovery import connector_enabled

        if binding.kind != {
            "jenkins": "ci_job",
            "sonarqube": "quality_project",
            "argocd": "deployment_application",
            "pagerduty": "incident_service",
            "linear": "tracker_team",
            "slack": "communication_channel",
        }[binding.provider] or not await connector_enabled(db, actor.org_id, lock=lock):
            return None
    actor = await connection_actor(db, actor)
    if actor is None:
        return None
    if binding.installation_id is not None:
        mapping = await db.get(ChannelTenantMap, binding.installation_id, with_for_update=lock, populate_existing=lock)
        if (
            mapping is None
            or mapping.org_id != actor.org_id
            or mapping.provider != binding.provider
            or mapping.ownership_disputed
            or not mapping.installation_id
            or (mapping.provider_scope_id.startswith("personal:") and mapping.installed_by_user_id != actor.user_id)
        ):
            return None
        try:
            await assert_installation_owned_by(actor.org_id, int(mapping.installation_id), db=db)
        except (InstallationOwnershipError, ValueError):
            return None
        return mapping
    visible = _visible_credential_filter(actor)
    if not visible:
        return None
    query = select(UserCredential).where(
        UserCredential.id == binding.credential_id,
        UserCredential.org_id == actor.org_id,
        or_(*visible),
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    credential = await db.scalar(query)
    if credential is None or (credential.provider or credential.service) != binding.provider:
        return None
    if binding.provider in ("jenkins", "sonarqube", "argocd", "pagerduty", "linear", "slack"):
        from src.product_onboarding.source_discovery import connector_credential

        if not connector_credential(binding.provider, credential):
            return None
    if binding.provider == "aws":
        try:
            verified_connection_evidence(credential)
        except HTTPException:
            return None
    if credential.expires_at is not None:
        expiry = credential.expires_at.replace(tzinfo=UTC) if credential.expires_at.tzinfo is None else credential.expires_at
        if expiry <= utcnow():
            return None
    return credential


async def collection_connection_access(db: AsyncSession, actor: TokenContext, binding: ProductSourceBinding, product_id: str, *, lock=False):
    if binding.provider != "atlassian-mcp":
        return await connection_access(db, actor, binding, lock=lock)
    if (
        actor.account_type != "human"
        or binding.kind != "tracker"
        or binding.installation_id is not None
        or binding.credential_id is None
        or not isinstance(binding.desired, dict)
        or binding.desired.get("share_with_product") is not True
        or binding.desired.get("product_id") != product_id
    ):
        return None
    from src.auth.atlassian_system_routes import current_system

    try:
        credential, current = await current_system(db, binding.credential_id, actor.org_id, product_id, lock=lock)
    except HTTPException as error:
        if error.status_code == 404:
            return None
        raise
    if (
        current.site_id != binding.namespace
        or (binding.observed or {}).get("connection_revision") != current.revision
        or binding_view(binding, True)["status"] != "healthy"
    ):
        return None
    return credential


def binding_view(binding: ProductSourceBinding, accessible: bool, now: datetime | None = None) -> dict:
    view = {
        "id": binding.id,
        "project_id": binding.project_id,
        "generation": binding.generation,
        "revision": binding.revision,
    }
    if binding.status == "unlinked":
        return {**view, "status": "unlinked"}
    if not accessible or binding.status == "revoked":
        return {**view, "status": "restricted"}
    status = binding.status
    if status == "healthy":
        observed = binding.observed or {}
        try:
            observed_at = datetime.fromisoformat(observed["observed_at"].replace("Z", "+00:00"))
            stale_after = datetime.fromisoformat(observed["stale_after"].replace("Z", "+00:00"))
            current = now or utcnow()
            if observed_at.tzinfo is None or stale_after.tzinfo is None or not observed_at <= current < stale_after:
                status = "degraded"
        except (KeyError, TypeError, ValueError, AttributeError):
            status = "degraded"
    view.update(selection=binding.desired, status=status)
    if binding.observed:
        view.update(
            {
                key: value
                for key, value in binding.observed.items()
                if key in ("observed_at", "stale_after", "source_revision", "verified_ref", "verified_environment", "web_url")
            }
        )
    return view


_SOURCE_CHECKS = {
    "repository": "repository",
    "tracker": "planning",
    "document": "documents",
    "environment": "environment",
    "pipeline": "pipeline",
    "ci_job": "pipeline",
    "quality_project": "observability",
    "deployment_application": "deployment",
    "incident_service": "support",
    "tracker_team": "planning",
    "communication_channel": "collaboration",
}


def document_event_support(resources):
    if any(
        isinstance(resource, dict) and (resource.get("kind") == "document" or resource.get("record_type") == "document") for resource in resources
    ):
        return {"event_support": {"document_revision": "unavailable"}}
    return {}


def _redact_refs(value, restricted: set[tuple[str, str]], allowed_sources=None, profile_scope=None):
    if isinstance(value, list):
        return [safe for item in value if (safe := _redact_refs(item, restricted, allowed_sources, profile_scope)) is not None]
    if isinstance(value, dict):
        source_fields = {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"}
        if source_fields <= value.keys() and profile_scope is not None:
            if (
                (value["tenant_id"], value["project_id"]) != profile_scope[:2]
                or (
                    value["source_connection_id"] == "adp.profile"
                    and value["record_type"] == "product"
                    and profile_scope[2] is not None
                    and value["record_id"] != profile_scope[2]
                )
                or (value["source_connection_id"] == "adp.profile" and value["record_type"] == "project" and value["record_id"] != profile_scope[1])
            ):
                return None
        if allowed_sources is not None and source_fields <= value.keys():
            profile_ref = (
                value["source_connection_id"] == "adp.profile"
                and (value["tenant_id"], value["project_id"]) == profile_scope[:2]
                and (value["record_type"] != "product" or value["record_id"] == profile_scope[2])
                and (value["record_type"] != "project" or value["record_id"] == profile_scope[1])
            )
            if not profile_ref and value not in allowed_sources:
                return None
        if (value.get("source_connection_id"), value.get("record_id")) in restricted and (allowed_sources is None or value not in allowed_sources):
            return None
        return {key: _redact_refs(item, restricted, allowed_sources, profile_scope) for key, item in value.items()}
    return value


async def source_overlay(
    db: AsyncSession,
    actor: TokenContext,
    project: Project,
    *,
    snapshot: dict | None = None,
    now: datetime | None = None,
    service_grant: CredentialServiceDelegation | None = None,
    product_id: str | None = None,
):
    """Apply live source authorization to current and frozen profile projections."""
    rows = (
        await db.scalars(
            select(ProductSourceBinding).where(
                ProductSourceBinding.tenant_id == actor.org_id,
                ProductSourceBinding.project_id == project.id,
            )
        )
    ).all()
    service_bindings = await service_visible_bindings(db, service_grant) if actor.account_type == "service" and service_grant is not None else []
    visible_service_bindings = {binding.id for binding in service_bindings}
    allowed_sources = [allowed_source_ref(service_grant, binding) for binding in service_bindings] if actor.account_type == "service" else None
    profile_scope = (
        (actor.org_id, project.id, service_grant.product_id if service_grant is not None else None)
        if actor.account_type == "service"
        else (actor.org_id, project.id, product_id or (snapshot or {}).get("product_id"))
    )
    evidence = {}
    restricted = set()
    authorized = set()
    for binding in rows:
        connection_id = binding.installation_id or binding.credential_id
        source_refs = {(connection_id, binding.native_id)}
        if binding.provider == "atlassian-mcp":
            source_refs.add((connection_id, binding.native_id.rsplit(":", 1)[-1]))
        if binding.unlinked_at is not None:
            restricted.update(source_refs)
            continue
        accessible = (
            binding.id in visible_service_bindings
            if actor.account_type == "service"
            else await collection_connection_access(db, actor, binding, profile_scope[2]) is not None
        )
        view = binding_view(binding, accessible, now)
        if view["status"] == "restricted":
            restricted.update(source_refs)
            if actor.account_type == "service":
                continue
        else:
            authorized.update(source_refs)
        check = _SOURCE_CHECKS.get(binding.kind)
        if check is None:
            continue
        status = {"healthy": "complete", "degraded": "stale"}.get(view["status"], "missing")
        if check not in evidence or {"complete": 2, "stale": 1, "missing": 0}[status] > {"complete": 2, "stale": 1, "missing": 0}[evidence[check]]:
            evidence[check] = status
    restricted.difference_update(authorized)
    source = snapshot or {"configuration": project.configuration, "provenance": project.provenance}
    configuration = normalize_environments(_redact_refs(source.get("configuration") or {}, restricted, allowed_sources, profile_scope))
    provenance = [item for item in source.get("provenance") or [] if _redact_refs(item, restricted, allowed_sources, profile_scope) == item]
    projected = Project(
        tenant_id=project.tenant_id,
        id=project.id,
        configuration=configuration,
        provenance=provenance,
    )
    return projected, evidence


async def permitted_source_refs(db: AsyncSession, actor: TokenContext, project_id: str) -> set[tuple[str, str]]:
    rows = (
        await db.scalars(
            select(ProductSourceBinding).where(
                ProductSourceBinding.tenant_id == actor.org_id,
                ProductSourceBinding.project_id == project_id,
                ProductSourceBinding.unlinked_at.is_(None),
                ProductSourceBinding.status != "revoked",
            )
        )
    ).all()
    return {
        (binding.installation_id or binding.credential_id, binding.native_id)
        for binding in rows
        if await connection_access(db, actor, binding) is not None
    }


async def scoped_binding(db, actor, product_id, project_id, binding_id):
    member, product, project = await scoped_product(db, actor, product_id, project_id)
    binding = await db.get(ProductSourceBinding, (actor.org_id, binding_id))
    if binding is None or binding.project_id != project.id or binding.unlinked_at is not None:
        raise HTTPException(404, detail={"code": "not_found"})
    return member, product, project, binding


@router.get("/sources", operation_id="listSources", dependencies=[Depends(require_profile_admission)])
async def list_sources(
    product_id: str,
    project_id: str,
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=2048),
    connection_id: str | None = None,
    connection_kind: Literal["github_installation", "credential"] | None = None,
    resource_cursor: str | None = Query(None, max_length=2048),
    pipeline_repository_id: str | None = Query(None, max_length=20),
    secrets_manager: SecretsManagerHelper = Depends(get_secrets_manager),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.product_onboarding.source_discovery import (
        associated_records,
        authorized_connections,
        aws_environment,
        github_pipeline_resources,
        github_resources,
        gitlab_resources,
    )

    _, product, project = await scoped_product(db, actor, product_id, project_id)
    if (connection_id is None) != (connection_kind is None) or (resource_cursor is not None and connection_id is None):
        raise HTTPException(400, detail={"code": "invalid_cursor"})
    if pipeline_repository_id is not None and (
        connection_kind != "github_installation" or not pipeline_repository_id.isascii() or not pipeline_repository_id.isdecimal()
    ):
        raise HTTPException(400, detail={"code": "invalid_cursor"})
    connections = await authorized_connections(db, actor)
    if connection_id is not None:
        matches = [pair for pair in connections if pair[0]["connection_ref"] == {"kind": connection_kind, "id": connection_id}]
        if not matches:
            raise HTTPException(404, detail={"code": "not_found"})
        source, connection = matches[0]
        installation_id = connection.installation_id if connection_kind == "github_installation" else None
        linked = await associated_records(db, actor, project, connection_id)
        if pipeline_repository_id is None and resource_cursor is not None and resource_cursor.startswith("linked:"):
            if source["provider"] == "aws":
                raise HTTPException(400, detail={"code": "invalid_cursor"})
            offset = resource_cursor.removeprefix("linked:")
            if not offset.isascii() or not offset.isdecimal() or int(offset) >= len(linked):
                raise HTTPException(400, detail={"code": "invalid_cursor"})
            start = int(offset)
            source.update(resources=linked[start : start + limit], coverage="partial")
            source.update(document_event_support(source["resources"]))
            if start + limit < len(linked):
                source["resource_cursor"] = f"linked:{start + limit}"
            db.expire(connection)
            if not any(item["connection_ref"] == source["connection_ref"] for item, _ in await authorized_connections(db, actor)):
                raise HTTPException(404, detail={"code": "not_found"})
            headers(response, product.revision)
            return {"items": [source], "next_cursor": None, "coverage": "partial"}
        if pipeline_repository_id is not None and source["provider"] == "github" and connection_kind == "github_installation":
            if resource_cursor is not None and (
                not resource_cursor.isascii() or not resource_cursor.isdecimal() or not 1 <= int(resource_cursor) <= 100000
            ):
                raise HTTPException(400, detail={"code": "invalid_cursor"})
            resources, continuation, coverage = await github_pipeline_resources(
                connection, pipeline_repository_id, page=int(resource_cursor or 1), limit=limit
            )
            source.update(resources=resources, coverage=coverage)
            if continuation:
                source["resource_cursor"] = continuation
            linked = []
        elif source["provider"] == "github" and connection_kind == "github_installation":
            if resource_cursor is not None and (
                not resource_cursor.isascii() or not resource_cursor.isdecimal() or not 1 <= int(resource_cursor) <= 100000
            ):
                raise HTTPException(400, detail={"code": "invalid_cursor"})
            resources, continuation, coverage = await github_resources(connection, page=int(resource_cursor or 1), limit=limit)
            source.update(resources=resources, coverage=coverage)
            if continuation:
                source["resource_cursor"] = continuation
        elif source["provider"] == "gitlab" and connection_kind == "credential":
            if resource_cursor is not None and (
                not resource_cursor.isascii() or not resource_cursor.isdecimal() or not 1 <= int(resource_cursor) <= 100000
            ):
                raise HTTPException(400, detail={"code": "invalid_cursor"})
            resources, continuation, coverage = await gitlab_resources(db, actor, connection, page=int(resource_cursor or 1), limit=limit)
            source.update(resources=resources, coverage=coverage)
            if continuation:
                source["resource_cursor"] = continuation
        elif source["provider"] == "jira" and connection_kind == "credential":
            from src.internal.connector_source_broker import jira_boards

            if resource_cursor is not None and (
                not resource_cursor.isascii() or not resource_cursor.isdecimal() or not 1 <= int(resource_cursor) <= 5
            ):
                raise HTTPException(400, detail={"code": "invalid_cursor"})
            resources, continuation, coverage = await jira_boards(connection, secrets_manager, page=int(resource_cursor or 1), limit=limit)
            source.update(resources=resources, coverage=coverage)
            if continuation:
                source["resource_cursor"] = continuation
        elif source["provider"] == "aws" and connection_kind == "credential":
            if resource_cursor is not None:
                raise HTTPException(400, detail={"code": "invalid_cursor"})
            environment = await aws_environment(connection, secrets_manager)
            if environment is not None:
                source.update(resources=[environment], coverage="partial", health="healthy", verified_at=environment["observed_at"])
            else:
                source.update(coverage="unavailable", health="unverified")
        else:
            if resource_cursor is not None:
                raise HTTPException(400, detail={"code": "invalid_cursor"})
            source["coverage"] = "unavailable"
        db.expire(connection)
        current = next(
            (credential for item, credential in await authorized_connections(db, actor) if item["connection_ref"] == source["connection_ref"]),
            None,
        )
        if current is None:
            raise HTTPException(404, detail={"code": "not_found"})
        if installation_id is not None and current.installation_id != installation_id:
            raise HTTPException(404, detail={"code": "not_found"})
        if source["provider"] == "aws":
            try:
                version = verified_connection_evidence(current)[1]
            except HTTPException:
                version = None
            if version != next((resource["source_revision"] for resource in source.get("resources", []) if resource["kind"] == "environment"), None):
                source.update(coverage="unavailable", health="unverified")
                source.pop("resources", None)
                source.pop("verified_at", None)
                linked = []
        if linked and "resource_cursor" not in source and source["coverage"] != "denied":
            resources = source.get("resources") or []
            available = max(0, limit - len(resources))
            source["resources"] = resources + linked[:available]
            if available < len(linked):
                source["resource_cursor"] = f"linked:{available}"
            if source["coverage"] == "complete":
                source["coverage"] = "partial"
        source.update(document_event_support(source.get("resources") or []))
        headers(response, product.revision)
        return {"items": [source], "next_cursor": None, "coverage": "partial" if source["coverage"] != "complete" else "complete"}
    if cursor is not None and cursor not in [f"{source['connection_ref']['kind']}:{source['connection_ref']['id']}" for source, _ in connections]:
        raise HTTPException(400, detail={"code": "invalid_cursor"})
    pairs = [
        (source, connection)
        for source, connection in connections
        if f"{source['connection_ref']['kind']}:{source['connection_ref']['id']}" > (cursor or "")
    ]
    page = pairs[:limit]
    headers(response, product.revision)
    return {
        "items": [{**source, "coverage": "unavailable"} for source, _ in page],
        "next_cursor": f"{page[-1][0]['connection_ref']['kind']}:{page[-1][0]['connection_ref']['id']}" if len(pairs) > limit else None,
        "coverage": "partial" if page or len(pairs) > limit else "complete",
    }


def allowed_source_ref(grant: CredentialServiceDelegation, binding: ProductSourceBinding) -> dict | None:
    from src.auth.l5_transform_branch_scope import OCCURRENCE, occurrence_binding, occurrence_source

    if isinstance(grant.execution_scope, dict) and OCCURRENCE in grant.execution_scope:
        return occurrence_source(grant) if occurrence_binding(grant, binding) else None
    revision = (binding.observed or {}).get("source_revision") or (binding.desired or {}).get("source_revision")
    required_fields = {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"}
    for ref in grant.sources:
        if (
            isinstance(ref, dict)
            and set(ref) == required_fields
            and ref.get("tenant_id") == grant.tenant_id
            and ref.get("project_id") == grant.project_id
            and ref.get("source_connection_id") == (binding.installation_id or binding.credential_id)
            and (
                ref.get("record_type") == binding.kind
                or binding.kind == "repository"
                and ref.get("record_type") == "repository_commit"
                and isinstance(revision, str)
                and re.fullmatch(r"[a-f0-9]{40}", revision)
            )
            and ref.get("record_id") == binding.native_id
            and revision is not None
            and ref.get("record_revision") == revision
        ):
            return ref
    return None


def source_ref_authorized(grant, binding, reference):
    """Permit a consented release base SHA only while its selected head remains current.

    This exception is confined to explicit DevOps release consent. It does not
    grant arbitrary repository history or change other services' revision rules.
    """
    current = allowed_source_ref(grant, binding)
    if current == reference:
        return current is not None
    from src.l5_service_identity import worker_id

    scope = grant.execution_scope
    return bool(
        current is not None
        and grant.service_id == worker_id("devops")
        and "release_readiness" in grant.capabilities
        and isinstance(scope, dict)
        and reference in grant.sources
        and reference in scope.get("sources", [])
        and binding.kind == "repository"
        and binding.provider == "github"
        and isinstance(reference, dict)
        and set(reference) == set(current)
        and reference.get("record_type") == "repository_commit"
        and re.fullmatch(r"[a-f0-9]{40}", reference.get("record_revision", ""))
        and all(reference.get(key) == current.get(key) for key in current if key != "record_revision")
    )


async def service_visible_bindings(db: AsyncSession, grant: CredentialServiceDelegation) -> list[ProductSourceBinding]:
    from src.auth.l5_devops_knowledge_scope import current_knowledge_scope, is_knowledge_scope
    from src.auth.l5_transform_branch_scope import OCCURRENCE
    from src.shared.identity.product_scope import current_l5_parent, product_member

    knowledge = is_knowledge_scope(grant)
    knowledge_owner = None
    if knowledge:
        if not await current_knowledge_scope(db, grant, admitted=True):
            return []
        knowledge_owner = await product_member(db, user_id=grant.created_by, org_id=grant.tenant_id)
        if knowledge_owner is None:
            return []

    if isinstance(grant.execution_scope, dict) and OCCURRENCE in grant.execution_scope and not await current_l5_parent(db, grant):
        return []
    if "source:read" not in grant.operations or not grant.sources:
        return []
    rows = (
        await db.scalars(
            select(ProductSourceBinding).where(
                ProductSourceBinding.tenant_id == grant.tenant_id,
                ProductSourceBinding.project_id == grant.project_id,
                ProductSourceBinding.unlinked_at.is_(None),
                ProductSourceBinding.status != "revoked",
            )
        )
    ).all()
    allowed = []
    for binding in rows:
        binding = await db.scalar(
            select(ProductSourceBinding)
            .where(
                ProductSourceBinding.tenant_id == grant.tenant_id,
                ProductSourceBinding.id == binding.id,
                ProductSourceBinding.unlinked_at.is_(None),
                ProductSourceBinding.status != "revoked",
            )
            .execution_options(populate_existing=True)
        )
        if binding is None or allowed_source_ref(grant, binding) is None:
            continue
        if binding.credential_id is not None:
            if binding.credential_id != grant.credential_id and not knowledge:
                continue
            if knowledge:
                # The AWS connection and selected document connections differ.
                # Only this explicit publication grant may use its current
                # consenting owner's visibility, never worker attribution.
                from types import SimpleNamespace

                owner = SimpleNamespace(org_id=grant.tenant_id, user_id=knowledge_owner.id, team_id=knowledge_owner.team_id, is_admin=False)
                visible = _visible_credential_filter(owner)
                if not visible or not await db.scalar(select(UserCredential.id).where(UserCredential.id == binding.credential_id, or_(*visible))):
                    continue
            credential = await db.scalar(
                select(UserCredential).where(UserCredential.id == binding.credential_id).execution_options(populate_existing=True)
            )
            if credential is None or credential.org_id != grant.tenant_id:
                continue
            if binding.provider == "atlassian-mcp":
                from src.auth.atlassian_system_routes import current_system

                if (
                    not isinstance(binding.desired, dict)
                    or binding.desired.get("share_with_product") is not True
                    or binding.desired.get("product_id") != grant.product_id
                ):
                    continue
                try:
                    _, current = await current_system(db, binding.credential_id, grant.tenant_id, grant.product_id, lock=True)
                except HTTPException as error:
                    if error.status_code == 404:
                        continue
                    raise
                if (
                    current.site_id != binding.namespace
                    or (binding.observed or {}).get("connection_revision") != current.revision
                    or binding_view(binding, True)["status"] != "healthy"
                ):
                    continue
            elif (credential.provider or credential.service) != binding.provider:
                continue
            if credential.expires_at is not None:
                expiry = credential.expires_at.replace(tzinfo=UTC) if credential.expires_at.tzinfo is None else credential.expires_at
                if expiry <= utcnow():
                    continue
        else:
            if grant.installation_id is not None and grant.installation_id != binding.installation_id:
                continue
            mapping = await db.scalar(
                select(ChannelTenantMap).where(ChannelTenantMap.id == binding.installation_id).execution_options(populate_existing=True)
            )
            if (
                mapping is None
                or mapping.org_id != grant.tenant_id
                or mapping.provider != binding.provider
                or mapping.ownership_disputed
                or not mapping.installation_id
                or (mapping.provider_scope_id.startswith("personal:") and mapping.installed_by_user_id != grant.created_by)
            ):
                continue
            try:
                await assert_installation_owned_by(grant.tenant_id, int(mapping.installation_id), db=db)
            except (InstallationOwnershipError, ValueError):
                continue
        allowed.append(binding)
    return allowed


async def service_sources(db: AsyncSession, actor: TokenContext, product_id: str, project_id: str, delegation_id: str | None, response: Response):
    grant, product, _ = await scoped_product(db, actor, product_id, project_id, delegation_id)
    bindings = await service_visible_bindings(db, grant)
    headers(response, product.revision)
    return {
        "items": [
            {
                "connection_ref": {
                    "kind": "github_installation" if binding.installation_id else "credential",
                    "id": binding.installation_id or binding.credential_id,
                },
                "provider": binding.provider,
                "resources": [allowed_source_ref(grant, binding)],
                "coverage": "partial",
                **document_event_support([{"kind": binding.kind}]),
            }
            for binding in bindings
        ],
        "next_cursor": None,
        "coverage": "complete",
    }


@bindings_router.get("", operation_id="listBindings")
async def list_bindings(
    product_id: str,
    project_id: str,
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
    x_l5_delegation_id: str | None = Header(default=None),
):
    member, product, _ = await scoped_product(db, actor, product_id, project_id, x_l5_delegation_id)
    if actor.account_type == "service":
        rows = [binding for binding in await service_visible_bindings(db, member) if binding.id > (cursor or "")]
        rows.sort(key=lambda binding: binding.id)
    else:
        rows = (
            await db.scalars(
                select(ProductSourceBinding)
                .where(
                    ProductSourceBinding.tenant_id == actor.org_id,
                    ProductSourceBinding.project_id == project_id,
                    ProductSourceBinding.id > (cursor or ""),
                )
                .order_by(ProductSourceBinding.id)
                .limit(limit + 1)
            )
        ).all()
    page = rows[:limit]
    headers(response, product.revision)
    return {
        "items": [
            binding_view(row, actor.account_type == "service" or await collection_connection_access(db, actor, row, product_id) is not None)
            for row in page
        ],
        "next_cursor": page[-1].id if len(rows) > limit else None,
    }


@bindings_router.post("", operation_id="createBinding", status_code=201, dependencies=[Depends(require_profile_admission)])
async def create_binding(
    product_id: str,
    project_id: str,
    selection: BindingInput,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
    secrets_manager: SecretsManagerHelper = Depends(get_secrets_manager),
):
    member, product, project = await scoped_product(db, actor, product_id, project_id)
    preconditions(idempotency_key, expected_revision)
    desired = selection.model_dump(exclude_none=True)
    if len(str(desired).encode()) > 65536:
        raise HTTPException(422, detail={"code": "validation_error"})
    key_hash = fingerprint({"key": idempotency_key})
    request_hash = fingerprint({"selection": desired, "product_id": product_id, "project_id": project_id, "expected_revision": expected_revision})
    prior = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "createBinding",
            ProductOperation.key_hash == key_hash,
        )
    )
    if prior is not None:
        if prior.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        binding = await db.get(ProductSourceBinding, (actor.org_id, prior.receipt["binding_id"]))
        if binding is None or binding.unlinked_at is not None:
            raise HTTPException(409, detail={"code": "binding_conflict"})
        headers(response, product.revision)
        response.status_code = 200
        return binding_view(binding, await connection_access(db, actor, binding) is not None)
    if product.archived_at is not None:
        raise HTTPException(409, detail={"code": "product_archived"})
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    connection_id = selection.connection_ref.id
    binding = ProductSourceBinding(
        tenant_id=actor.org_id,
        project_id=project_id,
        kind=selection.kind,
        provider=selection.provider,
        namespace=selection.namespace,
        native_id=selection.native_id,
        desired=desired,
        installation_id=connection_id if selection.connection_ref.kind == "github_installation" else None,
        credential_id=connection_id if selection.connection_ref.kind == "credential" else None,
        revision=product.revision + 1,
    )
    connection = await connection_access(db, actor, binding)
    if connection is None:
        raise HTTPException(404, detail={"code": "connection_unavailable"})
    verified_environment = None
    if selection.kind == "environment" and selection.provider == "aws":
        from src.product_onboarding.source_discovery import aws_environment

        if selection.connection_ref.kind != "credential":
            raise HTTPException(404, detail={"code": "connection_unavailable"})
        evidence = verified_connection_evidence(connection)
        verified_environment = await aws_environment(connection, secrets_manager)
        if verified_environment is None:
            raise HTTPException(409, detail={"code": "connection_not_verified"})
        current = await connection_access(db, actor, binding, lock=True)
        if current is None:
            raise HTTPException(404, detail={"code": "connection_unavailable"})
        if verified_connection_evidence(current) != evidence or verified_environment["source_revision"] != evidence[1]:
            raise HTTPException(409, detail={"code": "connection_not_verified"})
        if (selection.namespace, selection.native_id) != (verified_environment["namespace"], verified_environment["native_id"]):
            raise HTTPException(422, detail={"code": "invalid_environment_selection"})
    if selection.kind == "pipeline":
        from src.product_onboarding.source_discovery import github_pipeline_resources

        if selection.provider != "github" or selection.connection_ref.kind != "github_installation":
            raise HTTPException(404, detail={"code": "connection_unavailable"})
        resources, _, coverage = await github_pipeline_resources(connection, selection.repository_id, workflow_id=selection.native_id)
        if coverage != "complete" or len(resources) != 1:
            raise HTTPException(404, detail={"code": "source_unavailable"})
        if (resources[0]["namespace"], resources[0]["source_revision"]) != (selection.namespace, selection.source_revision):
            raise HTTPException(409, detail={"code": "source_changed"})
        current = await connection_access(db, actor, binding, lock=True)
        if current is None or current.installation_id != connection.installation_id or current.provider_scope_id != connection.provider_scope_id:
            raise HTTPException(404, detail={"code": "connection_unavailable"})
    existing = await db.scalar(
        select(ProductSourceBinding).where(
            ProductSourceBinding.tenant_id == actor.org_id,
            ProductSourceBinding.project_id == project_id,
            ProductSourceBinding.kind == selection.kind,
            ProductSourceBinding.provider == selection.provider,
            ProductSourceBinding.namespace == selection.namespace,
            ProductSourceBinding.native_id == selection.native_id,
            ProductSourceBinding.unlinked_at.is_(None),
        )
    )
    if existing is not None:
        if {key: value for key, value in existing.desired.items() if key != "display_name"} != {
            key: value for key, value in desired.items() if key != "display_name"
        }:
            raise HTTPException(409, detail={"code": "binding_conflict"})
        headers(response, product.revision)
        response.status_code = 200
        return binding_view(existing, True)
    now = utcnow()
    if verified_environment is not None:
        binding.status = "healthy"
        binding.observed = {
            "observed_at": now.isoformat(),
            "stale_after": (now + timedelta(minutes=15)).isoformat(),
            "source_revision": verified_environment["source_revision"],
            "namespace": selection.namespace,
            "native_id": selection.native_id,
            "verified_environment": verified_environment["verified_environment"],
        }
        basic = dict((project.configuration or {}).get("basic") or {})
        reference = {
            "tenant_id": actor.org_id,
            "project_id": project_id,
            "source_connection_id": connection_id,
            "record_type": "environment",
            "record_id": selection.native_id,
            "record_revision": verified_environment["source_revision"],
        }
        basic["environments"] = [*basic.get("environments", []), reference]
        project.configuration = normalize_environments({**(project.configuration or {}), "basic": basic})
    applied = await db.execute(
        update(Product)
        .where(
            Product.tenant_id == actor.org_id,
            Product.id == product_id,
            Product.revision == expected_revision,
            Product.archived_at.is_(None),
        )
        .values(revision=expected_revision + 1, updated_at=now)
    )
    if applied.rowcount != 1:
        raise HTTPException(412, detail={"code": "stale_revision"})
    await db.refresh(product)
    operation = ProductOperation(
        tenant_id=actor.org_id,
        actor_id=member.id,
        operation="createBinding",
        key_hash=key_hash,
        request_hash=request_hash,
        request={"selection": desired, "expected_revision": expected_revision},
        product_id=product_id,
        project_id=project_id,
        receipt={"binding_id": binding.id},
        expires_at=now + timedelta(days=1),
    )
    db.add(binding)
    await db.flush()
    operation.receipt = {"binding_id": binding.id}
    db.add(operation)
    await db.flush()
    db.add(
        ProductChange(
            tenant_id=actor.org_id,
            product_id=product_id,
            project_id=project_id,
            revision=product.revision,
            actor_id=member.id,
            operation_id=operation.id,
            changed_paths=["/bindings"],
            snapshot={**profile(product, project), **await scoped_snapshots(db, product)},
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "binding_conflict"}) from None
    headers(response, product.revision)
    return binding_view(binding, True)


@bindings_router.post("/{binding_id}/verify", operation_id="verifyBinding", status_code=202, dependencies=[Depends(require_profile_admission)])
async def verify_binding(
    product_id: str,
    project_id: str,
    binding_id: str,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, _, binding = await scoped_binding(db, actor, product_id, project_id, binding_id)
    product_revision = product.revision
    member_id = member.id
    preconditions(idempotency_key, expected_revision)
    if await connection_access(db, actor, binding) is None:
        raise HTTPException(404, detail={"code": "not_found"})
    digest = fingerprint(
        {
            "binding_id": binding_id,
            "product_id": product_id,
            "project_id": project_id,
            "generation": binding.generation,
            "expected_revision": expected_revision,
        }
    )
    key_hash = fingerprint({"key": idempotency_key})
    existing = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "verifyBinding",
            ProductOperation.key_hash == key_hash,
        )
    )
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        operation = existing
    else:
        if product.revision != expected_revision:
            raise HTTPException(412, detail={"code": "stale_revision"})
        operation = ProductOperation(
            tenant_id=actor.org_id,
            actor_id=member.id,
            operation="verifyBinding",
            key_hash=key_hash,
            request_hash=digest,
            state="pending",
            request={"binding_id": binding_id, "generation": binding.generation},
            product_id=product_id,
            project_id=project_id,
            binding_id=binding_id,
            binding_generation=binding.generation,
            expected_revision=expected_revision,
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add(operation)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            await db.refresh(binding)
            operation = await db.scalar(
                select(ProductOperation).where(
                    ProductOperation.tenant_id == actor.org_id,
                    ProductOperation.actor_id == member_id,
                    ProductOperation.operation == "verifyBinding",
                    ProductOperation.key_hash == key_hash,
                )
            )
            if operation is None or operation.request_hash != digest:
                raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    from src.products.verification_operations import run_verification

    result = await run_verification(db, actor, operation, binding, observe_binding, connection_access)
    headers(response, product_revision)
    return result


@bindings_router.delete("/{binding_id}", operation_id="unlinkBinding", dependencies=[Depends(require_profile_admission)])
async def unlink_binding(
    product_id: str,
    project_id: str,
    binding_id: str,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, project = await scoped_product(db, actor, product_id, project_id)
    preconditions(idempotency_key, expected_revision)
    key_hash = fingerprint({"key": idempotency_key})
    request_hash = fingerprint({"binding_id": binding_id, "product_id": product_id, "project_id": project_id, "expected_revision": expected_revision})
    prior_query = select(ProductOperation).where(
        ProductOperation.tenant_id == actor.org_id,
        ProductOperation.actor_id == member.id,
        ProductOperation.operation == "unlinkBinding",
        ProductOperation.key_hash == key_hash,
    )
    prior = await db.scalar(prior_query)
    if prior is not None:
        if prior.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        binding = await db.get(ProductSourceBinding, (actor.org_id, binding_id))
        if binding is None or binding.project_id != project_id:
            raise HTTPException(404, detail={"code": "not_found"})
        headers(response, product.revision)
        return binding_view(binding, False)
    binding = await db.get(ProductSourceBinding, (actor.org_id, binding_id))
    if binding is None or binding.project_id != project_id or binding.unlinked_at is not None:
        raise HTTPException(404, detail={"code": "not_found"})
    if product.archived_at is not None:
        raise HTTPException(409, detail={"code": "product_archived"})
    if product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    now = utcnow()
    home = product.delivery_home_repository
    clear_home = (
        home is not None
        and binding.kind == "repository"
        and binding.provider == "github"
        and binding.native_id.isascii()
        and binding.native_id.isdecimal()
        and int(binding.native_id) == home.get("repository_id")
    )
    if clear_home:
        remaining_sources = await product_github_bindings(db, product)
        clear_home = not any(
            source.id != binding.id and source.native_id.isascii() and source.native_id.isdecimal() and int(source.native_id) == home["repository_id"]
            for source in remaining_sources
        )
    values = {"revision": expected_revision + 1, "updated_at": now}
    if clear_home:
        values["delivery_home_repository"] = None
        values["provenance"] = [
            *(product.provenance or []),
            {
                "path": "/product/delivery_home_repository",
                "actor_id": member.id,
                "recorded_at": now.isoformat().replace("+00:00", "Z"),
                "revision": expected_revision + 1,
                "reason": "source_unbound",
                "binding_id": binding.id,
            },
        ]
    applied = await db.execute(
        update(Product)
        .where(
            Product.tenant_id == actor.org_id,
            Product.id == product_id,
            Product.revision == expected_revision,
            Product.archived_at.is_(None),
        )
        .values(**values)
    )
    if applied.rowcount != 1:
        await db.rollback()
        prior = await db.scalar(prior_query)
        if prior is not None:
            if prior.request_hash != request_hash:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            _, current_product, _ = await scoped_product(db, actor, product_id, project_id)
            current_binding = await db.get(ProductSourceBinding, (actor.org_id, binding_id))
            if current_binding is None or current_binding.project_id != project_id:
                raise HTTPException(404, detail={"code": "not_found"})
            headers(response, current_product.revision)
            return binding_view(current_binding, False)
        raise HTTPException(412, detail={"code": "stale_revision"})
    previous_generation = binding.generation
    await tombstone_binding(db, binding)
    await db.refresh(product)
    operation = ProductOperation(
        tenant_id=actor.org_id,
        actor_id=member.id,
        operation="unlinkBinding",
        key_hash=key_hash,
        request_hash=request_hash,
        state="succeeded",
        request={"binding_id": binding_id},
        receipt={"binding_id": binding_id, "revision": product.revision},
        product_id=product_id,
        project_id=project_id,
        binding_id=binding_id,
        binding_generation=previous_generation,
        expected_revision=expected_revision,
        expires_at=now + timedelta(days=1),
    )
    db.add(operation)
    await db.flush()
    db.add(
        ProductChange(
            tenant_id=actor.org_id,
            product_id=product_id,
            project_id=project_id,
            revision=product.revision,
            actor_id=member.id,
            operation_id=operation.id,
            changed_paths=["/bindings", "/product/delivery_home_repository"] if clear_home else ["/bindings"],
            snapshot={**profile(product, project), **await scoped_snapshots(db, product)},
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "binding_conflict"}) from None
    await db.refresh(binding)
    headers(response, product.revision)
    return binding_view(binding, False)


@router.get("/operations/{operation_id}", operation_id="getOperation")
async def get_operation(
    product_id: str,
    project_id: str,
    operation_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member, product, _ = await scoped_product(db, actor, product_id, project_id)
    operation = await db.get(ProductOperation, (actor.org_id, operation_id))
    if (
        operation is None
        or operation.actor_id != member.id
        or operation.product_id != product_id
        or operation.project_id != project_id
        or operation.operation not in ("verifyBinding", "unlinkBinding", "startHandoff", "discoverSources")
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    if operation.operation == "startHandoff":
        from src.product_onboarding.handoffs import handoff_readback

        headers(response, product.revision)
        return await handoff_readback(db, actor, operation, product.revision)
    if operation.operation == "discoverSources":
        result = await discovery_readback(db, actor, operation, project_id)
        headers(response, product.revision)
        return result
    view = operation_view(operation)
    state, coverage = view["status"], view["coverage"]
    if operation.operation == "verifyBinding":
        binding = await db.get(ProductSourceBinding, (actor.org_id, operation.binding_id))
        if binding is None or binding.project_id != project_id or binding.generation != operation.binding_generation:
            state, coverage = "cancelled", "denied"
        else:
            current = binding_view(binding, await connection_access(db, actor, binding) is not None)
            if current["status"] in ("restricted", "unlinked"):
                state, coverage = "cancelled", "denied"
            elif current["status"] == "degraded" and coverage == "complete":
                state, coverage = "expired", "unavailable"
    if state in ("pending", "running", "failed") and _aware(operation.expires_at) <= utcnow():
        state, coverage = "expired", "unavailable"
    result = {
        "id": operation.id,
        "tenant_id": actor.org_id,
        "product_id": product_id,
        "project_id": project_id,
        "revision": operation.expected_revision + (operation.operation == "unlinkBinding"),
        "kind": operation.operation,
        "state": state,
    }
    if coverage is not None:
        result["coverage"] = coverage
    headers(response, product.revision)
    return result


router.include_router(bindings_router)


async def discovery_readback(db, actor, operation: ProductOperation, project_id: str) -> dict:
    """Project only currently authorized discovery state for polling and resume."""
    import hashlib
    import json

    from src.product_onboarding.discovery import connection_fence, connector_enabled
    from src.product_onboarding.discovery import view as discovery_view

    def denied():
        result = discovery_view(operation, operation.expected_revision, coverage="denied", resources=[])
        result["state"] = "cancelled"
        result.pop("cursor", None)
        return result

    if operation.request.get("adapter") == "devlake" and not await connector_enabled(db, actor.org_id):
        return denied()
    devlake = operation.request.get("adapter") == "devlake"
    revisions = operation.request.get("source_revisions") if devlake else None
    fences = operation.request.get("connection_fences") if devlake else None
    if devlake and (
        not isinstance(revisions, list)
        or len(revisions) != len(operation.request["binding_ids"])
        or not isinstance(fences, list)
        or len(fences) != len(revisions)
    ):
        return denied()
    for index, (binding_id, generation) in enumerate(zip(operation.request["binding_ids"], operation.request["generations"], strict=True)):
        binding = await db.get(ProductSourceBinding, (actor.org_id, binding_id))
        access = (
            (await collection_connection_access(db, actor, binding, operation.product_id) if devlake else await connection_access(db, actor, binding))
            if binding is not None
            else None
        )
        if (
            binding is None
            or binding.project_id != project_id
            or binding.generation != generation
            or binding.unlinked_at is not None
            or binding_view(binding, access is not None)["status"] in ("restricted", "unlinked")
        ):
            return denied()
        if devlake:
            current_fence = hashlib.sha256(json.dumps([str(value) for value in connection_fence(access)], separators=(",", ":")).encode()).hexdigest()
            if (
                binding_view(binding, True)["status"] != "healthy"
                or not isinstance(revisions[index], str)
                or not revisions[index]
                or (binding.observed or {}).get("source_revision") != revisions[index]
                or current_fence != fences[index]
            ):
                return denied()
    return discovery_view(operation, operation.expected_revision)
