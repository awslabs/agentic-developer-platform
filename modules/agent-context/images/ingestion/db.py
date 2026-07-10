"""Postgres connection helper, dependency upsert, and index-run stage tracking.

Provides IAM-authenticated connections to the agent_context database,
batch upsert operations for SBOM dependency rows, and per-stage indexing
tracking with verify-after-write (issue #1423).

Design ref: docs/design-notes/1358-dual-rail-sbom-generation.md (section 4)
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sbom_parser import DependencyRecord

log = logging.getLogger("db")

# Valid stage names for index_run_stages
VALID_STAGES = frozenset(
    {
        "clone",
        "cgc_structural",
        "scip_structural",
        "embed_vectors",
        "sbom_source",
        "sbom_image",
        "deepwiki",
        "zoekt_index",
        "graphrag",
        # URL/doc ingestion stages (issue #2308)
        "fetch",
        "convert",
        "s3_upload",
    }
)

# Valid status values for index_run_stages
VALID_STATUSES = frozenset(
    {
        "pending",
        "running",
        "succeeded",
        "failed",
        "verified",
        "skipped",
    }
)


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


def ensure_repo_exists(
    conn,
    org_repo: str,
    git_url: str,
    *,
    allowed_principals: list[str] | None = None,
    tenant_id: str | None = None,
    owner_sub: str | None = None,
) -> str:
    """Ensure a repository row exists, returning its UUID.

    Creates the row if it doesn't exist (INSERT ... ON CONFLICT DO NOTHING)
    then fetches the id.

    Issue #2082: When allowed_principals is provided, sets it on insert and
    updates existing rows that have empty principals (fixes the #1920 root
    cause where repos were invisible due to empty allowed_principals).
    When tenant_id is provided, stamps it on the row for correct scoping.

    Issue #3529: When owner_sub is provided, stamps it on the row so the
    registering user's scoped queries (door/acl.py) correctly include this
    repo. Without this, the ACL row gets tenant_id derived from the GitHub
    org name instead of the scope envelope's values.
    """
    import json as _json

    owner = org_repo.split("/")[0] if "/" in org_repo else org_repo
    # Default: public if no principals specified (backward-compatible)
    principals_json = _json.dumps(allowed_principals if allowed_principals is not None else ["*"])
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO repositories (repo_name, git_url, owner, allowed_principals, tenant_id, owner_sub)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo_name) DO UPDATE
                SET allowed_principals = CASE
                        WHEN repositories.allowed_principals = '[]'::jsonb
                          OR repositories.allowed_principals IS NULL
                        THEN EXCLUDED.allowed_principals
                        ELSE repositories.allowed_principals
                    END,
                    tenant_id = COALESCE(EXCLUDED.tenant_id, repositories.tenant_id),
                    owner_sub = COALESCE(EXCLUDED.owner_sub, repositories.owner_sub)
            """,
            (org_repo, git_url, owner, principals_json, tenant_id, owner_sub),
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
        cursor.execute(sql, params)  # nosemgrep: sqlalchemy-execute-raw-query
        conn.commit()
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Index Run Stage Tracking (issue #1423)
# ---------------------------------------------------------------------------


def create_index_run(conn, repo_id: str | None, repo: str, commit_sha: str | None = None) -> str:
    """Create a new index_runs header row. Returns the run_id (UUID).

    This run_id is the canonical spine for all stage rows in this indexing run.

    Args:
        repo_id: UUID of the repository row (None for non-repo assets like URL/doc).
        repo: Canonical identifier — org/repo for repos, registry_asset_id for URL/doc.
        commit_sha: Git commit SHA (None for non-repo assets).
    """
    run_id = str(uuid.uuid4())
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO index_runs (id, repo_id, started_at, status, commit_sha)
            VALUES (%s, %s, NOW(), 'running', %s)
            """,
            (run_id, repo_id, commit_sha),
        )
        conn.commit()
        log.info("Created index run %s for repo %s (sha=%s)", run_id, repo, commit_sha)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    return run_id


def start_stage(
    conn,
    run_id: str,
    repo: str,
    stage: str,
    *,
    worker_pod: str | None = None,
) -> str:
    """Mark a stage as running. Returns the stage row id.

    Increments attempts counter. Creates the row if it doesn't exist for this
    run_id+stage combination, otherwise updates the existing row.

    Args:
        worker_pod: The Kubernetes pod name running this stage (for log lookup).
    """
    if stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage!r}. Must be one of {sorted(VALID_STAGES)}")

    stage_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO index_run_stages
                (id, run_id, repo, stage, status, attempts, started_at, worker_pod)
            VALUES (%s, %s, %s, %s, 'running', 1, %s, %s)
            """,
            (stage_id, run_id, repo, stage, now, worker_pod),
        )
        conn.commit()
        log.info("Stage %s started for run %s (repo=%s, pod=%s)", stage, run_id, repo, worker_pod)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    return stage_id


