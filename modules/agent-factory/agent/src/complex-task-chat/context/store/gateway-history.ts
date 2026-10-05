import { z } from 'zod';
import { ChatDataClient, ChatDataError } from '../../gateway/chat-data-client';
import { ContextItem, ContextStore, StoredMessage, StoredSummary, TranscriptEntry } from './port';

const reference = z.string().regex(/^[A-Za-z0-9_.:-]{1,256}$/);
const ordinal = z.number().int().min(0).max(99_999_999);
const version = ordinal;
const timestamp = z.iso.datetime({ offset: true });
const messageSchema = z.object({
  role: z.enum(['user', 'assistant']), content: z.string(), ts: timestamp,
  tokens: z.number().int().nonnegative(), parts: z.array(z.unknown()).optional(),
});
const summarySchema = z.object({
  depth: z.number().int().nonnegative(), kind: z.enum(['leaf', 'condensed']), content: z.string(),
  sourceIds: z.array(reference), parentIds: z.array(reference).optional(),
  earliestAt: timestamp, latestAt: timestamp, tokens: z.number().int().nonnegative(),
});
const itemSchema = z.object({
  ordinal, type: z.enum(['msg', 'sum']), ref: reference, tokens: z.number().int().nonnegative().optional(),
});
const envelopeSchema = z.object({
  status: z.enum(['ok', 'empty', 'partial']), next_cursor: z.string().min(1).max(2048).nullable(), observed_at: timestamp,
  coverage: z.object({ source: z.literal('session_context'), complete: z.boolean(), missing_source_ids: z.array(reference) }),
});
const itemsPageSchema = envelopeSchema.extend({ entries: z.array(itemSchema).max(100), version });
const messagesPageSchema = envelopeSchema.extend({ entries: z.array(z.object({ ref: reference, message: messageSchema })).max(100) });
const summaryPageSchema = envelopeSchema.extend({ entries: z.array(z.object({ ref: reference, summary: summarySchema })).max(1) });
const writeSchema = z.object({
  idempotency_key: z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/), expected_version: version,
  content: z.string().min(1).max(65_536).refine(value => Buffer.byteLength(value) <= 131_072),
  tokens: z.number().int().min(0).max(1_000_000),
}).strict();
const appendSchema = writeSchema.extend({ user_turn_id: reference });
const sourcesSchema = writeSchema.extend({
  source_ids: z.array(reference).max(1000).default([]), parent_ids: z.array(reference).max(1000).default([]),
});
function distinctSources(input: z.infer<typeof sourcesSchema>): boolean {
  const count = input.source_ids.length + input.parent_ids.length;
  return count > 0 && count <= 1000 && new Set(input.source_ids).size === input.source_ids.length
    && new Set(input.parent_ids).size === input.parent_ids.length;
}
const summaryWriteSchema = sourcesSchema.refine(distinctSources);
const compactSchema = sourcesSchema.extend({ from_ordinal: ordinal, to_ordinal: ordinal })
  .refine(distinctSources).refine(input => input.from_ordinal <= input.to_ordinal);
const appendResultSchema = z.object({ message_id: reference, ordinal, version }).strict();
const summaryResultSchema = z.object({ summary_id: reference, version }).strict();

export type HistoryAppend = z.input<typeof appendSchema>;
export type HistorySummaryWrite = z.input<typeof summaryWriteSchema>;
export type HistoryCompaction = z.input<typeof compactSchema>;
export interface HistorySnapshot { version: number; items: ContextItem[] }

type HistoryReads = Pick<ContextStore,
  'readContextItems' | 'readAllContextItems' | 'getFullTranscript' | 'getMessagesByIds' | 'getSummaryById'>;

function validated<Value>(schema: z.ZodType<Value>, input: unknown, code: 'invalid_request' | 'invalid_response'): Value {
  const parsed = schema.safeParse(input);
  if (!parsed.success) throw new ChatDataError(code);
  return parsed.data;
}

function completePage<Page extends z.infer<typeof envelopeSchema> & { entries: unknown[] }>(page: Page): Page {
  if (page.coverage.missing_source_ids.length) throw new ChatDataError('incomplete');
  if (page.coverage.complete !== (page.next_cursor === null)
    || page.status !== (page.next_cursor ? 'partial' : page.entries.length ? 'ok' : 'empty')) {
    throw new ChatDataError('invalid_response');
  }
  return page;
}

export class GatewayHistoryStore implements HistoryReads {
  constructor(private readonly client: ChatDataClient) {}

  async acceptedUserTurn(sessionId: string) {
    return validated(z.object({ message_id: reference, ordinal: ordinal.min(1) }).strict(),
      await this.client.sessionRequest('history/turn', sessionId), 'invalid_response');
  }

