# Server-Side Conversation Persistence

**Status:** ⚠️ **DEPRECATED — DO NOT IMPLEMENT**
**Deprecated:** 2026-04-24
**Original issue:** #124 (closed)
**Original implementation issue:** #126 (closed)
**Author:** @agent-architect
**Date:** 2026-04-24

## Why deprecated

Scope reframe. The user-visible symptom that prompted this design was narrower than what the design tackled: **replies land durably in DynamoDB but the browser never sees them when the WebSocket was dead at delivery time.**

Fixing that does not require:

- A new FastAPI module in the gateway service.
- Cross-module IAM grants (gateway EKS role → agent-factory's sessions table).
- Three REST endpoints (list / get / soft-delete).
- A frontend `useConversationSync` hook with reconciliation logic.
- Pass 1 / Pass 2 multi-PR delivery.

More fundamentally — the design put the endpoints in the **wrong place**. The chat stack is Lambda + SQS + DynamoDB (all in `modules/agent-factory/`); the gateway FastAPI is a separate service for the Bedrock proxy and admin UI. Adding chat-read endpoints to FastAPI crosses a module boundary for data the FastAPI doesn't own, introducing deploy/log/IAM coupling that doesn't need to exist.

## What's replacing it

A single small change over the existing WebSocket:

- New WS route `fetchHistory` handled by the **existing ingest Lambda**.
- Reads session messages from the sessions table (which the ingest Lambda already has access to).
- Pushes them back on the same connection via the response Lambda's existing frame shape.
- Frontend calls `fetchHistory` on conversation switch and on WS reconnect.

Reuses existing identity (`$connect` claims), existing deploy pipeline (`agent-gateway-deploy.yml`), existing log group. No new infra, no new API, no cross-module IAM.

Future user-facing needs (logout/login persistence for users on different browsers, admin listing, delete, etc.) will be filed as their own small issues *when users actually hit them* — not pre-built.

## Keeping this doc on main

This doc is preserved for historical reference on how the design evolved and why the smaller alternative was chosen. Do not use it as an implementation guide.

---

*Original design content below preserved verbatim.*

---

## Problem Summary

The chat UI relies on localStorage + live WebSocket frames for conversation state. Conversations are lost on logout, unreachable from other browsers, and responses to stale WebSocket connections are never surfaced to the user despite being durably stored in DynamoDB.

The data already exists server-side -- the `adp-dev-agent-gateway-sessions` table holds `messages`, `last_response`, and `user_workspace` per session. What's missing is a **read path** from the gateway backend and a **frontend that uses it**.

## Goals

1. User sends a long task in conv A, switches to conv B, returns to conv A -> sees the reply.
2. User closes the tab mid-task, reopens -> sees the reply (or a "still working" indicator).
3. User logs out, logs back in -> sees the same list of conversations with full history.
4. User opens the chat on a second browser -> sees all their conversations.
5. No regression for the live-WS happy path.

## Non-Goals

- Cross-tab real-time sync (v1: each tab fetches on mount/switch)
- Search across conversations
- Pagination (v1: assume <100 conversations per user)
- Retention/archival policy (existing TTL on sessions handles this)

---

## 1. GSI Choice and Key Structure

### Decision: Use existing `user-workspace-index` GSI

The `adp-dev-agent-gateway-sessions` table already has a GSI defined in Terraform at `modules/agent-factory/infra/modules/dynamodb-sessions/main.tf`:

```hcl
global_secondary_index {
  name            = "user-workspace-index"
  hash_key        = "user_workspace"
  range_key       = "session_id"
  projection_type = "ALL"
}
```

**Key structure:**
- **Partition key:** `user_workspace` (String) -- format: `{cognito_sub}#{channel}`
- **Range key:** `session_id` (String) -- format: `sess-{timestamp}-{random}`

**Why this works:**
- `user_workspace` is written on every session creation by the ingest Lambda (`handler.py` line 371): `f"{message.user_id}#{message.channel.value}"`
- `message.user_id` is the Cognito `sub` (stable, unique per user) -- extracted from JWT claims by the webchat adapter (`webchat.py` line 135)
- The GSI already has `ALL` projection, so all attributes (messages, last_response, threads, etc.) are available without a follow-up GetItem

**Query strategy:**

For the list endpoint, query with `begins_with(user_workspace, :sub_prefix)` where `:sub_prefix = "{cognito_sub}#"`. This returns sessions across all channels (webchat, slack, cli) for the user.

For webchat-only (the common case in the UI), query with exact match: `user_workspace = "{cognito_sub}#webchat"`.

The API defaults to `channel=webchat` but accepts an optional `channel` query parameter.

### Alternative considered: GSI on Cognito `sub` alone

Would require adding a new `user_id` attribute and a new GSI. Rejected because:
- The existing GSI already solves the problem
- Adding a new GSI means a Terraform change to the agent-factory module (which has its own pending plan)
- The `user_workspace` composite key gives us free channel filtering

### No Terraform changes required

The GSI, table, and TTL are already deployed. Zero infra risk for Pass 1.

---

## 2. Endpoint Contracts

New route file: `modules/gateway/src/chat/routes.py`

Register in `app.py` by adding `"src.chat.routes"` to `UNIT_MODULES`.

All endpoints require a valid Cognito JWT (`Authorization: Bearer <token>`).

### 2.1 List Conversations

```
GET /chat/conversations?channel=webchat&status=active
```

**Query parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `channel` | string | `webchat` | Channel filter. Use `all` for all channels. |
| `status` | string | `active` | `active` (non-deleted) or `all` (include soft-deleted) |

**Response: `200 OK`**
```json
{
  "conversations": [
    {
      "session_id": "sess-1777050617685-qs09zru",
      "title": "Help me debug a build failure",
      "channel": "webchat",
      "message_count": 12,
      "last_message_preview": "The build is failing because the Docker...",
      "last_message_role": "assistant",
      "has_pending_task": true,
      "created_at": "2026-04-24T18:30:17Z",
      "updated_at": "2026-04-24T19:06:38Z"
    }
  ],
  "count": 1
}
```

**Field derivation:**
- `title`: First user message content, truncated to 80 chars. Computed at query time from the `messages` array (first element where `role == "user"`). If no user message exists, use `"New conversation"`.
- `message_count`: `len(messages)` from the DDB item.
- `last_message_preview`: Last element of `messages` array, `content` field, truncated to 100 chars.
- `last_message_role`: Last element of `messages` array, `role` field.
- `has_pending_task`: `true` if any thread in `threads` has a non-empty `processing_task_id`.
- `created_at` / `updated_at`: From the session item's `created_at` / `updated_at` epoch fields, converted to ISO 8601.

**Sort order:** Descending by `updated_at` (most recent first). Sorting is done in the gateway service layer since DynamoDB returns items in range key order.

**Owner enforcement:** The query is scoped to `user_workspace = "{jwt.sub}#{channel}"`. The Cognito `sub` comes from the validated JWT, not from any user-supplied parameter. There is no code path that allows querying another user's sessions.

### 2.2 Get Conversation Messages

```
GET /chat/conversations/{session_id}/messages
```

**Path parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `session_id` | string | The session ID (e.g., `sess-1777050617685-qs09zru`) |

**Response: `200 OK`**
```json
{
  "session_id": "sess-1777050617685-qs09zru",
  "channel": "webchat",
  "messages": [
    {
      "role": "user",
      "content": "Help me debug this build failure",
      "timestamp": "2026-04-24T18:30:17Z"
    },
    {
      "role": "assistant",
      "content": "I'll look into the build logs...",
      "timestamp": "2026-04-24T18:30:45Z",
      "task_id": "abc-123"
    }
  ],
  "threads": {
    "a1b2c3d4": {
      "topic": "Debug build failure",
      "status": "idle",
      "persona": "developer"
    }
  },
  "has_pending_task": false
}
```

**Owner enforcement:** GetItem by `session_id`, then verify `user_workspace` starts with `{jwt.sub}#`. If it doesn't match, return 404 (not 403 -- don't leak the existence of other users' sessions).

**Error responses:**
- `404 Not Found`: Session does not exist or caller is not the owner.
  ```json
  {"error": "not_found", "message": "Conversation not found"}
  ```
- `401 Unauthorized`: Missing or invalid JWT (handled by auth dependency).

### 2.3 Delete Conversation (Soft Delete)

```
DELETE /chat/conversations/{session_id}
```

**Response: `200 OK`**
```json
{"message": "Conversation deleted", "session_id": "sess-1777050617685-qs09zru"}
```

**Implementation:** Sets `deleted_at` timestamp on the session item. The list endpoint filters these out by default (`status=active`). Actual data removal happens via the existing TTL (`expires_at`).

**Owner enforcement:** Same as Get -- verify `user_workspace` prefix, return 404 on mismatch.

**Why soft delete:** Enables undelete (future feature), prevents accidental data loss, and the TTL garbage-collects deleted sessions anyway.

---

## 3. Service Layer Architecture

### 3.1 Module Structure

```
modules/gateway/src/chat/
  __init__.py
  routes.py        # FastAPI router with 3 endpoints
  service.py       # ConversationService -- DynamoDB query logic
  schemas.py       # Pydantic models for request/response
```

### 3.2 ConversationService

```python
class ConversationService:
    """Read-only service for conversation persistence.

    Reads from the agent-gateway sessions DynamoDB table.
    Writes are owned by the ingest/response Lambdas -- this service
    only reads + soft-deletes.
    """

    def __init__(self, table_name: str, region: str = "us-east-1"):
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    async def list_conversations(
        self, user_id: str, channel: str = "webchat"
    ) -> list[ConversationSummary]:
        """Query the user-workspace-index GSI."""
        ...

    async def get_messages(
        self, session_id: str, user_id: str
    ) -> ConversationDetail | None:
        """GetItem + owner check."""
        ...

    async def delete_conversation(
        self, session_id: str, user_id: str
    ) -> bool:
        """Soft delete: set deleted_at, return False if not owner."""
        ...
```

### 3.3 Configuration

The sessions table name is sourced from the agent-factory Terraform outputs. The gateway needs to know this at runtime. Options:

**Chosen approach: Environment variable**

Set `AGENT_GATEWAY_SESSIONS_TABLE` in the gateway's EKS ConfigMap / deployment env. Value comes from Terraform output `gateway_sessions_table` (already exported in `modules/agent-factory/infra/gateway-outputs.tf`).

This is consistent with how other cross-module references work (e.g., `INPUT_QUEUE_URL` is an env var in the Lambda).

Add to `modules/gateway/src/shared/config.py`:
```python
agent_gateway_sessions_table: str = Field(
    default="", env="AGENT_GATEWAY_SESSIONS_TABLE"
)
```

If the env var is empty, the chat endpoints return 503 ("Chat history not available").

---

## 4. IAM Scope

### Gateway Backend (EKS) Needs Read Access

The gateway backend pods run with an IRSA role. Currently they do NOT have access to the agent-gateway sessions table. We need to add a read-only policy.

**Where to add it:** The gateway infra module (`modules/gateway/infra/`) should add a policy to the gateway's EKS service account role. Since the sessions table ARN comes from the agent-factory module, use a remote state data source (already patterned in `gateway-main.tf`).

**Alternatively** (simpler, avoids cross-module Terraform dependency): Add the policy in the agent-factory module where the table is declared. The agent-factory's `gateway-main.tf` already grants policies to the `gateway_agent` role. We can add a similar read-only policy for the gateway backend role, identified by its role ARN from the gateway module's remote state.

**Recommended: Add IAM policy in agent-factory module** to keep all sessions table permissions co-located.

**Policy (least privilege):**
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "GatewayReadSessions",
    "Effect": "Allow",
    "Action": [
      "dynamodb:GetItem",
      "dynamodb:Query"
    ],
    "Resource": [
      "arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/adp-dev-agent-gateway-sessions",
      "arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/adp-dev-agent-gateway-sessions/index/user-workspace-index"
    ]
  }]
}
```

**No write access.** The gateway backend is a read-only consumer. Writes are owned by the ingest and response Lambdas. The only write is the soft-delete (`UpdateItem` to set `deleted_at`), which requires adding `dynamodb:UpdateItem` scoped to a condition on the `deleted_at` attribute. However, to keep things simple for v1, we can grant `UpdateItem` on the table (still no `PutItem` or `DeleteItem`).

Revised policy:
```json
{
  "Action": [
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:UpdateItem"
  ]
}
```

The `UpdateItem` is used only for soft-delete (setting `deleted_at`). A condition expression in the service layer prevents it from modifying other attributes.

---

## 5. Frontend Reconciliation Strategy

### 5.1 Principle: Server-Authoritative, localStorage as Cache

- **On mount** of `AgentChat.tsx`: Fetch `GET /chat/conversations`, render immediately from localStorage, then reconcile when the fetch resolves.
- **Server wins** for any conversation that exists server-side. The server's `messages` array is the source of truth.
- **localStorage-only conversations** (created offline or before deployment) are kept as-is until they get a server-side session (which happens on first message send via the existing WS flow).
- **On logout**: Clear the localStorage cache. On next login, conversations load fresh from server.

### 5.2 Reconciliation Algorithm

```typescript
function reconcile(
  serverConversations: ServerConversation[],
  localConversations: Conversation[]
): Conversation[] {
  const merged: Conversation[] = [];
  const localMap = new Map(localConversations.map(c => [c.id, c]));

  // Server conversations take priority
  for (const server of serverConversations) {
    const local = localMap.get(server.session_id);
    localMap.delete(server.session_id);

    // Convert server messages to ChatMessage format
    const serverMessages = server.messages.map(toClientMessage);

    if (local) {
      // Merge: server messages + any local-only messages newer than
      // the server's last timestamp (sent via WS but not yet persisted)
      const lastServerTs = serverMessages.length > 0
        ? serverMessages[serverMessages.length - 1].timestamp
        : 0;
      const localOnlyMessages = local.messages.filter(
        m => m.timestamp > lastServerTs
      );
      merged.push({
        ...local,
        messages: [...serverMessages, ...localOnlyMessages],
        title: server.title || local.title,
        updatedAt: Math.max(server.updated_at, local.updatedAt),
      });
    } else {
      // Server-only conversation (from another browser or pre-cache)
      merged.push(serverToConversation(server));
    }
  }

  // Keep local-only conversations (not yet on server)
  for (const local of localMap.values()) {
    merged.push(local);
  }

  // Sort by updatedAt descending
  return merged.sort((a, b) => b.updatedAt - a.updatedAt);
}
```

### 5.3 Fetch Triggers

| Event | Action |
|-------|--------|
| Page mount (`AgentChat` renders) | Fetch conversation list |
| Conversation selected in sidebar | Fetch full messages for that session |
| Tab regains focus (`visibilitychange`) | Re-fetch conversation list (lightweight) |
| WS reconnect after disconnect | Re-fetch current conversation's messages |
| User logs out | Clear localStorage |

### 5.4 Loading States

- **Initial load:** Show localStorage conversations immediately (instant render), show a subtle loading indicator in sidebar, replace with merged data when fetch resolves.
- **Conversation switch:** Show cached messages immediately, fetch full messages in background, update on resolve.
- **Pending task indicator:** If `has_pending_task` is true from the list endpoint, show a spinner/pulsing indicator on that conversation in the sidebar.

### 5.5 New Custom Hook: `useConversationSync`

```typescript
function useConversationSync(userId: string | null) {
  // Returns: {
  //   conversations: Conversation[],
  //   isLoading: boolean,
  //   fetchMessages: (sessionId: string) => Promise<void>,
  //   refresh: () => Promise<void>,
  // }
}
```

This hook encapsulates the fetch + reconcile logic and is used by `AgentChat.tsx` alongside the existing `useAgUiEvents` hook. The WS hook continues to handle live updates; the sync hook provides the durable backstop.

---

## 6. Error Handling

### 6.1 Backend Errors

| Scenario | HTTP Status | Response | Rationale |
|----------|-------------|----------|-----------|
| Session not found | 404 | `{"error": "not_found", "message": "Conversation not found"}` | Don't distinguish "doesn't exist" from "not yours" |
| Owner mismatch | 404 | `{"error": "not_found", "message": "Conversation not found"}` | Prevents enumeration attacks |
| Sessions table not configured | 503 | `{"error": "service_unavailable", "message": "Chat history not available"}` | Graceful degradation if agent-factory not deployed |
| DynamoDB throttled | 503 | `{"error": "service_unavailable", "message": "Please try again"}` | Retry-safe |
| Invalid JWT | 401 | (handled by auth middleware) | Standard auth flow |
| `conn#` prefixed items | -- | Filtered out in query | Connection claims rows share the table; exclude by checking `session_id` prefix |

