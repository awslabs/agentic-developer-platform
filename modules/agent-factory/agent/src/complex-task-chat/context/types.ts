/**
 * ContextManager port — pluggable session context management.
 *
 * The orchestrator never depends on a concrete implementation.
 * Everything is wired at startup by the factory reading env vars.
 */
import type { ZodRawShape } from 'zod';

/** SDK-compatible message shape */
export interface SDKMessage {
  role: 'user' | 'assistant';
  content: string;
}

/**
 * A tool definition exposed to the agent.
 *
 * Shape is compatible with @anthropic-ai/claude-agent-sdk's `tool()` helper:
 * - `inputSchema` is a Zod raw shape (object of Zod validators)
 * - `handler` returns `{ content: [{ type: 'text', text: '...' }] }` — standard MCP CallToolResult
 *
 * At registration time the orchestrator wraps the tool list with
 * `createSdkMcpServer({ name, tools: [...] })` and passes it as
 * `options.mcpServers.<server>` to `query()`.
 */
export interface AgentTool {
  name: string;
  description: string;
  inputSchema: ZodRawShape;
  handler: (args: Record<string, unknown>) => Promise<AgentToolResult>;
}

/** Minimal MCP CallToolResult shape the handler returns */
export interface AgentToolResult {
  content: Array<{ type: 'text'; text: string }>;
  isError?: boolean;
}

/** Metadata about a context assembly result */
export interface AssemblyMeta {
  rawMessageCount: number;
  summaryCount: number;
  estimatedTokens: number;
  compactionTriggered: boolean;
}

/** A resolved context item ready for prompt injection */
export interface ResolvedItem {
  /** Ordinal position in the session timeline */
  ordinal: number;
  /** The message to inject into the prompt */
  message: SDKMessage;
  /** Estimated token count */
  tokens: number;
  /** Whether this is a raw message or a summary */
  type: 'message' | 'summary';
  /** The underlying message or summary ID */
  id: string;
  /** For summaries: the summary record ID */
  summaryId?: string;
}

/**
 * Primary port: ContextManager
 *
 * Implementations: LcmContext, NoopContextManager
 */
export interface ContextManager {
  assemble(input: {
    sessionId: string;
    userMessage: string;
    tokenBudget: number;
  }): Promise<{ messages: SDKMessage[]; meta: AssemblyMeta }>;

  /**
   * Append user + assistant messages atomically and refresh the session header
   * (lastActivityAt + ttl). Does NOT touch ownerUserId/tenantId after creation —
   * those are set exactly once by assertOwnership.
   */
  record(input: {
    sessionId: string;
    userMessage: SDKMessage;
    assistantMessage: SDKMessage;
  }): Promise<void>;

  /**
   * If the session exists and is owned by a different user, throws.
   * If the session does not exist, creates the header with ownerUserId + tenantId
   * via a conditional put (attribute_not_exists) to avoid races.
   * On concurrent create, the loser re-reads and verifies ownership.
   */
  assertOwnership(sessionId: string, userId: string, tenantId?: string): Promise<void>;

  /** Tools exposed to the agent (e.g. expand_summary) */
  tools(): AgentTool[];
}