  async readPage(sessionId: string, input: { limit?: number; cursor?: string } = {}) {
    const payload = validated(z.object({
      limit: z.number().int().min(1).max(100).default(100), cursor: z.string().min(1).max(2048).optional(),
    }).strict(), input, 'invalid_request');
    const page = completePage(validated(itemsPageSchema,
      await this.client.sessionRequest('history/read', sessionId, payload), 'invalid_response'));
    if (page.entries.length > payload.limit) throw new ChatDataError('invalid_response');
    return page;
  }

  async readSnapshot(sessionId: string): Promise<HistorySnapshot> {
    const items: ContextItem[] = [];
    const cursors = new Set<string>();
    const references = new Set<string>();
    let snapshotVersion: number | undefined;
    let cursor: string | undefined;
    for (let pageNumber = 0; pageNumber < 1000; pageNumber++) {
      const page = await this.readPage(sessionId, { ...(cursor ? { cursor } : {}) });
      if (snapshotVersion !== undefined && snapshotVersion !== page.version) throw new ChatDataError('conflict');
      snapshotVersion = page.version;
      for (const item of page.entries) {
        const key = `${item.type}:${item.ref}`;
        if ((items.length && item.ordinal <= items[items.length - 1].ordinal) || references.has(key)) {
          throw new ChatDataError('invalid_response');
        }
        references.add(key);
        items.push(item);
      }
      if (!page.next_cursor) return { version: snapshotVersion, items };
      if (cursors.has(page.next_cursor)) throw new ChatDataError('invalid_response');
      cursors.add(page.next_cursor);
      cursor = page.next_cursor;
    }
    throw new ChatDataError('incomplete');
  }

  async readContextItems(sessionId: string): Promise<ContextItem[]> {
    return (await this.readSnapshot(sessionId)).items;
  }

  async readAllContextItems(sessionId: string): Promise<ContextItem[]> {
    return this.readContextItems(sessionId);
  }

  async getMessagesByIds(sessionId: string, ids: string[]): Promise<StoredMessage[]> {
    const references = validated(z.array(reference), ids, 'invalid_request');
    const messages: StoredMessage[] = [];
    for (let offset = 0; offset < references.length || offset === 0; offset += 4) {
      const batch = references.slice(offset, offset + 4);
      const page = completePage(validated(messagesPageSchema,
        await this.client.sessionRequest('history/messages', sessionId, { ids: batch }), 'invalid_response'));
      if (page.next_cursor || page.entries.length !== batch.length || page.entries.some((entry, index) => entry.ref !== batch[index])) {
        throw new ChatDataError('invalid_response');
      }
      messages.push(...page.entries.map(entry => entry.message));
    }
    return messages;
  }

  async getSummaryById(sessionId: string, summaryId: string): Promise<StoredSummary> {
    validated(reference, summaryId, 'invalid_request');
    const page = completePage(validated(summaryPageSchema,
      await this.client.sessionRequest('history/summary', sessionId, { summary_id: summaryId }), 'invalid_response'));
    if (page.next_cursor || page.entries.length !== 1 || page.entries[0].ref !== summaryId) throw new ChatDataError('invalid_response');
    return page.entries[0].summary;
  }

  async getFullTranscript(sessionId: string): Promise<TranscriptEntry[]> {
    const snapshot = await this.readSnapshot(sessionId);
    const messages = await this.getMessagesByIds(sessionId, snapshot.items.filter(item => item.type === 'msg').map(item => item.ref));
    const entries: TranscriptEntry[] = [];
    let messageIndex = 0;
    for (const item of snapshot.items) {
      entries.push(item.type === 'msg'
        ? { ...item, message: messages[messageIndex++] }
        : { ...item, summary: await this.getSummaryById(sessionId, item.ref) });
    }
    if ((await this.readPage(sessionId, { limit: 1 })).version !== snapshot.version) throw new ChatDataError('conflict');
    return entries;
  }

  async appendAssistant(sessionId: string, input: HistoryAppend) {
    const payload = validated(appendSchema, input, 'invalid_request');
    const result = validated(appendResultSchema,
      await this.client.sessionRequest('history/append', sessionId, payload), 'invalid_response');
    if (result.version !== payload.expected_version + 1) throw new ChatDataError('invalid_response');
    return result;
  }

  async appendSummary(sessionId: string, input: HistorySummaryWrite) {
    const payload = validated(summaryWriteSchema, input, 'invalid_request');
    const result = validated(summaryResultSchema,
      await this.client.sessionRequest('history/summary/append', sessionId, payload), 'invalid_response');
    if (result.version !== payload.expected_version + 1) throw new ChatDataError('invalid_response');
    return result;
  }

  async compact(sessionId: string, input: HistoryCompaction) {
    const payload = validated(compactSchema, input, 'invalid_request');
    const result = validated(summaryResultSchema,
      await this.client.sessionRequest('history/compact', sessionId, payload), 'invalid_response');
    if (result.version !== payload.expected_version + 1) throw new ChatDataError('invalid_response');
    return result;
  }
}
