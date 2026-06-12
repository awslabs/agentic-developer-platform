#!/usr/bin/env python3
"""Generate cross-repo curricula and comparison cards from Neptune communities.

Produces:
  /platform-data/learning/curricula/{topic-slug}-curriculum.json
  /platform-data/learning/curricula/{topic-slug}-comparisons.json

Uses Neptune graph community detection to find topic clusters, then generates
multi-repo curricula spanning related repositories.

Usage:
  python generate-curricula.py
  python generate-curricula.py --repos-file /config/repos.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("curricula")

# Configuration (centralized via config.py)
from config import settings

LEARNING_DIR = settings.learning_dir
CODE_INDEX_DIR = settings.code_index_dir
LLM_MODEL = settings.model_learning
LLM_BASE_URL = settings.llm_base_url
REPOS_FILE = settings.repos_file
STATE_DIR = settings.state_dir


def safe_name(repo: str) -> str:
    return repo.replace("/", "-")


def slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def call_llm(prompt: str, max_tokens: int = 4000) -> str | None:
    """Call LLM via LiteLLM proxy."""
    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=120,
        )
        if resp.status_code < 300:
            return resp.json()["choices"][0]["message"]["content"]
        log.warning("LLM call failed: HTTP %d", resp.status_code)
        return None
    except Exception as e:
        log.warning("LLM call error: %s", e)
        return None


def extract_json_from_response(text: str) -> Any:
    """Extract JSON from LLM response."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


def discover_topic_clusters(repos: list[str]) -> list[dict[str, Any]]:
    """Discover topic clusters from concept maps and code indexes.

    When Neptune is available, uses community detection. Otherwise, clusters
    repos by shared external dependencies and concept keywords.
    """
    # Collect concept maps and dependencies per repo
    repo_data: dict[str, dict[str, Any]] = {}
    for repo in repos:
        data: dict[str, Any] = {"concepts": [], "deps": []}
        # Load concept map
        cm_path = os.path.join(LEARNING_DIR, safe_name(repo), "concept-map.json")
        if os.path.isfile(cm_path):
            try:
                with open(cm_path) as f:
                    concepts = json.load(f)
                data["concepts"] = [
                    c.get("concept", "") for c in concepts if isinstance(c, dict)
                ]
            except (json.JSONDecodeError, OSError):
                pass

        # Load code-index dependencies
        ci_path = os.path.join(CODE_INDEX_DIR, f"{safe_name(repo)}.json")
        if os.path.isfile(ci_path):
            try:
                with open(ci_path) as f:
                    ci = json.load(f)
                data["deps"] = ci.get("dependencies", {}).get("external", [])
            except (json.JSONDecodeError, OSError):
                pass

        if data["concepts"] or data["deps"]:
            repo_data[repo] = data

    if not repo_data:
        return []

    # Use LLM to cluster repos into topic groups
    summaries = []
    for repo, data in list(repo_data.items())[:50]:
        concepts = ", ".join(data["concepts"][:10])
        deps = ", ".join(data["deps"][:10])
        summaries.append(f"- {repo}: concepts=[{concepts}], deps=[{deps}]")

    prompt = f"""Given these repositories with their concepts and dependencies, identify 3-8 topic
clusters (groups of related repos that could form a curriculum).

Repositories:
{chr(10).join(summaries)}

Return a JSON array. Each cluster should have:
- "topic": string (human-readable topic name)
- "slug": string (URL-safe identifier, lowercase, hyphens)
- "description": string (1-2 sentences)
- "repos": array of repo names (org/repo format)
- "key_concepts": array of shared concepts

Return ONLY valid JSON, no markdown."""

    response = call_llm(prompt, max_tokens=3000)
    result = extract_json_from_response(response)
    return result if isinstance(result, list) else []


def generate_curriculum(cluster: dict[str, Any]) -> dict[str, Any] | None:
    """Generate a cross-repo curriculum for a topic cluster."""
    topic = cluster.get("topic", "")
    repos = cluster.get("repos", [])
    concepts = cluster.get("key_concepts", [])

    if len(repos) < 2:
        return None

    # Gather concept maps for each repo in the cluster
    repo_concepts = {}
    for repo in repos:
        cm_path = os.path.join(LEARNING_DIR, safe_name(repo), "concept-map.json")
        if os.path.isfile(cm_path):
            try:
                with open(cm_path) as f:
                    repo_concepts[repo] = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    prompt = f"""Create a multi-repo learning curriculum for the topic "{topic}".

Repos in this cluster: {json.dumps(repos)}
Key concepts: {json.dumps(concepts)}
Description: {cluster.get('description', '')}

Repo concept maps:
{json.dumps({r: [c.get('concept', '') for c in cm[:5]] for r, cm in repo_concepts.items()}, indent=2)}

Return a JSON object with:
- "topic": string
- "description": string (2-3 sentences)
- "difficulty": "beginner"|"intermediate"|"advanced"
- "estimated_hours": integer
- "modules": array of modules, each with:
  - "title": string
  - "description": string
  - "repos": array of repo names
  - "concepts": array of concepts covered
  - "order": integer
- "prerequisites": array of strings (what you should know first)

Return ONLY valid JSON, no markdown."""

    response = call_llm(prompt, max_tokens=4000)
    result = extract_json_from_response(response)
    if isinstance(result, dict):
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["source_repos"] = repos
    return result


