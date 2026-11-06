"""Bounded ingestion catalog commands. No SQL or row selector comes from a worker."""

from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import text

from src.knowledge.ingestion_authority import IngestionAuthority, deny

STAGES = frozenset(
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
        "fetch",
        "convert",
        "s3_upload",
    }
)


async def repository(db, authority: IngestionAuthority):
    if authority.asset_type != "repo" or authority.registered_only:
        return None
    row = (
        (
            await db.execute(
                text("""SELECT id, repo_name, tenant_id, owner_sub, allowed_principals,
        acl_public_verified FROM repositories WHERE repo_name=:repo FOR UPDATE"""),
                {"repo": authority.source},
            )
        )
        .mappings()
        .first()
    )
    if row is None or (row["tenant_id"], row["owner_sub"]) != (authority.tenant_id, authority.owner_sub):
        raise deny()
    collision = (
        await db.execute(
            text("""SELECT 1 FROM repositories
        WHERE replace(repo_name,'/','-')=:name AND repo_name<>:repo
          AND tenant_id IS NOT DISTINCT FROM :tenant
          AND owner_sub IS NOT DISTINCT FROM :owner LIMIT 1"""),
            {"name": authority.source.replace("/", "-"), "repo": authority.source, "tenant": authority.tenant_id, "owner": authority.owner_sub},
        )
    ).first()
    if collision is not None:
        raise deny()  # Legacy flattened filenames must not confer another repo's authority.
    return row


