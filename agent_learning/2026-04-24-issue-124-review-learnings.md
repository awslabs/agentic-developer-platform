# Learnings: Issue #124 Design Review — Conversation Persistence

**Date:** 2026-04-24
**Agent:** @agent-reviewer
**Issue:** #124
**PR:** #125

## Key Technical Findings

### DynamoDB GSI Query Semantics
- **DynamoDB `Query` requires exact equality (`=`) on partition key.** `begins_with` is ONLY valid on the sort key. This is a common mistake in design docs.
- The `user-workspace-index` GSI has `user_workspace` as partition key and `session_id` as sort key. To query across channels for one user, you CANNOT do `begins_with(user_workspace, "{sub}#")`. You must issue one Query per channel and merge results.
- For v1 this is fine since only `webchat` is active. Future channels (slack, cli) each add one more Query.

### Pydantic-Settings env_prefix
- The gateway's `Settings` class uses `env_prefix = "BG_"` (line 59 of `config.py`). All env vars are prefixed — e.g., `BG_DATABASE_URL`, `BG_COGNITO_USER_POOL_ID`.
- If you add a field with `Field(env="CUSTOM_NAME")`, the explicit `env=` overrides the prefix. This works but is inconsistent with the rest of the settings class. Prefer letting the prefix apply unless there's a strong reason not to.

### Async FastAPI + Synchronous boto3
- Using `async def` route handlers with synchronous `boto3` calls blocks the event loop. FastAPI only runs **sync** route handlers in a threadpool automatically.
- Options: `aioboto3`, `asyncio.to_thread()`, or make methods synchronous and call from sync route handlers (FastAPI threadpool).
- Check how `agent_registry` DynamoDB access works — follow the same pattern for consistency.

## Codebase Reference Points

- **DynamoDB sessions table Terraform:** `modules/agent-factory/infra/modules/dynamodb-sessions/main.tf`
- **GSI definition:** Lines 16-21, `user-workspace-index` with PK=`user_workspace`, SK=`session_id`, projection=ALL
- **user_workspace write path:** `modules/agent-factory/gateway/lambdas/ingest/handler.py` line 370
- **Cognito sub extraction:** `modules/agent-factory/gateway/lambdas/ingest/channels/webchat.py` line 135
- **Gateway auth dependency:** `modules/gateway/src/auth/dependencies.py` — `get_current_user()` returns `TokenContext`
- **TokenContext schema:** `modules/gateway/src/shared/schemas/auth.py` — has `user_id`, `org_id`, `is_admin`
- **Settings class:** `modules/gateway/src/shared/config.py` — `env_prefix = "BG_"`
- **Router auto-discovery:** `modules/gateway/src/app.py` lines 26-31 — `UNIT_MODULES` list, import + `include_router`
- **Terraform output for table name:** `modules/agent-factory/infra/gateway-outputs.tf` line 16 — `gateway_sessions_table`

## Tenant Isolation Pattern
- Tenant isolation for this feature relies on Cognito `sub` (globally unique UUID) embedded in the `user_workspace` GSI key.
- The `sub` comes from JWT validation, not user input — no manipulation possible.
- Owner mismatch returns 404 (not 403) to prevent session ID enumeration.
- No admin read bypass — per user-services invariant #3 (owner-only by default).
- `conn#` rows (connection claims) have no `user_workspace` attribute so they never appear in GSI queries.

## Review Process Notes
- PR #125 was already merged before the review workflow triggered. Post-merge reviews are still valuable for catching design issues before implementation begins.
- Design doc PRs should ideally be reviewed before merge since they guide implementation. But for a design-only PR with no code changes, the risk is low — findings are addressed during implementation.
