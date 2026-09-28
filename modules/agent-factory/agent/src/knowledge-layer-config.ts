/**
 * Knowledge Layer MCP configuration.
 *
 * Builds the mcpServers entry for mounting the Door (context-mcp) as a native
 * MCP server via HTTP transport. Used by both agent-worker.ts (webhook agents)
 * and complex-task-chat-agent.ts (webchat agents).
 *
 * Feature-flagged: KNOWLEDGE_LAYER_ENABLED=1 to enable (default: off).
 *
 * Issue #1592 — register Door as agent MCP tools.
 * Depends on: #1590 (canonical URL), #1591 (identity header), #1602 (native MCP).
 */

/**
 * Feature flag: register Knowledge Layer MCP tools.
 * Default OFF — flip ON after verifying identity headers propagate correctly
 * and verbs return real data in the target environment.
 */
export const KNOWLEDGE_LAYER_ENABLED =
  (process.env.KNOWLEDGE_LAYER_ENABLED ?? '0') === '1';

/**
 * Door MCP endpoint URL. Uses CONTEXT_MCP_SERVER_URL env var if set (for
 * non-standard deployments or local dev), otherwise defaults to the in-cluster
 * Kubernetes service address.
 */
const DOOR_MCP_URL =
  process.env.CONTEXT_MCP_SERVER_URL
    ? `${process.env.CONTEXT_MCP_SERVER_URL.replace(/\/+$/, '')}/mcp/`
    : 'http://context-mcp.agent-context.svc.cluster.local:5100/mcp/';

/**
 * Build identity headers from available environment variables.
 *
 * Code verbs (search/understand/impact/browse) ACL on X-GitHub-Login/X-GitHub-Teams.
 * Personal verbs (remember/experience) ACL on X-Owner-Sub/X-Tenant-Id.
 *
 * Headers are set from TRUSTED env vars injected by entrypoint.py (from SQS
 * envelope metadata) — never from agent/LLM input.
 */
export function buildKnowledgeLayerHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};

  const login = process.env.ADP_GITHUB_LOGIN;
  if (login) headers['X-GitHub-Login'] = login;

  const teams = process.env.ADP_GITHUB_TEAMS;
  if (teams) headers['X-GitHub-Teams'] = teams;

  const ownerSub = process.env.ADP_OWNER_SUB;
  if (ownerSub) headers['X-Owner-Sub'] = ownerSub;

  const tenantId = process.env.ADP_TENANT_ID;
  if (tenantId) headers['X-Tenant-Id'] = tenantId;

  return headers;
}

/**
 * MCP server config for the Door (HTTP transport).
 *
 * Shape matches McpHttpServerConfig from @anthropic-ai/claude-agent-sdk:
 *   { type: 'http', url: string, headers?: Record<string, string> }
 *
 * The Door uses stateless_http=True, so each tool call is a fresh HTTP POST
 * carrying its own identity headers — no session stickiness required.
 */
export function getKnowledgeLayerMcpConfig(): {
  type: 'http';
  url: string;
  headers: Record<string, string>;
} {
  return {
    type: 'http' as const,
    url: DOOR_MCP_URL,
    headers: buildKnowledgeLayerHeaders(),
  };
}

/** MCP server name used for the Knowledge Layer. Tools surface as mcp__knowledge-layer__<name>. */
export const KNOWLEDGE_LAYER_SERVER_NAME = 'knowledge-layer';

/**
 * Tool names exposed by the Door, prefixed for use in allowedTools.
 * These match the tool names registered in mcp_app.py.
 */
export const KNOWLEDGE_LAYER_TOOLS = [
  'mcp__knowledge-layer__search',
  'mcp__knowledge-layer__understand',
  'mcp__knowledge-layer__impact',
  'mcp__knowledge-layer__browse',
  'mcp__knowledge-layer__remember',
  'mcp__knowledge-layer__experience',
] as const;

/**
 * System prompt section injected when Knowledge Layer is enabled.
 * Teaches agents WHEN and HOW to use each verb effectively.
 *
 * Key design decisions:
 * - understand is for STRUCTURAL targets (file paths, symbols), NOT NL questions (#1643)
 * - impact should be called BEFORE editing/deleting symbols, not after
 * - browse first to check coverage before assuming "not found" = "no callers"
 */
export const KNOWLEDGE_LAYER_PROMPT = `
<knowledge-layer>
You have access to a Knowledge Layer with deep code intelligence across indexed repositories.

## Available tools (MCP server: knowledge-layer)
- search: Exact code search across all indexed repos. Better than grep for cross-repo queries.
- understand: Structural understanding — callers, callees, dependencies of a symbol/file/module. Pass STRUCTURAL targets (file paths, symbol names), NOT natural-language questions.
- impact: BEFORE editing or deleting ANY symbol, call this with cross_repo=true. Returns verdict-first blast radius.
- browse: Discover which repos are indexed and what capabilities each has. Use action="ls" uri="/" to list repos.
- remember: Save session decisions and context to long-term memory.
- experience: Save/recall experiential learnings scoped to your persona.

## When to use
1. Before editing a function -> impact (get all callers first)
2. Before deleting a symbol -> impact (cross_repo=true)
3. Entering an unfamiliar module -> understand (target="repo/path/file.py::SymbolName")
4. Looking for usage patterns -> search
5. "Does repo X have a call graph?" -> browse (check coverage.call_graph)
6. Recording a decision or gotcha -> experience (action=save)

## Important
- "not indexed" does not mean "no callers". Check browse first.
- Coverage varies per repo: some have full call graph + wiki, others search only.
- Results are scoped to your access — you only see repos you are authorized for.
- understand is for STRUCTURAL targets (paths, symbols), NOT natural-language questions.
</knowledge-layer>`;
