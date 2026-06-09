# Consolidated Design: Personal Context System

**EPIC:** #1287 (Personal Context)
**Consolidation Issue:** #1332
**Status:** Unified design + ordered implementation roadmap
**Author:** @agent-architect
**Date:** 2026-06-09
**Supersedes:** Individual implementation lists from #1319, #1325, #1327

---

## 1. Purpose of This Document

Four design documents were produced independently for the Personal Context system:

| Doc | Scope | Status |
|-----|-------|--------|
| EPIC #1283/#1287 (built: #1288-#1295) | Core store (OpenViking/Neptune), `experience` MCP tool, synthesis, recall/save hooks | **Implemented + deployed** |
| `design-1319-github-sender-to-cognito-sub.md` | Resolve GitHub webhook sender to `cognito_sub` | Design only |
| `design-1319-service-account-personal-context.md` | Service-account identity for autonomous/event-driven runs | Design only |
| `design-1325-chat-personal-knowledge-enrichment.md` | Chat session hydration + session-start recall composition | Design only |
| `design-1327-my-context-ui.md` | User-facing "My Context" UI + Cognito-JWT gateway API | Design only |

This document is the **single source of truth** for Personal Context going forward. It reconciles the four designs into one system: one identity model, one isolation invariant, one write/lifecycle model, one API surface, and one merged risk register.

---

## 2. Unified Identity Model

### 2.1 Core Invariant

> **Every Personal Context operation (read or write) is scoped by a single `owner_sub` (UUID). All paths into the system MUST resolve to the same `owner_sub` for the same principal, or fail-closed (empty `owner_sub` = no context, no error).**

### 2.2 Identity Resolution Paths

