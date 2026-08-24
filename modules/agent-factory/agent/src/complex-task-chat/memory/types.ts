/**
 * MemoryProvider port — pluggable cross-agent knowledge store.
 *
 * The orchestrator never depends on a concrete implementation.
 * Everything is wired at startup by the factory reading env vars.
 */
import { AgentTool } from '../context/types';

export interface MemoryRecord {
  id: string;
  content: string;
  scope: MemoryScope;
  kind?: string;
  tags?: string[];
  source?: { agent?: string; sessionId?: string };
  createdAt: string;
  updatedAt?: string;
  metadata?: Record<string, unknown>;
}

export interface MemoryScope {
  user?: string;
  component?: string;
  tenant?: string;
  persona?: string;
}

/**
 * The authenticated scope the agent-facing memory tools are bound to.
 *
 * Closure-injected into the tools by the orchestrator (see
 * `complex-task-chat-agent.ts`) so the model cannot choose which partition it
 * reads or writes. `user`/`tenant` come from verified JWT claims; `persona`
 * MUST be the sanitized `persona.name` from `loadPersona`, never the raw
 * `agent_type` (which is LLM-derived by the classifier).
 */
export interface MemoryToolScope {
  user?: string;
  tenant?: string;
  persona?: string;
}

export interface MemoryQuery {
  query: string;
  scope?: MemoryScope;
  limit?: number;
  tokenBudget?: number;
  kinds?: string[];
}

export interface MemoryCapabilities {
  semanticSearch: boolean;
  keywordSearch: boolean;
  tagFiltering: boolean;
  scoping: Array<keyof MemoryScope>;
  delete: boolean;
  asyncExtraction: boolean;
  ttl: boolean;
}

export interface MemoryProvider {
  retrieve(input: MemoryQuery): Promise<MemoryRecord[]>;
  save(record: Omit<MemoryRecord, 'id' | 'createdAt'>): Promise<MemoryRecord>;
  delete?(id: string): Promise<void>;
  /**
   * Agent-facing tools, bound to the authenticated `scope`. Optional so
   * providers that expose no tools need no implementation change.
   */
  tools(scope?: MemoryToolScope): AgentTool[];
  capabilities(): MemoryCapabilities;
}
