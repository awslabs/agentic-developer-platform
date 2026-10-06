import { createHash } from 'crypto';
import { z } from 'zod';
import { AgentTool } from '../context/types';
import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';
import { createMemoryTools } from './tools';
import { MemoryCapabilities, MemoryProvider, MemoryQuery, MemoryRecord, MemoryToolScope } from './types';

const memoryId = z.string().regex(/^mem_[a-f0-9]{32}$/);
const version = z.number().int().min(1).max(99_999_999);
const kind = z.enum(['preference', 'fact', 'learning', 'draft-learning']);
const labels = z.object({ component: z.string().min(1).max(128).optional(), persona: z.string().min(1).max(128).optional() }).strict();
const content = z.string().min(1).max(65_536).refine(value => Buffer.byteLength(value) <= 131_072);
const tags = z.array(z.string().min(1).max(128)).max(32);
const recordSchema = z.object({
  id: memoryId, version, content: z.string(), kind, tags: z.array(z.string()),
  scope: z.object({ user: z.string().min(1), tenant: z.string().min(1) }).strict(),
  labels: labels.default({}),
  source: z.object({ sessionId: z.string().min(1), runId: z.string().min(1) }).strict(),
  createdAt: z.iso.datetime({ offset: true }), updatedAt: z.iso.datetime({ offset: true }),
});
const pageSchema = z.object({
  status: z.enum(['ok', 'empty', 'partial']), entries: z.array(recordSchema).max(100),
  next_cursor: z.string().min(1).max(2048).nullable(), observed_at: z.iso.datetime({ offset: true }),
  coverage: z.object({ source: z.literal('owned_memory'), complete: z.boolean(), missing_source_ids: z.array(memoryId) }),
});
const writeSchema = z.object({ content, kind: kind.default('fact'), tags: tags.default([]), labels });
const querySchema = z.object({
  query: z.string().max(1024), kinds: z.array(kind).max(4).default([]), labels,
  limit: z.number().int().min(1).max(10_000).default(20), tokenBudget: z.number().int().nonnegative().optional(),
});

export interface GatewayMemoryRecord extends MemoryRecord {
  version: number;
}

export class GatewayMemoryProvider implements MemoryProvider {
  constructor(private readonly client: ChatDataClient) {}

