# Design: User-Facing "My Context" UI + Backend API

**Issue:** #1327 (sub of EPIC #1287)
**Status:** Design complete
**Author:** @agent-architect
**Date:** 2026-06-09

## Summary

Users of the ADP platform currently have **no visibility** into the personal context that agents have accumulated about them. The personal-context store (EPIC #1287) is entirely agent/MCP-side: written by post-task hooks, recalled by pre-task hooks, synthesized by a nightly CronJob. A user cannot see, search, correct, or delete what the system has learned about them.

This design adds:
1. A **Cognito-JWT-authenticated gateway API** (`/api/personal-context/...`) for list/search/get/edit/delete/visibility-toggle/export operations.
2. A **"My Context" frontend page** in the gateway React dashboard.
3. **Lifecycle propagation rules** for edit/delete operations across embeddings, Neptune graph, and synthesis entries.

**Dependencies:** #1319 (identity — same `cognito_sub` key), #1283 (store design), #1325 (chat hydration increases urgency).

---

## 1. Backend API Contract

### 1.1 Design Principles

- **Owner derived from JWT, never from a parameter.** `TokenContext.user_id` = Cognito `sub` = `owner_sub` in the personal-context store. `TokenContext.org_id` = `tenant_id`. No URL parameter or request body field can override these.
- **Fail-closed.** If `user_id` or `org_id` cannot be extracted from the token, return 403. If the store is unreachable, return 503 (never return partial/wrong data).
- **Same isolation invariant as #1288/#1319.** A user sees: (a) their own private entries, (b) shared entries within their tenant. Cross-tenant data never leaks.
- **Gateway-side proxy to OpenViking.** The gateway makes internal HTTP calls to the agent-context namespace's OpenViking API (`openviking.agent-context.svc.cluster.local:1933`) using a thin client. No long-lived secrets needed — connectivity is via cluster-internal DNS + Kubernetes NetworkPolicy allowlisting the gateway pod's service account.

### 1.2 Base Path

```
/api/personal-context
```

Mounted as a new FastAPI router in `src/personal_context/routes.py`, registered in `app.py`'s `UNIT_MODULES` list.

### 1.3 Authentication

All endpoints require:
```python
current_user: TokenContext = Depends(get_current_user)
```

Identity mapping:
```python
owner_sub = current_user.user_id   # == Cognito sub (UUID)
tenant_id = current_user.org_id    # == org_id
```

If `current_user.user_id` is empty or not a valid UUID → HTTP 403 (fail-closed).
If `current_user.org_id` is empty → HTTP 403 (fail-closed, same as vault pattern in `vault_routes.py:96`).

### 1.4 Endpoints

#### `GET /api/personal-context/entries`

**Purpose:** List/search the caller's personal-context entries.

**Query Parameters:**
| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `q` | string | no | — | Semantic search query (triggers embedding-based ranking) |
| `type` | enum | no | — | Filter by EntryType: `learning`, `synthesis`, `pattern` |
| `persona` | enum | no | — | Filter by Persona: `developer`, `architect`, `operations`, `reviewer` |
| `visibility` | enum | no | — | Filter: `private`, `shared` |
| `min_confidence` | float | no | 0.0 | Minimum confidence threshold |
| `sort` | enum | no | `created_at` | Sort by: `created_at`, `confidence`, `decay_score`, `last_accessed_at` |
| `order` | enum | no | `desc` | Sort order: `asc`, `desc` |
| `page` | int | no | 1 | Page number (1-indexed) |
| `limit` | int | no | 20 | Items per page (max 100) |

**Response:** `200 OK`
```json
{
  "entries": [
    {
      "id": "01HXYZ...",
      "type": "learning",
      "visibility": "private",
      "persona": "developer",
      "learning_type": "task_learning",
      "content": "User prefers Terraform over CloudFormation for IaC",
      "context": {
        "source_issue": "#456",
        "source_type": "task_completion"
      },
      "confidence": 0.85,
      "validated": false,
      "superseded_by": null,
      "created_at": "2026-05-10T14:30:00Z",
      "last_accessed_at": "2026-06-01T09:15:00Z",
      "decay_score": 0.9,
      "has_contradictions": false
    }
  ],
  "total": 42,
  "page": 1,
  "limit": 20,
  "has_more": true
}
```

**Authz:** Reads from `/personal/<owner_sub>/` (private) + `/shared/<tenant_id>/` (shared). Never crosses tenant boundary.

**Semantic search mode:** When `q` is provided, the gateway calls the agent-context LiteLLM proxy to embed the query, then ranks results by `cosine_similarity * decay_score`. This is the same ranking the experience tool uses for `recall`.

---

#### `GET /api/personal-context/entries/{entry_id}`

**Purpose:** Get a single entry by ID.

**Response:** `200 OK` — single entry object (same shape as list items, with additional fields):
```json
{
  "id": "01HXYZ...",
  "type": "learning",
  "visibility": "private",
  "persona": "developer",
  "learning_type": "task_learning",
  "content": "User prefers Terraform over CloudFormation for IaC",
  "context": {
    "source_issue": "#456",
    "source_type": "task_completion",
    "related_entries": ["01HABC...", "01HDEF..."]
  },
  "confidence": 0.85,
  "validated": false,
  "superseded_by": null,
  "created_at": "2026-05-10T14:30:00Z",
  "last_accessed_at": "2026-06-01T09:15:00Z",
  "decay_score": 0.9,
  "contradictions": ["01HGHI..."],
  "derived_syntheses": ["01HJKL..."],
  "graph_edges": [
    {"target_id": "01HABC...", "edge_type": "supports"},
    {"target_id": "01HGHI...", "edge_type": "contradicts"}
  ]
}
```

**Authz:** Same read filter — entry must be owned by caller OR be shared within caller's tenant.

**404:** Entry not found OR caller lacks access (same response to prevent enumeration).

---

#### `PATCH /api/personal-context/entries/{entry_id}`

**Purpose:** Edit/correct an entry's content or metadata.

**Request Body:**
```json
{
  "content": "User prefers Terraform (specifically OpenTofu) over CloudFormation",
  "confidence": 0.95,
  "learning_type": "validated_preference"
}
```

**Editable fields:** `content`, `confidence`, `learning_type`, `context` (merge, not replace).

**Non-editable fields:** `id`, `type`, `owner_sub`, `tenant_id`, `created_at` (immutable identity/provenance).

**Side effects on edit:**
1. Set `validated = true` (user-corrected = user-validated).
2. Re-generate embedding for new content (call LiteLLM proxy).
3. Update `last_accessed_at`.
4. If Neptune graph enabled: update vertex properties.
5. Mark any syntheses that `derived_from` this entry as `stale` (trigger re-synthesis on next cycle).

**Response:** `200 OK` — updated entry object.

**Authz:** Only the owner can edit. `entry.owner_sub != current_user.user_id` → 404.

---

#### `DELETE /api/personal-context/entries/{entry_id}`

**Purpose:** Permanently delete an entry.

**Response:** `204 No Content`

**Side effects on delete:**
1. Remove AGFS file at the entry's path.
2. Remove embedding from the index.
3. If Neptune graph enabled: remove vertex + all connected edges.
4. If the entry is referenced by syntheses (`derived_from`): mark those syntheses as `stale` in their context metadata (do NOT auto-delete syntheses — they may reference other learnings too).
5. If the entry is `type=synthesis`: also remove all `derived_from` edges pointing to it (but NOT the source learnings).

**Authz:** Only the owner can delete. Shared entries: the owner who shared it can delete it (removes it from shared view for the whole tenant). Non-owners cannot delete shared entries they can read.

---

#### `PATCH /api/personal-context/entries/{entry_id}/visibility`

**Purpose:** Toggle an entry's visibility between private and shared.

**Request Body:**
```json
{
  "visibility": "shared"
}
```

**Side effects:**
1. Move the AGFS file from `/personal/<owner_sub>/...` to `/shared/<tenant_id>/...` (or vice versa). This is a delete + re-create at the new path (atomic from the caller's perspective; the API handles the two-step internally).
2. Update Neptune vertex `visibility` property if graph enabled.
3. Embedding stays the same (content hasn't changed).

**Response:** `200 OK` — updated entry object with new visibility.

**Authz:** Only the owner can toggle. Moving to `shared` makes it visible to the whole tenant. Moving to `private` removes it from tenant-shared view.

---

#### `POST /api/personal-context/export`

**Purpose:** Export all of the caller's personal-context data (data portability).

**Request Body (optional):**
```json
{
  "format": "json",
  "include_shared": true
}
```

**Response:** `200 OK` with `Content-Disposition: attachment; filename="my-context-export-2026-06-09.json"`

Returns a JSON array of all entries owned by the caller. If `include_shared: false`, only private entries. Format is the same entry schema as the list endpoint.

**Rate limit:** Max 1 export per hour per user (prevents abuse).

**Authz:** Owner only. Never exports entries owned by others (even if readable via shared visibility).

---

#### `GET /api/personal-context/syntheses`

**Purpose:** List synthesis entries (the "dream cycle" output) separately from raw learnings.

**Query Parameters:** Same as `/entries` but `type` is pre-filtered to `synthesis`.

**Response:** Same structure as `/entries`, pre-filtered.

**Design note:** This is a convenience alias. The frontend uses it to render the "What the system has concluded about me" view separately from raw learnings.

---

### 1.5 Gateway → Agent-Context Internal Communication

The gateway has **no existing connectivity** to the agent-context namespace. This design introduces a thin internal HTTP client:

**Module:** `src/personal_context/agfs_client.py`

**Pattern:** Same as `src/admin/connections/github_client.py` (uses `httpx.AsyncClient`).

**Target:** `http://openviking.agent-context.svc.cluster.local:1933`

**NetworkPolicy:** A new K8s NetworkPolicy in `modules/agent-context/manifests/` must allow ingress from the `adp-gateway` namespace service account to OpenViking on port 1933.

**Embedding calls:** For semantic search, the gateway also needs to call the LiteLLM proxy at `http://litellm-proxy.agent-context.svc.cluster.local:4000/embeddings`. Same NetworkPolicy addition.

**Authentication:** Cluster-internal, no bearer token needed. The NetworkPolicy + namespace isolation is the trust boundary. This matches the existing pattern where the agent-worker pods call OpenViking directly without tokens.

**Failure mode:** If OpenViking or LiteLLM proxy is unreachable, return HTTP 503 with `{"error": "context_service_unavailable", "message": "Personal context service is temporarily unavailable"}`. Never return partial data.

### 1.6 Configuration

New environment variables for the gateway:
```
PERSONAL_CONTEXT_ENABLED=false          # Feature flag (disabled until agent-context is deployed)
OPENVIKING_URL=http://openviking.agent-context.svc.cluster.local:1933
LITELLM_PROXY_URL=http://litellm-proxy.agent-context.svc.cluster.local:4000
PERSONAL_CONTEXT_EXPORT_RATE_LIMIT=1    # Exports per hour per user
```

---

## 2. Frontend "My Context" Page

### 2.1 Navigation Placement

In `Navigation.tsx`, add after the "My Chats" entry (line 80):
```typescript
// My Context page for all authenticated users (Issue #1327)
navItems.push({ to: '/my-context', label: 'My Context', icon: '🧠' });
```

Route in `App.tsx`:
```typescript
const MyContext = lazy(() => import('./pages/MyContext')); // Issue #1327
// In protected routes:
<Route path="/my-context" element={<MyContext />} />
```

### 2.2 Page Layout

The page has **two tabs** at the top:

1. **Learnings** (default) — raw learnings + patterns
2. **Insights** — synthesis entries (what the system has concluded)

### 2.3 Learnings Tab

```
┌─────────────────────────────────────────────────────┐
│  🧠 My Context                                       │
├─────────────────────────────────────────────────────┤
│  [Learnings]  [Insights]                [Export ↓]   │
├─────────────────────────────────────────────────────┤
│  🔍 [Search what I know about you...              ]  │
│                                                      │
│  Filters: [Persona ▾] [Visibility ▾] [Type ▾]       │
│           [Min confidence: ___]  [Sort: Recent ▾]    │
├─────────────────────────────────────────────────────┤
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │ 🔒 Private · developer · 0.85 confidence        │ │
│  │ "User prefers Terraform over CloudFormation"     │ │
│  │ Source: Issue #456 · 2026-05-10 · decay: 0.9    │ │
│  │ [Edit ✏️] [Delete 🗑️] [Share →]                  │ │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │ 🌐 Shared · architect · 0.72 confidence         │ │
│  │ "Team uses Neptune for graph queries, not..."     │ │
│  │ Source: PR #789 · 2026-04-22 · decay: 0.8       │ │
│  │ ⚠️ Contradicts: "Team uses DynamoDB for..."       │ │
│  │ [Edit ✏️] [Delete 🗑️] [Make Private 🔒]           │ │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  ─ Page 1 of 3 ─  [← Prev] [Next →]                │
└─────────────────────────────────────────────────────┘
```

### 2.4 Insights Tab

```
┌─────────────────────────────────────────────────────┐
│  [Learnings]  [Insights]                             │
├─────────────────────────────────────────────────────┤
│                                                      │
│  These are patterns the system has identified from   │
│  your interactions. They guide how agents respond    │
│  to you.                                             │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │ 💡 Synthesis · developer · 0.91 confidence       │ │
│  │ "You prefer concise explanations with code       │ │
│  │  examples over verbose documentation-style       │ │
│  │  responses. You typically work with Python +     │ │
│  │  Terraform and prefer incremental changes."      │ │
│  │ Derived from: 5 learnings · Last updated: 3d    │ │
│  │ [View sources] [Delete 🗑️]                       │ │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │ 💡 Synthesis · operations · 0.78 confidence      │ │
│  │ "You manage a dev + staging + prod pipeline     │ │
│  │  on EKS, with a preference for blue-green       │ │
│  │  deploys over rolling updates."                  │ │
│  │ Derived from: 3 learnings · Last updated: 7d    │ │
│  │ [View sources] [Delete 🗑️]                       │ │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  ℹ️ Insights are generated nightly from your         │
│     learnings. Deleting a source learning marks      │
│     related insights for refresh.                    │
└─────────────────────────────────────────────────────┘
```

### 2.5 Entry Edit Modal

When "Edit" is clicked, a modal appears (same pattern as `ChatDetailModal` in `MyChats.tsx`):

```
┌─────────────────────────────────────────────────────┐
│  Edit Learning                              [Close]  │
├─────────────────────────────────────────────────────┤
│                                                      │
│  Content:                                            │
│  ┌─────────────────────────────────────────────────┐ │
│  │ User prefers Terraform (specifically OpenTofu)  │ │
│  │ over CloudFormation for infrastructure-as-code  │ │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  Confidence: [0.95 ━━━━━━━━━━━━━━━○━━]              │
│                                                      │
│  Type: [task_learning ▾]                             │
│                                                      │
│  ℹ️ Editing marks this as user-validated (higher     │
│     confidence in future recall).                    │
│                                                      │
│            [Cancel]  [Save Changes ✓]                │
└─────────────────────────────────────────────────────┘
```

### 2.6 Delete Confirmation

```
┌─────────────────────────────────────────────────────┐
│  Delete Learning?                                    │
├─────────────────────────────────────────────────────┤
│                                                      │
│  "User prefers Terraform over CloudFormation"        │
│                                                      │
│  This permanently removes this learning. Agents      │
│  will no longer recall it. Related insights will     │
│  be refreshed on the next nightly cycle.             │
│                                                      │
│  ⚠️ This entry is currently shared with your org.    │
│     Deleting it removes it for everyone.             │
│                                                      │
│            [Cancel]  [Delete permanently]             │
└─────────────────────────────────────────────────────┘
```

### 2.7 Empty State

```
┌─────────────────────────────────────────────────────┐
│  🧠 My Context                                       │
├─────────────────────────────────────────────────────┤
│                                                      │
│         🕳️ No learnings yet                          │
│                                                      │
│    As you interact with agents, they'll build up     │
│    context about your preferences, patterns, and     │
│    working style. You'll be able to review and       │
│    manage everything here.                           │
│                                                      │
│    Learnings are created when:                       │
│    • An agent completes a task for you               │
│    • You have conversations that surface insights    │
│    • The nightly synthesis identifies patterns       │
│                                                      │
└─────────────────────────────────────────────────────┘
```

### 2.8 Error State

```
┌─────────────────────────────────────────────────────┐
│  ⚠️ Unable to load your context                      │
│                                                      │
│  The personal context service is temporarily         │
│  unavailable. This does not affect your agent        │
│  interactions — try refreshing in a few minutes.     │
│                                                      │
│  [Retry]                                             │
└─────────────────────────────────────────────────────┘
```

### 2.9 Components to Create

| Component | File | Purpose |
|-----------|------|---------|
| `MyContext` | `pages/MyContext.tsx` | Page component with tab layout |
| `ContextEntryCard` | `components/context/ContextEntryCard.tsx` | Single entry display card |
| `ContextEditModal` | `components/context/ContextEditModal.tsx` | Edit modal |
| `ContextDeleteDialog` | `components/context/ContextDeleteDialog.tsx` | Delete confirmation |
| `ContextSearchBar` | `components/context/ContextSearchBar.tsx` | Semantic search input |
| `ContextFilters` | `components/context/ContextFilters.tsx` | Filter controls |

### 2.10 Service Layer

New file: `services/personalContext.ts`

```typescript
// API client for personal context endpoints
export async function getMyEntries(params: ContextListParams): Promise<ContextListResponse>;
export async function getEntry(entryId: string): Promise<ContextEntry>;
export async function updateEntry(entryId: string, data: ContextUpdateRequest): Promise<ContextEntry>;
export async function deleteEntry(entryId: string): Promise<void>;
export async function toggleVisibility(entryId: string, visibility: 'private' | 'shared'): Promise<ContextEntry>;
export async function exportMyContext(options: ExportOptions): Promise<Blob>;
export async function getMySyntheses(params: ContextListParams): Promise<ContextListResponse>;
```

Uses the same `apiClient` pattern as `services/chats.ts` (axios instance with auth interceptor from `AuthContext`).

---

## 3. Lifecycle & Consistency Design

### 3.1 Edit Propagation

When a user edits an entry's `content`:

```
1. Update AGFS file (OpenViking PUT)
2. Re-embed new content via LiteLLM proxy
3. Replace old embedding in index with new one
4. If GRAPH_ENABLED:
   a. Update vertex properties (content hash, confidence, validated)
5. Find syntheses that list this entry.id in context.source_ids:
   a. Set context.stale = true on those syntheses
   b. Next synthesis CronJob run will re-synthesize using updated content
6. Return updated entry to caller
```

**Atomicity:** Steps 1-3 are the critical path. Steps 4-5 are best-effort (logged failures, don't block the response). The entry is "correct" in storage after step 1; steps 2-5 ensure recall/graph consistency.

### 3.2 Delete Propagation

When a user deletes an entry:

```
1. Verify ownership (entry.owner_sub == caller.user_id)
2. Delete AGFS file (OpenViking DELETE)
3. Remove embedding from index (if persistent embeddings exist)
4. If GRAPH_ENABLED:
   a. Find all edges connected to this entry's vertex
   b. Delete all edges
   c. Delete the vertex
5. Find syntheses that reference this entry.id in context.source_ids:
   a. Remove entry.id from their context.source_ids list
   b. Set context.stale = true
   c. If context.source_ids is now empty, delete the synthesis too
6. Return 204
```

**Hard delete:** Entries are truly removed — no soft-delete/tombstone. The AGFS file is gone, the embedding is removed, the graph vertex is removed. This is the transparency/compliance guarantee: "delete means delete."

**Audit trail:** Before deletion, log `{action: "user_delete", entry_id, owner_sub, timestamp}` to the gateway audit log (same pattern as vault credential deletion in `credential_routes.py`).

### 3.3 Visibility Toggle Propagation

When a user moves an entry private→shared or shared→private:

```
1. Compute old path and new path:
   - private→shared: /personal/<sub>/learnings/<id>.json → /shared/<tenant_id>/learnings/<id>.json
   - shared→private: reverse
2. Read entry from old path
3. Update entry.visibility field
4. Write entry to new path (OpenViking PUT)
5. Delete old path (OpenViking DELETE)
6. If GRAPH_ENABLED: update vertex.visibility property
7. Embedding unchanged (content didn't change)
8. Return updated entry
```

**Race condition:** If the entry is being recalled by an agent concurrently, the agent may get a 404 during the move. This is acceptable — the recall is best-effort and the agent will retry on next task.

### 3.4 Shared Entry Delete Semantics

**Rule:** Only the `owner_sub` of a shared entry can delete it.

**Impact:** When a shared entry is deleted, other users in the tenant who could previously read it will no longer see it. There is no "undelete" or notification. This is by design — the owner owns the data.

**Future consideration:** If shared entries become critical infrastructure (e.g., team coding standards that agents rely on), an admin-level "pin" or "protect" mechanism may be needed. That's out of scope for v1.

### 3.5 Synthesis Staleness

When a source learning is edited or deleted:
- The synthesis entry gains `context.stale = true`.
- The nightly synthesis CronJob already re-processes all entries; stale syntheses are regenerated.
- The UI displays stale syntheses with a visual indicator ("Needs refresh — based on updated learnings").
- Users cannot edit syntheses directly (they're auto-generated); they can only delete them.

---

## 4. Risk Register

| # | Risk | Severity | Likelihood | Impact | Mitigation |
|---|------|----------|------------|--------|------------|
| R1 | **Cross-user exposure via API** — authz bug in the new gateway endpoint leaks one user's private context to another | 🔴 Critical | Low | Full privacy breach; user's accumulated personal knowledge exposed to unauthorized party | Owner derived from `TokenContext.user_id` (JWT `sub` claim), NEVER from a URL/body parameter. Read filter identical to `PersonalContextStore._caller_can_read`. Mandatory isolation test: create entries for user A, request as user B, assert 0 results. Test in CI on every PR. |
| R2 | **Cross-tenant leakage of shared entries** — shared entries from tenant A visible to tenant B | 🔴 Critical | Low | Org-confidential knowledge leaked to competing/unrelated org | All shared-path reads filter on `tenant_id == current_user.org_id`. Tenant boundary is already enforced by AGFS path structure (`/shared/<tenant_id>/`). Isolation test: create shared entry in tenant A, request as user in tenant B, assert 0 results. |
| R3 | **Privacy expectation shock** — user sees "the system knows X about me" (especially chat-derived learnings from #1325) and finds it invasive | 🟠 High | Medium | User trust erosion, potential support escalations, possible compliance complaints | Clear UI copy explaining what learnings are and how they're used. Easy one-click delete. "Source" attribution so users understand where each learning came from. Empty-state copy that sets expectations before learnings exist. Consider: per-user opt-out flag for learning accumulation. |
| R4 | **Synthesis/graph inconsistency on edit/delete** — user edits a learning but the synthesis still reflects old content; or deletes a learning but graph edges remain orphaned | 🟠 High | Medium | Agents recall stale/wrong information; user sees contradictions between "learnings" and "insights" tabs | Mark syntheses as stale on source edit/delete (trigger re-synthesis). Delete graph vertex + edges on entry delete. Log propagation failures (don't block the response, but alert on repeated failures). |
| R5 | **Shared entry disappears for others** — owner deletes a shared entry that other team members' agents rely on | 🟡 Medium | Medium | Other users' agent quality degrades silently (their recall no longer finds the deleted entry) | v1: accept this. The owner owns the data. Delete confirmation modal warns "this is shared with your org." Future: admin "pin" mechanism for critical shared entries. |
| R6 | **OpenViking/LiteLLM unavailability** — gateway calls to agent-context namespace fail (pod restart, namespace not deployed) | 🟡 Medium | Medium | "My Context" page shows error state; no data loss (store is durable) | Feature-flagged (`PERSONAL_CONTEXT_ENABLED`). Graceful 503 with retry guidance. Circuit breaker on the httpx client (3 failures → open for 30s). Independent of core gateway functionality. |
| R7 | **Semantic search abuse** — attacker sends expensive embedding requests via the search endpoint | 🟡 Medium | Low | LiteLLM proxy overloaded; Bedrock Titan Embed costs spike | Rate limit on the search endpoint (10 searches/minute per user). Query length cap (500 chars). Standard gateway rate-limit middleware applies. |
| R8 | **Export endpoint abuse** — automated export requests to exfiltrate data at scale | 🟡 Medium | Low | API cost; potential data scraping if account compromised | 1 export/hour rate limit. Audit log on every export. Standard auth required (compromised JWT is the prerequisite; that's a broader problem). |

---

## 5. Implementation Issue Breakdown

### Sequencing

```
#1319 (Identity — cognito_sub resolution)
  ↓ (must ship first: the API keys on cognito_sub)
#1327-A (Backend API — gateway router + AGFS client)
  ↓ (API must exist before frontend can call it)
#1327-B (Frontend — My Context page)
  ↓ (can develop in parallel with mocked API)
#1327-C (Isolation tests — cross-user + cross-tenant)
  ↓ (ship alongside or immediately after A)
#1327-D (NetworkPolicy + feature flag wiring)
  ↓ (infra prerequisite for A to reach agent-context)
```

### Issue Definitions

#### Issue A: Backend API — Personal Context Gateway Router

**Scope:** New FastAPI router at `/api/personal-context/` with 7 endpoints (list, get, edit, delete, visibility toggle, export, syntheses). AGFS client module. LiteLLM embedding client for semantic search.

**Files to create:**
- `modules/gateway/src/personal_context/__init__.py`
- `modules/gateway/src/personal_context/routes.py`
- `modules/gateway/src/personal_context/schemas.py`
- `modules/gateway/src/personal_context/service.py`
- `modules/gateway/src/personal_context/agfs_client.py`
- `modules/gateway/src/personal_context/embedding_client.py`
- `modules/gateway/tests/personal_context/test_routes.py`
- `modules/gateway/tests/personal_context/test_service.py`

**Files to modify:**
- `modules/gateway/src/app.py` — add `"src.personal_context.routes"` to `UNIT_MODULES`
- `modules/gateway/src/shared/config.py` — add PERSONAL_CONTEXT_* settings

**Depends on:** #1319 (identity), Issue D (NetworkPolicy)

---

#### Issue B: Frontend — My Context Page

**Scope:** React page with learnings/insights tabs, search, filters, edit/delete modals. Service layer for API calls.

**Files to create:**
- `modules/gateway/frontend/src/pages/MyContext.tsx`
- `modules/gateway/frontend/src/components/context/ContextEntryCard.tsx`
- `modules/gateway/frontend/src/components/context/ContextEditModal.tsx`
- `modules/gateway/frontend/src/components/context/ContextDeleteDialog.tsx`
- `modules/gateway/frontend/src/components/context/ContextSearchBar.tsx`
- `modules/gateway/frontend/src/components/context/ContextFilters.tsx`
- `modules/gateway/frontend/src/services/personalContext.ts`
- `modules/gateway/frontend/src/types/personalContext.ts`

**Files to modify:**
- `modules/gateway/frontend/src/App.tsx` — add route
- `modules/gateway/frontend/src/components/Navigation.tsx` — add nav entry

**Can develop in parallel with A** using mocked API responses.

---

#### Issue C: Isolation Tests — Cross-User + Cross-Tenant

**Scope:** Dedicated test suite verifying the isolation invariant at the API level. Must run in CI on every PR that touches `src/personal_context/`.

**Tests:**
- User A creates private entry → User B GET returns 404
- User A creates private entry → User B list returns 0 entries
- User A creates shared entry → User B (same tenant) list returns 1 entry
- User A creates shared entry → User C (different tenant) list returns 0 entries
- User A edits User B's entry → 404 (not 403, prevent enumeration)
- User A deletes User B's entry → 404
- User A toggles visibility on User B's entry → 404
- Missing JWT → 401
- Malformed user_id in JWT (not UUID) → 403

**Files to create:**
- `modules/gateway/tests/personal_context/test_isolation.py`

**Ships with Issue A** (same PR or immediately after).

---

#### Issue D: NetworkPolicy + Feature Flag Wiring

**Scope:** Kubernetes NetworkPolicy allowing gateway pods to reach OpenViking + LiteLLM in agent-context namespace. Feature flag in gateway config.

**Files to create:**
- `modules/agent-context/manifests/networkpolicy-gateway-ingress.yaml`

**Files to modify:**
- `modules/gateway/k8s/deployment.yaml` — add `PERSONAL_CONTEXT_ENABLED` env var from ConfigMap
- Gateway ConfigMap — add the three new env vars

**Ships before Issue A** (infra prerequisite).

---

## 6. Design Coverage Audit (Five-Section Check)

Per CLAUDE.md's mandatory five-section spec:

| Section | Status | Notes |
|---------|--------|-------|
| Description | ✅ Solid | Clear goal statement, motivation, gap analysis |
| Impact Analysis | ✅ Solid | Who benefits (users), who's impacted (agents, compliance), failure modes in risk register |
| Design | ✅ Solid | API contract, frontend layout, lifecycle rules, component list |
| Deployment | ⚠️ Needs addition below | |
| Validation | ⚠️ Needs addition below | |

### Deployment (added)

- **Issue A (backend):** Merging the gateway PR triggers `gateway-deploy.yml` → rebuilds gateway Docker image → deploys to EKS. No Terraform changes. No migration (AGFS is schema-less).
- **Issue B (frontend):** Merging triggers `gateway-deploy.yml` frontend job → `deploy-frontend.sh` builds SPA with VITE vars, syncs to S3, invalidates CloudFront.
- **Issue D (NetworkPolicy):** Manual `kubectl apply -f modules/agent-context/manifests/networkpolicy-gateway-ingress.yaml` OR add to the agent-context deploy workflow.
- **NOT triggered:** No agent-runtime image rebuild. No Terraform apply. No migration.
- **Prerequisite:** agent-context namespace must be deployed (`PERSONAL_CONTEXT_ENABLED=true` gated).
- **Rollback:** Revert gateway image (code-only change). Set `PERSONAL_CONTEXT_ENABLED=false` to disable without rollback.

### Validation

- **Unit tests (Issue A):** pytest suite covering all 7 endpoints with mocked AGFS backend.
- **Isolation tests (Issue C):** Cross-user + cross-tenant boundary tests (CI-blocking).
- **Integration test:** Live test against deployed agent-context namespace: create entry via MCP tool → verify visible in `/api/personal-context/entries` → edit → verify embedding updated → delete → verify gone.
- **Smoke test:** After deploy, as an authenticated user: `curl -H "Authorization: Bearer $TOKEN" https://<domain>/api/personal-context/entries` → expect `{"entries": [], "total": 0, ...}` (empty list, not error).
- **Frontend E2E:** Playwright test navigating to `/my-context`, verifying empty state renders, search input present, tabs switch.

---

## 7. Verdict

✅ **Ready for implementation** — the design is complete and implementation issues can be filed. The critical architectural decision (gateway proxying to OpenViking via cluster-internal HTTP, not a shared DB) aligns with the existing architecture where agent-context owns its own storage and the gateway is the user-facing API layer.

**Key decisions made:**
1. Gateway calls OpenViking directly (no shared Postgres table for personal context — it stays in AGFS)
2. Semantic search lives in the gateway API layer (embeds query, ranks results)
3. Hard delete (no tombstone) — compliance-friendly
4. Syntheses are read-only to users (auto-generated); users can only delete them
5. Feature-flagged for progressive rollout

---

## Appendix: Alignment with Existing Repo State

| Aspect | Finding | Conflict? |
|--------|---------|-----------|
| `TokenContext.user_id` = `claims.sub` | Confirmed in `dependencies.py:85` | No — maps directly to `owner_sub` |
| `TokenContext.org_id` = `claims.org_id` | Confirmed in `dependencies.py:89` | No — maps directly to `tenant_id` |
| Existing `/admin/users/me/chats` pattern | Confirmed in `routes.py:1130` | No — new router follows same pattern |
| Navigation pattern | Confirmed in `Navigation.tsx:80` | No — new entry slots after "My Chats" |
| Route/lazy-load pattern | Confirmed in `App.tsx:22-31` | No — new page follows same pattern |
| AGFS path structure | Confirmed in `storage.py:32-48` | No — API reads same paths |
| Identity validation | Confirmed in `identity.py:59-90` | No — gateway uses UUID validation from JWT |
| PersonalContextStore read filter | Confirmed in `storage.py:214-228` | No — gateway re-implements same logic |
| No existing gateway→agent-context connectivity | Confirmed (grep found 0 references) | **New** — requires NetworkPolicy |
| `httpx.AsyncClient` for internal HTTP | Confirmed in `credential_routes.py:34`, `github_client.py:16` | No — established pattern |
