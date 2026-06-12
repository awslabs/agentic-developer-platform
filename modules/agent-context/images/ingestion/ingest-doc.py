#!/usr/bin/env python3
"""Document ingestion pipeline — converts PDFs, PPTX, DOCX, MD to markdown and indexes.

Pipeline:
  1. Fetch document (URL download or S3 cp)
  2. Convert to markdown using markitdown (handles PDF, PPTX, DOCX, HTML, images)
  3. Upload to OpenViking at viking://resources/docs/{slug}.md
  4. GraphRAG entity extraction (if enabled)
  5. Return result summary

Usage:
  python ingest-doc.py --source https://arxiv.org/pdf/2405.12345 --ov-url http://openviking:1933 --ov-key KEY
  python ingest-doc.py --source s3://adp-docs/sprint.pdf --ov-url http://openviking:1933 --ov-key KEY
  python ingest-doc.py --source s3://adp-docs/sprint.pdf --title "Sprint Review" --tags '{"team":"platform"}'
"""

from __future__ import annotations

import argparse
import json
import logging
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ingest-doc")

from config import settings

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
# OpenViking helpers
# ---------------------------------------------------------------------------


def ov_headers(api_key: str) -> dict[str, str]:
    return {
        "X-API-Key": api_key,
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "default",
    }


def upload_to_openviking(
    ov_url: str,
    headers: dict,
    content: str,
    filename: str,
    target_uri: str,
) -> bool:
    """Upload content to OpenViking via temp_upload + add resource."""
    try:
        files = {"file": (filename, content.encode("utf-8"), "application/octet-stream")}
        resp = requests.post(
            f"{ov_url}/api/v1/resources/temp_upload",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            files=files,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 300:
            log.warning("temp_upload failed: HTTP %d", resp.status_code)
            return False

        temp_id = resp.json().get("result", {}).get("temp_file_id")
        if not temp_id:
            log.warning("temp_upload returned no temp_file_id")
            return False

        resp = requests.post(
            f"{ov_url}/api/v1/resources",
            headers={**headers, "Content-Type": "application/json"},
            json={"temp_file_id": temp_id, "to": target_uri, "wait": True, "timeout": REQUEST_TIMEOUT},
            timeout=REQUEST_TIMEOUT + 10,
        )
        if resp.status_code < 300:
            log.info("Uploaded -> %s", target_uri)
            return True
        else:
            log.warning("add resource failed: HTTP %d", resp.status_code)
            return False

    except Exception as e:
        log.error("Upload failed for %s: %s", filename, e)
        return False


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
# Viking URI helpers
# ---------------------------------------------------------------------------


def source_to_slug(source: str) -> str:
    """Convert a source path to a safe slug for the Viking URI."""
    # Remove protocol prefixes
    slug = re.sub(r"^(https?://|s3://)", "", source)
    # Replace path separators and special chars
    slug = re.sub(r"[/\\:?#\[\]@!$&'()*+,;=]", "-", slug)
    # Collapse multiple dashes, trim
    slug = re.sub(r"-+", "-", slug).strip("-")
    # Truncate
    return slug[:200]


def source_to_viking_uri(source: str) -> str:
    """Convert a source to a viking:// URI for docs."""
    slug = source_to_slug(source)
    return f"viking://resources/docs/{slug}.md"


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
    ov_url: str,
    ov_key: str,
    title: str | None = None,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Full document ingestion pipeline."""
    tags = tags or {}
    start_time = time.monotonic()
    result: dict[str, Any] = {
        "source": source,
        "title": title,
        "tags": tags,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    with tempfile.TemporaryDirectory(prefix="ingest-doc-") as tmpdir:
        # Step 1: Fetch document
        log.info("Step 1: Fetching %s", source)
        local_path = fetch_document(source, tmpdir)
        if not local_path:
            result["steps"]["fetch"] = "failed"
            result["error"] = f"Failed to fetch {source}"
            return result
        result["steps"]["fetch"] = "ok"

        # Step 2: Convert to markdown
        log.info("Step 2: Converting to markdown")
        markdown = convert_to_markdown(local_path, title)
        if not markdown:
            result["steps"]["convert"] = "failed"
            result["error"] = f"Failed to convert {local_path}"
            return result
        result["steps"]["convert"] = "ok"
        result["markdown_length"] = len(markdown)

        # Add metadata header
        header = f"---\nsource: {source}\n"
        if title:
            header += f"title: {title}\n"
        if tags:
            header += f"tags: {json.dumps(tags)}\n"
        header += f"ingested_at: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
        markdown = header + markdown

        # Step 3: Upload to OpenViking
        log.info("Step 3: Uploading to OpenViking")
        headers = ov_headers(ov_key)
        target_uri = source_to_viking_uri(source)
        slug = source_to_slug(source)
        filename = f"{slug}.md"

        uploaded = upload_to_openviking(ov_url, headers, markdown, filename, target_uri)
        result["steps"]["openviking"] = "ok" if uploaded else "failed"
        result["viking_uri"] = target_uri

        # Step 4: GraphRAG extraction
        if GRAPHRAG_ENABLED:
            log.info("Step 4: GraphRAG entity extraction")
            graphrag_ok = extract_entities_graphrag(markdown, source, tags)
            result["steps"]["graphrag"] = "ok" if graphrag_ok else "failed"
        else:
            result["steps"]["graphrag"] = "skipped"

    elapsed = time.monotonic() - start_time
    result["duration_sec"] = round(elapsed, 1)

    # Determine overall status
    step_results = result["steps"]
    if all(v in ("ok", "skipped") for v in step_results.values()):
        result["status"] = "complete"
    elif step_results.get("openviking") == "ok":
        result["status"] = "partial"
    else:
        result["status"] = "failed"

    log.info(
        "Document ingestion %s: %s in %.1fs (steps: %s)",
        result["status"], source, elapsed, step_results,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="Ingest a document into OpenViking")
    parser.add_argument("--source", required=True, help="Document source (URL or S3 URI)")
    parser.add_argument("--title", help="Document title")
    parser.add_argument("--ov-url", default=settings.ov_url)
    parser.add_argument("--ov-key", default=settings.ov_key)
    parser.add_argument("--tags", default="{}", help="JSON tags object")
    args = parser.parse_args()

    if not args.ov_key:
        log.error("No OpenViking API key. Set --ov-key or OPENVIKING_ROOT_KEY env var.")
        sys.exit(1)

    try:
        tags = json.loads(args.tags)
    except json.JSONDecodeError:
        tags = {}

    result = ingest_document(
        source=args.source,
        ov_url=args.ov_url,
        ov_key=args.ov_key,
        title=args.title,
        tags=tags,
    )

    print(json.dumps(result, indent=2))

    if result.get("status") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
