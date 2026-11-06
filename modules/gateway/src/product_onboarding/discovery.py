"""Bounded, checkpointed read-only discovery for selected Product sources."""

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy import update

from src.products.models import (
    ConnectorModuleAdmission,
    ProductOperation,
    ProductSourceBinding,
)
from src.products.source_routes import binding_view, collection_connection_access, connection_access
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.vault import ChannelTenantMap

PAGE_LIMIT = 100
PAGE_BUDGET = 5
ATTEMPT_SECONDS = 30
RECEIPT_LIMIT = 1024 * 1024
CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")
MAX_CURSOR_HISTORY = 50


def progress(receipt):
    return {
        "index": receipt.get("index", 0),
        "page": receipt.get("page", 1),
        "seen": receipt.get("seen", []),
        "seen_cursors": receipt.get("seen_cursors", []),
        "resources": [],
        "coverage": "partial",
    }


def view(operation, revision, *, coverage=None, resources=None):
    receipt = operation.receipt or {}
    observed_coverage = coverage or receipt.get("coverage", "partial")
    result = {
        "id": operation.id,
        "tenant_id": operation.tenant_id,
        "product_id": operation.product_id,
        "project_id": operation.project_id,
        "revision": revision,
        "kind": operation.operation,
        "state": operation.state,
        "coverage": observed_coverage,
    }
    if receipt.get("cursor"):
        if not receipt.get("consumed"):
            result["cursor"] = operation.id
        page = receipt.get("page")
        index = receipt.get("index")
        binding_ids = operation.request.get("binding_ids", [])
        generations = operation.request.get("generations", [])
        if (
            operation.request.get("adapter") == "devlake"
            and observed_coverage in ("partial", "unavailable")
            and type(index) is int
            and 0 <= index < len(binding_ids) == len(generations)
            and isinstance(page, dict)
            and set(page) == {"contract_version", "cursor", "binding_id", "generation", "namespace"}
            and type(page["contract_version"]) is int
            and page["contract_version"] == 1
            and isinstance(page["cursor"], str)
            and 0 < len(page["cursor"]) <= 512
            and page["binding_id"] == binding_ids[index]
            and page["generation"] == generations[index]
            and isinstance(page["namespace"], str)
            and page["namespace"]
        ):
            result["checkpoint_sha256"] = hashlib.sha256(json.dumps(page, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if resources is not None:
        result["resources"] = resources
    elif receipt.get("resources"):
        result["resources"] = receipt["resources"]
    return result


async def discover_binding_page(db, actor, binding, connection, page, limit):
    from src.product_onboarding.source_discovery import (
        github_resources,
        gitlab_resources,
    )

    if type(page) is not int:
        return [], None, "unavailable"
    if binding.provider == "github" and binding.installation_id is not None:
        return await github_resources(connection, page=page, limit=limit)
    if binding.provider == "gitlab" and binding.credential_id is not None:
        return await gitlab_resources(db, actor, connection, page=page, limit=limit)
    return [], None, "unavailable"


def configured_devlake():
    address = os.getenv("CONNECTOR_PROFILE_DISCOVERY_URL", "")
    consumer = os.getenv("CONNECTOR_PROFILE_DISCOVERY_CONSUMER", "")
    try:
        parsed = urlsplit(address)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.path != "/v1/profile-discovery"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or port != 8080
        or not re.fullmatch(r"connector-api\.[a-z0-9-]+\.svc\.cluster\.local", parsed.hostname or "")
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", consumer)
    ):
        return None
    return address, consumer


async def discover_devlake_page(db, actor, binding, connection, page, limit, operation):
    config = configured_devlake()
    if (
        config is None
        or binding.provider not in ("github", "atlassian-mcp", "jenkins", "sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack")
        or limit != PAGE_LIMIT
    ):
        return [], None, "unavailable"
    cursor = "" if type(page) is int and page == 1 else page.get("cursor") if isinstance(page, dict) else None
    if cursor is None or not valid_page(page, binding):
        return [], None, "unavailable"
    body = {
        "tenant_id": operation.tenant_id,
        "product_id": operation.product_id,
        "project_id": operation.project_id,
        "operation_id": operation.id,
        "attempt_id": operation.lease_owner,
        "binding_id": binding.id,
        "cursor": cursor,
        "limit": limit,
        "provider": "jira" if binding.provider == "atlassian-mcp" else binding.provider,
    }
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
            response = await client.post(config[0], json=body)
        if response.status_code != 200 or len(response.content) > RECEIPT_LIMIT:
            return [], None, "unavailable"
        result = response.json()
        resources = result["items"]
        if isinstance(resources, list) and binding.provider in ("sonarqube", "gitlab", "pagerduty", "linear", "slack"):
            resources = [root_scope(item) if isinstance(item, dict) and item.get("namespace") == "" else item for item in resources]
        next_cursor_value = result["next_cursor"]
        coverage = result["coverage"]
        if (
            result.get("contract_version") != 1
            or not isinstance(resources, list)
            or len(resources) > limit
            or coverage not in ("partial", "complete")
            or not isinstance(next_cursor_value, str)
            or (
                bool(next_cursor_value)
                if coverage == "complete"
                else not next_cursor_value and result.get("reason") not in ("discovery_page_limit", "unpaginated_provider_limit")
            )
            or any(
                safe_candidate(item) is None or not provider_scope(binding, item) or item.get("provenance") != "observation_only"
                for item in resources
            )
        ):
            return [], None, "unavailable"
        return resources, {"contract_version": 1, "cursor": next_cursor_value} if next_cursor_value else None, coverage
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return [], None, "unavailable"


def root_scope(item):
    fields = (item.get("kind"), item.get("namespace"), item.get("native_id"), item.get("name"))
    if not all(isinstance(value, str) for value in fields):
        return item
    if item.get("full_name"):
        fields += (item["full_name"],)
    original = "observation-sha256:" + hashlib.sha256("\x00".join(fields).encode()).hexdigest()
    if item.get("source_revision") != original:
        return item
    updated = (fields[0], "/", *fields[2:])
    return {**item, "namespace": "/", "source_revision": "observation-sha256:" + hashlib.sha256("\x00".join(updated).encode()).hexdigest()}


def provider_scope(binding, item):
    provider = binding.provider
    if provider in ("github", "atlassian-mcp", "jenkins"):
        return (
            item["namespace"] == binding.namespace
            and item["kind"] == {"github": "repository", "atlassian-mcp": "tracker", "jenkins": "ci_job"}[provider]
        )
    if provider in ("sonarqube", "pagerduty", "linear", "slack"):
        kind = {"sonarqube": "quality_project", "pagerduty": "incident_service", "linear": "tracker_team", "slack": "communication_channel"}[provider]
        return item["kind"] == kind and item["namespace"] == binding.namespace
    if provider == "argocd":
        project = binding.namespace.split("/", 1)[0]
        return item["kind"] == "deployment_application" and item["namespace"].startswith(project + "/")
    if provider == "gitlab":
        return item["kind"] in ("namespace", "repository") and item["namespace"] == binding.namespace
    return False


RESOURCE_KINDS = {
    "namespace",
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
}


def safe_candidate(resource):
    from src.product_onboarding.source_discovery import safe_name, safe_native

    if not isinstance(resource, dict) or not isinstance(resource.get("kind"), str) or resource["kind"] not in RESOURCE_KINDS:
        return None
    if not all(safe_native(resource.get(field)) for field in ("namespace", "native_id", "source_revision")):
        return None
    name = resource.get("name")
    observed_at = resource.get("observed_at")
    if not isinstance(name, str) or len(name) > 4096 or not isinstance(observed_at, str):
        return None
    try:
        timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
    except ValueError:
        return None
    candidate = {field: resource[field] for field in ("kind", "namespace", "native_id", "source_revision")}
    candidate.update(name=safe_name(name), observed_at=timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"))
    if not candidate["name"]:
        return None
    if isinstance(resource.get("default_branch"), str):
        branch = safe_name(resource["default_branch"])
        if branch:
            candidate["default_branch"] = branch
    if resource.get("kind") == "ci_job":
        if not safe_native(resource.get("full_name")) or resource["full_name"] != resource["native_id"]:
            return None
        candidate["full_name"] = resource["full_name"]
    if resource.get("kind") in ("repository", "namespace") and "full_name" in resource:
        if not safe_native(resource["full_name"]):
            return None
        candidate["full_name"] = resource["full_name"]
    return candidate


def identity(binding, resource):
    raw = json.dumps(
        [
            binding.id,
            resource.get("kind"),
            resource.get("namespace"),
            resource.get("native_id"),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def connection_fence(connection):
    if isinstance(connection, ChannelTenantMap):
        return (
            connection.id,
            connection.org_id,
            connection.provider,
            connection.provider_scope_id,
            connection.installation_id,
            connection.ownership_disputed,
            connection.installed_by_user_id,
        )
    return (
        connection.id,
        connection.org_id,
        connection.provider,
        connection.service,
        connection.credential_type,
        connection.secret_arn,
        connection.operation_fingerprint,
        connection.user_id,
        connection.team_id,
        connection.domain_app_id,
        connection.expires_at,
    )


def binding_fence(binding):
    return (
        binding.project_id,
        binding.generation,
        binding.status,
        binding.provider,
        binding.installation_id,
        binding.credential_id,
        binding.namespace,
        binding.native_id,
        binding.unlinked_at,
    )


def valid_cursor(cursor, binding):
    if binding.provider == "jenkins":
        return False  # This first integration enriches one explicit selected job.
    if binding.provider in ("sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack"):
        match = re.fullmatch(r"v1:(sonarqube|argocd|gitlab|pagerduty|linear|slack):100:([2-5]):([^\s]{0,448})", cursor)
        return match is not None and match.group(1) == binding.provider
    if CURSOR_PATTERN.fullmatch(cursor):
        return True
    token = re.fullmatch(r"v1:jira:100:t([1-9][0-9]{0,5})\.([A-Za-z0-9_-]{2,256})", cursor)
    if token is not None:
        return binding.provider in ("jira", "atlassian-mcp")
    match = re.fullmatch(r"v1:(github|jira):([1-9][0-9]{0,2}):([0-9]{1,6})", cursor)
    return (
        match is not None
        and match.group(1) == ("jira" if binding.provider == "atlassian-mcp" else binding.provider)
        and int(match.group(2)) == PAGE_LIMIT
        and int(match.group(3)) >= (1 if binding.provider == "github" else 0)
    )


def valid_page(page, binding):
    if type(page) is int:
        return page >= 1
    return (
        isinstance(page, dict)
        and set(page) == {"contract_version", "cursor", "binding_id", "generation", "namespace"}
        and type(page["contract_version"]) is int
        and page["contract_version"] == 1
        and isinstance(page["cursor"], str)
        and valid_cursor(page["cursor"], binding)
        and page["binding_id"] == binding.id
        and page["generation"] == binding.generation
        and page["namespace"] == binding.namespace
    )


def next_cursor(receipt, next_page, binding):
    if type(receipt["page"]) is int and isinstance(next_page, str) and len(next_page) <= 10 and next_page.isascii() and next_page.isdecimal():
        if int(next_page) > receipt["page"]:
            return int(next_page), receipt["seen_cursors"]
        return None
    if (
        isinstance(next_page, dict)
        and set(next_page) == {"contract_version", "cursor"}
        and type(next_page["contract_version"]) is int
        and next_page["contract_version"] == 1
        and isinstance(next_page["cursor"], str)
        and valid_cursor(next_page["cursor"], binding)
    ):
        if isinstance(receipt["page"], dict) and receipt["page"]["cursor"].startswith("v1:") != next_page["cursor"].startswith("v1:"):
            return None
        if next_page["cursor"].startswith("v1:") and binding.provider in ("sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack"):
            position = int(next_page["cursor"].split(":", 4)[3])
            previous = receipt["page"]
            if position <= (int(previous["cursor"].split(":", 4)[3]) if isinstance(previous, dict) else 1):
                return None
        elif next_page["cursor"].startswith("v1:"):
            suffix = next_page["cursor"].rsplit(":", 1)[1]
            position = int(suffix[1:].split(".", 1)[0] if suffix.startswith("t") else suffix)
            previous = receipt["page"]
            if isinstance(previous, dict) and previous["cursor"].startswith("v1:"):
                previous_suffix = previous["cursor"].rsplit(":", 1)[1]
                if previous_suffix.startswith("t") and not suffix.startswith("t"):
                    return None
                if previous_suffix.startswith("t") and suffix.startswith("t") and previous_suffix.split(".", 1)[1] == suffix.split(".", 1)[1]:
                    return None
                previous = int(previous_suffix[1:].split(".", 1)[0] if previous_suffix.startswith("t") else previous_suffix)
            elif type(previous) is int:
                previous = 1 if binding.provider == "github" else 0
            else:
                return None
            if position <= previous:
                return None
        cursor_marker = next_page["cursor"]
        if cursor_marker.startswith("v1:jira:100:t"):
            cursor_marker = cursor_marker.rsplit(":", 1)[-1].split(".", 1)[1]
        marker = hashlib.sha256(cursor_marker.encode()).hexdigest()
        if marker in receipt["seen_cursors"]:
            return None
        page = {**next_page, "binding_id": binding.id, "generation": binding.generation, "namespace": binding.namespace}
        return page, [*receipt["seen_cursors"][-(MAX_CURSOR_HISTORY - 1) :], marker]
    return None


async def connector_enabled(db, tenant_id, *, lock=False):
    if os.getenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "").lower() != "true":
        return False
    admission = await db.get(ConnectorModuleAdmission, tenant_id, with_for_update=lock, populate_existing=lock)
    return admission is not None and admission.enabled


async def run_attempt(db, actor, operation: ProductOperation):
    """Claim once; commit each complete page so a lost process cannot lose its cursor."""
    if operation.request.get("adapter") == "devlake" and not await connector_enabled(db, actor.org_id):
        await db.execute(
            update(ProductOperation)
            .where(
                ProductOperation.tenant_id == actor.org_id,
                ProductOperation.id == operation.id,
                ProductOperation.state == "pending",
            )
            .values(state="cancelled", receipt={"coverage": "denied"}, lease_owner=None, lease_until=None)
        )
        await db.commit()
        raise HTTPException(503, detail={"code": "feature_disabled"})
    now = utcnow()
    owner = new_uuid()
    claimed = await db.execute(
        update(ProductOperation)
        .where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.id == operation.id,
            ProductOperation.state == "pending",
            ProductOperation.expires_at > now,
        )
        .values(
            state="running", lease_owner=owner, lease_until=now + timedelta(seconds=ATTEMPT_SECONDS), attempt_count=ProductOperation.attempt_count + 1
        )
    )
    if claimed.rowcount != 1:
        await db.rollback()
        await db.refresh(operation)
        return view(operation, operation.expected_revision)
    await db.commit()
    await db.refresh(operation)
    receipt = progress(operation.receipt or {})
    bindings = operation.request["binding_ids"]
    source_revisions = operation.request.get("source_revisions")
    connection_fences = operation.request.get("connection_fences")
    seen = set(receipt["seen"])
    deadline = time.monotonic() + ATTEMPT_SECONDS
    pages = 0
    failure = None
    while receipt["index"] < len(bindings) and pages < PAGE_BUDGET:
        remaining = deadline - time.monotonic()
        if operation.request.get("adapter") == "devlake" and not await connector_enabled(db, actor.org_id):
            failure = "feature_disabled"
            break
        if remaining <= 0:
            failure = "unavailable"
            break
        binding = await db.get(ProductSourceBinding, (actor.org_id, bindings[receipt["index"]]))
        if (
            binding is None
            or binding.project_id != operation.project_id
            or binding.unlinked_at is not None
            or binding.status in ("revoked", "unlinked")
            or binding.generation != operation.request["generations"][receipt["index"]]
        ):
            failure = "denied"
            break
        if operation.request.get("adapter") == "devlake" and (
            not isinstance(source_revisions, list)
            or len(source_revisions) != len(bindings)
            or not isinstance(source_revisions[receipt["index"]], str)
            or not source_revisions[receipt["index"]]
            or (binding.observed or {}).get("source_revision") != source_revisions[receipt["index"]]
            or binding_view(binding, True)["status"] != "healthy"
        ):
            failure = "denied"
            break
        if not valid_page(receipt["page"], binding):
            failure = "denied"
            break
        connection = (
            await collection_connection_access(db, actor, binding, operation.product_id)
            if operation.request.get("adapter") == "devlake"
            else await connection_access(db, actor, binding)
        )
        if connection is None:
            failure = "denied"
            break
        if operation.request.get("adapter") == "devlake" and (
            not isinstance(connection_fences, list)
            or len(connection_fences) != len(bindings)
            or hashlib.sha256(json.dumps([str(value) for value in connection_fence(connection)], separators=(",", ":")).encode()).hexdigest()
            != connection_fences[receipt["index"]]
        ):
            failure = "denied"
            break
        expected_binding = binding_fence(binding)
        expected_connection = connection_fence(connection)
        try:
            async with asyncio.timeout(remaining):
                reader = discover_devlake_page if operation.request.get("adapter") == "devlake" else discover_binding_page
                if operation.request.get("adapter") == "devlake":
                    resources, next_page, coverage = await reader(db, actor, binding, connection, receipt["page"], PAGE_LIMIT, operation)
                else:
                    resources, next_page, coverage = await reader(db, actor, binding, connection, receipt["page"], PAGE_LIMIT)
        except TimeoutError:
            failure = "unavailable"
            break
        if operation.request.get("adapter") == "devlake" and not await connector_enabled(db, actor.org_id, lock=True):
            failure = "feature_disabled"
            break
        if coverage in ("denied", "unavailable"):
            failure = coverage
            break
        binding = await db.get(ProductSourceBinding, (actor.org_id, binding.id), with_for_update=True, populate_existing=True)
        if binding is None or binding.status in ("revoked", "unlinked") or binding_fence(binding) != expected_binding:
            failure = "denied"
            break
        if operation.request.get("adapter") == "devlake" and (
            (binding.observed or {}).get("source_revision") != source_revisions[receipt["index"]]
            or binding_view(binding, True)["status"] != "healthy"
        ):
            failure = "denied"
            break
        current_connection = (
            await collection_connection_access(db, actor, binding, operation.product_id, lock=True)
            if operation.request.get("adapter") == "devlake"
            else await connection_access(db, actor, binding, lock=True)
        )
        if current_connection is None or connection_fence(current_connection) != expected_connection:
            failure = "denied"
            break
        if len(resources) > PAGE_LIMIT:
            failure = "unavailable"
            break
        next_receipt = {**receipt, "seen": list(receipt["seen"]), "resources": list(receipt["resources"])}
        for resource in resources:
            sanitized = safe_candidate(resource)
            if sanitized is None or (operation.request.get("adapter") == "devlake" and resource.get("provenance") != "observation_only"):
                failure = "unavailable"
                break
            if operation.request.get("adapter") == "devlake":
                sanitized["provenance"] = "observation_only"
            marker = identity(binding, sanitized)
            if marker in seen:
                if operation.request.get("adapter") == "devlake":
                    failure = "unavailable"
                    break
                continue
            candidate = {"binding_id": binding.id, **sanitized}
            if len(json.dumps({**next_receipt, "resources": [*next_receipt["resources"], candidate]}).encode()) > RECEIPT_LIMIT:
                failure = "unavailable"
                break
            seen.add(marker)
            next_receipt["seen"].append(marker)
            next_receipt["resources"].append(candidate)
        if failure:
            break
        if next_page is None:
            next_receipt["index"] += 1
            next_receipt["page"] = 1
            next_receipt["seen_cursors"] = []
        else:
            continuation = next_cursor(receipt, next_page, binding)
            if continuation is None:
                failure = "unavailable"
                break
            next_receipt["page"], next_receipt["seen_cursors"] = continuation
        next_receipt["coverage"] = "partial" if next_receipt["index"] < len(bindings) else "complete"
        next_receipt["cursor"] = next_receipt["coverage"] == "partial"
        changed = await db.execute(
            update(ProductOperation)
            .where(
                ProductOperation.tenant_id == actor.org_id,
                ProductOperation.id == operation.id,
                ProductOperation.lease_owner == owner,
                ProductOperation.state == "running",
            )
            .values(receipt=next_receipt)
        )
        if changed.rowcount != 1:
            raise HTTPException(409, detail={"code": "operation_conflict"})
        await db.commit()
        receipt = next_receipt
        pages += 1
    receipt["coverage"] = "denied" if failure == "feature_disabled" else failure or ("partial" if receipt["index"] < len(bindings) else "complete")
    receipt["cursor"] = receipt["index"] < len(bindings) and failure != "feature_disabled"
    if failure in ("denied", "feature_disabled"):
        receipt["resources"] = []
    await db.execute(
        update(ProductOperation)
        .where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.id == operation.id,
            ProductOperation.lease_owner == owner,
            ProductOperation.state == "running",
        )
        .values(
            state="cancelled" if failure == "feature_disabled" else "succeeded",
            receipt=receipt,
            lease_owner=None,
            lease_until=None,
        )
    )
    await db.commit()
    await db.refresh(operation)
    if failure == "feature_disabled":
        raise HTTPException(503, detail={"code": "feature_disabled"})
    return view(operation, operation.expected_revision)
