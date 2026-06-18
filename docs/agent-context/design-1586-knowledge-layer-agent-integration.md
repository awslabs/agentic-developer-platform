# Knowledge Layer Agent Integration — Design Document

> **Status**: Implementation-ready
> **Issue**: #1586 (child of EPIC #1529)
> **Author**: @agent-architect
> **Date**: 2026-06-18
> **Depends on**: #1587 (Door must serve Neptune data, not S3 fallback)
> **Reconciles with**: #1536 (agent-consumption design — closed, incorporated here)

---

## 1. Problem Statement

The MCP Door is deployed, network-reachable (`context-mcp.agent-context.svc.cluster.local:5100`),
and populated (30,132 CALLS edges across ~9 repos, 14 wikis, 15 code-indexes). However:

1. **No agent can discover it** — zero references to `context-mcp` in `modules/agent-factory/agent/src/`'s tool registration path.
2. **No agent knows what to ask** — no system-prompt or skill teaches the verbs.
3. **Identity bridge is incomplete** — the Door's ACL uses `X-GitHub-Login`/`X-GitHub-Teams` headers (GitHub-identity realm), but the agent-worker only passes `X-Owner-Sub`/`X-Tenant-Id` (Cognito-identity realm) today.

This design wires the Door into all agent runtimes and solves the identity + discovery + guidance gaps.

---

## 2. Architecture Decision: In-Process Tool Port (not external MCP transport)

### Options Considered

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| A. External MCP transport | Register `context-mcp:5100` as an external stdio/SSE MCP server in the SDK's `mcpServers` config | Zero agent-side code; SDK handles tool listing | SDK `query()` as of v0.2.141 only supports `createSdkMcpServer()` (in-process); external MCP transport over HTTP requires a client library wrapper. Also: no control over identity header injection. |
| B. In-process tool port (AgentTool[]) | Build a `KnowledgeLayerPort` that returns `AgentTool[]` (same pattern as memory, vault, artifacts) with an HTTP client calling the Door | Full control over identity headers; matches existing patterns exactly; testable with mock backend | Requires writing tool stubs + HTTP client |
| C. Hybrid: SDK Skill that reads CLAUDE.md | Teach agents via a `.claude/skills/knowledge-layer/SKILL.md` and rely on `Bash` tool to `curl` the Door | Zero code changes to agent runtime | Fragile; no structured tool_use; no input validation; identity injection impossible |

### Decision: **Option B — In-process tool port**

