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

from .acl import SearchHit

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
_CONTENT_ROOTS = frozenset({"content", "code-indexes", "sbom"})

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
            return await _read_zoekt_file(repo_scope, uri, zoekt_url=zoekt_url)
        return await _read_content(
            uri,
            s3_client=s3_client,
            bucket=bucket,
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
                json={"q": query, "num": 5, "Whole": True},
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
) -> list[SearchHit]:
    """List S3 objects under a content path."""
    if s3_client is None or not bucket:
        log.debug("No s3_client or bucket — returning empty content list")
        return []

    safe_name = repo_name.replace("/", "-")
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
) -> list[SearchHit]:
    """List S3 objects directly under a prefix path.

    Used for content-path URIs (e.g., "content/wikis") that map directly to
    S3 key prefixes without requiring a repo name → safe_name transform.
    """
    if s3_client is None or not bucket:
        log.debug("No s3_client or bucket — returning empty for prefix %s", prefix)
        return []

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
                    repo_name="",
                    data={
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
                    repo_name="",
                    data={
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
        log.warning("Failed to list S3 prefix %s", prefix, exc_info=True)
        return []


async def _read_content(
    uri: str,
    *,
    s3_client: Any | None,
    bucket: str,
) -> list[SearchHit]:
    """Read the content of an S3 object by its key path.

    Used for action="read" — fetches the actual content of a file stored in S3
    (e.g., a wiki markdown file at "content/wikis/HKUDS-Vibe-Trading-wiki.md").

    Returns a single-element list with the content in the data dict,
    or an empty list if the object does not exist or cannot be read.
    """
    if s3_client is None or not bucket:
        log.debug("No s3_client or bucket — cannot read %s", uri)
        return []

    if not uri:
        log.debug("Empty URI for read action")
        return []

    # The URI is the S3 key (strip leading slash if present)
    s3_key = uri.lstrip("/")

    try:
        response = s3_client.get_object(Bucket=bucket, Key=s3_key)
        body = response["Body"].read()

        # Try to decode as text; if it fails, report it as binary
        try:
            content = body.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            content = body.hex()

        name = s3_key.split("/")[-1]
        return [
            SearchHit(
                repo_name="",
                data={
                    "name": name,
                    "path": s3_key,
                    "content": content,
                    "size": len(body),
                    "entry_type": "file",
                },
            )
        ]
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
        return [
            SearchHit(
                repo_name="",
                data={
                    "type": "root",
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