def verify_stage(
    conn,
    stage_id: str,
    artifact_ref: str,
    *,
    metrics: dict | None = None,
) -> None:
    """Mark a stage as verified after read-back confirms the artifact exists.

    This is the ONLY path to 'verified' status. Sets verified_at timestamp.

    Args:
        metrics: Optional per-stage metrics dict (e.g. {"symbols": 42, "files": 10}).
                 Stored as JSONB for the detailed ingestion view.
    """
    import json as _json

    now = datetime.now(timezone.utc)
    metrics_json = _json.dumps(metrics) if metrics is not None else None
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE index_run_stages
            SET status = 'verified',
                artifact_ref = %s,
                verified_at = %s,
                completed_at = %s,
                metrics = %s
            WHERE id = %s
            """,
            (artifact_ref, now, now, metrics_json, stage_id),
        )
        conn.commit()
        log.info("Stage %s verified (artifact=%s, metrics=%s)", stage_id, artifact_ref, metrics)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def fail_stage(
    conn,
    stage_id: str,
    error: str,
) -> None:
    """Mark a stage as failed. Never sets verified_at."""
    now = datetime.now(timezone.utc)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE index_run_stages
            SET status = 'failed',
                error = %s,
                completed_at = %s
            WHERE id = %s
            """,
            (error, now, stage_id),
        )
        conn.commit()
        log.info("Stage %s failed: %s", stage_id, error[:200])
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def skip_stage(
    conn,
    run_id: str,
    repo: str,
    stage: str,
    reason: str = "disabled",
) -> str:
    """Record a stage as skipped (e.g., feature disabled, not applicable).

    Returns the stage row id.
    """
    if stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage!r}. Must be one of {sorted(VALID_STAGES)}")

    stage_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO index_run_stages
                (id, run_id, repo, stage, status, error, started_at, completed_at)
            VALUES (%s, %s, %s, %s, 'skipped', %s, %s, %s)
            """,
            (stage_id, run_id, repo, stage, reason, now, now),
        )
        conn.commit()
        log.info("Stage %s skipped for run %s: %s", stage, run_id, reason)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    return stage_id


def should_skip_stage(conn, repo: str, stage: str, commit_sha: str | None = None) -> bool:
    """Check if a stage should be skipped (already verified at this SHA).

    Skip logic keys off 'verified' status, NOT a SHA alone.
    A stage re-runs unless its latest row is 'verified' at the current commit SHA.
    If commit_sha is None, never skip (force re-run).
    """
    if commit_sha is None:
        return False

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT 1 FROM index_run_stages irs
            JOIN index_runs ir ON ir.id = irs.run_id
            WHERE irs.repo = %s
              AND irs.stage = %s
              AND irs.status = 'verified'
              AND ir.commit_sha = %s
            LIMIT 1
            """,
            (repo, stage, commit_sha),
        )
        row = cursor.fetchone()
        return row is not None
    finally:
        cursor.close()


def complete_index_run(conn, run_id: str) -> None:
    """Mark an index_runs header as complete (all stages done).

    Sets completed_at, duration_ms, and derives overall status from stages:
    - all verified/skipped → 'complete'
    - any failed → 'partial'
    """
    cursor = conn.cursor()
    try:
        # Derive status from stages
        cursor.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'failed') as failed_count,
                COUNT(*) FILTER (WHERE status IN ('verified', 'skipped')) as done_count,
                COUNT(*) as total
            FROM index_run_stages
            WHERE run_id = %s
            """,
            (run_id,),
        )
        row = cursor.fetchone()
        failed_count, done_count, total = row if row else (0, 0, 0)

        if failed_count > 0:
            overall_status = "partial"
        elif done_count == total and total > 0:
            overall_status = "complete"
        else:
            overall_status = "incomplete"

        cursor.execute(
            """
            UPDATE index_runs
            SET status = %s,
                completed_at = NOW(),
                duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at))::INT * 1000
            WHERE id = %s
            """,
            (overall_status, run_id),
        )
        conn.commit()
        log.info("Index run %s completed with status=%s", run_id, overall_status)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def reconcile_stages(conn, repo: str, verify_fn) -> list[dict]:
    """Reconciliation: for each verified stage, confirm artifact still exists.

    Calls verify_fn(artifact_ref, stage) for each verified stage row.
    If verify_fn returns False, marks the stage as 'failed' (drift detected).

    Args:
        conn: Postgres connection.
        repo: org/repo to reconcile.
        verify_fn: callable(artifact_ref: str, stage: str) -> bool

    Returns:
        List of dicts for stages that drifted: [{stage_id, stage, artifact_ref}]
    """
    cursor = conn.cursor()
    drifted = []
    try:
        cursor.execute(
            """
            SELECT id, stage, artifact_ref
            FROM index_run_stages
            WHERE repo = %s
              AND status = 'verified'
              AND artifact_ref IS NOT NULL
            ORDER BY verified_at DESC
            """,
            (repo,),
        )
        rows = cursor.fetchall()

        for stage_id, stage, artifact_ref in rows:
            if not verify_fn(artifact_ref, stage):
                # Drift detected — mark as failed
                now = datetime.now(timezone.utc)
                cursor.execute(
                    """
                    UPDATE index_run_stages
                    SET status = 'failed',
                        error = 'reconciliation: artifact missing',
                        completed_at = %s
                    WHERE id = %s
                    """,
                    (now, stage_id),
                )
                drifted.append(
                    {
                        "stage_id": stage_id,
                        "stage": stage,
                        "artifact_ref": artifact_ref,
                    }
                )
                log.warning(
                    "Reconciliation drift: repo=%s stage=%s artifact=%s",
                    repo,
                    stage,
                    artifact_ref,
                )

        if drifted:
            conn.commit()
        return drifted
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
