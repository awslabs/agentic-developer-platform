/**
 * Agent-facing memory tools exposed via MemoryProvider.tools().
 *
 * Tools: recall_memory, save_fact, save_preference, save_learning
 *
 * All tools closure-inject the authenticated scope (user/tenant/persona) from
 * the session's trusted dispatch metadata, mirroring `vault/tools.ts`, so the
 * agent cannot read or poison another user's or tenant's memory by
 * manipulating tool input. The scope reaches
 * `DynamoMemoryProvider.buildScopeKeys` as the DynamoDB partition key, so a
 * model-supplied value there is a cross-tenant read and a persistent poisoning
 * primitive. The model therefore cannot express a scope dimension at all — the
 * fields are absent from every input schema, and handlers never read them.
 *
 * Issue #4074 (sub-EPIC #4068 · D, finding #6).
 */
import { z } from 'zod';
import { MemoryProvider, MemoryToolScope } from './types';
import { AgentTool, AgentToolResult } from '../context/types';

export function createMemoryTools(provider: MemoryProvider, scope: MemoryToolScope = {}): AgentTool[] {
  return [
    {
      name: 'recall_memory',
      description:
        'Search for remembered facts, preferences, and learnings. Use to check what you know about a user, component, or topic before starting work. Automatically scoped to the current session.',
      inputSchema: {
        query: z.string().describe('Search query (keyword match against stored content)'),
        limit: z.number().int().positive().optional().describe('Max results (default 10)'),
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const records = await provider.retrieve({
          query: input.query as string,
          scope,
          limit: (input.limit as number) ?? 10,
        });
        if (records.length === 0) return text('No matching memories found.');
        return text(
          records
            .map(r => `[${r.id}] (${r.kind ?? 'unknown'}, ${r.createdAt}) ${r.content}`)
            .join('\n\n'),
        );
      },
    },
    {
      name: 'save_fact',
      description:
        'Save a factual observation about a component, system, or process for future reference. Automatically scoped to the current session.',
      inputSchema: {
        content: z.string().describe('The fact to remember'),
        kind: z.enum(['fact', 'learning', 'draft-learning']).optional().describe('Record kind (default: fact)'),
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const record = await provider.save({
          content: input.content as string,
          scope,
          kind: (input.kind as string) ?? 'fact',
        });
        return text(`Saved: ${record.id}`);
      },
    },
    {
      name: 'save_preference',
      description:
        'Save a user preference (e.g. communication style, formatting preference). Automatically saved against the current user.',
      inputSchema: {
        content: z.string().describe('The preference to remember'),
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const record = await provider.save({
          content: input.content as string,
          scope: { user: scope.user },
          kind: 'preference',
        });
        return text(`Saved preference: ${record.id}`);
      },
    },
    {
      name: 'save_learning',
      description:
        'Save a durable learning or insight from this work session. Scoped to the current session and persona so other agents with the same role benefit.',
      inputSchema: {
        content: z.string().describe('The learning to remember'),
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const record = await provider.save({
          content: input.content as string,
          scope,
          kind: 'learning',
        });
        return text(`Saved learning: ${record.id}`);
      },
    },
  ];
}

function text(s: string): AgentToolResult {
  return { content: [{ type: 'text', text: s }] };
}
