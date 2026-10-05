"""Browse backend — navigate indexed content (browse verb).

Lists indexed repos from the Postgres catalog/S3 and source files from Zoekt.
Provides importable functions for the Context MCP server.

Discovery entry point: ``browse(action="ls", uri="/")`` returns the catalog of
all indexed repos with a **rich capability manifest** per repo. The manifest is
built from ``index_run_stages`` (verified/skipped status + metrics) — the source
of truth for what each repo actually has indexed — NOT the stale
``repositories.*_status`` columns.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .acl import SearchHit, canonicalize_key, is_shared_content_path, _normalize_repo_name, _build_allowed_lookup, _repo_is_allowed

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage → capability mapping: translates index_run_stages.stage values to
# user-facing capability keys used in the manifest.
# ---------------------------------------------------------------------------
_STAGE_TO_CAPABILITY: dict[str, str] = {
    "zoekt_index": "code_search",
    "cgc_structural": "code_search",  # contributes files/symbols to code_search
    "scip_structural": "call_graph",
    "deepwiki": "wiki",
    "sbom_source": "sbom",
    # Both stages produce semantic vectors: `embed_vectors` writes source-code
    # embeddings to S3 Vectors (#2297), `graphrag` writes GraphRAG vectors.
    # `_build_capabilities_index` OR-merges `ready`, so either verified stage
    # flips `vectors.ready` true. `embed_vectors` was previously unmapped, which
    # left `vectors.ready` permanently false even when embeddings existed (#2912).
    "embed_vectors": "vectors",
    "graphrag": "vectors",
}

# Known S3 content root directories — URIs starting with these are routed
# directly to S3 instead of the repo→Zoekt path.
_CONTENT_ROOTS = frozenset({"content", "code-indexes", "sbom", "tenants", "users"})

# Where per-repo SBOM artifacts live — kept in sync with
# ``secure_backend.SBOM_S3_PREFIX``.
_SBOM_REPO_PREFIX = "sbom/repos"

# Action aliases: users pass "list"/"read" but the backend uses "ls"/"read".
_ACTION_ALIASES: dict[str, str] = {
    "list": "ls",
}


async def browse(
    action: str,
    uri: str,
    *,
    db_pool: Any | None = None,
    s3_client: Any | None = None,
    bucket: str = "",
    content_prefix: str = "content",
    depth: int = 1,
    zoekt_url: str = "",
    repo_scope: str | None = None,
    allowed_repos: set[str] | None = None,
) -> list[SearchHit]:
    """Navigate the indexed content filesystem.

    Parameters
    ----------
    action:
        Action to perform: "ls"/"list" (list), "tree" (recursive list),
        "info" (metadata), "read" (fetch object content).
    uri:
        URI path to browse. Root "/" lists repos, "/repo-name" lists content types,
        "/repo-name/path" lists files via Zoekt.
        Content paths like "content/wikis" or "content/wikis/file.md" are routed
        directly to S3.
    db_pool:
        Database connection pool for catalog queries.
    s3_client:
        boto3 S3 client for content listing.
    bucket:
        S3 bucket name.
    content_prefix:
        S3 key prefix for content objects.
    depth:
        How many levels deep to list (default 1).
    zoekt_url:
        Zoekt webserver URL for file-level browsing.
    repo_scope:
        Optional repo name (e.g. "HKUDS/Vibe-Trading"). When set, a non
        content-root URI is treated as a **repo-relative path** inside that
        repo and dispatched to Zoekt — this is how agents browse a repo's
        directory tree (``browse list uri=agent/ project=HKUDS/Vibe-Trading``).
        Content-root URIs (content/, code-indexes/, sbom/) keep their S3
        routing regardless of repo_scope.

    Returns
    -------
    List of SearchHit representing directory entries.
    """
    uri = uri.strip().rstrip("/")

    # Normalize action aliases (e.g. "list" → "ls")
    action = _ACTION_ALIASES.get(action, action)

    if action == "ls":
        return await _list_path(
            uri,
            db_pool=db_pool,
            s3_client=s3_client,
            bucket=bucket,
            content_prefix=content_prefix,
            depth=depth,
            zoekt_url=zoekt_url,
            repo_scope=repo_scope,
        )
    elif action == "tree":
        return await _list_path(
            uri,
            db_pool=db_pool,
            s3_client=s3_client,
            bucket=bucket,
            content_prefix=content_prefix,
            depth=min(depth, 3),
            zoekt_url=zoekt_url,
            repo_scope=repo_scope,
        )
    elif action == "info":
        return await _get_info(
            uri,
            db_pool=db_pool,
            s3_client=s3_client,
            bucket=bucket,
            content_prefix=content_prefix,
        )
    elif action == "read":
        # Repo-scoped read: a repo-relative path (e.g. "agent/backtest/models.py")
        # is not in the S3 content bucket — its content lives in Zoekt.
        if repo_scope and not _is_content_root(uri):
            if allowed_repos is not None and not _repo_is_allowed(repo_scope, _build_allowed_lookup(allowed_repos)):
                return []
            return await _read_zoekt_file(repo_scope, uri, zoekt_url=zoekt_url)
        return await _read_content(
            uri,
            db_pool=db_pool,
            s3_client=s3_client,
            bucket=bucket,
            content_prefix=content_prefix,
            repo_scope=repo_scope,
            allowed_repos=allowed_repos,
        )
    else:
        log.warning("Unknown browse action: %s", action)
        return []


def _zoekt_repo_filter(repo_name: str) -> str:
    """Build a Zoekt ``r:`` regex that matches a repo in any naming form.

    Zoekt shard repository names are domain-qualified
    ("github.com/HKUDS/Vibe-Trading") while the catalog stores bare
    "org/repo" slugs. A strictly-anchored ``^org/repo$`` therefore matches
    nothing against live shards — allow an optional ``<domain>/`` prefix
    while still anchoring the tail so "HKUDS/Vibe-Trading" never matches
    "HKUDS/Vibe-Trading-fork".
    """
    escaped_name = re.escape(repo_name)
    if "/" in repo_name:
        # Full "org/repo": exact match, or exact match after a domain prefix.
        return f"^([^/]+/)?{escaped_name}$"
    # Bare repo name: anchor with a "/" prefix so "skills" matches
    # "github.com/mattpocock/skills" but not "agent-skills".
    return f"/{escaped_name}$"


def _is_content_root(uri: str) -> bool:
    """True if the URI's first path component is a known S3 content root."""
    parts = [p for p in uri.split("/") if p]
    return bool(parts) and parts[0] in _CONTENT_ROOTS


