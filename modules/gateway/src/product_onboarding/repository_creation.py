"""GitHub installation-backed repository creation from a pinned template and policy."""

from urllib.parse import quote

import httpx
from fastapi import HTTPException

from src.admin.connections.github_client import GitHubAppClient, github_account_id
from src.admin.connections.service import _get_github_app_credentials
from src.product_onboarding.routes import fingerprint


class RepositoryPreconditionError(Exception):
    def __init__(self, status_code: int, detail: dict):
        self.status_code = status_code
        self.detail = detail


def branch_path(repository: dict) -> str:
    owner = repository.get("owner") or {}
    if not isinstance(owner.get("login"), str) or not isinstance(repository.get("name"), str):
        raise HTTPException(409, detail={"code": "source_unavailable"})
    return f"/repos/{quote(owner['login'], safe='')}/{quote(repository['name'], safe='')}"


def protection_policy(observed: dict) -> dict:
    if not isinstance(observed, dict) or observed.get("restrictions") is not None:
        raise HTTPException(409, detail={"code": "unsupported_protection_policy"})
    checks = observed.get("required_status_checks")
    reviews = observed.get("required_pull_request_reviews")
    if not isinstance(checks, dict) or not isinstance(reviews, dict):
        raise HTTPException(409, detail={"code": "unsupported_protection_policy"})
    for field in (
        "required_linear_history",
        "allow_force_pushes",
        "allow_deletions",
        "required_conversation_resolution",
        "lock_branch",
        "block_creations",
        "required_signatures",
    ):
        setting = observed.get(field) or {}
        if not isinstance(setting, dict) or setting.get("enabled") is True:
            raise HTTPException(409, detail={"code": "unsupported_protection_policy"})
    if observed.get("enforce_admins") is not None and not isinstance(observed["enforce_admins"], dict):
        raise HTTPException(409, detail={"code": "unsupported_protection_policy"})
    if checks.get("checks"):
        raise HTTPException(409, detail={"code": "unsupported_protection_policy"})
    contexts = checks.get("contexts")
    if not isinstance(contexts, list) or any(not isinstance(value, str) for value in contexts):
        raise HTTPException(409, detail={"code": "unsupported_protection_policy"})
    count = reviews.get("required_approving_review_count")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 6:
        raise HTTPException(409, detail={"code": "unsupported_protection_policy"})
    return {
        "required_status_checks": {"strict": checks.get("strict") is True, "contexts": contexts},
        "enforce_admins": observed.get("enforce_admins", {}).get("enabled") is True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": reviews.get("dismiss_stale_reviews") is True,
            "require_code_owner_reviews": reviews.get("require_code_owner_reviews") is True,
            "required_approving_review_count": count,
        },
        "restrictions": None,
    }


async def observed_repository_source(client: GitHubAppClient, installation_id: int, owner_id: str, template_id: str):
    installation = await client.get_installation(installation_id)
    account = installation.get("account") or {}
    owner = account.get("login")
    if (
        github_account_id(installation.get("id")) != str(installation_id)
        or github_account_id(account.get("id")) != owner_id
        or not isinstance(owner, str)
        or not owner
        or installation.get("permissions", {}).get("administration") != "write"
        or installation.get("permissions", {}).get("contents") != "write"
        or installation.get("repository_selection") != "all"
    ):
        raise HTTPException(403, detail={"code": "permission_denied"})
    if not github_account_id(template_id):
        raise HTTPException(409, detail={"code": "unsupported_repository_plan"})
    template = await client.get_installation_repository(installation_id, int(template_id))
    if github_account_id(template.get("id")) != template_id or template.get("is_template") is not True:
        raise HTTPException(409, detail={"code": "source_unavailable"})
    base = branch_path(template)
    branch = template.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise HTTPException(409, detail={"code": "source_unavailable"})
    encoded_branch = quote(branch, safe="")
    head = await client.installation_request(installation_id, "GET", f"{base}/branches/{encoded_branch}")
    sha = (head.get("commit") or {}).get("sha")
    observed = await client.installation_request(installation_id, "GET", f"{base}/branches/{encoded_branch}/protection")
    policy = protection_policy(observed)
    return owner, base, sha, policy


