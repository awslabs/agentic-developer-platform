/** Read-only work summary; identity is bound to the gateway chat capability. */
import { z } from 'zod';
import { AgentTool } from '../context/types';
import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';
import { renderAgentWorkAnswer, WorkPresentation } from './answer';

interface WorkPage {
  runs: WorkPresentation['runs'];
  issues: WorkPresentation['issues'];
  last_key: string | null;
  coverage?: WorkPresentation['coverage'];
  observed_at?: string;
}

export function activityTools(client: ChatDataClient): AgentTool[] {
  return [{
    name: 'get_my_agent_work',
    description: 'Read recorded ADP agent work for the current user in a specified time window. A completed run is not proof an issue is closed. Incomplete coverage is not evidence of no work.',
    inputSchema: {
      from: z.string().datetime({ offset: true }).describe('Inclusive ISO-8601 start of the window'),
      to: z.string().datetime({ offset: true }).describe('Exclusive ISO-8601 end of the window'),
      timezone: z.string().min(1).max(64).describe('IANA timezone, for example America/New_York'),
      page_size: z.number().int().min(1).max(20).optional(),
      last_key: z.string().max(8192).optional(),
    },
    handler: async input => {
      const runs: WorkPage['runs'] = [];
      const issues = new Map<string, WorkPage['issues'][number]>();
      const coverage: NonNullable<WorkPage['coverage']> = input.last_key
        ? [{ source: 'pagination', status: 'partial', reason: 'continuation_only' }] : [];
      let observedAt: string | undefined;
      const seenRuns = new Set<string>();
      const seenCursors = new Set<string>();
      let cursor = input.last_key as string | undefined;
      const presentation = (nextCursor: string | null) => {
        const gap = coverage.some(entry => entry.status !== 'available');
        const status = gap || nextCursor ? (runs.length || coverage.some(entry => entry.status === 'available') ? 'partial' : 'unavailable')
          : runs.length ? 'ok' : 'empty';
        const work: WorkPresentation = {
          status, from: input.from as string, to: input.to as string, timezone: input.timezone as string, observed_at: observedAt,
          runs, issues: [...issues.values()], coverage, last_key: nextCursor,
        };
        return { ...work, answer: renderAgentWorkAnswer(work) };
      };
      for (let pageNumber = 0; pageNumber < 20; pageNumber++) {
        const page = await client.runRequest('activity/work', { ...input, ...(cursor ? { last_key: cursor } : {}) }) as WorkPage;
        if (!page || !Array.isArray(page.runs) || !Array.isArray(page.issues) ||
          (page.last_key !== null && typeof page.last_key !== 'string')) throw new ChatDataError('invalid_response');
        for (const run of page.runs) {
          const key = `${run.source_type}:${run.invocation_id}`;
          if (!seenRuns.has(key)) { seenRuns.add(key); runs.push(run); }
        }
        for (const issue of page.issues) {
          const existing = issues.get(issue.url);
          if (existing) {
            for (const id of issue.invocation_ids) if (!existing.invocation_ids.includes(id)) existing.invocation_ids.push(id);
          } else issues.set(issue.url, { ...issue, invocation_ids: [...issue.invocation_ids] });
        }
        if (page.coverage) coverage.push(...page.coverage.filter(entry =>
          !(entry.source === 'pagination' && entry.status === 'partial' && entry.reason === 'continuation_only')));
        observedAt = page.observed_at ?? observedAt;
        if (!page.last_key) {
          return { content: [{ type: 'text', text: JSON.stringify(presentation(null)) }] };
        }
        if (seenCursors.has(page.last_key)) throw new ChatDataError('invalid_response');
        seenCursors.add(page.last_key);
        cursor = page.last_key;
      }
      coverage.push({ source: 'pagination', status: 'partial', reason: 'page_limit' });
      return { content: [{ type: 'text', text: JSON.stringify(presentation(cursor ?? null)) }] };
    },
  }];
}
