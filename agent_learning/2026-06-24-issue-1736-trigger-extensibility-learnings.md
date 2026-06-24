# Learnings: #1736 — Phased Trigger Model + Extensible Asset Type (2026-06-24)

## Context
Third design iteration on EPIC #1736 (Knowledge Asset Registry). Operator provided two decisions that resolve internal contradictions in the design note and add extensibility requirements.

## Key Technical Decisions

### 1. Phased trigger: why "API publishes SQS inline" is correct for Phase 1
- The earlier design said "the API shouldn't need SQS permissions; the trigger service handles that" — but this created complexity (CronJob, separate IAM role, separate deployment) without user benefit at current scale.
- Phase 1 mirrors the existing pattern: `publish-ingestion.py` already publishes to SQS inline. The API just does the same thing from a DB row instead of a flat file.
- The row-first invariant (`INSERT … registered` → publish → `UPDATE … queued`) is the critical design: it makes Phase 2 additive because the sweeper reads the same `registered` rows.
- A failed publish leaves the row at `registered` — visible in the UI, recoverable via re-index or sweeper.

### 2. KEDA maxReplicaCount is the shared-infra backstop
- `maxReplicaCount: 50` in `manifests/ingestion-scaledjob.yaml:71` — already deployed.
- This means a burst of registrations just deepens SQS depth, it doesn't melt Bedrock/Zoekt/Neptune.
- Combined with per-scope quotas (bounded input), Phase 1 is safe without sophisticated backpressure.

### 3. Open VARCHAR + JSONB is the extensibility pattern for typed registries
- Postgres ENUMs and CHECK constraints require a migration per new type — bad for a registry that should grow by config.
- `asset_type VARCHAR(32)` + API-layer validation against a config dict = same safety, zero-migration extensibility.
- `metadata JSONB` for type-specific fields keeps the table stable as types are added.
- The `ASSET_TYPE_REGISTRY` dict (type→{steps, timeout, source_ref_pattern, requires_github_app}) is the same shape as `STEPS_BY_TYPE` in `publish-ingestion.py:56-61` — just enriched.

### 4. Existing codebase patterns that inform the design
- `publish-ingestion.py` STEPS_BY_TYPE (lines 56-61): `{"repo": ["s3_upload", "cgc", "deepwiki", "graphrag"], ...}`
- `sqs-worker.py` content_type dispatch (lines 316-325): routes to `ingest_repo()`/`ingest_url()`/`ingest_doc()` based on content_type
- `sqs-worker.py` TIMEOUTS dict (lines 42-48): per-type timeouts
- Worker uses `send_message()` with `MessageAttributes` containing `content_type` as a StringValue

## What Worked
- Reading the actual ingestion code (`publish-ingestion.py`, `sqs-worker.py`) before designing the type registry ensured alignment with existing patterns.
- Checking `manifests/ingestion-scaledjob.yaml` for the actual `maxReplicaCount` value confirmed the guardrail already exists (50, not 10 — it was raised from 10 to 50 in the #1353 parallel indexing design).

## What Didn't Work / Gotchas
- The §6.1/§6.2 contradiction in the original design was subtle: §6.1 said "trigger queries registered rows and publishes to SQS" (separate process), §6.2 said "API shouldn't need SQS perms" (separation of concerns), §8.9 said "API calls dispatch_to_trigger()" (API is involved). Three sections with slightly different models — caught by the operator. Lesson: when multiple sections describe the same flow, verify they're consistent.
- The §5.5 "Preview (fast-follow, not v1)" was inconsistent with resolved decision #4 and §8.3 which made preview v1. Always check all sections that reference the same decision when a decision gets resolved.

## Recommendations for Future Agents
- When an operator decision overrides a prior design rationale, make the override explicit in the text ("overrides the earlier X reasoning") rather than silently changing it. This prevents confusion when someone reads the doc linearly.
- The Phase 1 → Phase 2 pattern (simple inline first, sophisticated later when load justifies) is a good general approach for this codebase — it avoids premature optimization while maintaining a clean upgrade path via durable state (the DB row).
