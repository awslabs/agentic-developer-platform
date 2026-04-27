# Requirements Plan — Issue #181: User Identity + Per-Tenant Isolation

## Epic Overview

Propagate the gateway's `TokenContext` identity model (`org_id`, `team_id`, `user_id`, `department_id`, `account_type`) through the entire agent pipeline (WS connect -> ingest -> SQS -> worker -> S3/DDB) so that every stored artifact, message, memory, and usage record is attributable and access is enforceable.

## Architecture Decision: Reuse, Don't Rewrite

Based on codebase analysis, the gateway already has:
- `CognitoJWTValidator` (Python) — validates JWTs, extracts custom claims
- `TokenContext` (Pydantic) — universal auth currency
- `TenantMixin` — indexed `org_id` on all tenant-scoped tables
- `BudgetService` — hierarchical cascade (user -> team -> dept -> org)

The agent-factory module needs TypeScript equivalents and the pipeline plumbing to carry these fields end-to-end. No new auth infrastructure needed — just wiring.

## Stage Decomposition

### Stage A — JWT Claims Propagation (Foundation)

**What changes:**
1. `handler.py:_persist_connection_claims()` — extend to persist `org_id`, `team_id`, `department_id`, `account_type`, `role` from authorizer context
2. `handler.py:_restore_connection_claims()` — restore all new fields into `claims`
3. `sqs-client.ts:TaskPayload` — add `org_id`, `team_id`, `department_id`, `account_type` (required when present)
4. `store/port.ts:SessionHeader` — add `orgId`, `teamId`, `departmentId`, `accountType`
5. `dynamo-store.ts:createSessionHeader()` — write new fields
6. `dynamo-store.ts:getSessionHeader()` — read new fields
7. `lcm-context.ts:assertOwnership()` — validate team match (not just user)
8. `complex-task-chat-agent.ts:processOne()` — extract new fields from TaskPayload, pass to assertOwnership, log full identity
9. `handler.py:handle_long_running()` — include new fields in SQS message body
10. Tests: JWT decode unit test, ingest Lambda test, worker assertOwnership test

**Backward compatibility:** All new fields optional in TypeScript types with `?`. Existing sessions without `orgId` continue to work (legacy single-tenant mode). `assertOwnership` only enforces team check when both header and caller have `teamId`.

### Stage B — Catalog Schema Extension

**What changes:**
1. `s3-artifact-store.ts:publish()` — accept and write `org_id`, `team_id`, `user_id` to DDB catalog row
2. `s3-artifact-store.ts:listBySession()` — filter by team_id when caller provides one
3. `s3-artifact-store.ts:fetch()` — verify team match before returning
4. Lazy migration: existing rows without identity fields remain visible to their original session only
5. Add `org_id` GSI on artifact catalog table (Terraform)
6. Tests: cross-team access denied test

### Stage C — S3 Key Layout + User Uploads

**What changes:**
1. New S3 key format: `o/<org_id>/t/<team_id>/u/<user_id>/s/<session_id>/<task_id>/{in|out}/<filename>`
2. `publish()` writes new keys; `fetch()`/`listBySession()` try new-key-first, fall back to legacy
3. New ingest Lambda endpoint: `POST /upload-token` — returns presigned PUT URL scoped to caller's path
4. New ingest Lambda endpoint: `POST /upload-complete` — writes DDB catalog row
5. Frontend drag-drop component in chat composer
6. Worker: inject `<user-attachments>` block when `task.attachments` is non-empty
7. Tests: E2E upload + agent read

### Stage D — Quota / Billing Hooks

**What changes:**
1. New DDB table: `adp-<env>-budget-usage` with composite key for entity cascade
2. Port `BudgetConfig` + `BudgetUsage` schema from gateway (RDS) to DDB equivalent
3. Add `session` entity type (gateway only has user/team/dept/org)
4. Pre-turn check: `check_hierarchical_budget()` before SQS enqueue in ingest Lambda
5. Post-turn record: worker writes usage to all 5 levels (session/user/team/dept/org) x 3 periods
6. Stamp `model_id` on every usage row for cost-by-model reporting
7. Cost calculation using gateway's `model_pricing` table
8. Tests: hard cap enforcement, session cost query

## Dependency Graph

```
Stage A (foundation)
  |         \
  v          v
Stage B    Stage D
  |
  v
Stage C
```

- B depends on A (needs identity fields in the pipeline)
- C depends on B (extends catalog with hierarchical S3 keys)
- D depends on A (needs identity fields for budget attribution)
- B and D can run in parallel after A completes

---

## Questions (2 items requiring user input)

### Q1: Should Stage A enforce team isolation immediately, or start with logging-only?

**My Recommendation**: Logging-only in Stage A, enforcement in Stage B.

**Reasoning**: Stage A's acceptance criteria says "zero behavior change for existing users." If we enforce team isolation immediately, any misconfigured Cognito user (missing `custom:team_id`) would get locked out. Better to log the full `TokenContext` at INFO in Stage A (proving the pipeline works), then flip enforcement on in Stage B when we also have the catalog filtering.

The `assertOwnership` change in Stage A would be: if BOTH the existing header AND the caller have `teamId`, and they differ, reject. If either is missing, allow (legacy compat). Full enforcement comes in Stage B.

[Answer]: logging-only in Stage A, enforcement in Stage B

### Q2: For Stage D budget storage — DynamoDB (consistent with agent-factory's existing data) or RDS (consistent with gateway's existing BudgetUsage)?

**My Recommendation**: DynamoDB with a new `adp-<env>-agent-budget` table.

**Reasoning**: The agent-factory module is 100% DynamoDB + S3 today. Adding an RDS dependency for budget tracking would mean:
- New VPC configuration for Lambda -> RDS connectivity
- Connection pooling concerns (Lambda cold starts)
- Cross-module database coupling

DynamoDB is the natural fit: the ingest Lambda and worker already have DDB access, writes are simple atomic increments, and the access patterns (check budget by entity_id + period, record usage) map cleanly to composite keys. The schema mirrors gateway's `BudgetUsage` but uses DDB-native patterns (PK: `budget#<entity_type>#<entity_id>`, SK: `<period_type>#<period_start>`).

Cross-module cost aggregation (combining agent-factory DDB usage with gateway RDS usage) is explicitly out of scope for Stage D per the issue. A future admin dashboard can query both.

[Answer]: DynamoDB with new `adp-<env>-agent-budget` table

---

## Estimated Effort

| Stage | Effort | Duration (1 dev) | Depends On |
|-------|--------|-------------------|------------|
| A | Medium | 2-3 days | Nothing |
| B | Small | 1-2 days | Stage A |
| C | Large | 3-4 days | Stage B |
| D | Large | 3-4 days | Stage A |
| **Total** | | **8-11 days** (6-8 days with B+D parallel) | |

## Sub-Issue Plan

Will create 4 GitHub sub-issues under #181:
1. `Stage A: JWT claims propagation — wire TokenContext through agent pipeline`
2. `Stage B: Catalog schema extension — add identity to artifact DDB rows`
3. `Stage C: S3 key layout + user uploads — hierarchical keys + presigned upload`
4. `Stage D: Quota/billing hooks — per-session cost tracking + hierarchical budget enforcement`
