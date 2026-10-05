import { randomUUID } from 'crypto';
import { z } from 'zod';
import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';
import { DraftStore, IntentDraft } from './port';

const text = z.string().max(2000);
const draftSchema = z.object({
  intent: text.optional(),
  motivation: text.optional(),
  outcomes: z.array(text).max(20).optional(),
  constraints: z.array(text).max(20).optional(),
  openQuestions: z.array(text).max(20).optional(),
  waveDisplay: z.object({ title: z.string().trim().min(1).max(120), description: z.string().trim().min(1).max(500) }).strict().optional(),
  epicDisplay: z.object({ title: z.string().trim().min(1).max(200), description: z.string().trim().min(1).max(3000) }).strict().optional(),
}).strict();
const storedDraftSchema = draftSchema.extend({ updatedAt: z.iso.datetime({ offset: true }) });
const versionSchema = z.number().int().min(0).max(99_999_999);
const writeResultSchema = z.object({ draft: storedDraftSchema, version: versionSchema.min(1) });
const readResultSchema = z.object({
  status: z.enum(['ok', 'empty']),
  entries: z.array(z.object({ draft: storedDraftSchema })).max(1),
  version: versionSchema,
  observed_at: z.iso.datetime({ offset: true }),
  next_cursor: z.null(),
  coverage: z.object({ source: z.literal('session_draft'), complete: z.literal(true), missing_source_ids: z.array(z.string()).length(0) }),
}).refine(result => result.status === 'empty' ? result.entries.length === 0 && result.version === 0 : result.entries.length === 1);

export class GatewayDraftStore implements DraftStore {
  #version?: number;
  #pending?: { content: string; body: Record<string, unknown> };
  #queue: Promise<unknown> = Promise.resolve();

  constructor(private readonly client: ChatDataClient, private readonly sessionId: string) {}

  #serialized<Result>(sessionId: string, action: () => Promise<Result>): Promise<Result> {
    if (sessionId !== this.sessionId) return Promise.reject(new ChatDataError('scope_mismatch'));
    const result = this.#queue.then(action);
    this.#queue = result.catch(() => undefined);
    return result;
  }

  async #read(): Promise<IntentDraft | null> {
    const parsed = readResultSchema.safeParse(await this.client.sessionRequest('draft/read', this.sessionId));
    if (!parsed.success) throw new ChatDataError('invalid_response');
    this.#version = parsed.data.version;
    this.#pending = undefined;
    return parsed.data.entries[0]?.draft ?? null;
  }

  get(sessionId: string): Promise<IntentDraft | null> {
    return this.#serialized(sessionId, () => this.#read());
  }

  put(sessionId: string, draft: IntentDraft): Promise<IntentDraft> {
    const { updatedAt, ...input } = draft;
    const parsed = draftSchema.safeParse(input);
    if (!parsed.success) return Promise.reject(new ChatDataError('invalid_request'));
    const content = JSON.stringify(parsed.data);
    if (Buffer.byteLength(content) > 131_072) return Promise.reject(new ChatDataError('invalid_request'));
    return this.#serialized(sessionId, async () => {
      if (this.#version === undefined) await this.#read();
      if (this.#pending?.content !== content) {
        this.#pending = {
          content,
          body: { draft: parsed.data, expected_version: this.#version, idempotency_key: randomUUID() },
        };
      }
      const result = writeResultSchema.safeParse(await this.client.sessionRequest('draft/write', sessionId, this.#pending.body));
      if (!result.success || result.data.version !== Number(this.#pending.body.expected_version) + 1) {
        throw new ChatDataError('invalid_response');
      }
      this.#version = result.data.version;
      return result.data.draft;
    });
  }
}
