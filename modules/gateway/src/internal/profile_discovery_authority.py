"""Resolve current Profile discovery work for the separately served connector."""

import base64
import binascii
import hashlib
import json
import re
from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.atlassian_system_routes import adapter_read, current_system
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_product_routes import service_actor
from src.internal.service_authorization import service_tenant
from src.product_onboarding.discovery import PAGE_LIMIT, connection_fence, connector_enabled, safe_candidate, valid_page
from src.product_onboarding.routes import scoped_session
from src.product_onboarding.source_discovery import DEVLAKE_CREDENTIAL_TYPES, connector_credential, github_resources
from src.products.jira_context_contract import jira_board_in_project
from src.products.models import ProductOperation, ProductSourceBinding
from src.products.source_routes import binding_view, collection_connection_access
from src.shared.database import get_db
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap, UserCredential, UserIdentity

router = APIRouter(prefix="/internal/v1/profile-discovery", tags=["internal-products"], dependencies=[Depends(verify_internal_or_irsa)])


class ResolveProfileWork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    product_id: str = Field(min_length=1, max_length=36)
    project_id: str = Field(min_length=1, max_length=255)
    operation_id: str = Field(min_length=1, max_length=36)
    attempt_id: str = Field(min_length=1, max_length=64)
    binding_id: str = Field(min_length=1, max_length=36)
    cursor: str = Field(max_length=512)
    limit: int = Field(strict=True, ge=1, le=100)


def _current(value, now):
    return value is not None and (value.replace(tzinfo=UTC) if value.tzinfo is None else value) > now


def _github_page_cursor(page):
    return ":".join(("v1", "github", str(PAGE_LIMIT), str(page)))


def _refuse():
    raise HTTPException(404, detail={"code": "profile_discovery_work_unavailable"})


def selection_pin(binding):
    return json.dumps(
        [binding.provider, binding.kind, binding.namespace, binding.native_id, binding.desired, binding.observed],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@router.post("/resolve", operation_id="resolveProfileDiscoveryWork")
async def resolve_profile_discovery_work(
    body: ResolveProfileWork,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    service = service_actor(request)
    if service_tenant(request, body.tenant_id, "connector:profile:read") != body.tenant_id:
        _refuse()
    operation = await db.get(ProductOperation, (body.tenant_id, body.operation_id), with_for_update=True, populate_existing=True)
    now = utcnow()
    if (
        operation is None
        or operation.operation != "discoverSources"
        or operation.state != "running"
        or operation.product_id != body.product_id
        or operation.project_id != body.project_id
        or operation.lease_owner != body.attempt_id
        or operation.attempt_count < 1
        or not _current(operation.lease_until, now)
        or not _current(operation.expires_at, now)
        or not isinstance(operation.request, dict)
        or operation.request.get("adapter") != "devlake"
        or operation.request.get("consumer") != service.agent_registry_id
        or not await connector_enabled(db, body.tenant_id, lock=True)
    ):
        _refuse()
    receipt = operation.receipt
    binding_ids = operation.request.get("binding_ids")
    generations = operation.request.get("generations")
    fences = operation.request.get("connection_fences")
    source_revisions = operation.request.get("source_revisions")
    if (
        not isinstance(receipt, dict)
        or not isinstance(binding_ids, list)
        or not isinstance(generations, list)
        or not isinstance(fences, list)
        or not isinstance(source_revisions, list)
        or len(binding_ids) != len(generations)
        or len(binding_ids) != len(fences)
        or len(binding_ids) != len(source_revisions)
        or type(receipt.get("index")) is not int
        or not 0 <= receipt["index"] < len(binding_ids)
        or binding_ids[receipt["index"]] != body.binding_id
        or body.limit != PAGE_LIMIT
    ):
        _refuse()
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), with_for_update=True, populate_existing=True)
    if (
        binding is None
        or binding.project_id != body.project_id
        or binding.unlinked_at is not None
        or binding.generation != generations[receipt["index"]]
        or binding.status != "healthy"
        or binding.provider not in ("github", "jira", "atlassian-mcp", "jenkins", "sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack")
        or not isinstance(source_revisions[receipt["index"]], str)
        or not source_revisions[receipt["index"]]
        or (binding.observed or {}).get("source_revision") != source_revisions[receipt["index"]]
        or not valid_page(receipt.get("page"), binding)
    ):
        _refuse()
    page = receipt["page"]
    if (type(page) is int and (page != 1 or body.cursor)) or (type(page) is dict and page["cursor"] != body.cursor):
        _refuse()
    owner = service.model_copy(update={"org_id": body.tenant_id, "user_id": operation.actor_id, "account_type": "human", "is_admin": False})
    try:
        member, product, _, _ = await scoped_session(db, owner, body.product_id, body.project_id, operation.session_id)
    except HTTPException:
        _refuse()
    if member.id != operation.actor_id or product.archived_at is not None:
        _refuse()
    connection = await collection_connection_access(db, owner, binding, body.product_id, lock=True)
    if connection is None or binding_view(binding, True, now)["status"] != "healthy":
        _refuse()
    if binding.provider in DEVLAKE_CREDENTIAL_TYPES and (
        not isinstance(connection, UserCredential) or not connector_credential(binding.provider, connection)
    ):
        _refuse()
    revision = hashlib.sha256(json.dumps([str(value) for value in connection_fence(connection)], separators=(",", ":")).encode()).hexdigest()
    if fences[receipt["index"]] != revision:
        _refuse()
    return {
        "tenant_id": body.tenant_id,
        "product_id": body.product_id,
        "project_id": body.project_id,
        "operation_id": operation.id,
        "attempt_id": body.attempt_id,
        "binding_id": binding.id,
        "binding_generation": binding.generation,
        "connection_id": connection.id,
        "connection_revision": revision,
        "provider": "jira" if binding.provider == "atlassian-mcp" else binding.provider,
        "installation": binding.installation_id is not None,
        "namespace": binding.namespace,
        "cursor": body.cursor,
        "limit": PAGE_LIMIT,
    }


