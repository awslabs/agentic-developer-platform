#!/usr/bin/env python3
"""Generate topic index pages from LLM-discovered repo tags.

Instead of hardcoded topics, this script:
  1. Reads topic tags from repo-state.json (assigned during ingestion by refresh-repos.py)
  2. Asks the LLM to cluster related tags into topic groups
  3. Generates an index page for each cluster with 3+ repos
  4. Uploads index pages to viking://resources/meta/index-{slug}.md

Runs as the last step of the daily CronJob, after lint-wiki.py.

Usage:
  python generate-indexes.py
  python generate-indexes.py --force  # Regenerate all indexes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("generate-indexes")

# ---------------------------------------------------------------------------
# Configuration (centralized via config.py)
# ---------------------------------------------------------------------------

from config import settings

OV_URL = settings.ov_url
OV_KEY = settings.ov_key
STATE_DIR = settings.state_dir
REQUEST_TIMEOUT = settings.request_timeout

LLM_MODEL = settings.model_index
LLM_BASE_URL = settings.llm_base_url

# Minimum repos in a topic cluster to generate an index page
MIN_CLUSTER_SIZE = settings.min_cluster_size

# Maximum number of index pages to generate per run
MAX_INDEXES_PER_RUN = settings.max_indexes_per_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ov_headers(api_key: str) -> dict[str, str]:
    return {
        "X-API-Key": api_key,
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "default",
    }


def load_state(filename: str) -> dict[str, Any]:
    """Load state JSON from STATE_DIR."""
    path = os.path.join(STATE_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load %s: %s", path, e)
    return {}


def save_state(filename: str, state: dict[str, Any]):
    """Save state JSON to STATE_DIR."""
    path = os.path.join(STATE_DIR, filename)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    log.info("Saved state to %s", path)


def call_llm(prompt: str, max_tokens: int = 8192) -> str | None:
    """Call the LLM via LiteLLM proxy (OpenAI-compatible API)."""
    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=300,
        )
        if resp.status_code < 300:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        log.warning("LLM call returned HTTP %d: %s", resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        log.warning("LLM call failed: %s", e)
        return None


def fetch_existing_index(slug: str, headers: dict) -> str | None:
    """Fetch an existing index page from OpenViking."""
    target_uri = f"viking://resources/meta/index-{slug}.md"
    try:
        resp = requests.get(
            f"{OV_URL}/api/v1/content/read",
            headers=headers,
            params={"uri": target_uri},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 300:
            data = resp.json()
            content = data.get("result", "") if isinstance(data, dict) else str(data)
            if content and len(content) > 50:
                return content
        return None
    except Exception as e:
        log.warning("Failed to fetch index %s: %s", slug, e)
        return None


def search_openviking(query: str, headers: dict, limit: int = 20) -> list[dict]:
    """Search OpenViking for content related to a query."""
    try:
        resp = requests.post(
            f"{OV_URL}/api/v1/search/search",
            headers={**headers, "Content-Type": "application/json"},
            json={"query": query, "target_uri": "viking://resources/"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 300:
            data = resp.json()
            result = data.get("result", {})
            if isinstance(result, dict):
                resources = result.get("resources", [])
                return resources[:limit]
            return result[:limit] if isinstance(result, list) else []
        return []
    except Exception as e:
        log.warning("Search failed for '%s': %s", query, e)
        return []


def upload_index(slug: str, content: str, headers: dict) -> bool:
    """Upload an index page to OpenViking."""
    target_uri = f"viking://resources/meta/index-{slug}.md"
    try:
        resp = requests.post(
            f"{OV_URL}/api/v1/content/write",
            headers={**headers, "Content-Type": "application/json"},
            json={"uri": target_uri, "content": content, "wait": False},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 300:
            log.info("Uploaded index page: %s", target_uri)
            return True
        log.warning("content/write returned %d for %s", resp.status_code, target_uri)
    except Exception as e:
        log.warning("Upload failed for %s: %s", target_uri, e)

    # Fallback: temp_upload
    try:
        files = {"file": (f"index-{slug}.md", content.encode("utf-8"), "text/markdown")}
        resp = requests.post(
            f"{OV_URL}/api/v1/resources/temp_upload",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            files=files,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 300:
            return False
        temp_id = resp.json().get("result", {}).get("temp_file_id")
        if not temp_id:
            return False
        resp = requests.post(
            f"{OV_URL}/api/v1/resources",
            headers={**headers, "Content-Type": "application/json"},
            json={"temp_file_id": temp_id, "to": target_uri, "wait": True, "timeout": REQUEST_TIMEOUT},
            timeout=REQUEST_TIMEOUT + 10,
        )
        return resp.status_code < 300
    except Exception as e:
        log.warning("Upload fallback failed for %s: %s", target_uri, e)
        return False


# ---------------------------------------------------------------------------
# Topic clustering and index generation
# ---------------------------------------------------------------------------


def collect_all_tags(repo_state: dict[str, Any]) -> dict[str, list[str]]:
    """Collect all topic tags across all repos.

    Returns {tag: [repo1, repo2, ...]}
    """
    all_tags: dict[str, list[str]] = {}
    for repo, state in repo_state.items():
        for tag in state.get("topics", []):
            all_tags.setdefault(tag, []).append(repo)
    return all_tags


def discover_topic_clusters(
    all_tags: dict[str, list[str]], min_size: int = 3
) -> list[dict[str, Any]]:
    """Use LLM to cluster related tags into topic groups."""
    if not all_tags:
        log.info("No topic tags found — skipping cluster discovery")
        return []

    # Build tag frequency table
    tag_counts = {
        tag: len(repos)
        for tag, repos in sorted(all_tags.items(), key=lambda x: -len(x[1]))
    }

    prompt = f"""Here are all topic tags discovered across repositories:

