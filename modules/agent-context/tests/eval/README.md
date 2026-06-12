# Knowledge Layer Evaluation Harness

Measures whether the Knowledge Layer produces **correct, relevant answers on real code** — by indexing a fixed 15-repo corpus, querying all MCP verbs against a golden-answer dataset, and scoring the results automatically.

This complements the unit/component test suite (see `../` and `TESTING.md`) which proves plumbing correctness. This harness proves **answer quality**.

## Quick Start

```bash
# Prerequisites: deployed agent-context module with MCP endpoint reachable
cd modules/agent-context

# 1. Add corpus repos to the index
cp tests/eval/corpus-repos.txt index_content/repos-eval.txt
# OR append to existing repos.txt (see "Adding Corpus Repos" below)

# 2. Trigger ingestion
./scripts/ingest-repos.sh  # or manually via kubectl

# 3. Verify ingestion completed
TEST_ENV=dev python -m tests.eval.run_eval --check-only  # ingestion check only

# 4. Run full evaluation
TEST_ENV=dev python -m tests.eval.run_eval

# 5. View results (JSON for CI)
TEST_ENV=dev REPORT_FORMAT=json python -m tests.eval.run_eval
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  run_eval.py                                                     │
│                                                                   │
│  1. Load corpus.yaml (15 repos)                                  │
│  2. Load golden.yaml (~75 questions)                             │
│  3. Check ingestion status (browse each repo)                    │
│  4. For each question:                                           │
│     a. Build query arguments for the MCP verb                    │
│     b. POST to MCP endpoint (/call)                              │
│     c. Score response against golden answer                      │
│  5. Emit per-verb hit-rate report                                │
└──────────────────────────────┬────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  MCP Endpoint (context-mcp service, port 5100)                   │
│                                                                   │
│  Verbs tested:                                                    │
│    search  — exact (Zoekt) + semantic (S3 Vectors)               │
│    understand — structural index (S3 JSON)                        │
│    impact  — call graph / dependency analysis                     │
│    browse  — file/directory navigation                            │
└─────────────────────────────────────────────────────────────────┘
```

**Query path:** The harness queries the **MCP endpoint** (`/call`) for all verbs. This is the production query path through the Door, with ACL filtering. If the MCP endpoint is not yet wired for a verb, the harness will report errors for those questions (not silent failures).

If you need to test backends directly (e.g., Zoekt before the Door is wired), set `EVAL_MODE=direct` — but note this bypasses ACL filtering and is not the production path.

## Prerequisites

| Requirement | How to verify | Notes |
|-------------|---------------|-------|
| Deployed agent-context module | `kubectl get deploy -n agent-context` | All pods Running |
| MCP endpoint reachable | `curl -sf http://context-mcp.agent-context.svc:5100/tools` | Returns tool list |
| S3 Vectors index exists | Check via AWS Console or `aws s3vectors list-indexes` | `agent-context-code-embeddings` |
| Postgres schema applied | Alembic migrations up to date | `alembic current` shows head |
| Zoekt running | `curl -sf http://zoekt.agent-context.svc:6070/` | Returns 200 |
| GitHub access (for clone) | All 15 repos are **public** — no token needed | Anonymous clone OK |
| Python 3.11+ | `python3 --version` | With `pyyaml`, `httpx` installed |

## Adding Corpus Repos to the Index

### Option A: Append to repos.txt

```bash
# From repo root:
cat >> modules/agent-context/index_content/repos.txt << 'EOF'

# --- Evaluation Corpus (issue #1402) ---
addyosmani/agent-skills
obra/superpowers
msitarzewski/agency-agents
mvanhorn/last30days-skill
chopratejas/headroom
Panniantong/Agent-Reach
CopilotKit/CopilotKit
santifer/career-ops
colbymchenry/codegraph
Egonex-AI/Understand-Anything
CloakHQ/CloakBrowser
mattpocock/skills
Imbad0202/academic-research-skills
awesome-selfhosted/awesome-selfhosted
Hack-with-Github/Awesome-Hacking
EOF
```

