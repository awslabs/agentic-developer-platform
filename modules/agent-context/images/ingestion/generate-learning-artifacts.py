#!/usr/bin/env python3
"""Generate per-repo learning artifacts from all indexed knowledge.

Produces:
  /platform-data/learning/{org}-{repo}/concept-map.json
  /platform-data/learning/{org}-{repo}/quiz-bank.json
  /platform-data/learning/{org}-{repo}/learning-path.json

Input sources:
  - code-index.json (symbols, dependencies) — from cgc
  - DeepWiki wiki — architecture docs
  - Source code files — from clone
  - Neptune entities — from GraphRAG extraction

Usage:
  python generate-learning-artifacts.py --repo org/repo
  python generate-learning-artifacts.py --all --repos-file /config/repos.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
log = logging.getLogger("learning-artifacts")

# Configuration (centralized via config.py)
from config import settings

LEARNING_DIR = settings.learning_dir
CODE_INDEX_DIR = settings.code_index_dir
CLONE_BASE = settings.clone_base
LLM_MODEL = settings.model_learning
LLM_BASE_URL = settings.llm_base_url
REPOS_FILE = settings.repos_file


def safe_name(repo: str) -> str:
    return repo.replace("/", "-")


def load_code_index(repo: str) -> dict[str, Any] | None:
    """Load code-index.json for a repo."""
    path = os.path.join(CODE_INDEX_DIR, f"{safe_name(repo)}.json")
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load code-index for %s: %s", repo, e)
    return None


def load_wiki(repo: str) -> str | None:
    """Load DeepWiki wiki markdown for a repo."""
    wiki_path = os.path.join(CLONE_BASE, repo, ".deepwiki-wiki.md")
    if os.path.isfile(wiki_path):
        try:
            return Path(wiki_path).read_text(errors="replace")
        except OSError:
            pass
    # Try platform-data wiki directory
    alt_path = os.path.join("/platform-data/wikis", f"{safe_name(repo)}-wiki.md")
    if os.path.isfile(alt_path):
        try:
            return Path(alt_path).read_text(errors="replace")
        except OSError:
            pass
    return None


def list_source_files(repo: str, max_files: int = 50) -> list[dict[str, str]]:
    """List key source files from the repo clone."""
    clone_path = os.path.join(CLONE_BASE, repo)
    if not os.path.isdir(clone_path):
        return []

    ext_priority = [".py", ".ts", ".js", ".go", ".rs", ".java"]
    skip_dirs = {"node_modules", ".git", "vendor", "__pycache__", ".venv", "dist", "build"}
    files = []

    for ext in ext_priority:
        for fpath in Path(clone_path).rglob(f"*{ext}"):
            if any(p in fpath.parts for p in skip_dirs):
                continue
            rel = str(fpath.relative_to(clone_path))
            try:
                content = fpath.read_text(errors="replace")[:2000]
                files.append({"path": rel, "preview": content})
            except OSError:
                continue
            if len(files) >= max_files:
                break
        if len(files) >= max_files:
            break

    return files


def call_llm(prompt: str, max_tokens: int = 4000) -> str | None:
    """Call LLM via LiteLLM proxy for artifact generation."""
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
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        log.warning("LLM call failed: HTTP %d — %s", resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        log.warning("LLM call error: %s", e)
        return None


def extract_json_from_response(text: str) -> Any:
    """Extract JSON from LLM response (may be wrapped in markdown code blocks)."""
    if not text:
        return None
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting from code block
    import re
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


def generate_concept_map(
    repo: str,
    code_index: dict | None,
    wiki: str | None,
    source_files: list[dict],
) -> list[dict[str, Any]]:
    """Generate concept map: key concepts, prerequisites, teaching files."""
    context_parts = [f"Repository: {repo}\n"]

    if code_index:
        symbols = code_index.get("symbols", [])[:30]
        deps = code_index.get("dependencies", {}).get("external", [])[:20]
        langs = code_index.get("language_stats", {})
        context_parts.append(f"Languages: {json.dumps(langs)}")
        context_parts.append(f"External dependencies: {json.dumps(deps)}")
        context_parts.append(f"Key symbols: {json.dumps([s['name'] for s in symbols])}")

    if wiki:
        context_parts.append(f"Architecture wiki (first 3000 chars):\n{wiki[:3000]}")

    if source_files:
        for f in source_files[:5]:
            context_parts.append(f"File {f['path']}:\n{f['preview'][:500]}")

    prompt = f"""Analyze this repository and generate a concept map as JSON.

{chr(10).join(context_parts)}

Return a JSON array of concepts. Each concept should have:
- "concept": string (the concept name)
- "level": "beginner"|"intermediate"|"advanced"
- "prerequisites": array of concept names this builds on
- "taught_by_files": array of file paths that teach this concept
- "key_insight": string (1-2 sentence explanation)
- "category": string (e.g., "architecture", "api", "pattern", "tool")

Return 10-20 concepts. Return ONLY valid JSON, no markdown."""

    response = call_llm(prompt)
    result = extract_json_from_response(response)
    return result if isinstance(result, list) else []


def generate_quiz_bank(
    repo: str,
    code_index: dict | None,
    source_files: list[dict],
) -> list[dict[str, Any]]:
    """Generate quiz questions grounded in actual code."""
    context_parts = [f"Repository: {repo}\n"]

    if code_index:
        symbols = code_index.get("symbols", [])[:20]
        for sym in symbols:
            context_parts.append(
                f"Symbol: {sym.get('type', '')} '{sym.get('name', '')}' "
                f"in {sym.get('file', '')}:{sym.get('line', 0)}"
            )

    for f in source_files[:10]:
        context_parts.append(f"File {f['path']}:\n{f['preview'][:800]}")

    prompt = f"""Generate quiz questions about this codebase. Questions must be answerable
