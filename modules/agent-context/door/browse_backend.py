"""Browse backend — navigate indexed content (browse verb).

Lists indexed repos from the Postgres catalog and S3 content objects.
Provides importable functions for the Context MCP server.
"""

from __future__ import annotations

import logging
from typing import Any

from .acl import SearchHit

log = logging.getLogger(__name__)


async def browse(
    action: str,
    uri: str,
    *,
    db_pool: Any | None = None,
    s3_client: Any | None = None,
    bucket: str = "",
    content_prefix: str = "content",
    depth: int = 1,
) -> list[SearchHit]:
    """Navigate the indexed content filesystem.

    Parameters
    ----------
    action:
        Action to perform: "ls" (list), "tree" (recursive list), "info" (metadata).
    uri:
        URI path to browse. Root "/" lists repos, "/repo-name" lists content types,
        "/repo-name/wikis" lists wiki files, etc.
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

    Returns
    -------
    List of SearchHit representing directory entries.
    """
    uri = uri.strip().rstrip("/")

    if action == "ls":
        return await _list_path(
            uri,
            db_pool=db_pool,
            s3_client=s3_client,
            bucket=bucket,
            content_prefix=content_prefix,
            depth=depth,
        )
    elif action == "tree":
        return await _list_path(
            uri,
            db_pool=db_pool,
            s3_client=s3_client,
            bucket=bucket,
            content_prefix=content_prefix,
            depth=min(depth, 3),
        )
    elif action == "info":
        return await _get_info(
            uri,
            db_pool=db_pool,
            s3_client=s3_client,
            bucket=bucket,
            content_prefix=content_prefix,
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
) -> list[SearchHit]:
    """List contents at a URI path."""
    parts = [p for p in uri.split("/") if p]

    # Root level: list all indexed repos from catalog
    if not parts:
        return await _list_repos(db_pool)

    repo_name = parts[0]

    # Repo level: list content types available
    if len(parts) == 1:
        return _list_repo_content_types(repo_name)

    # Deeper: list S3 objects under the content path
    content_type = parts[1]  # e.g., "wikis", "code-indexes", etc.
    sub_path = "/".join(parts[2:]) if len(parts) > 2 else ""

    return await _list_s3_content(
        repo_name,
        content_type,
        sub_path,
        s3_client=s3_client,
        bucket=bucket,
        content_prefix=content_prefix,
    )


async def _list_repos(db_pool: Any | None) -> list[SearchHit]:
    """List all indexed repos from the catalog (Postgres)."""
    if db_pool is None:
        log.debug("No db_pool — returning empty repo list")
        return []

    try:
        conn = db_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT repo_name, description FROM repositories ORDER BY repo_name LIMIT 200"
                )
                rows = cur.fetchall()
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