### Option B: Use a separate eval repos file

```bash
# Create eval-only repos list
cat modules/agent-context/tests/eval/corpus.yaml | \
  grep 'url:' | sed 's|.*github.com/||' > /tmp/eval-repos.txt

# Pass to ingestion script
REPOS_FILE=/tmp/eval-repos.txt ./scripts/ingest-repos.sh
```

## Triggering Ingestion

```bash
# Option 1: Use the refresh-repos script (triggers full pipeline)
./scripts/refresh-repos.sh

# Option 2: Create a manual K8s Job from the CronJob template
kubectl create job eval-ingest-$(date +%s) \
  --from=cronjob/ingestion-refresh \
  -n agent-context

# Option 3: Direct ingestion of specific repos
kubectl exec -n agent-context deploy/ingestion-worker -- \
  python ingest-repo.py --repo addyosmani/agent-skills
```

## Confirming Ingestion Completed

All four producers must complete for each repo:

| Producer | What it creates | How to verify |
|----------|-----------------|---------------|
| Zoekt | Exact search index | `curl "http://zoekt:6070/api/search?q=UNIQUE_TOKEN&repos=org/repo"` returns results |
| Structural | S3 JSON structure map | `aws s3 ls s3://<bucket>/code-indexes/<repo>/structure.json` |
| Semantic | S3 Vectors embeddings | Query S3 Vectors with a known concept from the repo |
| SBOM | CycloneDX + Postgres rows | `SELECT * FROM dependencies WHERE repo_id = '<repo>'` has rows |

Quick check via MCP browse (if all producers feed the Door):

```bash
for repo in agent-skills superpowers agency-agents last30days-skill headroom \
            Agent-Reach CopilotKit career-ops codegraph Understand-Anything \
            CloakBrowser skills academic-research-skills awesome-selfhosted Awesome-Hacking; do
  echo -n "$repo: "
  curl -sf -X POST "http://context-mcp.agent-context.svc:5100/call" \
    -H 'Content-Type: application/json' \
    -d "{\"name\": \"browse\", \"arguments\": {\"action\": \"ls\", \"uri\": \"/$repo\"}}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if d.get('entries') else 'NOT INDEXED')"
done
```

## Running the Evaluation

### Full run

```bash
TEST_ENV=dev python -m tests.eval.run_eval
```

### JSON output (for CI/automation)

```bash
TEST_ENV=dev REPORT_FORMAT=json python -m tests.eval.run_eval > eval-results.json
```

### Custom thresholds

```bash
# Require 70% pass rate (default: 50%)
TEST_ENV=dev EVAL_PASS_THRESHOLD=0.7 python -m tests.eval.run_eval

# Increase top-K window (default: 10)
TEST_ENV=dev EVAL_TOP_K=5 python -m tests.eval.run_eval
```

## Scoring Methodology

| Verb | Scoring method | Automated? |
|------|---------------|------------|
| `search_exact` | Expected file appears in top-K results | Yes |
| `search_semantic` | Expected file in top-K + **flagged for manual review** | Partial — auto-scores file presence, human reviews relevance |
| `understand` | Expected location mentioned + majority of key facts present | Yes |
| `impact` | Expected callers/dependents appear in affected set | Yes |
| `browse` | Expected entries present in directory listing | Yes |

### Why semantic gets manual review

Semantic search answers a qualitative question ("is this relevant?") that can't be fully automated. The harness auto-checks whether the expected file appears in results (necessary condition), but a human should verify the returned content is actually *relevant* to the concept query — not just a coincidental file match. The report flags these for review.

## Interpreting Results

```
═══════════════════════════════════════════════════════════════════
  KNOWLEDGE LAYER EVALUATION REPORT
═══════════════════════════════════════════════════════════════════

  Total questions:   75
  Passed:            52
  Failed:            8
  Errors:            0
  Manual review:     15
  Pass rate:         86.7% (52/60 scoreable)

  Per-verb hit rates:
  --------------------------------------------------
    browse               95.0%  (19/20 scoreable, 0 manual-review)
    impact               80.0%  (8/10 scoreable, 0 manual-review)
    search_exact         90.0%  (18/20 scoreable, 0 manual-review)
    search_semantic       0.0%  (0/0 scoreable, 15 manual-review)
    understand           70.0%  (7/10 scoreable, 0 manual-review)
```

