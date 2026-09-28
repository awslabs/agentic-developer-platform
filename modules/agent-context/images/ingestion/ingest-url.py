#!/usr/bin/env python3
"""Per-URL web crawling pipeline.

Crawls web pages using crawl4ai, converts to markdown, and uploads to S3.
Supports sitemap discovery for documentation sites.

Usage:
  python ingest-url.py --url https://docs.example.com/
  python ingest-url.py --url https://blog.example.com/post --max-pages 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from defusedxml import ElementTree

import requests

from telemetry import configure_telemetry, get_logger

configure_telemetry(service_name="knowledge-layer-ingest-url")
log = get_logger("ingest-url")

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

# DynamoDB configuration
DYNAMO_TABLE = settings.dynamo_table
AWS_REGION = settings.aws_region

# Try to import crawl4ai — graceful fallback if not available
CRAWL4AI_AVAILABLE = False
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    CRAWL4AI_AVAILABLE = True
except ImportError:
    log.warning("crawl4ai not available — will use requests-based fallback")


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------


def discover_pages(base_url: str, max_pages: int = 100) -> list[str]:
    """Discover pages from sitemap.xml, falling back to just the base URL."""
    parsed = urlparse(base_url)
    sitemap_urls_to_try = [
        f"{parsed.scheme}://{parsed.netloc}/sitemap.xml",
        f"{base_url.rstrip('/')}/sitemap.xml",
    ]

    for sitemap_url in sitemap_urls_to_try:
        try:
            resp = requests.get(sitemap_url, timeout=30, headers={"User-Agent": "AgentContext-Crawler/1.0"})
            if resp.status_code != 200:
                continue

            tree = ElementTree.fromstring(resp.content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

            # Check if this is a sitemap index
            sitemaps = tree.findall(".//sm:sitemap/sm:loc", ns)
            if sitemaps:
                # Sitemap index — recursively discover from child sitemaps
                all_urls: list[str] = []
                for sitemap_loc in sitemaps[:10]:  # Limit child sitemaps
                    if sitemap_loc.text:
                        child_urls = _parse_sitemap(sitemap_loc.text, base_url, max_pages - len(all_urls))
                        all_urls.extend(child_urls)
                        if len(all_urls) >= max_pages:
                            break
                if all_urls:
                    log.info("Discovered %d pages from sitemap index at %s", len(all_urls), sitemap_url)
                    return all_urls[:max_pages]

            # Regular sitemap
            urls = _extract_urls_from_sitemap(tree, ns, base_url)
            if urls:
                log.info("Discovered %d pages from %s", len(urls), sitemap_url)
                return urls[:max_pages]

        except Exception as e:
            log.debug("Sitemap %s failed: %s", sitemap_url, e)

    log.info("No sitemap found for %s — will crawl URL directly", base_url)
    return [base_url]


def _parse_sitemap(sitemap_url: str, base_url: str, max_pages: int) -> list[str]:
    """Parse a single sitemap XML and return filtered URLs."""
    try:
        resp = requests.get(sitemap_url, timeout=30, headers={"User-Agent": "AgentContext-Crawler/1.0"})
        if resp.status_code != 200:
            return []
        tree = ElementTree.fromstring(resp.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        return _extract_urls_from_sitemap(tree, ns, base_url)[:max_pages]
    except Exception:
        return []


def _extract_urls_from_sitemap(tree: ElementTree.Element, ns: dict, base_url: str) -> list[str]:
    """Extract URLs from a parsed sitemap, filtered by the base URL path prefix."""
    parsed_base = urlparse(base_url)
    base_path = parsed_base.path.rstrip("/")

    urls = []
    for loc in tree.findall(".//sm:loc", ns):
        if loc.text:
            url = loc.text.strip()
            parsed = urlparse(url)
            # Filter: same domain and path starts with the base URL path
            if parsed.netloc == parsed_base.netloc:
                if not base_path or parsed.path.startswith(base_path):
                    urls.append(url)
    return urls


# ---------------------------------------------------------------------------
# Web crawling
# ---------------------------------------------------------------------------


async def crawl_url_crawl4ai(url: str) -> str | None:
    """Crawl a URL using crawl4ai and return clean markdown."""
    if not CRAWL4AI_AVAILABLE:
        return None

    try:
        browser_config = BrowserConfig(headless=True)
        run_config = CrawlerRunConfig()

        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)
            if result and result.markdown:
                return result.markdown
            return None
    except Exception as e:
        log.warning("crawl4ai failed for %s: %s", url, e)
        return None


def crawl_url_requests(url: str) -> str | None:
    """Fallback: crawl a URL using requests and basic HTML-to-markdown conversion."""
    try:
        resp = requests.get(
            url,
            timeout=30,
            headers={
                "User-Agent": "AgentContext-Crawler/1.0 (compatible; documentation indexer)",
            },
        )
        if resp.status_code != 200:
            log.warning("HTTP %d for %s", resp.status_code, url)
            return None

        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type:
            return _html_to_markdown(resp.text, url)
        elif "text/plain" in content_type or "text/markdown" in content_type:
            return resp.text
        else:
            log.warning("Unsupported content-type %s for %s", content_type, url)
            return None

    except Exception as e:
        log.warning("requests crawl failed for %s: %s", url, e)
        return None


def _html_to_markdown(html: str, url: str) -> str:
    """Basic HTML to markdown conversion (strips tags, preserves structure)."""
    import re

    # Remove script, style, nav, footer, header tags
    for tag in ("script", "style", "nav", "footer", "header", "aside"):
        html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # Convert headers
    for i in range(1, 7):
        html = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>", rf"\n{'#' * i} \1\n", html, flags=re.IGNORECASE)

    # Convert paragraphs and breaks
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<p[^>]*>", "\n\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</p>", "", html, flags=re.IGNORECASE)

    # Convert links
    html = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", html, flags=re.IGNORECASE)

    # Convert code blocks
    html = re.sub(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", r"\n```\n\1\n```\n", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", html, flags=re.IGNORECASE)

    # Convert lists
    html = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", html, flags=re.IGNORECASE)

    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", html)

    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # Add source header
    return f"<!-- Source: {url} -->\n\n{text}"


async def crawl_url(url: str) -> str | None:
    """Crawl a URL, trying crawl4ai first, falling back to requests."""
    if CRAWL4AI_AVAILABLE:
        result = await crawl_url_crawl4ai(url)
        if result:
            return result
    return crawl_url_requests(url)


# ---------------------------------------------------------------------------
# URL-to-S3-path conversion
# ---------------------------------------------------------------------------


def url_to_s3_path(url: str) -> str:
    """Convert a URL to an S3 content path.

    Example: https://docs.aws.amazon.com/bedrock/userguide/agents.html
          -> web/docs.aws.amazon.com/bedrock/userguide/agents.md
    """
    parsed = urlparse(url)
    domain = parsed.netloc
    path = parsed.path.strip("/")

    # Remove file extensions, normalize
    path = re.sub(r"\.(html?|php|asp|aspx)$", "", path)
    if not path:
        path = "index"

    return f"web/{domain}/{path}.md"


def url_to_filename(url: str) -> str:
    """Convert a URL to a safe filename."""
    parsed = urlparse(url)
    name = f"{parsed.netloc}{parsed.path}".replace("/", "-").strip("-")
    name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    return f"{name[:200]}.md"


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def ingest_url(
    url: str,
    max_pages: int = 100,
    registry_asset_id: str | None = None,
) -> dict[str, Any]:
    """Full crawling pipeline for one URL (may expand to multiple pages via sitemap).

    Args:
        url: Base URL to crawl.
        max_pages: Maximum pages to discover via sitemap.
        registry_asset_id: UUID from knowledge_assets registry (used as stage tracking key).
    """
    # Initialize S3 content store
    store = S3ContentStore(
        bucket_name=settings.s3_bucket_name,
        prefix=settings.s3_content_prefix,
        region_name=settings.aws_region,
    )

    # Initialize stage tracker if registry_asset_id provided (issue #2308)
    tracker = None
    if registry_asset_id and STAGE_TRACKING_AVAILABLE:
        try:
            db_conn = stage_db.get_connection()
            tracker = StageTracker(db_conn, registry_asset_id, repo_id=None, commit_sha=None)
            log.info("Stage tracking initialized for URL asset %s (run_id=%s)", registry_asset_id, tracker.run_id)
        except Exception as e:
            log.warning("Stage tracking init failed (non-fatal): %s", e)
            tracker = None

    result: dict[str, Any] = {
        "url": url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pages_discovered": 0,
        "pages_crawled": 0,
        "pages_uploaded": 0,
        "errors": [],
    }

    # Step 1: Discover pages
    pages = discover_pages(url, max_pages)
    result["pages_discovered"] = len(pages)
    log.info("Discovered %d pages for %s", len(pages), url)

    # Step 2: Crawl each page (tracked as "fetch" stage)
    crawled_pages: dict[str, str] = {}  # page_url -> markdown content

    for i, page_url in enumerate(pages):
        log.info("[%d/%d] Crawling %s", i + 1, len(pages), page_url)
        try:
            markdown = await crawl_url(page_url)
            if not markdown or len(markdown.strip()) < 50:
                log.warning("Skipping %s — empty or too short", page_url)
                result["errors"].append(f"empty: {page_url}")
                continue
            crawled_pages[page_url] = markdown
            result["pages_crawled"] += 1
        except Exception as e:
            log.warning("Error crawling %s: %s", page_url, e)
            result["errors"].append(f"error: {page_url}: {e}")

        # Rate limiting — be polite
        if i < len(pages) - 1:
            await asyncio.sleep(1.0)

    # Record fetch stage
    if tracker:
        with tracker.stage("fetch") as ctx:
            if result["pages_crawled"] > 0:
                ctx.set_artifact(url)
                ctx.set_metrics({
                    "pages_discovered": result["pages_discovered"],
                    "pages_crawled": result["pages_crawled"],
                })
                ctx.verify(lambda: result["pages_crawled"] > 0)
            else:
                ctx.fail("No pages crawled successfully")

    # Step 3: Upload crawled pages to S3 (tracked as "s3_upload" stage)
    last_s3_path = None
    for page_url, markdown in crawled_pages.items():
        s3_path = url_to_s3_path(page_url)
        uploaded = store.put_content(s3_path, markdown)
        if uploaded:
            result["pages_uploaded"] += 1
            last_s3_path = s3_path
        else:
            result["errors"].append(f"upload_failed: {page_url}")

    # Record s3_upload stage
    if tracker:
        with tracker.stage("s3_upload") as ctx:
            if result["pages_uploaded"] > 0 and last_s3_path:
                ctx.set_artifact(last_s3_path)
                ctx.set_metrics({"pages_uploaded": result["pages_uploaded"]})
                ctx.verify(lambda: store.exists(last_s3_path))
            else:
                ctx.fail("No pages uploaded to S3")

    # Finalize stage tracker
    if tracker:
        try:
            tracker.finalize()
            result["run_id"] = tracker.run_id
        except Exception as e:
            log.warning("Stage tracker finalize failed (non-fatal): %s", e)

    log.info(
        "URL ingestion complete: %d discovered, %d crawled, %d uploaded",
        result["pages_discovered"],
        result["pages_crawled"],
        result["pages_uploaded"],
    )
    return result


def update_dynamo_state_url(url: str, result: dict[str, Any], tags: dict[str, str] | None = None):
    """Update DynamoDB STATE record after URL ingestion."""
    try:
        import boto3
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DYNAMO_TABLE)
        pk = f"url#{url}"
        now = datetime.now(timezone.utc).isoformat()

        item = {
            "source": pk,
            "record_type": "STATE",
            "content_type": "url",
            "updated_at": now,
            "s3_status": "complete" if result.get("pages_uploaded", 0) > 0 else "failed",
            "pages_discovered": result.get("pages_discovered", 0),
            "pages_uploaded": result.get("pages_uploaded", 0),
            "graphrag_status": "skipped",
            "deepwiki_status": "skipped",
            "code_index_status": "skipped",
        }
        if tags:
            item["user_tags"] = tags

        # Store ETag/Last-Modified for change detection
        try:
            resp = requests.head(url, timeout=15, headers={"User-Agent": "AgentContext-Crawler/1.0"}, allow_redirects=True)
            if resp.status_code < 400:
                etag = resp.headers.get("ETag", "")
                last_mod = resp.headers.get("Last-Modified", "")
                if etag:
                    item["last_etag"] = etag
                if last_mod:
                    item["last_modified"] = last_mod
        except Exception:
            pass

        table.put_item(Item={k: v for k, v in item.items() if v is not None})
        log.info("DynamoDB state updated for url#%s", url)
    except Exception as e:
        log.warning("DynamoDB state update failed for %s: %s", url, e)


def main():
    parser = argparse.ArgumentParser(description="Crawl a URL and ingest into S3 content store")
    parser.add_argument("--url", required=True, help="URL to crawl")
    parser.add_argument("--max-pages", type=int, default=100, help="Max pages to crawl (default: 100)")
    parser.add_argument("--tags", default="{}", help="JSON tags object for metadata")
    parser.add_argument("--registry-asset-id", default=None, help="UUID from knowledge_assets registry for stage tracking")
    args = parser.parse_args()

    try:
        tags = json.loads(args.tags)
    except json.JSONDecodeError:
        tags = {}

    result = asyncio.run(
        ingest_url(
            url=args.url,
            max_pages=args.max_pages,
            registry_asset_id=args.registry_asset_id,
        )
    )

    # Update DynamoDB state
    update_dynamo_state_url(args.url, result, tags=tags)

    print(json.dumps(result, indent=2))

    if result["pages_uploaded"] == 0 and result["pages_discovered"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
