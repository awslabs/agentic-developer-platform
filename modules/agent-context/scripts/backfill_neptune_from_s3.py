#!/usr/bin/env python3
"""One-time backfill: load SCIP CSVs from S3 into Neptune.

Issue #2493 (Child 4/4 of EPIC #1529).

While the Neptune endpoint was empty (neptune_enabled=false), the ingestion
pipeline still ran SCIP extraction and uploaded CSVs to
`s3://<bucket>/neptune-bulk-load/<repo_safe>/<timestamp>/{vertices,edges}.csv`,
but the `load_to_neptune()` step was skipped. This script loads those existing
CSVs into Neptune for repos that were ingested before the endpoint was wired.

It reuses the proven `scip_neptune_loader.load_to_neptune()` path (delete-then-load
via openCypher UNWIND, per-repo isolation), so re-running is idempotent.

Repo resolution: S3 prefixes are safe-encoded (`org/repo` -> `org-repo`), which is
ambiguous for multi-hyphen org names (e.g. `aws-e/adp`). Rather than guess, the
true `repo:String` value is read from the first data row of vertices.csv.

Usage:
  # Dry run (default) — show what would be loaded, no mutations
  python scripts/backfill_neptune_from_s3.py

  # Load all repos found in S3 (skips the corpus-excluded set)
  python scripts/backfill_neptune_from_s3.py --apply

  # Load a single repo (matches on the true repo name from the CSV)
  python scripts/backfill_neptune_from_s3.py --apply --repo colbymchenry/codegraph

Environment variables:
  S3_FILES_BUCKET  S3 bucket holding neptune-bulk-load/ (required)
  NEPTUNE_ENDPOINT Neptune cluster host (required for --apply)
  NEPTUNE_PORT     Neptune port (default 8182)
  AWS_REGION       AWS region (default us-east-1)
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile

import boto3

# Add images/ingestion so we can reuse the proven Neptune loader.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "images", "ingestion"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_neptune_from_s3")

S3_PREFIX = "neptune-bulk-load/"

# Repos deliberately excluded from the eval corpus (index_content/repos.txt).
# CopilotKit/CopilotKit is >600MB and OOM-kills DeepWiki (#1564); its CSVs may
# still linger in S3 from an earlier run, but they must NOT be loaded.
CORPUS_EXCLUDED = {"CopilotKit/CopilotKit"}


def latest_csv_prefixes(s3, bucket: str) -> dict[str, str]:
    """Map each S3 repo-safe prefix to its newest timestamp subdirectory.

    Returns {repo_safe: "neptune-bulk-load/<repo_safe>/<latest_ts>/"}.
    """
    paginator = s3.get_paginator("list_objects_v2")
    # First list the repo-level common prefixes.
    repo_prefixes: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=S3_PREFIX, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            repo_prefixes.append(cp["Prefix"])

    latest: dict[str, str] = {}
    for repo_prefix in repo_prefixes:
        repo_safe = repo_prefix[len(S3_PREFIX) :].rstrip("/")
        timestamps: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=repo_prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                timestamps.append(cp["Prefix"])
        if timestamps:
            # Timestamp dirs sort lexicographically == chronologically (YYYYMMDDTHHMMSSZ).
            latest[repo_safe] = sorted(timestamps)[-1]
    return latest


def read_repo_name(s3, bucket: str, ts_prefix: str) -> str:
    """Read the true `repo:String` from the first data row of vertices.csv."""
    obj = s3.get_object(Bucket=bucket, Key=f"{ts_prefix}vertices.csv")
    # Stream just enough lines to parse the header + first data row.
    body = obj["Body"]
    text = body.read().decode("utf-8", errors="replace")
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        return row.get("repo:String", "").strip()
    return ""


def download_csvs(s3, bucket: str, ts_prefix: str, dest_dir: str) -> tuple[str, str]:
    """Download vertices.csv + edges.csv into dest_dir. Returns (vertices, edges)."""
    vertices_path = os.path.join(dest_dir, "vertices.csv")
    edges_path = os.path.join(dest_dir, "edges.csv")
    s3.download_file(bucket, f"{ts_prefix}vertices.csv", vertices_path)
    s3.download_file(bucket, f"{ts_prefix}edges.csv", edges_path)
    return vertices_path, edges_path


def _make_csv_output(vertices_path: str, edges_path: str):
    """Build the minimal CSVOutput that load_to_neptune() consumes."""
    from scip_neptune_csv import CSVOutput

    return CSVOutput(
        vertices_path=vertices_path,
        edges_path=edges_path,
        vertex_count=0,
        edge_count=0,
        calls_count=0,
        references_count=0,
        output_dir=os.path.dirname(vertices_path),
    )


def backfill(
    *,
    bucket: str,
    neptune_endpoint: str,
    neptune_port: str,
    region: str,
    only_repo: str | None,
    apply: bool,
) -> int:
    """Discover S3 CSVs and load each into Neptune. Returns process exit code."""
    s3 = boto3.client("s3", region_name=region)

    latest = latest_csv_prefixes(s3, bucket)
    if not latest:
        log.error("No repos found under s3://%s/%s", bucket, S3_PREFIX)
        return 1

    # Resolve each safe prefix to its true repo name and decide inclusion.
    plan: list[tuple[str, str]] = []  # (repo_name, ts_prefix)
    for repo_safe, ts_prefix in sorted(latest.items()):
        repo_name = read_repo_name(s3, bucket, ts_prefix)
        if not repo_name:
            log.warning("Skipping %s — could not read repo name from CSV", repo_safe)
            continue
        if repo_name in CORPUS_EXCLUDED:
            log.info("Skipping %s — excluded from eval corpus (repos.txt)", repo_name)
            continue
        if only_repo and repo_name != only_repo:
            continue
        plan.append((repo_name, ts_prefix))

    if only_repo and not plan:
        log.error("Requested repo %s not found in S3 backfill data", only_repo)
        return 1

    log.info("Backfill plan (%d repos): %s", len(plan), [p[0] for p in plan])
    if not apply:
        log.info("DRY RUN — no changes made. Re-run with --apply to load.")
        for repo_name, ts_prefix in plan:
            log.info("  would load %s from s3://%s/%s", repo_name, bucket, ts_prefix)
        return 0

    from scip_neptune_loader import load_to_neptune

    neptune_ep = f"{neptune_endpoint}:{neptune_port}"
    failures = 0
    for repo_name, ts_prefix in plan:
        log.info("Loading %s from s3://%s/%s", repo_name, bucket, ts_prefix)
        with tempfile.TemporaryDirectory() as tmp:
            vertices_path, edges_path = download_csvs(s3, bucket, ts_prefix, tmp)
            csv_output = _make_csv_output(vertices_path, edges_path)
            result = load_to_neptune(csv_output, neptune_ep, region)
        if result.get("error") or not result.get("success"):
            log.error("Load failed for %s: %s", repo_name, result)
            failures += 1
        else:
            log.info(
                "Loaded %s: %d vertices + %d edges",
                repo_name,
                result.get("vertices_loaded", 0),
                result.get("edges_loaded", 0),
            )

    if failures:
        log.error("Backfill completed with %d failed repos", failures)
        return 1
    log.info("Backfill complete — %d repos loaded", len(plan))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Neptune from S3 SCIP CSVs (#2493)")
    parser.add_argument(
        "--apply", action="store_true", help="Perform the load (default is dry run)"
    )
    parser.add_argument("--repo", default=None, help="Load a single repo (e.g. org/repo)")
    args = parser.parse_args()

    bucket = os.environ.get("S3_FILES_BUCKET", "")
    if not bucket:
        log.error("S3_FILES_BUCKET is required")
        return 1

    neptune_endpoint = os.environ.get("NEPTUNE_ENDPOINT", "")
    if args.apply and not neptune_endpoint:
        log.error("NEPTUNE_ENDPOINT is required for --apply")
        return 1

    return backfill(
        bucket=bucket,
        neptune_endpoint=neptune_endpoint,
        neptune_port=os.environ.get("NEPTUNE_PORT", "8182"),
        region=os.environ.get("AWS_REGION", "us-east-1"),
        only_repo=args.repo,
        apply=args.apply,
    )


if __name__ == "__main__":
    sys.exit(main())
