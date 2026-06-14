#!/usr/bin/env python3
"""Evaluation harness for the Knowledge Layer.

Workflow:
1. Check that all 15 corpus repos have been ingested (all four producers complete).
2. For each question in golden.yaml, query the appropriate MCP verb.
3. Score the response against the expected answer using the pass criterion.
4. Emit a per-verb hit-rate report + overall pass-rate.

Usage:
    # Against deployed MCP endpoint (live)
    TEST_ENV=dev python -m tests.eval.run_eval

    # Against direct backends (if Door not wired yet)
    TEST_ENV=dev EVAL_MODE=direct python -m tests.eval.run_eval

Environment variables:
    TEST_ENV        - "dev" for live evaluation (required)
    MCP_URL         - MCP endpoint URL (default: cluster-internal)
    EVAL_MODE       - "mcp" (default) or "direct" (hit backends directly)
    ZOEKT_URL       - Zoekt backend URL (for direct mode)
    S3_VECTORS_INDEX - S3 Vectors index name (for direct mode)
    REPORT_FORMAT   - "text" (default) or "json"
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

log = logging.getLogger(__name__)

# Paths
EVAL_DIR = Path(__file__).resolve().parent
CORPUS_FILE = EVAL_DIR / "corpus.yaml"
GOLDEN_FILE = EVAL_DIR / "golden.yaml"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class EvalConfig:
    """Runtime configuration for the evaluation harness."""

    mcp_url: str = "http://context-mcp.agent-context.svc.cluster.local:5100"
    eval_mode: str = "mcp"  # "mcp" or "direct"
    zoekt_url: str = "http://zoekt.agent-context.svc.cluster.local:6070"
    s3_vectors_index: str = "agent-context-code-embeddings"
    report_format: str = "text"  # "text" or "json"
    timeout: float = 30.0
    top_k: int = 10  # top-K for scoring (result must appear in top K)

    @classmethod
    def from_env(cls) -> "EvalConfig":
        import os

        return cls(
            mcp_url=os.environ.get(
                "MCP_URL",
                "http://context-mcp.agent-context.svc.cluster.local:5100",
            ),
            eval_mode=os.environ.get("EVAL_MODE", "mcp"),
            zoekt_url=os.environ.get(
                "ZOEKT_URL",
                "http://zoekt.agent-context.svc.cluster.local:6070",
            ),
            s3_vectors_index=os.environ.get("S3_VECTORS_INDEX", "agent-context-code-embeddings"),
            report_format=os.environ.get("REPORT_FORMAT", "text"),
            timeout=float(os.environ.get("EVAL_TIMEOUT", "30")),
            top_k=int(os.environ.get("EVAL_TOP_K", "10")),
        )


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Repo:
    """A corpus repository."""

    name: str
    url: str
    type: str
    description: str = ""
    languages: list[str] = field(default_factory=list)


@dataclass
class GoldenQuestion:
    """A single evaluation question with expected answer."""

    id: str
    repo: str
    verb: str  # search_exact, search_semantic, understand, impact, browse
    query: str
    expected: dict[str, Any]  # verb-specific expected answer
    pass_criterion: str  # human-readable criterion description
    notes: str = ""  # optional notes (e.g., "manual review needed for semantic")


@dataclass
class EvalResult:
    """Result of evaluating one question."""

    question_id: str
    repo: str
    verb: str
    query: str
    passed: bool
    score: float  # 0.0 or 1.0 for exact; 0.0-1.0 for semantic
    response: Any = None
    error: str = ""
    manual_review: bool = False  # flagged for human review (semantic)
    elapsed_ms: float = 0.0


@dataclass
class EvalReport:
    """Aggregated evaluation report."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    manual_review: int = 0
    by_verb: dict[str, dict[str, int]] = field(default_factory=dict)
    results: list[EvalResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        scoreable = self.total - self.manual_review
        if scoreable == 0:
            return 0.0
        return self.passed / scoreable

    def verb_hit_rate(self, verb: str) -> float:
        stats = self.by_verb.get(verb, {})
        total = stats.get("total", 0) - stats.get("manual_review", 0)
        if total == 0:
            return 0.0
        return stats.get("passed", 0) / total


# ---------------------------------------------------------------------------
# Corpus + Golden loaders
# ---------------------------------------------------------------------------


def load_corpus() -> list[Repo]:
    """Load the evaluation corpus from corpus.yaml."""
    with open(CORPUS_FILE) as f:
        data = yaml.safe_load(f)
    return [
        Repo(
            name=r["name"],
            url=r["url"],
            type=r["type"],
            description=r.get("description", ""),
            languages=r.get("languages", []),
        )
        for r in data["repos"]
    ]


def load_golden() -> list[GoldenQuestion]:
    """Load golden-answer questions from golden.yaml."""
    with open(GOLDEN_FILE) as f:
        data = yaml.safe_load(f)
    questions = []
    for q in data["questions"]:
        questions.append(
            GoldenQuestion(
                id=q["id"],
                repo=q["repo"],
                verb=q["verb"],
                query=q["query"],
                expected=q["expected"],
                pass_criterion=q["pass_criterion"],
                notes=q.get("notes", ""),
            )
        )
    return questions


# ---------------------------------------------------------------------------
# Ingestion check
# ---------------------------------------------------------------------------


def check_ingestion(config: EvalConfig, corpus: list[Repo]) -> dict[str, bool]:
    """Verify all corpus repos have been ingested.

    Returns a dict of repo_name → ingested (True/False).
    In MCP mode: queries the browse verb for each repo.
    In direct mode: queries Zoekt to check if the repo has indexed shards.
    """
    results: dict[str, bool] = {}

    for repo in corpus:
        try:
            if config.eval_mode == "direct":
                # Query Zoekt: search for any file in this repo
                # Use a common token as content query (Zoekt needs a content term)
                resp = httpx.post(
                    f"{config.zoekt_url}/api/search",
                    json={"q": f"r:{repo.name}", "num": 1},
                    timeout=config.timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result = data.get("Result", {})
                    # Zoekt v16+ uses "Files" instead of "FileMatches"
                    file_matches = result.get("FileMatches") or result.get("Files") or []
                    # Also check FileCount for repo-only queries (no content matches but files exist)
                    file_count = result.get("FileCount", 0)
                    results[repo.name] = len(file_matches) > 0 or file_count > 0
                else:
                    results[repo.name] = False
            else:
                # Use browse verb to check if repo is indexed
                headers = {
                    "Content-Type": "application/json",
                    "X-GitHub-Login": "eval-harness",
                    "X-GitHub-Teams": "platform-team",
                }
                resp = httpx.post(
                    f"{config.mcp_url}/call",
                    json={
                        "name": "browse",
                        "arguments": {"action": "ls", "uri": f"/{repo.name}"},
                    },
                    headers=headers,
                    timeout=config.timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # If browse returns entries, repo is indexed
                    entries = data.get("entries", [])
                    results[repo.name] = len(entries) > 0
                else:
                    results[repo.name] = False
        except Exception as e:
            log.warning("Ingestion check failed for %s: %s", repo.name, e)
            results[repo.name] = False

    return results


# ---------------------------------------------------------------------------
# Query dispatchers
# ---------------------------------------------------------------------------


def query_mcp(config: EvalConfig, verb: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Query the MCP endpoint for a given verb."""
    # Map eval verb names to MCP tool names
    tool_name = verb
    if verb in ("search_exact", "search_semantic"):
        tool_name = "search"

    # Auth headers required by the Door's ACL (fail-closed without them)
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Login": "eval-harness",
        "X-GitHub-Teams": "platform-team",
    }

    resp = httpx.post(
        f"{config.mcp_url}/call",
        json={"name": tool_name, "arguments": arguments},
        headers=headers,
        timeout=config.timeout,
    )
    resp.raise_for_status()
    return resp.json()


def query_direct(config: EvalConfig, verb: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Query backends directly (bypasses MCP/Door server).

    Supports search_exact and search_semantic via Zoekt.
    Other verbs (understand, impact, browse) raise NotImplementedError.
    """
    if verb in ("search_exact", "search_semantic"):
        return _query_zoekt(config, arguments)
    elif verb == "browse":
        return _query_zoekt_browse(config, arguments)
    else:
        raise NotImplementedError(
            f"Direct mode does not support verb '{verb}' — requires MCP/Door server"
        )


def _query_zoekt(config: EvalConfig, arguments: dict[str, Any]) -> dict[str, Any]:
    """Query Zoekt directly for code search."""
    query = arguments.get("query", "")
    limit = arguments.get("limit", 20)

    # Zoekt web API: POST /api/search
    resp = httpx.post(
        f"{config.zoekt_url}/api/search",
        json={"q": query, "num": limit},
        timeout=config.timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    # Transform Zoekt response to match MCP search response format
    # Zoekt v16+ uses "Files" instead of "FileMatches"
    result_data = data.get("Result", {})
    file_matches = result_data.get("FileMatches") or result_data.get("Files") or []

    results = []
    for file_match in file_matches:
        file_name = file_match.get("FileName", "")
        repo_name = file_match.get("Repository", "")
        for line_match in file_match.get("LineMatches", []) or []:
            content = line_match.get("Line", "")
            results.append(
                {
                    "file": f"{repo_name}/{file_name}" if repo_name else file_name,
                    "path": file_name,
                    "content": content,
                    "line": line_match.get("LineNumber", 0),
                    "repo": repo_name,
                }
            )

    # Also add file-level matches (in case LineMatches is empty)
    if not results:
        for file_match in file_matches:
            file_name = file_match.get("FileName", "")
            repo_name = file_match.get("Repository", "")
            results.append(
                {
                    "file": f"{repo_name}/{file_name}" if repo_name else file_name,
                    "path": file_name,
                    "content": "",
                    "repo": repo_name,
                }
            )

    return {"results": results[:limit]}


def _query_zoekt_browse(config: EvalConfig, arguments: dict[str, Any]) -> dict[str, Any]:
    """Query Zoekt for directory listing (browse verb)."""
    uri = arguments.get("uri", "")
    # Parse URI: /<repo>/<path> → search for files under that path
    parts = uri.strip("/").split("/", 1)
    repo = parts[0] if parts else ""
    path_prefix = parts[1] if len(parts) > 1 else ""

    # Use Zoekt file search to list files under the path
    query = f"r:{repo} f:{path_prefix}" if path_prefix else f"r:{repo} f:."
    resp = httpx.post(
        f"{config.zoekt_url}/api/search",
        json={"q": query, "num": 100},
        timeout=config.timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    # Extract unique directory entries at the requested depth
    # Zoekt v16+ uses "Files" instead of "FileMatches"
    result_data = data.get("Result", {})
    file_matches = result_data.get("FileMatches") or result_data.get("Files") or []
    entries = set()
    for file_match in file_matches:
        file_name = file_match.get("FileName", "")
        # Get the relative path from the prefix
        if path_prefix and file_name.startswith(path_prefix):
            relative = file_name[len(path_prefix) :].lstrip("/")
        elif not path_prefix:
            relative = file_name
        else:
            relative = file_name
        # Take the first path component (file or directory)
        first_component = relative.split("/")[0] if relative else ""
        if first_component:
            entries.add(first_component)

    return {"entries": [{"name": e} for e in sorted(entries)]}


def build_query_arguments(question: GoldenQuestion) -> dict[str, Any]:
    """Build MCP tool arguments from a golden question."""
    if question.verb == "search_exact":
        return {
            "query": question.query,
            "scope": "code",
            "limit": 20,
        }
    elif question.verb == "search_semantic":
        return {
            "query": question.query,
            "scope": "code",
            "limit": 20,
        }
    elif question.verb == "understand":
        return {
            "target": question.query,
            "depth": "detailed",
        }
    elif question.verb == "impact":
        return {
            "target": question.query,
            "cross_repo": False,
        }
    elif question.verb == "browse":
        return {
            "action": "ls",
            "uri": question.query,
        }
    else:
        raise ValueError(f"Unknown verb: {question.verb}")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_result(
    question: GoldenQuestion, response: dict[str, Any], config: EvalConfig
) -> EvalResult:
    """Score a query response against the golden answer.

    Scoring rules by verb:
    - search_exact: expected file must appear in top-K results
    - search_semantic: expected file/section in top-K (flagged for manual review)
    - understand: expected key facts present in response
    - impact: expected callers/dependents in result set
    - browse: expected entries present in listing
    """
    if question.verb == "search_exact":
        return _score_search(question, response, config)
    elif question.verb == "search_semantic":
        result = _score_search(question, response, config)
        result.manual_review = True  # always flag semantic for human review
        return result
    elif question.verb == "understand":
        return _score_understand(question, response, config)
    elif question.verb == "impact":
        return _score_impact(question, response, config)
    elif question.verb == "browse":
        return _score_browse(question, response, config)
    else:
        return EvalResult(
            question_id=question.id,
            repo=question.repo,
            verb=question.verb,
            query=question.query,
            passed=False,
            score=0.0,
            error=f"Unknown verb: {question.verb}",
        )


def _score_search(
    question: GoldenQuestion, response: dict[str, Any], config: EvalConfig
) -> EvalResult:
    """Score a search result: expected file in top-K FileMatches.

    HARDENED: Deduplicates results to file-level (one entry per unique file
    path) before scoring. Handles both file path formats:
    - Zoekt via Door: "path/to/file.py" (repo_id is separate)
    - Direct Zoekt: "org/repo/path/to/file.py"
    """
    results = response.get("results", [])
    expected_files = question.expected.get("files", [])
    expected_content = question.expected.get("content", [])

    # Dedup results to one per file (Door already does this but be safe)
    seen_files: set[str] = set()
    deduped_results: list[dict[str, Any]] = []
    for result in results:
        file_key = result.get("file", "") or result.get("path", "")
        if file_key and file_key not in seen_files:
            seen_files.add(file_key)
            deduped_results.append(result)

    # Check if any expected file appears in top-K deduped results
    found = False
    for result in deduped_results[: config.top_k]:
        result_file = result.get("file", "") or result.get("path", "")
        result_content = result.get("content", "")

        # File match (substring: expected may be a partial path)
        for exp_file in expected_files:
            if exp_file in result_file:
                found = True
                break

        # Content match (for cases where we expect specific text in the line)
        if not found:
            for exp_content in expected_content:
                if exp_content.lower() in result_content.lower():
                    found = True
                    break

        if found:
            break

    return EvalResult(
        question_id=question.id,
        repo=question.repo,
        verb=question.verb,
        query=question.query,
        passed=found,
        score=1.0 if found else 0.0,
        response=deduped_results[:3],  # store top 3 for review
    )


def _score_understand(
    question: GoldenQuestion, response: dict[str, Any], config: EvalConfig
) -> EvalResult:
    """Score an understand result: key facts present in structural data.

    HARDENED: Only checks the 'definitions' array content (symbols, files,
    signatures from the structural index loaded from S3). Does NOT check the
    full JSON response (which would allow false positives from echoed
    query/target strings or debug fields).
    """
    expected_facts = question.expected.get("key_facts", [])
    expected_location = question.expected.get("location", "")

    # Extract ONLY the structural data fields from definitions
    # (this is what actually came from S3 code-index.json)
    definitions = response.get("definitions", [])

    # Build searchable text from structural fields only:
    # symbol names, file paths, kinds, signatures, callers, callees
    structural_parts: list[str] = []
    for defn in definitions:
        structural_parts.append(defn.get("symbol", ""))
        structural_parts.append(defn.get("file", ""))
        structural_parts.append(defn.get("kind", ""))
        structural_parts.append(defn.get("signature", ""))
        structural_parts.append(defn.get("content", ""))
        # Include caller/callee names if present
        for caller in defn.get("callers", []):
            structural_parts.append(caller)
        for callee in defn.get("callees", []):
            structural_parts.append(callee)

    structural_text = " ".join(structural_parts).lower()

    # Also check if definitions reference the expected location file
    definition_files = [d.get("file", "") for d in definitions]
    location_found = not expected_location or any(
        expected_location.lower() in f.lower() for f in definition_files
    )

    # Check key facts against structural text only
    facts_found = 0
    for fact in expected_facts:
        if fact.lower() in structural_text:
            facts_found += 1

    # Pass if location found AND majority of facts found in actual structural data
    min_facts = max(1, len(expected_facts) // 2)
    passed = location_found and facts_found >= min_facts and len(definitions) > 0
    score = (
        facts_found / len(expected_facts) if expected_facts else (1.0 if location_found else 0.0)
    )

    return EvalResult(
        question_id=question.id,
        repo=question.repo,
        verb=question.verb,
        query=question.query,
        passed=passed,
        score=score,
        response=definitions[:5],  # store top 5 for review
    )


def _score_impact(
    question: GoldenQuestion, response: dict[str, Any], config: EvalConfig
) -> EvalResult:
    """Score an impact result: expected callers/dependents in result set.

    HARDENED: Checks the 'file' and 'symbol' fields of each affected entry
    (which come from S3 call-graph data or Zoekt reference search). Does
    NOT match against the full JSON (which would false-positive on the
    echoed 'target' field).
    """
    expected_affected = question.expected.get("affected", [])

    affected_results = response.get("affected", [])

    # Build searchable text from structural fields only:
    # file paths, symbols, relationships — NOT the 'target' field (which echoes the query)
    structural_parts: list[str] = []
    for entry in affected_results:
        structural_parts.append(entry.get("file", ""))
        structural_parts.append(entry.get("symbol", ""))
        structural_parts.append(entry.get("content", ""))
        # Include caller reference if present
        if "caller" in entry:
            structural_parts.append(entry["caller"])

    structural_text = " ".join(structural_parts).lower()

    found = 0
    for expected in expected_affected:
        if expected.lower() in structural_text:
            found += 1

    min_expected = max(1, len(expected_affected) // 2)
    passed = found >= min_expected and len(affected_results) > 0
    score = found / len(expected_affected) if expected_affected else 0.0

    return EvalResult(
        question_id=question.id,
        repo=question.repo,
        verb=question.verb,
        query=question.query,
        passed=passed,
        score=score,
        response=affected_results[:5],
    )


def _score_browse(
    question: GoldenQuestion, response: dict[str, Any], config: EvalConfig
) -> EvalResult:
    """Score a browse result: expected entries present in listing."""
    expected_entries = question.expected.get("entries", [])

    entries = response.get("entries", [])
    entry_names = [e.get("name", "") for e in entries]
    entry_text = " ".join(entry_names).lower()

    found = 0
    for expected in expected_entries:
        if expected.lower() in entry_text:
            found += 1

    min_expected = max(1, len(expected_entries) // 2)
    passed = found >= min_expected
    score = found / len(expected_entries) if expected_entries else 0.0

    return EvalResult(
        question_id=question.id,
        repo=question.repo,
        verb=question.verb,
        query=question.query,
        passed=passed,
        score=score,
        response=entries[:10],
    )


# ---------------------------------------------------------------------------
# Main evaluation loop
# ---------------------------------------------------------------------------


def run_evaluation(config: EvalConfig) -> EvalReport:
    """Run the full evaluation: ingest-check → query → score → report."""
    corpus = load_corpus()
    golden = load_golden()

    log.info("Loaded %d repos, %d golden questions", len(corpus), len(golden))

    # Phase 1: Check ingestion status
    log.info("Checking ingestion status for %d repos...", len(corpus))
    ingestion_status = check_ingestion(config, corpus)
    indexed_count = sum(1 for v in ingestion_status.values() if v)
    log.info("Ingestion: %d/%d repos indexed", indexed_count, len(corpus))

    not_indexed = [name for name, status in ingestion_status.items() if not status]
    if not_indexed:
        log.warning("NOT indexed (will skip questions): %s", ", ".join(not_indexed))

    # Phase 2: Query and score each question
    report = EvalReport()

    for question in golden:
        # Skip questions for non-indexed repos
        if question.repo in not_indexed:
            log.info("Skipping %s (repo %s not indexed)", question.id, question.repo)
            continue

        report.total += 1
        start = time.time()

        try:
            arguments = build_query_arguments(question)
            if config.eval_mode == "direct":
                response = query_direct(config, question.verb, arguments)
            else:
                response = query_mcp(config, question.verb, arguments)
            elapsed = (time.time() - start) * 1000

            result = score_result(question, response, config)
            result.elapsed_ms = elapsed

        except NotImplementedError as e:
            # In direct mode, some verbs aren't supported — skip gracefully
            elapsed = (time.time() - start) * 1000
            result = EvalResult(
                question_id=question.id,
                repo=question.repo,
                verb=question.verb,
                query=question.query,
                passed=False,
                score=0.0,
                error=f"SKIPPED (direct mode): {e}",
                elapsed_ms=elapsed,
            )
            report.errors += 1

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            result = EvalResult(
                question_id=question.id,
                repo=question.repo,
                verb=question.verb,
                query=question.query,
                passed=False,
                score=0.0,
                error=str(e),
                elapsed_ms=elapsed,
            )
            report.errors += 1

        # Tally results
        if result.manual_review:
            report.manual_review += 1
        elif result.passed:
            report.passed += 1
        else:
            report.failed += 1

        # Per-verb stats
        verb_stats = report.by_verb.setdefault(
            question.verb, {"total": 0, "passed": 0, "failed": 0, "errors": 0, "manual_review": 0}
        )
        verb_stats["total"] += 1
        if result.error:
            verb_stats["errors"] += 1
        elif result.manual_review:
            verb_stats["manual_review"] += 1
        elif result.passed:
            verb_stats["passed"] += 1
        else:
            verb_stats["failed"] += 1

        report.results.append(result)

    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(report: EvalReport, format: str = "text") -> None:
    """Print the evaluation report."""
    if format == "json":
        _print_json_report(report)
    else:
        _print_text_report(report)


def _print_text_report(report: EvalReport) -> None:
    """Print a human-readable text report."""
    print("\n" + "=" * 70)
    print("  KNOWLEDGE LAYER EVALUATION REPORT")
    print("=" * 70)

    print(f"\n  Total questions:   {report.total}")
    print(f"  Passed:            {report.passed}")
    print(f"  Failed:            {report.failed}")
    print(f"  Errors:            {report.errors}")
    print(f"  Manual review:     {report.manual_review}")
    scoreable = report.total - report.manual_review
    if scoreable > 0:
        print(f"  Pass rate:         {report.pass_rate:.1%} ({report.passed}/{scoreable})")
    print()

    # Per-verb breakdown
    print("  Per-verb hit rates:")
    print("  " + "-" * 50)
    for verb in sorted(report.by_verb.keys()):
        stats = report.by_verb[verb]
        rate = report.verb_hit_rate(verb)
        scoreable_v = stats["total"] - stats["manual_review"]
        print(
            f"    {verb:<20} {rate:>6.1%}  "
            f"({stats['passed']}/{scoreable_v} scoreable, "
            f"{stats['manual_review']} manual-review)"
        )
    print()

    # Failed questions detail
    failures = [r for r in report.results if not r.passed and not r.manual_review and not r.error]
    if failures:
        print("  FAILED questions:")
        print("  " + "-" * 50)
        for r in failures:
            print(f"    [{r.question_id}] {r.verb} | {r.repo}")
            print(f"      Query: {r.query[:60]}...")
            print(f"      Time: {r.elapsed_ms:.0f}ms")
            print()

    # Errors
    errors = [r for r in report.results if r.error]
    if errors:
        print("  ERRORS:")
        print("  " + "-" * 50)
        for r in errors:
            print(f"    [{r.question_id}] {r.error[:80]}")
        print()

    # Manual review items
    manual = [r for r in report.results if r.manual_review]
    if manual:
        print("  FLAGGED FOR MANUAL REVIEW (semantic search):")
        print("  " + "-" * 50)
        for r in manual:
            status = "PASS (auto)" if r.passed else "NEEDS REVIEW"
            print(f"    [{r.question_id}] {status} | {r.repo}")
            print(f"      Query: {r.query[:60]}")
            print()

    print("=" * 70 + "\n")


def _print_json_report(report: EvalReport) -> None:
    """Print a machine-readable JSON report."""
    output = {
        "summary": {
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "errors": report.errors,
            "manual_review": report.manual_review,
            "pass_rate": report.pass_rate,
        },
        "by_verb": {
            verb: {**stats, "hit_rate": report.verb_hit_rate(verb)}
            for verb, stats in report.by_verb.items()
        },
        "results": [
            {
                "id": r.question_id,
                "repo": r.repo,
                "verb": r.verb,
                "query": r.query,
                "passed": r.passed,
                "score": r.score,
                "manual_review": r.manual_review,
                "elapsed_ms": r.elapsed_ms,
                "error": r.error,
            }
            for r in report.results
        ],
    }
    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# Relevance evaluation (LLM-as-judge mode)
# ---------------------------------------------------------------------------


@dataclass
class RelevanceReport:
    """Aggregated relevance evaluation report."""

    calibration_passed: bool = False
    calibration_details: list[dict] = field(default_factory=list)
    total: int = 0
    by_verb: dict[str, dict[str, Any]] = field(default_factory=dict)
    results: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)

    @property
    def overall_mean_score(self) -> float:
        if not self.results:
            return 0.0
        return sum(r["score"] for r in self.results) / len(self.results)

    def worst_n(self, n: int = 5) -> list[dict]:
        """Return the N lowest-scoring results with justifications."""
        return sorted(self.results, key=lambda r: r["score"])[:n]


def _load_relevance_annotations(golden_data: dict) -> dict[str, dict[str, Any]]:
    """Load relevance_annotations from golden.yaml."""
    return golden_data.get("relevance_annotations", {})


def _load_calibration(golden_data: dict) -> list[dict]:
    """Load calibration items from golden.yaml."""
    return golden_data.get("calibration", [])


def _load_tasks(golden_data: dict) -> list[dict]:
    """Load end-to-end task scenarios from golden.yaml."""
    return golden_data.get("tasks", [])


def run_relevance_evaluation(config: EvalConfig) -> RelevanceReport:
    """Run the relevance evaluation: calibrate judge → grade answers → report.

    This mode uses an LLM-as-judge to score answer quality (0-3) rather than
    just checking presence/hit-rate.
    """
    from .judge import CalibrationItem, RelevanceJudge

    # Load data
    with open(GOLDEN_FILE) as f:
        golden_data = yaml.safe_load(f)

    golden = load_golden()
    corpus = load_corpus()
    annotations = _load_relevance_annotations(golden_data)
    calibration_data = _load_calibration(golden_data)
    tasks_data = _load_tasks(golden_data)

    report = RelevanceReport()

    # Phase 1: Calibration — judge must prove itself trustworthy
    log.info("Phase 1: Running judge calibration (%d items)...", len(calibration_data))
    judge = RelevanceJudge()

    calibration_items = [
        CalibrationItem(
            id=item["id"],
            verb=item["verb"],
            query=item["query"],
            response=item["response"],
            expected_substance=item["expected_substance"],
            grounding_snippet=item.get("grounding_snippet", ""),
            expected_score=item["expected_score"],
            category=item.get("category", ""),
        )
        for item in calibration_data
    ]

    cal_result = judge.run_calibration(calibration_items)
    report.calibration_passed = cal_result.passed
    report.calibration_details = cal_result.details

    if not cal_result.passed:
        log.error(
            "CALIBRATION FAILED — judge is not trustworthy. Relevance scores will NOT be reported."
        )
        if not cal_result.echo_guard_passed:
            log.error("ECHO GUARD FAILED: judge scored echo-answers > 1")
        return report

    log.info("Calibration PASSED. Proceeding with relevance grading.")

    # Phase 2: Check ingestion
    log.info("Phase 2: Checking ingestion status...")
    ingestion_status = check_ingestion(config, corpus)
    not_indexed = [name for name, status in ingestion_status.items() if not status]

    # Phase 3: Grade each annotated question
    annotated_questions = [q for q in golden if q.id in annotations]
    log.info("Phase 3: Grading %d annotated questions...", len(annotated_questions))

    for question in annotated_questions:
        if question.repo in not_indexed:
            log.info("Skipping %s (repo %s not indexed)", question.id, question.repo)
            continue

        annotation = annotations[question.id]
        report.total += 1

        try:
            arguments = build_query_arguments(question)
            if config.eval_mode == "direct":
                response = query_direct(config, question.verb, arguments)
            else:
                response = query_mcp(config, question.verb, arguments)

            judge_result = judge.grade(
                query=question.query,
                verb=question.verb,
                response=response,
                expected_substance=annotation["expected_substance"],
                grounding_snippet=annotation.get("grounding_snippet", ""),
            )

            result_entry = {
                "id": question.id,
                "repo": question.repo,
                "verb": question.verb,
                "query": question.query,
                "score": judge_result.score,
                "justification": judge_result.justification,
                "precision": judge_result.precision,
                "recall": judge_result.recall,
                "pass_threshold": annotation.get("pass_threshold", 2),
                "passed": judge_result.score >= annotation.get("pass_threshold", 2),
                "error": judge_result.error,
            }
            report.results.append(result_entry)

            # Per-verb aggregation
            verb_stats = report.by_verb.setdefault(
                question.verb,
                {"scores": [], "passed": 0, "failed": 0, "precisions": [], "recalls": []},
            )
            verb_stats["scores"].append(judge_result.score)
            if result_entry["passed"]:
                verb_stats["passed"] += 1
            else:
                verb_stats["failed"] += 1
            if judge_result.precision is not None:
                verb_stats["precisions"].append(judge_result.precision)
            if judge_result.recall is not None:
                verb_stats["recalls"].append(judge_result.recall)

        except Exception as e:
            log.error("Error grading %s: %s", question.id, e)
            report.results.append(
                {
                    "id": question.id,
                    "repo": question.repo,
                    "verb": question.verb,
                    "query": question.query,
                    "score": 0,
                    "justification": "",
                    "precision": None,
                    "recall": None,
                    "pass_threshold": annotation.get("pass_threshold", 2),
                    "passed": False,
                    "error": str(e),
                }
            )

    # Phase 4: Task scenarios
    log.info("Phase 4: Running %d end-to-end task scenarios...", len(tasks_data))
    for task in tasks_data:
        if task["repo"] in not_indexed:
            log.info("Skipping task %s (repo %s not indexed)", task["id"], task["repo"])
            continue

        combined_responses: list[dict] = []
        task_error = ""

        for step in task["steps"]:
            try:
                # Build a minimal GoldenQuestion-like object for argument building
                step_question = GoldenQuestion(
                    id=f"{task['id']}-{step['verb']}",
                    repo=task["repo"],
                    verb=step["verb"],
                    query=step["query"],
                    expected={},
                    pass_criterion="",
                )
                arguments = build_query_arguments(step_question)
                if config.eval_mode == "direct":
                    resp = query_direct(config, step["verb"], arguments)
                else:
                    resp = query_mcp(config, step["verb"], arguments)
                combined_responses.append({"verb": step["verb"], "response": resp})
            except Exception as e:
                task_error = f"Step {step['verb']} failed: {e}"
                break

        if task_error:
            report.tasks.append(
                {
                    "id": task["id"],
                    "description": task["description"],
                    "score": 0,
                    "justification": "",
                    "passed": False,
                    "error": task_error,
                }
            )
            continue

        # Judge the combined responses
        combined_text = json.dumps(combined_responses, indent=2, default=str)
        judge_result = judge.grade(
            query=task["description"],
            verb="task",
            response=combined_text,
            expected_substance=task["expected_substance"],
            grounding_snippet="",
        )

        report.tasks.append(
            {
                "id": task["id"],
                "description": task["description"],
                "score": judge_result.score,
                "justification": judge_result.justification,
                "passed": judge_result.score >= task.get("pass_threshold", 2),
                "error": judge_result.error,
            }
        )

    return report


def print_relevance_report(report: RelevanceReport, format: str = "text") -> None:
    """Print the relevance evaluation report."""
    if format == "json":
        _print_relevance_json(report)
    else:
        _print_relevance_text(report)


def _print_relevance_text(report: RelevanceReport) -> None:
    """Print a human-readable relevance report."""
    print("\n" + "=" * 70)
    print("  KNOWLEDGE LAYER RELEVANCE EVALUATION REPORT")
    print("=" * 70)

    # Calibration status
    cal_status = "PASSED" if report.calibration_passed else "FAILED"
    print(f"\n  Calibration: {cal_status}")
    if not report.calibration_passed:
        print("  *** EVAL INVALID — judge failed calibration ***")
        for detail in report.calibration_details:
            status = "OK" if detail["within_tolerance"] else "FAIL"
            print(
                f"    [{detail['id']}] ({detail['category']}): "
                f"expected={detail['expected_score']}, actual={detail['actual_score']} [{status}]"
            )
        print("=" * 70 + "\n")
        return

    for detail in report.calibration_details:
        status = "OK" if detail["within_tolerance"] else "FAIL"
        print(
            f"    [{detail['id']}] ({detail['category']}): "
            f"expected={detail['expected_score']}, actual={detail['actual_score']} [{status}]"
        )

    # Overall stats
    print(f"\n  Total graded:      {report.total}")
    print(f"  Mean score:        {report.overall_mean_score:.2f} / 3.0")
    passed_count = sum(1 for r in report.results if r["passed"])
    print(f"  Pass rate (>= threshold): {passed_count}/{report.total}")
    print()

    # Per-verb breakdown
    print("  Per-verb scores:")
    print("  " + "-" * 60)
    for verb in sorted(report.by_verb.keys()):
        stats = report.by_verb[verb]
        scores = stats["scores"]
        mean = sum(scores) / len(scores) if scores else 0.0
        pass_rate = (
            stats["passed"] / (stats["passed"] + stats["failed"])
            if (stats["passed"] + stats["failed"]) > 0
            else 0.0
        )
        line = f"    {verb:<20} mean={mean:.2f}  pass_rate={pass_rate:.0%}"
        if stats["precisions"]:
            avg_prec = sum(stats["precisions"]) / len(stats["precisions"])
            line += f"  precision={avg_prec:.2f}"
        if stats["recalls"]:
            avg_rec = sum(stats["recalls"]) / len(stats["recalls"])
            line += f"  recall={avg_rec:.2f}"
        print(line)
    print()

    # Worst 5
    worst = report.worst_n(5)
    if worst:
        print("  Worst 5 scores (actionable failures):")
        print("  " + "-" * 60)
        for r in worst:
            print(f"    [{r['id']}] score={r['score']} | {r['verb']} | {r['repo']}")
            if r["justification"]:
                # Truncate long justifications
                justification = r["justification"][:120]
                print(f"      Judge: {justification}")
            print()

    # Task scenarios
    if report.tasks:
        print("  End-to-end task scenarios:")
        print("  " + "-" * 60)
        for task in report.tasks:
            status = "PASS" if task["passed"] else "FAIL"
            print(f"    [{task['id']}] {status} (score={task['score']})")
            print(f"      {task['description']}")
            print()

    print("=" * 70 + "\n")


def _print_relevance_json(report: RelevanceReport) -> None:
    """Print a machine-readable JSON relevance report."""
    # Compute per-verb summaries
    by_verb_summary = {}
    for verb, stats in report.by_verb.items():
        scores = stats["scores"]
        by_verb_summary[verb] = {
            "mean_score": sum(scores) / len(scores) if scores else 0.0,
            "pass_rate": stats["passed"] / (stats["passed"] + stats["failed"])
            if (stats["passed"] + stats["failed"]) > 0
            else 0.0,
            "n": len(scores),
            "passed": stats["passed"],
            "failed": stats["failed"],
        }
        if stats["precisions"]:
            by_verb_summary[verb]["mean_precision"] = sum(stats["precisions"]) / len(
                stats["precisions"]
            )
        if stats["recalls"]:
            by_verb_summary[verb]["mean_recall"] = sum(stats["recalls"]) / len(stats["recalls"])

    output = {
        "relevance": {
            "calibration_passed": report.calibration_passed,
            "calibration_details": report.calibration_details,
            "total": report.total,
            "overall_mean_score": report.overall_mean_score,
            "by_verb": by_verb_summary,
            "worst_5": report.worst_n(5),
            "tasks": report.tasks,
            "results": report.results,
        }
    }
    print(json.dumps(output, indent=2, default=str))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the evaluation harness.

    Modes:
      --mode presence   (default) Hit-rate / retrievability scoring
      --mode relevance  LLM-as-judge quality grading (0-3)
    """
    import os

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Verify we're in a live environment
    test_env = os.environ.get("TEST_ENV", "unit")
    if test_env == "unit":
        log.error(
            "Evaluation requires a live environment. Set TEST_ENV=dev and ensure "
            "the MCP endpoint is reachable."
        )
        return 1

    # Parse --mode argument
    eval_mode = "presence"
    if "--mode" in sys.argv:
        idx = sys.argv.index("--mode")
        if idx + 1 < len(sys.argv):
            eval_mode = sys.argv[idx + 1]

    config = EvalConfig.from_env()
    log.info(
        "Eval config: mode=%s, eval_mode=%s, mcp_url=%s",
        eval_mode,
        config.eval_mode,
        config.mcp_url,
    )

    if eval_mode == "relevance":
        report = run_relevance_evaluation(config)
        print_relevance_report(report, config.report_format)

        if not report.calibration_passed:
            log.error("Relevance eval INVALID: calibration failed")
            return 1

        # Exit code: 0 if overall mean score >= 1.5 (configurable)
        threshold = float(os.environ.get("EVAL_RELEVANCE_THRESHOLD", "1.5"))
        if report.overall_mean_score < threshold:
            log.warning(
                "Mean relevance score %.2f below threshold %.2f",
                report.overall_mean_score,
                threshold,
            )
            return 1
        return 0

    else:
        # Default: presence/hit-rate mode (existing behavior)
        report = run_evaluation(config)
        print_report(report, config.report_format)

        threshold = float(os.environ.get("EVAL_PASS_THRESHOLD", "0.5"))
        if report.pass_rate < threshold:
            log.warning(
                "Pass rate %.1f%% below threshold %.1f%%",
                report.pass_rate * 100,
                threshold * 100,
            )
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
