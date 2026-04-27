# Requirements Specification — Issue #181
# User Identity + Per-Tenant Isolation Across the Agent Platform

**Phase**: Inception | **Stage**: Requirements | **Date**: 2026-04-27
**Epic**: [#181](https://github.com/aws-e/adp/issues/181)

---

## 1. Problem Statement

The agent-factory module has `tenant_id`/`user_id` threaded through its types but never validates them against a real identity source. Artifacts, sessions, memory, and outputs are scoped only by `session_id` with no enforceable boundary between users, teams, or organizations. Meanwhile, the gateway module already implements a full multi-tenant model (`Organization > Department > Team > User`) that should be extended, not reinvented.

## 2. Architecture Decision Record

### ADR-1: Enforcement Strategy
**Decision**: Logging-only in Stage A, enforcement in Stage B.
**Rationale**: Stage A's goal is "zero behavior change for existing users." Enforcing team isolation immediately would lock out Cognito users missing `custom:team_id`. Stage A logs the full `TokenContext` at INFO (proving the pipeline works), then Stage B flips enforcement on alongside catalog filtering. The `assertOwnership` change in Stage A is: if BOTH the existing header AND the caller have `teamId`, and they differ, reject. If either is missing, allow (legacy compat).

### ADR-2: Budget Storage Backend
**Decision**: DynamoDB with a new `adp-<env>-agent-budget` table.
**Rationale**: The agent-factory module is 100% DynamoDB + S3 today. Adding RDS would mean new VPC configuration for Lambda->RDS, connection pooling concerns, and cross-module coupling. DynamoDB is natural: ingest Lambda and worker already have DDB access, writes are simple atomic increments, and access patterns map cleanly to composite keys (`PK: budget#<entity_type>#<entity_id>`, `SK: <period_type>#<period_start>`). Cross-module cost aggregation is explicitly out of scope.

## 3. Functional Requirements

### FR-1: Identity Propagation (Stage A)
| ID | Requirement | Priority |
|----|------------|----------|
| FR-1.1 | WS `$connect` authorizer extracts `custom:org_id`, `custom:team_id`, `custom:department_id`, `custom:role`, `custom:account_type` from Cognito JWT and persists them in DDB connection table | P0 |
| FR-1.2 | Session header row stores `org_id`, `team_id`, `user_id`, `account_type` on first message | P0 |
| FR-1.3 | SQS task payload carries `org_id`, `team_id`, `user_id`, `account_type` (required when present in JWT) | P0 |
| FR-1.4 | Worker logs full identity context at INFO level on task start | P0 |
| FR-1.5 | `assertOwnership` validates team match when both header and caller have `teamId`; allows access if either is missing (legacy compat) | P0 |
| FR-1.6 | All new fields are optional (`?`) in TypeScript types for backward compatibility | P0 |

### FR-2: Artifact Identity (Stage B)
| ID | Requirement | Priority |
|----|------------|----------|
| FR-2.1 | `chat_artifacts` DDB catalog rows include `org_id`, `team_id`, `user_id` as indexed attributes | P0 |
| FR-2.2 | `org_id` GSI on catalog table enables org-wide admin queries | P1 |
| FR-2.3 | `publish`, `listBySession`, `fetch` populate and filter by identity fields | P0 |
| FR-2.4 | Legacy rows (no identity) visible only to their original session | P0 |
| FR-2.5 | Lazy migration backfills identity on read when session header has the data | P1 |

### FR-3: Hierarchical Storage + User Uploads (Stage C)
| ID | Requirement | Priority |
|----|------------|----------|
| FR-3.1 | New S3 key format: `o/<org_id>/t/<team_id>/u/<user_id>/s/<session_id>/<task_id>/{in\|out}/<filename>` | P0 |
| FR-3.2 | Dual-read path: try new key first, fall back to legacy pattern | P0 |
| FR-3.3 | `POST /upload-token` returns presigned PUT URL scoped to caller's identity path (1h expiry) | P0 |
| FR-3.4 | `POST /upload-complete` writes DDB catalog row (idempotent via sha256 dedup) | P0 |
| FR-3.5 | Frontend drag-drop UI in chat composer calls upload-token, PUTs file, calls upload-complete | P0 |
| FR-3.6 | Worker injects `<user-attachments>` block in system prompt for tasks with attachments | P0 |
| FR-3.7 | Worker IAM narrowed to per-path `s3:GetObject` if feasible; otherwise bucket-level with catalog enforcement | P1 |

### FR-4: Budget Enforcement + Cost Attribution (Stage D)
| ID | Requirement | Priority |
|----|------------|----------|
| FR-4.1 | New DDB table `adp-<env>-agent-budget` with composite key for entity cascade | P0 |
| FR-4.2 | Entity types: `session`, `user`, `team`, `department`, `org` (extends gateway's model with `session`) | P0 |
| FR-4.3 | Metrics tracked: `artifact_storage_bytes`, `bedrock_input_tokens`, `bedrock_output_tokens`, `bedrock_cost_usd` (tagged by `stage=ingest\|worker`) | P0 |
| FR-4.4 | Pre-turn check: `check_hierarchical_budget` before SQS enqueue; soft limit = warn, hard limit = reject | P0 |
| FR-4.5 | Post-turn record: usage written to all 5 entity levels x 3 periods (daily/weekly/monthly) | P0 |
| FR-4.6 | Every usage row stamped with `model_id` for cost-by-model reporting | P0 |
| FR-4.7 | Cost calculation: token count x `model_pricing` at write time (dollar-denominated rows) | P0 |
| FR-4.8 | `ref.sizeBytes` from `publish_artifact` recorded against entity cascade | P1 |

## 4. Non-Functional Requirements

| ID | Requirement | Category |
|----|------------|----------|
| NFR-1 | Identity propagation adds < 10ms latency to WS `$connect` | Performance |
| NFR-2 | Budget pre-turn check adds < 50ms (single DDB query with begins_with) | Performance |
| NFR-3 | All new DDB attributes backward-compatible (existing data keeps working) | Compatibility |
| NFR-4 | Zero downtime during rollout (each stage independently deployable) | Availability |
| NFR-5 | All identity fields encrypted at rest (DDB default encryption) | Security |
| NFR-6 | Presigned upload URLs scoped to exact S3 key path (no wildcard) | Security |

## 5. Dependency Graph

```
Stage A (JWT propagation — foundation)
  |              \
  v               v
Stage B          Stage D
(catalog)        (budget)
  |
  v
Stage C
(uploads)
```

- **A -> B**: Catalog needs identity fields in the pipeline
- **B -> C**: Upload endpoints extend catalog with hierarchical S3 keys
- **A -> D**: Budget needs identity fields for attribution
- **B and D are parallel** after A completes

## 6. Acceptance Criteria (Epic Level)

1. Stage A: `TokenContext` flows end-to-end; worker logs show `org_id`, `team_id`, `user_id`, `account_type`
2. Stage B: User in team A cannot list artifacts created by user in team B
3. Stage C: Drag file into chat composer, send message referencing it, agent reads and responds
4. Stage D: 100-token hard cap on test team rejects long request; session cost query returns correct USD
5. All four stages have tests committed and green

## 7. Out of Scope

- Admin UI for team creation/invitations (Cognito groups assumed)
- Gateway RDS table migration (shared identity via JWT, not DB)
- IAM-based agent auth (Cognito-only; IAM is Stage-E follow-up)
- DynamoDB RLS (enforcement at application layer)
- Infrastructure cost attribution (EKS, NAT, SQS — separate feature)
- Cross-module cost aggregation (separate admin-UI feature)
- Real-time cost streaming during a turn

## 8. Related Issues

| Issue | Relationship |
|-------|-------------|
| #51 | Unblocked by Stage C (Slack upload scoping) |
| #132 | Unblocked by Stage A (Vault per-user credentials) |
| #104 | Unblocked by Stage A (memory scoping) |
| #103 | Related (chat-context DDB needs identity stamp) |

## 9. Estimated Effort

| Stage | Effort | Duration (1 dev) | Depends On |
|-------|--------|-------------------|------------|
| A — JWT propagation | Medium | 2-3 days | Nothing |
| B — Catalog schema | Small | 1-2 days | Stage A |
| C — S3 keys + uploads | Large | 3-4 days | Stage B |
| D — Budget/quota | Large | 3-4 days | Stage A |
| **Total** | | **8-11 days** (6-8 with B+D parallel) | |
