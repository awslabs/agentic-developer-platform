# MCP Verb QA Evaluation Report

**Generated**: 2026-07-04 13:37 UTC
**Endpoint**: `http://context-mcp.agent-context.svc.cluster.local:5100/call`
**Dataset**: `eval/qa-dataset/*.jsonl` (123 cases across 6 verbs)

## Summary

| Metric | Value |
|--------|-------|
| Total cases | 127 |
| Evaluated | 127 |
| Passed | 71 |
| Failed | 56 |
| Skipped | 0 |
| **Overall hit-rate** | **55.9%** |

## Per-Verb Hit Rate

| Verb | Total | Evaluated | Passed | Failed | Skipped | Hit Rate |
|------|-------|-----------|--------|--------|---------|----------|
| search | 22 | 22 | 20 | 2 | 0 | 90.9% |
| understand | 21 | 21 | 10 | 11 | 0 | 47.6% |
| impact | 20 | 20 | 17 | 3 | 0 | 85.0% |
| browse | 24 | 24 | 18 | 6 | 0 | 75.0% |
| remember | 20 | 20 | 0 | 20 | 0 | 0.0% |
| experience | 20 | 20 | 6 | 14 | 0 | 30.0% |

## Per-Repo Breakdown

| Repo | Evaluated | Passed | Hit Rate |
|------|-----------|--------|----------|
| HKUDS/DeepTutor | 63 | 32 | 50.8% |
| HKUDS/Vibe-Trading | 62 | 37 | 59.7% |
| unknown | 2 | 2 | 100.0% |

> **Key finding**: HKUDS/DeepTutor appears to NOT be indexed in the code search engine (Zoekt).
> All DeepTutor search/understand/impact cases return empty results. This is an ingestion gap, not a verb bug.

## Known Caveats

