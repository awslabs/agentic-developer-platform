/**
 * Agent-facing memory tools exposed via MemoryProvider.tools().
 *
 * Tools: recall_memory, save_fact, save_preference, save_learning
 */
import { z } from 'zod';
import { MemoryProvider, MemoryScope } from './types';
import { AgentTool, AgentToolResult } from '../context/types';

const scopeSchema = z
  .object({
    user: z.string().optional(),
    component: z.string().optional(),
    persona: z.string().optional(),
    tenant: z.string().optional(),
  })
  .partial()
  .optional();

export function createMemoryTools(provider: MemoryProvider): AgentTool[] {
  return [
    {
      name: 'recall_memory',
      description:
        'Search for remembered facts, preferences, and learnings. Use to check what you know about a user, component, or topic before starting work.',
      inputSchema: {
        query: z.string().describe('Search query (keyword match against stored content)'),
        scope: scopeSchema,
        limit: z.number().int().positive().optional().describe('Max results (default 10)'),
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const records = await provider.retrieve({
          query: input.query as string,
          scope: input.scope as MemoryScope | undefined,
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
      description: 'Save a factual observation about a component, system, or process for future reference.',
      inputSchema: {
        content: z.string().describe('The fact to remember'),
        scope: z
          .object({
            user: z.string().optional(),
            component: z.string().optional(),
            persona: z.string().optional(),
            tenant: z.string().optional(),
          })
          .partial(),
        kind: z.enum(['fact', 'learning', 'draft-learning']).optional().describe('Record kind (default: fact)'),
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const record = await provider.save({
          content: input.content as string,
          scope: input.scope as MemoryScope,
          kind: (input.kind as string) ?? 'fact',
        });
        return text(`Saved: ${record.id}`);
      },
    },
    {
      name: 'save_preference',
      description: 'Save a user preference (e.g. communication style, formatting preference).',
      inputSchema: {
        content: z.string().describe('The preference to remember'),
        user: z.string().describe('User ID this preference belongs to'),
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const record = await provider.save({
          content: input.content as string,
          scope: { user: input.user as string },
          kind: 'preference',
        });
        return text(`Saved preference: ${record.id}`);
      },
    },
    {
      name: 'save_learning',
      description:
        'Save a durable learning or insight from this work session. Scoped to the current persona by default so other agents with the same role benefit.',
      inputSchema: {
        content: z.string().describe('The learning to remember'),
        scope: scopeSchema,
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const scope = (input.scope as MemoryScope) ?? {};
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