from the provided source code.

{chr(10).join(context_parts)}

Return a JSON array of questions. Each question should have:
- "question": string
- "options": array of 3-4 answer choices
- "correct": integer (0-indexed correct answer)
- "explanation": string (why the answer is correct)
- "source_file": string (file path)
- "source_line": integer (approximate line number)
- "difficulty": "easy"|"medium"|"hard"

Generate 15-20 questions covering different files and concepts.
Return ONLY valid JSON, no markdown."""

    response = call_llm(prompt, max_tokens=6000)
    result = extract_json_from_response(response)
    return result if isinstance(result, list) else []


def generate_learning_path(
    repo: str,
    code_index: dict | None,
    wiki: str | None,
    concept_map: list[dict],
) -> list[dict[str, Any]]:
    """Generate ordered learning path based on dependency structure."""
    context_parts = [f"Repository: {repo}\n"]

    if concept_map:
        context_parts.append(f"Concepts: {json.dumps(concept_map[:15])}")

    if code_index:
        deps = code_index.get("dependencies", {})
        context_parts.append(f"Dependencies: {json.dumps(deps)}")

    if wiki:
        context_parts.append(f"Architecture wiki (first 2000 chars):\n{wiki[:2000]}")

    prompt = f"""Create a step-by-step learning path for understanding this repository.

{chr(10).join(context_parts)}

Return a JSON array of learning steps. Each step should have:
- "step": integer (1-indexed)
- "topic": string (what to learn)
- "description": string (1-2 sentences)
- "read": array of file paths to read
- "practice": string (hands-on exercise)
- "estimated_minutes": integer
- "prerequisites": array of step numbers this builds on

Order from fundamentals to advanced. Create 8-15 steps.
Return ONLY valid JSON, no markdown."""

    response = call_llm(prompt)
    result = extract_json_from_response(response)
    return result if isinstance(result, list) else []


def process_repo(repo: str) -> dict[str, str]:
    """Generate all learning artifacts for a single repo."""
    log.info("Generating learning artifacts for %s", repo)
    result = {"repo": repo}

    code_index = load_code_index(repo)
    wiki = load_wiki(repo)
    source_files = list_source_files(repo)

    if not code_index and not wiki and not source_files:
        log.warning("No knowledge sources available for %s — skipping", repo)
        result["status"] = "skipped"
        return result

    output_dir = os.path.join(LEARNING_DIR, safe_name(repo))
    os.makedirs(output_dir, exist_ok=True)

    # Generate concept map
    concept_map = generate_concept_map(repo, code_index, wiki, source_files)
    if concept_map:
        with open(os.path.join(output_dir, "concept-map.json"), "w") as f:
            json.dump(concept_map, f, indent=2)
        log.info("  Concept map: %d concepts", len(concept_map))
        result["concept_map"] = f"{len(concept_map)} concepts"
    else:
        log.warning("  Concept map generation failed for %s", repo)
        result["concept_map"] = "failed"

    # Generate quiz bank
    quiz_bank = generate_quiz_bank(repo, code_index, source_files)
    if quiz_bank:
        with open(os.path.join(output_dir, "quiz-bank.json"), "w") as f:
            json.dump({"questions": quiz_bank, "repo": repo,
                       "generated_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
        log.info("  Quiz bank: %d questions", len(quiz_bank))
        result["quiz_bank"] = f"{len(quiz_bank)} questions"
    else:
        log.warning("  Quiz bank generation failed for %s", repo)
        result["quiz_bank"] = "failed"

    # Generate learning path
    learning_path = generate_learning_path(repo, code_index, wiki, concept_map)
    if learning_path:
        with open(os.path.join(output_dir, "learning-path.json"), "w") as f:
            json.dump({"steps": learning_path, "repo": repo,
                       "generated_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
        log.info("  Learning path: %d steps", len(learning_path))
        result["learning_path"] = f"{len(learning_path)} steps"
    else:
        log.warning("  Learning path generation failed for %s", repo)
        result["learning_path"] = "failed"

    result["status"] = "ok"
    return result


def parse_repos_file(path: str) -> list[str]:
    """Parse repos.txt, skipping comments and blank lines."""
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
    parser = argparse.ArgumentParser(description="Generate learning artifacts per repo")
    parser.add_argument("--repo", help="Single repo to process (org/repo)")
    parser.add_argument("--all", action="store_true", help="Process all repos from repos file")
    parser.add_argument("--repos-file", default=REPOS_FILE)
    parser.add_argument("--max-repos", type=int, default=20, help="Max repos to process per run")
    args = parser.parse_args()

    if args.repo:
        result = process_repo(args.repo)
        print(json.dumps(result, indent=2))
    elif args.all:
        repos = parse_repos_file(args.repos_file)
        if not repos:
            log.error("No repos found in %s", args.repos_file)
            sys.exit(1)

        log.info("Processing %d repos (max %d per run)", len(repos), args.max_repos)
        results = []
        for repo in repos[:args.max_repos]:
            try:
                result = process_repo(repo)
                results.append(result)
            except Exception as e:
                log.error("Failed to process %s: %s", repo, e)
                results.append({"repo": repo, "status": f"error: {e}"})

        ok = sum(1 for r in results if r.get("status") == "ok")
        log.info("Complete: %d/%d repos processed successfully", ok, len(results))
        print(json.dumps(results, indent=2))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
