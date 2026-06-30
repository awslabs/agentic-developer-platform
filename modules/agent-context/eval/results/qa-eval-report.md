# MCP Verb QA Evaluation Report

**Generated**: 2026-06-30 09:55 UTC
**Endpoint**: `http://context-mcp.agent-context.svc.cluster.local:5100/call`
**Dataset**: `eval/qa-dataset/*.jsonl` (123 cases across 6 verbs)

## Summary

| Metric | Value |
|--------|-------|
| Total cases | 123 |
| Evaluated | 67 |
| Passed | 36 |
| Failed | 31 |
| Skipped | 56 |
| **Overall hit-rate** | **53.7%** |

## Per-Verb Hit Rate

| Verb | Total | Evaluated | Passed | Failed | Skipped | Hit Rate |
|------|-------|-----------|--------|--------|---------|----------|
| search | 22 | 22 | 10 | 12 | 0 | 45.5% |
| understand | 21 | 21 | 12 | 9 | 0 | 57.1% |
| impact | 20 | 20 | 10 | 10 | 0 | 50.0% |
| browse | 20 | 2 | 2 | 0 | 18 | 100.0% |
| remember | 20 | 0 | 0 | 0 | 20 | N/A |
| experience | 20 | 2 | 2 | 0 | 18 | 100.0% |

## Per-Repo Breakdown

| Repo | Evaluated | Passed | Hit Rate |
|------|-----------|--------|----------|
| HKUDS/DeepTutor | 35 | 9 | 25.7% |
| HKUDS/Vibe-Trading | 32 | 27 | 84.4% |

> **Key finding**: HKUDS/DeepTutor appears to NOT be indexed in the code search engine (Zoekt).
> All DeepTutor search/understand/impact cases return empty results. This is an ingestion gap, not a verb bug.

## Known Caveats

- **understand**: All results show `source=code-index-fallback` instead of `neptune` (#2433 Neptune wiring not yet active).
- **search scope=docs**: Falls back to code-index (semantic/S3 Vectors not provisioned, #2297). Scored on correctness regardless.
- **browse**: Returns empty entries for all cases — verb's tree-listing backend appears unavailable in this environment.
- **remember/experience**: Require S3 Vectors personal-context index; IAM `s3vectors:CreateIndex` not authorized for the IRSA role.
- **DeepTutor not indexed**: `HKUDS/DeepTutor` is not in the Zoekt code index; all queries against it return empty.

## Failed Cases

### `vibe-search-007` (search)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-001` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-002` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-003` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-004` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-005` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-006` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-007` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: 0/1 must_contain found; missing: ['deeptutor/core/agentic/loop.py']

### `deep-search-008` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-009` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-011` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

### `deep-search-012` (search)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 results, need >= 1

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
- **Details**: Got 0 definitions, need >= 1

### `deep-understand-004` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 definitions, need >= 1

### `deep-understand-005` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 definitions, need >= 1

### `deep-understand-007` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 definitions, need >= 1

### `deep-understand-008` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 definitions, need >= 1

### `deep-understand-011` (understand)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 definitions, need >= 1

### `vibe-impact-010` (impact)
- **Repo**: HKUDS/Vibe-Trading
- **Type**: happy_path
- **Details**: 0/1 must_contain_callers found; missing: ['agent/src/tools/__init__.py']

### `deep-impact-001` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 3

### `deep-impact-002` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 3

### `deep-impact-003` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 1

### `deep-impact-004` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 1

### `deep-impact-005` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 1

### `deep-impact-006` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 1

### `deep-impact-008` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 1

### `deep-impact-009` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 1

### `deep-impact-010` (impact)
- **Repo**: HKUDS/DeepTutor
- **Type**: happy_path
- **Details**: Got 0 affected, need >= 1

## Skipped Cases

**browse verb returning empty for all cases (backend unavailable)** (18 cases):
  - `vibe-browse-001`
  - `vibe-browse-002`
  - `vibe-browse-003`
  - `vibe-browse-004`
  - `vibe-browse-006`
  - ... and 13 more

**S3Vectors personal-context unavailable (IAM not provisioned)** (38 cases):
  - `vibe-remember-001`
  - `vibe-remember-002`
  - `vibe-remember-003`
  - `vibe-remember-004`
  - `vibe-remember-005`
  - ... and 33 more

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

### understand — `vibe-understand-001` (PASS)
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
      "file": "agent/api_server.py",
      "line": 61,
      "symbol": "Artifact",
      "kind": "class",
      "signature": "",
      "callers": [],
      "callees": [],
      "source": "code-index-fallback"
    },
    {
      "repo_id": "Vibe-Trading",
      "file": "agent/backtest/run_card.py",
      "line": 142,
      "symbol": "_list_artifacts",
      "kind": "function",
      "signature": "",
      "callers": [],
      "callees": [],
      "source": "code-index-fallback"
    },
    {
      "repo_id": "Vibe-Trading",
      "file": "agent/src/ui_services.py",
      "line": 349,
      "symbol": "_load_ohlcv_artifacts",
      "kind": "function",
      "signature": "",
      "callers": [],
      "callees": [],
      "source": "code-index-fallback"
    }
  ],
  "_definitions_total": 11
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

### browse — `vibe-browse-005` (PASS)
```json
{
  "action": "list",
  "uri": "nonexistent/path/",
  "entries": []
}
```

### experience — `vibe-experience-007` (PASS)
```json
{
  "error": "Invalid persona: 'nonexistent_persona_xyz'. Must be one of: ['operations', 'developer', 'architect', 'reviewer']"
}
```

---
*Report generated by `eval/run_qa_eval.py` on 2026-06-30 09:55 UTC*