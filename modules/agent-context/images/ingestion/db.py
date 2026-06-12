"""Postgres connection helper and upsert functions for the dependencies table.

Provides IAM-authenticated connections to the agent_context database and
batch upsert operations for SBOM dependency rows.

Design ref: docs/design-notes/1358-dual-rail-sbom-generation.md (section 4)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sbom_parser import DependencyRecord

log = logging.getLogger("db")


def _get_iam_auth_token(host: str, port: int, user: str, region: str) -> str:
    """Generate an RDS IAM authentication token."""
    import boto3

    client = boto3.client("rds", region_name=region)
    return client.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=user,
        Region=region,
    )


def get_connection():
    """Get a Postgres connection to the agent_context database.

    Uses IAM auth when DB_USE_IAM_AUTH=true (production), otherwise
    falls back to password-based auth (local dev / CI).

    Environment variables:
        DB_HOST: RDS endpoint
        DB_PORT: port (default 5432)
        DB_NAME: database name (default "agent_context")
        DB_USER: database user (default "agent_context_rw")
        DB_PASSWORD: password (only for non-IAM auth)
        DB_USE_IAM_AUTH: "true" to use IAM auth (default)
        AWS_REGION: region for IAM token generation
    """
    import psycopg2

    host = os.environ.get("DB_HOST", "")
    port = int(os.environ.get("DB_PORT", "5432"))
    dbname = os.environ.get("DB_NAME", "agent_context")
    user = os.environ.get("DB_USER", "agent_context_rw")
    region = os.environ.get("AWS_REGION", "us-east-1")
    use_iam = os.environ.get("DB_USE_IAM_AUTH", "true").lower() in ("true", "1", "yes")

    if not host:
        raise RuntimeError("DB_HOST not set — cannot connect to Postgres")

    if use_iam:
        password = _get_iam_auth_token(host, port, user, region)
        sslmode = "require"
    else:
        password = os.environ.get("DB_PASSWORD", "")
        sslmode = os.environ.get("DB_SSLMODE", "prefer")

    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode=sslmode,
        connect_timeout=10,
    )
    conn.autocommit = False
    return conn


def upsert_dependencies(
    conn,
    repo_id: str,
    records: list["DependencyRecord"],
    batch_size: int = 100,
) -> int:
    """Upsert dependency records into the dependencies table.

    Uses INSERT ... ON CONFLICT DO UPDATE to handle re-indexing without duplicates.
    The unique constraint is (repo_id, package_coordinate, source).

    Args:
        conn: psycopg2 connection (with autocommit=False).
        repo_id: UUID of the repository in the repositories table.
        records: Parsed dependency records from sbom_parser.
        batch_size: Number of rows per INSERT statement.

    Returns:
        Number of rows upserted.
    """
    if not records:
        return 0

    sql = """
        INSERT INTO dependencies (
            repo_id, package_coordinate, version, is_transitive, source, base_image
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (repo_id, package_coordinate, source)
        DO UPDATE SET
            version = EXCLUDED.version,
            is_transitive = EXCLUDED.is_transitive,
            base_image = EXCLUDED.base_image
    """

    total = 0
    cursor = conn.cursor()
    try:
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            rows = [
                (
                    repo_id,
                    r.package_url,  # package_coordinate = full purl
                    r.package_version,
                    r.is_transitive,
                    r.source,
                    r.base_image,
                )
                for r in batch
            ]
            cursor.executemany(sql, rows)
            total += len(batch)

        conn.commit()
        log.info("Upserted %d dependency rows for repo %s", total, repo_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()

    return total


def ensure_repo_exists(conn, org_repo: str, git_url: str) -> str:
    """Ensure a repository row exists, returning its UUID.

    Creates the row if it doesn't exist (INSERT ... ON CONFLICT DO NOTHING)
    then fetches the id.
    """
    owner = org_repo.split("/")[0] if "/" in org_repo else org_repo
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO repositories (repo_name, git_url, owner)
            VALUES (%s, %s, %s)
            ON CONFLICT (repo_name) DO NOTHING
            """,
            (org_repo, git_url, owner),
        )
        conn.commit()

        cursor.execute(
            "SELECT id FROM repositories WHERE repo_name = %s",
            (org_repo,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"Repository {org_repo} not found after ensure")
        return str(row[0])
    finally:
        cursor.close()


def update_repo_sbom_status(
    conn,
    repo_id: str,
    source_status: str | None = None,
    image_status: str | None = None,
    last_source_sha: str | None = None,
    has_dockerfile: bool | None = None,
) -> None:
    """Update SBOM-related fields on the repositories table.

    Only updates fields that are not None.
    """
    updates = []
    params: list = []

    if source_status is not None:
        updates.append("sbom_status = %s")
        params.append(source_status)
    if image_status is not None:
        updates.append("sbom_status = %s")
        params.append(image_status)
    if last_source_sha is not None:
        updates.append("last_indexed_sha = %s")
        params.append(last_source_sha)

    if not updates:
        return

    updates.append("updated_at = NOW()")
    params.append(repo_id)

    sql = f"UPDATE repositories SET {', '.join(updates)} WHERE id = %s"
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        conn.commit()
    finally:
        cursor.close()