**Good results:** Pass rate > 70% across exact/structural verbs; semantic questions all flagged for manual review (expected).

**Bad results:** If exact search or browse fail > 30%, likely an ingestion problem (repos not indexed). If understand/impact fail, the structural index may be incomplete.

## Verb Coverage by Repo Type

| Repo type | Verbs that apply | Notes |
|-----------|------------------|-------|
| Code (`code`) | All 5 verbs | Full coverage — exact, semantic, understand, impact, browse |
| Skills (`skills`) | search_exact, search_semantic, browse, understand | Impact may not apply (no call graphs in markdown/config) |
| List (`list`) | search_exact, search_semantic, browse | Structural/impact don't apply to curated markdown lists |

The golden dataset honestly notes when a verb doesn't apply rather than forcing it.

## Files

| File | Purpose |
|------|---------|
| `corpus.yaml` | The 15 repos: name, URL, type, languages |
| `golden.yaml` | ~75 questions with expected answers + pass criteria |
| `run_eval.py` | Evaluation harness: ingest-check → query → score → report |
| `README.md` | This file — setup + run instructions |
| `__init__.py` | Package marker |

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TEST_ENV` | `unit` | Must be `dev` for eval to run |
| `MCP_URL` | `http://context-mcp.agent-context.svc.cluster.local:5100` | MCP endpoint |
| `EVAL_MODE` | `mcp` | `mcp` (production path) or `direct` (backend bypass) |
| `ZOEKT_URL` | `http://zoekt.agent-context.svc.cluster.local:6070` | Zoekt (direct mode) |
| `S3_VECTORS_INDEX` | `agent-context-code-embeddings` | S3 Vectors index name |
| `REPORT_FORMAT` | `text` | `text` or `json` |
| `EVAL_TOP_K` | `10` | Result must appear in top K |
| `EVAL_PASS_THRESHOLD` | `0.5` | Minimum pass rate for exit code 0 |
| `EVAL_TIMEOUT` | `30` | HTTP timeout per query (seconds) |

## Relationship to Other Tests

```
tests/
├── unit/               ← Pure logic (mocks, no AWS)
├── e2e/                ← Live plumbing (real cluster, fixture repos)
├── eval/               ← THIS: answer quality (real repos, golden answers)
│   ├── corpus.yaml
│   ├── golden.yaml
│   ├── run_eval.py
│   └── README.md
├── conftest.py
└── config.py
```

- **Unit/Component** (`tests/unit/`): proves code correctness — fast, CI-friendly
- **E2E** (`tests/e2e/`): proves plumbing works — needs cluster, uses fixture repos
- **Eval** (this): proves **answer quality** — needs full index of real repos, scores against golden answers

## Extending the Dataset

To add a new repo to the corpus:

1. Add entry to `corpus.yaml` (name, URL, type)
2. Add 5 questions to `golden.yaml` following the verb-spread pattern
3. Index the repo (add to `repos.txt`, trigger ingestion)
4. Run `run_eval.py` to verify questions score correctly

To add questions to an existing repo:

1. Review the repo's actual content (clone + read)
2. Add questions to `golden.yaml` with **specific** expected answers
3. For semantic questions: ensure the query uses words NOT in the code (true vocabulary mismatch)
4. Run eval to verify

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| All questions error | MCP endpoint not reachable | Check `kubectl get svc context-mcp -n agent-context` |
| All browse questions fail | Repos not indexed | Re-run ingestion, check job logs |
| Exact search fails but browse works | Zoekt not synced | Check Zoekt pod logs, verify index files in S3 |
| Semantic search scores 0 | S3 Vectors index empty or query timeout | Check embedding pipeline ran, verify index has vectors |
| Understand/impact errors | Structural index not built | Check structural producer in ingestion logs |
