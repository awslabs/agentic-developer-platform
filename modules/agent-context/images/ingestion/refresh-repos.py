#!/usr/bin/env python3
"""Daily incremental refresh — SHA-based for repos, ETag-based for URLs.

Checks for changes without cloning, only re-processes what changed.
Designed to run as a K8s CronJob (daily at 6am UTC).

Usage:
  python refresh-repos.py
  python refresh-repos.py --repos-file /config/repos.txt --urls-file /config/urls.txt
  python refresh-repos.py --force  # Re-process everything regardless of state
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from config import settings
from s3_store import S3ContentStore

# ---------------------------------------------------------------------------
# Input validators — guard subprocess args against flag-injection
# ---------------------------------------------------------------------------

_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")  # owner/name pattern
_URL_RE = re.compile(r"^https://[a-zA-Z0-9.-]+(/[a-zA-Z0-9._~!$&'()*+,;=:@%/-]*)?$")


def _safe_repo(repo: str) -> str:
    """Validate repo name before passing to subprocess."""
    if repo.startswith("-") or not _REPO_NAME_RE.match(repo):
        raise ValueError(f"refusing to ingest repo with suspicious name: {repo!r}")
    return repo


def _safe_url(url: str) -> str:
    """Validate URL before passing to subprocess."""
    if not _URL_RE.match(url):
        raise ValueError(f"refusing URL: {url!r}")
    return url


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("refresh")

# ---------------------------------------------------------------------------
# Configuration (centralized via config.py)
# ---------------------------------------------------------------------------

STATE_DIR = settings.state_dir
REPOS_FILE = settings.repos_file
URLS_FILE = settings.urls_file
DOCS_FILE = settings.docs_file
ACCOUNTS_FILE = settings.accounts_file

# SQS publisher mode: when SQS_QUEUE_URL is set, delegate to publish-ingestion.py
SQS_QUEUE_URL = settings.sqs_queue_url
DEEPWIKI_URL = settings.deepwiki_url
DEEPWIKI_SIGNIFICANT_THRESHOLD = 10  # Re-run DeepWiki if >N files changed
MAX_WIKIS_PER_RUN = settings.max_wikis_per_run

# LLM configuration for incremental wiki updates and topic tagging
LLM_MODEL = settings.model_wiki
LLM_BASE_URL = settings.llm_base_url
CLONE_BASE = settings.clone_base

# S3 content store
store = S3ContentStore(
    bucket_name=settings.s3_bucket_name,
    prefix=settings.s3_content_prefix,
    region_name=settings.aws_region,
)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state(filename: str) -> dict[str, Any]:
    """Load state JSON from STATE_DIR."""
    path = os.path.join(STATE_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load %s: %s — starting fresh", path, e)
    return {}


def save_state(filename: str, state: dict[str, Any]):
    """Save state JSON to STATE_DIR."""
    path = os.path.join(STATE_DIR, filename)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    log.info("Saved state to %s (%d entries)", path, len(state))


# ---------------------------------------------------------------------------
# Content file parsing
# ---------------------------------------------------------------------------


def parse_content_file(path: str) -> list[str]:
    """Parse a repos.txt or urls.txt file, skipping comments and blank lines."""
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


# ---------------------------------------------------------------------------
# LLM helper (via LiteLLM proxy)
# ---------------------------------------------------------------------------


def call_llm(prompt: str, model: str = LLM_MODEL, max_tokens: int = 8192) -> str | None:
    """Call the LLM via LiteLLM proxy (OpenAI-compatible API)."""
    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            json={
                "model": model,
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


# ---------------------------------------------------------------------------
# Git diff helpers (for incremental wiki updates)
# ---------------------------------------------------------------------------


def git_clone_full(repo: str, dest: str, old_sha: str | None = None) -> bool:
    """Clone a repo (shallow if no old_sha, otherwise deep enough for diff)."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = ["git", "clone", "--depth=50", f"https://github.com/{repo}", dest]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("git clone failed for %s: %s", repo, e)
        return False


