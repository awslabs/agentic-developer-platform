"""Read-only source-size diagnostics for the saved Security configuration."""

import asyncio
import json
import re
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select

from src.admin.connections.github_client import GitHubAppClient, github_account_id
from src.products.l5_branch_sources import branch_binding_identity, repository_owned_by_mapping
from src.products.models import ProductSourceBinding
from src.products.source_capture_metadata import (
    MAX_TREE_BYTES,
    repository_capture_preflight,
)
from src.products.source_capture_metadata import (
    REPOSITORY_MAX_BYTES as MAX_CONTENT,
)
from src.products.source_capture_metadata import (
    REPOSITORY_MAX_FILES as MAX_FILES,
)
from src.products.source_routes import connection_access

MAX_SOURCES = 20


def denied():
    return HTTPException(404, detail={"code": "not_found"}, headers={"Cache-Control": "no-store"})


async def metadata_json(client, path, headers, *, limit, params=None):
    """Bound the response before parsing it; no contents, blobs or archive API."""
    async with client.stream("GET", path, headers=headers, params=params) as response:
        response.raise_for_status()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > limit:
                raise ValueError("source_metadata_incomplete")
            content.extend(chunk)
    return json.loads(content)


async def read_repository_preflight(binding, mapping, reference, exclusions):
    from src.admin.connections.service import _get_github_app_credentials

    revision = reference["record_revision"]
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("immutable_source_required")
    try:
        app_id, private_key = _get_github_app_credentials()
    except Exception:
        raise ValueError("source_metadata_unavailable") from None
    if not app_id or not private_key:
        raise ValueError("source_metadata_unavailable")
    client = GitHubAppClient(app_id, private_key)
    try:
        async with asyncio.timeout(25):
            repository = await client.get_installation_repository(int(mapping.installation_id), int(binding.native_id))
            name = repository.get("full_name") if isinstance(repository, dict) else None
            if (
                not repository_owned_by_mapping(repository, binding, mapping)
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
            ):
                raise denied()
            token = await client.get_installation_token(int(mapping.installation_id))
            if not token:
                raise ValueError("source_metadata_unavailable")
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            # The ordinary /commits API can include patches. Git commit objects
            # contain only commit metadata and the immutable tree identity.
            commit = await metadata_json(client._http_client, f"/repos/{name}/git/commits/{revision}", headers, limit=256 * 1024)
            if not isinstance(commit, dict) or commit.get("sha") != revision:
                raise ValueError("immutable_source_required")
            commit_tree = commit.get("tree")
            tree_sha = commit_tree.get("sha") if isinstance(commit_tree, dict) else None
            if not isinstance(tree_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", tree_sha):
                raise ValueError("source_metadata_incomplete")
            tree = await metadata_json(
                client._http_client, f"/repos/{name}/git/trees/{tree_sha}", headers, limit=MAX_TREE_BYTES, params={"recursive": "1"}
            )
            return repository_capture_preflight(tree, tree_sha, exclusions, max_bytes=MAX_CONTENT, max_files=MAX_FILES)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {401, 403, 404}:
            raise denied() from None
        raise ValueError("source_metadata_unavailable") from None
    finally:
        await client.aclose()


async def saved_selection(backend, product_id, project_id, expected_revision):
    if backend.actor.account_type != "human":
        raise denied()
    configuration, version = await backend.current(product_id, project_id, "security")
    if version.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision", "message": "Refresh the saved configuration before checking its source size."})
    settings = version.settings
    sources = [source for source in settings.get("source_refs", []) if source.get("record_type") in {"repository", "repository_commit"}]
    if len(sources) > MAX_SOURCES:
        raise HTTPException(
            422, detail={"code": "source_preflight_limit", "message": "Source preflight supports at most 20 saved repositories per check."}
        )
    targets, states = [], []
    for source in sources:
        if (source.get("tenant_id"), source.get("project_id")) != (backend.actor.org_id, project_id):
            raise denied()
        rows = (
            await backend.db.scalars(
                select(ProductSourceBinding)
                .where(
                    ProductSourceBinding.tenant_id == backend.actor.org_id,
                    ProductSourceBinding.project_id == project_id,
                    ProductSourceBinding.installation_id == source.get("source_connection_id"),
                    ProductSourceBinding.native_id == source.get("record_id"),
                    ProductSourceBinding.kind == "repository",
                    ProductSourceBinding.provider == "github",
                    ProductSourceBinding.unlinked_at.is_(None),
                    ProductSourceBinding.status != "revoked",
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(rows) != 1:
            raise denied()
        binding = rows[0]
        mapping = await connection_access(backend.db, backend.actor, binding)
        revision = (binding.observed or {}).get("source_revision") or (binding.desired or {}).get("source_revision")
        if (
            mapping is None
            or not github_account_id(mapping.installation_id)
            or not github_account_id(binding.native_id)
            or revision != source.get("record_revision")
        ):
            raise denied()
        targets.append((binding, mapping, dict(source)))
        states.append([branch_binding_identity(binding, mapping), binding.revision, binding.status, binding.observed, binding.desired])
    state = json.dumps([configuration.configuration_id, version.revision, version.hash, settings, states], sort_keys=True, separators=(",", ":"))
    return configuration.configuration_id, targets, settings.get("exclusions", []), state


async def source_preflight(backend, product_id, project_id, expected_revision):
    identifier, targets, exclusions, before = await saved_selection(backend, product_id, project_id, expected_revision)
    semaphore = asyncio.Semaphore(4)

    async def check(target):
        binding, mapping, reference = target
        try:
            async with semaphore:
                result = await read_repository_preflight(binding, mapping, reference, exclusions)
        except (ValueError, httpx.HTTPError, TimeoutError) as error:
            known = {
                "source_metadata_incomplete",
                "source_metadata_invalid",
                "unsupported_source_entry",
                "invalid_exclusions",
                "immutable_source_required",
            }
            result = {
                "status": "unknown",
                "metadata_coverage": "unknown",
                "reasons": [str(error) if isinstance(error, ValueError) and str(error) in known else "source_metadata_unavailable"],
            }
        return {"source_ref": reference, **result}

    tasks = [asyncio.create_task(check(target)) for target in targets]
    try:
        async with asyncio.timeout(30):
            results = await asyncio.gather(*tasks)
    except TimeoutError:
        results = [
            {"source_ref": reference, "status": "unknown", "metadata_coverage": "unknown", "reasons": ["source_metadata_unavailable"]}
            for _, _, reference in targets
        ]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # Never release metadata obtained under revoked access or a changed saved
    # configuration. Refresh ORM state as well as re-running source authority.
    backend.db.expire_all()
    _, _, _, after = await saved_selection(backend, product_id, project_id, expected_revision)
    if before != after:
        raise HTTPException(
            409,
            detail={"code": "source_selection_changed", "message": "Source access or selection changed. Refresh the saved setup and check again."},
        )
    known = [item for item in results if item["metadata_coverage"] == "complete"]
    combined_files = sum(item["selected_files"] for item in known)
    combined_bytes = sum(item["selected_bytes"] for item in known)
    combined_reasons = []
    if combined_files > MAX_FILES:
        combined_reasons.append("combined_source_file_count_exceeded")
    if combined_bytes > MAX_CONTENT:
        combined_reasons.append("combined_source_content_bytes_exceeded")
    if not results:
        status = "not_applicable"
    elif combined_reasons or any(item["status"] == "exceeds_limits" for item in results):
        status = "exceeds_limits"
    elif len(known) != len(results):
        status = "unknown"
    else:
        status = "within_limits"
    return {
        "configuration_id": identifier,
        "configuration_revision": expected_revision,
        "observed_at": datetime.now(UTC).isoformat(),
        "sources": results,
        "limits": {"max_bytes": MAX_CONTENT, "max_files": MAX_FILES},
        "status": status,
        "combined_repositories": {
            "metadata_coverage": "complete" if len(known) == len(results) else "partial",
            "known_selected_files": combined_files,
            "known_selected_bytes": combined_bytes,
            "snapshot_archive_bytes": sum(item["archive_bytes"] for item in known),
            "native_combined_archive_bytes": None,
            "reasons": combined_reasons,
        },
        "advisory_only": True,
        "content_captured": False,
        "run_authority_checked": False,
        "limitations": [
            "Repository metadata only; document content and native analysis coverage are not checked.",
            "Run authorization may impose lower limits; multiple sources in one run share cumulative file and byte limits.",
            "This check does not grant execution authority or change saved sources, exclusions, or readiness.",
        ],
    }


def create_router(backend_dependency):
    router = APIRouter()

    @router.get(
        "/v1/products/{product_id}/projects/{project_id}/services/{service_id}/configuration/source-preflight",
        operation_id="getSecuritySourcePreflight",
    )
    async def read_source_preflight(
        product_id: str,
        project_id: str,
        service_id: str,
        response: Response,
        configuration_revision: int = Query(ge=1),
        backend=Depends(backend_dependency),
    ):
        if service_id != "security":
            raise denied()
        response.headers["Cache-Control"] = "no-store"
        return await source_preflight(backend, product_id, project_id, configuration_revision)

    return router
