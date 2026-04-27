# Design Plan — Issue #181: User Identity + Per-Tenant Isolation

## Objective

Create detailed technical design documents for each stage, specifying exact interface contracts, DDB schema definitions, TypeScript/Python type changes, and test plans. These designs will be the implementation spec that developers follow.

## Design Documents to Produce

### 1. Stage A Design: Identity Pipeline Wiring
- Exact Python changes to `handler.py` (`_persist_connection_claims`, `_restore_connection_claims`, `handle_long_running`)
- TypeScript interface diffs for `TaskPayload`, `SessionHeader`
- `assertOwnership` team-check logic (pseudocode)
- Logging format for `TokenContext` in worker
- Test matrix: JWT decode, ingest Lambda, assertOwnership (team match, legacy compat)

### 2. Stage B Design: Catalog Schema + Access Control
- DDB attribute additions to `chat_artifacts` table
- GSI definition for `org_id` queries
- `publish`/`listBySession`/`fetch` filter logic (pseudocode)
- Lazy migration strategy
- Test matrix: cross-team denial, legacy row access, lazy backfill

### 3. Stage C Design: Hierarchical S3 + Upload API
- S3 key format specification
- Dual-read path logic (new key -> legacy fallback)
- REST API contract for `/upload-token` and `/upload-complete` endpoints
- Frontend component spec (drag-drop, progress, error states)
- Worker attachment injection template
- Test matrix: presigned URL scoping, idempotent upload-complete, E2E flow

### 4. Stage D Design: Budget DDB Schema + Enforcement
- DDB table schema (`adp-<env>-agent-budget`): PK/SK patterns, GSIs, attribute definitions
- `BudgetConfig` TypeScript type (ported from gateway Python)
- `check_hierarchical_budget` algorithm (cascade walk)
- Post-turn recording: write pattern for 5 levels x 3 periods
- Cost calculation formula (token count x model_pricing)
- Test matrix: hard cap, session cost query, cross-level consistency

## Research Needed
- [ ] Exact current DDB schema for `chat_artifacts` table (attribute names, key schema)
- [ ] Current `model_pricing` table schema in gateway (for Stage D cost calculation)
- [ ] Current authorizer Lambda code for claim extraction patterns
- [ ] Frontend chat composer component structure (for Stage C drag-drop integration)

## Output
Single design document: `aidlc-docs/inception/design.md` covering all 4 stages with enough detail for a developer to implement without ambiguity.

## Estimated Effort
This design phase: ~1 session (research + write)
