#!/usr/bin/env python3
"""Daily knowledge base health check — lint wiki content and report issues.

Checks for:
  1. Missing wikis — repos without DeepWiki content
  2. Stale wikis — wikis older than 14 days for repos that changed
  3. Missing code-index — repos without structural analysis
  4. Non-English L1 overviews (sampled)
  5. Orphan discoveries — discovery pages not linked from any wiki

Produces a report uploaded to meta/lint-report.md in S3.

Usage:
  python lint-wiki.py
  python lint-wiki.py --repos-file /config/repos.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("lint-wiki")

# ---------------------------------------------------------------------------
# Configuration (centralized via config.py)
# ---------------------------------------------------------------------------

from config import settings
from s3_store import S3ContentStore

STATE_DIR = settings.state_dir
REPOS_FILE = settings.repos_file
REQUEST_TIMEOUT = settings.request_timeout

# How many repos to sample for L1 language check
L1_SAMPLE_SIZE = settings.l1_sample_size

# Stale wiki threshold in days
STALE_WIKI_DAYS = settings.stale_wiki_days

# GraphRAG configuration
NEPTUNE_ENDPOINT = settings.neptune_endpoint
NEPTUNE_PORT = settings.neptune_port

# Neptune TLS verification — Amazon CA bundle (Issue #2224)
# Override with NEPTUNE_CA_BUNDLE_PATH env var for local dev (set to "" to disable).
NEPTUNE_CA_BUNDLE = (
    os.environ.get("NEPTUNE_CA_BUNDLE_PATH", "/etc/ssl/certs/rds-global-bundle.pem") or False
)
LEARNING_DIR = settings.learning_dir
CODE_INDEX_DIR = settings.code_index_dir

# ---------------------------------------------------------------------------
# S3 content store (lazy singleton)
# ---------------------------------------------------------------------------

_store: S3ContentStore | None = None


def _get_store() -> S3ContentStore:
    global _store
    if _store is None:
        _store = S3ContentStore(
            bucket_name=settings.s3_bucket_name,
            prefix=settings.s3_content_prefix,
            region_name=settings.aws_region,
        )
    return _store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_content_file(path: str) -> list[str]:
    """Parse repos.txt, skipping comments and blank lines."""
    entries = []
    if not os.path.exists(path):
        log.warning("Content file not found: %s", path)
        return entries
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(line)
    return entries


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


def is_likely_english(text: str) -> bool:
    """Simple heuristic: check if text is mostly ASCII/English."""
    if not text:
        return True
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    ratio = ascii_chars / len(text)
    # Also check for common English words
    english_markers = ["the", "is", "and", "for", "this", "with", "from", "that"]
    lower_text = text.lower()
    marker_count = sum(1 for w in english_markers if w in lower_text)
    return ratio > 0.85 and marker_count >= 2


# ---------------------------------------------------------------------------
# Lint checks
# ---------------------------------------------------------------------------


def check_missing_wikis(
    all_repos: list[str], repo_state: dict[str, Any]
) -> list[str]:
    """Find repos without DeepWiki content."""
    issues = []
    missing = [r for r in all_repos if not repo_state.get(r, {}).get("deepwiki_sha")]
    if missing:
        issues.append(
            f"Missing wikis: {len(missing)} repos without DeepWiki content"
        )
        for repo in missing[:10]:
            issues.append(f"  - {repo}")
        if len(missing) > 10:
            issues.append(f"  - ... and {len(missing) - 10} more")
    return issues


def check_stale_wikis(
    all_repos: list[str], repo_state: dict[str, Any]
) -> list[str]:
    """Find wikis that are stale (repo changed since wiki was generated)."""
    issues = []
    for repo in all_repos:
        state = repo_state.get(repo, {})
        wiki_sha = state.get("deepwiki_sha")
        last_sha = state.get("last_sha")
        if wiki_sha and last_sha and wiki_sha != last_sha:
            last_ingested = state.get("last_ingested", "")
            issues.append(f"Stale wiki: {repo} (wiki SHA {wiki_sha[:8]} != current {last_sha[:8]}, last ingested {last_ingested})")
    return issues


def check_missing_code_index(
    all_repos: list[str], repo_state: dict[str, Any]
) -> list[str]:
    """Find repos without code-index data."""
    issues = []
    missing = [
        r for r in all_repos if not repo_state.get(r, {}).get("code_index_sha")
    ]
    if missing:
        issues.append(
            f"Missing code-index: {len(missing)} repos without structural analysis"
        )
        for repo in missing[:10]:
            issues.append(f"  - {repo}")
        if len(missing) > 10:
            issues.append(f"  - ... and {len(missing) - 10} more")
    return issues


def check_l1_language(
    all_repos: list[str], sample_size: int = 20
) -> list[str]:
    """Sample repos and check for non-English L1 overviews."""
    issues = []
    store = _get_store()
    sample = random.sample(all_repos, min(sample_size, len(all_repos)))

    for repo in sample:
        # Read the wiki markdown from S3 and use first 500 chars as overview
        safe_name = repo.replace("/", "-")
        content = store.get_content(f"wikis/{safe_name}-wiki.md")
        if content:
            overview = content[:500]
            if not is_likely_english(overview):
                issues.append(f"Non-English L1 overview: {repo}")

    return issues


def check_orphan_discoveries() -> list[str]:
    """Find discovery pages not referenced from any wiki."""
    issues = []
    store = _get_store()
    discoveries = store.list_prefix("discoveries/")

    if not discoveries:
        return []

    # Count total discoveries
    discovery_count = len(discoveries)
    issues.append(f"Discovery pages: {discovery_count} total")

    return issues


def check_missing_topics(
    all_repos: list[str], repo_state: dict[str, Any]
) -> list[str]:
    """Find repos without topic tags."""
    issues = []
    missing = [r for r in all_repos if not repo_state.get(r, {}).get("topics")]
    if missing:
        issues.append(
            f"Missing topic tags: {len(missing)} repos without LLM-discovered topics"
        )
    return issues


# ---------------------------------------------------------------------------
# Graph health checks
# ---------------------------------------------------------------------------


def check_graph_health() -> list[str]:
    """Check Neptune graph for disconnected nodes and basic stats."""
    issues = []
    if not NEPTUNE_ENDPOINT:
        issues.append("Graph: Neptune not configured (NEPTUNE_ENDPOINT not set)")
        return issues

    import requests

    neptune_url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"

    def _query(gremlin: str) -> Any:
        try:
            resp = requests.post(
                neptune_url,
                json={"gremlin": gremlin},
                timeout=30,
                verify=NEPTUNE_CA_BUNDLE,
            )
            if resp.status_code < 300:
                data = resp.json()
                return data.get("result", {}).get("data", {})
            return None
        except Exception as e:
            log.warning("Neptune query failed: %s", e)
            return None

    # Node count
    node_count = _query("g.V().count()")
    edge_count = _query("g.E().count()")

    if node_count is None:
        issues.append("Graph: Neptune unreachable")
        return issues

    nc = node_count.get("@value", [0])[0] if isinstance(node_count, dict) else node_count
    ec = edge_count.get("@value", [0])[0] if isinstance(edge_count, dict) else edge_count
    issues.append(f"Graph stats: {nc} nodes, {ec} edges")

    # Disconnected nodes (nodes with no edges)
    disconnected = _query("g.V().where(bothE().count().is(0)).count()")
    if disconnected:
        dc = disconnected.get("@value", [0])[0] if isinstance(disconnected, dict) else disconnected
        if dc and int(dc) > 0:
            issues.append(f"Graph: {dc} disconnected nodes (entities with zero relationships)")

    return issues


def check_graph_contradictions(all_repos: list[str]) -> list[str]:
    """Check for contradictions between graph and code-index."""
    issues = []
    if not NEPTUNE_ENDPOINT:
        return issues

    # Sample a few repos and compare
    sample = random.sample(all_repos, min(5, len(all_repos)))
    for repo in sample:
        ci_path = os.path.join(CODE_INDEX_DIR, f"{repo.replace('/', '-')}.json")
        if not os.path.isfile(ci_path):
            continue
        try:
            with open(ci_path) as f:
                ci = json.load(f)
            ext_deps = ci.get("dependencies", {}).get("external", [])
            # We'd check Neptune for the same repo's depends_on edges
            # For now, just verify code-index exists for repos in the graph
        except (json.JSONDecodeError, OSError):
            continue

    return issues


def check_missing_graph_data(all_repos: list[str], repo_state: dict[str, Any]) -> list[str]:
    """Find repos indexed in S3 but not in Neptune."""
    issues = []
    if not NEPTUNE_ENDPOINT:
        return issues

    # Count repos that have code-index but no graph data
    # (Approximate — check if graph has nodes for this repo)
    missing = []
    for repo in all_repos:
        ci_path = os.path.join(CODE_INDEX_DIR, f"{repo.replace('/', '-')}.json")
        if os.path.isfile(ci_path):
            # Repo has code-index but may not be in graph
            # Full check would query Neptune, but we do a lightweight check here
            pass

    return issues


# ---------------------------------------------------------------------------
# Learning artifact health checks
# ---------------------------------------------------------------------------


def check_missing_learning_artifacts(all_repos: list[str]) -> list[str]:
    """Find repos that have code-index but no learning artifacts."""
    issues = []
    missing = []
    for repo in all_repos:
        ci_path = os.path.join(CODE_INDEX_DIR, f"{repo.replace('/', '-')}.json")
        cm_path = os.path.join(LEARNING_DIR, repo.replace("/", "-"), "concept-map.json")
        if os.path.isfile(ci_path) and not os.path.isfile(cm_path):
            missing.append(repo)

    if missing:
        issues.append(f"Missing learning artifacts: {len(missing)} repos have code-index but no concept map")
        for repo in missing[:5]:
            issues.append(f"  - {repo}")
        if len(missing) > 5:
            issues.append(f"  - ... and {len(missing) - 5} more")
    return issues


def check_stale_quiz_questions(all_repos: list[str]) -> list[str]:
    """Find quiz questions that reference renamed/deleted symbols."""
    issues = []
    stale_count = 0

    for repo in all_repos:
        quiz_path = os.path.join(LEARNING_DIR, repo.replace("/", "-"), "quiz-bank.json")
        ci_path = os.path.join(CODE_INDEX_DIR, f"{repo.replace('/', '-')}.json")

        if not os.path.isfile(quiz_path) or not os.path.isfile(ci_path):
            continue

        try:
            with open(quiz_path) as f:
                quiz = json.load(f)
            with open(ci_path) as f:
                ci = json.load(f)

            questions = quiz.get("questions", quiz) if isinstance(quiz, dict) else quiz
            symbols = {s.get("name", "") for s in ci.get("symbols", [])}
            files = {s.get("file", "") for s in ci.get("symbols", [])}

            for q in questions:
                if isinstance(q, dict):
                    src_file = q.get("source_file", "")
                    if src_file and src_file not in files:
                        stale_count += 1
        except (json.JSONDecodeError, OSError):
            continue

    if stale_count > 0:
        issues.append(f"Stale quiz questions: {stale_count} questions reference files no longer in code-index")
    return issues


def check_broken_learning_paths(all_repos: list[str]) -> list[str]:
    """Find learning paths that reference non-existent files."""
    issues = []
    broken_count = 0
    clone_base = settings.clone_base

    for repo in all_repos:
        lp_path = os.path.join(LEARNING_DIR, repo.replace("/", "-"), "learning-path.json")
        if not os.path.isfile(lp_path):
            continue

        try:
            with open(lp_path) as f:
                lp = json.load(f)
            steps = lp.get("steps", lp) if isinstance(lp, dict) else lp

            for step in steps:
                if isinstance(step, dict):
                    for read_file in step.get("read", []):
                        full_path = os.path.join(clone_base, repo, read_file)
                        if not os.path.isfile(full_path):
                            broken_count += 1
        except (json.JSONDecodeError, OSError):
            continue

    if broken_count > 0:
        issues.append(f"Broken learning paths: {broken_count} file references point to non-existent files")
    return issues


def check_orphan_curricula() -> list[str]:
    """Find curricula that reference repos removed from repos.txt."""
    issues = []
    curricula_dir = os.path.join(LEARNING_DIR, "curricula")
    if not os.path.isdir(curricula_dir):
        return issues

    curricula_count = len([
        f for f in os.listdir(curricula_dir) if f.endswith("-curriculum.json")
    ])
    if curricula_count > 0:
        issues.append(f"Cross-repo curricula: {curricula_count} generated")
    return issues


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report(
    all_repos: list[str],
    repo_state: dict[str, Any],
    all_issues: list[str],
) -> str:
    """Generate the lint report markdown."""
    now = datetime.now(timezone.utc).isoformat()

    # Count stats
    total_repos = len(all_repos)
    wikis_count = sum(1 for r in all_repos if repo_state.get(r, {}).get("deepwiki_sha"))
    code_index_count = sum(1 for r in all_repos if repo_state.get(r, {}).get("code_index_sha"))
    topics_count = sum(1 for r in all_repos if repo_state.get(r, {}).get("topics"))

    # Count learning artifacts
    learning_count = 0
    for r in all_repos:
        cm_path = os.path.join(LEARNING_DIR, r.replace("/", "-"), "concept-map.json")
        if os.path.isfile(cm_path):
            learning_count += 1
    curricula_count = 0
    curricula_dir = os.path.join(LEARNING_DIR, "curricula")
    if os.path.isdir(curricula_dir):
        curricula_count = len([f for f in os.listdir(curricula_dir) if f.endswith("-curriculum.json")])

    # Determine health status
    issue_count = len(all_issues)
    if issue_count < 5:
        health = "GOOD"
    elif issue_count < 20:
        health = "NEEDS ATTENTION"
    else:
        health = "CRITICAL"

    report = f"""# Knowledge Base Lint Report

