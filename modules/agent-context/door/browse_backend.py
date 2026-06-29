"""Browse backend — navigate indexed content (browse verb).

Lists indexed repos from the Postgres catalog/S3 and source files from Zoekt.
Provides importable functions for the Context MCP server.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .acl import SearchHit

log = logging.getLogger(__name__)

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
        return await _read_content(
            uri,
            s3_client=s3_client,
            bucket=bucket,
        )
    else:
        log.warning("Unknown browse action: %s", action)
        return []


async def _list_path(
    uri: str,
    *,
    db_pool: Any | None,
    s3_client: Any | None,
    bucket: str,
    content_prefix: str,
    depth: int,
    zoekt_url: str = "",
) -> list[SearchHit]:
    """List contents at a URI path.

    Two URI schemes are supported:

    1. **Content-path URIs** (start with a known content root like "content/"):
       Route directly to S3 listing. E.g., "content/wikis" lists all wiki files,
       "content/code-indexes" lists code-index JSONs.

    2. **Repo-path URIs** (everything else):
       Root "/" lists repos from catalog; "/repo-name" lists top-level via Zoekt;
       "/repo-name/subdir" lists deeper paths via Zoekt.
    """
    parts = [p for p in uri.split("/") if p]

    # Root level: list all indexed repos from catalog (or S3 fallback)
    if not parts:
        return await _list_repos(db_pool, s3_client=s3_client, bucket=bucket)

    # --- Content-path routing ---
    # If the first path component is a known content root (e.g., "content"),
    # route directly to S3 listing rather than treating it as a repo name.
    if parts[0] in _CONTENT_ROOTS:
        s3_prefix = "/".join(parts)
        return await _list_s3_prefix(
            s3_prefix,
            s3_client=s3_client,
            bucket=bucket,
        )

    # --- Repo-path routing ---
    repo_name = parts[0]

    # Repo level with no sub-path: list top-level directories from Zoekt
    if len(parts) == 1:
        if zoekt_url:
            return await _list_zoekt_files(repo_name, "", zoekt_url=zoekt_url)
        return _list_repo_content_types(repo_name)

    # Deeper paths: use Zoekt to list files at that directory
    sub_path = "/".join(parts[1:])

    if zoekt_url:
        return await _list_zoekt_files(repo_name, sub_path, zoekt_url=zoekt_url)

    # Fall back to S3 content listing if Zoekt unavailable
    content_type = parts[1]
    sub_path_s3 = "/".join(parts[2:]) if len(parts) > 2 else ""
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
    """List all indexed repos from the catalog (Postgres) or S3 fallback.

    When Postgres is unavailable (rds_enabled=false), discovers repos from the
    code-indexes/ S3 prefix — each file is named {org}-{repo}.json.
    """
    # Try Postgres first (authoritative catalog)
    if db_pool is not None:
        try:
            conn = db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT repo_name, description FROM repositories ORDER BY repo_name LIMIT 200"
                    )
                    rows = cur.fetchall()
                    if rows:
                        return [
                            SearchHit(
                                repo_name=row[0],
                                data={
                                    "repo_id": row[0],
                                    "type": "repository",
                                    "description": row[1] or "",
                                    "entry_type": "directory",
                                },
                            )
                            for row in rows
                        ]
            finally:
                db_pool.putconn(conn)
        except Exception:
            log.warning("Failed to list repos from catalog", exc_info=True)

    # S3 fallback: discover repos from code-indexes/ prefix
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
                    # Convert org-repo.json back to a display name
                    repo_name = filename.removesuffix(".json")
                    results.append(
                        SearchHit(
                            repo_name=repo_name,
                            data={
                                "repo_id": repo_name,
                                "type": "repository",
                                "description": "",
                                "entry_type": "directory",
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

    # Build Zoekt query to find files in this repo under the path
    # Use "f:" filter to match file paths, "r:" to scope to repo
    # Anchor repo name with / prefix and $ suffix to avoid matching substrings
    # e.g., "skills" should match "github.com/mattpocock/skills" but not "agent-skills"
    escaped_name = re.escape(repo_name)
    repo_filter = f"/{escaped_name}$"
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
    """Get metadata for a specific path."""
    parts = [p for p in uri.split("/") if p]
    if not parts:
        return [
            SearchHit(
                repo_name="",
                data={"type": "root", "description": "Agent Context content index"},
            )
        ]

    repo_name = parts[0]

    # Repo info from catalog
    if len(parts) == 1 and db_pool is not None:
        try:
            conn = db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT repo_name, description FROM repositories WHERE repo_name = %s",
                        (repo_name,),
                    )
                    row = cur.fetchone()
                    if row:
                        return [
                            SearchHit(
                                repo_name=row[0],
                                data={
                                    "repo_id": row[0],
                                    "type": "repository",
                                    "description": row[1] or "",
                                },
                            )
                        ]
            finally:
                db_pool.putconn(conn)
        except Exception:
            log.warning("Failed to get repo info for %s", repo_name, exc_info=True)

    return []
