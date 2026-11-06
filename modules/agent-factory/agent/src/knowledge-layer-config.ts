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

import { knowledgeBridgeUrl } from './lib/knowledgeBridge';

/**
 * Feature flag: register Knowledge Layer MCP tools.
 * Default OFF — enable after verifying the run-bound gateway bridge
 * and verbs return real data in the target environment.
 */
export const KNOWLEDGE_LAYER_ENABLED =
  (process.env.KNOWLEDGE_LAYER_ENABLED ?? '0') === '1';

/** Identity belongs to the run-bound gateway bridge, never to worker-supplied headers. */
export function buildKnowledgeLayerHeaders(): Record<string, string> {
  return {};
}

/**
 * MCP server config for the Door (HTTP transport).
 *
 * Shape matches McpHttpServerConfig from @anthropic-ai/claude-agent-sdk:
 *   { type: 'http', url: string, headers?: Record<string, string> }
 *
 * The Door uses stateless_http=True; the bridge attaches current run proof to
 * each call instead of trusting worker-supplied identity headers.
 */
export function getKnowledgeLayerMcpConfig(): {
  type: 'http';
  url: string;
  headers: Record<string, string>;
} {
  return {
    type: 'http' as const,
    url: `${knowledgeBridgeUrl()}/mcp/`,
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