async def catalog_command(db, authority: IngestionAuthority, command: str, args: dict):
    """Execute only commands used by db.py, always under canonical asset scope."""
    run_id = authority.run_id
    if "run_id" in args and str(args["run_id"]) != run_id:
        raise deny()
    if "repo" in args and args["repo"] != authority.tracking_source:
        raise deny()
    repo = await repository(db, authority)
    repo_id = str(repo["id"]) if repo else None
    if "repo_id" in args and args["repo_id"] != repo_id:
        raise deny()
    if command == "ensure_repo_exists":
        if repo is None or args.get("org_repo") != authority.source or args.get("git_url") != f"https://github.com/{authority.source}":
            raise deny()
        # ACLs and ownership are created/refreshed by trusted admission, never by
        # the parser. Ignore no worker-provided replacement silently.
        if args.get("tenant_id") != authority.tenant_id or args.get("owner_sub") != authority.owner_sub:
            raise deny()
        if args.get("allowed_principals") is not None and args["allowed_principals"] != repo["allowed_principals"]:
            raise deny()
        return repo_id
    if command == "create_index_run":
        sha = args.get("commit_sha")
        if sha is not None and not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
            raise deny()
        await db.execute(
            text("""INSERT INTO index_runs (id, repo_id, started_at, status, commit_sha)
            VALUES (CAST(:id AS uuid), CAST(:repo_id AS uuid), NOW(), 'running', :sha)
            ON CONFLICT (id) DO NOTHING"""),
            {"id": run_id, "repo_id": repo_id, "sha": sha},
        )
        return run_id
    if command == "should_skip_stage":
        # A new attempt must re-verify its own outputs. A previous asset's SHA
        # or telemetry cannot grant authority or suppress this attempt's work.
        return False
    if command in {"start_stage", "skip_stage"}:
        stage = args.get("stage")
        if stage not in STAGES:
            raise deny()
        stage_id = str(uuid.uuid5(uuid.UUID(run_id), stage))
        await db.execute(
            text("""INSERT INTO index_run_stages
            (id,run_id,repo,stage,status,attempts,started_at,worker_pod,error,completed_at)
            SELECT CAST(:id AS uuid),id,:repo,:stage,:status,1,NOW(),:pod,:reason,
                   CASE WHEN :status='skipped' THEN NOW() ELSE NULL END
            FROM index_runs WHERE id=CAST(:run_id AS uuid)
            ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, attempts=index_run_stages.attempts+1,
                started_at=NOW(), completed_at=EXCLUDED.completed_at, error=EXCLUDED.error,
                artifact_ref=NULL, verified_at=NULL, metrics=NULL"""),
            {
                "id": stage_id,
                "run_id": run_id,
                "repo": authority.tracking_source,
                "stage": stage,
                "status": "skipped" if command == "skip_stage" else "running",
                "pod": str(args.get("worker_pod") or "")[:253],
                "reason": str(args.get("reason") or "")[:2000],
            },
        )
        return stage_id
    if command in {"verify_stage", "fail_stage"}:
        stage_id = args.get("stage_id")
        if stage_id not in {str(uuid.uuid5(uuid.UUID(run_id), s)) for s in STAGES}:
            raise deny()
        await db.execute(
            text("""UPDATE index_run_stages SET status=:status, completed_at=NOW(),
            verified_at=CASE WHEN :status='verified' THEN NOW() ELSE NULL END,
            artifact_ref=:artifact, error=:error, metrics=CAST(:metrics AS jsonb)
            WHERE id=CAST(:id AS uuid) AND run_id=CAST(:run AS uuid)"""),
            {
                "id": stage_id,
                "run": run_id,
                "status": "verified" if command == "verify_stage" else "failed",
                "artifact": str(args.get("artifact_ref") or "")[:2048],
                "error": str(args.get("error") or "")[:2000],
                "metrics": json.dumps(args.get("metrics")),
            },
        )
        return None
    if command == "complete_index_run":
        await db.execute(
            text("""UPDATE index_runs SET completed_at=NOW(),
            duration_ms=(EXTRACT(EPOCH FROM (NOW()-started_at))*1000)::int,
            status=CASE
              WHEN EXISTS (SELECT 1 FROM index_run_stages WHERE run_id=index_runs.id AND status='failed') THEN 'partial'
              WHEN EXISTS (SELECT 1 FROM index_run_stages WHERE run_id=index_runs.id)
                AND NOT EXISTS (SELECT 1 FROM index_run_stages WHERE run_id=index_runs.id AND status NOT IN ('verified','skipped')) THEN 'complete'
              ELSE 'incomplete' END
            WHERE id=CAST(:id AS uuid)"""),
            {"id": run_id},
        )
        return None
    if command == "upsert_dependencies":
        records = args.get("records")
        if repo_id is None or not isinstance(records, list) or len(records) > 10000:
            raise deny()
        for record in records:
            coordinate = record.get("package_url")
            if not isinstance(coordinate, str) or not coordinate or len(coordinate) > 512 or record.get("source", "code") != "code":
                raise deny()
            await db.execute(
                text("""INSERT INTO dependencies
                (repo_id,package_coordinate,version,is_transitive,source,base_image)
                VALUES(CAST(:repo_id AS uuid),:coordinate,:version,:transitive,'code',NULL)
                ON CONFLICT (repo_id,package_coordinate,source) DO UPDATE
                SET version=EXCLUDED.version,is_transitive=EXCLUDED.is_transitive"""),
                {
                    "repo_id": repo_id,
                    "coordinate": coordinate,
                    "version": str(record.get("package_version") or "")[:128],
                    "transitive": record.get("is_transitive") is True,
                },
            )
        return len(records)
    if command == "update_repo_sbom_status":
        if repo_id is None or args.get("image_status") is not None:
            raise deny()
        status, sha = args.get("source_status"), args.get("last_source_sha")
        if status not in {None, "complete", "failed", "pending"} or (sha and not re.fullmatch(r"[a-fA-F0-9]{40}", sha)):
            raise deny()
        await db.execute(
            text("""UPDATE repositories SET sbom_status=COALESCE(:status,sbom_status),
            last_indexed_sha=COALESCE(:sha,last_indexed_sha),updated_at=NOW() WHERE id=CAST(:id AS uuid)"""),
            {"status": status, "sha": sha, "id": repo_id},
        )
        return None
    raise deny()
