# Learnings: Issue #2445 — Ground-truth QA eval dataset for MCP verbs

## What worked

1. **Cloning repos for ground truth**: Using `git clone --depth=1` to get shallow clones was fast and sufficient for reading source code structure. Both repos are ~100KB of file listings.

2. **Systematic exploration pattern**: Starting with `find -type f -name "*.py"` then progressively drilling into specific files with `grep -n "class "` and `head -N` was efficient for building the mental map needed to author realistic test cases.

3. **JSONL-per-verb structure**: Having one file per verb made validation simple (`json.loads` per line) and keeps the dataset extensible — adding cases is just appending lines.

4. **Spot-checking source_evidence**: After writing all cases, going back and `grep -n` to verify line numbers caught no errors — the line numbers were stable across the session because the repos weren't changing.

## Key technical decisions

- **≥10 cases per repo per verb** ensures balanced coverage and prevents eval scores from being dominated by one corpus.
- **Edge cases (min_results: 0)** are critical for testing that the system doesn't hallucinate when asked about nonexistent symbols.
- **remember/experience use save→recall pairs**: The eval harness must run save cases before their paired recall cases to test the full round-trip.
- **source_evidence as plain string**: Kept it simple rather than structured (no separate file/line fields) since it's primarily for human verification.

## Gotchas

- The issue body mentioned `deeptutor_cli/` containing `ToolResultEntry` — this was correct. The index hint about "5000 symbols" was approximate but the structural claims checked out.
- `git add` from inside a subdirectory doesn't work if CWD is the target directory itself — always `cd /work/repo` first.
- `gh pr create` needs `--head` flag when the branch was already pushed in a separate command.

## Repo structure highlights (useful for future eval work)

### HKUDS/Vibe-Trading
- `agent/api_server.py` — FastAPI server with Pydantic models (Artifact, BacktestMetrics, RunResponse, etc.)
- `agent/backtest/` — Complete backtest system: engines (base.py + 8 market-specific), loaders (18 sources), models, metrics, runner
- `agent/src/agent/loop.py` — AgentLoop class (ReAct, 5-layer context management)
- `agent/src/tools/` — 40+ tool implementations
- `frontend/src/` — React 19 + TypeScript SPA with 9 pages

### HKUDS/DeepTutor
- `deeptutor/agents/` — Modular agents (chat, research, math_animator, question, notebook, vision_solver, visualize)
- `deeptutor/core/agentic/` — Label-driven iteration loop with LabelProtocol
- `deeptutor/services/memory/` — Three-layer memory (L1 trace, L2/L3 documents, consolidator)
- `deeptutor_cli/` — CLI with ToolResultBuffer for /show command
- `web/` — Next.js 16 frontend

## Recommendations

- When the eval harness is built, it should group remember/experience cases by session_id for correct sequencing.
- If repos get significant refactors, line numbers in source_evidence may drift — consider a periodic refresh script that re-verifies citations.
- The dataset could be extended with more cross-repo impact cases (using `cross_repo: true`) once that feature is mature.
