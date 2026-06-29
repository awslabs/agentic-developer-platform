"""Repo-grain ACL filter for the Door (MCP query layer).

Enforces who-can-see-which-repo at query time by checking each search hit's
repo against the caller's allowed repos (derived from GitHub permissions stored
in Postgres). Fails closed: unresolved or empty principal -> empty results.

Trust boundary: X-GitHub-Login and X-GitHub-Teams headers are set by the
trusted dispatch layer (webhook Lambda -> SQS -> agent worker). In-cluster
NetworkPolicy prevents external header injection.

See: docs/design-1356-repo-acl-door-filter.md for full design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Header names (canonical lowercase for case-insensitive matching)
HEADER_GITHUB_LOGIN = "x-github-login"
HEADER_GITHUB_TEAMS = "x-github-teams"
HEADER_TENANT_ID = "x-tenant-id"
HEADER_OWNER_SUB = "x-owner-sub"

# Sentinel value in allowed_principals meaning "visible to everyone"
PUBLIC_SENTINEL = "*"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerPrincipal:
    """The caller's identity, extracted from request headers.

    Combines GitHub identity (login/teams) with tenant isolation headers
    (tenant_id/owner_sub). The principal is "resolved" if it has at least
    a login or team membership — tenant headers alone are not sufficient.
    """

    github_login: str = ""
    github_teams: list[str] = field(default_factory=list)
    tenant_id: str = ""
    owner_sub: str = ""

    @property
    def is_resolved(self) -> bool:
        """A principal is resolved if it has at least a login or team membership."""
        return bool(self.github_login) or bool(self.github_teams)


@dataclass
class SearchHit:
    """A single search result with provenance.

    The filter inspects ``repo_name`` to decide whether the caller can see it.
    All other fields are opaque to the filter.
    """

    repo_name: str
    # Everything else is pass-through
    data: dict[str, Any] = field(default_factory=dict)


class ACLStore(Protocol):
    """Protocol for the backing store that provides repo -> principals mapping.

    Implementations may be backed by Postgres, an in-memory dict (for tests),
    or a cache layer.
    """

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        """Return the set of repo_names this principal is allowed to see.

        When tenant scoping is disabled, includes repos where allowed_principals contains:
        - The PUBLIC_SENTINEL ("*"), OR
        - principal.github_login, OR
        - Any value in principal.github_teams

        When tenant scoping is enabled, additionally enforces:
        - Shared repos (tenant_id IS NULL): principals match required
        - Per-tenant repos (tenant_id matches caller): principals match required
        - Per-individual repos (owner_sub matches caller): visible regardless of principals
        - Cross-tenant repos (tenant_id != caller's): excluded

        If the store is unreachable, implementations MUST raise (not return
        all repos). The caller handles the exception as fail-closed.
        """
        ...


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------


def extract_caller_principal(headers: dict[str, str]) -> CallerPrincipal | None:
    """Extract the caller's identity from request headers.

    Reads four headers:
    - X-GitHub-Login: GitHub username
    - X-GitHub-Teams: comma-separated team slugs
    - X-Tenant-Id: organization/tenant identifier
    - X-Owner-Sub: individual user identifier (Cognito sub or similar)

    Returns None if neither GitHub header is present (fail-closed at filter time).
    Tenant/owner headers are optional enrichment — they narrow scope but cannot
    establish identity alone.
    """
    normalized = {k.lower(): v for k, v in headers.items()}

    login = normalized.get(HEADER_GITHUB_LOGIN, "").strip().lower()
    teams_raw = normalized.get(HEADER_GITHUB_TEAMS, "").strip()

    # Parse teams: comma-separated, lowercased, stripped
    teams: list[str] = []
    if teams_raw:
        teams = [t.strip().lower() for t in teams_raw.split(",") if t.strip()]

    if not login and not teams:
        return None

    # Tenant isolation headers (optional enrichment)
    tenant_id = normalized.get(HEADER_TENANT_ID, "").strip()
    owner_sub = normalized.get(HEADER_OWNER_SUB, "").strip().lower()

    return CallerPrincipal(
        github_login=login,
        github_teams=teams,
        tenant_id=tenant_id,
        owner_sub=owner_sub,
    )


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def filter_results(
    results: list[SearchHit],
    caller: CallerPrincipal | None,
    acl_store: ACLStore,
) -> list[SearchHit]:
    """Post-query filter. Drops results from repos the caller cannot access.

    INVARIANT: if caller is None or unresolved, returns [] (fail-closed).
    INVARIANT: if acl_store raises, returns [] (fail-closed).

    Parameters
    ----------
    results:
        Raw search hits from the engine (Zoekt, S3 Vectors, etc.).
    caller:
        The resolved caller principal, or None if unresolvable.
    acl_store:
        The backing store for repo -> principals lookup.

    Returns
    -------
    Filtered list containing only hits from repos the caller is allowed to see.
    """
    # FAIL-CLOSED: no principal -> no results
    if caller is None:
        log.debug("filter_results: caller is None, returning empty (fail-closed)")
        return []

    if not caller.is_resolved:
        log.debug("filter_results: caller has no login or teams, returning empty (fail-closed)")
        return []

    # Resolve allowed repos — fail-closed on error
    try:
        allowed_repos = acl_store.get_allowed_repos(caller)
    except Exception:
        log.warning(
            "filter_results: ACL store raised an exception, returning empty (fail-closed)",
            exc_info=True,
        )
        return []

    # Filter: only pass hits whose repo is in the allowed set
    filtered = [hit for hit in results if hit.repo_name in allowed_repos]

    if len(filtered) < len(results):
        log.debug(
            "filter_results: dropped %d/%d hits for principal %s",
            len(results) - len(filtered),
            len(results),
            caller.github_login or "<teams-only>",
        )

    return filtered


# ---------------------------------------------------------------------------
# Postgres-backed ACL store
# ---------------------------------------------------------------------------


class PostgresACLStore:
    """ACL store backed by the repositories table in Postgres.

    Uses the `allowed_principals` TEXT[] column with a GIN index.
    When tenant scoping is enabled, additionally filters by tenant_id/owner_sub.

    Callers are expected to pass a connection/pool; this class does not
    manage connection lifecycle.
    """

    def __init__(self, db_pool: Any, *, tenant_scope_enabled: bool = False):
        """Initialize with a database connection pool and optional tenant scoping.

        Parameters
        ----------
        db_pool:
            Database connection pool (psycopg2 or similar).
        tenant_scope_enabled:
            When True, enforce tenant/individual scoping in addition to
            principal matching. When False, use legacy principal-only logic.
        """
        self._pool = db_pool
        self._tenant_scope_enabled = tenant_scope_enabled

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        """Query Postgres for repos this principal can access.

        Raises on connection failure (caller handles as fail-closed).
        """
        if self._tenant_scope_enabled:
            return self._get_allowed_repos_scoped(principal)
        return self._get_allowed_repos_legacy(principal)

    def _get_allowed_repos_legacy(self, principal: CallerPrincipal) -> set[str]:
        """Legacy query: principal matching only (no tenant isolation)."""
        login = principal.github_login or ""
        teams = principal.github_teams or []

        # allowed_principals is jsonb (array of strings).
        # Use ? (element exists) and ?| (any element exists) operators.
        query = """
            SELECT repo_name FROM repositories
            WHERE allowed_principals ? %s
               OR allowed_principals ? %s
               OR allowed_principals ?| %s
        """
        params = [PUBLIC_SENTINEL, login, teams]

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
                return {row[0] for row in rows}
        finally:
            self._pool.putconn(conn)

    def _get_allowed_repos_scoped(self, principal: CallerPrincipal) -> set[str]:
        """Tenant-scoped query: visibility rule per design §7.2–§7.4.

        Visibility:
        1. Shared repos (tenant_id IS NULL) — require principals match
        2. Per-tenant repos (tenant_id == caller's) — require principals match
        3. Per-individual repos (owner_sub == caller's) — visible unconditionally
        4. Cross-tenant repos — excluded (fail-closed)
        """
        login = principal.github_login or ""
        teams = principal.github_teams or []
        tenant_id = principal.tenant_id or ""
        owner_sub = principal.owner_sub or ""

        # The query combines three visibility paths via UNION to keep logic clear.
        # Path 1+2: shared + same-tenant repos where principals match
        # Path 3: individual repos where owner_sub matches (no principal check)
        # allowed_principals is jsonb (array of strings).
        # Use ? (element exists) and ?| (any element exists) operators.
        query = """
            SELECT repo_name FROM repositories
            WHERE (
                (tenant_id IS NULL OR tenant_id = %s)
                AND (
                    allowed_principals ? %s
                    OR allowed_principals ? %s
                    OR allowed_principals ?| %s
                )
            )
            OR (
                %s != '' AND owner_sub = %s
            )
        """
        params = [tenant_id, PUBLIC_SENTINEL, login, teams, owner_sub, owner_sub]

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
                return {row[0] for row in rows}
        finally:
            self._pool.putconn(conn)


# ---------------------------------------------------------------------------
# ACL derivation (ingest-time)
# ---------------------------------------------------------------------------


def derive_acl_from_github(
    repo_full_name: str,
    github_token: str,
    *,
    request_timeout: int = 30,
) -> list[str]:
    """Derive the allowed_principals list for a repo from the GitHub API.

    For public repos: returns ["*"].
    For private/internal repos: returns user logins + team slugs with push+ access.

    Parameters
    ----------
    repo_full_name:
        Full repo name, e.g. "aws-e/adp".
    github_token:
        GitHub App installation token with repo + read:org scope.
    request_timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    List of principal strings. Empty list means no one can see the repo
    (safe default on API failure for new repos).

    Raises
    ------
    Does NOT raise on API failure — returns [] for new repos (fail-closed)
    or preserves caller's previous value via the upsert logic.
    """
    import requests as http_requests

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_url = "https://api.github.com"
    owner = repo_full_name.split("/")[0] if "/" in repo_full_name else ""

    # Step 1: Check repo visibility
    try:
        resp = http_requests.get(
            f"{base_url}/repos/{repo_full_name}",
            headers=headers,
            timeout=request_timeout,
        )
        if resp.status_code != 200:
            log.warning(
                "derive_acl: GET /repos/%s returned %d — cannot derive ACL",
                repo_full_name,
                resp.status_code,
            )
            return []

        repo_data = resp.json()
        visibility = repo_data.get("visibility", "private")

        if visibility == "public":
            return [PUBLIC_SENTINEL]

    except Exception as e:
        log.warning("derive_acl: failed to check repo visibility for %s: %s", repo_full_name, e)
        return []

    # Step 2: Enumerate collaborators (direct, with push+ access)
    principals: list[str] = []
    try:
        page = 1
        while True:
            resp = http_requests.get(
                f"{base_url}/repos/{repo_full_name}/collaborators",
                headers=headers,
                params={"affiliation": "direct", "per_page": 100, "page": page},
                timeout=request_timeout,
            )
            if resp.status_code == 403:
                log.warning(
                    "derive_acl: 403 on collaborators for %s (missing admin:read scope?)",
                    repo_full_name,
                )
                break
            if resp.status_code != 200:
                log.warning(
                    "derive_acl: collaborators returned %d for %s",
                    resp.status_code,
                    repo_full_name,
                )
                break

            collabs = resp.json()
            if not collabs:
                break

            for c in collabs:
                permissions = c.get("permissions", {})
                if permissions.get("push") or permissions.get("admin"):
                    login = c.get("login", "").lower()
                    if login:
                        principals.append(login)

            # Check for next page
            if len(collabs) < 100:
                break
            page += 1

    except Exception as e:
        log.warning("derive_acl: collaborators fetch failed for %s: %s", repo_full_name, e)

    # Step 3: Enumerate teams (with push/maintain/admin access)
    try:
        page = 1
        while True:
            resp = http_requests.get(
                f"{base_url}/repos/{repo_full_name}/teams",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=request_timeout,
            )
            if resp.status_code == 403:
                log.warning(
                    "derive_acl: 403 on teams for %s (missing read:org scope?)",
                    repo_full_name,
                )
                break
            if resp.status_code != 200:
                log.warning(
                    "derive_acl: teams returned %d for %s",
                    resp.status_code,
                    repo_full_name,
                )
                break

            teams = resp.json()
            if not teams:
                break

            for t in teams:
                perm = t.get("permission", "")
                if perm in ("push", "maintain", "admin"):
                    slug = t.get("slug", "")
                    if slug and owner:
                        principals.append(f"{owner}/{slug}".lower())

            if len(teams) < 100:
                break
            page += 1

    except Exception as e:
        log.warning("derive_acl: teams fetch failed for %s: %s", repo_full_name, e)

    return principals
