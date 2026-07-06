# MCP Verb QA Evaluation Dataset

Ground-truth evaluation dataset for the 6 MCP verbs exposed by `context-mcp`.
Corpus: two public repos — **HKUDS/Vibe-Trading** and **HKUDS/DeepTutor**.

## Purpose

This dataset provides (input → expected-output) cases for automated evaluation
of the Knowledge Layer's MCP verbs. An eval harness (out of scope for this
dataset; see follow-up issues) loads these JSONL files, replays each input
against a live `context-mcp` instance, and scores responses against the expected
fields.

## Schema

Each line in a `.jsonl` file is a self-contained test case:

```json
{
  "id": "vibe-search-001",
  "repo": "HKUDS/Vibe-Trading",
  "verb": "search",
  "input": { ... },
  "expected": { ... },
  "case_type": "happy_path | edge",
  "source_evidence": "file:line — what was verified in the live clone"
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique case identifier: `{repo_prefix}-{verb}-{NNN}` |
| `repo` | string | Full repo name (`HKUDS/Vibe-Trading` or `HKUDS/DeepTutor`) |
| `verb` | string | One of: `search`, `understand`, `impact`, `browse`, `remember`, `experience` |
| `input` | object | The exact arguments to pass to the MCP verb |
| `expected` | object | What the response must satisfy (see Evaluation Logic below) |
| `case_type` | string | `happy_path` (should return results) or `edge` (should return empty / handle gracefully) |
| `source_evidence` | string | Citation: `file:line — description` proving the expected answer is correct |

### Per-verb input shapes

| Verb | Input fields |
|------|-------------|
| `search` | `query`, `scope` (code\|docs\|memory), `limit`, `project` |
| `understand` | `target` (symbol/file/repo::symbol), `depth` (overview\|detailed), `project` |
| `impact` | `target`, `cross_repo` (bool), `project` |
| `browse` | `action` (list\|read), `uri`, `depth`, `project` |
| `remember` | `session_id`, `messages` (array of {role, content}), `outcome` |
| `experience` | `action` (save\|recall), `persona`, `content`, `learning_type`, `context`, `query`, ... |

### Expected-output shapes

| Verb | Expected fields |
|------|----------------|
| `search` | `must_contain` (files/symbols that must appear), `min_results` |
| `understand` | `must_contain` (file + symbol + kind tuples), `min_results` |
| `impact` | `must_contain_callers` (files that reference the target), `min_results` |
| `browse` (list) | `must_contain_entries` (directory entries that must appear), `min_results` |
| `browse` (read) | `must_contain_content` (strings that must appear in content), `min_results` |
| `remember` | `must_save` (bool), `recall_query` (optional — for paired recall test) |
| `experience` (save) | `must_save` (bool) |
| `experience` (recall) | `must_contain_content`, `min_results` |

## Evaluation Logic (for a future harness)

Suggested scoring per case:

1. **For `search`/`understand`/`impact`**: check `min_results` threshold; for
   each item in `must_contain` / `must_contain_callers`, verify it appears in
   the response. Score = fraction of must_contain items found.

2. **For `browse`**: for `list` action, verify `must_contain_entries` appear in
   the response entries. For `read` action, verify `must_contain_content`
   strings appear in the returned content.

3. **For `remember`**: call the verb with the input; then issue a `search` with
   `scope=memory` using `recall_query` to verify the session was persisted.

4. **For `experience`**: `save` cases are setup; paired `recall` cases verify
   retrieval. Group by consecutive IDs (e.g., `-001` save, `-002` recall).

5. **Edge cases** (`case_type: "edge"`): verify `min_results: 0` — the response
   should be empty or an appropriate error, NOT hallucinated content.

## Files

| File | Verb | Cases |
|------|------|-------|
| `search.jsonl` | search | 22 |
| `understand.jsonl` | understand | 21 |
| `impact.jsonl` | impact | 20 |
| `browse.jsonl` | browse | 20 |
| `remember.jsonl` | remember | 20 |
| `experience.jsonl` | experience | 20 |
| **Total** | | **123** |

## How expected answers were derived

1. Both repos were cloned from GitHub at HEAD (public, no auth required):
   - `git clone https://github.com/HKUDS/Vibe-Trading`
   - `git clone https://github.com/HKUDS/DeepTutor`

2. Key source files, READMEs, and directory structures were read manually.

3. Each `source_evidence` field cites the exact file and line number verified
   in the live clone. No expected answers were invented — all are grounded in
   actual repo content.

4. Edge cases (nonexistent symbols, fake paths) verify the system returns empty
   results rather than hallucinating.

## Corpus summary

### HKUDS/Vibe-Trading
- **Purpose**: Personal trading agent with ReAct loop, backtesting engines, 18+ data loaders
- **Languages**: Python (agent backend) + TypeScript/React (frontend)
- **Key modules**: `agent/backtest/` (engines, loaders, models, metrics, runner), `agent/src/agent/` (AgentLoop, tools), `agent/api_server.py` (FastAPI), `frontend/src/` (React 19 SPA)

### HKUDS/DeepTutor
- **Purpose**: Agent-native personalized tutoring with multi-capability agentic engine
- **Languages**: Python (backend, 587 files) + TypeScript/Next.js (web)
- **Key modules**: `deeptutor/agents/` (chat, research, math_animator, question, notebook), `deeptutor/core/agentic/` (label-driven loop), `deeptutor/services/memory/` (three-layer memory), `deeptutor_cli/` (CLI interface)

## Limitations

- Dataset reflects repo state at time of authoring (2026-06-30). If repos
  receive significant refactors, line numbers in `source_evidence` may drift.
- `remember` and `experience` cases require a self-contained save→recall
  round-trip within a single eval run. They do NOT test pre-existing persisted
  state.
- Building the eval harness/runner itself is out of scope (separate follow-up).

## References

- Verb definitions: `modules/agent-context/door/mcp_app.py`
- Verb contracts: `docs/agent-context/verb-contracts.md`
- Related issues: #2400 (read-path), #2415 (deep-validation), #1888 (retrieval-quality)
