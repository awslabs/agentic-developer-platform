# Design: GitHub Sender to Cognito Sub Resolution for Personal Context

**Issue:** #1319 (Sub of EPIC #1287)
**Status:** Design + Risk Register (no implementation)
**Author:** @agent-architect
**Date:** 2026-06-09

---

## 1. Problem Statement

The Personal Context feature (EPIC #1287) requires `cognito_sub` in the SQS envelope so that the agent worker can set `X-Owner-Sub` on MCP requests. The **chat/webchat path** correctly propagates this (the JWT `sub` claim IS the cognito_sub), but the **GitHub webhook path** sets `cognito_sub = resolved.user_id` (line 635 of `handler.py`), which is the **platform user_id (a UUID)**, not the Cognito sub.

This means: for GitHub-triggered runs, either Personal Context is silently disabled (fail-closed middleware rejects), or memories are keyed under a different identity than the same human's chat sessions (dual-store split).

The mapping data to resolve this already exists (100% coverage in `adp-dev-user-identity-index`). This is purely a code gap.

---

## 2. Recommended Resolution Design

### 2.1 Where: In the Webhook-Ingress Lambda (at sender-resolution time)

**Recommendation:** Extend the existing `POST /internal/v1/resolve-user` gateway endpoint to return `cognito_sub` alongside the current `{user_id, org_id, team_id, is_shadow}` response. The webhook handler already conditionally calls this endpoint (via `gateway_client.resolve_user_by_identity()`) when `RESOLVE_CANONICAL_VIA_GATEWAY` is enabled; the fix enriches that response.

**Why here and not elsewhere:**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **A. Extend `/internal/v1/resolve-user` response** (recommended) | Single-hop Postgres query; canonical source; already-plumbed call path; no new infra | Requires `RESOLVE_CANONICAL_VIA_GATEWAY=true` (feature flag); adds ~10ms to cold-path; gateway must be reachable from Lambda | **Best fit** |
| B. Add `cognito_sub` to DynamoDB user-identity-index | Zero-latency (same DDB read); no gateway dependency | DDB is a projection (not source-of-truth); requires backfill + dual-write change in gateway; staleness risk if Cognito sub changes | Rejected — projection should stay thin |
| C. New GSI on DDB `cognito` provider row (reverse lookup) | Lambda-native; no gateway call | Requires scan/GSI; `cognito` rows are keyed `PK=cognito, SK=<sub>` with `user_id` as attr — no GSI on `user_id` today; adding one is infra change for a single use case | Rejected — over-engineered |
| D. Dedicated identity-resolution microservice | Clean separation | Does not exist; significant new infra for one field | Rejected — premature |

### 2.2 The Lookup Mechanism (Two-Hop via Postgres)

**Step 1** (already exists): `identity_resolver.resolve(installation_id, sender_id)` resolves GitHub sender to `user_id` via DynamoDB.

**Step 2** (new): After Step 1 succeeds for a human sender, call the gateway's `/internal/v1/resolve-user` (already called when `RESOLVE_CANONICAL_VIA_GATEWAY=true`) which now returns `cognito_sub` from the `users` table.

**Concrete change:**

1. **Gateway `POST /internal/v1/resolve-user` response** — add `cognito_sub: str | None` to `ResolveUserResponse`:
   ```python
   class ResolveUserResponse(BaseModel):
       user_id: str
       org_id: str
       team_id: str
       is_shadow: bool
       cognito_sub: str | None = None  # NEW — null for shadow/unlinked users
   ```
   The handler already fetches the `User` row (`select(User).where(User.id == identity.user_id)`) — just return `user.cognito_sub`.

2. **`gateway_client.resolve_user_by_identity()` return** — include `cognito_sub` in the dict:
   ```python
   return {
       "user_id": data.get("user_id", ""),
       "org_id": data.get("org_id", ""),
       "team_id": data.get("team_id", ""),
       "is_shadow": data.get("is_shadow", False),
       "cognito_sub": data.get("cognito_sub"),  # NEW
   }
   ```

3. **`identity_resolver.py`** — when Postgres cross-validation is enabled and returns a result, extract `cognito_sub` from the gateway response and store it on `ResolvedIdentity`:
   ```python
   @dataclass
   class ResolvedIdentity:
       tenant_id: str
       org_id: str
       user_id: str
       user_provisioning_mode: str
       user_kind: str = "human"
       bot_kind: str = ""
       cognito_sub: str = ""  # NEW — populated from gateway response
   ```

4. **`handler.py` line 635** — use the resolved `cognito_sub` instead of `user_id`:
   ```python
   # Before (BROKEN):
   cognito_sub = resolved.user_id if resolved.user_kind == "human" else ""
   
   # After (FIXED):
   cognito_sub = resolved.cognito_sub if (resolved.user_kind == "human" and resolved.cognito_sub) else ""
   ```

### 2.3 Feature-Flag Strategy

The fix is gated behind `RESOLVE_CANONICAL_VIA_GATEWAY=true` (already exists as a feature flag in `identity_resolver.py:77`). This flag controls whether the Lambda calls the gateway for Postgres cross-validation. The implementation:

- **Flag ON (target state):** Gateway call happens; `cognito_sub` is populated from Postgres; Personal Context works for GitHub-triggered runs.
- **Flag OFF (current default):** No gateway call; `cognito_sub` remains empty; Personal Context degrades to no-op for GitHub runs (fail-closed, no regression from current behavior).
- **Rollout:** Enable flag in dev immediately; promote to prod after validation.

If the flag is already ON in production (which it should be for canonical user resolution), the fix is immediately effective once deployed.

### 2.4 Fallback When Gateway Is Unreachable

If the gateway call fails (timeout, 5xx, network error), `gateway_client.resolve_user_by_identity()` already returns `None`. In that case:
- `cognito_sub` remains empty string
- Personal Context degrades to no-op (fail-closed)
- The webhook dispatch **proceeds normally** — it is NOT blocked
- A CloudWatch metric `PersonalContextResolutionFailed` is emitted for alerting

This is the correct behavior: Personal Context is a non-critical enrichment; dispatch must never fail because of it.

---

## 3. Fail-Closed Contract

### 3.1 Core Invariant

> **When `cognito_sub` cannot be determined with certainty, it MUST be empty string. The Personal Context MCP server MUST reject requests with missing/empty `X-Owner-Sub` (return 403, no recall, no save). Under NO circumstance should data be written to or read from the wrong user's store.**

### 3.2 Enumerated Failure Modes

| Condition | `cognito_sub` value | Personal Context behavior |
|-----------|---------------------|--------------------------|
| Human sender, gateway resolves, `cognito_sub` present | The actual Cognito sub UUID | Full personal context (recall + save) |
| Human sender, gateway resolves, `cognito_sub` is NULL (shadow user) | `""` (empty) | No personal context (fail-closed) |
| Human sender, gateway call fails (timeout/error) | `""` (empty) | No personal context (fail-closed) |
| Human sender, gateway call disabled (flag OFF) | `""` (empty) | No personal context (fail-closed) |
| Bot/app sender (`user_kind == "bot"`) | `""` (empty) | No personal context (by design) |
| Unknown sender (identity resolution fails entirely) | N/A (dispatch rejected) | No dispatch, no context |

### 3.3 Structural Enforcement

The fail-closed guarantee is enforced at THREE layers (defense in depth):

1. **Webhook handler** (line 635): Only sets `cognito_sub` when BOTH `user_kind == "human"` AND `resolved.cognito_sub` is a non-empty string.
2. **Worker entrypoint** (`entrypoint.py:146-148`): Only sets `ADP_OWNER_SUB` env var if `cognito_sub` is truthy.
3. **Personal context header builder** (`personal-context-headers.ts:57-59`): Returns `null` if `ownerSub` is empty → MCP tools skip personal context calls entirely.

---

## 4. Bot / Service-Account / Chained-Run Handling

### 4.1 Bot and Service-Account Senders

**Decision:** Bots and service accounts get **NO personal context**. They have no Cognito identity and no human memory store.

- `identity_resolver.py` already classifies senders via `user_kind: "human" | "bot"` and `bot_kind` (e.g., `"agent-developer"`).
- The handler already gates on `user_kind == "human"` before setting `cognito_sub`.
- No change needed for this path.

**Guard against bot-misclassification:** The `user_kind` field in the DDB identity-index row is authoritative (set during user creation). A bot sender in DDB has `user_kind="bot"` and a matching `bot_kind` value. The risk of a bot being stored as `user_kind="human"` is mitigated by:
- Bot users are created programmatically via `modules/gateway/src/admin/identity/` with explicit `user_kind="bot"` and `bot_kind` values.
- The `user_kind` field has no mutation endpoint (cannot be changed after creation without direct DB access).
- A validation check in the identity resolver can log/alert if a sender with `is_bot: true` in the GitHub payload resolves to `user_kind="human"` in DDB (type mismatch → emit metric, treat as bot, fail-closed for personal context).

### 4.2 Chained Runs (Agent-Triggered-Agent)

**Context:** The correlation layer tracks `root_human_id` — the platform `user_id` of the human who originated the chain. When Agent-A (a bot) triggers Agent-B on behalf of Human-X:
- The webhook payload's `sender` is Agent-A (a bot).
- `root_human_id` in the correlation pointer is Human-X's `user_id`.

**Decision:** For chained runs, propagate the **root human's** `cognito_sub`:

1. When the sender is a bot AND `correlation.is_human_rooted == true` AND `correlation.root_human_id` is non-empty:
   - Perform the same two-hop lookup on `root_human_id` → `cognito_sub` via the gateway endpoint.
   - Set `cognito_sub` to the root human's Cognito sub.
   - This ensures the chained agent's Personal Context reads/writes go to the originating human's store.

2. When the sender is a bot AND the chain is NOT human-rooted (autonomous/scheduled):
   - `cognito_sub` remains empty.
   - No personal context.

**Justification:** The human who triggered the chain is the owner of the context. Their memories should be accessible to agents acting on their behalf, regardless of how many hops the chain has traversed. The correlation layer already has this provenance data.

**Implementation note:** The gateway endpoint needs a variant that accepts `user_id` directly (not `provider + provider_user_id`) for the chained-run case. Options:
- Add a query parameter `?by=user_id` to the existing endpoint, OR
- Add `POST /internal/v1/resolve-user-by-id` that takes `{user_id}` and returns `{cognito_sub}`.

The second option is cleaner (no overloading). The implementation should add this as a minimal new internal endpoint.

### 4.3 Summary Decision Table

| Sender type | Chain state | `cognito_sub` source | Personal Context? |
|-------------|------------|---------------------|-------------------|
| Human | N/A (direct trigger) | Gateway lookup on sender's `user_id` | Yes (if cognito_sub found) |
| Bot | Human-rooted chain | Gateway lookup on `root_human_id` | Yes (root human's context) |
| Bot | Not human-rooted | Empty | No |
| Unknown | N/A | N/A (dispatch rejected) | No |

---

## 5. Consistency Guarantee

### 5.1 One Owner Sub Per Human

> **Invariant:** A given human MUST have the SAME `owner_sub` (cognito_sub) regardless of whether the run was triggered via chat/webchat or GitHub webhook.

This is guaranteed by design because:
1. **Chat path:** `cognito_sub` = the JWT `sub` claim from Cognito (set at `$connect`).
2. **GitHub path (after fix):** `cognito_sub` = `users.cognito_sub` from Postgres (set during onboarding/linking).
3. **Both resolve to the same value** because `users.cognito_sub` IS the Cognito `sub` claim — it's written to the `users` row during the Cognito-backed onboarding flow (see `modules/gateway/src/admin/onboarding/handler.py`).
4. **Uniqueness enforced:** The partial unique index `uq_users_cognito_sub` (migration `012`) prevents two users from sharing a Cognito sub.

### 5.2 Preventing Dual-Store Split

The dual-store split (same human having chat memories under cognito_sub and GitHub memories under platform user_id) is the **current broken state**. The fix eliminates it by:
- Replacing `resolved.user_id` with `resolved.cognito_sub` in the envelope.
- Both paths now key on cognito_sub → one store.

**Detection:** Add a monitoring query that checks for `owner_sub` values in the personal-context store that are platform UUIDs (not Cognito subs). Since both are UUIDs, distinguish by:
- Cross-referencing against `users.id` (platform UUID) vs. `users.cognito_sub` (Cognito UUID).
- Any `owner_sub` that matches a `users.id` but NOT a `users.cognito_sub` is evidence of the old bug.
- This should be a periodic health check (daily CloudWatch metric or a CI assertion in #1295).

---

## 6. Risk Register

| # | Risk | Severity | Likelihood | Impact | Mitigation |
|---|------|----------|------------|--------|------------|
| R1 | **Cross-user mis-attribution** — Gateway returns wrong `cognito_sub` for a `user_id` (stale row, DB corruption, or race during user merge/delete) | **Critical** | Very Low | One user's private memories written/read under another user's identity. Data leak + integrity breach. | (a) `cognito_sub` is a UNIQUE indexed column — DB structurally prevents two users sharing one sub. (b) The lookup is `users.id → users.cognito_sub` on the SAME row — no sideways walk (enforced by `canonical_user.py` design principle). (c) Add assertion: `cognito_sub returned by gateway == cognito_sub for the user_id we sent` (idempotency check). (d) Personal Context MCP server logs every `owner_sub` it operates on — anomaly detection on unexpected subs. |
| R2 | **Spoofable sender** — Attacker forges webhook payload with a different `sender.id` to impersonate a user | **Critical** | Very Low | Attacker's agent run executes under victim's identity; reads/writes victim's personal context. | (a) GitHub webhook signature verification (HMAC-SHA256 via `verify_github_signature()` in `signature.py`) ensures payload integrity — if the shared secret is not compromised, the sender cannot be forged. (b) The webhook secret is stored in Secrets Manager with the Lambda's exclusive read access. (c) Signature check is the FIRST gate (lines 405-424) — unsigned/tampered payloads never reach identity resolution. (d) GitHub rotates webhook secrets on App regeneration. **Trust boundary is solid IF webhook secret is not leaked.** |
| R3 | **Unlinked / partial-coverage tenants** — A tenant has GitHub users who never completed Cognito linking (shadow users with `cognito_sub = NULL`) | **Medium** | Medium (new tenants, external contributors) | Those users get no Personal Context (fail-closed). Not a security issue, but a feature gap. Silent degradation. | (a) Fail-closed: `cognito_sub = NULL` → empty string → no personal context → no data leakage. (b) **Operational signal:** Add CloudWatch metric `PersonalContext.UnlinkedSender` emitted when a human sender resolves to `cognito_sub = NULL`. Alert threshold: >10% of human-triggered runs in a tenant have no cognito_sub → tenant admin notified. (c) Admin UI shows "identity coverage" per tenant (% of GitHub users with linked Cognito). (d) Document in tenant onboarding guide: "Users must complete Cognito sign-in at least once for Personal Context." |
| R4 | **Bot sender writing personal context** — A bot/app sender is misclassified as human (e.g., `user_kind` field corrupted or not set during bot provisioning) | **High** | Very Low | Bot's autonomous runs pollute a user's memory store with irrelevant or misleading entries. | (a) `user_kind` is set explicitly during user creation (bot provisioning path in `users_service.py`). (b) GitHub payload includes `sender.type == "Bot"` — add cross-check: if GitHub says Bot but DDB says human, emit `BotHumanMismatch` metric and treat as bot (fail-closed). (c) Bot user_ids are prefixed with recognizable patterns in practice (e.g., `agent-*[bot]` logins). (d) The correlation layer's `is_human_rooted` flag provides a second signal. |
| R5 | **Chained-run identity drift** — In a multi-hop chain (human → agent-A → agent-B), the root human's identity silently drops at a hop boundary | **Medium** | Low | Agent-B runs without Personal Context even though it should have access to the human's memories. Feature degradation, not a security breach. | (a) Use `correlation.root_human_id` (persisted in DDB correlation pointer) as the identity anchor for chained runs. (b) If root_human_id is present and chain is human-rooted, perform the same gateway lookup. (c) Add e2e assertion: "chained run envelope has same cognito_sub as direct run from same human." (d) Correlation pointer TTL is 7 days — chains older than that lose identity (acceptable; chains don't run that long). |
| R6 | **Latency / failure of gateway lookup in hot path** — Gateway is slow/down; webhook dispatch is blocked waiting for cognito_sub resolution | **Medium** | Low | Webhook processing latency increases; in worst case, Lambda timeout causes dispatch failure. | (a) Gateway call has 10s timeout (existing `urllib.request.urlopen(req, timeout=10)`). (b) On failure, `cognito_sub` is empty (fail-open for dispatch, fail-closed for personal context) — the run proceeds without personal context but IS dispatched. (c) Lambda timeout is 30s — even with 10s gateway call, there's headroom. (d) Gateway call is already made when `RESOLVE_CANONICAL_VIA_GATEWAY=true` — this adds zero new latency if the flag is already on (cognito_sub piggybacks on existing response). (e) Add circuit breaker: if gateway fails 3x in a row (within Lambda warm lifetime), skip the call for remaining invocations until Lambda recycles. |
| R7 | **PII / identity data exposure** — `cognito_sub` travels in SQS messages, env vars, and MCP headers; could be logged in cleartext | **Medium** | Low | Cognito sub is an opaque UUID — not email, not name, not PII. However, it's a stable identifier that correlates across systems. | (a) `cognito_sub` is an **opaque UUID** (e.g., `a1b2c3d4-e5f6-...`) — not PII by itself. (b) Confirm: no logging statements in the hot path log `cognito_sub` at INFO level. The `entrypoint.py` and `handler.py` should log at DEBUG only. (c) SQS messages are encrypted at rest (KMS). (d) `ADP_OWNER_SUB` env var is process-scoped to the agent worker pod — not visible outside the container. (e) `X-Owner-Sub` MCP header travels over the pod's localhost loopback (MCP server is a sidecar) — not over network. (f) **Action:** Audit logging config to ensure cognito_sub is never logged at INFO/WARN in production. |
| R8 | **Dual-store split (regression)** — Partial deployment or rollback leaves some runs keying on platform user_id and others on cognito_sub | **High** | Low (during rollout only) | Same human has two disjoint memory stores. Confusing, not a security breach, but violates the consistency guarantee. | (a) The feature flag (`RESOLVE_CANONICAL_VIA_GATEWAY`) provides atomic rollout — either all runs use the new path or none do. (b) **No backfill needed for existing data** — Personal Context was non-functional for GitHub runs before this fix (cognito_sub was the platform user_id, which the MCP server wouldn't have valid data for). (c) Add detection: periodic job checks `owner_sub` values in context store against `users.id` vs `users.cognito_sub` — any match against `users.id` is evidence of the old bug or a regression. (d) Rollback plan: set `RESOLVE_CANONICAL_VIA_GATEWAY=false` → reverts to pre-fix behavior (Personal Context disabled for GitHub runs). |
| R9 | **Webhook secret compromise** — If the GitHub App webhook secret is leaked, an attacker can forge arbitrary webhook payloads with any `sender.id` | **Critical** | Very Low | Full identity impersonation. Attacker can trigger runs as any user, read/write their personal context. | (a) Webhook secret is in Secrets Manager with Lambda-exclusive read IAM policy. (b) Secret is never logged, never in env vars in production (read at runtime from SM). (c) GitHub App regeneration rotates the secret. (d) **Detection:** Anomalous webhook patterns (unknown repos, impossible sender combinations) should trigger alerts. (e) **Blast radius:** Even with a compromised secret, the attacker can only impersonate users whose GitHub IDs they know — they cannot enumerate users. |
| R10 | **Race condition during identity linking** — User links Cognito identity WHILE a webhook is being processed; Lambda reads stale NULL cognito_sub | **Low** | Very Low | One run misses personal context (cognito_sub was NULL at read time, non-NULL moments later). Not a security issue — just a transient feature gap. | (a) Acceptable: next run will have the correct cognito_sub. (b) No retry logic needed — the linking is a one-time event per user. (c) The Lambda caches nothing (each invocation reads fresh from gateway). |

---

## 7. Implementation Issues to File

### Issue A: Implement `cognito_sub` Resolution in Webhook Path

**Scope:** Developer implementation issue.

**Changes:**
1. `modules/gateway/src/internal/routes.py` — Add `cognito_sub` to `ResolveUserResponse` and return `user.cognito_sub` in the response body.
2. `modules/agent-factory/webhook-ingress/lambda/common/gateway_client.py` — Include `cognito_sub` in the return dict from `resolve_user_by_identity()`.
3. `modules/agent-factory/webhook-ingress/lambda/common/identity_resolver.py` — Add `cognito_sub: str = ""` to `ResolvedIdentity` dataclass; populate it from the gateway response when available.
4. `modules/agent-factory/webhook-ingress/lambda/github/handler.py` (line 635) — Use `resolved.cognito_sub` instead of `resolved.user_id`.
5. Add `POST /internal/v1/resolve-user-by-id` endpoint for chained-run lookup (takes `user_id`, returns `cognito_sub`).
6. In handler.py, for bot senders with human-rooted chains, resolve `cognito_sub` from `correlation.root_human_id` via the new endpoint.
7. Add bot/human type mismatch detection (GitHub `sender.type` vs DDB `user_kind`).

**Tests:**
- Unit: identity_resolver returns cognito_sub when gateway provides it.
- Unit: handler uses resolved.cognito_sub (not user_id) in envelope.
- Unit: bot sender → empty cognito_sub.
- Unit: human-rooted chain → root human's cognito_sub.
- Unit: gateway failure → empty cognito_sub (dispatch not blocked).
- Integration: full webhook → SQS envelope has correct cognito_sub.

**Deployment:**
- Gateway deploy (response shape change) MUST deploy BEFORE webhook-ingress (consumer of new field).
- Feature flag `RESOLVE_CANONICAL_VIA_GATEWAY` must be `true` for the fix to be active.
- No DB migration needed (reads existing `users.cognito_sub` column).

### Issue B: Propagation-Completeness E2E Assertion (#1295 addition)

**Scope:** Add to the existing Personal Context e2e test suite (#1295).

**Assertion:**
```
Given: A human user with linked Cognito identity triggers a run via GitHub webhook
When: The webhook is processed and envelope published to SQS
Then: envelope.cognito_sub == users.cognito_sub for that user (NOT users.id)
AND: The same human triggering via webchat produces the same cognito_sub
```

**Additional assertions:**
- Bot sender → `cognito_sub` is empty in envelope.
- Shadow user (no Cognito link) → `cognito_sub` is empty.
- Chained run from human → `cognito_sub` matches root human.
- Gateway down → dispatch succeeds, `cognito_sub` empty.

### Issue C: Observability — Personal Context Resolution Metrics

**Scope:** Add CloudWatch metrics for monitoring coverage and failures.

**Metrics to add (namespace `ADP/PersonalContext`):**
- `ResolutionSuccess` — cognito_sub successfully resolved for human sender.
- `ResolutionFailedUnlinked` — human sender has NULL cognito_sub (shadow user).
- `ResolutionFailedGatewayError` — gateway call failed.
- `BotHumanTypeMismatch` — GitHub says Bot, DDB says human (or vice versa).
- `ChainedRunIdentityPropagated` — root_human_id successfully resolved for chained run.

---

## 8. Sequence Diagram (Post-Fix)

```
GitHub Webhook → Lambda
  │
  ├─ 1. Verify HMAC signature (reject if invalid)
  │
  ├─ 2. Extract sender.id from payload
  │
  ├─ 3. identity_resolver.resolve(installation_id, sender_id)
  │     └─ DDB: github / <sender_id> → {user_id, org_id, user_kind}
  │
  ├─ 4. [If RESOLVE_CANONICAL_VIA_GATEWAY=true]
  │     └─ gateway_client.resolve_user_by_identity("github", sender_id)
  │         └─ POST /internal/v1/resolve-user → {user_id, org_id, team_id, is_shadow, cognito_sub}
  │
  ├─ 5. Determine cognito_sub:
  │     ├─ Human + cognito_sub present → use it
  │     ├─ Human + cognito_sub NULL → empty (fail-closed)
  │     ├─ Bot + human-rooted chain → resolve root_human_id → cognito_sub
  │     └─ Bot + no chain → empty
  │
  ├─ 6. Build envelope with cognito_sub
  │
  └─ 7. Publish to SQS
        │
        └─ Worker reads envelope
              ├─ ADP_OWNER_SUB = cognito_sub (if truthy)
              └─ X-Owner-Sub header on MCP requests
                    └─ Personal Context MCP Server: validates, scopes to owner_sub
```

---

## 9. Decision Log

| Decision | Rationale | Alternatives rejected |
|----------|-----------|----------------------|
| Resolve via gateway Postgres (not DDB) | Postgres is the canonical source for `cognito_sub`; DDB is a projection that doesn't carry this field | DDB GSI (over-engineered), DDB attribute addition (staleness risk) |
| Fail-closed (empty string, not error) | Personal Context is a non-critical enrichment; dispatch must never fail because of it | Fail-hard (blocks dispatch), fail-open with default sub (security risk) |
| Chained runs use root_human_id | The human who initiated the chain owns the context; agent intermediaries have no personal store | Each hop uses its own sender (breaks context continuity), no personal context for chains (loses value) |
| Gate behind existing feature flag | Atomic rollout; easy rollback; no new flag proliferation | New flag (unnecessary), always-on (risky without testing) |
| Extend existing endpoint (not new) | `/internal/v1/resolve-user` already does the User table lookup; adding one field is minimal change | New endpoint (unnecessary duplication), direct DB access from Lambda (coupling) |

---

## 10. Open Questions (for maintainer review)

1. **Should `RESOLVE_CANONICAL_VIA_GATEWAY` become always-on?** Currently it's a feature flag. If the gateway call is now required for correct Personal Context, should we remove the flag and make it mandatory? (Trade-off: removes the ability to run webhook-ingress without gateway connectivity.)

2. **Chained-run depth limit:** Should there be a maximum chain depth beyond which `root_human_id` is no longer trusted? (Current: no limit; correlation pointer TTL of 7 days is the only bound.)

3. **Backfill existing context-store entries:** If any runs have already written under platform `user_id` (unlikely given fail-closed, but worth confirming), should we run a one-time migration to re-key them under `cognito_sub`?
