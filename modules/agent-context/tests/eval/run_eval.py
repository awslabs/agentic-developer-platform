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
                resp = httpx.post(
                    f"{config.mcp_url}/call",
                    json={
                        "name": "browse",
                        "arguments": {"action": "ls", "uri": f"/{repo.name}"},
                    },
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

    resp = httpx.post(
        f"{config.mcp_url}/call",
        json={"name": tool_name, "arguments": arguments},
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
    """Score a search result: expected file in top-K."""
    results = response.get("results", [])
    expected_files = question.expected.get("files", [])
    expected_content = question.expected.get("content", [])

    # Check if any expected file appears in top-K results
    found = False
    for result in results[: config.top_k]:
        result_file = result.get("file", "") or result.get("path", "")
        result_content = result.get("content", "")

        # File match
        for exp_file in expected_files:
            if exp_file in result_file:
                found = True
                break

        # Content match (for cases where we expect specific text)
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
        response=results[:3],  # store top 3 for review
    )


def _score_understand(
    question: GoldenQuestion, response: dict[str, Any], config: EvalConfig
) -> EvalResult:
    """Score an understand result: key facts present in response."""
    expected_facts = question.expected.get("key_facts", [])
    expected_location = question.expected.get("location", "")

    response_text = json.dumps(response).lower()

    # Check location mentioned
    location_found = not expected_location or expected_location.lower() in response_text

    # Check key facts
    facts_found = 0
    for fact in expected_facts:
        if fact.lower() in response_text:
            facts_found += 1

    # Pass if location found AND majority of facts found
    min_facts = max(1, len(expected_facts) // 2)
    passed = location_found and facts_found >= min_facts
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
        response=response,
    )


def _score_impact(
    question: GoldenQuestion, response: dict[str, Any], config: EvalConfig
) -> EvalResult:
    """Score an impact result: expected callers/dependents in result set."""
    expected_affected = question.expected.get("affected", [])

    affected_results = response.get("affected", [])
    response_text = json.dumps(affected_results).lower()

    found = 0
    for expected in expected_affected:
        if expected.lower() in response_text:
            found += 1

    min_expected = max(1, len(expected_affected) // 2)
    passed = found >= min_expected
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
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the evaluation harness."""
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

    config = EvalConfig.from_env()
    log.info("Eval config: mode=%s, mcp_url=%s", config.eval_mode, config.mcp_url)

    report = run_evaluation(config)
    print_report(report, config.report_format)

    # Exit code: 0 if pass rate >= 50% (configurable threshold)
    threshold = float(os.environ.get("EVAL_PASS_THRESHOLD", "0.5"))
    if report.pass_rate < threshold:
        log.warning(
            "Pass rate %.1f%% below threshold %.1f%%", report.pass_rate * 100, threshold * 100
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