# The complete set of per-repo artifact filename suffixes, i.e. every way the
# ingestion side turns a ``safe_name`` into an object name:
#
#   content/wikis/{safe_name}-wiki.md              ingest-repo.py:1936, lint-wiki.py:194
#   content/code-indexes/{safe_name}-code-index.md ingest-repo.py
#   code-indexes/{safe_name}.json                  ingest-repo.py:460
#
# Enumerated rather than approximated. The earlier version of this matched "the
# safe_name followed by any of -._", which is not a suffix test at all: under it
# the scope "org-a/service" claimed
# ``content/wikis/org-a-service-fork-wiki.md`` — a DIFFERENT repo's wiki —
# because "org-a-service" is a prefix of "org-a-service-fork" and the next
# character is a hyphen. A repo whose name merely extends another's would have
# had its artifacts readable under the shorter repo's scope.
#
# Adding an artifact type means adding it here, and a missing entry fails closed
# (the artifact simply is not attributable to its repo) rather than open.
_ARTIFACT_SUFFIXES: tuple[str, ...] = (
    "-wiki.md",
    "-code-index.md",
    ".json",
)


def _strip_artifact_suffix(leaf: str) -> str:
    """Return ``leaf`` without its artifact suffix, or "" if it has none.

    Longest suffix first so "-code-index.md" is preferred over ".json"-style
    shorter matches and a name is never half-stripped.
    """
    for suffix in sorted(_ARTIFACT_SUFFIXES, key=len, reverse=True):
        if leaf.endswith(suffix) and len(leaf) > len(suffix):
            return leaf[: -len(suffix)]
    return ""


class RepositoryNameIndex(dict[str, str]):
    """Keep exact repository identities alongside unambiguous storage aliases."""

    def __init__(self, names=(), ownership=None):
        super().__init__()
        self.ownership = ownership or {}
        self.repo_names = frozenset(name for name in names if name)
        for name in self.repo_names:
            alias = name.replace("/", "-")
            if alias in self and self[alias] != name:
                self[alias] = ""  # An ambiguous alias must never authorize a read.
            else:
                self[alias] = name


def _safe_name_index(db_pool: Any | None) -> RepositoryNameIndex:
    """Map ``safe_name`` → canonical ``repo_name`` from the catalog.

    Artifacts are stored under ``org-repo`` (``safe_name``), and that transform
    is lossy on its own — "a-b-c" could be "a/b-c" or "a-b/c". It is exactly
    resolvable only when exactly one catalogued repository maps to it. Collisions
    remain denied regardless of row order. Exact identities remain available for
    hierarchical SBOM paths that do not use the lossy transform.

    Returns an empty map when the catalog is unavailable. Callers treat that as
    "cannot attribute" — which ``server._apply_acl`` withholds — rather than as
    "no restrictions".
    """
    if db_pool is None:
        return RepositoryNameIndex()
    try:
        conn = db_pool.getconn()
        try:
            with conn.cursor() as cur:
                # A partial catalogue can hide a colliding owner beyond the limit.
                cur.execute("SELECT repo_name, tenant_id, owner_sub FROM repositories")
                rows = cur.fetchall()
        finally:
            db_pool.putconn(conn)
    except Exception:
        log.warning("Could not build safe_name index from catalog", exc_info=True)
        return RepositoryNameIndex()

    return RepositoryNameIndex(
        (row[0] for row in rows),
        {row[0]: (row[1], row[2]) for row in rows if len(row) >= 3},
    )