class ProviderPageWork(ResolveProfileWork):
    method: str
    path: str
    query: dict[str, list[str]] | None = None
    body: dict | None = None


async def gitlab_personal_identity(db, tenant_id, operation_id, namespace, instance):
    if not namespace.startswith("user:"):
        return True
    operation = await db.get(ProductOperation, (tenant_id, operation_id), populate_existing=True)
    linked = await db.scalar(
        select(UserIdentity.id).where(
            UserIdentity.org_id == tenant_id,
            UserIdentity.user_id == operation.actor_id,
            UserIdentity.provider == "gitlab",
            UserIdentity.provider_user_id == instance + "#" + namespace.removeprefix("user:"),
            UserIdentity.verified_at.is_not(None),
        )
    )
    return linked is not None


@router.post("/provider-page", operation_id="readProfileProviderPage")
async def read_profile_provider_page(body: ProviderPageWork, request: Request, db: AsyncSession = Depends(get_db)):
    from src.auth.vault_routes import get_secrets_manager
    from src.product_onboarding.expanded_provider import PROVIDERS, expected, material, read

    work = await resolve_profile_discovery_work(
        ResolveProfileWork.model_validate(body.model_dump(exclude={"method", "path", "query", "body"})), request, db
    )
    if work["provider"] not in PROVIDERS or work["installation"]:
        _refuse()
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), populate_existing=True)
    pinned_selection = selection_pin(binding)
    method, path, query, payload = expected(work["provider"], binding.namespace, body.cursor, body.limit)
    if (body.method, body.path, body.query or {}, body.body) != (method, path, query, payload):
        _refuse()
    credential = await db.get(UserCredential, work["connection_id"], populate_existing=True)
    if credential is None or credential.id != binding.credential_id or not isinstance((binding.observed or {}).get("credential_version"), str):
        _refuse()
    secrets = get_secrets_manager()
    personal_instance = None
    if work["provider"] == "gitlab" and binding.namespace.startswith("user:"):
        base, _, _ = await material(credential, secrets, expected_version=binding.observed["credential_version"])
        personal_instance = base.removesuffix("/api/v4")
        if not await gitlab_personal_identity(db, body.tenant_id, body.operation_id, binding.namespace, personal_instance):
            _refuse()
    data, headers, _ = await read(credential, secrets, method, path, query, payload, expected_version=binding.observed["credential_version"])
    await db.rollback()
    db.expire_all()
    current = await resolve_profile_discovery_work(
        ResolveProfileWork.model_validate(body.model_dump(exclude={"method", "path", "query", "body"})), request, db
    )
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), populate_existing=True)
    if (
        current != work
        or binding is None
        or selection_pin(binding) != pinned_selection
        or (
            personal_instance is not None
            and not await gitlab_personal_identity(db, body.tenant_id, body.operation_id, binding.namespace, personal_instance)
        )
    ):
        _refuse()
    return {"contract_version": 1, "work": work, "data": data, "headers": headers}