async def repository_plan_source(client: GitHubAppClient, installation_id: int, plan: dict) -> tuple[str, str, dict]:
    template_ref, protections_ref = plan["template_ref"], plan["protections_ref"]
    if (
        template_ref["record_type"] != "repository"
        or protections_ref["record_type"] != "repository_protections"
        or template_ref["record_id"] != protections_ref["record_id"]
    ):
        raise HTTPException(409, detail={"code": "unsupported_repository_plan"})
    owner, base, sha, policy = await observed_repository_source(client, installation_id, plan["owner_native_id"], template_ref["record_id"])
    if sha != template_ref["record_revision"] or fingerprint(policy) != protections_ref["record_revision"]:
        raise HTTPException(412, detail={"code": "stale_revision"})
    return owner, base, policy


async def repository_options(installation_id: int, owner_id: str, template_id: str) -> dict:
    async def read(client):
        _, _, sha, policy = await observed_repository_source(client, installation_id, owner_id, template_id)
        if not isinstance(sha, str) or len(sha) != 40:
            raise HTTPException(409, detail={"code": "source_unavailable"})
        return {"template_revision": sha, "protections_revision": fingerprint(policy), "protections": policy}

    return await with_repository_client(read)


async def with_repository_client(callback):
    app_id, private_key = _get_github_app_credentials()
    if not app_id or not private_key:
        raise HTTPException(503, detail={"code": "source_unavailable"})
    client = GitHubAppClient(app_id, private_key)
    try:
        return await callback(client)
    finally:
        await client.aclose()


async def validate_repository_source(installation_id: int, plan: dict) -> None:
    async def validate(client):
        await repository_plan_source(client, installation_id, plan)

    await with_repository_client(validate)


def repository_marker(operation_id: str) -> str:
    return f"ADP repository creation: {operation_id}"


async def observed_repository(client: GitHubAppClient, installation_id: int, plan: dict, operation_id: str) -> dict | None:
    installation = await client.get_installation(installation_id)
    account = installation.get("account") or {}
    owner = account.get("login")
    if (
        github_account_id(installation.get("id")) != str(installation_id)
        or github_account_id(account.get("id")) != plan["owner_native_id"]
        or not isinstance(owner, str)
        or not owner
    ):
        raise HTTPException(403, detail={"code": "permission_denied"})
    target = f"/repos/{quote(owner, safe='')}/{quote(plan['name'], safe='')}"
    try:
        repository = await client.installation_request(installation_id, "GET", target)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            return None
        raise
    repository_id = github_account_id(repository.get("id"))
    if (
        not repository_id
        or github_account_id((repository.get("owner") or {}).get("id")) != plan["owner_native_id"]
        or repository.get("name") != plan["name"]
        or repository.get("description") != repository_marker(operation_id)
    ):
        raise HTTPException(409, detail={"code": "effect_identity_conflict"})
    exact = await client.get_installation_repository(installation_id, int(repository_id))
    if (
        github_account_id(exact.get("id")) != repository_id
        or github_account_id((exact.get("owner") or {}).get("id")) != plan["owner_native_id"]
        or exact.get("name") != plan["name"]
        or exact.get("description") != repository_marker(operation_id)
    ):
        raise HTTPException(409, detail={"code": "effect_identity_conflict"})
    branch = exact.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise HTTPException(409, detail={"code": "source_unavailable"})
    try:
        protections = await client.installation_request(installation_id, "GET", f"{target}/branches/{quote(branch, safe='')}/protection")
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:
            raise
        protections = None
    try:
        covered = protections is not None and fingerprint(protection_policy(protections)) == plan["protections_ref"]["record_revision"]
    except HTTPException:
        covered = False
    return {
        "native_id": repository_id,
        "owner_native_id": plan["owner_native_id"],
        "applied_plan": plan,
        "protections_applied": covered,
        "coverage": "complete" if covered and exact.get("visibility") == plan["visibility"] else "partial",
    }