def _repo_for_content_key(s3_key: str, safe_names: dict[str, str], content_prefix: str) -> str:
    """Resolve the owning repo of an object-store key, or "" if unattributable.

    Provenance comes from the storage location plus the catalog — never from a
    caller-supplied argument. An unattributable key returns "", which the ACL
    layer withholds unless the key is in the enumerated shared set.
    """
    if not s3_key:
        return ""
    root = content_prefix.strip("/") or "content"
    segments = s3_key.split("/")

    if segments[0] in {"tenants", "users"}:
        if len(segments) < 4:
            return ""
        namespace, owner = segments[:2]
        ownership = getattr(safe_names, "ownership", {})
        names = [name for name, (tenant_id, owner_sub) in ownership.items()
                 if (namespace == "tenants" and tenant_id == owner and not owner_sub)
                 or (namespace == "users" and owner_sub == owner)]
        relative = "/".join(segments[2:])
        if segments[2] not in {root, "sbom", "code-indexes"}:
            relative = f"{root}/{relative}"
        return _repo_for_content_key(relative, RepositoryNameIndex(names), content_prefix)

    # sbom/repos/{org}/{repo}/... — the repo name is present verbatim.
    if s3_key.startswith(f"{_SBOM_REPO_PREFIX}/") and len(segments) >= 4:
        candidate = f"{segments[2]}/{segments[3]}"
        if candidate in getattr(safe_names, "repo_names", safe_names.values()):
            return candidate
        return ""

    # {content_prefix}/{type}/{safe_name}[suffix] or code-indexes/{safe_name}[suffix]
    if segments[0] == root and len(segments) >= 3:
        leaf = segments[2]
    elif segments[0] == "code-indexes" and len(segments) >= 2:
        leaf = segments[1]
    else:
        return ""

    # A bare directory entry (no suffix), e.g. the "org-repo" dir under an
    # sbom-style layout.
    if leaf in safe_names:
        return safe_names[leaf]
    # Otherwise the leaf is "{safe_name}{suffix}". Strip the suffix and require
    # the remainder to be a catalogued safe_name EXACTLY — never a prefix of
    # one. "org-a-service-fork-wiki.md" reduces to "org-a-service-fork", which
    # resolves to the fork and not to "org-a/service".
    stem = _strip_artifact_suffix(leaf)
    if stem and stem in safe_names:
        return safe_names[stem]
    return ""


def _content_key_belongs_to_repo(s3_key: str, repo_name: str, content_prefix: str) -> bool:
    """Check only whether a key has the repository's lexical storage layout.

    This does not prove ownership: two repositories can share the same lossy
    flat alias. Read authorization uses the authoritative catalogue through
    ``_repo_for_content_key`` instead. Kept for layout compatibility tests.
    """
    if not s3_key or not repo_name:
        return False

    safe_name = repo_name.replace("/", "-")
    root = content_prefix.strip("/") or "content"
    segments = s3_key.split("/")

    # sbom/repos/{org}/{repo}/... — repo name appears verbatim as segments.
    if s3_key.startswith(f"{_SBOM_REPO_PREFIX}/{repo_name}/"):
        return True

    # {content_prefix}/{type}/{safe_name}* and code-indexes/{safe_name}*
    if segments[0] == root and len(segments) >= 3:
        leaf = segments[2]
    elif segments[0] == "code-indexes" and len(segments) >= 2:
        leaf = segments[1]
    else:
        return False

    # The artifact file/dir is named after the repo, optionally with one of the
    # enumerated artifact suffixes. Require the stem to equal the safe_name
    # EXACTLY: a prefix test would let the scope "org-a/service" claim
    # "org-a-service-fork-wiki.md", which belongs to a different repo.
    if leaf == safe_name:
        return True
    return _strip_artifact_suffix(leaf) == safe_name


