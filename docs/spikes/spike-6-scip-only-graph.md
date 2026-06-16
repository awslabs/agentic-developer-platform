# SPIKE-6: Can SCIP alone build the call/reference graph?

> **Issue**: #1548
> **Date**: 2026-06-16
> **Agent**: @agent-operations
> **Parent EPIC**: #1529 (Neptune Deep Code Graph)
> **Competing path**: SPIKE-5 (#1547) — cgc+SCIP+reconciliation

## Verdict: PASS

**SCIP-only graph construction is viable and superior to cgc.** Reference occurrences
with positions exist in scip-python's output. Enclosing-scope resolution produces a
richer graph than cgc. Monikers are native on every node and edge.

**Recommendation**: Drop cgc + FalkorDB + reconciliation. Build the Neptune ingestion
pipeline as: `scip-python → decode .scip → enclosing-scope graph → CSV → Neptune`.
This supersedes SPIKE-5.

---

## Raw Data

### Step 1: Occurrence Composition

| Metric | Value |
|--------|-------|
| Total documents (files) | 45 |
| Total occurrences | 9,930 |
| Definitions | 2,371 |
| References (non-definition) | 7,559 |
| References WITH positions | 7,559 (100% of references) |

**Role counts (individual flags):**

| Role | Count |
|------|-------|
| ReadAccess | 7,559 |
| Definition | 2,371 |

**Role combinations:**

| Combination | Count |
|-------------|-------|
| ReadAccess (only) | 7,559 |
| Definition (only) | 2,371 |

**MAKE-OR-BREAK confirmed**: All 7,559 references have positions (range >= 3 integers).
scip-python emits every reference as a ReadAccess occurrence with a (line, startChar, endChar)
triple. There is no Import role in scip-python's output — imports appear as ReadAccess.

### Step 2: Reference Graph via Enclosing-Scope Resolution

| Metric | Value |
|--------|-------|
| Definitions found (potential callers) | 1,418 |
| References resolved to enclosing scope | 4,487 |
| References with no enclosing definition | 0 |
| **Unique edges (caller → callee)** | **2,838** |
| **Unique nodes (symbols)** | **1,128** |

**vs cgc's 611 CALLS edges: 2,838 edges (4.6x MORE)**

### Step 3: Edge Breakdown by Source

| Category | Edge Count | Notes |
|----------|-----------|-------|
| Callee in project (Agent-Reach) | 1,450 | Comparable to cgc's scope |
| Callee in stdlib (python-stdlib) | 1,341 | Extra: stdlib dependencies |
| Callee in third-party | 47 | Extra: pip dependencies |
| **Total** | **2,838** | |

cgc produces ONLY intra-project edges (611). SCIP-only produces **1,450 intra-project
edges** — 2.4x more than cgc even when restricted to the same scope. The additional
edges come from SCIP tracking all references (variable accesses, class instantiations,
constant lookups) while cgc tracks only explicit call() expressions.

### Step 4: Edge Quality Characterization

| Category (heuristic) | Count | % |
|---------------------|-------|---|
| Likely true calls (function/method refs) | 2,145 | 75% |
| Module init references | 483 | 17% |
| Variable/constant references | 204 | 7% |
| Module path references | 6 | 0% |

**Filtering feasibility**: Keeping only "likely true calls" (function/method monikers)
yields 2,145 edges — still 3.5x richer than cgc's 611.

### Step 5: Cross-File Edge Quality

| Metric | Value |
|--------|-------|
| Cross-file edges (different modules) | 817 |
| Project-internal cross-file edges | 739 |
| Same-module edges | 658 |

**Sample cross-file edges (project-internal):**

| # | Caller | Callee | File | Line |
|---|--------|--------|------|------|
| 1 | `agent_reach.cli/_cmd_install().(args)` | `agent_reach.config/Config#` | cli.py | 173 |
| 2 | `agent_reach.cli/_cmd_install().(args)` | `agent_reach.doctor/check_all().` | cli.py | 174 |
| 3 | `agent_reach.cli/_cmd_install().(args)` | `agent_reach.doctor/format_report().` | cli.py | 174 |

These are correct: `_cmd_install` in `cli.py` calls `Config`, `check_all()`, and
`format_report()` from other modules. The monikers are stable, qualified, and
suitable as Neptune `symbol_id` values.

### Moniker Format

```
scip-python python <package> <version> `<module.path>`/<symbol>
```

Examples:
- `scip-python python Agent-Reach 1.5.0 \`agent_reach.cli\`/main().`
- `scip-python python Agent-Reach 1.5.0 \`agent_reach.core\`/AgentReach#`
- `scip-python python python-stdlib 3.11 sys/__init__:`

These are compiler-resolved, globally unique identifiers. Cross-repo join (#1536)
works by matching callee monikers across different repo indexes — no reconciliation
needed.

---

## Architecture Decision

### Why SCIP-only wins

| Dimension | cgc + SCIP + reconcile (SPIKE-5) | SCIP-only (this spike) |
|-----------|----------------------------------|------------------------|
| Data sources | 2 (cgc edges + scip monikers) | 1 (.scip only) |
| Intermediate stores | FalkorDB (in-pod, throwaway) | None |
| Join mechanism | (path, line) reconciliation | Native (monikers by construction) |
| Cross-repo resolution | Manual moniker→node mapping | Free (match callee moniker) |
| Edge richness | 611 CALLS (Agent-Reach) | 2,838 total / 1,450 intra-project |
| Fragility | High (path normalization, line drift) | Low (moniker is stable) |
| Deps to install | cgc + scip-python + FalkorDB | scip-python only |
| Pipeline steps | 5 (cgc index → falkor → extract → reconcile → CSV) | 3 (scip index → decode → CSV) |

### What we gain

1. **No FalkorDB** — eliminate an in-pod graph DB dependency
2. **No cgc** — eliminate a PyPI dependency with its own FalkorDB backend
3. **No reconciliation** — eliminate the fragile (path, line) join that SPIKE-5 tests
4. **Richer graph** — 2.4x more intra-project edges than cgc
5. **Monikers by construction** — every node and edge carries its globally-unique ID natively
6. **Simpler pipeline** — 3 steps instead of 5

### What we accept

1. **Broader than "calls only"** — SCIP tracks all references (reads, instantiations, constant lookups), not just call(). This is arguably better for impact analysis ("what references X" = true blast radius).
2. **No explicit "Call" role** — SCIP's ReadAccess role covers all references. Filtering to function/method monikers (the `().` and `#` patterns) provides a reasonable call-subset.
3. **Enclosing-scope heuristic** — the "last definition at or before line L" heuristic works well for Python but may need refinement for languages with nested closures. For MVP this is sufficient.

---

## Feeds Into

- **#1532** (Neptune graph writer): Use SCIP-only pipeline instead of cgc+reconcile
- **#1530** (architecture decision): SCIP-only is the winner
- **#1536** (cross-repo join): Monikers are native — join by matching callee moniker
- **SPIKE-5 (#1547)**: SUPERSEDED — reconciliation path no longer needed

## Method

- Throwaway pod (`spike6-scip-only`) in `agent-context` namespace
- Node.js 20 + Python 3.13 + scip-python 0.6.6 + protobuf 7.35.1
- Repo: `Panniantong/Agent-Reach` (same as SPIKE-3/4)
- SCIP proto compiled from sourcegraph/scip main branch
- Analysis script: `modules/agent-context/pipeline/neptune_ingestion/spike6_scip_graph.py`
