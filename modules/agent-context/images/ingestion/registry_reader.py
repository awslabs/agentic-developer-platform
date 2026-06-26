"""Registry reader — reads knowledge_assets from the gateway DB.

Issue #2082 Phase 2: The refresh CronJob reads the registry (knowledge_assets)
instead of repos.txt. This module provides a thin reader that connects to the
gateway database and returns asset rows suitable for SQS publishing.

The gateway database lives on the same RDS instance as agent_context, so the
ingestion pod's IAM role already has network access + rds-connect permission.
We just need to connect to a different database name.

Configuration:
  GATEWAY_DB_NAME: the gateway database (default empty = disabled)
  GATEWAY_DB_HOST: override host (defaults to DB_HOST if empty)
  DB_PORT, DB_USER, DB_USE_IAM_AUTH, AWS_REGION: shared with agent-context DB

Design contract:
  - Returns only 'repo' assets (CronJob scope — URLs/docs have different refresh)
  - Carries tenant_id, owner_sub, project_id, installation_id per row
  - Filters to status IN ('queued', 'indexing', 'complete') — skip failed/pending
"""

from __future__ import annotations

import logging
import os
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


def _get_gateway_connection():
    """Connect to the gateway database (bedrockgateway).

    Uses IAM auth when DB_USE_IAM_AUTH=true (production), otherwise
    password-based auth (local dev / CI). Same logic as db.py but
    targeting the gateway DB name.
    """
    from config import settings

    # Gateway DB name must be configured
    gateway_db = settings.gateway_db_name
    if not gateway_db:
        raise RuntimeError(
            "GATEWAY_DB_NAME not set — cannot read registry. "
            "Set this env var to enable registry-backed refresh."
        )

    # Host: use gateway-specific override or fall back to shared DB_HOST
    host = settings.gateway_db_host or os.environ.get("DB_HOST", "")
    if not host:
        raise RuntimeError("Neither GATEWAY_DB_HOST nor DB_HOST set — cannot connect")

    import psycopg2

    port = int(os.environ.get("DB_PORT", "5432"))
    user = os.environ.get("DB_USER", "agent_context_rw")
    region = settings.aws_region
    use_iam = os.environ.get("DB_USE_IAM_AUTH", "true").lower() in ("true", "1", "yes")

    if use_iam:
        import boto3

        client = boto3.client("rds", region_name=region)
        password = client.generate_db_auth_token(
            DBHostname=host, Port=port, DBUsername=user, Region=region
        )
        sslmode = "require"
    else:
        password = os.environ.get("DB_PASSWORD", "")
        sslmode = os.environ.get("DB_SSLMODE", "prefer")

    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=gateway_db,
        user=user,
        password=password,
        sslmode=sslmode,
        connect_timeout=10,
    )
    conn.autocommit = True  # Read-only queries, no transaction needed
    return conn


def read_registry_assets(
    asset_type: str = "repo",
    *,
    statuses: tuple[str, ...] = ("queued", "indexing", "complete"),
) -> list[RegistryAsset]:
    """Read knowledge_assets rows from the gateway DB.

    Returns asset rows matching the given type and status filter.
    Only returns rows that have been successfully registered (not pending/failed).

    Args:
        asset_type: Filter by asset_type (default "repo").
        statuses: Tuple of status values to include.

    Returns:
        List of RegistryAsset dataclass instances.
    """
    conn = _get_gateway_connection()
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
    """Extract org/repo from a source_ref URL (e.g. https://github.com/acme/svc → acme/svc)."""
    for prefix in ("https://github.com/", "git@github.com:"):
        if source_ref.startswith(prefix):
            path = source_ref[len(prefix) :]
            if path.endswith(".git"):
                path = path[:-4]
            return path.rstrip("/")
    # Already in org/repo form
    return source_ref