@router.post("/jenkins-page", operation_id="readProfileJenkinsPage")
async def read_profile_jenkins_page(body: ResolveProfileWork, request: Request, db: AsyncSession = Depends(get_db)):
    from src.auth.vault_routes import get_secrets_manager
    from src.product_onboarding.jenkins import read_jenkins, selection

    work = await resolve_profile_discovery_work(body, request, db)
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), populate_existing=True)
    if (
        work["provider"] != "jenkins"
        or body.cursor != ""
        or work["installation"]
        or binding.kind != "ci_job"
        or not selection(binding.namespace, binding.native_id)
        or not isinstance((binding.observed or {}).get("credential_version"), str)
        or not binding.observed["credential_version"]
    ):
        _refuse()
    pinned_selection = selection_pin(binding)
    credential = await db.get(UserCredential, work["connection_id"], populate_existing=True)
    try:
        data, _ = await read_jenkins(
            credential,
            get_secrets_manager(),
            name=binding.native_id,
            expected_version=binding.observed["credential_version"],
        )
    except HTTPException:
        _refuse()
    await db.rollback()
    db.expire_all()
    if await resolve_profile_discovery_work(body, request, db) != work:
        _refuse()
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), populate_existing=True)
    if binding is None or selection_pin(binding) != pinned_selection:
        _refuse()
    # Preserve the native job object in the envelope expected by the upstream
    # discovery helper. No parent inventory or sibling reads are authorized.
    return {"contract_version": 1, "work": work, "data": {"jobs": [data]}}


@router.post("/github-page", operation_id="readProfileGithubPage")
async def read_profile_github_page(
    body: ResolveProfileWork,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    work = await resolve_profile_discovery_work(body, request, db)
    if work["provider"] != "github" or not work["installation"]:
        _refuse()
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), populate_existing=True)
    pinned_selection = selection_pin(binding)
    page = 1
    if body.cursor:
        prefix = _github_page_cursor("")
        if not body.cursor.startswith(prefix) or not body.cursor[len(prefix) :].isdigit():
            _refuse()
        page = int(body.cursor[len(prefix) :])
        if page < 2 or page > 1_000_000:
            _refuse()
    mapping = await db.get(ChannelTenantMap, work["connection_id"], with_for_update=True, populate_existing=True)
    if mapping is None or mapping.org_id != body.tenant_id or mapping.provider != "github":
        _refuse()
    try:
        resources, next_page, coverage = await github_resources(mapping, page=page, limit=PAGE_LIMIT)
    except Exception:
        _refuse()
    await db.rollback()
    if (
        coverage not in {"partial", "complete"}
        or not isinstance(resources, list)
        or len(resources) > PAGE_LIMIT
        or (coverage == "partial") != (next_page is not None)
    ):
        _refuse()
    if next_page is not None and next_page != str(page + 1):
        _refuse()
    candidates = [safe_candidate(resource) for resource in resources]
    if any(candidate is None or candidate["kind"] != "repository" or candidate["namespace"] != work["namespace"] for candidate in candidates):
        _refuse()
    db.expire_all()
    if await resolve_profile_discovery_work(body, request, db) != work:
        _refuse()
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), populate_existing=True)
    if binding is None or selection_pin(binding) != pinned_selection:
        _refuse()
    return {
        "contract_version": 1,
        "work": work,
        "items": candidates,
        "next_cursor": _github_page_cursor(next_page) if next_page else "",
        "coverage": coverage,
    }


