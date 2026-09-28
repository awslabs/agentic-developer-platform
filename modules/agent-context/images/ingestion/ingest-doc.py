#!/usr/bin/env python3
"""Document ingestion pipeline — converts PDFs, PPTX, DOCX, MD to markdown and indexes.

Pipeline:
  1. Fetch document (URL download or S3 cp)
  2. Convert to markdown using markitdown (handles PDF, PPTX, DOCX, HTML, images)
  3. Upload to S3 at docs/{slug}.md
  4. GraphRAG entity extraction (if enabled)
  5. Return result summary

Usage:
  python ingest-doc.py --source https://arxiv.org/pdf/2405.12345
  python ingest-doc.py --source s3://adp-docs/sprint.pdf
  python ingest-doc.py --source s3://adp-docs/sprint.pdf --title "Sprint Review" --tags '{"team":"platform"}'
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from telemetry import configure_telemetry, get_logger

configure_telemetry(service_name="knowledge-layer-ingest-doc")
log = get_logger("ingest-doc")

from config import settings
from s3_store import S3ContentStore

# Stage tracking (issue #2308) — optional, fail-open if DB unavailable
STAGE_TRACKING_AVAILABLE = False
try:
    import db as stage_db
    from stage_tracker import StageTracker

    STAGE_TRACKING_AVAILABLE = True
except ImportError:
    log.info("Stage tracking not available (db/stage_tracker not importable)")

REQUEST_TIMEOUT = settings.request_timeout
MAX_DOWNLOAD_SIZE = settings.max_download_size

# GraphRAG configuration
GRAPHRAG_ENABLED = settings.graphrag_enabled
NEPTUNE_ENDPOINT = settings.neptune_endpoint
NEPTUNE_PORT = settings.neptune_port
OPENSEARCH_ENDPOINT = settings.opensearch_endpoint
LLM_MODEL = settings.model_graphrag
LLM_BASE_URL = settings.llm_base_url

# Try to import markitdown
MARKITDOWN_AVAILABLE = False
try:
    from markitdown import MarkItDown

    MARKITDOWN_AVAILABLE = True
except ImportError:
    log.warning("markitdown not available — will use basic text extraction")


# ---------------------------------------------------------------------------
# Document fetching
# ---------------------------------------------------------------------------


def fetch_document(source: str, dest_dir: str) -> str | None:
    """Fetch a document from URL or S3 to a local path. Returns local path or None."""
    if source.startswith("s3://"):
        return _fetch_from_s3(source, dest_dir)
    elif source.startswith("http://") or source.startswith("https://"):
        return _fetch_from_url(source, dest_dir)
    elif os.path.exists(source):
        return source
    else:
        log.error("Unknown source format: %s", source)
        return None


def _fetch_from_s3(s3_uri: str, dest_dir: str) -> str | None:
    """Download a file from S3."""
    import boto3

    try:
        parts = s3_uri.replace("s3://", "").split("/", 1)
        bucket = parts[0]
        key = parts[1] if len(parts) > 1 else ""

        # Handle S3 folder (ends with /)
        if key.endswith("/"):
            log.info("S3 folder detected: %s — listing objects", s3_uri)
            s3 = boto3.client("s3")
            resp = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=100)
            paths = []
            for obj in resp.get("Contents", []):
                obj_key = obj["Key"]
                if obj_key == key:
                    continue
                filename = os.path.basename(obj_key)
                dest_path = os.path.join(dest_dir, filename)
                s3.download_file(bucket, obj_key, dest_path)
                paths.append(dest_path)
                log.info("Downloaded %s -> %s", obj_key, dest_path)
            # Return first file (caller should handle folders separately)
            return paths[0] if paths else None

        filename = os.path.basename(key) or "document"
        dest_path = os.path.join(dest_dir, filename)

        s3 = boto3.client("s3")
        s3.download_file(bucket, key, dest_path)
        log.info("Downloaded s3://%s/%s -> %s", bucket, key, dest_path)
        return dest_path

    except Exception as e:
        log.error("S3 download failed for %s: %s", s3_uri, e)
        return None


def _fetch_from_url(url: str, dest_dir: str) -> str | None:
    """Download a document from a URL."""
    try:
        resp = requests.get(
            url, timeout=60,
            headers={"User-Agent": "AgentContext-DocIngestion/1.0"},
            stream=True,
        )
        resp.raise_for_status()

        # Determine filename from URL or Content-Disposition
        content_disp = resp.headers.get("Content-Disposition", "")
        if "filename=" in content_disp:
            filename = content_disp.split("filename=")[1].strip('"\'')
        else:
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path) or "document"

        dest_path = os.path.join(dest_dir, filename)
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_SIZE:
                    log.error("Download exceeds %d MB limit: %s", MAX_DOWNLOAD_SIZE // (1024 * 1024), url)
                    return None
                f.write(chunk)

        log.info("Downloaded %s -> %s (%.1f KB)", url, dest_path, os.path.getsize(dest_path) / 1024)
        return dest_path

    except Exception as e:
        log.error("URL download failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Document conversion to markdown
# ---------------------------------------------------------------------------


def convert_to_markdown(file_path: str, title: str | None = None) -> str | None:
    """Convert a document to markdown using markitdown or basic extraction."""
    ext = Path(file_path).suffix.lower()

    if MARKITDOWN_AVAILABLE:
        try:
            md = MarkItDown()
            result = md.convert(file_path)
            content = result.text_content if hasattr(result, "text_content") else str(result)
            if content and len(content.strip()) > 10:
                log.info("markitdown converted %s (%d chars)", file_path, len(content))
                if title:
                    content = f"# {title}\n\n{content}"
                return content
        except Exception as e:
            log.warning("markitdown failed for %s: %s", file_path, e)

    # Fallback for text-based formats
    if ext in (".md", ".txt", ".rst", ".adoc"):
        try:
            with open(file_path) as f:
                content = f.read()
            if title and not content.startswith("#"):
                content = f"# {title}\n\n{content}"
            return content
        except Exception as e:
            log.error("Failed to read %s: %s", file_path, e)

    log.error("Cannot convert %s — markitdown not available and format not supported natively", file_path)
    return None


# ---------------------------------------------------------------------------
# S3 path helpers
# ---------------------------------------------------------------------------


def source_to_slug(source: str) -> str:
    """Convert a source path to a safe slug for the S3 path."""
    # Remove protocol prefixes
    slug = re.sub(r"^(https?://|s3://)", "", source)
    # Replace path separators and special chars
    slug = re.sub(r"[/\\:?#\[\]@!$&'()*+,;=]", "-", slug)
    # Collapse multiple dashes, trim
    slug = re.sub(r"-+", "-", slug).strip("-")
    # Truncate
    return slug[:200]


def source_to_s3_path(source: str) -> str:
    """Convert a source to an S3 content path for docs."""
    slug = source_to_slug(source)
    return f"docs/{slug}.md"


# ---------------------------------------------------------------------------
# GraphRAG extraction (reuses pattern from ingest-repo.py)
# ---------------------------------------------------------------------------


def extract_entities_graphrag(markdown: str, source: str, tags: dict[str, str]) -> bool:
    """Extract entities from markdown and write to Neptune + OpenSearch."""
    if not GRAPHRAG_ENABLED or not NEPTUNE_ENDPOINT:
        log.info("GraphRAG disabled or Neptune not configured — skipping")
        return False

    # Import GraphRAG helpers (same as ingest-repo.py uses)
    try:
        from gremlin_python.driver import client as gremlin_client
        from gremlin_python.driver.serializer import GraphSONSerializersV2d0

        log.info("Extracting entities via LLM for %s", source)

        # Call LLM to extract entities
        prompt = f"""Extract key entities (concepts, technologies, services, patterns) from this document.
