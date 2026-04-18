#!/usr/bin/env python3
"""Per-URL web crawling pipeline.

Crawls web pages using crawl4ai, converts to markdown, and uploads to OpenViking.
Supports sitemap discovery for documentation sites.

Usage:
  python ingest-url.py --url https://docs.example.com/ --ov-url http://openviking:1933 --ov-key KEY
  python ingest-url.py --url https://blog.example.com/post --ov-url http://openviking:1933 --ov-key KEY --max-pages 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ingest-url")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))

# DynamoDB configuration
DYNAMO_TABLE = os.getenv("DYNAMO_TABLE", "adp-context-service-state")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Try to import crawl4ai — graceful fallback if not available
CRAWL4AI_AVAILABLE = False
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    CRAWL4AI_AVAILABLE = True
except ImportError:
    log.warning("crawl4ai not available — will use requests-based fallback")


# ---------------------------------------------------------------------------
# OpenViking helpers (shared pattern with ingest-repo.py)
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
    """Upload a file to OpenViking via temp_upload + add resource."""
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
# URL-to-Viking-path conversion
# ---------------------------------------------------------------------------


def url_to_viking_path(url: str) -> str:
    """Convert a URL to a viking:// resource path.

    Example: https://docs.aws.amazon.com/bedrock/userguide/agents.html
          -> viking://resources/web/docs.aws.amazon.com/bedrock/userguide/agents.md
    """
    parsed = urlparse(url)
    domain = parsed.netloc
    path = parsed.path.strip("/")

    # Remove file extensions, normalize
    path = re.sub(r"\.(html?|php|asp|aspx)$", "", path)
    if not path:
        path = "index"

    return f"viking://resources/web/{domain}/{path}.md"


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
    ov_url: str,
    ov_key: str,
    max_pages: int = 100,
) -> dict[str, Any]:
    """Full crawling pipeline for one URL (may expand to multiple pages via sitemap)."""
    headers = ov_headers(ov_key)
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

    # Step 2 & 3: Crawl each page and upload
    for i, page_url in enumerate(pages):
        log.info("[%d/%d] Crawling %s", i + 1, len(pages), page_url)
        try:
            markdown = await crawl_url(page_url)
            if not markdown or len(markdown.strip()) < 50:
                log.warning("Skipping %s — empty or too short", page_url)
                result["errors"].append(f"empty: {page_url}")
                continue

            result["pages_crawled"] += 1

            # Upload to OpenViking
            target_uri = url_to_viking_path(page_url)
            filename = url_to_filename(page_url)
            uploaded = upload_to_openviking(ov_url, headers, markdown, filename, target_uri)
            if uploaded:
                result["pages_uploaded"] += 1
            else:
                result["errors"].append(f"upload_failed: {page_url}")

            # Rate limiting — be polite
            if i < len(pages) - 1:
                await asyncio.sleep(1.0)

        except Exception as e:
            log.warning("Error crawling %s: %s", page_url, e)
            result["errors"].append(f"error: {page_url}: {e}")

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
            "openviking_status": "complete" if result.get("pages_uploaded", 0) > 0 else "failed",
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
    parser = argparse.ArgumentParser(description="Crawl a URL and ingest into OpenViking")
    parser.add_argument("--url", required=True, help="URL to crawl")
    parser.add_argument("--ov-url", default=os.getenv("OV_URL", "http://openviking.agent-context.svc.cluster.local:1933"))
    parser.add_argument("--ov-key", default=os.getenv("OPENVIKING_ROOT_KEY", os.getenv("ROOT_KEY", "")))
    parser.add_argument("--max-pages", type=int, default=100, help="Max pages to crawl (default: 100)")
    parser.add_argument("--tags", default="{}", help="JSON tags object for metadata")
    args = parser.parse_args()

    if not args.ov_key:
        log.error("No OpenViking API key. Set --ov-key or OPENVIKING_ROOT_KEY env var.")
        sys.exit(1)

    try:
        tags = json.loads(args.tags)
    except json.JSONDecodeError:
        tags = {}

    result = asyncio.run(
        ingest_url(
            url=args.url,
            ov_url=args.ov_url,
            ov_key=args.ov_key,
            max_pages=args.max_pages,
        )
    )

    # Update DynamoDB state
    update_dynamo_state_url(args.url, result, tags=tags)

    print(json.dumps(result, indent=2))

    if result["pages_uploaded"] == 0 and result["pages_discovered"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