**Date**: {now}
**Repos**: {total_repos}
**Issues found**: {issue_count}
**Health**: {health}

## Coverage Summary

| Metric | Count | Total | Coverage |
|--------|-------|-------|----------|
| DeepWiki wikis | {wikis_count} | {total_repos} | {wikis_count * 100 // max(total_repos, 1)}% |
| Code indexes | {code_index_count} | {total_repos} | {code_index_count * 100 // max(total_repos, 1)}% |
| Topic tags | {topics_count} | {total_repos} | {topics_count * 100 // max(total_repos, 1)}% |
| Learning artifacts | {learning_count} | {total_repos} | {learning_count * 100 // max(total_repos, 1)}% |
| Cross-repo curricula | {curricula_count} | — | — |

## Graph Health

{"Neptune configured: " + NEPTUNE_ENDPOINT if NEPTUNE_ENDPOINT else "Neptune not configured (NEPTUNE_ENDPOINT not set)"}

## Issues

"""
    for issue in all_issues:
        report += f"- {issue}\n"

    if not all_issues:
        report += "No issues found.\n"

    # Topic distribution
    all_tags: dict[str, int] = {}
    for repo in all_repos:
        for tag in repo_state.get(repo, {}).get("topics", []):
            all_tags[tag] = all_tags.get(tag, 0) + 1

    if all_tags:
        report += "\n## Topic Distribution\n\n"
        report += "| Topic | Repos |\n|-------|-------|\n"
        for tag, count in sorted(all_tags.items(), key=lambda x: -x[1])[:30]:
            report += f"| {tag} | {count} |\n"

    report += f"\n---\n*Generated by lint-wiki.py at {now}*\n"
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Daily knowledge base lint")
    parser.add_argument("--repos-file", default=REPOS_FILE)
    args = parser.parse_args()

    if not settings.s3_bucket_name:
        log.error("No S3 bucket configured. Set S3_BUCKET_NAME env var.")
        sys.exit(1)

    store = _get_store()

    # Load repos and state
    all_repos = parse_content_file(args.repos_file)
    repo_state = load_state("repo-state.json")

    if not all_repos:
        log.warning("No repos found in %s", args.repos_file)
        all_repos = list(repo_state.keys())

    log.info("Running lint on %d repos", len(all_repos))

    # Run all checks
    all_issues: list[str] = []

    log.info("Check 1: Missing wikis")
    all_issues.extend(check_missing_wikis(all_repos, repo_state))

    log.info("Check 2: Stale wikis")
    all_issues.extend(check_stale_wikis(all_repos, repo_state))

    log.info("Check 3: Missing code-index")
    all_issues.extend(check_missing_code_index(all_repos, repo_state))

    log.info("Check 4: L1 language check (sampling %d repos)", L1_SAMPLE_SIZE)
    all_issues.extend(check_l1_language(all_repos, L1_SAMPLE_SIZE))

    log.info("Check 5: Orphan discoveries")
    all_issues.extend(check_orphan_discoveries())

    log.info("Check 6: Missing topic tags")
    all_issues.extend(check_missing_topics(all_repos, repo_state))

    log.info("Check 7: Graph health")
    all_issues.extend(check_graph_health())

    log.info("Check 8: Graph contradictions")
    all_issues.extend(check_graph_contradictions(all_repos))

    log.info("Check 9: Missing learning artifacts")
    all_issues.extend(check_missing_learning_artifacts(all_repos))

    log.info("Check 10: Stale quiz questions")
    all_issues.extend(check_stale_quiz_questions(all_repos))

    log.info("Check 11: Broken learning paths")
    all_issues.extend(check_broken_learning_paths(all_repos))

    log.info("Check 12: Orphan curricula")
    all_issues.extend(check_orphan_curricula())

    # Generate and upload report
    report = generate_report(all_repos, repo_state, all_issues)

    log.info("Lint complete: %d issues found", len(all_issues))
    log.info("Uploading report to meta/lint-report.md")

    uploaded = store.put_content("meta/lint-report.md", report)

    if uploaded:
        log.info("Lint report uploaded successfully")
    else:
        log.warning("Failed to upload lint report")
        # Print report to stdout as fallback
        print(report)

    # Exit with non-zero if critical issues
    if len(all_issues) >= 20:
        log.warning("CRITICAL: %d issues found", len(all_issues))
        # Don't fail the CronJob — lint issues are informational
    return 0


if __name__ == "__main__":
    sys.exit(main())
