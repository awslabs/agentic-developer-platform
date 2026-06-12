#!/usr/bin/env python3
"""Queue Publisher — reads source files, checks DynamoDB state, enqueues changed items to SQS.

Reads index_content/*.txt files, checks DynamoDB for each source's current state,
and publishes SQS messages only for items that have changed (or all if --force).

Usage:
  python publish-ingestion.py --type repo --source-file /config/repos.txt
  python publish-ingestion.py --type url --source-file /config/urls.txt
  python publish-ingestion.py --type doc --source-file /config/docs.txt
  python publish-ingestion.py --type infra --source-file /config/accounts.txt
  python publish-ingestion.py --type repo --force  # Re-process everything
  python publish-ingestion.py --all                # Publish all types
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import boto3
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("publish-ingestion")

# ---------------------------------------------------------------------------
# Configuration (centralized via config.py)
# ---------------------------------------------------------------------------

from config import settings

AWS_REGION = settings.aws_region
SQS_QUEUE_URL = settings.sqs_queue_url
DYNAMO_TABLE = settings.dynamo_table

# Default source file paths (used inside K8s pods with ConfigMap mounts)
DEFAULT_SOURCE_FILES = {
    "repo": settings.repos_file,
    "url": settings.urls_file,
    "doc": settings.docs_file,
    "infra": settings.accounts_file,
}

# Steps per content type
STEPS_BY_TYPE = {
    "repo": ["s3_upload", "cgc", "deepwiki", "graphrag"],
    "url": ["s3_upload", "graphrag"],
    "doc": ["s3_upload", "graphrag"],
    "infra": ["discovery", "graphrag"],
}

# AWS clients (lazy init)
_sqs = None
_dynamodb = None


def sqs_client():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=AWS_REGION)
    return _sqs


def dynamodb_resource():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _dynamodb


# ---------------------------------------------------------------------------
# Source file parsing (with tags support)
# ---------------------------------------------------------------------------


def parse_source_file(path: str, content_type: str) -> list[tuple[str, str | None, dict[str, str]]]:
    """Parse a source file, returning list of (source, title, tags).

    Supports two formats:
      Simple:    source_identifier
      Extended:  source_identifier | title | tag1:val1, tag2:val2
    """
    entries = []
    if not os.path.exists(path):
        log.warning("Source file not found: %s", path)
        return entries

    with open(path) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = [p.strip() for p in line.split("|")]
            source = parts[0]
            title = parts[1] if len(parts) > 1 else None
            tags: dict[str, str] = {}

            if len(parts) > 2:
                for tag_str in parts[2].split(","):
                    tag_str = tag_str.strip()
                    if ":" in tag_str:
                        k, v = tag_str.split(":", 1)
                        tags[k.strip()] = v.strip()

            if not source:
                continue

            entries.append((source, title, tags))

    log.info("Parsed %d entries from %s", len(entries), path)
    return entries


# ---------------------------------------------------------------------------
# DynamoDB state checks
# ---------------------------------------------------------------------------


def get_dynamo_state(source: str, content_type: str) -> dict[str, Any] | None:
    """Get the current STATE record for a source from DynamoDB."""
    try:
        table = dynamodb_resource().Table(DYNAMO_TABLE)
        pk = f"{content_type}#{source}"
        resp = table.get_item(Key={"source": pk, "record_type": "STATE"})
        return resp.get("Item")
    except Exception as e:
        log.debug("DynamoDB get failed for %s: %s", source, e)
        return None


def has_changed(source: str, state: dict[str, Any] | None, content_type: str) -> bool:
    """Check if a source has changed since last ingestion."""
    if state is None:
        return True  # New source

    if content_type == "repo":
        return _repo_has_changed(source, state)
    elif content_type == "url":
        return _url_has_changed(source, state)
    elif content_type == "doc":
        return _doc_has_changed(source, state)
    elif content_type == "infra":
        return True  # Always re-discover infra

    return True


def _repo_has_changed(source: str, state: dict[str, Any]) -> bool:
    """Check if a repo has new commits since last ingestion."""
    prev_sha = state.get("last_sha")
    if not prev_sha:
        return True
    try:
        result = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{source}", "HEAD"],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            current_sha = result.stdout.decode().split()[0]
            if current_sha != prev_sha:
                log.info("Repo %s changed: %s -> %s", source, prev_sha[:8], current_sha[:8])
                return True
            return False
    except Exception as e:
        log.warning("git ls-remote failed for %s: %s", source, e)
    return True  # Re-process on error


def _url_has_changed(source: str, state: dict[str, Any]) -> bool:
    """Check if a URL has changed via ETag/Last-Modified."""
    try:
        resp = requests.head(
            source, timeout=15,
            headers={"User-Agent": "AgentContext-Publisher/1.0"},
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return True

        etag = resp.headers.get("ETag", "")
        last_modified = resp.headers.get("Last-Modified", "")

        if etag and etag == state.get("last_etag", ""):
            return False
        if last_modified and last_modified == state.get("last_modified", ""):
            return False
        return True
    except Exception:
        return True


def _doc_has_changed(source: str, state: dict[str, Any]) -> bool:
    """Check if a document has changed (S3 last modified or URL ETag)."""
    if source.startswith("s3://"):
        try:
            parts = source.replace("s3://", "").split("/", 1)
            bucket, key = parts[0], parts[1] if len(parts) > 1 else ""
            s3 = boto3.client("s3", region_name=AWS_REGION)
            resp = s3.head_object(Bucket=bucket, Key=key)
            last_mod = resp["LastModified"].isoformat()
            return last_mod != state.get("last_modified", "")
        except Exception:
            return True
    else:
        return _url_has_changed(source, state)


# ---------------------------------------------------------------------------
# SQS publishing
# ---------------------------------------------------------------------------


def publish_message(
    source: str,
    content_type: str,
    tags: dict[str, str],
    title: str | None = None,
    force: bool = False,
    triggered_by: str = "manual",
) -> bool:
    """Publish a single ingestion message to SQS."""
    now = datetime.now(timezone.utc).isoformat()
    message = {
        "source": source,
        "content_type": content_type,
        "steps": STEPS_BY_TYPE.get(content_type, []),
        "force": force,
        "tags": tags,
        "triggered_by": triggered_by,
        "enqueued_at": now,
    }
    if title:
        message["title"] = title

    try:
        sqs_client().send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message),
            MessageAttributes={
                "content_type": {
                    "DataType": "String",
                    "StringValue": content_type,
                },
            },
        )
        log.info("Enqueued: %s (%s)", source, content_type)
        return True
    except Exception as e:
        log.error("Failed to enqueue %s: %s", source, e)
        return False


# ---------------------------------------------------------------------------
# Main publish flow
# ---------------------------------------------------------------------------


def publish(
    content_type: str,
    source_file: str,
    force: bool = False,
    triggered_by: str = "manual",
) -> dict[str, int]:
    """Read source file, check DynamoDB state, enqueue changed items."""
    stats = {"total": 0, "enqueued": 0, "skipped": 0, "errors": 0}

    items = parse_source_file(source_file, content_type)
    stats["total"] = len(items)

    for source, title, tags in items:
        state = get_dynamo_state(source, content_type)

        if not force and not has_changed(source, state, content_type):
            log.info("SKIP %s — no changes", source)
            stats["skipped"] += 1
            continue

        if publish_message(source, content_type, tags, title, force, triggered_by):
            stats["enqueued"] += 1
        else:
            stats["errors"] += 1

    log.info(
        "Publish complete (%s): %d total, %d enqueued, %d skipped, %d errors",
        content_type, stats["total"], stats["enqueued"], stats["skipped"], stats["errors"],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Publish ingestion messages to SQS")
    parser.add_argument("--type", choices=["repo", "url", "doc", "infra"],
                        help="Content type to publish")
    parser.add_argument("--source-file", help="Path to source file (overrides default)")
    parser.add_argument("--force", action="store_true", help="Re-process all items")
    parser.add_argument("--all", action="store_true", help="Publish all content types")
    parser.add_argument("--triggered-by", default="manual",
                        help="Trigger source (manual, daily_refresh, gitops)")
    args = parser.parse_args()

    if not SQS_QUEUE_URL:
        log.error("SQS_QUEUE_URL not set")
        sys.exit(1)

    if args.all:
        total_stats = {"total": 0, "enqueued": 0, "skipped": 0, "errors": 0}
        for ctype, default_file in DEFAULT_SOURCE_FILES.items():
            source_file = default_file
            if not os.path.exists(source_file):
                log.info("Skipping %s — file not found: %s", ctype, source_file)
                continue
            stats = publish(ctype, source_file, args.force, args.triggered_by)
            for k in total_stats:
                total_stats[k] += stats[k]
        log.info("All types published: %s", json.dumps(total_stats))
        print(json.dumps(total_stats, indent=2))
    elif args.type:
        source_file = args.source_file or DEFAULT_SOURCE_FILES.get(args.type, "")
        if not source_file:
            log.error("No source file for type %s", args.type)
            sys.exit(1)
        stats = publish(args.type, source_file, args.force, args.triggered_by)
        print(json.dumps(stats, indent=2))
        if stats["errors"] > 0 and stats["enqueued"] == 0:
            sys.exit(1)
    else:
        parser.error("Either --type or --all is required")


if __name__ == "__main__":
    main()
