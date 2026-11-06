"""Read-only provider proof for an explicitly owner-reviewed maintenance merge."""

import hashlib
from datetime import UTC, datetime
from urllib.parse import quote

from .deployment_release_provider import ReleaseProvider
from .deployment_workflow_provider import MAX_WORKFLOW_RESPONSE_BYTES, decode_workflow_content
from .review_cycle import CycleBlockedError


def require(value, code):
    if not value:
        raise CycleBlockedError("maintenance_" + code)


class MaintenanceProvider(ReleaseProvider):
    async def token(self, binding, *, write=False):
        from .deployment_workflow_provider import mint_installation_token_with_expiry, resolve_tenant_app_credentials

        require(not write, "read_only_provider")
        app, key = await resolve_tenant_app_credentials(binding.org_id)
        token, expires = await mint_installation_token_with_expiry(
            app,
            key,
            binding.installation_id,
            repositories=[binding.repo.split("/", 1)[1]],
            permissions={"actions": "read", "checks": "read", "contents": "read", "metadata": "read", "pull_requests": "read"},
        )
        require(datetime.fromisoformat(expires.replace("Z", "+00:00")) > datetime.now(UTC), "credential_expired")
        return token


async def verified_checks(provider, binding, revision, names):
    checks = await provider.pages(binding, f"/repos/{binding.repo}/commits/{revision}/check-runs", "check_runs")
    latest = {}
    for check in checks:
        require(check.get("head_sha") == revision, "check_head_changed")
        name = check.get("name")
        if name not in latest or check.get("id", 0) > latest[name].get("id", 0):
            latest[name] = check
    require(
        all(name in latest and latest[name].get("status") == "completed" and latest[name].get("conclusion") == "success" for name in names),
        "checks_unverified",
    )
    return [{key: latest[name].get(key) for key in ("id", "name", "head_sha", "status", "conclusion")} for name in sorted(names)]


async def verify_maintenance(provider, binding, request, release, sources, published_heads, publications):
    """Owner approval is explicit; GitHub independently proves merge/check facts."""
    from .maintenance_squash_history import verify_squashed_history

    repo = (await provider.request(binding, "GET", f"/repos/{binding.repo}")).json()
    require(repo.get("id") == binding.provider_repository_id and repo.get("default_branch") == release.default_branch, "repository_changed")
    pr = (await provider.request(binding, "GET", f"/repos/{binding.repo}/pulls/{request.pull_number}")).json()
    require(
        pr.get("number") == request.pull_number
        and pr.get("merged") is True
        and pr.get("merge_commit_sha") == request.source_revision
        and pr.get("head", {}).get("sha") == request.reviewed_head
        and pr.get("head", {}).get("repo", {}).get("id") == binding.provider_repository_id
        and pr.get("base", {}).get("repo", {}).get("id") == binding.provider_repository_id
        and pr.get("base", {}).get("ref") == release.default_branch,
        "merge_changed",
    )
    commit = (await provider.request(binding, "GET", f"/repos/{binding.repo}/commits/{request.source_revision}")).json()
    parents = commit.get("parents", [])
    require(
        commit.get("sha") == request.source_revision and len(parents) == 2 and parents[1].get("sha") == request.reviewed_head,
        "merge_parents_unverified",
    )
    branch = (await provider.request(binding, "GET", f"/repos/{binding.repo}/branches/{quote(release.default_branch, safe='')}")).json()
    main = branch.get("commit", {}).get("sha")
    require(isinstance(main, str) and len(main) == 40, "default_revision_missing")
    squash_proofs, covered = await verify_squashed_history(provider, binding, request, release, publications)
    for source in sorted(set(sources) | (set(published_heads) - covered)):
        if source != request.source_revision:
            await provider.contains(binding, source, request.source_revision)
    if main != request.source_revision:
        await provider.contains(binding, request.source_revision, main)
    files = await provider.pages(binding, f"/repos/{binding.repo}/pulls/{request.pull_number}/files")
    paths = sorted({path for row in files for path in (row.get("filename"), row.get("previous_filename")) if path})
    require(paths and paths == sorted(set(request.reviewed_paths)), "reviewed_paths_changed")
    checks = await verified_checks(provider, binding, request.reviewed_head, request.required_checks)
    path = ".github/workflows/" + release.workflows[0]
    blobs = []
    for revision in (request.reviewed_head, request.source_revision, main):
        record = (
            await provider.request(
                binding, "GET", f"/repos/{binding.repo}/contents/{path}", params={"ref": revision}, max_bytes=MAX_WORKFLOW_RESPONSE_BYTES
            )
        ).json()
        try:
            blobs.append(decode_workflow_content(record))
        except ValueError:
            require(False, "workflow_unverifiable")
    require(blobs[0] == blobs[1] == blobs[2] and hashlib.sha256(blobs[0]).hexdigest() == request.workflow_sha256, "workflow_changed")
    return {
        "repository_id": binding.provider_repository_id,
        "installation_id": binding.installation_id,
        "pull_number": request.pull_number,
        "reviewed_head": request.reviewed_head,
        "source_revision": request.source_revision,
        "merge_parents": [p["sha"] for p in parents],
        "default_revision": main,
        "workflow_path": path,
        "workflow_sha256": request.workflow_sha256,
        "reviewed_paths": paths,
        "owner_review_ref": request.review_ref,
        "required_checks": checks,
        "included_sources": sorted(set(sources)),
        "included_published_heads": sorted(set(published_heads)),
        **({"squashed_repair_history": squash_proofs} if squash_proofs else {}),
    }