| Entry Path | Principal | Resolution Mechanism | `owner_sub` Value | `owner_kind` |
|---|---|---|---|---|
| **Chat/Webchat** | Human user | Cognito JWT `sub` claim (set at `$connect`) | `users.cognito_sub` | `"human"` |
| **GitHub webhook (human sender)** | Human user | DDB identity-index -> gateway `POST /internal/v1/resolve-user` -> `users.cognito_sub` | `users.cognito_sub` | `"human"` |
| **GitHub webhook (bot, human-rooted chain)** | Root human | Correlation pointer `root_human_id` -> gateway `POST /internal/v1/resolve-user-by-id` -> `users.cognito_sub` | Root human's `users.cognito_sub` | `"human"` |
| **GitHub webhook (bot, autonomous)** | Service account | `bot_kind` + `org_id` -> gateway `POST /internal/v1/resolve-service-account` -> `service_accounts.id` | `service_accounts.id` (UUID) | `"service_account"` |
| **User-facing API (#1327)** | Human user | Cognito JWT in `TokenContext.user_id` (= `sub` claim) | `TokenContext.user_id` | `"human"` |
| **Synthesis CronJob** | System (per-user batch) | Reads `owner_sub` from stored entries being synthesized | Existing entry's `owner_sub` | Inherits |
| **Chat hydration hook (#1325)** | Human user | Propagated from chat session's Cognito identity | Chat session's `cognito_sub` | `"human"` |

### 2.3 Why All Paths Converge

All human paths resolve to the **same UUID** because:
1. **Chat path:** `cognito_sub` = JWT `sub` claim.
2. **GitHub path:** `cognito_sub` = `users.cognito_sub` from Postgres (written during Cognito-backed onboarding).
3. **User API path:** `TokenContext.user_id` = JWT `sub` claim.
4. These are identical because `users.cognito_sub` IS the Cognito `sub` claim written during onboarding.
5. Uniqueness enforced: partial unique index `uq_users_cognito_sub` (migration `012`).

Service accounts use `ServiceAccount.id` (a SQLAlchemy-generated UUID). The UUID namespaces are structurally disjoint from Cognito-generated UUIDs (collision probability: 2^-122).

### 2.4 Fail-Closed Contract (Universal)

> **When `owner_sub` cannot be determined with certainty, it MUST be empty string. ALL personal-context operations MUST reject empty/missing `owner_sub`. No data is EVER written or read under a wrong or uncertain identity.**

Enforcement layers (defense-in-depth):

| Layer | Component | Enforcement |
|---|---|---|
| 1 - Dispatch | `handler.py:635` / webchat gateway | Only sets `cognito_sub` when identity is confirmed |
| 2 - Worker | `entrypoint.py:146-148` | Only sets `ADP_OWNER_SUB` env var if `cognito_sub` is truthy |
| 3 - Header Builder | `personal-context-headers.ts:57-59` | Returns `null` if `ownerSub` is empty |
| 4 - MCP Server | `identity.py:74-81` | Raises `IdentityError` (403) on missing/invalid `X-Owner-Sub` |
| 5 - Storage | `storage.py:122-124` | Force-stamps `owner_sub` from identity (ignores client-supplied values) |
| 6 - User API | `routes.py` (new, #1327) | Derives `owner_sub` from JWT, never from request params |

### 2.5 Decision: `owner_kind` Field

A new **optional** envelope field `owner_kind` distinguishes identity types:

```python
owner_kind: "human" | "service_account" | ""
```

- Defaults to `"human"` if absent (backward compat).
- Enables observability, audit, and future access-control distinctions.
- The MCP server treats `owner_sub` as an opaque UUID regardless of `owner_kind`.

---

## 3. Unified Isolation Invariant

### 3.1 The Single Rule

> **A caller sees: (a) their own entries (any visibility, `entry.owner_sub == caller.owner_sub`), and (b) entries shared within their tenant (`entry.visibility == "shared" AND entry.tenant_id == caller.tenant_id`). Everything else is invisible. A missing identity yields ZERO results, never all.**

### 3.2 Where This Rule Is Enforced

| Access Point | Component | How It Enforces |
|---|---|---|
| MCP `experience` tool (recall) | `storage.py:214-228` (`_caller_can_read`) | Owner OR same-tenant-shared filter |
| MCP `experience` tool (save) | `storage.py:122-124` (`write_entry`) | Force-stamps `owner_sub` + `tenant_id` from identity |
| MCP `experience` tool (list_syntheses) | `storage.py:165-189` (`list_entries`) | Same read filter |
| User-facing API (list/get) | `routes.py` (new, #1327) | `owner_sub = TokenContext.user_id`; read paths identical to `build_read_paths()` |
| User-facing API (edit/delete) | `routes.py` (new, #1327) | Ownership check: `entry.owner_sub != current_user.user_id` -> 404 |
| Chat hydration hook | `chat-experience-extract-hook.ts` (new, #1325) | Uses `personalContextIdentity` from session; fail-closed if null |
| Synthesis CronJob | `synthesis.py:352-353` | Constructs `CallerIdentity` from source entries; never crosses namespaces |

### 3.3 Critical Confirmation: User API (#1327) Uses the SAME Logic

The new `GET /api/personal-context/entries` endpoint in the gateway MUST implement the identical read filter as `PersonalContextStore._caller_can_read()`:

```python
# Gateway user API — SAME rule as MCP middleware:
def _gateway_read_filter(entry, current_user):
    # Owner always sees their own
    if entry.owner_sub == current_user.user_id:  # user_id = Cognito sub
        return True
    # Shared entries visible within same tenant
    if entry.visibility == "shared" and entry.tenant_id == current_user.org_id:
        return True
    return False
```

This is NOT a new isolation implementation — it is the same logic applied at a different network boundary. The gateway calls OpenViking directly and applies this filter on the response.

### 3.4 No Second Weaker Path

The designs propose no path that bypasses isolation:
- Export endpoint: owner-only (never exports others' entries).
- Synthesis job: reads ALL entries in a namespace for grouping, but only writes BACK to the same owner's namespace.
- Chat hydration: writes under the session owner's identity only.
- Visibility toggle: owner-only operation; shared entries remain tenant-scoped.

### 3.5 Tenant Scoping for Service Accounts

Service accounts are tenant-scoped by construction:
- Lookup: `WHERE org_id = :org_id AND name = :bot_kind` (SQL-level tenant scoping)
- Even if two tenants have a "pipeline-responder" SA, they get different UUID `owner_sub` values -> physically separate stores.

---

## 4. Unified Write / Lifecycle Model

### 4.1 Entry Lifecycle

```
                    [CREATE]
                       |
            +---------+---------+
            |                   |
      task-save-hook      chat-extract-hook       user-manual-create (future)
     (experience_tool)     (session-end)              (via UI API)
            |                   |                         |
            +----> ENTRY (learning, confidence=0.7, decay=1.0) <----+
                       |
            +----------+----------+
            |          |          |
         [RECALL]   [DECAY]   [SYNTHESIZE]
         (updates   (nightly   (nightly CronJob,
       last_accessed  idle>30d    >=5 unsynthesized)
           _at)    decay -= 0.1)
            |          |          |
            +----------+----------+
                       |
              [USER-CORRECT] (via UI #1327)
              sets validated=true, re-embeds
                       |
              [SUPERSEDE] (synthesis detects contradiction + newer validated)
              sets superseded_by, lowers confidence
                       |
              [USER-DELETE] (via UI #1327)
              hard-deletes: AGFS + embedding + graph vertex + marks related syntheses stale
```

### 4.2 Writers and Their Contracts

| Writer | Trigger | Entry Type | Persona | Dedup Mechanism |
|---|---|---|---|---|
| `experience-save-hook.ts` (#1294) | Post-task completion (agent work) | `learning` | Agent's persona (developer/architect/etc.) | Per-task cap (max 5); secret filter |
| `chat-experience-extract-hook.ts` (#1325 Issue A) | Session end or lazy-on-next-start | `learning` | `"chat"` (new persona value) | Session marked `personalContextExtracted: true`; min 4 turns gate |
| Synthesis CronJob (#1291) | Nightly 3am UTC | `synthesis` / `pattern` | Inherits from source learnings | `context.synthesized=true` on source learnings; skips already-synthesized |
| User edit (via UI API #1327) | Manual PATCH | (existing entry) | (unchanged) | Not a new write — updates existing entry; re-embeds; marks syntheses stale |
| Synthesis chat ingestion (#1325 Issue B) | Nightly synthesis pre-stage | `learning` | `"chat"` | Only reads sessions where `personalContextExtracted != true` AND `lastActivityAt > 24h ago` |

### 4.3 No Double-Write Guarantee

The two chat-hydration paths (session-end hook + synthesis ingestion) are explicitly designed to NOT duplicate:

1. **Session-end hook** writes entries and marks the DDB session header with `personalContextExtracted: true`.
2. **Synthesis ingestion** only reads sessions where `personalContextExtracted` is NOT true.
3. The 24h delay on synthesis ingestion prevents racing with the session-end hook.

The task-save-hook and chat-extract-hook cannot duplicate because they operate on different input sources (agent output Learnings markdown section vs. chat conversation messages) and use different persona values (`developer`/`architect` vs. `chat`).

### 4.4 Canonical Entry Schema

All writers produce entries conforming to `PersonalContextEntry` (`models.py`):

```python
{
    "id": "01HXYZ...",          # ULID (generated at write time)
    "type": "learning|synthesis|pattern",
    "owner_sub": "...",         # Force-stamped from identity (UUID)
    "tenant_id": "...",         # Force-stamped from identity
    "visibility": "private|shared",
    "persona": "developer|architect|operations|reviewer|chat",
    "learning_type": "...",     # Free-form category tag
    "content": "...",           # The actual knowledge
    "context": { ... },         # Source metadata (issue, session, synthesis info)
    "confidence": 0.7,          # Initial; raised by validation, lowered by decay/supersession
    "validated": false,         # true only if user explicitly confirmed/edited
    "superseded_by": null,      # Set by synthesis when contradiction + newer validated found
    "created_at": "...",        # Immutable
    "last_accessed_at": "...",  # Updated on recall
    "decay_score": 1.0          # Lowered by nightly decay (floor 0.1)
}
```

**Gap identified (see Section 7):** The `Persona` enum in `models.py` currently has 4 values. The `"chat"` persona from #1325 must be added.

### 4.5 Edit/Delete Propagation (from #1327 UI)

When a user edits content via the UI API:
1. Update AGFS file (OpenViking PUT)
2. Re-embed new content via LiteLLM proxy
3. Set `validated = true`
4. Update Neptune graph vertex (if enabled)
5. Mark syntheses referencing this entry as `stale` (re-synthesis on next cycle)

When a user deletes via the UI API:
1. Hard-delete AGFS file
2. Remove embedding from index
3. Remove Neptune vertex + edges
4. Mark referencing syntheses as stale (or delete if they have no other sources)
5. Audit log: `{action: "user_delete", entry_id, owner_sub, timestamp}`

These propagation rules apply consistently regardless of which writer created the entry.

---

## 5. Unified API Surface

### 5.1 Three Access Patterns, One Store

```
+------------------+       +-------------------+       +------------------+
| Agent Path       |       | User Path         |       | Job Path         |
| (MCP tool)       |       | (Gateway API)     |       | (CronJob/Hook)   |
+------------------+       +-------------------+       +------------------+
| experience:save  |       | GET  /entries     |       | synthesis.run()  |
| experience:recall|       | GET  /entries/:id |       | chat_ingestion() |
| experience:      |       | PATCH /entries/:id|       |                  |
|   list_syntheses |       | DELETE /entries/:id|       |                  |
|                  |       | PATCH .../visibility|     |                  |
|                  |       | POST /export      |       |                  |
|                  |       | GET /syntheses    |       |                  |
+------------------+       +-------------------+       +------------------+
         |                          |                          |
         v                          v                          v
+------------------------------------------------------------------------+
| PersonalContextStore (storage.py)                                       |
| - write_entry() [force-stamps identity]                                 |
| - read_entry() [applies _caller_can_read filter]                        |
| - list_entries() [fail-closed on null identity]                         |
| - delete_entry() [ownership check]                                      |
+------------------------------------------------------------------------+
         |
         v
+------------------------------------------------------------------------+
| OpenViking AGFS                                                         |
| /personal/<owner_sub>/{learnings,syntheses,patterns}/<ulid>.json       |
| /shared/<tenant_id>/{learnings,syntheses,patterns}/<ulid>.json          |
+------------------------------------------------------------------------+
```

### 5.2 Agent Path (MCP `experience` Tool)

**Existing, implemented (#1288-#1295).** Called by agent-worker pods via Context MCP Server.

- `save`: Persist a learning. Identity from `X-Owner-Sub` + `X-Tenant-Id` headers.
- `recall`: Semantic search by query. Returns top-k results ranked by `similarity * decay_score`.
- `list_syntheses`: List synthesis entries for a persona.

Authentication: NetworkPolicy (K8s namespace isolation) + trusted headers from dispatch layer.

### 5.3 User Path (Gateway API, #1327)

**New, not yet implemented.** Called by authenticated users via Cognito JWT.

Base: `/api/personal-context/`

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /entries` | List/search entries (supports semantic search via `q` param) | JWT -> `TokenContext` |
| `GET /entries/:id` | Single entry with graph edges | JWT |
| `PATCH /entries/:id` | Edit content/metadata | JWT, owner-only |
| `DELETE /entries/:id` | Hard delete with propagation | JWT, owner-only |
| `PATCH /entries/:id/visibility` | Toggle private/shared | JWT, owner-only |
| `POST /export` | Export all owned entries (rate-limited: 1/hr) | JWT |
| `GET /syntheses` | List synthesis entries (convenience alias) | JWT |

Authentication: Cognito JWT via `get_current_user` dependency. Gateway calls OpenViking directly (cluster-internal HTTP).

### 5.4 Job Path (CronJob + Hooks)

| Job | Schedule | What It Does |
|---|---|---|
| **Synthesis CronJob** | Nightly 3am UTC | Enumerate unsynthesized learnings, call Claude Sonnet, write synthesis entries, decay stale entries |
| **Chat ingestion** (synthesis pre-stage) | Same CronJob, pre-stage | Catch-up for missed chat sessions (not extracted by session-end hook) |
| **Session-end hook** | On chat session close | Extract durable signals from conversation, write as `persona: "chat"` learnings |
| **Task-save hook** | Post-task completion | Extract learnings from agent output markdown section |

### 5.5 Shared Contracts

All three paths share:
- **Entry schema:** `PersonalContextEntry` (Section 4.4)
- **Identity model:** `CallerIdentity(owner_sub, tenant_id)` (Section 2)
- **Read filter:** `_caller_can_read()` (Section 3)
- **Storage paths:** `/personal/<owner_sub>/...` and `/shared/<tenant_id>/...`
- **Embedding model:** LiteLLM proxy -> Bedrock Titan Embed

No path forks from these shared contracts.

---

## 6. Merged Risk Register

Risks de-duplicated and consolidated from all four designs. Numbered R1-R18 with source attribution.

### Critical Risks

| # | Risk | Sources | Mitigation Summary |
|---|---|---|---|
| **R1** | **Cross-user mis-attribution** — wrong `owner_sub` assigned to a read/write | #1319 R1, #1325 R1, #1327 R1 | UUID validated at 6 layers (Section 2.4). Force-stamping (storage.py:122). Unique index on `cognito_sub`. Detection query (platform UUID vs. Cognito UUID cross-reference). |
| **R2** | **Cross-tenant leakage** — data from tenant A visible to tenant B | #1319-SA R11, #1327 R2 | Tenant-scoped queries (SQL WHERE + AGFS path structure). Isolation tests in CI. Different UUIDs per SA per tenant. |
| **R3** | **Spoofable sender** — forged GitHub webhook impersonates a user | #1319 R2, R9 | HMAC-SHA256 signature verification. Webhook secret in Secrets Manager (Lambda-exclusive read). Signature check is gate zero. |
| **R4** | **SA impersonation** — attacker injects `X-Owner-Sub` with SA UUID | #1319-SA R14 | Headers set by trusted dispatch layer only. K8s NetworkPolicy restricts MCP server access. Agent subprocess cannot modify parent env. |

### High Risks

| # | Risk | Sources | Mitigation Summary |
|---|---|---|---|
| **R5** | **PII/secret capture** — chat content persisted durably | #1325 R2 | Two-layer guard: regex pre-scrub (SECRET_PATTERNS + conversation-specific) + post-extraction filter. Extraction prompt instructs exclusion. Visibility always `private`. |
| **R6** | **Privacy expectation shock** — user discovers "the system knows X about me" | #1327 R3, #1325 R4 | "My Context" UI with one-click delete. Source attribution. Feature flags (opt-out). Clear UI copy. Chat entries tagged `source: "chat"` for selective purge. |
| **R7** | **Dual-store split (regression)** — partial deployment leaves some runs keying on `user_id`, others on `cognito_sub` | #1319 R8 | Feature flag provides atomic rollout. Detection query (periodic). Rollback: flag OFF reverts to pre-fix state. |
| **R8** | **Bot misclassified as human** — `user_kind` corruption | #1319 R4 | Explicit `user_kind` at creation (no mutation endpoint). GitHub `sender.type` cross-check. Metric `BotHumanTypeMismatch`. |
| **R9** | **Unbounded service-identity creation** | #1319-SA R12 | No on-demand SA creation. Pre-provisioned by admin. Quota: max 50 SAs/org. Unregistered bot -> fail-closed. |
| **R10** | **Synthesis/graph inconsistency on edit/delete** — stale syntheses after user edits | #1327 R4 | Mark syntheses stale on source edit/delete. Nightly re-synthesis. UI shows stale indicator. |

### Medium Risks

| # | Risk | Sources | Mitigation Summary |
|---|---|---|---|
| **R11** | **Self-reinforcement loop** — recalled context re-persisted, inflating confidence | #1325 R5 | Extract ONLY from user-role messages (assistant messages excluded). New entries start at confidence 0.7 (never auto-escalate). |
| **R12** | **Noise pollution** — over-eager extraction fills store | #1325 R3 | Min 4-turn threshold. Confidence >= 0.7 floor. Per-session cap (max 5 items). Decay naturally downgrades low-value entries. |
| **R13** | **Chained-run identity drift** — root_human_id drops at chain hop | #1319 R5 | Correlation pointer persisted in DDB with 7-day TTL. Gateway resolves root_human_id -> cognito_sub. E2E assertion. |
| **R14** | **Gateway lookup latency** — slows webhook dispatch | #1319 R6 | 10s timeout. Fail-open for dispatch (fail-closed for context). Already-plumbed call path (zero new latency if flag is ON). Circuit breaker: 3 failures -> skip. |
| **R15** | **Stale correlation pointer** — autonomous run uses expired human attribution | #1319-SA R18 | 7-day TTL. Additional guard: pointer >24h old + no human activity -> treat as autonomous. |
| **R16** | **bot_kind/SA name mismatch** — admin creates SA with wrong name | #1319-SA R16 | Strict equality match. Admin UI shows mapping. Validation on create. Documentation. |
| **R17** | **Shared entry disappears** — owner deletes a shared entry others rely on | #1327 R5 | v1: accept (owner owns data). Delete modal warns "shared with org." Future: admin pin mechanism. |
| **R18** | **OpenViking/LiteLLM unavailability** — services unreachable | #1327 R6 | Feature-flagged. 503 with retry guidance. Circuit breaker. Independent of core gateway. |

### Low Risks

| # | Risk | Sources | Mitigation Summary |
|---|---|---|---|
| **R19** | Unlinked tenants (shadow users, no Cognito link) | #1319 R3 | Fail-closed (no personal context). CloudWatch metric for coverage. Admin UI shows identity coverage per tenant. |
| **R20** | Race condition during identity linking | #1319 R10 | Transient (next run correct). No retry needed. Lambda reads fresh. |
| **R21** | Cost runaway from extraction LLM | #1325 R6 | Per-session token cap. Min turn threshold. Feature flag. ~$1-3/day estimate. |
| **R22** | DynamoDB scan cost for synthesis chat ingestion | #1325 R8 | Sparse GSI on `personalContextExtracted`. Cap 20 sessions/user/run. |
| **R23** | Semantic search abuse | #1327 R7 | 10 searches/min/user rate limit. Query length cap (500 chars). Standard rate-limit middleware. |
| **R24** | Export endpoint abuse | #1327 R8 | 1 export/hour. Audit log. Standard auth. |

### Cross-Cutting Theme: Cross-User / Cross-Tenant Mis-Attribution

The through-line across all four designs is: **no data may EVER be attributed to or visible from the wrong identity**. The defense-in-depth story:

1. **Source authentication** — Cognito JWT (chat/UI), HMAC signature (GitHub), IAM/IRSA (service accounts)
2. **Identity resolution** — Single gateway endpoint (canonical source: Postgres `users` table)
3. **Transport integrity** — `owner_sub` in SQS envelope (encrypted at rest, KMS), env var (process-scoped), MCP header (localhost loopback)
4. **Write-time enforcement** — Force-stamping (client-supplied `owner_sub` ignored)
5. **Read-time enforcement** — `_caller_can_read()` filter at every access point
6. **Storage-time isolation** — Filesystem path partitioning (`/personal/<uuid>/`)
7. **Detection** — Periodic health check: `owner_sub` values cross-referenced against `users.id` vs `users.cognito_sub`

---

## 7. Contradiction / Gap Report

### 7.1 Contradictions Found (Resolved)

| # | Contradiction | Resolution |
|---|---|---|
| C1 | **`cognito_sub` field semantics.** #1319 uses it for human Cognito subs. #1319-SA reuses it for `ServiceAccount.id` (not a Cognito sub). | **Resolved: acceptable.** The MCP server validates it as UUID format only (no Cognito-specific semantics downstream). The field name is misleading but changing it would break backward compat. `owner_kind` field disambiguates. Long-term (optional): rename to `owner_sub` in envelope v2. |
| C2 | **`Persona` enum.** #1325 proposes a new `"chat"` persona. Current `models.py:27-33` only has 4 values (operations, developer, architect, reviewer). | **Resolved: add `"chat"` to Persona enum.** The extraction hook needs it. Impact: zero (additive enum expansion, existing entries unaffected). Migration file: add `chat = "chat"` to the Python Enum. |
| C3 | **Visibility default.** Task-save-hook uses `"private"` (configurable). Chat-extract-hook mandates `"private"` (never configurable). #1327 allows user to toggle. | **Resolved: no contradiction.** All NEW entries default to `private`. Task-save-hook allows `shared` (agent can choose). Chat-extract always `private` (higher PII risk). User can toggle any entry's visibility post-creation. Consistent. |
| C4 | **Who can read service-account stores?** #1319-SA says "SA stores are private to the automation." #1327 gives users full CRUD on their entries. | **Resolved: no conflict.** Users only see entries where `owner_sub == their cognito_sub`. SA entries have `owner_sub == SA.id`. No human's `owner_sub` matches an SA's — they're structurally disjoint UUID sets. Admin read-only API for SA stores is a future concern (out of scope). |

### 7.2 Gaps Found (Require Resolution Before Implementation)

| # | Gap | Which Docs | Required Resolution |
|---|---|---|---|
| G1 | **Gateway -> agent-context connectivity.** #1327 needs the gateway to call OpenViking + LiteLLM proxy. No such connectivity exists today (confirmed: grep found 0 references). | #1327 | **File Issue:** NetworkPolicy + K8s service addressing. Must ship BEFORE the user API. Included in roadmap Phase 2.1. |
| G2 | **`"chat"` persona not in Enum.** Required by #1325 but not present in current `models.py`. | #1325 | **Action:** Add `chat = "chat"` to `Persona` enum. Zero-risk, additive. Include in #1325 Issue A implementation. |
| G3 | **`personalContextExtracted` DDB attribute not defined.** #1325 requires this on session headers in `chat-context` table, but no GSI exists. | #1325 | **File Issue:** DDB schema update (sparse GSI). Include in #1325 Issue A/B implementation. |
| G4 | **Synthesis CronJob has no DDB read permission.** #1325 Issue B requires synthesis job to query `chat-context` table. Current IRSA policy (`agent-context/terraform/`) doesn't grant DDB access. | #1325 | **File Issue:** IAM policy update for synthesis pod. Include in #1325 Issue B. |
| G5 | **`POST /internal/v1/resolve-user-by-id` endpoint doesn't exist.** #1319 needs it for chained-run resolution (takes `user_id`, returns `cognito_sub`). | #1319 | **Include in #1319 implementation.** Minimal new internal endpoint. |
| G6 | **`POST /internal/v1/resolve-service-account` endpoint doesn't exist.** #1319-SA needs it for autonomous-bot resolution. | #1319-SA | **Include in #1319-SA implementation.** Depends on unique constraint migration. |
| G7 | **`owner_kind` field not in current SQS envelope schema.** Both #1319-SA and the unified design need it. | #1319-SA | **Include in #1319-SA implementation.** Backward-compatible (defaults to `"human"` if absent). |
| G8 | **User opt-out mechanism for chat memory.** #1325 proposes `CHAT_CONTEXT_SAVE_ENABLED` (platform-wide) + future per-user opt-out. #1327 mentions it but doesn't design it. | #1325, #1327 | **Defer:** Per-user opt-out is Issue D in #1325. Not needed for initial launch (platform flag sufficient for internal/dev). |

---

## 8. Implementation Roadmap (Dependency-Ordered)

### Legend

- **GATE:** Must complete before any downstream work
- **PARALLEL:** Can run concurrently with items in the same tier
- **MILESTONE:** Marks a significant integration point

### Phase 0: Already Built (COMPLETE)

**What's deployed:** Core personal-context store (#1288-#1295)
- `experience` MCP tool (save/recall/list_syntheses)
- OpenViking/AGFS storage with owner-scoped paths
- Neptune graph (feature-flagged)
- Identity middleware (`X-Owner-Sub`, `X-Tenant-Id`)
- Personal-context-headers.ts (header builder from dispatch metadata)
- Recall-at-task-start hook
- Experience-save-hook (post-task)
- Synthesis CronJob (nightly)
- Embedding client (LiteLLM proxy -> Bedrock Titan)
- Isolation tests

**Status:** Functional for chat/webchat path. GitHub-webhook path produces wrong `owner_sub`. Service accounts have no context. Chat conversations not hydrated. No user visibility/control.

---

### Phase 1: Identity Fix (GATE - Must Complete First)

**Issue:** #1319 Implementation Issue A
**Priority:** P0 (blocks everything else)
**Rationale:** Without correct `owner_sub` on the GitHub-webhook path, any data written by webhook-triggered agents is mis-keyed. This must be fixed before adding MORE writers (chat hydration) or readers (UI API).

**Deliverables:**
1. Gateway: Add `cognito_sub` to `ResolveUserResponse` (extend existing endpoint)
2. Gateway: Add `POST /internal/v1/resolve-user-by-id` (for chained runs)
3. Webhook-ingress Lambda: `gateway_client.resolve_user_by_identity()` includes `cognito_sub`
4. Webhook-ingress Lambda: `ResolvedIdentity` dataclass gains `cognito_sub` field
5. Webhook-ingress Lambda: `handler.py:635` uses `resolved.cognito_sub` (not `user_id`)
6. Bot/human type mismatch detection (metric)
7. Human-rooted chain: resolve `root_human_id` -> `cognito_sub`

**Deploy sequence:** Gateway FIRST (response shape change), then webhook-ingress (consumer).
**Feature flag:** `RESOLVE_CANONICAL_VIA_GATEWAY=true` (already exists, gates the fix).
**Tests:** Unit + integration (see #1319 Issues A+B spec).

---

### Phase 2: Infrastructure Prerequisites (PARALLEL within phase)

Can start after Phase 1 is deployed (or in parallel if teams are separate).

#### 2.1 NetworkPolicy for Gateway -> Agent-Context

**Issue:** #1327 Issue D
**Deliverable:** K8s NetworkPolicy allowing gateway pods to reach OpenViking (port 1933) + LiteLLM proxy (port 4000) in agent-context namespace.
**File:** `modules/agent-context/manifests/networkpolicy-gateway-ingress.yaml`
**Blocks:** #1327 Issues A+B (user-facing API).

#### 2.2 Service Account Provisioning (for Autonomous Runs)

**Issue:** #1319-SA Issue D (partial)
**Deliverables:**
1. Migration `017_service_account_unique_name.py`: UNIQUE constraint on `(org_id, name)`
2. Gateway: `POST /internal/v1/resolve-service-account` endpoint
3. Seed SAs for known `bot_kind` values in dev tenant

**Blocks:** SA path activation in webhook handler.

#### 2.3 Persona Enum Extension

**Deliverable:** Add `chat = "chat"` to `Persona` enum in `models.py`.
**Trivial change, can merge immediately.** Required before chat hydration writes entries.

---

### Phase 3: Service-Account Context Activation

**Issue:** #1319-SA Issue D (webhook handler changes)
**Depends on:** Phase 1 (identity fix), Phase 2.2 (SA infra)

**Deliverables:**
1. Handler: autonomous bot + registered SA -> `cognito_sub = SA.id`, `owner_kind = "service_account"`
2. Handler: autonomous bot + no SA -> fail-closed (empty)
3. `owner_kind` field added to SQS envelope
4. Feature flag: reuses `RESOLVE_CANONICAL_VIA_GATEWAY`

**Deploy:** Webhook-ingress after gateway SA endpoint is live.

---

### Phase 4: Chat Hydration (PARALLEL with Phase 3)

**Issue:** #1325 Issues A+B
**Depends on:** Phase 1 (identity fix), Phase 2.3 (Persona enum)

#### 4.1 Session-End Extraction Hook (#1325 Issue A)

**Deliverables:**
1. `chat-experience-extract-hook.ts` (mirrors experience-save-hook.ts pattern)
2. Extraction LLM prompt (§2.3 of #1325 design)
3. PII/secret guard (two-layer: regex + LLM instruction)
4. `personalContextExtracted` attribute on DDB session header
5. `CHAT_CONTEXT_SAVE_ENABLED` feature flag (default OFF)
6. Wire hook at session-close in `complex-task-chat-agent.ts`

#### 4.2 Synthesis Pipeline Chat Ingestion (#1325 Issue B)

**Depends on:** 4.1 deployed
**Deliverables:**
1. `chat_ingestion.py` — DDB query for unextracted sessions
2. Synthesis pipeline pre-stage integration
3. IAM: synthesis pod DDB read/update permission on `chat-context` table
4. Sparse GSI on `personalContextExtracted`

---

### Phase 5: User-Facing API + UI (PARALLEL with Phase 4)

**Issue:** #1327 Issues A+B+C
**Depends on:** Phase 2.1 (NetworkPolicy)

#### 5.1 Backend API (#1327 Issue A)

**Deliverables:**
1. FastAPI router: `/api/personal-context/` (7 endpoints)
2. AGFS client module (`agfs_client.py`)
3. LiteLLM embedding client for semantic search
4. Edit/delete propagation (re-embed, mark syntheses stale, graph cleanup)
5. Feature flag: `PERSONAL_CONTEXT_ENABLED` (default false until agent-context deployed)

#### 5.2 Frontend (#1327 Issue B)

**Deliverables:** "My Context" page (Learnings/Insights tabs, search, filter, edit/delete modals)
**Can develop in parallel with 5.1** using mocked API responses.

#### 5.3 Isolation Tests (#1327 Issue C)

**Deliverables:** Cross-user + cross-tenant boundary test suite (CI-blocking).
**Ships with 5.1.**

---

### Phase 6: Validation + Observability

**Issues:** #1319 Issue B (E2E assertions), #1319 Issue C (metrics), #1325 Issue E (anti-loop tests)
**Depends on:** Phases 1-5 deployed

**Deliverables:**
1. E2E assertion: same human via chat and GitHub -> same `owner_sub` in store
2. E2E assertion: autonomous bot with SA -> SA.id in store
3. E2E assertion: chat extraction does NOT re-persist recalled content
4. CloudWatch metrics namespace `ADP/PersonalContext`:
   - `ResolutionSuccess`, `ResolutionFailedUnlinked`, `ResolutionFailedGatewayError`
   - `BotHumanTypeMismatch`, `ChainedRunIdentityPropagated`
   - `SAResolutionSuccess`, `SANotFound`, `AutonomousRunNoContext`
   - `ChatExtractionSuccess`, `ChatExtractionSkipped`
5. Dual-store detection periodic job

---

### Phase 7: Recall Enablement + User Controls

**Issues:** #1325 Issues C+D
**Depends on:** Phase 4 (store populated), Phase 5 (UI exists)

#### 7.1 Enable Chat Recall (#1325 Issue C)

Set `PERSONAL_CONTEXT_RECALL_ENABLED=true` for chat agent deployment. Monitor recall hit rate.

#### 7.2 User Opt-Out + Visibility (#1325 Issue D)

User settings: `chat_memory_enabled: false` -> extraction hook checks before firing.
Gateway API: `DELETE /api/personal-context/entries?source=chat` for bulk chat-entry deletion.

---

### MILESTONE: #1287 Complete

After Phase 7, the Personal Context system is fully operational:
- All identity paths resolve correctly
- Human, service-account, and chat contexts all flowing
- Users can see, search, correct, and delete their context
- Synthesis producing cross-session insights
- Feature flags can be toggled for progressive rollout

---

### Roadmap Visualization

```
Phase 0 [DONE]
   |
Phase 1: Identity Fix (#1319) ─── GATE ─── [MUST COMPLETE FIRST]
   |
   +── Phase 2.1: NetworkPolicy (#1327-D)  ─┐
   |                                         |
   +── Phase 2.2: SA Infra (#1319-SA)    ─┐ |
   |                                      | |
   +── Phase 2.3: Persona Enum           | |
   |                                      | |
   +── Phase 3: SA Activation            ─┘ |  (depends on 2.2)
   |                                         |
   +── Phase 4: Chat Hydration               |  (depends on 2.3)
   |     4.1: Session-end hook               |
   |     4.2: Synthesis ingestion            |
   |                                         |
   +── Phase 5: User API + UI               ─┘  (depends on 2.1)
   |     5.1: Backend API
   |     5.2: Frontend
   |     5.3: Isolation tests
   |
Phase 6: Validation + Observability  (depends on 1-5)
   |
Phase 7: Recall + User Controls      (depends on 4, 5)
   |
   === MILESTONE: #1287 COMPLETE ===
```

---

## 9. Recommendation: Supersede Per-Doc Issue Lists

**Recommendation: YES — supersede the implementation lists in individual design docs in favor of this roadmap.**

Rationale:
1. The individual docs' issue lists overlap (e.g., #1319 and #1319-SA both define "Issue A" for the same handler change).
2. Dependencies between issues in DIFFERENT docs aren't captured in those docs (e.g., #1325 depends on #1319 but #1319's issue list doesn't reference #1325).
3. A single ordered roadmap (Section 8) resolves all cross-doc dependencies in one place.

**Action:** Close or label as "superseded by #1332" the implementation items listed in each design doc. File fresh implementation issues based on this roadmap's phases. Reference this document as the canonical design.

**Exception:** Issues #1288-#1295 (already implemented from EPIC #1283) are COMPLETE — no action needed on those.

---

## 10. Open Questions (Requiring Stakeholder Decision)

These are carried forward from the four designs, de-duplicated:

| # | Question | From | Recommended Answer |
|---|---|---|---|
| Q1 | Should `RESOLVE_CANONICAL_VIA_GATEWAY` become always-on? | #1319 | Yes, after Phase 6 validates. The flag exists for rollback safety during rollout; once validated, remove. |
| Q2 | Chained-run depth limit? | #1319 | No explicit limit. 7-day correlation pointer TTL is sufficient. Chains don't run >7 days. |
| Q3 | Should SA stores be visible to tenant admins? | #1319-SA | Yes (read-only admin API), but out of scope for this EPIC. File as follow-up. |
| Q4 | Shared SAs across tenants? | #1319-SA | No. One SA per tenant even for platform agents. Keeps isolation absolute. |
| Q5 | Retention/eviction for SA stores? | #1319-SA | Defer to Phase 7 (same strategy as human stores: decay + archival flagging, never auto-delete). |
| Q6 | Turn threshold for chat extraction? | #1325 | 4 substantive user turns. Can tune via env var (`CHAT_CONTEXT_MIN_TURNS`). |
| Q7 | Opt-in vs opt-out for chat memory? | #1325 | Platform-wide opt-in (feature flag OFF by default). Per-user opt-out comes in Phase 7.2. |
| Q8 | Can users edit syntheses directly? | #1327 | No. Syntheses are auto-generated; users can only delete them. Edit your source learnings instead. |

---

## 11. Decision Log (Consolidated)

| # | Decision | Rationale | Alternatives Rejected |
|---|---|---|---|
| D1 | One `owner_sub` (UUID) as the universal identity key | Simple, validates everywhere, single-partition storage | Compound key (type+sub), cognito-only (excludes SAs) |
| D2 | `ServiceAccount.id` (UUID) as SA `owner_sub` | Passes UUID validation, tenant-scoped, already provisioned | Synthetic string (fails validation), fake Cognito sub (semantic pollution) |
| D3 | Pre-provisioned SAs (not on-demand) | Prevents unbounded store creation, admin control | On-demand (sprawl risk), hybrid (complexity) |
| D4 | Human-rooted chains attribute to the human | Human initiated the work; their context matters | SA takes precedence (loses human context), both (complex) |
| D5 | Reuse `cognito_sub` envelope field for SA.id | Zero downstream changes (MCP server is agnostic) | New field (breaks backward compat) |
| D6 | Chat extraction: user-role messages only | Prevents self-reinforcement loop (assistant messages contain recalled context) | All messages (feedback loop risk), [RECALLED] tags (complex) |
| D7 | Session-end hook is primary; synthesis ingestion is catch-up | Immediate extraction is higher-value; synthesis handles missed sessions | Synthesis-only (24h delay), both-write-always (double-write risk) |
| D8 | Gateway proxies to OpenViking directly (no shared Postgres table) | Personal context stays in AGFS (its designed store); no schema coupling | Shared Postgres table (wrong store), direct frontend→OpenViking (no auth layer) |
| D9 | Hard delete (no tombstone) | Compliance-friendly (GDPR right to erasure); user trust | Soft delete (tombstone complexity), 30-day retention (compliance risk) |
| D10 | Feature flags gate all new capabilities | Atomic rollout; easy rollback; progressive enablement | Always-on (risky), per-endpoint toggles (proliferation) |
| D11 | `"chat"` as a distinct persona value | Clean separation from task-derived learnings; enables per-source filtering | Inherit session agent persona (conflates source), no persona (loses signal) |
| D12 | Same `_caller_can_read` logic in gateway API and MCP middleware | Single isolation invariant, provably identical behavior | Separate implementations (divergence risk), shared library (over-engineering for 2 call sites) |

---

## Appendix A: File Map (Where Each Concern Lives)

| Concern | File(s) | Module |
|---|---|---|
| Identity validation | `personal_context/identity.py` | agent-context |
| Entry schema | `personal_context/models.py` | agent-context |
| Storage / isolation | `personal_context/storage.py` | agent-context |
| MCP experience tool | `personal_context/experience_tool.py` | agent-context |
| Synthesis pipeline | `personal_context/synthesis.py` | agent-context |
| Graph (Neptune) | `personal_context/graph.py` | agent-context |
| Embeddings | `personal_context/embeddings.py` | agent-context |
| Header builder | `complex-task-chat/personal-context-headers.ts` | agent-factory/agent |
| Recall hook | `complex-task-chat/recall-at-task-start.ts` | agent-factory/agent |
| Save hook | `experience-save-hook.ts` | agent-factory/agent |
| Worker entrypoint | `agent-worker-image/entrypoint.py` | agent-factory |
| Webhook handler | `webhook-ingress/lambda/github/handler.py` | agent-factory |
| Identity resolver | `webhook-ingress/lambda/common/identity_resolver.py` | agent-factory |
| Gateway client | `webhook-ingress/lambda/common/gateway_client.py` | agent-factory |
| Resolve-user endpoint | `gateway/src/internal/routes.py` | gateway |
| User-facing API (new) | `gateway/src/personal_context/routes.py` | gateway |
| Frontend (new) | `gateway/frontend/src/pages/MyContext.tsx` | gateway |
| Synthesis CronJob manifest | `agent-context/manifests/personal-context-synthesis-cronjob.yaml` | agent-context |
| NetworkPolicy (new) | `agent-context/manifests/networkpolicy-gateway-ingress.yaml` | agent-context |

---

## Appendix B: Implementation Issues to File (Summary)

| Phase | Issue Title | Depends On | Module(s) |
|---|---|---|---|
| 1 | Implement `cognito_sub` resolution in webhook path | — | gateway, agent-factory/webhook-ingress |
| 2.1 | Add NetworkPolicy for gateway -> agent-context | — | agent-context |
| 2.2 | SA unique constraint migration + resolve endpoint | Phase 1 | gateway |
| 2.3 | Add `chat` to Persona enum | — | agent-context |
| 3 | SA context activation in webhook handler | 1, 2.2 | agent-factory/webhook-ingress |
| 4.1 | Chat session-end extraction hook | 1, 2.3 | agent-factory/agent |
| 4.2 | Synthesis pipeline chat ingestion | 4.1 | agent-context |
| 5.1 | Personal Context gateway API (backend) | 2.1 | gateway |
| 5.2 | "My Context" frontend page | 5.1 (can start with mocks) | gateway/frontend |
| 5.3 | Cross-user/tenant isolation test suite | 5.1 | gateway |
| 6 | E2E assertions + observability metrics | 1-5 | agent-factory, agent-context |
| 7.1 | Enable personal-context recall for chat | 4.1 | agent-factory/agent |
| 7.2 | User opt-out + chat memory controls | 5.1, 4.1 | gateway |
