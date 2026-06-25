"""Shared fixtures for cross-tenant isolation integration tests.

Provides:
- An ASGI test client hitting the real FastAPI app (door/server.py)
- Tenant-aware fake backends (Zoekt, ACL store, experience tool)
- Two tenant identities (A and B) with pre-seeded content
- Helpers for constructing identity headers per tenant

The key design: patch the server's module-level `state` object with
tenant-aware fakes so the full request pipeline is exercised:
  HTTP headers -> identity extraction -> verb dispatch -> ACL filter -> result
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from door.acl import PUBLIC_SENTINEL, CallerPrincipal, SearchHit


# ---------------------------------------------------------------------------
# Constants: Two tenants with distinct identities
# ---------------------------------------------------------------------------

TENANT_A_ID = "org-alpha"
TENANT_B_ID = "org-beta"

# Tenant A users
USER_A_LOGIN = "alice"
USER_A_TEAMS = ["org-alpha/eng"]
USER_A_OWNER_SUB = str(uuid.uuid4())

# Tenant B users
USER_B_LOGIN = "bob"
USER_B_TEAMS = ["org-beta/eng"]
USER_B_OWNER_SUB = str(uuid.uuid4())


def headers_tenant_a() -> dict[str, str]:
    """Build identity headers for a Tenant A user."""
    return {
        "X-GitHub-Login": USER_A_LOGIN,
        "X-GitHub-Teams": ",".join(USER_A_TEAMS),
        "X-Tenant-Id": TENANT_A_ID,
        "X-Owner-Sub": USER_A_OWNER_SUB,
    }


def headers_tenant_b() -> dict[str, str]:
    """Build identity headers for a Tenant B user."""
    return {
        "X-GitHub-Login": USER_B_LOGIN,
        "X-GitHub-Teams": ",".join(USER_B_TEAMS),
        "X-Tenant-Id": TENANT_B_ID,
        "X-Owner-Sub": USER_B_OWNER_SUB,
    }


# ---------------------------------------------------------------------------
# Tenant-aware fake ACL store
# ---------------------------------------------------------------------------


class TenantIsolatedACLStore:
    """In-memory ACL store implementing full tenant isolation logic.

    Repos are seeded per-tenant. Cross-tenant access is denied.
    Implements the same visibility rules as PostgresACLStore._get_allowed_repos_scoped.
    """

    def __init__(self, repos: list[dict[str, Any]] | None = None) -> None:
        self._repos: list[dict[str, Any]] = repos or []

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        """Return repos this principal can access (tenant-scoped)."""
        allowed: set[str] = set()
        for repo in self._repos:
            repo_tenant = repo.get("tenant_id")
            repo_owner = repo.get("owner_sub")
            repo_principals = repo.get("principals", [])

            # Path 3: Per-individual (owner_sub match, unconditional)
            if repo_owner and principal.owner_sub and repo_owner == principal.owner_sub:
                allowed.add(repo["repo_name"])
                continue

            # Path 1: Shared (tenant_id=NULL) — principals must match
            if repo_tenant is None:
                if self._principals_match(repo_principals, principal):
                    allowed.add(repo["repo_name"])
                continue

            # Path 2: Per-tenant — same tenant + principals match
            if principal.tenant_id and repo_tenant == principal.tenant_id:
                if self._principals_match(repo_principals, principal):
                    allowed.add(repo["repo_name"])

            # Else: cross-tenant -> excluded (implicit deny)

        return allowed

    @staticmethod
    def _principals_match(repo_principals: list[str], principal: CallerPrincipal) -> bool:
        if PUBLIC_SENTINEL in repo_principals:
            return True
        if principal.github_login and principal.github_login in repo_principals:
            return True
        if any(team in repo_principals for team in principal.github_teams):
            return True
        return False


# Seeded repos: each tenant owns private repos invisible to the other
SEEDED_REPOS = [
    # Tenant A repos
    {
        "repo_name": "org-alpha/service-core",
        "principals": [USER_A_LOGIN, *USER_A_TEAMS],
        "tenant_id": TENANT_A_ID,
        "owner_sub": None,
    },
    {
        "repo_name": "org-alpha/internal-lib",
        "principals": USER_A_TEAMS,
        "tenant_id": TENANT_A_ID,
        "owner_sub": None,
    },
    # Tenant B repos
    {
        "repo_name": "org-beta/platform",
        "principals": [USER_B_LOGIN, *USER_B_TEAMS],
        "tenant_id": TENANT_B_ID,
        "owner_sub": None,
    },
    {
        "repo_name": "org-beta/data-pipeline",
        "principals": USER_B_TEAMS,
        "tenant_id": TENANT_B_ID,
        "owner_sub": None,
    },
    # Shared (public) repo — visible to both tenants
    {
        "repo_name": "community/open-lib",
        "principals": [PUBLIC_SENTINEL],
        "tenant_id": None,
        "owner_sub": None,
    },
]


# ---------------------------------------------------------------------------
# Fake Zoekt search backend (returns tenant-scoped hits)
# ---------------------------------------------------------------------------


class FakeZoektIndex:
    """In-memory code search index seeded with per-tenant content.

    Returns SearchHit objects (same as production Zoekt backend).
    Content is tagged with repo_name so ACL filter can verify tenant isolation.
    """

    def __init__(self) -> None:
        self._documents: list[dict[str, Any]] = []

    def seed(self, repo_name: str, file_path: str, content: str) -> None:
        """Add a document to the index."""
        self._documents.append({"repo_name": repo_name, "file": file_path, "content": content})

    async def search(
        self,
        query: str,
        *,
        repo_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[SearchHit]:
        """Search documents (case-insensitive substring match)."""
        query_lower = query.lower()
        results: list[SearchHit] = []
        for doc in self._documents:
            if repo_ids and doc["repo_name"] not in repo_ids:
                continue
            if query_lower in doc["content"].lower() or query_lower in doc["file"].lower():
                results.append(
                    SearchHit(
                        repo_name=doc["repo_name"],
                        data={
                            "repo_id": doc["repo_name"],
                            "file": doc["file"],
                            "content": doc["content"],
                            "line": 1,
                        },
                    )
                )
            if len(results) >= limit:
                break
        return results


# ---------------------------------------------------------------------------
# Fake experience tool (for remember/experience verbs)
# ---------------------------------------------------------------------------


class FakeExperienceTool:
    """In-memory experience tool with per-user isolation.

    Stores entries keyed by (owner_sub, tenant_id). Cross-tenant/cross-user
    queries return empty.
    """

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []

    def handle(self, arguments: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        """Process an experience tool call."""
        from personal_context.identity import require_identity

        identity = require_identity(headers)
        action = arguments.get("action", "")

        if action == "save":
            entry_id = str(uuid.uuid4())[:8]
            entry = {
                "id": entry_id,
                "owner_sub": identity.owner_sub,
                "tenant_id": identity.tenant_id,
                "persona": arguments.get("persona", "developer"),
                "content": arguments.get("content", ""),
                "visibility": arguments.get("visibility", "private"),
            }
            self._entries.append(entry)
            return {"status": "saved", "id": entry_id, **entry}

        elif action == "recall":
            query = arguments.get("query", "").lower()
            visible = self._get_visible_entries(identity)
            matched = [e for e in visible if query in e.get("content", "").lower()]
            return {
                "status": "ok",
                "total": len(matched),
                "results": matched,
            }

        elif action == "list_syntheses":
            visible = self._get_visible_entries(identity)
            syntheses = [e for e in visible if e.get("type") == "synthesis"]
            return {"status": "ok", "total": len(syntheses), "syntheses": syntheses}

        return {"error": f"Unknown action: {action}"}

    def _get_visible_entries(self, identity) -> list[dict[str, Any]]:
        """Return entries visible to this identity (owner isolation + shared)."""
        visible = []
        for entry in self._entries:
            # Private: only owner can see
            if entry.get("visibility", "private") == "private":
                if entry["owner_sub"] == identity.owner_sub:
                    visible.append(entry)
            # Shared: same tenant can see
            elif entry.get("visibility") == "shared":
                if entry["tenant_id"] == identity.tenant_id:
                    visible.append(entry)
        return visible


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_zoekt() -> FakeZoektIndex:
    """Pre-seeded Zoekt index with content from both tenants."""
    index = FakeZoektIndex()

    # Tenant A content — contains "ISOLATION_MARKER_ALPHA"
    index.seed(
        "org-alpha/service-core",
        "src/main.py",
        "def handle_request(): ISOLATION_MARKER_ALPHA pass",
    )
    index.seed(
        "org-alpha/service-core",
        "src/utils.py",
        "def helper(): SHARED_SEARCH_TERM utility code for alpha",
    )
    index.seed(
        "org-alpha/internal-lib",
        "lib/crypto.py",
        "def encrypt(): ISOLATION_MARKER_ALPHA secret encryption logic",
    )

    # Tenant B content — contains "ISOLATION_MARKER_BETA"
    index.seed(
        "org-beta/platform",
        "src/app.py",
        "def main(): ISOLATION_MARKER_BETA platform entry point",
    )
    index.seed(
        "org-beta/platform",
        "src/config.py",
        "def load_config(): SHARED_SEARCH_TERM configuration loader for beta",
    )
    index.seed(
        "org-beta/data-pipeline",
        "pipeline/etl.py",
        "def extract(): ISOLATION_MARKER_BETA ETL pipeline code",
    )

    # Shared repo content — visible to both
    index.seed(
        "community/open-lib",
        "src/public.py",
        "def open_function(): SHARED_SEARCH_TERM public library code",
    )

    return index


@pytest.fixture
def fake_acl_store() -> TenantIsolatedACLStore:
    """ACL store seeded with tenant-scoped repos."""
    return TenantIsolatedACLStore(SEEDED_REPOS)


@pytest.fixture
def fake_experience() -> FakeExperienceTool:
    """Fresh in-memory experience tool."""
    return FakeExperienceTool()


@pytest_asyncio.fixture
async def client(
    fake_zoekt: FakeZoektIndex,
    fake_acl_store: TenantIsolatedACLStore,
    fake_experience: FakeExperienceTool,
) -> AsyncClient:
    """ASGI test client with patched server state for isolation testing.

    Patches the module-level `state` object in door/server.py so that
    the full HTTP request pipeline is exercised (headers -> identity -> ACL).
    """
    from door import server

    # Patch state with our fakes
    with (
        patch.object(server.state, "zoekt", fake_zoekt),
        patch.object(server.state, "acl_store", fake_acl_store),
        patch.object(server.state, "experience_tool", fake_experience),
        patch.object(server.state, "s3_client", None),
        patch.object(server.state, "db_pool", None),
        patch.object(server.state, "neptune_driver", None),
        patch.object(server.state, "semantic_code_store", None),
        patch.object(server.state, "semantic_http_client", None),
    ):
        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
