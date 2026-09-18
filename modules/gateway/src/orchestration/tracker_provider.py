"""The GitHub adapter for tracker projection: read an issue body, write it back.

Issue #5284. Split out of `tracker_projection.py` so the rendering and
sentinel-safety rules stay pure and unit-testable with no network in sight, and so
the provider boundary is one small file to audit. GitHub is an **output adapter
only** — nothing here returns a value that influences an engine decision; the body
is read solely to locate the sentinels and preserve the text around them.

**Why not `GitHubAppClient`.** That class (`admin/connections/github_client.py`)
mints a token with the installation's full permission set. This pass needs exactly
`issues: write` on exactly one repository, so it follows the newer least-privilege
idiom used by `pr_identity.py`, `work_admission.py` and `agentauth/waves.py`:
`mint_installation_token_with_expiry(..., repositories=[name], permissions={...})`
against a locally-scoped `httpx.AsyncClient`. A projection that can only edit issues
in one repository cannot be turned into a lever on anything else.

`trust_env=False, follow_redirects=False` match those callers: environment proxies
must not silently reroute a credentialed call, and a redirect chain is how a
credentialed request ends up somewhere it was never authorized to go — GitHub
answers a moved repository with a 301 whose `Location` may be a different owner.

Errors deliberately carry no provider body. App resolution and token minting can
surface signed URLs and secret material in their responses, so every failure is
re-raised as a fixed-string :class:`TrackerProviderError` with `from None`, exactly
as `pr_identity.py` does. The caller counts the failure and retries on a later tick.
"""

from __future__ import annotations

import re

import httpx

# GitHub caps an issue body at 65536 characters and rejects a longer one. Checked
# before the write so an over-long body is a counted, explained refusal rather than
# an opaque 422 that reads like an outage.
_MAX_BODY = 65536

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class TrackerProviderError(Exception):
    """A provider call did not complete. Never includes credentials or response bodies."""


def _validated(repo: str, issue_number: int) -> str:
    """Reject a malformed target before it reaches the provider.

    Path-traversal segments are excluded explicitly: `owner/..` would otherwise
    resolve to a different API path once joined onto the base URL. Same check as
    `pr_identity.resolve_pr_identity`, for the same reason.
    """
    if type(issue_number) is not int or issue_number < 1:
        raise TrackerProviderError("Tracker target is not a usable issue number.")
    if not _REPO_RE.fullmatch(repo or "") or any(part in {".", ".."} for part in (repo or "").split("/")):
        raise TrackerProviderError("Tracker target is not a usable repository.")
    return repo.split("/")[1]


class GitHubTrackerProvider:
    """Reads and writes one issue body under the tenant's own installation.

    Stateless: a token is minted per call rather than cached on the instance. The
    tick is a short-lived Lambda invocation, so a cached token buys nothing and a
    cached one outliving its usefulness is a real failure mode the handler already
    works around (`reset_engine()`).
    """

    async def read_issue_body(self, *, org_id: str, installation_id: int, repo: str, issue_number: int) -> str:
        """The issue's current body text.

        Read immediately before the splice, because preserving the user's text
        requires knowing what it currently is. The returned string is treated as
        untrusted display data by the caller: it is scanned for sentinels and for a
        snapshot marker, and never parsed for instructions.
        """
        name = _validated(repo, issue_number)
        token = await self._token(org_id=org_id, installation_id=installation_id, repository=name, write=False)
        try:
            async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.get(
                    f"/repos/{repo}/issues/{issue_number}",
                    headers=self._headers(token),
                )
                response.raise_for_status()
                payload = response.json()
        except TrackerProviderError:
            raise
        except Exception:
            raise TrackerProviderError("The tracker issue could not be read.") from None

        body = payload.get("body")
        # A GitHub issue with an empty body returns JSON null, which is a legitimate
        # state (and one where the sentinels are simply absent, so the caller
        # refuses). Coerced here rather than left as None so every downstream string
        # operation has a string.
        return body if isinstance(body, str) else ""

    async def write_issue_body(self, *, org_id: str, installation_id: int, repo: str, issue_number: int, body: str) -> None:
        """Replace the issue body with `body`.

        The caller has already spliced the region into the body it read, so this is
        an unconditional PATCH of a value derived from that read. GitHub's issue
        endpoint offers no documented conditional-update primitive, so the caller
        serializes engine writers around the complete read/compare/write operation
        with a target-scoped database advisory lock.
        """
        name = _validated(repo, issue_number)
        if not isinstance(body, str) or not body.strip():
            # An empty body would erase the issue. Refused unconditionally: there is
            # no legitimate projection that blanks the issue it is reporting on.
            raise TrackerProviderError("Refusing to write an empty tracker issue body.")
        if len(body) > _MAX_BODY:
            raise TrackerProviderError("The updated tracker body exceeds the provider's size limit.")

        token = await self._token(org_id=org_id, installation_id=installation_id, repository=name, write=True)
        try:
            async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.patch(
                    f"/repos/{repo}/issues/{issue_number}",
                    headers=self._headers(token),
                    json={"body": body},
                )
                response.raise_for_status()
        except TrackerProviderError:
            raise
        except Exception:
            raise TrackerProviderError("The tracker issue could not be updated.") from None

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    async def _token(*, org_id: str, installation_id: int, repository: str, write: bool) -> str:
        """Mint a token scoped to one repository and the least verb needed.

        The read path asks for `issues: read` and the write path for `issues: write`,
        rather than one token used for both: the read happens on every examined flow
        while the write happens only when something actually changed, so the wider
        permission is minted far less often.
        """
        from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

        if not org_id or type(installation_id) is not int or installation_id < 1:
            raise TrackerProviderError("Tracker target is not an authorized installation.")
        try:
            app_id, key = await resolve_tenant_app_credentials(org_id)
            token, _ = await mint_installation_token_with_expiry(
                app_id,
                key,
                installation_id,
                repositories=[repository],
                permissions={"issues": "write" if write else "read", "metadata": "read"},
            )
        except Exception:
            # Fixed string, `from None`: app-resolution and token responses may carry
            # provider bodies or signed URLs, and this text reaches logs.
            raise TrackerProviderError("Tracker credentials are unavailable.") from None
        return token