**Rationale:**
- Matches the established pattern exactly (`deps.context.tools()`, `deps.memory.tools()`, `deps.vault.tools()` all return `AgentTool[]`; assembled at line 340 in `complex-task-chat-agent.ts`).
- Allows injecting `X-GitHub-Login` + `X-GitHub-Teams` headers on every request (identity bridge).
- Allows typed input schemas (Zod) so the SDK validates agent-generated arguments.
- Allows structured error handling (distinguish "not indexed" from "no callers" per #1587).
- Tools surface as `mcp__chat-agent-tools__search`, `mcp__chat-agent-tools__understand` etc. in the model's tool list — same as vault/memory tools today.

---

## 3. Identity Bridge Design

### Current State (Dual-Rail Identity)

The agent runtime has **two identity systems** that do NOT overlap:

| System | Headers | Source | Used By |
|--------|---------|--------|---------|
| Personal Context (Cognito) | `X-Owner-Sub`, `X-Tenant-Id` | `cognito_sub` + `tenant_id` from SQS task payload | `remember`, `experience` verbs |
| Code ACL (GitHub) | `X-GitHub-Login`, `X-GitHub-Teams` | **NOT currently propagated to agent runtime** | `search`, `understand`, `impact`, `browse` verbs |

### The Gap

- The webhook-ingress Lambda extracts `actor.github_login` from the GitHub webhook payload and stores it in the SQS envelope (see `envelope.py` line 79: `"github_login": self.actor.github_login`).
- The `entrypoint.py` reads `actor = envelope.get("actor", {})` at line 130 but **never exports `actor["github_login"]` as an env var**.
- The agent-worker Node runtime has no `ADP_GITHUB_LOGIN` env var and cannot construct `X-GitHub-Login` headers.

### Fix: Propagate GitHub Identity Through the Dispatch Chain

```
webhook-ingress Lambda (handler.py)
  → SQS envelope.actor.github_login = sender["login"]
  → SQS envelope.actor.github_teams = [...] (NEW — from org membership API)

entrypoint.py (agent-worker-image)
  → os.environ["ADP_GITHUB_LOGIN"] = actor.get("github_login", "")  (NEW)
  → os.environ["ADP_GITHUB_TEAMS"] = actor.get("github_teams", "")  (NEW)

agent-worker.ts / complex-task-chat-agent.ts (Node runtime)
  → KnowledgeLayerClient reads process.env.ADP_GITHUB_LOGIN
  → Injects X-GitHub-Login + X-GitHub-Teams on every Door HTTP request
```

### Chat-Agent Path (webchat, not webhook)

For the `complex-task-chat` path (SQS `TaskPayload`), the task comes from the gateway webchat:
- `TaskPayload` already carries `user_id` (Cognito sub) and `tenant_id`.
- **GitHub login is NOT in the webchat TaskPayload today.** The gateway knows the user's linked GitHub identity (from the `github_connections` table in Postgres).
- **Proposal:** Add a `github_login` field to `TaskPayload` (optional), set by the gateway's dispatch Lambda when a linked GitHub account exists. Fail-closed: if missing, code-ACL verbs return empty (by design).
- **Alternatively:** the KnowledgeLayerClient can look up `cognito_sub → github_login` at the Door level (Door queries `repositories.allowed_principals` which are GitHub logins anyway). This adds a Cognito↔GitHub lookup table. Simpler to propagate at dispatch.

### Team Membership (Phase 2)

`X-GitHub-Teams` requires calling the GitHub API (`GET /orgs/{org}/teams/{team}/members/{username}`) at dispatch time or maintaining a sync. For Phase 1, **GitHub login alone** is sufficient — the Door's ACL query already supports login-only matching:

```sql
SELECT repo_name FROM repositories
WHERE '*' = ANY(allowed_principals)
   OR $login = ANY(allowed_principals)
```

Team-based ACL can be wired in Phase 2 by adding team sync to the ingestion pipeline.

---

## 4. Discovery Mechanism Design

### Decision: Enrich Existing `browse` Verb (No New Tool)

The Door's `browse(action="ls", uri="/")` already queries the `repositories` table and returns the indexed repo list. However, it returns only `repo_name` + `description` — agents need **coverage metadata** (which backends are populated per repo).

### Enhancement: Add Coverage Metadata to Browse Root

Modify `_list_repos()` in `browse_backend.py` to return per-repo backend status:

```python
# Current query:
"SELECT repo_name, description FROM repositories ORDER BY repo_name LIMIT 200"

# Enhanced query:
"""
SELECT repo_name, description, owner, last_indexed_sha,
       zoekt_status, vectors_status, structure_status, sbom_status,
       indexed_at
FROM repositories
ORDER BY repo_name LIMIT 200
"""
```

The returned `SearchHit.data` dict gains:

```json
{
  "repo_id": "aws-e/adp",
  "type": "repository",
  "description": "Agentic Developer Platform",
  "entry_type": "directory",
  "coverage": {
    "code_search": "complete",
    "wiki": "complete",
    "call_graph": "complete",
    "sbom": "complete"
  },
  "last_indexed_sha": "abc1234",
  "indexed_at": "2026-06-17T14:30:00Z"
}
```

### Why Not a Separate `catalog` Tool?

- Adding a 7th tool increases the agent's decision surface (more tools = more tool-selection errors).
- `browse` already exists, is ACL-filtered, and semantically IS the discovery verb ("navigate the indexed content filesystem").
- The `coverage` field in browse results gives agents exactly what they need: "can I do impact analysis on repo X?" → check `coverage.call_graph == "complete"`.
- Matches #1536's principle: don't add tools when enriching existing ones suffices.

---

## 5. Tool Registration — KnowledgeLayerPort

### New Files

| File | Purpose |
|------|---------|
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/port.ts` | Port interface |
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/client.ts` | HTTP client for Door |
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/tools.ts` | `AgentTool[]` factory |
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/index.ts` | Barrel export |

### Port Interface

```typescript
// port.ts
export interface KnowledgeLayerPort {
  /** Returns all knowledge-layer tools for registration */
  tools(): AgentTool[];
}
```

### HTTP Client

```typescript
// client.ts
const DOOR_URL = process.env.CONTEXT_MCP_SERVER_URL
  ?? 'http://context-mcp.agent-context.svc.cluster.local:5100';

export class KnowledgeLayerClient {
  private readonly baseUrl: string;
  private readonly githubLogin: string;
  private readonly githubTeams: string;
  private readonly ownerSub: string;
  private readonly tenantId: string;

  constructor(config: {
    baseUrl?: string;
    githubLogin?: string;
    githubTeams?: string;
    ownerSub?: string;
    tenantId?: string;
  }) {
    this.baseUrl = config.baseUrl ?? DOOR_URL;
    this.githubLogin = config.githubLogin ?? '';
    this.githubTeams = config.githubTeams ?? '';
    this.ownerSub = config.ownerSub ?? '';
    this.tenantId = config.tenantId ?? '';
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    // Code-ACL verbs: GitHub identity
    if (this.githubLogin) headers['X-GitHub-Login'] = this.githubLogin;
    if (this.githubTeams) headers['X-GitHub-Teams'] = this.githubTeams;
    // Personal-context verbs: Cognito identity
    if (this.ownerSub) headers['X-Owner-Sub'] = this.ownerSub;
    if (this.tenantId) headers['X-Tenant-Id'] = this.tenantId;

    const response = await fetch(`${this.baseUrl}/call`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ name, arguments: args }),
      signal: AbortSignal.timeout(10_000), // 10s timeout
    });

    if (!response.ok) {
      throw new Error(`Door returned ${response.status}: ${await response.text()}`);
    }
    return response.json();
  }
}
```

### Tool Definitions

Register **5 tools** (excluding `remember` — it's already handled by the existing experience-save hook):

```typescript
// tools.ts
export function knowledgeLayerTools(client: KnowledgeLayerClient): AgentTool[] {
  return [
    {
      name: 'knowledge_search',
      description: 'Search indexed code, docs, and wikis across all repos you have access to',
      inputSchema: { query: z.string(), scope: z.enum(['code', 'docs']).optional(), limit: z.number().optional() },
      handler: async (args) => client.callTool('search', args),
    },
    {
      name: 'knowledge_understand',
      description: 'Get structural understanding of a symbol, file, or module (callers, callees, dependencies)',
      inputSchema: { target: z.string(), depth: z.enum(['overview', 'detailed']).optional() },
      handler: async (args) => client.callTool('understand', args),
    },
    {
      name: 'knowledge_impact',
      description: 'Before editing/deleting a symbol, get its complete caller set across all repos. Prefer over grep for blast-radius analysis. Returns verdict-first (no_callers|contained|high_impact|cross_repo_impact)',
      inputSchema: { target: z.string(), cross_repo: z.boolean().optional() },
      handler: async (args) => client.callTool('impact', args),
    },
    {
      name: 'knowledge_browse',
      description: 'Discover which repos are indexed and what capabilities each has (code search, wiki, call graph, SBOM). Use action="ls" uri="/" to list all repos with coverage',
      inputSchema: { action: z.enum(['ls', 'tree', 'info']), uri: z.string(), depth: z.number().optional() },
      handler: async (args) => client.callTool('browse', args),
    },
    {
      name: 'knowledge_experience',
      description: 'Save or recall experiential knowledge (learnings, decisions, gotchas) scoped to your persona',
      inputSchema: {
        action: z.enum(['save', 'recall', 'list_syntheses']),
        persona: z.enum(['operations', 'developer', 'architect', 'reviewer']),
        content: z.string().optional(),
        learning_type: z.string().optional(),
        query: z.string().optional(),
        visibility: z.enum(['private', 'shared']).optional(),
        limit: z.number().optional(),
      },
      handler: async (args) => client.callTool('experience', args),
    },
  ];
}
```

### Tool Name Prefix: `knowledge_*`

Why prefix with `knowledge_` instead of using the Door's raw verb names (`search`, `understand`, etc.)?

1. **Avoid collisions** — `search` is too generic; could collide with future tools.
2. **Discoverability** — model sees `mcp__chat-agent-tools__knowledge_search` and immediately knows it's the knowledge layer.
3. **Grouping** — all knowledge-layer tools share a prefix, making system-prompt guidance ("tools starting with `knowledge_`...") easy.

### Integration Point

In `complex-task-chat-agent.ts`, add alongside existing tool sources:

```typescript
// After existing tool aggregation (line 340):
const knowledgeTools = KNOWLEDGE_LAYER_ENABLED
  ? knowledgeLayerTools(knowledgeClient)
  : [];

const tools: AgentTool[] = [
  ...deps.context.tools(),
  ...deps.memory.tools(),
  ...artifactTools,
  ...vaultTools,
  ...knowledgeTools,  // NEW
];
```

Feature flag: `KNOWLEDGE_LAYER_ENABLED` env var (default `'0'`). Set to `'1'` when the Door is deployed and #1587 is fixed.

### Agent-Worker Path (webhook agents)

The `agent-worker.ts` uses the Claude Code SDK `query()` directly without custom tools (line 1109: `allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill']`). Two options:

1. **Add the tools to agent-worker's SDK call** — requires refactoring to use the MCP server pattern.
2. **Rely on the Skill tool** — agents can read `.claude/skills/knowledge-layer/SKILL.md` which teaches them to use `curl` + the env var `CONTEXT_MCP_SERVER_URL`.

**Recommendation**: Phase 1 uses Option 2 (Skill) for webhook agents. Phase 2 refactors `agent-worker.ts` to support custom tools (larger refactor, separate issue). The chat-agent path gets full first-class tool registration immediately.

---

## 6. Usage Guidance — System Prompt Addition

### Approach: Persona-Agnostic Section in `composeSystemPrompt()`

Add a `<knowledge-layer>` section to the system prompt when knowledge tools are registered. Injected by `composeSystemPrompt()` the same way `<prior-experience>` is injected (conditional, after persona-learnings).

### Content (Injected Section)

```xml
<knowledge-layer>
You have access to a Knowledge Layer with deep code intelligence across indexed repositories.

## Available tools (all prefixed knowledge_*)

- knowledge_browse: ALWAYS call first with action="ls" uri="/" to see which repos are indexed and what each supports (code_search, wiki, call_graph, sbom).
- knowledge_search: Exact code search across all indexed repos. Better than grep for cross-repo queries.
- knowledge_understand: Structural understanding — callers, callees, dependencies of a symbol/file/module. Uses the call graph when available.
- knowledge_impact: BEFORE editing or deleting ANY symbol, call this with cross_repo=true. Returns verdict-first blast radius. Prefer over grep.
- knowledge_experience: Save/recall learnings and decisions scoped to your persona.

## When to use

1. Before editing a function → knowledge_impact (get all callers first)
2. Before deleting a symbol → knowledge_impact (cross_repo=true)
3. Entering an unfamiliar module → knowledge_understand, then Read the cited files
4. Looking for usage patterns → knowledge_search
5. "Does repo X have a call graph?" → knowledge_browse (check coverage)
6. Recording a decision or gotcha → knowledge_experience (action=save)

## Navigate-then-read workflow

1. knowledge_browse(action="ls", uri="/") → see available repos + coverage
2. knowledge_understand(target="repo/path/file.py::SymbolName") → structural overview
3. Read the cited files for full context
4. knowledge_impact(target="repo/path/file.py::SymbolName") → who depends on it

## Important

- "not indexed" ≠ "no callers". If browse shows a repo lacks call_graph coverage, don't interpret impact returning empty as proof nothing calls it.
- Coverage varies per repo: some have full call graph + wiki, others have search only.
- Results are scoped to your access — you only see repos you're authorized for.
</knowledge-layer>
```

### Persona-Specific Emphasis

Each persona gets a one-line addition in their `.md` file referencing the knowledge layer:

- **developer.md**: "Before editing/deleting any function, call `knowledge_impact` for blast radius. Before exploring unfamiliar code, call `knowledge_understand`."
- **architect.md**: "Use `knowledge_browse` + `knowledge_understand` to survey system structure before proposing designs."
- **reviewer.md**: "Use `knowledge_impact` to verify the PR author checked all callers of modified symbols."
- **operations.md**: "Use `knowledge_search` to find configuration patterns and `knowledge_impact` for dependency analysis."

---

## 7. Agent-Worker Skill (Webhook Path — Phase 1 Fallback)

For the webhook-triggered `agent-worker.ts` path (which doesn't support custom tools yet), create a skill:

### File: `.claude/skills/knowledge-layer/SKILL.md`

```markdown
# Knowledge Layer Skill

The Knowledge Layer provides code intelligence across all indexed repositories.

## Endpoint
${CONTEXT_MCP_SERVER_URL:-http://context-mcp.agent-context.svc.cluster.local:5100}

## Tools Available
Call via HTTP POST to /call with JSON body {"name": "<verb>", "arguments": {...}}

### browse — discover what's indexed
curl -s -X POST $CONTEXT_MCP_SERVER_URL/call \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Login: $ADP_GITHUB_LOGIN" \
  -d '{"name":"browse","arguments":{"action":"ls","uri":"/"}}' | jq .

### search — find code across repos
curl -s -X POST $CONTEXT_MCP_SERVER_URL/call \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Login: $ADP_GITHUB_LOGIN" \
  -d '{"name":"search","arguments":{"query":"<your query>"}}' | jq .

### understand — structural analysis
curl -s -X POST $CONTEXT_MCP_SERVER_URL/call \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Login: $ADP_GITHUB_LOGIN" \
  -d '{"name":"understand","arguments":{"target":"<repo/path::symbol>"}}' | jq .

### impact — blast radius before editing
curl -s -X POST $CONTEXT_MCP_SERVER_URL/call \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Login: $ADP_GITHUB_LOGIN" \
  -d '{"name":"impact","arguments":{"target":"<repo/path::symbol>","cross_repo":true}}' | jq .

## When to Use
- BEFORE editing or deleting any function: call impact
- When entering unfamiliar code: call understand, then read cited files
- When searching across repos: call search (better than grep for indexed repos)
- To check what's available: call browse with action="ls" uri="/"
```

---

## 8. entrypoint.py Changes (Identity Propagation)

### File: `modules/agent-factory/agent-worker-image/entrypoint.py`

Add after line 158 (`os.environ["ADP_TENANT_ID"] = tenant_id`):

```python
# Issue #1586: Expose GitHub identity for Knowledge Layer ACL.
# The Door's code-ACL verbs (search/understand/impact/browse) require
# X-GitHub-Login headers. The actor's GitHub login comes from the webhook
# envelope (set by the Lambda from the GitHub event sender field).
github_login = actor.get("github_login", "")
if github_login:
    os.environ["ADP_GITHUB_LOGIN"] = github_login
# Phase 2: github_teams for team-based ACL
github_teams = actor.get("github_teams", "")
if github_teams:
    os.environ["ADP_GITHUB_TEAMS"] = github_teams
```

### Chat-Agent Path (TaskPayload Extension)

Add `github_login?: string` to the `TaskPayload` interface in `sqs-client.ts`. The gateway dispatch Lambda should populate this from the user's linked GitHub connection.

---

## 9. Feature Flag & Deployment Gating

### Environment Variables (agent-worker pod)

| Var | Default | Purpose |
|-----|---------|---------|
| `KNOWLEDGE_LAYER_ENABLED` | `0` | Master switch; set to `1` to register tools |
| `CONTEXT_MCP_SERVER_URL` | `http://context-mcp.agent-context.svc.cluster.local:5100` | Door endpoint |
| `ADP_GITHUB_LOGIN` | (empty) | Set by entrypoint.py from dispatch envelope |
| `ADP_GITHUB_TEAMS` | (empty) | Phase 2: team memberships |

### Deployment Sequence

1. **#1587 MUST ship first** — without it, `understand`/`impact` return S3 fallback data (useless).
2. Merge identity propagation changes (`entrypoint.py` + `TaskPayload` extension).
3. Merge KnowledgeLayerPort + tools + system-prompt injection (feature-flagged off).
4. Merge skill file (`.claude/skills/knowledge-layer/SKILL.md`).
5. Rebuild agent-worker image (picks up entrypoint.py + skill).
6. Set `KNOWLEDGE_LAYER_ENABLED=1` in the deployment ConfigMap.
7. Verify with smoke test.

---

## 10. Security Considerations

| Risk | Mitigation |
|------|-----------|
| Agent fabricates `X-GitHub-Login` header | Impossible — headers injected by trusted code from env vars set by entrypoint.py from SQS envelope. Agent code runs in SDK subprocess with inherited env but cannot modify parent headers |
| Cross-tenant data leak via ACL bypass | Door ACL is fail-closed (no principal → empty results). If `ADP_GITHUB_LOGIN` is empty/missing, agent gets nothing from code verbs — safe default |
| Agent receives data from repos it shouldn't see | ACL filter is post-query, based on `repositories.allowed_principals`. Same mechanism already proven for browse/search |
| Denial of service via expensive queries | Door has 10s timeout on Neptune queries (D14: `connection_timeout=5s`), bounded results (D15: 100 paths max), Zoekt search capped at raw_limit |
| Identity spoofing on chat-agent path | `github_login` must come from the gateway's authenticated context (JWT → linked GitHub account lookup), not from user input |

---

## 11. Testing Strategy

### Unit Tests

| Test | What it proves |
|------|---------------|
| `knowledge-layer/client.test.ts` | HTTP client sends correct headers, handles timeouts, parses responses |
| `knowledge-layer/tools.test.ts` | Tools have valid schemas, return structured results, surface errors cleanly |
| `entrypoint.py` test update | `ADP_GITHUB_LOGIN` is exported from envelope.actor.github_login |

### Integration Tests

- Agent pod calls `knowledge_browse(action="ls", uri="/")` → gets non-empty repo list.
- Agent pod calls `knowledge_impact(target="known/symbol")` → gets results scoped to allowed repos only.
- Agent pod with empty `ADP_GITHUB_LOGIN` → all code verbs return empty (fail-closed verified).

### Smoke Test (Post-Deploy)

```bash
# From an agent pod in adp-agents namespace:
curl -s -X POST http://context-mcp.agent-context.svc.cluster.local:5100/call \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Login: <test-user>" \
  -d '{"name":"browse","arguments":{"action":"ls","uri":"/"}}' | jq '.[] | .data.repo_id'

# Verify ACL: call with non-existent login → empty results
curl -s -X POST http://context-mcp.agent-context.svc.cluster.local:5100/call \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Login: nonexistent-user-12345" \
  -d '{"name":"search","arguments":{"query":"main"}}' | jq '.total'
# Expected: 0
```

---

## 12. Phasing Summary

| Phase | Scope | Depends On |
|-------|-------|-----------|
| **Phase 1a** | Identity propagation (`entrypoint.py` + skill file) | Nothing |
| **Phase 1b** | KnowledgeLayerPort + tools for chat-agent path | Phase 1a |
| **Phase 1c** | browse enhancement (coverage metadata) in Door | Nothing |
| **Phase 2** | `agent-worker.ts` refactor for custom tools (webhook path gets first-class tools) | Phase 1b proven |
| **Phase 2** | `X-GitHub-Teams` propagation + team-based ACL | Team sync mechanism |
| **Phase 2** | Gateway dispatch: populate `github_login` in webchat TaskPayload | Gateway linked-accounts query |

---

## 13. Files to Create

| File | Module | Purpose |
|------|--------|---------|
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/port.ts` | agent-factory | Port interface |
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/client.ts` | agent-factory | HTTP client for Door |
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/tools.ts` | agent-factory | AgentTool[] factory |
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/index.ts` | agent-factory | Barrel export |
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/client.test.ts` | agent-factory | Unit tests |
| `modules/agent-factory/agent/src/complex-task-chat/knowledge-layer/tools.test.ts` | agent-factory | Unit tests |
| `.claude/skills/knowledge-layer/SKILL.md` | repo root | Skill for webhook agents |

## 14. Files to Modify

| File | Change |
|------|--------|
| `modules/agent-factory/agent-worker-image/entrypoint.py` | Export `ADP_GITHUB_LOGIN` + `ADP_GITHUB_TEAMS` from envelope |
| `modules/agent-factory/agent/src/complex-task-chat/sqs-client.ts` | Add `github_login?: string` to TaskPayload |
| `modules/agent-factory/agent/src/complex-task-chat/complex-task-chat-agent.ts` | Import + instantiate KnowledgeLayerClient, add tools to aggregation |
| `modules/agent-factory/agent/src/complex-task-chat/persona-loader.ts` | Add `<knowledge-layer>` section to composeSystemPrompt |
| `modules/agent-context/door/browse_backend.py` | Enhance `_list_repos()` query to include coverage metadata |
| `modules/agent-factory/rules/personas/developer.md` | One-line knowledge-layer guidance |
| `modules/agent-factory/rules/personas/architect.md` | One-line knowledge-layer guidance |
| `modules/agent-factory/rules/personas/reviewer.md` | One-line knowledge-layer guidance |
| `modules/agent-factory/rules/personas/operations.md` | One-line knowledge-layer guidance |

---

## 15. Relationship to #1536 (Closed)

Issue #1536 defined the agent-consumption contract:
- **Verdict-first, ranked, bounded output** — already implemented in Door's `impact` verb (returns verdict + bounded at 100).
- **Task-shaped verb descriptions** — implemented in this design via tool descriptions + system-prompt guidance.
- **Navigate-then-read workflow** — documented in the `<knowledge-layer>` system-prompt section.
- **Eval integration (#1511)** — orthogonal; once tools are registered, the eval can grade tool selection.

This design fulfills #1536's agent-consumption requirements without duplicating work.

---

## 16. Dependency on #1587

**Critical**: Until #1587 is fixed, `understand` and `impact` serve S3-fallback data (flat symbol lists, not transitive call graphs). Registering tools that return low-quality data is worse than no tools — agents will learn to distrust the knowledge layer.

**Mitigation**: Feature flag (`KNOWLEDGE_LAYER_ENABLED=0` by default). Only flip to `1` after #1587 is verified fixed in the live pod. The Phase 1a identity propagation + skill file can ship independently.
