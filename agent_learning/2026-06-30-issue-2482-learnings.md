# Learnings from Issue #2482 — MCP Verb QA Evaluation

## What worked

1. **Using the existing eval harness headers**: The issue said to use `x-github-login: eval-bot` / `x-owner-sub: eval-bot`, but these didn't work. The existing eval runner at `modules/agent-context/tests/eval/run_eval.py` uses `X-GitHub-Login: eval-harness` and `X-GitHub-Teams: platform-team` — those are the correct auth headers for the MCP endpoint.

2. **urllib.request over httpx**: Kept the runner dependency-free (no pip install required) by using stdlib `urllib.request` instead of `httpx`. The endpoint is simple enough that the stdlib is sufficient.

3. **Incremental testing**: Testing each verb individually before building the full runner helped discover the exact response shapes and error modes.

## Key technical decisions

- **Headers**: `X-GitHub-Login: eval-harness` + `X-GitHub-Teams: platform-team` for code verbs (search, understand, impact, browse). Personal-context verbs (remember, experience) additionally need `X-Owner-Sub: <valid-uuid>` + `X-Tenant-Id: <string>`.
- **Infrastructure skip detection**: Rather than failing 60+ cases due to unprovisioned backends, the runner detects when ALL happy_path cases for a verb return empty and marks them as "infrastructure unavailable" — distinguishing infra gaps from verb bugs.
- **Edge case scoring**: Edge cases (`case_type: "edge"`, `min_results: 0`) that get error responses (like "Invalid persona") are scored as PASS because the endpoint correctly rejected the invalid input rather than hallucinating results.

## Gotchas and non-obvious findings

1. **`project_not_found` error**: Using `x-github-login: eval-bot` (as the issue suggested) triggers `project_not_found`. The eval-harness identity + platform-team team header bypasses the project ownership check.

2. **X-Owner-Sub must be a valid UUID**: Personal-context verbs validate the format — plain strings like "eval-bot" are rejected. Use a dummy UUID like `00000000-0000-4000-8000-000000000042`.

3. **S3 Vectors IAM gap**: The IRSA role (`adp-dev-agent-context-irsa`) lacks `s3vectors:CreateIndex` permission, so remember/experience verbs can't create personal indices. This is a known infra gap, not a code bug.

4. **DeepTutor not indexed**: `HKUDS/DeepTutor` is not in the Zoekt code index. All search/understand/impact queries against it return empty. Only `HKUDS/Vibe-Trading` returns results. This is an ingestion gap — the dataset assumes both repos are indexed.

5. **Browse verb empty**: The `browse` verb's tree-listing/file-read backend is entirely non-functional in the current deployment. All cases return `{"entries": []}` regardless of URI or action.

6. **Understand source**: All `understand` results come from `source: "code-index-fallback"` (Neptune graph not wired per #2433). The fallback does symbol search in Zoekt, which works but can be noisy (returns partial symbol matches).

7. **Search scope=docs fallback**: Requesting `scope=docs` doesn't use semantic search (S3 Vectors not provisioned per #2297) — it silently falls back to the code index. Results still have `match_type: "exact"`, not semantic.

## Useful endpoints and response shapes

- **Endpoint**: `POST http://context-mcp.agent-context.svc.cluster.local:5100/call`
- **Body**: `{"name": "<verb>", "arguments": {...}}`
- **search response**: `{"results": [{repo_id, file, line, content, match_type}], "total": N, "query": "..."}`
- **understand response**: `{"target": "...", "summary": "...", "definitions": [{repo_id, file, line, symbol, kind, signature, callers, callees, source}]}`
- **impact response**: `{"verdict": "...", "target": "...", "blast_radius": N, "repos_affected": {...}, "source": "...", "affected": [{repo_id, file, line, content, match_type, relationship, symbol}]}`
- **browse response**: `{"action": "...", "uri": "...", "entries": [...]}`

## Recommendations

- File a follow-up to ingest `HKUDS/DeepTutor` into Zoekt
- File a follow-up for the browse verb backend (tree-listing)
- File a follow-up for the S3Vectors IAM permissions (s3vectors:CreateIndex)
- Re-run this eval after each fix to track regression/improvement
