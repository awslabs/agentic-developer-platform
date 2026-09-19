"""Provider-confirmed PR identity shared by run registration and operator recovery."""

from __future__ import annotations

import re

import httpx

from .pr_bindings import PullRequestIdentity


class PrIdentityError(Exception):
    """The provider could not confirm the requested identity; never includes credentials."""


async def resolve_pr_identity(*, org_id: str, installation_id: int, repo: str, pr_number: int) -> PullRequestIdentity:
    """Read identity under the tenant's authorized installation and repository.

    Callers authorize the tenant, installation and repository before calling. The
    PR number selects an artifact; its immutable IDs and head come only from GitHub.
    """
    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    if (
        not org_id
        or type(installation_id) is not int
        or installation_id < 1
        or type(pr_number) is not int
        or pr_number < 1
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        or any(part in {".", ".."} for part in repo.split("/"))
    ):
        raise PrIdentityError("Pull-request identity could not be verified.")
    try:
        app_id, key = await resolve_tenant_app_credentials(org_id)
        token, _ = await mint_installation_token_with_expiry(
            app_id,
            key,
            installation_id,
            repositories=[repo.split("/")[1]],
            permissions={"metadata": "read", "pull_requests": "read"},
        )
        async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10, trust_env=False, follow_redirects=False) as client:
            response = await client.get(
                f"/repos/{repo}/pulls/{pr_number}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            pull_request = response.json()
        repository = pull_request["base"]["repo"]
        repository_id = repository["id"]
        node_id = pull_request["node_id"]
        head_sha = pull_request["head"]["sha"]
        if (
            type(repository_id) is not int
            or repository_id < 1
            or not isinstance(node_id, str)
            or not node_id.strip()
            or not isinstance(head_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", head_sha)
            or pull_request["number"] != pr_number
            or repository["full_name"].lower() != repo.lower()
        ):
            raise PrIdentityError("Pull-request identity could not be verified.")
        return PullRequestIdentity(
            provider_repository_id=repository_id,
            provider_pr_node_id=node_id,
            repo=repository["full_name"],
            pr_number=pr_number,
            head_sha=head_sha,
        )
    except PrIdentityError:
        raise
    except Exception:
        # App resolution/token errors may contain provider bodies or signed URLs.
        # Both callers expose this failure, so keep that material out of the error.
        raise PrIdentityError("Pull-request identity could not be verified.") from None
