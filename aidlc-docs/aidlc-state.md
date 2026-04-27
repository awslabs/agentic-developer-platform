# AIDLC State — Issue #181: User Identity + Per-Tenant Isolation

## Current Phase: INCEPTION (Requirements)
**Complexity: COMPLEX**
**Date: 2026-04-27**

## Research Summary

### What Exists (Gateway Module — Source of Truth for Identity)
- `TokenContext` schema: `user_id`, `org_id`, `team_id`, `department_id`, `account_type`, `is_admin`, `expires_at`, `auth_source`
- `CognitoJWTValidator` validates JWTs, extracts `custom:org_id`, `custom:team_id`, `custom:department_id`, `custom:role`, `custom:account_type`
- `TenantMixin` adds indexed `org_id` to every tenant-scoped table
- `BudgetService` with hierarchical enforcement: user -> team -> department -> org, daily/weekly/monthly periods
- `BudgetConfig` + `BudgetUsage` SQLAlchemy models (RDS/Postgres)
- Entity types: `org`, `department`, `team`, `user`, `service_account`, `agent`

### What's Missing (Agent-Factory Module — The Gap)
1. **$connect authorizer** (`handler.py:94-126`): persists only `sub`, `email`, `tenant_id` — NOT `org_id`, `team_id`, `department_id`, `account_type`, `role`
2. **TaskPayload** (`sqs-client.ts:20-38`): has `user_id` + optional `tenant_id` — no `org_id`, `team_id`
3. **SessionHeader** (`store/port.ts:37-45`): has `ownerUserId` + optional `tenantId` — no `org_id`, `team_id`
4. **assertOwnership** (`lcm-context.ts:121`): checks user match only, not team-level isolation
5. **S3 artifact keys**: flat `<sessionId>/<taskId>/<filename>` — no hierarchy
6. **Artifact catalog DDB rows** (`s3-artifact-store.ts:96-107`): no identity fields at all
7. **Memory scope** (`memory/types.ts:21-26`): has `tenant` (vague string) instead of `org_id`/`team_id`
8. **No budget enforcement** in the agent pipeline — gateway has it but agent-factory doesn't

### Stage Breakdown
| Stage | Scope | Files Changed | Risk |
|-------|-------|--------------|------|
| A — JWT propagation | Extend claims persistence, TaskPayload, SessionHeader, assertOwnership | ~8 files (Python + TypeScript) | Low — read-and-propagate only |
| B — Catalog schema | Add identity to DDB artifact rows, filter by team | ~3 files | Low — additive, backward compat via lazy migration |
| C — S3 keys + uploads | Hierarchical keys, presigned upload, frontend drag-drop | ~6 files + new Lambda endpoint | Medium — dual-read path, new frontend component |
| D — Budget/quota | Port BudgetService to DDB, add session entity, pre-turn check | ~5 new files + integration | Medium — new DDB table, cost calculation logic |

## Recommendations
- **Stage A first**: Foundation — zero behavior change, pure data propagation
- **Stages B and C can partially overlap**: B is catalog-only, C extends B to S3
- **Stage D is independent** of C and can run in parallel after A completes
- Each stage = 1 GitHub sub-issue = 1 developer task

## Status
- [x] Codebase research complete
- [x] Gap analysis complete
- [ ] Requirements plan created (awaiting user input on 2 questions)
- [ ] Sub-issues created
- [ ] Project board created