### 6.2 Frontend Error Handling

- **Fetch fails (network error, 5xx):** Show cached localStorage data, display a non-blocking toast: "Couldn't load conversation history from server. Showing cached data."
- **Fetch returns empty but localStorage has data:** Keep localStorage data (user may be on a fresh deployment where existing sessions haven't been backfilled).
- **401 on fetch:** Trigger token refresh, retry once. If still 401, redirect to login.

---

## 7. Tenant Isolation

### How It's Enforced

1. **JWT validation:** The `get_current_user` dependency validates the Cognito JWT and extracts `sub` (user ID) and `custom:org_id` (tenant ID).
2. **GSI query scoping:** The list endpoint queries with `user_workspace = "{jwt.sub}#{channel}"`. The `sub` comes from the validated JWT -- there's no user-supplied parameter that could be manipulated.
3. **GetItem owner check:** The get-messages endpoint does a GetItem by `session_id`, then verifies `user_workspace.startswith(f"{jwt.sub}#")`. On mismatch, returns 404.
4. **No admin override for reads:** Per user-services invariant #3 ("Owner-only by default. Admins have break-glass delete only -- never read"), the endpoints do NOT have an admin bypass for reading other users' conversations. The `is_admin` flag is ignored.

### Cross-Org Protection

Even if two users in different orgs somehow shared a Cognito user pool, the `sub` is unique per user and the `user_workspace` key includes it. There's no path to cross-org data leakage through these endpoints.

### `conn#` Row Filtering

The sessions table contains `conn#{connection_id}` rows for connection claims (see ingest Lambda `_persist_connection_claims`). These have `kind: "connection_claims"` and no `user_workspace` attribute, so they never appear in GSI queries. The get-messages endpoint checks `session_id.startswith("sess-")` as a belt-and-braces filter.

---

## 8. Build Order: Pass 1 vs Pass 2

### Pass 1: Backend + Infra (estimate: 1-2 days)

**Deliverables:**
1. `modules/gateway/src/chat/` -- routes, service, schemas
2. `modules/gateway/src/app.py` -- register the new router
3. `modules/gateway/src/shared/config.py` -- add `agent_gateway_sessions_table` setting
4. IAM policy for gateway EKS role (Terraform in agent-factory module)
5. `modules/gateway/k8s/` -- add env var to deployment ConfigMap
6. `modules/gateway/tests/test_chat/` -- pytest coverage:
   - Owner scoping: user A cannot list/read user B's conversations
   - Happy path: list returns conversations, get returns messages
   - 404 on missing session
   - 404 on owner mismatch (not 403)
   - Soft delete sets `deleted_at`, subsequent list excludes it
   - `conn#` rows are excluded from results
   - Empty messages array handling
   - Graceful 503 when table not configured

**Acceptance criteria:**
- `curl` with a valid Cognito JWT can list and fetch conversations
- Owner checks verified by tests (user A's JWT cannot see user B's data)
- No regression on existing gateway endpoints

### Pass 2: Frontend (estimate: 1-2 days)

**Deliverables:**
1. `modules/gateway/frontend/src/hooks/useConversationSync.ts` -- fetch + reconcile hook
2. `modules/gateway/frontend/src/services/chat.ts` -- API client for chat endpoints
3. `modules/gateway/frontend/src/pages/AgentChat.tsx` -- integrate sync hook
4. `modules/gateway/frontend/src/components/chat/ConversationSidebar.tsx` -- loading states, pending task indicator
5. `modules/gateway/frontend/src/__tests__/hooks/useConversationSync.test.ts` -- unit tests
6. Playwright E2E tests:
   - Log out -> log back in -> conversations visible
   - Send in A, switch to B, come back to A -> reply visible
   - No regression on live-WS happy path

**Acceptance criteria:**
- Playwright reproduces "log out -> log back in -> conversations visible"
- Playwright reproduces "send in A, switch to B, come back to A -> reply visible"
- Live WS path verified by re-running `/tmp/adp-multi-conv-probe.py`

### Dependency

Pass 2 depends on Pass 1 being deployed (endpoints must be live for the frontend to call them). Pass 1 has no dependencies on Pass 2.

---

## 9. Session Data Schema Reference

For implementer reference, here's the DynamoDB session item structure as written by the ingest/response Lambdas:

```json
{
  "session_id": "sess-1777050617685-qs09zru",         // PK
  "user_workspace": "abc123-def456#webchat",            // GSI PK (cognito_sub#channel)
  "connection_id": "cVaPIcQpoAMCLfA=",                 // Current WS connection (may be stale/empty)
  "channel": "webchat",
  "messages": [
    {"role": "user", "content": "Hello", "timestamp": 1777050617},
    {"role": "assistant", "content": "Hi there!", "timestamp": 1777050645, "task_id": "uuid"}
  ],
  "last_response": "Hi there!",                         // Most recent assistant response
  "threads": {
    "a1b2c3d4": {
      "topic": "Debug build",
      "path": "long_running",
      "persona": "developer",
      "processing_task_id": "uuid-or-empty",
      "messages": [],
      "created_at": 1777050617
    }
  },
  "created_at": 1777050617,
  "updated_at": 1777050645,
  "expires_at": 1777137017                              // TTL (24h from last update)
}
```

Items with `session_id` starting with `conn#` are connection claim records, not conversations. They have a different shape (`kind`, `sub`, `email`, `tenant_id`) and no `user_workspace` attribute.

---

## 10. Migration and Backward Compatibility

### No migration needed

All existing session rows already have `user_workspace` populated (written by the ingest Lambda since the table was created). The GSI is already deployed and indexed.

### Backward compatibility

- **Frontend:** The sync hook is additive. localStorage continues to work as before; server data is merged on top. Users who haven't upgraded their frontend see no change.
- **WebSocket:** The live WS path is untouched. The sync hook is a parallel read path, not a replacement.
- **Lambda writes:** No changes to the ingest or response Lambda. They continue to own all writes.
- **API route registration:** The new `src.chat.routes` module is added to `UNIT_MODULES` in `app.py`. If the module fails to import (e.g., missing boto3 in a dev setup), it's skipped gracefully (existing pattern in `create_app()`).

---

## 11. Future Considerations

These are explicitly out of scope for #124 but documented for awareness:

1. **WebSocket multiplexing:** The frontend should eventually maintain a single WS connection across conversation switches, eliminating the orphan-WS problem. This is the upstream fix; persistence is the downstream safety net.
2. **Pagination:** If users accumulate >100 conversations, add `LastEvaluatedKey`-based cursor pagination to the list endpoint.
3. **Full-text search:** Build a secondary index or use OpenSearch for searching across conversation content.
4. **Cross-tab sync:** Use `BroadcastChannel` API or `storage` events to sync state across browser tabs.
5. **Message-level IDs:** The DynamoDB messages array doesn't have per-message IDs. If we need deduplication at the message level (not just content-based), add `message_id` to the Lambda write paths.
6. **TTL extension:** Consider extending `expires_at` beyond 24h for conversations that the user has explicitly interacted with (bookmarked, starred). The current 24h TTL may be too aggressive for a "durable" conversation history.

---

## Self-Review Notes

**Material items for human review:**

1. **TTL concern:** The current 24h TTL on sessions means conversations disappear after 24 hours of inactivity. This conflicts with the "durable across logout" promise if the user logs back in >24h later. The architect recommends extending TTL to 30 days for sessions with >1 message, implemented as a Lambda write-path change in a follow-up issue.

2. **UpdateItem for soft delete:** Granting `dynamodb:UpdateItem` to the gateway role is broader than ideal. A DynamoDB condition expression in the service layer (`SET deleted_at = :ts IF attribute_not_exists(deleted_at)`) constrains what can be modified, but IAM doesn't distinguish UpdateItem by attribute. Acceptable risk for v1; can be tightened with a Lambda-backed delete endpoint later if needed.

3. **No org_id filter in DynamoDB:** The sessions table doesn't store `org_id` as a separate attribute. Tenant isolation relies on the `user_workspace` key being derived from the Cognito `sub`, which is globally unique. This is secure but means we can't do org-level queries (e.g., "list all conversations in org X"). Not needed for this issue but worth noting for future admin tooling.