  #record(row: z.infer<typeof recordSchema>): GatewayMemoryRecord {
    const { labels: recordLabels, ...record } = row;
    return { ...record, scope: { ...record.scope, ...recordLabels } };
  }

  #page(raw: unknown) {
    const parsed = pageSchema.safeParse(raw);
    if (!parsed.success) throw new ChatDataError('invalid_response');
    const page = parsed.data;
    if (page.coverage.missing_source_ids.length) throw new ChatDataError('incomplete');
    if (page.coverage.complete !== (page.next_cursor === null)
      || page.status !== (page.next_cursor ? 'partial' : page.entries.length ? 'ok' : 'empty')) {
      throw new ChatDataError('invalid_response');
    }
    return page;
  }

  async retrieve(input: MemoryQuery): Promise<GatewayMemoryRecord[]> {
    const parsed = querySchema.safeParse({
      query: input.query, kinds: input.kinds, limit: input.limit, tokenBudget: input.tokenBudget,
      labels: { component: input.scope?.component, persona: input.scope?.persona },
    });
    if (!parsed.success) throw new ChatDataError('invalid_request');
    const { limit, tokenBudget, ...query } = parsed.data;
    const records: GatewayMemoryRecord[] = [];
    const ids = new Set<string>();
    const cursors = new Set<string>();
    let tokens = 0;
    let cursor: string | null = null;
    for (let pageNumber = 0; pageNumber < 100; pageNumber++) {
      const page = this.#page(await this.client.runRequest('memory/search', { ...query, limit: Math.min(limit, 4), ...(cursor ? { cursor } : {}) }));
      for (const row of page.entries) {
        if (ids.has(row.id)) throw new ChatDataError('invalid_response');
        ids.add(row.id);
        const recordTokens = Math.ceil(row.content.length / 4);
        if (tokenBudget !== undefined && tokens + recordTokens > tokenBudget) return records;
        tokens += recordTokens;
        records.push(this.#record(row));
        if (records.length === limit) return records;
      }
      if (!page.next_cursor) return records;
      if (cursors.has(page.next_cursor)) throw new ChatDataError('invalid_response');
      cursors.add(page.next_cursor);
      cursor = page.next_cursor;
    }
    throw new ChatDataError('incomplete');
  }

  async read(id: string): Promise<GatewayMemoryRecord> {
    if (!memoryId.safeParse(id).success) throw new ChatDataError('invalid_request');
    const page = this.#page(await this.client.runRequest('memory/read', { memory_id: id }));
    if (page.next_cursor || page.entries.length !== 1 || page.entries[0].id !== id) throw new ChatDataError('invalid_response');
    return this.#record(page.entries[0]);
  }

  async #write(record: Omit<MemoryRecord, 'id' | 'createdAt'>, id?: string, expectedVersion = 0): Promise<GatewayMemoryRecord> {
    const parsed = writeSchema.safeParse({
      content: record.content, kind: record.kind, tags: record.tags,
      labels: { component: record.scope?.component, persona: record.scope?.persona },
    });
    if (!parsed.success || record.metadata !== undefined) throw new ChatDataError('invalid_request');
    const write = { ...parsed.data, expected_version: expectedVersion, ...(id ? { memory_id: id } : {}) };
    const receiptSchema = z.object({ memory_id: memoryId, version });
    const result = receiptSchema.safeParse(await this.client.runRequest('memory/write', {
      ...write, idempotency_key: createHash('sha256').update(JSON.stringify(write)).digest('hex'),
    }));
    if (!result.success || result.data.version !== expectedVersion + 1 || (id && result.data.memory_id !== id)) {
      throw new ChatDataError('invalid_response');
    }
    const stored = await this.read(result.data.memory_id);
    if (stored.version !== result.data.version) throw new ChatDataError('conflict', 409);
    return stored;
  }

  save(record: Omit<MemoryRecord, 'id' | 'createdAt'>): Promise<GatewayMemoryRecord> {
    return this.#write(record);
  }

  async update(id: string, expectedVersion: number, record: Omit<MemoryRecord, 'id' | 'createdAt'>): Promise<GatewayMemoryRecord> {
    if (!memoryId.safeParse(id).success || !version.max(99_999_998).safeParse(expectedVersion).success) {
      throw new ChatDataError('invalid_request');
    }
    return this.#write(record, id, expectedVersion);
  }

  tools(scope: MemoryToolScope = {}): AgentTool[] {
    return [
      ...createMemoryTools(this, scope).map(tool => ({
        ...tool,
        description: tool.name === 'save_learning'
          ? 'Save a learning in your own memory, labelled for this persona. This does not grant other users access.' : tool.description,
      })),
      {
        name: 'read_memory', description: 'Read an authorized memory and its current version before updating it.',
        inputSchema: { id: memoryId },
        handler: async input => ({ content: [{ type: 'text', text: JSON.stringify(await this.read(input.id as string)) }] }),
      },
      {
        name: 'update_memory', description: 'Replace an owned memory at the version you read. A conflict requires rereading; it is never overwritten automatically.',
        inputSchema: { id: memoryId, expected_version: version.max(99_999_998), content, kind, tags: tags.optional() },
        handler: async input => ({ content: [{ type: 'text', text: JSON.stringify(await this.update(input.id as string, input.expected_version as number, {
          content: input.content as string, kind: input.kind as string, tags: input.tags as string[] | undefined, scope,
        })) }] }),
      },
    ];
  }

  capabilities(): MemoryCapabilities {
    return { semanticSearch: false, keywordSearch: true, tagFiltering: false, scoping: ['user', 'tenant', 'component', 'persona'], delete: false, asyncExtraction: false, ttl: true };
  }
}
