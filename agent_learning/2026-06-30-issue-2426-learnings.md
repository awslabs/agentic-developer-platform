# Issue #2426 — MCP Read-Path Closed-Loop Fix

## Date: 2026-06-30
## Agent: @agent-operations

## Summary
Fixed all 6 MCP read-path verbs by resolving the ACL store query failures and zoekt-webserver CrashLoopBackOff through 7 commits across 3 problem domains: SQL compatibility, repo-name normalization, and K8s volume management.

## What Worked

### Iterative debugging in production
Testing each fix live immediately after deploy revealed cascading issues that unit tests couldn't catch:
1. First fix ($N → %s) revealed the deeper jsonb type mismatch
2. jsonb fix revealed the repo-name format mismatch (short vs full vs domain-qualified)
3. Zoekt readOnly fix revealed that symlinks don't work through S3 Mountpoint FUSE

### Using `kubectl exec` with curl for in-pod testing
The identity headers (`x-github-login`, `x-owner-sub`, `x-tenant-id`) are critical — without them the ACL always returns empty (fail-closed). Testing from inside the pod via curl is the fastest feedback loop.

### Checking the actual database schema
The ACL code assumed `allowed_principals` was `text[]` but it's actually `jsonb`. The fix required jsonb operators (`?` and `?|`) instead of array operators (`ANY()` and `&&`).

## Key Technical Decisions

### ACL repo-name normalization
Three sources produce repo names in different formats:
- Database: `HKUDS/Vibe-Trading` (org/repo)
- Structural backend: `Vibe-Trading` (short name from target parsing)
- Zoekt: `github.com/HKUDS/Vibe-Trading` (domain-qualified)

Solution: normalize by stripping domain prefixes and building a lookup set containing both org/repo and short-name formats.

### Zoekt flat-copy approach for S3 Mountpoint
S3 Mountpoint CSI has several limitations that prevented simpler solutions:
1. **`readOnly: true` doesn't prevent mkdir errors** — Go's `os.MkdirAll` gets EEXIST (not EROFS) because the mount point exists on the parent filesystem
2. **Symlinks don't work for directory traversal** — S3 Mountpoint FUSE doesn't support following symlinks into its own mounted dirs
3. **Mount permissions default to root** — the zoekt container runs as uid=100 and can't read root-owned FUSE mounts
4. **Zoekt only scans .zoekt files at the top level** — nested subdirectories (org/repo/file.zoekt) are not discovered

Final solution: init container copies .zoekt files FLAT from S3 mount to an emptyDir, zoekt reads from the emptyDir.

### Experience tool import path
The Docker image puts `door/` and `personal_context/` as sibling top-level directories under `/app/`. Relative imports (`..personal_context`) fail because `door` IS the top-level package (no parent). Absolute imports (`personal_context.embeddings`) work because `/app/` is in sys.path.

## Gotchas

1. **`kubectl apply` with strategic merge doesn't remove list items** — stale volumes from previous deployments persist. Use `kubectl replace` for a clean replacement.
2. **Deploy workflow uses `:latest` tag with `imagePullPolicy: IfNotPresent`** — the pod won't pull a new image unless explicitly restarted. Use `kubectl rollout restart` after a new image push.
3. **AWS session tokens from IRSA-assumed roles expire after ~1 hour** — plan for credential refresh in long-running operations.
4. **Zoekt shard file naming**: the shard filename includes the URL-encoded repo path (e.g., `github.com%2FHKUDS%2FVibe-Trading_v16.00000.zoekt`). This is how zoekt internally maps search results back to repositories.

## Useful Commands

```bash
# Test MCP verb with identity headers
MCP=$(kubectl get pods -n agent-context --no-headers | grep context-mcp | grep Running | head -1 | awk '{print $1}')
kubectl exec -n agent-context "$MCP" -- sh -c "curl -s -X POST http://localhost:5100/call -H 'Content-Type: application/json' -H 'x-github-login: prsaws' -H 'x-owner-sub: test-user' -H 'x-tenant-id: ' -d '{\"name\":\"understand\",\"arguments\":{\"target\":\"Vibe-Trading::Artifact\"}}'"

# Check zoekt directly
kubectl exec -n agent-context <zoekt-pod> -c zoekt-webserver -- wget -qO- 'http://localhost:6070/api/search' --post-data='{"q":"Backtest","num":5}'

# Check ACL errors
kubectl logs -n agent-context <context-mcp-pod> --tail=30 | grep "ACL store"

# Query the database for repo ACL data
kubectl exec -n agent-context <context-mcp-pod> -- python3 -c "import os, psycopg2, boto3; ..."
```

## Files Modified
- `modules/agent-context/door/acl.py` — ACL store queries + filter normalization
- `modules/agent-context/door/server.py` — absolute imports for personal_context
- `modules/agent-context/manifests/zoekt.yaml` — emptyDir flat-copy approach
- `modules/agent-context/tests/unit/test_acl_postgres_placeholders.py` — regression tests (new)
