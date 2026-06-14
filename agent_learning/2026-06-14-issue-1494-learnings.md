# Issue #1494: MCP Door Functional Verification Loop — Learnings

## Date: 2026-06-14
## Agent: @agent-operations

## Summary
Drove the Context MCP Door (Knowledge Layer serving) from 0% to 100% on all 4 deterministic verbs using the eval harness in `EVAL_MODE=mcp`. The Door was already partially fixed by a prior run but the branch wasn't synced, and the eval scorer had false-positive vulnerabilities.

## What worked

1. **Assess-first approach**: Rather than re-implementing fixes, I checked the live pod's code first. The previous run had already deployed working fixes to the pod — I just needed to sync the branch and fix the eval harness.

2. **Port-forward for local eval**: `kubectl port-forward -n agent-context svc/context-mcp 5100:5100` lets you run the eval harness locally against the deployed Door. Much faster iteration than rebuilding images.

3. **Scorer hardening caught the real issue**: The eval harness was sending requests without auth headers, and the Door's ACL correctly returned empty. Adding `X-GitHub-Login: eval-harness` to all requests unblocked the entire eval.

4. **Golden data corrections**: Several golden.yaml entries had wrong expected paths (e.g., `skills/grill-with-docs` when the real path is `skills/engineering/grill-with-docs`). This is a data quality issue, not a Door bug.

## Key technical details

### Door architecture (deployed state)
- **Search**: Zoekt v16 POST to `/api/search` with `{"Q": "...", "Opts": {...}}` payload. Door deduplicates results to file-level.
- **Browse**: At root `/`, lists repos from S3 `code-indexes/` prefix. Deeper paths use Zoekt `file:` filter queries.
- **Understand**: Loads `code-indexes/{org}-{repo}.json` from S3, falls back to Zoekt content search if structural index is sparse.
- **Impact**: Uses S3 call_graph first, falls back to Zoekt intra-repo symbol search.
- **ACL**: Fail-closed. No `X-GitHub-Login` header → returns `[]`. With header → allows all indexed repos (no Postgres in dev).

### S3 key layout
- Code indexes: `s3://agent-context-platform-data-879318057152/code-indexes/{org}-{repo}.json`
- Default `CODE_INDEX_S3_PREFIX` env var is `content/code-indexes` but the structural backend has a multi-strategy loader: tries `code-indexes/{name}.json` first, then suffix match, then legacy path.

### Eval harness configuration
- `TEST_ENV=dev EVAL_MODE=mcp MCP_URL=http://localhost:5100` (or cluster-internal URL)
- Auth headers required: `X-GitHub-Login: eval-harness` + `X-GitHub-Teams: platform-team`
- Scorer checks structural fields only (hardened against false positives)

### Repos with sparse structural indexes
- `mattpocock-skills`: 0 symbols, 0 call_graph (all markdown/TOML)
- `Hack-with-Github-Awesome-Hacking`: 243 bytes (minimal)
- `awesome-selfhosted-awesome-selfhosted`: 248 bytes (minimal)
- These repos fall back entirely to Zoekt for understand/impact/browse

## Gotchas

1. **kubectl context name lies**: The `embark1-adp-dev` context name might point at the wrong account. Always verify with `aws sts get-caller-identity --query Account`.

2. **Zoekt v16 payload format**: The web API requires `POST /api/search` with `{"Q": "query string", "Opts": {"NumContextLines": 0, "MaxDocDisplayCount": 50}}`. The old format `{"q": "...", "num": ...}` returns 0 results silently (200 OK but empty).

3. **File paths in Zoekt include the repo prefix**: Zoekt stores repos as `github.com/org/repo` so file paths in results are like `file: "src/foo.py"` with `repo: "github.com/org/repo"`. The golden data uses short names (`headroom/compress.py`) so the scorer does substring matching.

4. **ACL in dev mode**: No Postgres = no `PostgresACLStore`. The Door uses an "AllowIndexedRepos" store that requires a valid `X-GitHub-Login` header but allows access to all indexed repos. This satisfies fail-closed (no header → denied) while being permissive for authenticated callers.

5. **Port-forward persistence**: The background `kubectl port-forward` can die silently. If eval suddenly gets 0 repos indexed, check if port-forward is still running.

## Recommendations

1. **Semantic search gate**: Currently report-only (15% with Zoekt fallback). Enabling requires S3 Vectors embedding store + a semantic similarity judge. File as a separate issue.

2. **Sparse index handling**: Repos that are all-markdown produce near-empty code-indexes. The understand/impact verbs handle this gracefully via Zoekt fallback, but golden questions for these repos should expect content-level results, not symbol-level.

3. **Golden data maintenance**: When the corpus or indexing pipeline changes, re-validate golden.yaml against actual indexed content. Wrong expectations create false failures.