def git_diff_names(clone_path: str, old_sha: str, new_sha: str) -> list[str]:
    """Get list of changed file names between two SHAs."""
    try:
        result = subprocess.run(
            ["git", "-C", clone_path, "diff", "--name-only", old_sha, new_sha],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.decode().strip().split("\n") if f]
        return []
    except Exception as e:
        log.warning("git diff --name-only failed: %s", e)
        return []


def git_diff_stat(clone_path: str, old_sha: str, new_sha: str) -> str:
    """Get diff stat summary between two SHAs."""
    try:
        result = subprocess.run(
            ["git", "-C", clone_path, "diff", "--stat", old_sha, new_sha],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.decode()[:3000]
        return "(diff stat unavailable)"
    except Exception as e:
        log.warning("git diff --stat failed: %s", e)
        return "(diff stat unavailable)"


# ---------------------------------------------------------------------------
# Incremental wiki update (LLM-based)
# ---------------------------------------------------------------------------


def fetch_existing_wiki(org_repo: str) -> str | None:
    """Fetch the existing wiki for a repo from S3."""
    safe_name = org_repo.replace("/", "-")
    content = store.get_content(f"wikis/{safe_name}-wiki.md")
    if content and len(content) > 100:
        return content
    return None


def incremental_wiki_update(repo: str, old_sha: str, new_sha: str) -> bool:
    """Update an existing wiki based on what changed, instead of full regeneration.

    Returns True if the wiki was updated successfully.
    """

    # 1. Fetch existing wiki
    existing_wiki = fetch_existing_wiki(repo)
    if not existing_wiki:
        log.info("No existing wiki found for %s — cannot do incremental update", repo)
        return False

    # 2. Clone and get diff
    clone_path = os.path.join(CLONE_BASE, repo.replace("/", "-") + "-diff")
    if os.path.exists(clone_path):
        shutil.rmtree(clone_path, ignore_errors=True)

    if not git_clone_full(repo, clone_path, old_sha):
        return False

    changed_files = git_diff_names(clone_path, old_sha, new_sha)
    diff_summary = git_diff_stat(clone_path, old_sha, new_sha)

    # Cleanup clone
    shutil.rmtree(clone_path, ignore_errors=True)

    if not changed_files:
        log.info("No file changes detected for %s — skipping wiki update", repo)
        return False

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 3. Call LLM to produce incremental update
    prompt = f"""You are maintaining a wiki for the repository {repo}.

The following files changed (diff from {old_sha[:8]} to {new_sha[:8]}):
{diff_summary}

Changed files: {", ".join(changed_files[:20])}
{"(and " + str(len(changed_files) - 20) + " more files)" if len(changed_files) > 20 else ""}

Here is the current wiki:
{existing_wiki[:30000]}

Update the wiki to reflect these changes:
- Update sections affected by the changed files
- Note any contradictions with previous content (mark with WARNING)
- Add new sections if significant new functionality was added
- Keep unchanged sections intact
- Add a changelog entry at the bottom: "Updated {today}: <brief description of changes>"

Return the complete updated wiki markdown."""

    updated_wiki = call_llm(prompt)
    if not updated_wiki:
        log.warning("LLM failed to generate incremental wiki update for %s", repo)
        return False

    # 4. Upload updated wiki
    if upload_wiki_to_s3(updated_wiki, repo):
        log.info(
            "Incremental wiki update successful for %s (%d changed files)", repo, len(changed_files)
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Topic tagging (LLM-discovered topics for each repo)
# ---------------------------------------------------------------------------


def tag_repo_with_topics(repo: str, repo_state: dict[str, Any]) -> list[str]:
    """Use LLM to discover topic tags for a repo based on its content."""
    # Check if we already have tags and repo hasn't changed
    state = repo_state.get(repo, {})
    if state.get("topics") and state.get("topics_sha") == state.get("last_sha"):
        return state.get("topics", [])

    # Fetch overview/wiki content for context
    existing_wiki = fetch_existing_wiki(repo)
    context = existing_wiki[:3000] if existing_wiki else f"Repository: {repo}"

    prompt = f"""Analyze this repository and assign 3-7 topic tags.

Repo: {repo}
Content:
{context}

Return ONLY a JSON array of lowercase topic slugs. Examples:
["agent-framework", "mcp", "rag", "bedrock", "memory", "coding-agent", "multi-agent", "tool-use", "guardrails", "voice-agent", "document-processing"]

Discover topics from the content. If this repo introduces a new concept, create a new topic for it."""

    result = call_llm(prompt, max_tokens=512)
    if not result:
        return state.get("topics", [])

    try:
        # Extract JSON array from response
        match = re.search(r"\[.*\]", result, re.DOTALL)
        if match:
            tags = json.loads(match.group())
            if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
                return [t.lower().strip() for t in tags[:10]]
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse topic tags for %s: %s", repo, e)

    return state.get("topics", [])


# ---------------------------------------------------------------------------
# Repo refresh (SHA-based)
# ---------------------------------------------------------------------------


def git_ls_remote(repo: str) -> str | None:
    """Get the HEAD SHA of a GitHub repo without cloning."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            sha = result.stdout.decode().split()[0]
            return sha
        return None
    except (subprocess.TimeoutExpired, Exception) as e:
        log.warning("git ls-remote failed for %s: %s", repo, e)
        return None


def refresh_repo(repo: str, state: dict[str, Any], force: bool = False) -> bool:
    """Check if a repo has changed and re-ingest if needed.

    Returns True if the repo was re-processed.
    """
    current_sha = git_ls_remote(repo)
    if not current_sha:
        log.warning("Could not get SHA for %s — skipping", repo)
        return False

    prev_state = state.get(repo, {})
    prev_sha = prev_state.get("last_sha")

    if not force and current_sha == prev_sha:
        log.info("SKIP %s — no changes (SHA: %s)", repo, current_sha[:8])
        return False

    if prev_sha:
        log.info("UPDATE %s — SHA changed: %s -> %s", repo, prev_sha[:8], current_sha[:8])
    else:
        log.info("NEW %s — first ingestion", repo)

    # Re-ingest using ingest-repo.py
    try:
        result = subprocess.run(
            [
                sys.executable,
                "/app/ingest-repo.py",
                "--repo",
                _safe_repo(repo),
            ],
            capture_output=True,
            timeout=900,  # 15 minute timeout per repo
        )
        if result.returncode == 0:
            log.info("Re-ingested %s successfully", repo)
        else:
            log.warning("ingest-repo.py failed for %s: %s", repo, result.stderr.decode()[:300])
    except subprocess.TimeoutExpired:
        log.warning("ingest-repo.py timed out for %s", repo)
    except Exception as e:
        log.warning("ingest-repo.py error for %s: %s", repo, e)

    # --- Incremental wiki update for repos with existing wikis ---
    has_existing_wiki = bool(prev_state.get("deepwiki_sha"))
    wiki_updated = False

    if has_existing_wiki and prev_sha:
        log.info(
            "Attempting incremental wiki update for %s (diff %s..%s)",
            repo,
            prev_sha[:8],
            current_sha[:8],
        )
        wiki_updated = incremental_wiki_update(repo, prev_sha, current_sha)
        if wiki_updated:
            log.info("Incremental wiki update succeeded for %s", repo)
        else:
            log.info(
                "Incremental wiki update failed for %s — wiki will be regenerated on backfill", repo
            )

    # --- Topic tagging via LLM ---
    topics = tag_repo_with_topics(repo, state)

    # Update state regardless (to avoid re-processing on next run)
    state[repo] = {
        "last_sha": current_sha,
        "last_ingested": datetime.now(timezone.utc).isoformat(),
        "code_index_sha": current_sha,
        "deepwiki_sha": current_sha
        if wiki_updated
        else (current_sha if not prev_sha else prev_state.get("deepwiki_sha")),
        "topics": topics,
        "topics_sha": current_sha if topics else prev_state.get("topics_sha"),
    }
    return True


# ---------------------------------------------------------------------------
# URL refresh (ETag/Last-Modified based)
# ---------------------------------------------------------------------------


def check_url_changed(url: str, prev_state: dict[str, Any]) -> bool:
    """Check if a URL has changed using HEAD request + ETag/Last-Modified."""
    try:
        resp = requests.head(
            url,
            timeout=15,
            headers={"User-Agent": "AgentContext-Crawler/1.0"},
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            log.warning("HEAD %s returned %d", url, resp.status_code)
            return True  # Re-crawl on error (might be a new URL)

        etag = resp.headers.get("ETag", "")
        last_modified = resp.headers.get("Last-Modified", "")

        prev_etag = prev_state.get("etag", "")
        prev_last_modified = prev_state.get("last_modified", "")

        if etag and etag == prev_etag:
            return False
        if last_modified and last_modified == prev_last_modified:
            return False

        # No caching headers or they changed
        return True

    except Exception as e:
        log.warning("HEAD request failed for %s: %s", url, e)
        return True  # Re-crawl on error


def refresh_url(url: str, state: dict[str, Any], force: bool = False) -> bool:
    """Check if a URL has changed and re-crawl if needed."""
    prev_state = state.get(url, {})

    if not force and prev_state and not check_url_changed(url, prev_state):
        log.info("SKIP %s — no changes", url)
        return False

    log.info("CRAWL %s", url)

    # Re-crawl using ingest-url.py
    try:
        result = subprocess.run(
            [
                sys.executable,
                "/app/ingest-url.py",
                "--url",
                _safe_url(url),
                "--max-pages",
                "100",
            ],
            capture_output=True,
            timeout=600,  # 10 minute timeout per URL
        )
        if result.returncode == 0:
            log.info("Re-crawled %s successfully", url)
            # Parse pages count from stdout
            try:
                output = json.loads(result.stdout)
                pages_count = output.get("pages_uploaded", 0)
            except (json.JSONDecodeError, KeyError):
                pages_count = 0
        else:
            log.warning("ingest-url.py failed for %s: %s", url, result.stderr.decode()[:300])
            pages_count = 0
    except subprocess.TimeoutExpired:
        log.warning("ingest-url.py timed out for %s", url)
        pages_count = 0
    except Exception as e:
        log.warning("ingest-url.py error for %s: %s", url, e)
        pages_count = 0

    # Get current ETag/Last-Modified for state tracking
    try:
        resp = requests.head(
            url,
            timeout=15,
            headers={"User-Agent": "AgentContext-Crawler/1.0"},
            allow_redirects=True,
        )
        etag = resp.headers.get("ETag", "")
        last_modified = resp.headers.get("Last-Modified", "")
    except Exception:
        etag = ""
        last_modified = ""

    state[url] = {
        "etag": etag,
        "last_modified": last_modified,
        "last_crawled": datetime.now(timezone.utc).isoformat(),
        "pages_count": pages_count,
    }
    return True


# ---------------------------------------------------------------------------
# DeepWiki backfill — generate wikis for repos missing them
# ---------------------------------------------------------------------------


def deepwiki_generate(org_repo: str) -> str | None:
    """Call DeepWiki API to generate a wiki for a repo. Returns markdown or None."""
    try:
        resp = requests.post(
            f"{DEEPWIKI_URL}/api/wiki/generate",
            json={
                "repo_url": f"https://github.com/{org_repo}",
                "provider": "openai",
                "model": "bedrock/global.anthropic.claude-sonnet-4-6",
            },
            timeout=600,
        )
        if resp.status_code < 300:
            data = resp.json()
            pages = data.get("pages", [])
            if pages:
                wiki_parts = [f"# {org_repo} — Architecture Wiki\n"]
                for page in pages:
                    title = page.get("title", "")
                    content = page.get("content", "")
                    if title:
                        wiki_parts.append(f"\n## {title}\n")
                    if content:
                        wiki_parts.append(content)
                return "\n".join(wiki_parts)
            return data.get("content", data.get("wiki", ""))
        else:
            log.warning(
                "DeepWiki returned HTTP %d for %s: %s", resp.status_code, org_repo, resp.text[:200]
            )
            return None
    except requests.Timeout:
        log.warning("DeepWiki timed out for %s", org_repo)
        return None
    except Exception as e:
        log.warning("DeepWiki failed for %s: %s", org_repo, e)
        return None


def upload_wiki_to_s3(wiki: str, org_repo: str) -> bool:
    """Upload a DeepWiki wiki markdown to S3."""
    safe_name = org_repo.replace("/", "-")
    success = store.put_content(f"wikis/{safe_name}-wiki.md", wiki)
    if success:
        log.info(
            "Uploaded wiki for %s -> s3://.../%s/wikis/%s-wiki.md",
            org_repo,
            settings.s3_content_prefix,
            safe_name,
        )
    else:
        log.warning("Wiki upload to S3 failed for %s", org_repo)
    return success


def backfill_deepwiki_wikis(repo_state: dict[str, Any]) -> int:
    """Generate DeepWiki wikis for repos that don't have them yet.

    Caps at MAX_WIKIS_PER_RUN to stay within rate limits.
    Returns the number of wikis generated.
    """
    repos_needing_wiki = [
        repo for repo, state in repo_state.items() if not state.get("deepwiki_sha")
    ]

    if not repos_needing_wiki:
        log.info("All repos already have DeepWiki wikis — nothing to backfill")
        return 0

    log.info(
        "Found %d repos missing DeepWiki wikis — generating up to %d this run",
        len(repos_needing_wiki),
        MAX_WIKIS_PER_RUN,
    )

    wikis_generated = 0
    for repo in repos_needing_wiki[:MAX_WIKIS_PER_RUN]:
        log.info(
            "Generating DeepWiki wiki for %s (%d/%d)", repo, wikis_generated + 1, MAX_WIKIS_PER_RUN
        )
        wiki = deepwiki_generate(repo)
        if wiki:
            uploaded = upload_wiki_to_s3(wiki, repo)
            if uploaded:
                repo_state[repo]["deepwiki_sha"] = repo_state[repo].get("last_sha", "backfill")
                wikis_generated += 1
                log.info("DeepWiki wiki generated and uploaded for %s", repo)
            else:
                log.warning("DeepWiki wiki generated but upload failed for %s", repo)
        else:
            log.warning("DeepWiki wiki generation failed for %s — will retry next run", repo)

        # Small delay between wiki generations to avoid rate limits
        if wikis_generated < MAX_WIKIS_PER_RUN:
            # nosemgrep: arbitrary-sleep — rate-limit pacing for DeepWiki API
            time.sleep(5)

    log.info(
        "DeepWiki backfill: %d wikis generated, %d repos still need wikis",
        wikis_generated,
        len(repos_needing_wiki) - wikis_generated,
    )
    return wikis_generated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_publisher(force: bool = False, triggered_by: str = "daily_refresh") -> dict[str, Any]:
    """Delegate to publish-ingestion.py when SQS_QUEUE_URL is configured.

    Instead of processing sequentially, enqueues work to SQS for parallel processing
    by KEDA ScaledJob workers.
    """
    log.info("SQS mode: delegating to publish-ingestion.py (triggered_by=%s)", triggered_by)
    cmd = [
        sys.executable,
        "/app/publish-ingestion.py",
        "--all",
        "--triggered-by",
        triggered_by,
    ]
    if force:
        cmd.append("--force")

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        stdout = result.stdout.decode()
        stderr = result.stderr.decode()
        if result.returncode == 0:
            log.info("Publisher output: %s", stdout[:500])
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return {"status": "ok", "output": stdout[:500]}
        else:
            log.error("Publisher failed: %s", stderr[:500])
            return {"status": "failed", "error": stderr[:500]}
    except subprocess.TimeoutExpired:
        log.error("Publisher timed out")
        return {"status": "failed", "error": "timeout"}
    except Exception as e:
        log.error("Publisher error: %s", e)
        return {"status": "failed", "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Daily incremental refresh")
    parser.add_argument("--repos-file", default=REPOS_FILE)
    parser.add_argument("--urls-file", default=URLS_FILE)
    parser.add_argument("--force", action="store_true", help="Re-process everything")
    parser.add_argument("--repos-only", action="store_true", help="Only refresh repos")
    parser.add_argument("--urls-only", action="store_true", help="Only refresh URLs")
    args = parser.parse_args()

    # Support FORCE_REINDEX env var (set by agent-context-ingest.yml workflow)
    if not args.force and os.environ.get("FORCE_REINDEX", "").lower() in ("true", "1", "yes"):
        log.info("FORCE_REINDEX env var detected — enabling force mode")
        args.force = True

    # --- SQS Publisher Mode ---
    # When SQS_QUEUE_URL is configured, delegate to publish-ingestion.py
    # which enqueues work to SQS for parallel processing by KEDA workers.
    if SQS_QUEUE_URL:
        result = run_publisher(force=args.force, triggered_by="daily_refresh")
        log.info("Publisher result: %s", json.dumps(result))
        print(json.dumps(result, indent=2))
        return

    # --- Legacy Sequential Mode (fallback when SQS is not configured) ---
    if not settings.s3_bucket_name:
        log.error("No S3 bucket configured. Set S3_BUCKET_NAME env var.")
        sys.exit(1)

    start_time = time.monotonic()
    repos_processed = 0
    repos_skipped = 0
    urls_processed = 0
    urls_skipped = 0
    wikis_generated = 0

    # --- Repo refresh ---
    if not args.urls_only:
        repos = parse_content_file(args.repos_file)
        log.info("Loaded %d repos from %s", len(repos), args.repos_file)

        repo_state = load_state("repo-state.json")

        for repo in repos:
            if refresh_repo(repo, repo_state, force=args.force):
                repos_processed += 1
            else:
                repos_skipped += 1

        # --- DeepWiki backfill: generate wikis for repos missing them ---
        wikis_generated = backfill_deepwiki_wikis(repo_state)

        save_state("repo-state.json", repo_state)

        # Clean up repos that were removed from repos.txt
        stale = set(repo_state.keys()) - set(repos)
        if stale:
            log.info("Removing %d stale repos from state: %s", len(stale), stale)
            for repo in stale:
                del repo_state[repo]
            save_state("repo-state.json", repo_state)

    # --- URL refresh ---
    if not args.repos_only:
        urls = parse_content_file(args.urls_file)
        log.info("Loaded %d URLs from %s", len(urls), args.urls_file)

        url_state = load_state("url-state.json")

        for url in urls:
            if refresh_url(url, url_state, force=args.force):
                urls_processed += 1
            else:
                urls_skipped += 1

        save_state("url-state.json", url_state)

        # Clean up stale URLs
        stale_urls = set(url_state.keys()) - set(urls)
        if stale_urls:
            log.info("Removing %d stale URLs from state", len(stale_urls))
            for url in stale_urls:
                del url_state[url]
            save_state("url-state.json", url_state)

    elapsed = time.monotonic() - start_time
    log.info(
        "Refresh complete in %.1fs: repos=%d processed/%d skipped, deepwiki_backfill=%d, urls=%d processed/%d skipped",
        elapsed,
        repos_processed,
        repos_skipped,
        wikis_generated,
        urls_processed,
        urls_skipped,
    )


if __name__ == "__main__":
    main()