async def _list_path(
    uri: str,
    *,
    db_pool: Any | None,
    s3_client: Any | None,
    bucket: str,
    content_prefix: str,
    depth: int,
    zoekt_url: str = "",
    repo_scope: str | None = None,
) -> list[SearchHit]:
    """List contents at a URI path.

    Three URI schemes are supported:

    1. **Content-path URIs** (start with a known content root like "content/"):
       Route directly to S3 listing. E.g., "content/wikis" lists all wiki files,
       "content/code-indexes" lists code-index JSONs. Takes precedence even when
       a repo_scope is set.

    2. **Repo-scoped URIs** (repo_scope set, non content-root URI):
       The whole URI is a repo-relative path within ``repo_scope``. E.g.
       ``uri="agent/"`` with ``repo_scope="HKUDS/Vibe-Trading"`` lists the
       ``agent/`` directory of that repo via Zoekt. An empty URI lists the
       repo top level.

    3. **Repo-path URIs** (no repo_scope):
       Root "/" lists repos from catalog; "/repo-name" lists top-level via Zoekt;
       "/repo-name/subdir" lists deeper paths via Zoekt.
    """
    parts = [p for p in uri.split("/") if p]

    # --- Content-path routing (highest precedence) ---
    # If the first path component is a known content root (e.g., "content"),
    # route directly to S3 listing rather than treating it as a repo name.
    if parts and parts[0] in _CONTENT_ROOTS:
        s3_prefix = "/".join(parts)
        return await _list_s3_prefix(
            s3_prefix,
            s3_client=s3_client,
            bucket=bucket,
            db_pool=db_pool,
            content_prefix=content_prefix,
        )

    # --- Repo-scoped routing ---
    # A project/repo scope was supplied: treat the ENTIRE URI as a path within
    # that repo (empty URI = repo top level) and list it via Zoekt. This is the
    # repo directory-tree browse contract the eval dataset exercises.
    if repo_scope:
        return await _list_zoekt_files(repo_scope, "/".join(parts), zoekt_url=zoekt_url)

    # Root level: list all indexed repos from catalog (or S3 fallback)
    if not parts:
        return await _list_repos(db_pool, s3_client=s3_client, bucket=bucket)

    # --- Repo-path routing ---
    # Repos are named "org/repo" (matching Zoekt / the catalog), so the first
    # TWO path components form the repo name — same convention as _get_info.
    # e.g. uri="HKUDS/Vibe-Trading" → repo, no sub-path;
    #      uri="HKUDS/Vibe-Trading/agent" → repo + sub-path "agent".
    if len(parts) >= 2:
        repo_name = "/".join(parts[:2])
        sub_path = "/".join(parts[2:])
    else:
        repo_name = parts[0]
        sub_path = ""

    if zoekt_url:
        return await _list_zoekt_files(repo_name, sub_path, zoekt_url=zoekt_url)

    # Fall back to static content-type listing if Zoekt unavailable.
    if not sub_path:
        return _list_repo_content_types(repo_name)

    # Sub-path with no Zoekt: attempt S3 content listing.
    content_type = parts[2] if len(parts) > 2 else ""
    sub_path_s3 = "/".join(parts[3:]) if len(parts) > 3 else ""
    return await _list_s3_content(
        repo_name,
        content_type,
        sub_path_s3,
        s3_client=s3_client,
        bucket=bucket,
        content_prefix=content_prefix,
        db_pool=db_pool,
    )


async def _list_repos(
    db_pool: Any | None, s3_client: Any | None = None, bucket: str = ""
) -> list[SearchHit]:
    """List all indexed repos from the catalog with rich capability manifests.

    Returns each repo with a capability manifest built from ``index_run_stages``
    (the source of truth for what's actually indexed). This lets agents plan
    which verbs to call without blind probing.

    When Postgres is unavailable (rds_enabled=false), discovers repos from the
    code-indexes/ S3 prefix — each file is named {org}-{repo}.json.
    """
    # Try Postgres first (authoritative catalog)
    if db_pool is not None:
        try:
            conn = db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    # Step 1: fetch repos (columns that EXIST in the schema)
                    cur.execute(
                        "SELECT repo_name, git_url, indexed_at"
                        " FROM repositories ORDER BY repo_name LIMIT 200"
                    )
                    repo_rows = cur.fetchall()
                    if not repo_rows:
                        # Table exists but empty — fall through to S3
                        db_pool.putconn(conn)
                        # Let the S3 fallback below handle it
                        return await _list_repos_s3_fallback(s3_client, bucket)

                    # Step 2: fetch capabilities from index_run_stages
                    # Get the LATEST row per (repo, stage) with status in
                    # ('verified', 'skipped') — these represent the final state.
                    cur.execute(
                        """
                        SELECT DISTINCT ON (repo, stage)
                            repo, stage, status, metrics
                        FROM index_run_stages
                        WHERE status IN ('verified', 'skipped')
                        ORDER BY repo, stage, completed_at DESC NULLS LAST
                        """
                    )
                    stage_rows = cur.fetchall()

                    # Build per-repo capability index
                    capabilities_by_repo = _build_capabilities_index(stage_rows)

                    # Step 3: assemble results
                    results: list[SearchHit] = []
                    for row in repo_rows:
                        repo_name = row[0]
                        git_url = row[1] or ""
                        indexed_at = row[2]

                        manifest = capabilities_by_repo.get(repo_name, {})
                        data: dict[str, Any] = {
                            "repo_id": repo_name,
                            "type": "repository",
                            "entry_type": "directory",
                            "git_url": git_url,
                            "indexed_at": str(indexed_at) if indexed_at else None,
                            "capabilities": manifest,
                        }
                        results.append(SearchHit(repo_name=repo_name, data=data))
                    return results
            finally:
                db_pool.putconn(conn)
        except Exception:
            log.warning("Failed to list repos from catalog", exc_info=True)

    # S3 fallback
    return await _list_repos_s3_fallback(s3_client, bucket)


