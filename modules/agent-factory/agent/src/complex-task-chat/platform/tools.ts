/**
 * First-party ADP platform tools for the sandbox turn (#8113).
 *
 * Which tools exist, who is entitled and who consented is the tools layer's decision
 * (#7245): the gateway's `tools/list` returns only the first-party tools the owner's tenant
 * entitles AND the owner consented to, evaluated for the run's tenant principal. This
 * module registers the ones it knows how to call with strict input schemas, executes them
 * through `tools/call` under the turn capability (the sandbox holds no other credential),
 * and gathers each result's `source` / `observed_at` / `coverage` into the turn's
 * `AnswerEvidence` so the owner's screen can report what the answer rested on.
 *
 * A tool the gateway lists but this build does not know is skipped, never offered half-wired.
 */
import { randomUUID } from 'node:crypto';
import { z } from 'zod';
import { AgentTool, AgentToolResult } from '../context/types';
import { AnswerEvidence, EvidenceCoverage, EvidenceSource, evidenceCoverage } from '../gateway/answer-evidence';
import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';

const instant = z.string().max(64).regex(/^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})$/);
const toolName = z.string().regex(/^[a-z][a-z0-9_]{0,63}___[a-z][a-z0-9_]{0,63}$/);

export const toolListingSchema = z.object({
  tools: z.array(z.object({
    name: toolName, description: z.string().min(1).max(4000),
    inputSchema: z.record(z.string(), z.json()), kind: z.enum(['first_party']), read_only: z.literal(true),
  }).strict()).max(32),
  withheld: z.array(z.object({ name: toolName, reason: z.string().min(1).max(100) }).strict()).max(32),
  coverage: z.array(z.object({ source: z.string().min(1).max(100), status: z.string().min(1).max(32), reason: z.string().max(160).nullable().optional() })).max(32),
  policy_revision: z.number().int().nonnegative(),
  observed_at: instant,
}).strict();
export type ToolListing = z.infer<typeof toolListingSchema>;

const callResultSchema = z.object({
  tool: toolName,
  receipt_id: z.string().min(1).max(128),
  request_id: z.string().min(1).max(128),
  status: z.literal('confirmed'),
  result: z.object({
    source: z.string().min(1).max(100),
    observed_at: instant,
    coverage: z.array(z.object({ source: z.string().min(1).max(100), status: evidenceCoverage.shape.status, reason: z.string().max(160).nullable().optional() })).max(32),
    has_more: z.boolean().optional(),
    window: z.object({ from: instant, to: instant }).strict().optional(),
  }).passthrough(),
}).strict();

/** Client-side input contracts for the first-party tools this build knows (mirrors tools_platform.py). */
export const PLATFORM_TOOL_INPUTS: Record<string, z.ZodRawShape> = {
  platform___my_flows: {
    limit: z.number().int().min(1).max(20).optional().describe('Flows to return, most recently updated first (default 10).'),
    status: z.enum(['attention_needed', 'awaiting_you', 'running', 'queued', 'complete']).optional().describe('Only flows in this derived status.'),
    include_archived: z.boolean().optional().describe('Include archived flows (default false).'),
  },
  platform___my_agent_runs: {
    since: instant.optional().describe('Inclusive ISO-8601 start of the window (default 7 days before `until`).'),
    until: instant.optional().describe('Exclusive ISO-8601 end of the window (default now).'),
    limit: z.number().int().min(1).max(20).optional().describe('Runs to return, newest first (default 10).'),
  },
  platform___my_work_items: {
    state: z.enum(['open', 'closed', 'all']).optional().describe('Item state (default open).'),
    kind: z.enum(['issue', 'pull_request', 'all']).optional().describe('Issues, pull requests or both (default all).'),
    since: instant.optional().describe('Only items updated at or after this instant (default 30 days ago).'),
    limit: z.number().int().min(1).max(30).optional().describe('Items to return, most recently updated first (default 20).'),
  },
  platform___my_budget: {
    period_type: z.enum(['daily', 'weekly', 'monthly']).optional().describe('Calendar period (default monthly).'),
  },
};

/** Keep a tool result well inside the 32,000-character tool_result bound by dropping trailing rows. */
const RESULT_BUDGET = 24_000;
const LIST_KEYS = ['flows', 'runs', 'items'] as const;

function bounded(result: Record<string, unknown>): Record<string, unknown> {
  let current = result;
  while (JSON.stringify(current).length > RESULT_BUDGET) {
    const key = LIST_KEYS.find(name => Array.isArray(current[name]) && (current[name] as unknown[]).length > 0);
    if (!key) {
      return { source: current.source, observed_at: current.observed_at, has_more: true,
        coverage: [{ source: String(current.source), status: 'partial', reason: 'result_truncated' }] };
    }
    const rows = current[key] as unknown[];
    current = { ...current, [key]: rows.slice(0, Math.max(1, Math.floor(rows.length / 2)) - (rows.length === 1 ? 1 : 0)), has_more: true,
      coverage: [...(current.coverage as unknown[]), { source: String(current.source), status: 'partial', reason: 'result_truncated' }] };
  }
  return current;
}

