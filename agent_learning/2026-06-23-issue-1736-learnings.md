# Learnings: Issue #1736 — Knowledge Asset Registry Design Spike

**Date:** 2026-06-23
**Agent:** @agent-architect
**Issue:** #1736
**Deliverable:** `docs/agent-context/design-1736-knowledge-asset-registry.md`

---

## Key Technical Decisions

1. **New table, not extending `repositories`:** The `repositories` table has repo-specific columns (zoekt_status, vectors_status, last_indexed_sha) that don't apply to URLs/docs. A new `knowledge_assets` table handles all three asset types uniformly.

2. **Scope from session, not from request body:** Critical security decision — `tenant_id` and `owner_sub` are derived from the JWT, never from user-provided fields. The request body only carries a `scope` enum ("personal" | "tenant") that controls whether `owner_sub` is populated.

3. **Event-driven, not polling:** Registry rows are processed via SQS (consistent with existing `publish-ingestion.py` pattern), not by polling the table for `status = 'registered'`.

4. **Flat files coexist:** The shared corpus stays in `repos.txt`/`urls.txt`/`docs.txt`. The registry is exclusively for self-serve/tenant/user registrations. This avoids migrating a battle-tested CronJob path.

---

## Codebase Patterns Discovered

- **Router pattern for agent-context APIs:** Defined in `modules/agent-context/agent_context/api/`, mounted by the gateway via `app.dependency_overrides[get_indexing_db]`. The gateway provides auth; agent-context provides logic. See `indexing_router.py` as the template.

- **Migration numbering:** Sequential in `modules/agent-context/alembic/versions/` (001, 002, 003). Next available is 004 (reserved by #1721) and 005 (this EPIC).

- **SQS message format:** `publish-ingestion.py` sends `{source, content_type, steps, force, tags, triggered_by, enqueued_at}`. The registry extends this with `registry_asset_id` + `scope` fields — backward compatible.

- **DynamoDB `ingestion_state` table:** Used for change detection (has the repo's SHA changed since last index?). The registry's `status` field serves a similar purpose at a higher level (registration intent vs. DynamoDB's per-source operational state).

- **Conditional router mounting:** Agent-context routes are behind `AGENT_CONTEXT_ENABLED` env var check in the gateway's `app.py`.

---

## Critical Dependencies and Sequencing

```
#1721 migration 004 (tenant_id/owner_sub on repositories)
    → #1736 migration 005 (knowledge_assets table) — can be made independent
        → #1728 (projects table references knowledge_assets.project_id)
```

**Important:** The registry table CAN be created independently of #1721's migration (no FK dependency). Only the column semantics/naming must align. This was identified as an optimization opportunity.

---

## Gotchas

1. **`owner_sub` type:** #1721's design says `UUID` for `owner_sub` on `repositories`, but Cognito subs are strings (e.g., `us-east-1:abc-def`). VARCHAR(128) is correct. This inconsistency needs to be caught during #1721 implementation.

2. **The #1672 seam is the hardest part.** Without drawing this boundary cleanly, both EPICs would implement trigger/quota logic and conflict. The key insight: #1736 owns the data (registry rows); #1672 owns the process (trigger, validate, enqueue, callback).

3. **Bulk upload scope assignment:** Tenant admins upload under tenant scope (owner_sub=NULL). Individual users uploading a file get personal scope. The API must infer scope from the authenticated user's role, not from an explicit request field.

4. **No FK between knowledge_assets and repositories:** Intentional. URLs/docs don't produce `repositories` rows, and repos may exist in `repositories` without a registry entry (flat-file-sourced shared corpus).

---

## What Worked Well

- Starting with a thorough codebase scan (3 parallel Explore agents) before writing anything. Found the exact SQS message format, migration numbering, and router patterns to mirror.
- Reading #1721 and #1728 design notes in full — they pre-answer many schema questions.
- The existing `indexing_router.py` is a perfect template for the new `assets_router.py`.

---

## Recommendations for Future Agents

- When implementing Issue B (Registry CRUD API), copy `indexing_router.py`'s structure exactly — same dependency injection pattern, same Pydantic schema approach, same conditional mount in gateway `app.py`.
- When implementing Issue A (migration), ensure `pgcrypto` extension is referenced with `CREATE EXTENSION IF NOT EXISTS` (idempotent, as in migration 001).
- The quota defaults (20 repos/user, 200 repos/tenant) should be SSM parameters, not hardcoded — follow the pattern in `config.py` (pydantic-settings with env var override).