def generate_comparisons(cluster: dict[str, Any]) -> dict[str, Any] | None:
    """Generate side-by-side comparison cards for repos in a cluster."""
    topic = cluster.get("topic", "")
    repos = cluster.get("repos", [])

    if len(repos) < 2:
        return None

    # Gather info per repo
    repo_info = {}
    for repo in repos:
        info: dict[str, Any] = {}
        ci_path = os.path.join(CODE_INDEX_DIR, f"{safe_name(repo)}.json")
        if os.path.isfile(ci_path):
            try:
                with open(ci_path) as f:
                    ci = json.load(f)
                info["languages"] = ci.get("language_stats", {})
                info["deps"] = ci.get("dependencies", {}).get("external", [])[:15]
            except (json.JSONDecodeError, OSError):
                pass
        cm_path = os.path.join(LEARNING_DIR, safe_name(repo), "concept-map.json")
        if os.path.isfile(cm_path):
            try:
                with open(cm_path) as f:
                    cm = json.load(f)
                info["concepts"] = [c.get("concept", "") for c in cm[:10] if isinstance(c, dict)]
            except (json.JSONDecodeError, OSError):
                pass
        repo_info[repo] = info

    prompt = f"""Create comparison cards for these repos in the "{topic}" topic cluster.

Repos and their details:
{json.dumps(repo_info, indent=2)}

Return a JSON object with:
- "topic": string
- "comparisons": array of comparison cards, each with:
  - "title": string (e.g., "CrewAI vs LangGraph")
  - "repos": array of 2 repo names being compared
  - "dimensions": array of comparison dimensions, each with:
    - "dimension": string (e.g., "architecture", "ease of use")
    - "values": object mapping repo name to its description for this dimension
  - "when_to_use": object mapping repo name to when it's the better choice
  - "summary": string (1-2 sentence summary)

Return ONLY valid JSON, no markdown."""

    response = call_llm(prompt, max_tokens=4000)
    result = extract_json_from_response(response)
    if isinstance(result, dict):
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
    return result


def parse_repos_file(path: str) -> list[str]:
    repos = []
    if not os.path.exists(path):
        return repos
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                repos.append(line)
    return repos


def main():
    parser = argparse.ArgumentParser(description="Generate cross-repo curricula")
    parser.add_argument("--repos-file", default=REPOS_FILE)
    parser.add_argument("--max-clusters", type=int, default=8)
    args = parser.parse_args()

    repos = parse_repos_file(args.repos_file)
    if not repos:
        log.error("No repos found in %s", args.repos_file)
        sys.exit(1)

    curricula_dir = os.path.join(LEARNING_DIR, "curricula")
    os.makedirs(curricula_dir, exist_ok=True)

    # Step 1: Discover topic clusters
    log.info("Discovering topic clusters from %d repos...", len(repos))
    clusters = discover_topic_clusters(repos)
    if not clusters:
        log.warning("No topic clusters discovered — need concept maps first")
        sys.exit(0)

    log.info("Found %d topic clusters", len(clusters))
    for c in clusters:
        log.info("  %s: %d repos — %s", c.get("slug", "?"), len(c.get("repos", [])), c.get("topic", ""))

    # Step 2: Generate curricula for each cluster
    for cluster in clusters[:args.max_clusters]:
        slug = cluster.get("slug", slugify(cluster.get("topic", "unknown")))
        log.info("Generating curriculum for '%s'...", cluster.get("topic", slug))

        curriculum = generate_curriculum(cluster)
        if curriculum:
            with open(os.path.join(curricula_dir, f"{slug}-curriculum.json"), "w") as f:
                json.dump(curriculum, f, indent=2)
            log.info("  Curriculum saved: %s-curriculum.json", slug)

        comparisons = generate_comparisons(cluster)
        if comparisons:
            with open(os.path.join(curricula_dir, f"{slug}-comparisons.json"), "w") as f:
                json.dump(comparisons, f, indent=2)
            log.info("  Comparisons saved: %s-comparisons.json", slug)

    log.info("Curricula generation complete")


if __name__ == "__main__":
    main()