async def _list_repos_s3_fallback(s3_client: Any | None, bucket: str) -> list[SearchHit]:
    """Discover repos from code-indexes/ S3 prefix (fallback when no DB)."""
    if s3_client is not None and bucket:
        try:
            response = s3_client.list_objects_v2(
                Bucket=bucket, Prefix="code-indexes/", Delimiter="/"
            )
            results: list[SearchHit] = []
            for obj in response.get("Contents", []):
                key = obj["Key"]
                # code-indexes/{org}-{repo}.json → extract repo name
                filename = key.split("/")[-1]
                if filename.endswith(".json") and filename != "":
                    repo_name = filename.removesuffix(".json")
                    results.append(
                        SearchHit(
                            repo_name=repo_name,
                            data={
                                "repo_id": repo_name,
                                "type": "repository",
                                "entry_type": "directory",
                                "capabilities": {},
                            },
                        )
                    )
            if results:
                log.debug("Listed %d repos from S3 code-indexes/ fallback", len(results))
                return results
        except Exception:
            log.warning("S3 fallback for repo listing failed", exc_info=True)

    log.debug("No repos found (no db_pool and no S3 fallback)")
    return []


def _build_capabilities_index(
    stage_rows: list[tuple],
) -> dict[str, dict[str, Any]]:
    """Build per-repo capability manifests from index_run_stages rows.

    Each row is (repo, stage, status, metrics). Aggregates into the manifest
    shape: {capability_key: {ready: bool, ...metrics}}.

    Manifest shape per repo::

        {
            "code_search": {"ready": true, "files": 549, "symbols": 5000, "size_bytes": 27661540},
            "call_graph": {"ready": true, "nodes": 3512, "edges": 2923},
            "wiki": {"ready": true, "chars": 14616},
            "sbom": {"ready": true, "dependencies": 415},
            "vectors": {"ready": false}
        }
    """
    caps: dict[str, dict[str, Any]] = {}

    for repo, stage, status, metrics in stage_rows:
        capability_key = _STAGE_TO_CAPABILITY.get(stage)
        if capability_key is None:
            continue

        if repo not in caps:
            caps[repo] = {}

        is_ready = status == "verified"

        # Merge metrics into the capability entry
        # Multiple stages can contribute to the same capability (e.g. zoekt_index
        # and cgc_structural both feed code_search) — merge their metrics.
        existing = caps[repo].get(capability_key, {"ready": False})
        existing["ready"] = existing.get("ready", False) or is_ready

        if metrics and isinstance(metrics, dict):
            for k, v in metrics.items():
                if v is not None:
                    existing[k] = v

        caps[repo][capability_key] = existing

    return caps


def _list_repo_content_types(repo_name: str) -> list[SearchHit]:
    """List available content types for a repo (static structure)."""
    types = [
        ("wikis", "Generated documentation and wiki pages"),
        ("code-indexes", "Structural code analysis (symbols, call graph)"),
        ("files", "Source files"),
    ]
    return [
        SearchHit(
            repo_name=repo_name,
            data={
                "repo_id": repo_name,
                "name": name,
                "description": desc,
                "entry_type": "directory",
            },
        )
        for name, desc in types
    ]


