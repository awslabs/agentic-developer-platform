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


async def resolve_head_check_runs(*, org_id: str, installation_id: int, repo: str, head_sha: str) -> frozenset[str]:
    """The check runs the provider reports for one exact commit (#5146).

    Review evidence that cites a check run is only evidence if that check run really
    ran, really belongs to this repository, and really ran against the commit under
    review. Those are three separate facts and none of them is in the submitted
    document — a reviewer can write any check-run id, including a real one from
    another commit or another repository, and before this existed nothing looked.

    Scoped to ``head_sha`` in the request path rather than filtered afterwards: the
    provider resolves check runs *for a ref*, so a run against a different commit is
    simply not in the response. That is what makes the returned set usable as a
    trusted set — membership means "the provider says this ran against this commit",
    not "the reviewer says so".

    ``conclusion`` is deliberately **not** filtered on. This answers "does this
    reference resolve", not "did the check pass"; whether a failing check undermines
    the review is the reviewer's judgement, recorded in its findings. Filtering here
    would silently convert a cited failure into an unverifiable reference and change
    a reasoned request-changes into ``UNTRUSTED_ARTIFACT``.

    Returns:
        References in the ``check-run:<id>`` form the review contract uses, so a
        caller can intersect directly with the document's refs without either side
        re-deriving the spelling. Empty when the commit has no check runs — which is
        a real answer and must not be read as a failure to look.

    Raises:
        PrIdentityError: when the provider could not be read. The caller must treat
            this as "not verified" and refuse, never as an empty set: the two are
            indistinguishable to a later reader and mean opposite things.
    """
    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    if (
        not org_id
        or type(installation_id) is not int
        or installation_id < 1
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        or any(part in {".", ".."} for part in repo.split("/"))
        or not re.fullmatch(r"[0-9a-f]{40,64}", head_sha)
    ):
        raise PrIdentityError("Check runs could not be verified.")
    try:
        app_id, key = await resolve_tenant_app_credentials(org_id)
        token, _ = await mint_installation_token_with_expiry(
            app_id,
            key,
            installation_id,
            repositories=[repo.split("/")[1]],
            # `checks: read` and nothing else. The narrowest grant that can answer
            # the question, so a token minted for verification cannot write a check,
            # a comment or a review.
            permissions={"metadata": "read", "checks": "read"},
        )
        refs: set[str] = set()
        async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10, trust_env=False, follow_redirects=False) as client:
            # Paged because a large repository's head can carry more check runs than
            # one page holds, and a silently truncated first page would make a
            # legitimately-cited run on page two look unverifiable.
            for page in range(1, 6):
                response = await client.get(
                    f"/repos/{repo}/commits/{head_sha}/check-runs",
                    params={"per_page": 100, "page": page},
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                )
                response.raise_for_status()
                body = response.json()
                runs = body.get("check_runs") or []
                for run in runs:
                    identifier = run.get("id")
                    if type(identifier) is int and identifier > 0:
                        refs.add(f"check-run:{identifier}")
                if len(runs) < 100:
                    break
        return frozenset(refs)
    except PrIdentityError:
        raise
    except Exception:
        # Same redaction rule as `resolve_pr_identity`: provider bodies and signed
        # URLs must not reach a caller that surfaces this to a reviewer.
        raise PrIdentityError("Check runs could not be verified.") from None
