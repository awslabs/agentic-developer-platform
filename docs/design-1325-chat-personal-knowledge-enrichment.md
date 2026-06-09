# Design: Chat <-> Personal-Knowledge Enrichment

**Issue**: #1325 (sub of EPIC #1287)
**Status**: Design proposal
**Author**: @agent-architect
**Date**: 2026-06-09
**Prerequisites**: #1319 (identity fix), #1287 phases 1-4 deployed

---

## 1. Problem Statement

Today, chat conversations are the richest source of durable user signal —
preferences, ownership declarations, recurring problems, architectural decisions,
team context. This signal is written to:

- `adp-<env>-chat-context` DynamoDB table (PK: `session#{sessionId}`, SK: `msg#{messageId}`)
- TTL-expiring (configurable, typically 30+ days)
- Never hydrated into the personal-knowledge store (OpenViking/Neptune)

The only existing writer into personal-context is `experience-save-hook.ts` (#1294),
which fires post-task on **agent work** (deploys/PRs/reviews via `agent-worker.ts:1643`).
The chat agent (`complex-task-chat-agent.ts`) has no equivalent post-session save path.

**Result**: the most user-defining signal expires from DynamoDB and is permanently lost.

---

## 2. Recommended Hydration Design

### 2.1 Extraction Trigger: Dual-path (session-end hook + synthesis ingestion)

| Path | When | What it captures | Latency | Cost |
|------|------|------------------|---------|------|
| **Session-end hook** (primary) | Chat session closes (idle timeout or explicit end) | Durable signals from that session's conversation | Immediate (same-turn) | ~$0.01-0.03/session (one extraction LLM call) |
| **Synthesis-job chat ingestion** (secondary) | Daily 3am UTC dream-cycle (#1291) | Cross-session patterns, corrections, reinforcement | Batch (next cycle) | Amortized into existing synthesis cost |

**Recommendation: session-end hook is the primary writer; synthesis ingestion is
a secondary enrichment path for cross-session pattern detection.**

Rationale:
- The session-end hook mirrors the proven `experience-save-hook.ts` pattern, but
  instead of parsing a `### Learnings` markdown section, it uses a lightweight LLM
  extraction call to distill the conversation.
- Synthesis-job ingestion addresses sessions where the session-end hook was skipped
  (crash, forced restart) and enables cross-session pattern detection that a single
  session can't observe.
- The two paths do NOT duplicate: the session-end hook marks extracted sessions
  in the chat-context DDB record (attribute: `personalContextExtracted: true`);
  the synthesis job only reads sessions NOT marked extracted.

### 2.2 Session-End Hook Design

```
┌─────────────────────────────────────────────────────────────────┐
│  Chat Session Lifecycle (complex-task-chat-agent.ts)            │
│                                                                 │
│  ... conversation turns ...                                     │
│                                                                 │
│  [Session end detected: idle timeout OR user goodbye]           │
│         │                                                       │
│         ▼                                                       │
│  ┌─────────────────────────────────────────────────────┐       │
│  │  chat-experience-extract-hook.ts (NEW)              │       │
│  │                                                     │       │
│  │  1. Gate: CHAT_CONTEXT_SAVE_ENABLED (default: off)  │       │
│  │  2. Gate: identity present (fail-closed)            │       │
│  │  3. Gate: session has ≥N substantive turns (N=4)    │       │
│  │  4. Collect session messages (from dynamo store)    │       │
│  │  5. LLM extraction call (see §2.3)                 │       │
│  │  6. Secret/PII guard (see §2.5)                    │       │
│  │  7. Write distilled entries via Context MCP Server  │       │
│  │  8. Mark session header: personalContextExtracted   │       │
│  └─────────────────────────────────────────────────────┘       │
│                                                                 │
│  [Non-blocking: extraction failure → log + continue]           │
└─────────────────────────────────────────────────────────────────┘
```

**Trigger heuristic for "session end":**
- The chat agent already has a concept of session lifecycle via the `dynamo-store.ts`
  session header (`status` field). When status transitions to `completed` or after
  an idle timeout (configurable, e.g., 30 min of no new messages), fire the hook.
- Alternative: fire on the LAST assistant turn if the turn count threshold is met.
  This is simpler and doesn't require a separate timer. **Recommended: fire on
  explicit session-close or on the next session-open if the previous session
  wasn't extracted.** (Lazy extraction on next-session-start ensures no signal is
  lost even if the pod terminates unexpectedly.)

### 2.3 Extraction Criteria: What Conversational Signal is Durable

Not every message is a learning. The extraction LLM call uses a focused prompt:

```
System: You are a knowledge-extraction engine. Given a conversation between a
user and an AI assistant, extract ONLY durable, user-defining information.

Extract these categories:
- PREFERENCE: Stated preferences about tools, processes, communication style,
  code conventions, infrastructure choices
- OWNERSHIP: What the user owns, maintains, is responsible for (repos, services,
  teams, accounts)
- DECISION: Explicit decisions made during the conversation (architectural,
  process, priority)
- RECURRING_ISSUE: Problems the user mentions experiencing repeatedly
- CONTEXT: Organizational context revealed (team structure, stakeholders,
  approval chains)

DO NOT extract:
- Transient task details ("deploy module X today")
- Greetings, acknowledgments, small talk
- Information the assistant provided (only what the USER reveals)
- Anything that looks like a credential, secret, API key, or password

For each extracted item, output JSON:
{ "category": "...", "content": "...", "confidence": 0.7-1.0 }

If nothing durable was revealed in this conversation, return an empty array.
```

**Minimum quality bar**: confidence >= 0.7. Items below threshold are discarded.

**Token budget for extraction call**: max 4000 input tokens from the conversation
(most recent messages prioritized; older messages summarized via the existing
summary mechanism in `dynamo-store.ts` SK: `sum#*`). Output capped at 1024 tokens.

### 2.4 Identity: Correct `owner_sub` Attribution

All writes MUST carry the correct `owner_sub` (the user's `cognito_sub`) per #1319.

**For chat sessions**, identity is already correctly propagated:
- `complex-task-chat-agent.ts:290-294` builds `personalContextIdentity` from
  dispatch metadata (`cognito_sub`, `tenant_id`, `user_id`)
- The chat path receives these from the gateway session (Cognito-authenticated)
- Unlike the GitHub-webhook path (which #1319 fixes), the chat path has no
  identity gap — the user authenticated via Cognito to open the chat session

**Fail-closed rule**: if `personalContextIdentity` is null (no `cognito_sub`
resolved), the extraction hook MUST skip entirely. No orphaned or mis-attributed
writes. This mirrors `experience-save-hook.ts:106-110`.

**Additional guard**: before writing, assert that the `owner_sub` matches the
session header's `ownerUserId`. If they diverge (shouldn't happen, but defense-in-depth),
skip and log an error.

### 2.5 PII/Secret Guard

Conversations carry higher PII risk than agent task outputs. The guard is:

1. **Reuse existing `SECRET_PATTERNS`** from `experience-save-hook.ts:46-57`
   (AWS keys, GitHub PATs, JWTs, Slack tokens, key=value secrets).

2. **Add conversation-specific patterns** (new):
   - Email addresses: `/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/`
   - Phone numbers: `/\+?[\d\s\-()]{10,}/` (loose match, over-redacts is fine)
   - Credit card numbers: `/\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b/`
   - SSN: `/\b\d{3}-\d{2}-\d{4}\b/`

3. **Two-layer guard**:
   - Layer 1 (pre-extraction): scrub raw conversation before sending to extraction LLM
   - Layer 2 (post-extraction): run extracted items through secret patterns before persisting.
     Any item matching a pattern → skip (same behavior as `experience-save-hook.ts:138-140`).

4. **LLM instruction**: the extraction prompt explicitly instructs "DO NOT extract
   anything that looks like a credential, secret, API key, or password." This is a
   soft guard (LLM can fail), hence the regex hard guard.

### 2.6 Synthesis Job Chat Ingestion (Secondary Path)

The existing `SynthesisPipeline._enumerate_users()` in `synthesis.py:281-299`
scans `/personal/` for unsynthesized learnings. It does NOT read DynamoDB.

**Extension for chat ingestion** (added as a pre-stage to the synthesis pipeline):

1. Query DynamoDB `adp-<env>-chat-context` for sessions where:
   - `personalContextExtracted` is NOT true (missed by session-end hook)
   - Session `lastActivityAt` > 24h ago (don't race with the session-end hook)
   - Session has ≥ N substantive turns (same threshold as session-end hook)
2. Run the same extraction LLM call as §2.3
3. Write extracted entries to personal-context
4. Mark session as extracted

**Cost guard**: process at most 20 sessions per synthesis run per user. Sessions
older than the DynamoDB TTL are gone anyway — this is a best-effort catch-up, not
a guarantee.

### 2.7 What Gets Written to Personal-Context

Extracted items are written via the same Context MCP Server API as
`experience-save-hook.ts`:

```
POST {CONTEXT_MCP_SERVER_URL}/tools/call
Headers: X-Owner-Sub: {cognito_sub}, X-Tenant-Id: {tenant_id}
Body: {
  name: "experience",
  arguments: {
    action: "save",
    persona: "chat",           // NEW persona value for chat-derived learnings
    content: "<extracted content>",
    learning_type: "<category>",  // preference, ownership, decision, etc.
    context: {
      source: "chat",
      session_id: "<session_id>",
      extracted_at: "<ISO8601>"
    },
    visibility: "private"      // Chat-derived entries are ALWAYS private by default
  }
}
```

**New persona value: `"chat"`**. This distinguishes chat-derived learnings from
task-derived ones (developer/architect/reviewer/operations). The synthesis job
can cross-reference chat-persona entries with task-persona entries to find
reinforcing patterns.

### 2.8 Layering: What Lives Where

| Store | Content | Lifecycle | Access Pattern |
|-------|---------|-----------|----------------|
| DynamoDB `chat-context` | Raw conversation messages + summaries | Ephemeral, TTL-expiring (30+ days) | Session replay, context window assembly |
| DynamoDB `agent-memory` | User preferences + facts (keyword-retrievable) | Kind-based TTL (90-180 days), extends on read | Quick keyword lookup at session start |
| Personal-context (OpenViking/Neptune) | Distilled durable insights from chat + tasks | Permanent (decay-scored, never deleted) | Semantic recall via embeddings + graph traversal |

**Invariant: the personal-context store NEVER contains raw conversation messages.**
It contains only the distilled, durable signal extracted from conversations.
DynamoDB remains the canonical raw conversation log.

---

## 3. Session-Start Composition Design

### 3.1 Current State (Already Wired)

The chat agent performs TWO retrievals at session start:

1. **DynamoDB memory** (`complex-task-chat-agent.ts:204-209`):
   - Source: `adp-<env>-agent-memory` table
   - Query: keyword match on user message
   - Scope: `user` + `component`
   - Kinds: `preference`, `fact`
   - Token budget: 500
   - Output: `MemoryRecord[]` → formatted as `<memories>` XML block

2. **Personal-context recall** (`complex-task-chat-agent.ts:296-306`):
   - Source: Context MCP Server → OpenViking/Neptune
   - Query: semantic similarity on user message
   - Scope: owner-isolated (cognito_sub)
   - Token budget: 800
   - Output: `RecalledLearning[]` → formatted as `<prior-experience>` XML block

### 3.2 Composition in System Prompt

`composeSystemPrompt` in `persona-loader.ts:105-155` composes these as:

```xml
<persona>
  [base system prompt + channel directive + attachments + credentials + AWS hint]
</persona>

<persona-learnings>
  [component-specific learnings from memory, scoped to persona]
</persona-learnings>

<prior-experience>
  [recalled personal-context entries — semantic, durable, cross-session]
  The following are relevant learnings recalled from your prior experience
  with this user. These are possibly-stale memories — weigh them as context,
  do not blindly trust them.
  <learning id="..." confidence="85%" relevance="72%">
    User prefers Terraform modules over standalone resources...
  </learning>
</prior-experience>

<memories>
  [DynamoDB agent-memory entries — keyword-matched, session-local, recent]
  <memory id="..." scope="user:alice, component:chat" kind="preference" updated="...">
    Prefers concise responses over verbose explanations
  </memory>
</memories>
```

### 3.3 Design Confirmation + Recommendations

**Confirm: this composition is correct.** The two sources serve complementary purposes:

| Source | Retrieval | Recency bias | Durability | Signal type |
|--------|-----------|--------------|------------|-------------|
| `<memories>` (DynamoDB) | Keyword | Recent (90-180d TTL) | Ephemeral | Session-level prefs/facts |
| `<prior-experience>` (personal-context) | Semantic | None (decay-scored) | Permanent | Cross-session synthesized insights |

**Recommendation: enable recall for chat (`PERSONAL_CONTEXT_RECALL_ENABLED=true`)**
once #1319 identity fix ships and personal-context store is populated. The two
retrieval paths are complementary, not redundant:
- DynamoDB memories capture "what did this user say recently in this chat component?"
- Personal-context recall captures "what do we know about this user across all their
  interactions (chat + tasks), synthesized over time?"

**Dedup/Precedence Rule**: If the same fact appears in both sources (e.g., "user
prefers TypeScript"), the personal-context version takes precedence (higher signal:
it survived synthesis). No explicit dedup mechanism needed — the LLM naturally
handles slightly-redundant context. The two XML sections have different labels
(`<memories>` vs `<prior-experience>`) which signal their provenance to the model.

**Anti-self-reinforcement guard (critical)**: The extraction hook (§2.2) MUST NOT
extract content that was INJECTED by the recall mechanism. Implementation:

1. Tag recalled content in the system prompt with a marker: `[RECALLED]`
2. The extraction LLM prompt instructs: "Ignore any content marked [RECALLED] —
   these are pre-existing memories, not new user-revealed information."
3. Alternatively (simpler): only extract from USER role messages, never from
   ASSISTANT role messages. Since recalled content only appears in the assistant's
   system prompt or assistant responses, it's automatically excluded.

**Recommended approach: Option 3 (extract only from user messages).** This is the
simplest, most robust anti-loop mechanism. The extraction prompt is amended:
"Extract durable information ONLY from messages with role=user. Ignore all
assistant messages."

---

## 4. Risk Register

| # | Risk | Severity | Likelihood | Impact | Mitigation |
|---|------|----------|------------|--------|------------|
| R1 | **Mis-attribution**: learning persisted under wrong `owner_sub` | Critical | Low (chat path has no identity gap) | User A's private context visible to User B; privacy breach | Fail-closed on missing identity (§2.4). Assert `owner_sub == session.ownerUserId`. Chat path already propagates Cognito identity correctly. Depends on #1319 only for the webhook path — chat is already correct. |
| R2 | **PII/secret capture**: conversation contains sensitive content persisted durably | High | Medium | Credential exposure; compliance violation | Two-layer guard (§2.5): regex pre-scrub + post-extraction filter. Extraction prompt instructs exclusion. Visibility always `private` for chat-derived entries. Audit log on every write. |
| R3 | **Noise pollution**: over-eager extraction fills store with low-value chatter | Medium | Medium | Recall quality degrades (irrelevant results in `<prior-experience>`) | Minimum turn threshold (≥4 substantive turns). Confidence floor (≥0.7). Per-session cap (max 5 extracted items). Synthesis job (#1291) naturally decays low-value entries via confidence decay. |
| R4 | **Privacy/expectation violation**: user doesn't expect casual chat to become durable memory | High | Medium | User trust erosion; potential regulatory issue (GDPR right to erasure) | Feature flag (`CHAT_CONTEXT_SAVE_ENABLED`, default OFF). Future: user-visible "memory" indicator in UI + opt-out toggle. Chat-derived entries tagged `source: "chat"` in context field for selective deletion. |
| R5 | **Self-reinforcement loop**: recalled context re-persisted, inflating confidence | Medium | Medium | Unvalidated claims get reinforced; hallucinated "facts" become permanent | Extract ONLY from user-role messages (§3.3). Recalled content only appears in system prompt/assistant responses — never in user messages. New entries start at confidence 0.7 (never auto-escalate on re-encounter). |
| R6 | **Cost runaway**: extraction LLM call on every session | Low | Low | Unexpected token spend | Per-session token cap (4000 input, 1024 output). Minimum turn threshold skips trivial sessions. Feature flag allows immediate disable. Estimated: $0.01-0.03/session × ~100 sessions/day = $1-3/day. |
| R7 | **Session-end hook race with session start**: extraction not complete before next session recall | Low | Low | Next session might not have the freshest extraction | Acceptable: personal-context is "eventually enriched." The DynamoDB memory path provides immediate-session continuity. Personal-context provides cross-session, synthesized continuity. Slight lag is fine. |
| R8 | **DynamoDB scan cost**: synthesis job scanning chat-context table | Low | Medium | Elevated DynamoDB read costs on large tables | Synthesis only scans sessions not already marked extracted. GSI on `personalContextExtracted` attribute (sparse index) keeps scan efficient. Cap at 20 sessions/user/run. |

### Severity Definitions
- **Critical**: Data breach, privacy violation, or permanent data corruption
- **High**: Feature malfunction affecting user trust or compliance
- **Medium**: Quality degradation or operational inefficiency
- **Low**: Minor cost or latency impact, easily mitigable

---

## 5. Implementation Issues to File

All issues sequenced AFTER:
- #1319 (identity fix) — implementation ships
- #1287 phases 1-4 deployed — personal-context store operational

### Issue A: Chat-experience-extract hook (session-end hydration)

**Priority**: P1 (the gap closure)
**Depends on**: #1319 deployed, personal-context store deployed
**Scope**:
- New file: `modules/agent-factory/agent/src/complex-task-chat/chat-experience-extract-hook.ts`
- New file: `modules/agent-factory/agent/src/complex-task-chat/chat-experience-extract-hook.test.ts`
- Modify: `complex-task-chat-agent.ts` — wire hook at session-close/idle-timeout
- Modify: `dynamo-store.ts` — add `personalContextExtracted` attribute on session header
- New env vars: `CHAT_CONTEXT_SAVE_ENABLED`, `CHAT_CONTEXT_MIN_TURNS`, `CHAT_CONTEXT_MAX_EXTRACTIONS`
- Extraction prompt (§2.3) and PII patterns (§2.5)

### Issue B: Synthesis pipeline chat-context ingestion (secondary path)

**Priority**: P2 (catch-up for missed sessions)
**Depends on**: Issue A deployed
**Scope**:
- Modify: `modules/agent-context/personal_context/synthesis.py` — add pre-stage that
  queries DynamoDB for unextracted sessions
- New: `modules/agent-context/personal_context/chat_ingestion.py` — DynamoDB query +
  extraction logic (reuses extraction prompt from Issue A)
- IAM: synthesis CronJob needs `dynamodb:Query` + `dynamodb:UpdateItem` on `chat-context` table
- Terraform: update IRSA policy for synthesis job pod

### Issue C: Enable personal-context recall for chat (flip the flag)

**Priority**: P3 (depends on store being populated)
**Depends on**: Issue A deployed + some sessions extracted
**Scope**:
- Set `PERSONAL_CONTEXT_RECALL_ENABLED=true` in chat agent deployment
- Validate composition in system prompt looks correct
- Add integration test: session with extraction → new session → recalled content appears
- Monitor: log recall hit rate, token budget usage, latency

### Issue D: User visibility + opt-out for chat-derived memories

**Priority**: P3 (trust/compliance)
**Depends on**: Issue A deployed
**Scope**:
- Gateway frontend: "Memory" section in user settings showing chat-derived entries
- API endpoint: `DELETE /api/personal-context/entries?source=chat` for bulk deletion
- API endpoint: `PUT /api/personal-context/settings` with `chat_memory_enabled: false`
- When disabled: extraction hook checks user preference before firing

### Issue E: Anti-self-reinforcement test coverage

**Priority**: P2 (safety validation)
**Depends on**: Issue A deployed
**Scope**:
- Integration test: inject recalled content → verify extraction does NOT re-persist it
- Test: user says something → extracted → recalled in next session → extraction of next
  session does NOT duplicate the original extraction
- Test: confidence scores don't escalate without new user-provided evidence

### Sequencing

```
#1319 (identity) ─┐
                   ├──→ Issue A (session-end hook) ──→ Issue B (synthesis ingestion)
#1287 deployed ────┘         │                              │
                             ├──→ Issue C (enable recall)   │
                             └──→ Issue D (user visibility) │
                                                            └──→ Issue E (anti-loop tests)
```

---

## 6. Appendix: Integration Points Summary

| Component | File | What changes |
|-----------|------|--------------|
| Chat agent | `modules/agent-factory/agent/src/complex-task-chat/complex-task-chat-agent.ts` | Wire extraction hook at session lifecycle boundary |
| Session store | `modules/agent-factory/agent/src/complex-task-chat/context/store/dynamo-store.ts` | New attribute `personalContextExtracted` on session header |
| New hook | `modules/agent-factory/agent/src/complex-task-chat/chat-experience-extract-hook.ts` | The extraction logic (mirrors experience-save-hook.ts pattern) |
| Synthesis pipeline | `modules/agent-context/personal_context/synthesis.py` | Pre-stage for DynamoDB chat ingestion |
| DynamoDB table | `modules/agent-factory/infra/chat-agent-infra.tf` | GSI on `personalContextExtracted` (sparse, for synthesis scan efficiency) |
| IAM | `modules/agent-context/terraform/` | Synthesis pod needs read/update on `chat-context` table |
| Context MCP Server | `modules/agent-context/personal_context/experience_tool.py` | No changes needed — existing `save` action works as-is |
| Personal-context models | `modules/agent-context/personal_context/models.py` | Add `"chat"` to Persona enum |

---

## 7. Open Questions for Stakeholder Review

1. **Turn threshold**: Is 4 substantive user turns the right minimum for extraction?
   Too low → noise; too high → miss valuable short conversations where a user
   states a clear preference.

2. **Persona name**: Should chat-derived entries use persona `"chat"` (clean
   separation) or inherit the agent persona that handled the session (e.g.,
   `"developer"` if the chat agent was in developer mode)? Recommendation: `"chat"`
   with a `context.agent_persona` field for downstream correlation.

3. **Shared vs. private**: Chat-derived entries are always `private` in this design.
   Should users be able to mark extracted learnings as `shared` (visible to team)?
   Recommendation: private-only initially; sharing is a future Issue D concern.

4. **Opt-in vs. opt-out**: The feature flag (`CHAT_CONTEXT_SAVE_ENABLED`) controls
   platform-wide enablement. Per-user opt-out (Issue D) should be designed before
   GA rollout. For internal/dev: flag is sufficient.