async def _list_zoekt_files(
    repo_name: str,
    path_prefix: str,
    *,
    zoekt_url: str,
) -> list[SearchHit]:
    """List files/directories in a repo at a given path using Zoekt.

    Queries Zoekt for files matching repo + path prefix, then extracts unique
    entries at the next directory level (simulating ls behavior).
    """
    if not zoekt_url:
        return []

    # Build Zoekt query to find files in this repo under the path.
    # Use "f:" filter to match file paths, "r:" to scope to repo.
    repo_filter = _zoekt_repo_filter(repo_name)
    if path_prefix:
        query = f"r:{repo_filter} f:^{path_prefix}/"
    else:
        query = f"r:{repo_filter} f:."

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{zoekt_url}/api/search",
                json={"q": query, "num": 500},
            )
            if resp.status_code != 200:
                log.warning(
                    "Zoekt browse returned HTTP %d for repo %s", resp.status_code, repo_name
                )
                return []
            data = resp.json()
    except Exception:
        log.warning("Zoekt browse failed for %s/%s", repo_name, path_prefix, exc_info=True)
        return []

    # Parse results and extract unique entries at the next level
    result_data = data.get("Result", {})
    file_matches = result_data.get("FileMatches") or result_data.get("Files") or []

    entries: dict[str, str] = {}  # name → entry_type
    for file_match in file_matches:
        file_name = file_match.get("FileName", "")
        if not file_name:
            continue

        # Get relative path from the prefix
        if path_prefix:
            if file_name.startswith(path_prefix + "/"):
                relative = file_name[len(path_prefix) + 1 :]
            elif file_name.startswith(path_prefix):
                relative = file_name[len(path_prefix) :]
                if relative.startswith("/"):
                    relative = relative[1:]
            else:
                continue
        else:
            relative = file_name

        if not relative:
            continue

        # Take the first path component
        components = relative.split("/")
        first = components[0]
        if not first:
            continue

        # Determine type: if there are more components, it's a directory
        if len(components) > 1:
            entries.setdefault(first, "directory")
        else:
            entries[first] = "file"

    # Convert to SearchHit list
    results: list[SearchHit] = []
    for name in sorted(entries.keys()):
        entry_type = entries[name]
        full_path = f"{path_prefix}/{name}" if path_prefix else name
        results.append(
            SearchHit(
                repo_name=repo_name,
                data={
                    "repo_id": repo_name,
                    "name": name,
                    "path": full_path,
                    "entry_type": entry_type,
                },
            )
        )

    return results


async def _read_zoekt_file(
    repo_name: str,
    file_path: str,
    *,
    zoekt_url: str,
) -> list[SearchHit]:
    """Read a repo-relative file's content via Zoekt.

    Used for ``action="read"`` when a repo scope is active — repo source files
    are not stored in the S3 content bucket (only wikis / code-indexes / sbom
    are), so their content is retrieved from Zoekt.

    Queries Zoekt for the exact file (``whole:true`` returns the full file
    content, not just matching lines) and returns a single-element list with the
    decoded content, or an empty list if the file is not found.
    """
    if not zoekt_url:
        return []

    file_path = file_path.strip().lstrip("/")
    if not file_path:
        return []

    repo_filter = _zoekt_repo_filter(repo_name)
    # Anchor the file path exactly so we fetch the one file, not substrings.
    escaped_path = re.escape(file_path)
    query = f"r:{repo_filter} f:^{escaped_path}$"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{zoekt_url}/api/search",
                json={"q": query, "num": 5, "opts": {"Whole": True}},
            )
            if resp.status_code != 200:
                log.warning(
                    "Zoekt read returned HTTP %d for %s/%s",
                    resp.status_code,
                    repo_name,
                    file_path,
                )
                return []
            data = resp.json()
    except Exception:
        log.warning("Zoekt read failed for %s/%s", repo_name, file_path, exc_info=True)
        return []

    result_data = data.get("Result") or data.get("result", {})
    file_matches = result_data.get("FileMatches") or result_data.get("Files") or []

    for file_match in file_matches:
        matched_name = file_match.get("FileName", "")
        if matched_name != file_path:
            continue
        content = _extract_file_content(file_match)
        if content is None:
            continue
        name = file_path.split("/")[-1]
        return [
            SearchHit(
                repo_name=repo_name,
                data={
                    "repo_id": repo_name,
                    "name": name,
                    "path": file_path,
                    "content": content,
                    "size": len(content),
                    "entry_type": "file",
                },
            )
        ]

    log.debug("Zoekt read: no exact match for %s/%s", repo_name, file_path)
    return []


def _extract_file_content(file_match: dict[str, Any]) -> str | None:
    """Extract whole-file text from a Zoekt FileMatch.

    Zoekt's ``whole:true`` search puts the full file in ``Content`` (base64 in
    the Go JSON encoder). Fall back to concatenating ChunkMatches when the whole
    file field is absent.
    """
    from .search_backend import _decode_line

    whole = file_match.get("Content")
    if whole:
        decoded = _decode_line(whole)
        if decoded:
            return decoded

    chunk_matches = file_match.get("ChunkMatches") or []
    if chunk_matches:
        parts = [_decode_line(cm.get("Content", "")) for cm in chunk_matches]
        joined = "\n".join(p for p in parts if p)
        if joined:
            return joined

    return None