function links(tool: string, result: Record<string, unknown>): EvidenceSource[] {
  const found: EvidenceSource[] = [];
  const safe = (url: unknown): url is string => typeof url === 'string' && url.length <= 2048 &&
    (url.startsWith('https://') || (url.startsWith('/') && !url.startsWith('//')));
  for (const row of (result.items as Array<Record<string, unknown>> | undefined) ?? []) {
    if (safe(row.url) && typeof row.repo === 'string' && typeof row.number === 'number') found.push({ label: `${row.repo}#${row.number}`.slice(0, 100), url: row.url });
  }
  for (const row of (result.flows as Array<Record<string, unknown>> | undefined) ?? []) {
    if (safe(row.url) && typeof row.title === 'string' && row.title) found.push({ label: row.title.slice(0, 100), url: row.url });
  }
  for (const row of (result.runs as Array<Record<string, unknown>> | undefined) ?? []) {
    if (safe(row.url) && typeof row.invocation_id === 'string') found.push({ label: `run ${row.invocation_id}`.slice(0, 100), url: row.url });
  }
  return found.map(source => ({ ...source, label: source.label || tool }));
}

/** Accumulates what the turn's platform tools reported, into one `AnswerEvidence`. */
export class EvidenceCollector {
  readonly #coverage: EvidenceCoverage[] = [];
  readonly #sources: EvidenceSource[] = [];
  #from?: string;
  #to?: string;
  #observedAt?: string;
  #hasMore = false;

  get used(): boolean { return this.#coverage.length > 0; }

  record(tool: string, result: Record<string, unknown>): void {
    for (const entry of (result.coverage as Array<{ source: string; status: EvidenceCoverage['status']; reason?: string | null }>) ?? []) {
      this.#coverage.push({ source: `${tool}:${entry.source}`.slice(0, 100), status: entry.status,
        ...(entry.reason ? { reason: entry.reason.slice(0, 160) } : {}) });
    }
    const window = result.window as { from: string; to: string } | undefined;
    if (window) {
      if (!this.#from || window.from < this.#from) this.#from = window.from;
      if (!this.#to || window.to > this.#to) this.#to = window.to;
    }
    const observed = result.observed_at as string | undefined;
    if (observed && (!this.#observedAt || observed > this.#observedAt)) this.#observedAt = observed;
    if (result.has_more === true) this.#hasMore = true;
    this.#sources.push(...links(tool, result));
  }

  failure(tool: string, status: 'denied' | 'unavailable', reason: string): void {
    this.#coverage.push({ source: tool, status, reason: reason.slice(0, 160) });
  }

  compose(): AnswerEvidence | undefined {
    if (!this.used) return undefined;
    const coverage = this.#coverage.slice(0, 32);
    const statuses = new Set(coverage.map(entry => entry.status));
    const status: AnswerEvidence['status'] = statuses.has('available') && statuses.size === 1 && !this.#hasMore ? 'ok'
      : statuses.has('available') ? 'partial'
        : statuses.size === 1 && statuses.has('empty') ? 'empty'
          : statuses.size === 1 && statuses.has('denied') ? 'denied'
            : statuses.has('empty') || statuses.has('partial') ? 'partial' : 'unavailable';
    const seen = new Set<string>();
    const sources = this.#sources.filter(source => !seen.has(source.url) && seen.add(source.url)).slice(0, 32);
    return {
      status, coverage, sources, has_more: this.#hasMore,
      ...(this.#from && this.#to ? { from: this.#from, to: this.#to, timezone: 'UTC' } : {}),
      ...(this.#observedAt ? { observed_at: this.#observedAt } : {}),
    };
  }
}

/** Tool errors the model can read and recover from; anything else fails the turn. */
const READABLE_FAILURES = new Set(['denied', 'unavailable', 'invalid_request', 'rate_limited', 'conflict', 'missing']);

export function platformTools(client: ChatDataClient, listing: ToolListing, evidence: EvidenceCollector): AgentTool[] {
  const tools: AgentTool[] = [];
  for (const offered of listing.tools) {
    const inputSchema = PLATFORM_TOOL_INPUTS[offered.name];
    if (!inputSchema) {
      console.error(`[sandbox] platform tool ${offered.name} is offered by the gateway but unknown to this build; not registered`);
      continue;
    }
    tools.push({
      name: offered.name,
      description: offered.description.slice(0, 4000),
      inputSchema,
      handler: async (input): Promise<AgentToolResult> => {
        let raw: unknown;
        try {
          raw = await client.runRequest('tools/call', { tool: offered.name, arguments: input, request_id: randomUUID() });
        } catch (error) {
          if (!(error instanceof ChatDataError) || !READABLE_FAILURES.has(error.code) || error.reason !== undefined) throw error;
          const status = error.code === 'denied' ? 'denied' : 'unavailable';
          evidence.failure(offered.name, status, error.detail ?? error.code);
          return { isError: true, content: [{ type: 'text', text: `${offered.name} ${status === 'denied' ? 'was refused' : 'is unavailable'} (${error.detail ?? error.code}). Report this gap to the owner instead of guessing.` }] };
        }
        const parsed = callResultSchema.safeParse(raw);
        if (!parsed.success || parsed.data.tool !== offered.name) throw new ChatDataError('invalid_response');
        const result = bounded(parsed.data.result);
        evidence.record(offered.name, result);
        return { content: [{ type: 'text', text: JSON.stringify(result) }] };
      },
    });
  }
  return tools;
}