@router.post("/jira-boards-page", operation_id="readProfileJiraBoardsPage")
async def read_profile_jira_boards_page(
    body: ResolveProfileWork,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    work = await resolve_profile_discovery_work(body, request, db)
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), with_for_update=True, populate_existing=True)
    if (
        work["provider"] != "jira"
        or work["installation"]
        or binding.provider != "atlassian-mcp"
        or binding.kind != "tracker"
        or not isinstance(binding.desired.get("jira_project_id"), str)
        or not binding.desired["jira_project_id"].isdigit()
    ):
        _refuse()
    pinned_selection = selection_pin(binding)
    project_id = binding.desired["jira_project_id"]
    project_key = binding.desired.get("jira_project_key")
    if not isinstance(project_key, str) or not project_key:
        _refuse()
    board_id = binding.desired.get("board_id")
    if (
        board_id is not None
        and (type(board_id) is not int or not 0 < board_id <= (1 << 63) - 1)
        or binding.native_id != f"{project_id}:{board_id if board_id is not None else 'all'}"
        or board_id is not None
        and (not isinstance(binding.desired.get("board_type"), str) or not binding.desired["board_type"])
    ):
        _refuse()
    if board_id is not None and body.cursor:
        _refuse()
    start_at = 0
    arguments = {"projectKeyOrId": project_id, "maxResults": PAGE_LIMIT}
    if body.cursor:
        prefix = f"v1:jira:{PAGE_LIMIT}:"
        if not body.cursor.startswith(prefix):
            _refuse()
        suffix = body.cursor[len(prefix) :]
        if suffix.startswith("t"):
            match = re.fullmatch(r"t([1-9][0-9]{0,5})\.([A-Za-z0-9_-]{2,256})", suffix)
            if match is None:
                _refuse()
            start_at = int(match.group(1))
            encoded = match.group(2)
            try:
                token = base64.urlsafe_b64decode(encoded + "===").decode("ascii")
            except (ValueError, UnicodeError, binascii.Error):
                _refuse()
            if not token or not token.isprintable() or len(token) > 180 or base64.urlsafe_b64encode(token.encode()).decode().rstrip("=") != encoded:
                _refuse()
            arguments["nextPageToken"] = token
        elif suffix.isascii() and suffix.isdigit():
            start_at = int(suffix)
            arguments["startAt"] = start_at
        else:
            _refuse()
        if not 0 < start_at <= 1_000_000:
            _refuse()
    else:
        arguments["startAt"] = 0
    credential = await db.get(UserCredential, work["connection_id"], with_for_update=True, populate_existing=True)
    if credential is None or credential.org_id != body.tenant_id:
        _refuse()
    try:
        _, connection = await current_system(db, credential.id, body.tenant_id, body.product_id, lock=True)
    except HTTPException:
        _refuse()
    if (
        connection.site_id != work["namespace"]
        or connection.connection_method != "service_account"
        or not isinstance(connection.schemas, dict)
        or ("getJiraBoardConfig" if board_id is not None else "listJiraBoards") not in connection.schemas
    ):
        _refuse()
    try:
        result = await adapter_read(
            connection, "board" if board_id is not None else "boards", {"boardId": board_id} if board_id is not None else arguments, timeout=20
        )
    except Exception:
        _refuse()
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        _refuse()
    if board_id is not None:
        if (
            str(data.get("id")) != str(board_id)
            or data.get("type") != binding.desired["board_type"]
            or not jira_board_in_project(data, project_id, project_key)
        ):
            _refuse()
        data = {"startAt": 0, "maxResults": PAGE_LIMIT, "total": 1, "isLast": True, "values": [data]}
    continuation = data.get("nextPageToken")
    boards = data.get("values")
    total = data.get("total")
    end = start_at + len(boards) if isinstance(boards, list) else -1
    if (
        not isinstance(boards, list)
        or len(boards) > PAGE_LIMIT
        or type(data.get("startAt")) is not int
        or data["startAt"] != start_at
        or type(data.get("maxResults")) is not int
        or not 1 <= data["maxResults"] <= PAGE_LIMIT
        or len(boards) > data["maxResults"]
        or type(total) is not int
        or not end <= total <= 1_000_000
        or type(data.get("isLast")) is not bool
        or data["isLast"] != (end == total)
        or (not data["isLast"] and (not boards or end <= start_at))
    ):
        _refuse()
    if continuation is not None and (
        data["isLast"]
        or not isinstance(continuation, str)
        or not continuation.isascii()
        or not continuation.isprintable()
        or not 0 < len(continuation) <= 180
    ):
        _refuse()
    observed = utcnow().isoformat().replace("+00:00", "Z")
    items = []
    seen = set()
    for board in boards:
        if not isinstance(board, dict) or type(board.get("id")) not in (int, str):
            _refuse()
        if not jira_board_in_project(board, project_id, project_key):
            _refuse()
        board_id = str(board["id"])
        if (
            not board_id.isascii()
            or not board_id.isdecimal()
            or len(board_id) > 32
            or not 0 < int(board_id)
            or board_id.startswith("0")
            or board_id in seen
        ):
            _refuse()
        seen.add(board_id)
        try:
            digest = hashlib.sha256(json.dumps(board, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError):
            _refuse()
        item = safe_candidate(
            {
                "kind": "tracker",
                "namespace": work["namespace"],
                "native_id": board_id,
                "name": board.get("name"),
                "source_revision": "observation-sha256:" + digest,
                "observed_at": observed,
            }
        )
        if item is None or item["name"] == "[redacted]":
            _refuse()
        items.append(item)
    await db.rollback()
    db.expire_all()
    if await resolve_profile_discovery_work(body, request, db) != work:
        _refuse()
    binding = await db.get(ProductSourceBinding, (body.tenant_id, body.binding_id), populate_existing=True)
    if binding is None or selection_pin(binding) != pinned_selection:
        _refuse()
    next_cursor = "" if data["isLast"] else f"v1:jira:{PAGE_LIMIT}:{end}"
    if continuation is not None:
        encoded = base64.urlsafe_b64encode(continuation.encode()).decode().rstrip("=")
        next_cursor = f"v1:jira:{PAGE_LIMIT}:t{end}.{encoded}"
    return {
        "contract_version": 1,
        "items": items,
        "coverage": "complete" if data["isLast"] else "partial",
        "work": work,
        "next_cursor": next_cursor,
    }
