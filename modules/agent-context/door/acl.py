"""Repo-grain ACL filter for the Door (MCP query layer).

Enforces who-can-see-which-repo at query time by checking each search hit's
repo against the caller's allowed repos (derived from GitHub permissions stored
in Postgres). Fails closed: unresolved or empty principal -> empty results.

Trust boundary: X-GitHub-Login and X-GitHub-Teams headers are set by the
trusted dispatch layer (webhook Lambda -> SQS -> agent worker). Callers are
authenticated by the shared secret enforced in ``door/auth.py``, and
``manifests/networkpolicy.yaml`` restricts which namespaces can reach the
service at all.

Note what those two controls do and do not buy (issue #4073, finding #8). They
establish that the caller is a legitimate in-cluster workload; they do NOT make
the identity headers unforgeable. Anything holding the shared secret can still
claim any login or team, because the secret is shared across all Door callers.
Cross-tenant isolation therefore rests on this module's filtering, not on the
headers being trustworthy — which is why ``filter_results`` fails closed.

Earlier versions of this docstring claimed an in-cluster NetworkPolicy
prevented external header injection. No NetworkPolicy existed anywhere in this
module when that was written, and nothing authenticated the caller, so the
boundary described here was asserted and never enforced.

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

# Object-store prefixes holding genuinely shared, non-tenant platform assets.
#
# This is an enumerated allow-list, and that shape is the point (#5658). The
# previous rule was "a hit with no repo label is shared content", which made
# every unattributable result public — including personal-context memory and
# any repo-scoped object whose provenance was simply lost on the way through a
# backend. Anything not positively matched here is denied.
#
# Entries are matched as whole path segments against the canonicalised key, so
# "content/catalog" matches "content/catalog/repos.json" but never
# "content/catalog-private/..." or "content/../personal/...".
SHARED_CONTENT_PREFIXES: tuple[str, ...] = (
    "content/catalog",
    "content/capabilities",
)


def is_shared_content_path(key: str) -> bool:
    """True if an object-store key is in the enumerated shared-platform set.

    Fails closed: an empty, absolute, traversing or non-canonical key is never
    shared content.
    """
    if not key:
        return False
    canonical = canonicalize_key(key)
    if canonical is None:
        return False
    for prefix in SHARED_CONTENT_PREFIXES:
        if canonical == prefix or canonical.startswith(prefix + "/"):
            return True
    return False


def canonicalize_key(key: str) -> str | None:
    """Canonicalise an object-store key to a relative, traversal-free form.

    Returns None when the key cannot be represented safely — absolute paths,
    any ".." segment, backslashes, NUL bytes or percent-encoding that could
    decode into a separator after this check. Rejecting rather than rewriting
    is deliberate: a sanitiser that silently repairs a hostile path hides the
    attempt, and the caller has no legitimate reason to send one.
    """
    if not key or "\x00" in key:
        return None
    if key.startswith("/") or "\\" in key:
        return None
    # Percent-encoding is not meaningful in an S3 key supplied through our own
    # API, but it IS a way to smuggle "%2e%2e%2f" past a literal ".." check.
    if "%" in key:
        return None
    segments = [seg for seg in key.split("/") if seg and seg != "."]
    if any(seg == ".." for seg in segments):
        return None
    if not segments:
        return None
    return "/".join(segments)


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
    run_bound: bool = False

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
        run_bound=normalized.get("x-adp-run-service") == "true",
    )


# ---------------------------------------------------------------------------
# Repo-name normalization helpers
# ---------------------------------------------------------------------------

# Common domain prefixes that Zoekt prepends to repo names.
_DOMAIN_PREFIXES = ("github.com/", "gitlab.com/", "bitbucket.org/")


def _normalize_repo_name(name: str) -> str:
    """Normalize a repo name by stripping domain prefixes.

    "github.com/HKUDS/Vibe-Trading" → "HKUDS/Vibe-Trading"
    "HKUDS/Vibe-Trading" → "HKUDS/Vibe-Trading"
    "Vibe-Trading" → "Vibe-Trading"
    """
    for prefix in _DOMAIN_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _build_allowed_lookup(allowed_repos: set[str]) -> set[str]:
    """Build a lookup set of fully-qualified allowed repo names.

    Only the domain prefix is stripped, and case is folded, so that the same
    repository written "github.com/HKUDS/Vibe-Trading" and "HKUDS/Vibe-Trading"
    compares equal. Both sides of the comparison are normalised identically by
    ``_repo_is_allowed``.

    Short names are deliberately NOT added (#5658). A previous version also
    inserted the bare "Vibe-Trading", which meant a caller permitted on
    "HKUDS/Vibe-Trading" matched any other tenant's "OtherOrg/Vibe-Trading" —
    a cross-tenant read through name collision alone.
    """
    return {_normalize_repo_name(repo).casefold() for repo in allowed_repos if repo}


def _repo_is_allowed(repo_name: str, allowed_lookup: set[str]) -> bool:
    """Check if a fully-qualified repo name is in the allowed set.

    Fail-closed on an unqualified name: a bare "Vibe-Trading" carries no owner,
    so it cannot be attributed to a tenant and must not match.
    """
    if not repo_name:
        return False
    normalized = _normalize_repo_name(repo_name).casefold()
    if "/" not in normalized:
        return False
    return normalized in allowed_lookup


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

    # Filter: only pass hits whose fully-qualified repo is in the allowed set.
    # Both sides are normalised the same way (domain prefix stripped, case
    # folded) so "github.com/HKUDS/Vibe-Trading" from Zoekt matches
    # "HKUDS/Vibe-Trading" from the catalog. Short names no longer match at all
    # — see _build_allowed_lookup.
    allowed_normalized = _build_allowed_lookup(allowed_repos)
    filtered: list[SearchHit] = []
    denied: list[str] = []
    for hit in results:
        if _repo_is_allowed(hit.repo_name, allowed_normalized):
            filtered.append(hit)
        else:
            denied.append(hit.repo_name or "<no-provenance>")

    if denied:
        # Log the compared values. A denial is either an attack or a
        # normalisation mismatch on a tenant's own repo, and the two are
        # indistinguishable from an empty result alone.
        record_acl_denial(
            caller=caller,
            requested=sorted(set(denied)),
            reason="repo_not_in_allowed_set",
            allowed_sample=sorted(allowed_normalized)[:10],
        )

    return filtered


def record_acl_denial(
    *,
    caller: CallerPrincipal | None,
    requested: list[str],
    reason: str,
    allowed_sample: list[str] | None = None,
) -> None:
    """Log and count an ACL denial. Fail-open on the telemetry itself.

    Emits the caller, the scope they asked for and why it was refused, so that
    a misconfigured repo-name form is diagnosable and probing is visible.
    Never raises — a metrics failure must not change an authorisation outcome.
    """
    log.warning(
        "acl_denied: caller=%s tenant=%s owner_sub=%s requested=%s reason=%s allowed_sample=%s",
        (caller.github_login if caller else "") or "<none>",
        (caller.tenant_id if caller else "") or "<none>",
        (caller.owner_sub if caller else "") or "<none>",
        requested,
        reason,
        allowed_sample if allowed_sample is not None else "<not-computed>",
    )
    try:
        from .metrics import record_denial

        record_denial(
            tenant_id=(caller.tenant_id if caller else "") or "",
            reason=reason,
            count=max(len(requested), 1),
        )
    except Exception:
        pass  # fail-open: telemetry never blocks an authorisation decision


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

    def check_health(self) -> None:
        """Verify access to the ACL schema without retrieving repository rows."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '1000ms'")
                cur.execute(
                    "SELECT repo_name, allowed_principals, tenant_id, owner_sub, acl_public_verified "
                    "FROM repositories LIMIT 0"
                )
        finally:
            self._pool.putconn(conn)

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        """Query Postgres for repos this principal can access.

        Raises on connection failure (caller handles as fail-closed).
        """
        if principal.run_bound and not principal.tenant_id:
            return set()
        if self._tenant_scope_enabled or principal.run_bound:
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
            WHERE (allowed_principals ? %s AND acl_public_verified IS TRUE)
               OR (allowed_principals - '*') ? %s
               OR (allowed_principals - '*') ?| %s
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
        1. Shared repos (tenant_id and owner_sub NULL) — require public sentinel
        2. Per-tenant repos (tenant_id == caller's) — require principals match
        3. Per-individual repos (owner_sub == caller's) — visible unconditionally
        4. Cross-tenant repos — excluded (fail-closed)
        """
        login = principal.github_login or ""
        teams = principal.github_teams or []
        tenant_id = principal.tenant_id or ""
        owner_sub = principal.owner_sub or ""

        # An old wildcard is not proof of public visibility. The persisted marker
        # is set only by a trusted producer/backfill after source re-observation.
        # Remove the wildcard from ordinary principal comparisons as well.
        # Unknown legacy ownership is not shared content. Only positively public
        # rows can use the unowned branch. Personal rows never inherit the
        # tenant-wide principal branch; their owner is the authority.
        query = """
            SELECT repo_name FROM repositories
            WHERE (
                tenant_id = %s AND owner_sub IS NULL
                AND ((allowed_principals ? %s AND acl_public_verified IS TRUE)
                     OR (allowed_principals - '*') ? %s
                     OR (allowed_principals - '*') ?| %s)
            ) OR (
                tenant_id IS NULL AND owner_sub IS NULL
                AND allowed_principals ? '*' AND acl_public_verified IS TRUE
            ) OR (
                %s != '' AND owner_sub = %s
            )
        """
        params = [tenant_id, PUBLIC_SENTINEL, login, teams, owner_sub, owner_sub]
        if principal.run_bound:
            # A delegated run is confined to its originating tenant even when
            # its owner has personal material under another tenant.
            query = (
                "SELECT repo_name FROM repositories WHERE repo_name IN ("
                + query
                + ") AND (tenant_id IS NULL OR tenant_id = %s)"
            )
            params.append(tenant_id)

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