async def _list_s3_content(
    repo_name: str,
    content_type: str,
    sub_path: str,
    *,
    s3_client: Any | None,
    bucket: str,
    content_prefix: str,
    db_pool: Any | None = None,
) -> list[SearchHit]:
    """List S3 objects under a content path."""
    if s3_client is None or not bucket:
        log.debug("No s3_client or bucket — returning empty content list")
        return []

    safe_name = repo_name.replace("/", "-")
    if _safe_name_index(db_pool).get(safe_name) != repo_name:
        return []
    prefix = f"{content_prefix}/{content_type}/{safe_name}"
    if sub_path:
        prefix = f"{prefix}/{sub_path}"
    prefix = prefix.rstrip("/") + "/"

    try:
        response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")

        results: list[SearchHit] = []

        # Add "directories" (common prefixes)
        for cp in response.get("CommonPrefixes", []):
            dir_path = cp["Prefix"].rstrip("/")
            dir_name = dir_path.split("/")[-1]
            results.append(
                SearchHit(
                    repo_name=repo_name,
                    data={
                        "repo_id": repo_name,
                        "name": dir_name,
                        "path": dir_path,
                        "entry_type": "directory",
                    },
                )
            )

        # Add files (objects)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            name = key.split("/")[-1]
            if not name:
                continue
            results.append(
                SearchHit(
                    repo_name=repo_name,
                    data={
                        "repo_id": repo_name,
                        "name": name,
                        "path": key,
                        "size": obj.get("Size", 0),
                        "entry_type": "file",
                        "last_modified": obj.get("LastModified", ""),
                    },
                )
            )

        return results
    except Exception:
        log.warning("Failed to list S3 content at %s", prefix, exc_info=True)
        return []


async def _list_s3_prefix(
    prefix: str,
    *,
    s3_client: Any | None,
    bucket: str,
    db_pool: Any | None = None,
    content_prefix: str = "content",
) -> list[SearchHit]:
    """List S3 objects directly under a prefix path.

    Used for content-path URIs (e.g., "content/wikis") that map directly to
    S3 key prefixes without requiring a repo name → safe_name transform.

    A single prefix like ``content/wikis`` holds artifacts belonging to MANY
    repos, so each entry is attributed individually via the catalog and then
    ACL-filtered per entry downstream. Previously every entry was emitted with
    ``repo_name=""``, which the filter read as shared content — so listing a
    content root enumerated every tenant's artifacts (#5658).
    """
    if s3_client is None or not bucket:
        log.debug("No s3_client or bucket — returning empty for prefix %s", prefix)
        return []

    canonical_prefix = canonicalize_key(prefix.lstrip("/"))
    if canonical_prefix is None:
        log.warning("browse ls: refusing non-canonical prefix %r", prefix)
        return []
    prefix = canonical_prefix + "/"

    safe_names = _safe_name_index(db_pool)

    try:
        response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")

        results: list[SearchHit] = []

        # Add "directories" (common prefixes)
        for cp in response.get("CommonPrefixes", []):
            dir_path = cp["Prefix"].rstrip("/")
            dir_name = dir_path.split("/")[-1]
            origin = _repo_for_content_key(dir_path, safe_names, content_prefix)
            data: dict[str, Any] = {
                "name": dir_name,
                "path": dir_path,
                "entry_type": "directory",
            }
            if origin:
                data["repo_id"] = origin
            results.append(SearchHit(repo_name=origin, data=data))

        # Add files (objects)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            name = key.split("/")[-1]
            if not name:
                continue
            origin = _repo_for_content_key(key, safe_names, content_prefix)
            file_data: dict[str, Any] = {
                "name": name,
                "path": key,
                "size": obj.get("Size", 0),
                "entry_type": "file",
                "last_modified": obj.get("LastModified", ""),
            }
            if origin:
                file_data["repo_id"] = origin
            results.append(SearchHit(repo_name=origin, data=file_data))

        return results
    except Exception:
        log.warning("Failed to list S3 prefix %s", prefix, exc_info=True)
        return []