async def reconcile_repository(installation_id: int, plan: dict, operation_id: str) -> dict | None:
    async def read(client):
        return await observed_repository(client, installation_id, plan, operation_id)

    return await with_repository_client(read)


async def finish_repository(installation_id: int, plan: dict, operation_id: str, repository_id: str) -> dict:
    async def finish(client):
        observed = await observed_repository(client, installation_id, plan, operation_id)
        if observed is None or observed["native_id"] != repository_id:
            raise HTTPException(409, detail={"code": "effect_identity_conflict"})
        if observed["coverage"] == "complete":
            return observed
        owner, _, policy = await repository_plan_source(client, installation_id, plan)
        target = f"/repos/{quote(owner, safe='')}/{quote(plan['name'], safe='')}"
        repository = await client.get_installation_repository(installation_id, int(repository_id))
        branch = repository.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise HTTPException(409, detail={"code": "source_unavailable"})
        if not observed["protections_applied"]:
            if repository.get("visibility") != "private":
                await client.installation_request(installation_id, "PATCH", target, body={"visibility": "private"})
            await client.installation_request(
                installation_id,
                "PUT",
                f"{target}/branches/{quote(branch, safe='')}/protection",
                body=policy,
            )
        if repository.get("visibility") != plan["visibility"] or (not observed["protections_applied"] and plan["visibility"] != "private"):
            await client.installation_request(installation_id, "PATCH", target, body={"visibility": plan["visibility"]})
        verified = await observed_repository(client, installation_id, plan, operation_id)
        if verified is None or verified["native_id"] != repository_id or verified["coverage"] != "complete":
            raise HTTPException(409, detail={"code": "effect_identity_conflict"})
        return verified

    return await with_repository_client(finish)


async def create_confirmed_repository(installation_id: int, plan: dict, operation_id: str) -> dict:
    async def create(client):
        try:
            owner, template_base, policy = await repository_plan_source(client, installation_id, plan)
            existing = await observed_repository(client, installation_id, plan, operation_id)
            if existing is not None:
                raise HTTPException(409, detail={"code": "repository_exists"})
        except HTTPException as error:
            raise RepositoryPreconditionError(error.status_code, error.detail) from None
        created = await client.installation_request(
            installation_id,
            "POST",
            f"{template_base}/generate",
            body={
                "owner": owner,
                "name": plan["name"],
                "private": True,
                "include_all_branches": False,
                "description": repository_marker(operation_id),
            },
        )
        repository_id = github_account_id(created.get("id"))
        if not repository_id or github_account_id((created.get("owner") or {}).get("id")) != plan["owner_native_id"]:
            raise ValueError("Provider returned an unexpected repository identity")
        target = f"/repos/{quote(owner, safe='')}/{quote(plan['name'], safe='')}"
        branch = created.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ValueError("Provider did not return the repository default branch")
        await client.installation_request(
            installation_id,
            "PUT",
            f"{target}/branches/{quote(branch, safe='')}/protection",
            body=policy,
        )
        if plan["visibility"] != "private":
            await client.installation_request(installation_id, "PATCH", target, body={"visibility": plan["visibility"]})
        observed = await client.get_installation_repository(installation_id, int(repository_id))
        if (
            github_account_id(observed.get("id")) != repository_id
            or github_account_id((observed.get("owner") or {}).get("id")) != plan["owner_native_id"]
            or observed.get("name") != plan["name"]
            or observed.get("description") != repository_marker(operation_id)
            or observed.get("visibility") != plan["visibility"]
        ):
            raise ValueError("Provider repository readback differs from the confirmed plan")
        applied = await client.installation_request(installation_id, "GET", f"{target}/branches/{quote(branch, safe='')}/protection")
        if protection_policy(applied) != policy:
            raise ValueError("Provider protection readback differs from the confirmed policy")
        return {"native_id": repository_id, "owner_native_id": plan["owner_native_id"], "applied_plan": plan}

    return await with_repository_client(create)
