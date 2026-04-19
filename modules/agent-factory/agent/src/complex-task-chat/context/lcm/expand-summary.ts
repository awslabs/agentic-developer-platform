/**
 * expand_summary tool — fetches raw source messages that a summary was built from.
 *
 * Registered via ContextManager.tools() and merged into the agent's tool set.
 */
import { z } from 'zod';
import { AgentTool, AgentToolResult } from '../types';
import { ContextStore } from '../store/port';

export function createExpandSummaryTool(store: ContextStore): AgentTool {
  return {
    name: 'expand_summary',
    description:
      'Fetch the raw source messages that a summary was built from. Use when a summary is too compressed for the current task.',
    inputSchema: {
      summary_id: z
        .string()
        .describe('The summary ID to expand, e.g. sum_<session>_<hash>'),
    },
    handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
      const summaryId = input.summary_id as string;
      if (!summaryId) {
        return text('Error: summary_id is required', true);
      }

      // Parse session ID from the summary ID format: sum_<sessionId>_<hash>
      const parts = summaryId.split('_');
      if (parts.length < 3 || parts[0] !== 'sum') {
        return text(`Error: invalid summary_id format: ${summaryId}`, true);
      }
      const sessionId = parts.slice(1, -1).join('_');

      const summary = await store.getSummaryById(sessionId, summaryId);
      if (!summary) {
        return text(`Error: summary not found: ${summaryId}`, true);
      }

      const messages = await store.getMessagesByIds(sessionId, summary.sourceIds);
      if (messages.length === 0) {
        return text(
          `Summary ${summaryId} found but source messages have been purged.\n\nSummary content:\n${summary.content}`,
        );
      }

      const lines = [
        `--- Expanded summary ${summaryId} ---`,
        `Time range: ${summary.earliestAt} to ${summary.latestAt}`,
        `Source messages: ${messages.length}`,
        '',
      ];

      for (const msg of messages) {
        lines.push(`[${msg.ts}] ${msg.role}:`);
        lines.push(msg.content);
        lines.push('');
      }

      lines.push('--- End expanded summary ---');
      return text(lines.join('\n'));
    },
  };
}

function text(s: string, isError = false): AgentToolResult {
  return { content: [{ type: 'text', text: s }], ...(isError ? { isError: true } : {}) };
}