async def _read_content(
    uri: str,
    *,
    s3_client: Any | None,
    bucket: str,
    content_prefix: str = "content",
    repo_scope: str | None = None,
    db_pool: Any | None = None,
    allowed_repos: set[str] | None = None,
) -> list[SearchHit]:
    """Read only content with authoritative catalogue provenance or shared status.

    Repository scope narrows the resolved identity; it never supplies an identity
    for ambiguous bytes. Flat filenames with colliding repository aliases and
    unknown owners are refused before fetching. Hierarchical SBOM keys retain
    their exact owner, and enumerated platform assets remain shared.
    """
    if s3_client is None or not bucket:
        log.debug("No s3_client or bucket — cannot read %s", uri)
        return []

    if not uri:
        log.debug("Empty URI for read action")
        return []

    # Canonicalise and bound the key server-side. canonicalize_key rejects
    # "..", backslashes, NUL and percent-encoding outright.
    #
    # A leading "/" is stripped first because it is the documented URI form for
    # this API ("/content/wikis/x.md") and an S3 key has no root to escape to.
    # The bound that matters is the content-root check below, which is applied
    # to the canonical key — not the shape of the caller's prefix.
    s3_key = canonicalize_key(uri.lstrip("/"))
    if s3_key is None:
        log.warning("browse read: refusing non-canonical key %r", uri)
        return []
    if not _is_content_root(s3_key):
        log.warning(
            "browse read: refusing key outside the content roots %s: %r",
            sorted(_CONTENT_ROOTS),
            s3_key,
        )
        return []

    # Bind the read to the declared scope. If the caller said "this is in repo
    # X" then the key must be one of repo X's artifacts; a mismatch means the
    # scope label would not describe the bytes, so refuse instead of relabelling.
    origin_repo = _repo_for_content_key(s3_key, _safe_name_index(db_pool), content_prefix)
    shared_content = is_shared_content_path(s3_key)
    if repo_scope and not shared_content:
        if not origin_repo or (
            _normalize_repo_name(origin_repo).casefold()
            != _normalize_repo_name(repo_scope).casefold()
        ):
            log.warning(
                "browse read: key %r is not an artifact of declared scope %r",
                s3_key,
                repo_scope,
            )
            return []
    if not origin_repo and not shared_content:
        return []
    if origin_repo and allowed_repos is not None and not _repo_is_allowed(origin_repo, _build_allowed_lookup(allowed_repos)):
        return []

    try:
        response = s3_client.get_object(Bucket=bucket, Key=s3_key)
        body = response["Body"].read()

        # Try to decode as text; if it fails, report it as binary
        try:
            content = body.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            content = body.hex()

        name = s3_key.split("/")[-1]
        data: dict[str, Any] = {
            "name": name,
            "path": s3_key,
            "content": content,
            "size": len(body),
            "entry_type": "file",
        }
        if origin_repo:
            data["repo_id"] = origin_repo
        return [SearchHit(repo_name=origin_repo, data=data)]
    except Exception as exc:
        # Handle NoSuchKey gracefully
        exc_name = type(exc).__name__
        if "NoSuchKey" in exc_name or "NoSuchKey" in str(exc):
            log.debug("S3 object not found: %s/%s", bucket, s3_key)
        else:
            log.warning("Failed to read S3 object %s/%s", bucket, s3_key, exc_info=True)
        return []


async def _get_info(
    uri: str,
    *,
    db_pool: Any | None,
    s3_client: Any | None,
    bucket: str,
    content_prefix: str,
) -> list[SearchHit]:
    """Get metadata for a specific path, including rich capability manifest.

    For root ("/") returns a description of the catalog.
    For a repo name returns the full capability manifest from index_run_stages.
    """
    parts = [p for p in uri.split("/") if p]
    if not parts:
        # The catalog descriptor is platform metadata, not tenant content: it
        # names no repo and reveals nothing about what is indexed. It is stamped
        # with a shared-platform path so the ACL layer authorises it explicitly
        # via SHARED_CONTENT_PREFIXES rather than by the old "unlabelled means
        # public" default (#5658).
        return [
            SearchHit(
                repo_name="",
                data={
                    "type": "root",
                    "path": "content/catalog",
                    "description": (
                        "Agent Context catalog. Use browse(action='ls', uri='/') "
                        "to enumerate all indexed repos and their capabilities."
                    ),
                },
            )
        ]

    # Repo names may contain slashes (e.g. "HKUDS/Vibe-Trading") so try
    # joining the first two parts as org/repo before falling back to parts[0].
    repo_name = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]

    # Repo info from catalog + rich capability manifest
    if len(parts) <= 2 and db_pool is not None:
        try:
            conn = db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    # Fetch repo metadata (real columns only)
                    cur.execute(
                        "SELECT repo_name, git_url, indexed_at"
                        " FROM repositories WHERE repo_name = %s",
                        (repo_name,),
                    )
                    row = cur.fetchone()
                    if row:
                        # Fetch capability stages for this repo
                        cur.execute(
                            """
                            SELECT DISTINCT ON (stage)
                                stage, status, metrics
                            FROM index_run_stages
                            WHERE repo = %s AND status IN ('verified', 'skipped')
                            ORDER BY stage, completed_at DESC NULLS LAST
                            """,
                            (repo_name,),
                        )
                        stage_rows = cur.fetchall()
                        # Build manifest — reuse same helper (wrap in expected tuple shape)
                        stage_tuples = [(repo_name, s[0], s[1], s[2]) for s in stage_rows]
                        caps_index = _build_capabilities_index(stage_tuples)
                        manifest = caps_index.get(repo_name, {})

                        return [
                            SearchHit(
                                repo_name=row[0],
                                data={
                                    "repo_id": row[0],
                                    "type": "repository",
                                    "git_url": row[1] or "",
                                    "indexed_at": str(row[2]) if row[2] else None,
                                    "capabilities": manifest,
                                },
                            )
                        ]
            finally:
                db_pool.putconn(conn)
        except Exception:
            log.warning("Failed to get repo info for %s", repo_name, exc_info=True)

    return []