{json.dumps(tag_counts, indent=2)}

1. Group related tags into topic clusters (e.g., "rag", "retrieval", "knowledge-base" -> "RAG & Knowledge Systems")
2. For each cluster with {min_size}+ repos, generate a topic name and slug
3. Return ONLY a JSON array: [{{"slug": "rag-knowledge", "name": "RAG & Knowledge Systems", "tags": ["rag", "retrieval", "knowledge-base"], "repo_count": 12}}]

Only include clusters with {min_size}+ repos. Order by repo count descending. Maximum 15 clusters."""

    result = call_llm(prompt, max_tokens=4096)
    if not result:
        log.warning("LLM failed to discover topic clusters")
        return []

    try:
        # Extract JSON array from response
        match = re.search(r'\[.*\]', result, re.DOTALL)
        if match:
            clusters = json.loads(match.group())
            if isinstance(clusters, list):
                return clusters
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse topic clusters: %s", e)

    return []


def generate_topic_index(
    cluster: dict[str, Any],
    repos_in_cluster: list[str],
    repo_state: dict[str, Any],
    headers: dict,
) -> bool:
    """Generate and upload a topic index page for a cluster."""
    slug = cluster.get("slug", "unknown")
    name = cluster.get("name", slug)
    tags = cluster.get("tags", [])

    log.info("Generating index for topic '%s' (%d repos)", name, len(repos_in_cluster))

    # Search OpenViking for related content
    search_query = " ".join(tags[:5]) + " " + name
    search_results = search_openviking(search_query, headers)

    # Format search results for LLM
    results_text = ""
    for r in search_results[:15]:
        uri = r.get("uri", "")
        abstract = r.get("abstract", r.get("content", ""))[:200]
        results_text += f"- {uri}: {abstract}\n"

    # Get existing index page (for incremental update)
    existing = fetch_existing_index(slug, headers)

    # Build repo info
    repo_info = ""
    for repo in repos_in_cluster[:25]:
        state = repo_state.get(repo, {})
        topics = ", ".join(state.get("topics", []))
        repo_info += f"- **{repo}**: tags=[{topics}]\n"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""Generate a topic index page for: {name}

Topic tags: {', '.join(tags)}

Repos in this topic ({len(repos_in_cluster)} total):
{repo_info}

Related content from search:
{results_text if results_text else "(no search results available)"}

{f"Previous version of this index (update it with any new repos or changes):\n{existing[:5000]}" if existing else "This is a new index page."}

Format as markdown:
- Title and description
- Table of repos with: name, what it does (based on tags/content), key patterns
- Comparison section: how do these repos differ in approach?
- Cross-references to related topics
- {"'What's new' section highlighting changes since last update" if existing else ""}
- Date: {today}

Keep it concise and useful for a developer choosing between these tools."""

    index_content = call_llm(prompt)
    if not index_content:
        log.warning("LLM failed to generate index for %s", slug)
        return False

    return upload_index(slug, index_content, headers)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Generate topic index pages")
    parser.add_argument("--force", action="store_true", help="Regenerate all indexes")
    args = parser.parse_args()

    if not OV_KEY:
        log.error("No OpenViking API key. Set OPENVIKING_ROOT_KEY env var.")
        sys.exit(1)

    headers = ov_headers(OV_KEY)

    # Load repo state (contains topic tags from refresh-repos.py)
    repo_state = load_state("repo-state.json")
    if not repo_state:
        log.warning("No repo-state.json found — nothing to index")
        return 0

    # Load index state (tracks which indexes have been generated)
    index_state = load_state("index-state.json")

    # Step 1: Collect all tags
    all_tags = collect_all_tags(repo_state)
    log.info("Found %d unique topic tags across %d repos", len(all_tags), len(repo_state))

    if not all_tags:
        log.info("No topic tags found — skipping index generation")
        return 0

    # Step 2: Ask LLM to cluster tags
    clusters = discover_topic_clusters(all_tags, MIN_CLUSTER_SIZE)
    log.info("Discovered %d topic clusters", len(clusters))

    if not clusters:
        log.info("No topic clusters discovered — skipping index generation")
        return 0

    # Step 3: Generate index pages for each cluster
    indexes_generated = 0
    for cluster in clusters[:MAX_INDEXES_PER_RUN]:
        slug = cluster.get("slug", "unknown")
        tags = cluster.get("tags", [])

        # Find repos in this cluster
        repos_in_cluster = list(set(
            repo
            for repo, state in repo_state.items()
            if any(t in state.get("topics", []) for t in tags)
        ))

        if len(repos_in_cluster) < MIN_CLUSTER_SIZE:
            log.info("Skipping cluster '%s' — only %d repos", slug, len(repos_in_cluster))
            continue

        # Check if we need to regenerate (any underlying repo changed?)
        prev_index = index_state.get(slug, {})
        prev_repos = set(prev_index.get("repos", []))
        current_repos = set(repos_in_cluster)

        repos_changed = any(
            repo_state.get(r, {}).get("last_sha") != repo_state.get(r, {}).get("_prev_index_sha")
            for r in repos_in_cluster
        )

        if not args.force and prev_repos == current_repos and not repos_changed and prev_index.get("generated"):
            log.info("SKIP index '%s' — no changes", slug)
            continue

        # Generate index
        if generate_topic_index(cluster, repos_in_cluster, repo_state, headers):
            indexes_generated += 1
            index_state[slug] = {
                "name": cluster.get("name", slug),
                "tags": tags,
                "repos": repos_in_cluster,
                "generated": datetime.now(timezone.utc).isoformat(),
                "repo_count": len(repos_in_cluster),
            }
            log.info("Generated index for '%s' (%d repos)", slug, len(repos_in_cluster))

    # Save index state
    save_state("index-state.json", index_state)

    log.info(
        "Index generation complete: %d indexes generated, %d total clusters",
        indexes_generated,
        len(clusters),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