Return a JSON array of objects: [{{"name": "entity name", "type": "concept|service|technology|pattern", "description": "brief description"}}]

Document source: {source}
Content (first 5000 chars):
{markdown[:5000]}"""

        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
            },
            timeout=120,
        )
        if resp.status_code >= 300:
            log.warning("LLM entity extraction failed: HTTP %d", resp.status_code)
            return False

        result_text = resp.json()["choices"][0]["message"]["content"]
        # Extract JSON array from response
        match = re.search(r"\[.*\]", result_text, re.DOTALL)
        if not match:
            log.warning("No entity array found in LLM response")
            return False

        entities = json.loads(match.group())
        log.info("Extracted %d entities from %s", len(entities), source)

        # Write to Neptune
        ws_url = f"wss://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"
        gc = gremlin_client.Client(
            ws_url, "g",
            message_serializer=GraphSONSerializersV2d0(),
        )

        slug = source_to_slug(source)
        for entity in entities[:50]:  # Cap at 50 entities per doc
            name = entity.get("name", "")
            etype = entity.get("type", "concept")
            desc = entity.get("description", "")
            if not name:
                continue

            query = (
                "g.V().has('entity', 'name', name).fold()"
                ".coalesce(unfold(), addV('entity').property('name', name))"
                ".property('type', etype)"
                ".property('description', desc)"
                ".property('source', source)"
                ".property('content_type', 'doc')"
            )
            # Add tag properties using parameterized bindings (prevent Gremlin injection)
            bindings: dict[str, str] = {
                "name": name, "etype": etype, "desc": desc, "source": slug,
            }
            for i, (tk, tv) in enumerate(tags.items()):
                # Sanitize tag key to alphanumeric + underscore only
                safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", tk)
                binding_key = f"tag_val_{i}"
                query += f".property('tag_{safe_key}', {binding_key})"
                bindings[binding_key] = tv

            gc.submit(query, bindings).all().result()

        gc.close()
        log.info("Wrote %d entities to Neptune for %s", len(entities), source)
        return True

    except Exception as e:
        log.warning("GraphRAG extraction failed for %s: %s", source, e)
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def ingest_document(
    source: str,
    title: str | None = None,
    tags: dict[str, str] | None = None,
    registry_asset_id: str | None = None,
) -> dict[str, Any]:
    """Full document ingestion pipeline.

    Args:
        source: Document source (URL, S3 URI, or local path).
        title: Optional document title.
        tags: Optional metadata tags.
        registry_asset_id: UUID from knowledge_assets registry for stage tracking.
    """
    tags = tags or {}
    start_time = time.monotonic()
    result: dict[str, Any] = {
        "source": source,
        "title": title,
        "tags": tags,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    # Initialize stage tracker if registry_asset_id provided (issue #2308)
    tracker = None
    if registry_asset_id and STAGE_TRACKING_AVAILABLE:
        try:
            db_conn = stage_db.get_connection()
            tracker = StageTracker(db_conn, registry_asset_id, repo_id=None, commit_sha=None)
            log.info("Stage tracking initialized for doc asset %s (run_id=%s)", registry_asset_id, tracker.run_id)
        except Exception as e:
            log.warning("Stage tracking init failed (non-fatal): %s", e)
            tracker = None

    # Initialize S3 content store
    store = S3ContentStore(
        bucket_name=settings.s3_bucket_name,
        prefix=settings.s3_content_prefix,
        region_name=settings.aws_region,
    )

    with tempfile.TemporaryDirectory(prefix="ingest-doc-") as tmpdir:
        # Step 1: Fetch document (tracked as "fetch" stage)
        log.info("Step 1: Fetching %s", source)
        local_path = fetch_document(source, tmpdir)
        if not local_path:
            result["steps"]["fetch"] = "failed"
            result["error"] = f"Failed to fetch {source}"
            if tracker:
                with tracker.stage("fetch") as ctx:
                    ctx.fail(f"Failed to fetch {source}")
                tracker.finalize()
                result["run_id"] = tracker.run_id
            return result
        result["steps"]["fetch"] = "ok"

        if tracker:
            with tracker.stage("fetch") as ctx:
                ctx.set_artifact(local_path)
                ctx.set_metrics({"file_size_bytes": os.path.getsize(local_path)})
                ctx.verify(lambda: os.path.exists(local_path))

        # Step 2: Convert to markdown (tracked as "convert" stage)
        log.info("Step 2: Converting to markdown")
        markdown = convert_to_markdown(local_path, title)
        if not markdown:
            result["steps"]["convert"] = "failed"
            result["error"] = f"Failed to convert {local_path}"
            if tracker:
                with tracker.stage("convert") as ctx:
                    ctx.fail(f"Failed to convert {local_path}")
                tracker.finalize()
                result["run_id"] = tracker.run_id
            return result
        result["steps"]["convert"] = "ok"
        result["markdown_length"] = len(markdown)

        if tracker:
            with tracker.stage("convert") as ctx:
                ctx.set_artifact(source)
                ctx.set_metrics({"markdown_length": len(markdown)})
                ctx.verify(lambda: len(markdown) > 0)

        # Add metadata header
        header = f"---\nsource: {source}\n"
        if title:
            header += f"title: {title}\n"
        if tags:
            header += f"tags: {json.dumps(tags)}\n"
        header += f"ingested_at: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
        markdown = header + markdown

        # Step 3: Upload to S3 (tracked as "s3_upload" stage)
        log.info("Step 3: Uploading to S3")
        s3_path = source_to_s3_path(source)

        uploaded = store.put_content(s3_path, markdown)
        result["steps"]["s3_upload"] = "ok" if uploaded else "failed"
        result["s3_path"] = s3_path

        if tracker:
            with tracker.stage("s3_upload") as ctx:
                if uploaded:
                    ctx.set_artifact(s3_path)
                    ctx.set_metrics({"content_length": len(markdown)})
                    ctx.verify(lambda: store.exists(s3_path))
                else:
                    ctx.fail(f"S3 upload failed for {s3_path}")

        # Step 4: GraphRAG extraction (tracked as "graphrag" stage)
        if GRAPHRAG_ENABLED:
            log.info("Step 4: GraphRAG entity extraction")
            graphrag_ok = extract_entities_graphrag(markdown, source, tags)
            result["steps"]["graphrag"] = "ok" if graphrag_ok else "failed"

            if tracker:
                with tracker.stage("graphrag") as ctx:
                    if graphrag_ok:
                        ctx.set_artifact(f"neptune://{source_to_slug(source)}")
                        ctx.set_metrics({"enabled": True})
                        ctx.verify(lambda: graphrag_ok)
                    else:
                        ctx.fail("GraphRAG entity extraction failed")
        else:
            result["steps"]["graphrag"] = "skipped"
            if tracker:
                tracker.mark_skipped("graphrag", "GraphRAG disabled")

    elapsed = time.monotonic() - start_time
    result["duration_sec"] = round(elapsed, 1)

    # Determine overall status
    step_results = result["steps"]
    if all(v in ("ok", "skipped") for v in step_results.values()):
        result["status"] = "complete"
    elif step_results.get("s3_upload") == "ok":
        result["status"] = "partial"
    else:
        result["status"] = "failed"

    # Finalize stage tracker
    if tracker:
        try:
            tracker.finalize()
            result["run_id"] = tracker.run_id
        except Exception as e:
            log.warning("Stage tracker finalize failed (non-fatal): %s", e)

    log.info(
        "Document ingestion %s: %s in %.1fs (steps: %s)",
        result["status"], source, elapsed, step_results,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="Ingest a document into S3 content store")
    parser.add_argument("--source", required=True, help="Document source (URL or S3 URI)")
    parser.add_argument("--title", help="Document title")
    parser.add_argument("--tags", default="{}", help="JSON tags object")
    parser.add_argument("--registry-asset-id", default=None, help="UUID from knowledge_assets registry for stage tracking")
    args = parser.parse_args()

    try:
        tags = json.loads(args.tags)
    except json.JSONDecodeError:
        tags = {}

    result = ingest_document(
        source=args.source,
        title=args.title,
        tags=tags,
        registry_asset_id=args.registry_asset_id,
    )

    print(json.dumps(result, indent=2))

    if result.get("status") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
