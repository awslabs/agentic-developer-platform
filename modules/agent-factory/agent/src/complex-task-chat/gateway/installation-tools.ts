import { z } from 'zod';
import { AgentTool } from '../context/types';
import { ChatDataClient, ChatDataError } from './chat-data-client';

const installationIdSchema = z.number().int().positive().max(Number.MAX_SAFE_INTEGER).describe('Connected installation identifier');

export function installationToolsForTurn(client: ChatDataClient): AgentTool[] {
  return (['status', 'failure'] as const).map(kind => ({
    name: `installation_${kind}`,
    description: kind === 'status'
      ? 'Read bounded deployment status for an installation you installed. Unknown release data is not a verified version.'
      : 'Read bounded failed-stage evidence for an installation you installed. This cannot query logs or cloud resources.',
    inputSchema: {
      installation_id: installationIdSchema,
    },
    inputSummarySanitizer: (input: Record<string, unknown>) => {
      const parsed = installationIdSchema.safeParse(input?.installation_id);
      return parsed.success ? { installation_id: parsed.data } : {};
    },
    handler: async ({ installation_id }) => {
      try {
        const result = await client.runRequest(`installation/${kind}`, { installation_id });
        return { content: [{ type: 'text' as const, text: JSON.stringify(result) }] };
      } catch (error) {
        const code = error instanceof ChatDataError ? error.code : 'unavailable';
        return { content: [{ type: 'text' as const, text: JSON.stringify({ status: code }) }], isError: true };
      }
    },
  }));
}
