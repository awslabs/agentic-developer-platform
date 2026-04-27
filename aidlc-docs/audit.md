# AIDLC Audit Log — Issue #181

## 2026-04-27 — Initial Research & Analysis

### Agent: @agent-pm
### Action: Deep codebase analysis + gap assessment

### Files Analyzed (19 files)
| File | Purpose | Key Findings |
|------|---------|-------------|
| `gateway/src/shared/schemas/auth.py` | TokenContext schema | Has all 5 identity fields: user_id, org_id, team_id, department_id, account_type |
| `gateway/src/auth/cognito_jwt.py` | JWT validator | Extracts `custom:org_id`, `custom:team_id`, `custom:department_id`, `custom:role`, `custom:account_type` from Cognito JWT |
| `gateway/src/shared/models/base.py` | TenantMixin | Adds `org_id` (indexed, NOT NULL) to all tenant-scoped tables |
| `gateway/src/shared/models/organization.py` | Org hierarchy | Organization -> Department -> Team -> User -> ServiceAccount |
| `gateway/src/budget/service.py` | Budget enforcement | Full hierarchical cascade: user -> team -> dept -> org, 3 period types |
| `gateway/src/shared/models/budget.py` | Budget DB models | BudgetConfig + BudgetUsage with TenantMixin |
| `gateway/src/shared/schemas/budget.py` | Budget schemas | EntityType enum, EnforcementResult, CostRecordRequest |
| `agent-factory/agent/src/complex-task-chat/sqs-client.ts` | SQS types | TaskPayload has user_id + optional tenant_id — **gap**: no org_id/team_id |
| `agent-factory/agent/src/complex-task-chat/complex-task-chat-agent.ts` | Main worker | Calls assertOwnership(session_id, user_id, tenant_id) — **gap**: only user-level |
| `agent-factory/agent/src/complex-task-chat/context/store/port.ts` | Context store interface | SessionHeader has ownerUserId + optional tenantId — **gap**: no org_id/team_id |
| `agent-factory/agent/src/complex-task-chat/context/store/dynamo-store.ts` | DDB implementation | Creates header with ownerUserId/tenantId only |
| `agent-factory/agent/src/complex-task-chat/context/lcm/lcm-context.ts` | LCM context manager | assertOwnership checks user match only — **gap**: no team isolation |
| `agent-factory/agent/src/complex-task-chat/artifacts/s3-artifact-store.ts` | S3 artifacts | Keys: `<sessionId>/<taskId>/<filename>` — **gap**: flat, no hierarchy, no identity in catalog |
| `agent-factory/agent/src/complex-task-chat/memory/types.ts` | Memory types | MemoryScope has user/component/tenant/persona — **gap**: tenant is vague |
| `agent-factory/agent/src/complex-task-chat/memory/dynamo-memory.ts` | Memory DDB impl | Scope keys: `scope#tenant#<value>` — needs org_id/team_id |
| `agent-factory/gateway/lambdas/ingest/handler.py` | Ingest Lambda | **Critical gap**: $connect persists sub, email, tenant_id only. Missing org_id/team_id/department_id/account_type |
| `agent-factory/gateway/lambdas/ingest/channels/webchat.py` | WebChat adapter | Extracts user_id from claims.sub — correct but incomplete identity |
| `agent-factory/infra/modules/api-gateway-ws/main.tf` | WS API infra | Uses REQUEST authorizer on $connect with querystring token |
| `docs/user-identity-and-credentials-design.md` | Design doc | Covers vault/credentials — confirms identity flows through Cognito JWT |

### External Research
- No sub-issues exist yet for #181
- Related epics: #132 (Vault), #61 (Identity linking), #134-139 (Vault phases)
- Beads state is empty — needs bootstrap

### Gap Analysis Summary
The identity boundary between gateway (Python/FastAPI) and agent-factory (TypeScript/Lambda) is the core gap. The gateway has a complete `TokenContext` flowing through every request. The agent-factory has a rudimentary `user_id` + optional `tenant_id` — the full `(org_id, team_id, user_id)` tuple never crosses the WebSocket -> SQS -> Worker boundary.

**Root cause**: The `$connect` authorizer Lambda only persists 3 of the ~8 available JWT claims. Everything downstream inherits this truncated identity.

### Risk Assessment
- Stage A (JWT propagation): LOW — additive, read-and-propagate, zero behavior change
- Stage B (catalog schema): LOW — additive DDB attributes, backward-compat via lazy migration
- Stage C (S3 keys + uploads): MEDIUM — new S3 key format requires dual-read path, new presigned upload endpoint, frontend component
- Stage D (budget/quota): MEDIUM — new DDB table, cost calculation logic ported from gateway's RDS model to DDB