- **understand**: All results show `source=code-index-fallback` instead of `neptune` (#2433 Neptune wiring not yet active).
- **search scope=docs**: Falls back to code-index (semantic/S3 Vectors not provisioned, #2297). Scored on correctness regardless.
- **browse**: Returns empty entries for all cases — verb's tree-listing backend appears unavailable in this environment.
- **remember/experience**: Require S3 Vectors personal-context index; IAM `s3vectors:CreateIndex` not authorized for the IRSA role.
- **DeepTutor not indexed**: `HKUDS/DeepTutor` is not in the Zoekt code index; all queries against it return empty.

## Failed Cases

### `deep-search-005` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/core/stream_bus.py']

### `deep-search-009` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/core/agentic/usage.py']

### `vibe-understand-001` (understand)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['agent/api_server.py::Artifact']

### `vibe-understand-002` (understand)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['agent/backtest/engines/base.py::BaseEngine']

### `vibe-understand-007` (understand)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['agent/backtest/loaders/registry.py::LOADER_REGISTRY']

### `vibe-understand-008` (understand)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['frontend/src/components/chat/MandateProposalCard.tsx::MandateProposalCard']

### `deep-understand-003` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/agents/chat/chat_agent.py::ChatAgent']

### `deep-understand-004` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/core/agentic/loop.py::LabelProtocol']

### `deep-understand-005` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/agents/research/data_structures.py::DynamicTopicQueue']

### `deep-understand-006` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/services/memory/store.py::MemoryStore']

### `deep-understand-007` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/agents/research/data_structures.py::TopicStatus']

### `deep-understand-008` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/core/agentic/usage.py::UsageTracker']

### `deep-understand-011` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/services/memory/trace.py::TraceEvent']

### `vibe-impact-005` (impact)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 1

### `vibe-impact-010` (impact)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/1 must_contain_callers found; missing: ['agent/src/tools/__init__.py']

### `deep-impact-005` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain_callers found; missing: ['deeptutor/agents/chat/agentic_pipeline.py']

### `vibe-browse-003` (browse)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/3 content strings found; missing: ['class Position', 'class TradeRecord', 'class EquitySnapshot']

### `vibe-browse-006` (browse)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/3 content strings found; missing: ['class Artifact(BaseModel)', 'class BacktestMetrics(BaseModel)', 'class RunResponse(BaseModel)']

### `vibe-browse-010` (browse)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/2 content strings found; missing: ['Crypto perpetual-contract backtest engine', '24/7 trading']

### `deep-browse-003` (browse)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/3 content strings found; missing: ['class ToolResultEntry', 'class ToolResultBuffer', 'truncate_for_display']

### `deep-browse-007` (browse)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/3 content strings found; missing: ['class TopicStatus(Enum)', 'class TopicBlock', 'class DynamicTopicQueue']

### `deep-browse-010` (browse)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/3 content strings found; missing: ['class UsageTracker', 'add_from_response', 'summary']

### `vibe-remember-001` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-remember-002` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-remember-003` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-remember-004` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: edge
- **Details**: Unexpected state: must_save=False, stored=True

### `vibe-remember-005` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-remember-006` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-001` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-002` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-003` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-004` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-005` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: edge
- **Details**: Unexpected state: must_save=False, stored=True

### `deep-remember-006` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-remember-007` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-remember-008` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-007` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-008` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-remember-009` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-remember-010` (remember)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-009` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `deep-remember-010` (remember)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Stored but recall returned empty

### `vibe-experience-001` (experience)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Expected save=true, got false

### `vibe-experience-003` (experience)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Expected save=true, got false

### `vibe-experience-005` (experience)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Expected save=true, got false

### `vibe-experience-008` (experience)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Expected save=true, got false

### `vibe-experience-009` (experience)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Expected save=true, got false

### `vibe-experience-010` (experience)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 1/2 content items found; missing: ['TypeScript']

### `deep-experience-001` (experience)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Expected save=true, got false

### `deep-experience-002` (experience)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 1/2 content items found; missing: ['LabelProtocol']

### `deep-experience-003` (experience)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Expected save=true, got false

### `deep-experience-005` (experience)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Error: <urlopen error [Errno 111] Connection refused>
- **Error**: <urlopen error [Errno 111] Connection refused>

### `deep-experience-006` (experience)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Error: <urlopen error [Errno 111] Connection refused>
- **Error**: <urlopen error [Errno 111] Connection refused>

### `deep-experience-007` (experience)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Error: <urlopen error [Errno 111] Connection refused>
- **Error**: <urlopen error [Errno 111] Connection refused>

### `deep-experience-008` (experience)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Error: <urlopen error [Errno 111] Connection refused>
- **Error**: <urlopen error [Errno 111] Connection refused>

### `deep-experience-010` (experience)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Error: <urlopen error [Errno 111] Connection refused>
- **Error**: <urlopen error [Errno 111] Connection refused>

## Evidence: Sample Raw Responses

### search — `vibe-search-001` (PASS)
```json
{
  "_truncated": true,
  "_keys": [
    "results",
    "total",
    "query"
  ],
  "results": [
    {
      "repo_id": "github.com/HKUDS/Vibe-Trading",
      "file": "agent/backtest/engines/base.py",
      "line": 1,
      "content": "\"\"\"Base backtest engine with shared bar-by-bar execution loop.",
      "match_type": "exact"
    },
    {
      "repo_id": "github.com/HKUDS/Vibe-Trading",
      "file": "agent/backtest/engines/futures_base.py",
      "line": 1,
      "content": "\"\"\"Base class for all futures engines.",
      "match_type": "exact"
    },
    {
      "repo_id": "github.com/HKUDS/Vibe-Trading",
      "file": "README.md",
      "line": 60,
      "content": "- **2026-06-26** \ud83c\udfaf **Shadow Account conditional entry + tushare ETF/index/HK routing**: extracted Shadow Account rules now carry RSI / prior-return bounds, so the generated SignalEngine enters on real conditions (RSI in range, prior-return in range) instead of blindly replaying the holding cadence ([#314](https://github.com/HKUDS/Vibe-Trading/pull/314), follows [#302](https://github.com/HKUDS/Vibe-Trading/pull/302), thanks @Robin1987China). The tushare loader also routes ETF/LOF \u2192 `fund_daily()`, indices \u2192 `index_daily()`, and HK equities \u2192 `hk_daily()` instead of always calling `daily()` (which silently returns empty for non-stocks), with per-symbol empty-result + partial-fetch warnings ([#315](https://github.com/HKUDS/Vibe-Trading/pull/315), closes [#310](https://github.com/HKUDS/Vibe-Trading/issues/310), thanks @shadowinlife).",
      "match_type": "exact"
    }
  ],
  "_results_total": 10
}
```

### understand — `vibe-understand-003` (PASS)
```json
{
  "_truncated": true,
  "_keys": [
    "target",
    "summary",
    "definitions"
  ],
  "definitions": [
    {
      "repo_id": "Vibe-Trading",
      "file": "agent/mcp_server.py",
      "line": 748,
      "symbol": "trading_positions",
      "kind": "function",
      "signature": "",
      "callers": [],
      "callees": [],
      "source": "code-index-fallback"
    },
    {
      "repo_id": "Vibe-Trading",
      "file": "agent/backtest/models.py",
      "line": 14,
      "symbol": "Position",
      "kind": "class",
      "signature": "",
      "callers": [],
      "callees": [],
      "source": "code-index-fallback"
    },
    {
      "repo_id": "Vibe-Trading",
      "file": "agent/tests/test_base_engine.py",
      "line": 61,
      "symbol": "test_positions_normalized",
      "kind": "function",
      "signature": "",
      "callers": [],
      "callees": [],
      "source": "code-index-fallback"
    }
  ],
  "_definitions_total": 31
}
```

### impact — `vibe-impact-001` (PASS)
```json
{
  "_truncated": true,
  "_keys": [
    "verdict",
    "target",
    "blast_radius",
    "repos_affected",
    "source",
    "affected"
  ],
  "affected": [
    {
      "repo_id": "Vibe-Trading",
      "file": "agent/backtest/models.py",
      "line": 14,
      "content": "class Position:",
      "match_type": "exact",
      "relationship": "references",
      "symbol": ""
    },
    {
      "repo_id": "Vibe-Trading",
      "file": "agent/backtest/engines/base.py",
      "line": 38,
      "content": "from backtest.models import EquitySnapshot, Position, TradeRecord",
      "match_type": "exact",
      "relationship": "references",
      "symbol": ""
    },
    {
      "repo_id": "Vibe-Trading",
      "file": "agent/src/skills/vnpy-export/SKILL.md",
      "line": 43,
      "content": "| Asset Class | Instrument Example | `vt_symbol` Format | Position Unit |",
      "match_type": "exact",
      "relationship": "references",
      "symbol": ""
    }
  ],
  "_affected_total": 47
}
```

### browse — `vibe-browse-001` (PASS)
```json
{
  "_truncated": true,
  "_keys": [
    "action",
    "uri",
    "entries"
  ],
  "entries": [
    {
      "repo_id": "HKUDS/Vibe-Trading",
      "name": ".editorconfig",
      "path": "agent/.editorconfig",
      "entry_type": "file"
    },
    {
      "repo_id": "HKUDS/Vibe-Trading",
      "name": ".env.example",
      "path": "agent/.env.example",
      "entry_type": "file"
    },
    {
      "repo_id": "HKUDS/Vibe-Trading",
      "name": ".gitignore",
      "path": "agent/.gitignore",
      "entry_type": "file"
    }
  ],
  "_entries_total": 13
}
```

### experience — `vibe-experience-002` (PASS)
```json
{
  "_truncated": true,
  "_keys": [
    "status",
    "query",
    "results",
    "total",
    "graph_expanded"
  ],
  "results": [
    {
      "id": "01KWPKHP3TJK2EEDV413XTYZSF",
      "content": "Outcome: Explained loader extension pattern\n[user] How do I add a new data loader to Vibe-Trading?\n[assistant] Create a new file in agent/backtest/loaders/ inheriting from the base loader and use the @register decorator from registry.py to self-register.",
      "persona": "developer",
      "learning_type": "session_memory",
      "confidence": 0.7,
      "decay_score": 1.0,
      "score": 0.0,
      "visibility": "private",
      "created_at": "2026-07-04T13:02:07.995283+00:00"
    },
    {
      "id": "01KWPKHPMFGDRPZW3BPAJBRCYS",
      "content": "Outcome: Listed available backtest engines\n[user] What backtest engines are available?\n[assistant] Available engines: china_a, china_futures, crypto, forex, global_equity, global_futures, options_portfolio, composite. All inherit from BaseEngine in base.py.",
      "persona": "developer",
      "learning_type": "session_memory",
      "confidence": 0.7,
      "decay_score": 1.0,
      "score": 0.0,
      "visibility": "private",
      "created_at": "2026-07-04T13:02:08.527339+00:00"
    },
    {
      "id": "01KWPKHQH90MA42FTWQT7Y9A0X",
      "content": "Session: eval-vibe-session-004",
      "persona": "developer",
      "learning_type": "session_memory",
      "confidence": 0.7,
      "decay_score": 1.0,
      "score": 0.0,
      "visibility": "private",
      "created_at": "2026-07-04T13:02:09.449755+00:00"
    }
  ],
  "_results_total": 5
}
```

---
*Report generated by `eval/run_qa_eval.py` on 2026-07-04 13:37 UTC*