"""External data scanner service (US-G2).

Monitors external ML/AI data sources and stores structured findings.
Each source has a dedicated scan function that fetches, parses, scores,
and stores results. The scanner is designed to run as a scheduled task
triggered by cron or CloudWatch Events via the agent gateway.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_finding import VALID_SOURCES, ResearchFinding
from app.models.workspace import Workspace
from app.services.scanner_sources import (
    ALL_SOURCES,
    AUTO_TAGS,
    COMPETITOR_URLS,
    HIGH_RELEVANCE_KEYWORDS,
    SourceConfig,
)

logger = logging.getLogger(__name__)

# Notification threshold constants
HIGH_RELEVANCE_THRESHOLD = 70
LOW_RELEVANCE_THRESHOLD = 30
HTTP_TIMEOUT = 30.0
MAX_RESULTS_PER_SOURCE = 50


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


def compute_relevance_score(
    title: str,
    summary: str,
    source_config: SourceConfig,
    raw_data: dict[str, Any] | None = None,
) -> int:
    """Compute a 0-100 relevance score for a finding.

    Scoring factors:
    - Keyword matches in title (weighted higher) and summary
    - High-relevance keyword matches (bonus)
    - Source-specific signals (e.g. GitHub stars, HN score, Reddit upvotes)
    """
    score = 0
    text_lower = f"{title} {summary}".lower()
    title_lower = title.lower()

    # Base keyword matching from source config
    for keyword in source_config.keywords:
        kw = keyword.lower()
        if kw in title_lower:
            score += 8  # Title matches worth more
        elif kw in text_lower:
            score += 4

    # High-relevance keyword bonus
    for keyword in HIGH_RELEVANCE_KEYWORDS:
        kw = keyword.lower()
        if kw in title_lower:
            score += 10
        elif kw in text_lower:
            score += 5

    # Source-specific scoring signals
    if raw_data:
        # GitHub stars
        stars = raw_data.get("stargazers_count", 0)
        if stars > 10000:
            score += 15
        elif stars > 1000:
            score += 10
        elif stars > 100:
            score += 5

        # HN / Reddit score
        ext_score = raw_data.get("score", 0)
        if ext_score > 500:
            score += 15
        elif ext_score > 200:
            score += 10
        elif ext_score > 100:
            score += 5

        # HuggingFace downloads
        downloads = raw_data.get("downloads", 0)
        if downloads > 100000:
            score += 15
        elif downloads > 10000:
            score += 10
        elif downloads > 1000:
            score += 5

    # Clamp to 0-100
    return max(0, min(100, score))


def compute_tags(title: str, summary: str) -> list[str]:
    """Automatically assign tags based on keyword detection."""
    text_lower = f"{title} {summary}".lower()
    tags = []
    for tag, keywords in AUTO_TAGS.items():
        if any(kw in text_lower for kw in keywords):
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Individual source scanners
# ---------------------------------------------------------------------------


async def scan_arxiv(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan arXiv for new papers in configured categories."""
    findings: list[dict[str, Any]] = []
    categories = "+OR+".join(f"cat:{cat}" for cat in config.categories)
    params: dict[str, str | int] = {
        "search_query": categories,
        "start": 0,
        "max_results": MAX_RESULTS_PER_SOURCE,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }

    try:
        resp = await client.get(config.api_url, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        findings = _parse_arxiv_response(resp.text, config)
    except Exception:
        logger.exception("Failed to scan arXiv")

    return findings


def _parse_arxiv_response(xml_text: str, config: SourceConfig) -> list[dict[str, Any]]:
    """Parse arXiv Atom feed response into findings."""
    import xml.etree.ElementTree as ET

    findings: list[dict[str, Any]] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.error("Failed to parse arXiv XML response")
        return findings

    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        summary_el = entry.find("atom:summary", ns)
        link_el = entry.find("atom:id", ns)
        published_el = entry.find("atom:published", ns)

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        summary = (
            summary_el.text.strip()
            if summary_el is not None and summary_el.text
            else ""
        )
        source_url = (
            link_el.text.strip() if link_el is not None and link_el.text else ""
        )

        if not title:
            continue

        # Extract authors
        authors = []
        for author_el in entry.findall("atom:author", ns):
            name_el = author_el.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        # Extract categories
        categories = []
        for cat_el in entry.findall("atom:category", ns):
            term = cat_el.get("term", "")
            if term:
                categories.append(term)

        raw_data = {
            "authors": authors,
            "categories": categories,
            "published": published_el.text if published_el is not None else None,
        }

        relevance = compute_relevance_score(title, summary, config, raw_data)
        tags = compute_tags(title, summary)
        tags.append("paper")

        findings.append(
            {
                "source": "arxiv",
                "source_url": source_url,
                "title": title,
                "summary": summary[:2000],  # Truncate long abstracts
                "relevance_score": relevance,
                "tags": tags,
                "raw_content_json": raw_data,
            }
        )

    return findings


async def scan_huggingface(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan HuggingFace for new/trending models and datasets."""
    findings = []

    try:
        # Trending models
        resp = await client.get(
            f"{config.api_url}/models",
            params={"sort": "trending", "limit": MAX_RESULTS_PER_SOURCE},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        models = resp.json()

        for model in models[:MAX_RESULTS_PER_SOURCE]:
            model_id = model.get("modelId", model.get("id", ""))
            title = f"HuggingFace Model: {model_id}"
            summary = (
                model.get("description", "")
                or f"Pipeline: {model.get('pipeline_tag', 'unknown')}"
            )
            source_url = f"https://huggingface.co/{model_id}"

            raw_data = {
                "model_id": model_id,
                "pipeline_tag": model.get("pipeline_tag"),
                "downloads": model.get("downloads", 0),
                "likes": model.get("likes", 0),
                "tags": model.get("tags", []),
            }

            relevance = compute_relevance_score(title, summary, config, raw_data)
            tags = compute_tags(title, summary)
            tags.append("model")

            findings.append(
                {
                    "source": "huggingface",
                    "source_url": source_url,
                    "title": title,
                    "summary": summary[:2000],
                    "relevance_score": relevance,
                    "tags": tags,
                    "raw_content_json": raw_data,
                }
            )
    except Exception:
        logger.exception("Failed to scan HuggingFace")

    return findings


async def scan_github(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan GitHub for trending ML/AI repos and releases of key projects."""
    findings = []

    try:
        # Search for recently updated ML repos (last 7 days)
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        resp = await client.get(
            f"{config.api_url}/search/repositories",
            params={
                "q": f"topic:machine-learning stars:>100 pushed:>{week_ago}",
                "sort": "updated",
                "per_page": MAX_RESULTS_PER_SOURCE,
            },
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        for repo in data.get("items", [])[:MAX_RESULTS_PER_SOURCE]:
            title = f"GitHub: {repo['full_name']}"
            summary = repo.get("description", "") or "No description"
            source_url = repo.get("html_url", "")

            raw_data = {
                "full_name": repo.get("full_name"),
                "stargazers_count": repo.get("stargazers_count", 0),
                "forks_count": repo.get("forks_count", 0),
                "language": repo.get("language"),
                "topics": repo.get("topics", []),
                "updated_at": repo.get("updated_at"),
            }

            relevance = compute_relevance_score(title, summary, config, raw_data)
            tags = compute_tags(title, summary)
            tags.append("repo")

            findings.append(
                {
                    "source": "github",
                    "source_url": source_url,
                    "title": title,
                    "summary": summary[:2000],
                    "relevance_score": relevance,
                    "tags": tags,
                    "raw_content_json": raw_data,
                }
            )

        # Check releases from key projects
        for project in config.keywords[:5]:  # Top 5 key projects
            try:
                resp = await client.get(
                    f"{config.api_url}/search/repositories",
                    params={"q": f"{project} in:name", "per_page": 1},
                    timeout=HTTP_TIMEOUT,
                )
                resp.raise_for_status()
                repos = resp.json().get("items", [])
                if not repos:
                    continue

                repo_name = repos[0]["full_name"]
                rel_resp = await client.get(
                    f"{config.api_url}/repos/{repo_name}/releases",
                    params={"per_page": 3},
                    timeout=HTTP_TIMEOUT,
                )
                rel_resp.raise_for_status()

                for release in rel_resp.json()[:3]:
                    title = f"Release: {repo_name} {release.get('tag_name', '')}"
                    summary = release.get("body", "")[:500] or "New release"
                    source_url = release.get("html_url", "")

                    raw_data = {
                        "repo": repo_name,
                        "tag_name": release.get("tag_name"),
                        "published_at": release.get("published_at"),
                        "prerelease": release.get("prerelease", False),
                    }

                    relevance = compute_relevance_score(
                        title, summary, config, raw_data
                    )
                    tags = compute_tags(title, summary)
                    tags.append("release")

                    findings.append(
                        {
                            "source": "github",
                            "source_url": source_url,
                            "title": title,
                            "summary": summary[:2000],
                            "relevance_score": relevance,
                            "tags": tags,
                            "raw_content_json": raw_data,
                        }
                    )
            except Exception:
                logger.warning("Failed to check releases for %s", project)

    except Exception:
        logger.exception("Failed to scan GitHub")

    return findings


async def scan_hackernews(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan Hacker News for AI/ML stories above score threshold."""
    findings = []

    try:
        # Get top stories
        resp = await client.get(
            f"{config.api_url}/topstories.json",
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        story_ids = resp.json()[:100]  # Check top 100

        for story_id in story_ids:
            try:
                item_resp = await client.get(
                    f"{config.api_url}/item/{story_id}.json",
                    timeout=HTTP_TIMEOUT,
                )
                item_resp.raise_for_status()
                item = item_resp.json()

                if not item or item.get("type") != "story":
                    continue

                score = item.get("score", 0)
                if score < config.score_threshold:
                    continue

                title = item.get("title", "")
                # Check if AI/ML related
                title_lower = title.lower()
                is_ml_related = any(kw.lower() in title_lower for kw in config.keywords)
                if not is_ml_related:
                    continue

                source_url = item.get(
                    "url", f"https://news.ycombinator.com/item?id={story_id}"
                )
                summary = f"Score: {score}, Comments: {item.get('descendants', 0)}"

                raw_data = {
                    "score": score,
                    "descendants": item.get("descendants", 0),
                    "by": item.get("by"),
                    "time": item.get("time"),
                }

                relevance = compute_relevance_score(title, summary, config, raw_data)
                tags = compute_tags(title, summary)
                tags.append("news")

                findings.append(
                    {
                        "source": "hackernews",
                        "source_url": source_url,
                        "title": title,
                        "summary": summary,
                        "relevance_score": relevance,
                        "tags": tags,
                        "raw_content_json": raw_data,
                    }
                )
            except Exception:
                logger.warning("Failed to fetch HN item %s", story_id)
                continue

    except Exception:
        logger.exception("Failed to scan Hacker News")

    return findings


async def scan_reddit(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan Reddit ML subreddits for top posts."""
    findings = []

    headers = {"User-Agent": "superplane-scanner/1.0"}

    for subreddit in config.categories:
        try:
            resp = await client.get(
                f"{config.api_url}/{subreddit}/hot.json",
                params={"limit": MAX_RESULTS_PER_SOURCE},
                headers=headers,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            for post in data.get("data", {}).get("children", []):
                post_data = post.get("data", {})
                score = post_data.get("score", 0)
                if score < config.score_threshold:
                    continue

                title = post_data.get("title", "")
                summary = post_data.get("selftext", "")[:500] or f"Score: {score}"
                source_url = f"https://reddit.com{post_data.get('permalink', '')}"

                raw_data = {
                    "score": score,
                    "num_comments": post_data.get("num_comments", 0),
                    "subreddit": post_data.get("subreddit"),
                    "author": post_data.get("author"),
                    "created_utc": post_data.get("created_utc"),
                    "url": post_data.get("url"),
                }

                relevance = compute_relevance_score(title, summary, config, raw_data)
                tags = compute_tags(title, summary)
                tags.append("discussion")

                findings.append(
                    {
                        "source": "reddit",
                        "source_url": source_url,
                        "title": title,
                        "summary": summary[:2000],
                        "relevance_score": relevance,
                        "tags": tags,
                        "raw_content_json": raw_data,
                    }
                )
        except Exception:
            logger.exception("Failed to scan %s", subreddit)

    return findings


async def scan_twitter(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan Twitter/X for ML-related posts.

    Note: Requires Twitter API v2 Bearer token. Returns empty list
    if credentials are not configured.
    """
    # Twitter API requires authentication; return empty for now
    # until API credentials are configured in settings
    logger.info("Twitter scanner: API credentials required. Skipping.")
    return []


async def scan_aws_whatsnew(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan AWS What's New RSS feed for GPU/ML relevant announcements."""
    findings = []

    try:
        resp = await client.get(config.api_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        findings = _parse_rss_feed(resp.text, "aws_whatsnew", config)
    except Exception:
        logger.exception("Failed to scan AWS What's New")

    return findings


async def scan_nvidia_blog(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan NVIDIA Blog RSS feed for GPU/AI announcements."""
    findings = []

    try:
        resp = await client.get(config.api_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        findings = _parse_rss_feed(resp.text, "nvidia_blog", config)
    except Exception:
        logger.exception("Failed to scan NVIDIA Blog")

    return findings


async def scan_competitor_blogs(
    client: httpx.AsyncClient,
    config: SourceConfig,
) -> list[dict[str, Any]]:
    """Scan competitor blogs for new announcements."""
    findings = []

    for competitor, url in COMPETITOR_URLS.items():
        try:
            resp = await client.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            # Basic HTML title extraction (real implementation would use
            # proper HTML parsing or RSS feeds from competitors)
            title = f"Competitor update: {competitor}"
            summary = f"New content detected on {competitor} blog"
            source_url = url

            raw_data = {
                "competitor": competitor,
                "status_code": resp.status_code,
                "content_length": len(resp.text),
            }

            relevance = compute_relevance_score(title, summary, config, raw_data)
            tags = compute_tags(title, summary)
            tags.append("competitor")

            findings.append(
                {
                    "source": "competitor_blog",
                    "source_url": source_url,
                    "title": title,
                    "summary": summary,
                    "relevance_score": relevance,
                    "tags": tags,
                    "raw_content_json": raw_data,
                }
            )
        except Exception:
            logger.warning("Failed to scan competitor blog: %s", competitor)

    return findings


def _parse_rss_feed(
    xml_text: str, source_name: str, config: SourceConfig
) -> list[dict[str, Any]]:
    """Parse an RSS feed into findings."""
    import xml.etree.ElementTree as ET

    findings: list[dict[str, Any]] = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.error("Failed to parse RSS XML for %s", source_name)
        return findings

    # Handle both RSS 2.0 and Atom formats
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for item in items[:MAX_RESULTS_PER_SOURCE]:
        title_el = item.find("title")
        if title_el is None:
            title_el = item.find("{http://www.w3.org/2005/Atom}title")
        desc_el = item.find("description")
        if desc_el is None:
            desc_el = item.find("{http://www.w3.org/2005/Atom}summary")
        link_el = item.find("link")
        if link_el is None:
            link_el = item.find("{http://www.w3.org/2005/Atom}link")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        summary = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
        if link_el is not None:
            source_url = (
                link_el.text.strip() if link_el.text else link_el.get("href", "")
            )
        else:
            source_url = ""

        if not title:
            continue

        # Check relevance to ML/GPU topics
        text_lower = f"{title} {summary}".lower()
        is_relevant = any(kw.lower() in text_lower for kw in config.keywords)
        if not is_relevant:
            continue

        raw_data = {"title": title, "description": summary[:1000]}

        relevance = compute_relevance_score(title, summary, config, raw_data)
        tags = compute_tags(title, summary)

        findings.append(
            {
                "source": source_name,
                "source_url": source_url,
                "title": title,
                "summary": summary[:2000],
                "relevance_score": relevance,
                "tags": tags,
                "raw_content_json": raw_data,
            }
        )

    return findings


# ---------------------------------------------------------------------------
# Scanner registry — maps source name to scan function
# ---------------------------------------------------------------------------

SCANNER_REGISTRY: dict[str, Any] = {
    "arxiv": scan_arxiv,
    "huggingface": scan_huggingface,
    "github": scan_github,
    "twitter": scan_twitter,
    "reddit": scan_reddit,
    "hackernews": scan_hackernews,
    "aws_whatsnew": scan_aws_whatsnew,
    "nvidia_blog": scan_nvidia_blog,
    "competitor_blog": scan_competitor_blogs,
}


# ---------------------------------------------------------------------------
# Main scanner orchestrator
# ---------------------------------------------------------------------------


async def run_scan(
    session: AsyncSession,
    sources: list[str] | None = None,
    workspace_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Run the external data scanner for specified sources.

    Args:
        session: Async database session.
        sources: List of source names to scan. If None, scan all sources.
        workspace_id: Optional workspace to associate findings with.

    Returns:
        Summary dict with counts and high-relevance findings.
    """
    if sources is None:
        sources = list(ALL_SOURCES.keys())
    else:
        # Validate source names
        sources = [s for s in sources if s in VALID_SOURCES]

    now = datetime.now(timezone.utc)
    all_findings: list[dict[str, Any]] = []
    sources_scanned: list[str] = []

    async with httpx.AsyncClient() as client:
        for source_name in sources:
            config = ALL_SOURCES.get(source_name)
            scanner_fn = SCANNER_REGISTRY.get(source_name)
            if not config or not scanner_fn:
                logger.warning("Unknown source: %s", source_name)
                continue

            logger.info("Scanning source: %s", source_name)
            try:
                findings = await scanner_fn(client, config)
                all_findings.extend(findings)
                sources_scanned.append(source_name)
                logger.info("Source %s: found %d items", source_name, len(findings))
            except Exception:
                logger.exception("Scanner failed for source: %s", source_name)

    # Store findings in database
    high_relevance_count = 0
    for finding_data in all_findings:
        finding = ResearchFinding(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            source=finding_data["source"],
            source_url=finding_data["source_url"],
            title=finding_data["title"],
            summary=finding_data.get("summary"),
            relevance_score=finding_data["relevance_score"],
            tags=finding_data.get("tags"),
            raw_content_json=finding_data.get("raw_content_json"),
            scanned_at=now,
        )
        session.add(finding)

        if finding.relevance_score > HIGH_RELEVANCE_THRESHOLD:
            high_relevance_count += 1

    await session.commit()

    logger.info(
        "Scan complete: %d findings (%d high-relevance) from %d sources",
        len(all_findings),
        high_relevance_count,
        len(sources_scanned),
    )

    return {
        "status": "completed",
        "sources_scanned": sources_scanned,
        "findings_count": len(all_findings),
        "high_relevance_count": high_relevance_count,
    }


async def get_scanner_stats(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """Get aggregate statistics for ONE organization.

    Issue #5682 (A02): ``org_id`` is required and has no ``None`` default. It
    previously defaulted to ``None``, and ``owned()`` returned the query
    unfiltered for that value — so the shipped configuration reported counts
    across every tenant. Aggregates are as disclosive as rows here: the totals and
    the per-source breakdown reveal how much research other organizations are
    doing and where they are sourcing it.

    Removing the default is the enforcement: a caller that forgets to pass a
    tenant now fails at the call site instead of silently receiving every
    tenant's numbers.
    """

    def owned(query):
        return query.join(
            Workspace, Workspace.id == ResearchFinding.workspace_id
        ).where(Workspace.org_id == org_id)

    # Total counts by relevance tier
    total_q = await session.execute(owned(select(func.count(ResearchFinding.id))))
    total = total_q.scalar() or 0

    high_q = await session.execute(
        owned(select(func.count(ResearchFinding.id))).where(
            ResearchFinding.relevance_score > HIGH_RELEVANCE_THRESHOLD
        )
    )
    high_count = high_q.scalar() or 0

    low_q = await session.execute(
        owned(select(func.count(ResearchFinding.id))).where(
            ResearchFinding.relevance_score < LOW_RELEVANCE_THRESHOLD
        )
    )
    low_count = low_q.scalar() or 0

    medium_count = total - high_count - low_count

    # Counts by source
    source_q = await session.execute(
        owned(
            select(
                ResearchFinding.source,
                func.count(ResearchFinding.id),
            )
        ).group_by(ResearchFinding.source)
    )
    findings_by_source = {row[0]: row[1] for row in source_q.all()}

    # Last scan time
    last_scan_q = await session.execute(
        owned(select(func.max(ResearchFinding.scanned_at)))
    )
    last_scan = last_scan_q.scalar()

    return {
        "total_findings": total,
        "high_relevance_count": high_count,
        "medium_relevance_count": medium_count,
        "low_relevance_count": low_count,
        "findings_by_source": findings_by_source,
        "last_scan_at": last_scan,
    }
