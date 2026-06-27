"""Registry reader — reads knowledge_assets from the agent_context DB.

Issue #2182: The knowledge_assets table lives in agent_context (the KL's
dedicated database). This reader uses the same DB connection as the rest of
the ingestion code (via db.get_connection()) — no cross-DB machinery needed.

Configuration:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_USE_IAM_AUTH, AWS_REGION:
  standard agent-context DB settings (shared with all ingestion code).

Design contract:
  - Returns only 'repo' assets (CronJob scope — URLs/docs have different refresh)
  - Carries tenant_id, owner_sub, project_id, installation_id per row
  - Filters to status IN ('queued', 'indexing', 'complete') — skip failed/pending
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("registry_reader")


@dataclass
class RegistryAsset:
    """A knowledge_assets row relevant to refresh."""

    asset_id: str
    source_ref: str
    asset_type: str
    tenant_id: str | None
    owner_sub: str | None
    project_id: str | None
    installation_id: int | None


def read_registry_assets(
    asset_type: str = "repo",
    *,
    statuses: tuple[str, ...] = ("queued", "indexing", "complete"),
) -> list[RegistryAsset]:
    """Read knowledge_assets rows from the agent_context DB.

    Uses the standard db.get_connection() — same database the ingestion
    workers already connect to. No cross-DB connection needed (Issue #2182).

    Args:
        asset_type: Filter by asset_type (default "repo").
        statuses: Tuple of status values to include.

    Returns:
        List of RegistryAsset dataclass instances.
    """
    from db import get_connection

    conn = get_connection()
    conn.autocommit = True  # Read-only queries, no transaction needed
    try:
        cursor = conn.cursor()
        # Use %s placeholders (psycopg2 style)
        placeholders = ",".join(["%s"] * len(statuses))
        query = f"""
            SELECT id, source_ref, asset_type, tenant_id, owner_sub,
                   project_id::text, installation_id
            FROM knowledge_assets
            WHERE asset_type = %s
              AND status IN ({placeholders})
            ORDER BY created_at
        """
        params: tuple[Any, ...] = (asset_type, *statuses)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        cursor.close()

        assets = []
        for row in rows:
            assets.append(
                RegistryAsset(
                    asset_id=str(row[0]),
                    source_ref=row[1],
                    asset_type=row[2],
                    tenant_id=row[3],
                    owner_sub=row[4],
                    project_id=row[5],
                    installation_id=row[6],
                )
            )
        log.info(
            "Read %d %s assets from registry (statuses=%s)",
            len(assets),
            asset_type,
            statuses,
        )
        return assets
    finally:
        conn.close()


def extract_org_repo(source_ref: str) -> str:
    """Extract org/repo from a source_ref URL (e.g. https://github.com/acme/svc -> acme/svc)."""
    for prefix in ("https://github.com/", "git@github.com:"):
        if source_ref.startswith(prefix):
            path = source_ref[len(prefix) :]
            if path.endswith(".git"):
                path = path[:-4]
            return path.rstrip("/")
    # Already in org/repo form
    return source_ref
