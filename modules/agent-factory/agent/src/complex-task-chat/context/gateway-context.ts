import { createHash } from 'crypto';
import { z } from 'zod';
import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';
import { ChronologicalEviction } from './eviction/chronological';
import { resolveContextItems, sanitizeMessages, splitByTail } from './lcm/assembler';
import { maybeCompact } from './lcm/compactor';
import { DEFAULT_LCM_CONFIG, LcmConfig } from './lcm/config';
import { GatewayHistoryStore, HistoryAppend } from './store/gateway-history';
import { Summarizer } from './summarize/port';
import { CharBasedEstimator } from './tokens/char-estimator';
import { AgentTool, ContextManager } from './types';

const recordSchema = z.object({
  sessionId: z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/),
  userMessage: z.object({ role: z.literal('user'), content: z.string() }).strict(),
  assistantMessage: z.object({ role: z.literal('assistant'), content: z.string().min(1).max(65_536) }).strict(),
}).strict();

function writeKey(kind: string, ...parts: Array<string | number>): string {
  return `${kind}_${createHash('sha256').update(JSON.stringify(parts)).digest('hex')}`;
}

export class GatewayContextManager implements ContextManager {
  private readonly history: GatewayHistoryStore;
  private readonly tokens = new CharBasedEstimator();
  private readonly evictor = new ChronologicalEviction();
  private readonly config: LcmConfig;
  private snapshotVersion?: number;
  private pendingAppend?: HistoryAppend;
  private recording: Promise<void> = Promise.resolve();
  private compactionAttempted = false;

  constructor(
    private readonly client: ChatDataClient,
    private readonly summarizer: Summarizer,
    config: LcmConfig = DEFAULT_LCM_CONFIG,
  ) {
    if ([config.freshTailCount, config.leafChunkTokens, config.leafTargetTokens, config.maxTurnsPerCompaction]
      .some(value => !Number.isSafeInteger(value) || value < 1)) throw new ChatDataError('invalid_request');
    this.config = { ...config };
    this.history = new GatewayHistoryStore(client);
  }

  async assertOwnership(sessionId: string, _userId: string, _tenantId?: string, _identity?: {
    orgId?: string; teamId?: string; departmentId?: string; accountType?: string;
  }): Promise<void> {
    await this.history.readPage(sessionId, { limit: 1 });
  }

  async assemble(input: Parameters<ContextManager['assemble']>[0]) {
    if (!Number.isSafeInteger(input.tokenBudget) || input.tokenBudget < 0 || typeof input.userMessage !== 'string') {
      throw new ChatDataError('invalid_request');
    }
    const accepted = await this.history.acceptedUserTurn(input.sessionId);
    const snapshot = await this.history.readSnapshot(input.sessionId);
    const priorItems = snapshot.items.filter(item => item.ordinal < accepted.ordinal);
    const resolved = await resolveContextItems(this.history, input.sessionId, priorItems, this.tokens);
    if ((await this.history.readPage(input.sessionId, { limit: 1 })).version !== snapshot.version) throw new ChatDataError('conflict');
    this.snapshotVersion = snapshot.version;
    const { freshTail, evictable } = splitByTail(resolved, this.config.freshTailCount);
    const remaining = input.tokenBudget - this.tokens.count(input.userMessage) - freshTail.reduce((sum, item) => sum + item.tokens, 0);
    const kept = remaining > 0 ? this.evictor.pick(evictable, remaining, input.userMessage) : [];
    const allKept = [...kept, ...freshTail];
    return {
      messages: sanitizeMessages(allKept.map(item => item.message)),
      meta: {
        rawMessageCount: allKept.filter(item => item.type === 'message').length,
        summaryCount: allKept.filter(item => item.type === 'summary').length,
        estimatedTokens: allKept.reduce((sum, item) => sum + item.tokens, 0),
        compactionTriggered: false,
      },
    };
  }

  record(input: Parameters<ContextManager['record']>[0]): Promise<void> {
    const parsed = recordSchema.safeParse(input);
    if (!parsed.success || Buffer.byteLength(parsed.data.assistantMessage.content) > 131_072) {
      return Promise.reject(new ChatDataError('invalid_request'));
    }
    const operation = this.recording.then(() => this.recordAssistant(parsed.data));
    this.recording = operation.catch(() => undefined);
    return operation;
  }

  private async recordAssistant(input: z.infer<typeof recordSchema>): Promise<void> {
    const scope = await this.client.sessionScope();
    if (scope.session_id !== input.sessionId) throw new ChatDataError('scope_mismatch');
    if (!this.pendingAppend) {
      const accepted = await this.history.acceptedUserTurn(input.sessionId);
      this.pendingAppend = {
        user_turn_id: accepted.message_id,
        idempotency_key: writeKey('assistant', scope.run_id, scope.session_id),
        expected_version: this.snapshotVersion ?? (await this.history.readPage(input.sessionId, { limit: 1 })).version,
        content: input.assistantMessage.content,
        tokens: this.tokens.count(input.assistantMessage.content),
      };
    }
    if (this.pendingAppend.content !== input.assistantMessage.content) throw new ChatDataError('conflict');
    await this.history.appendAssistant(input.sessionId, this.pendingAppend);
    if (this.compactionAttempted) return;
    this.compactionAttempted = true;
    try {
      await this.compact(input.sessionId, scope.run_id);
    } catch {
      console.warn('[gateway-context] Assistant recorded; compaction unavailable');
    }
  }

  private async compact(sessionId: string, runId: string): Promise<void> {
    let snapshotVersion: number | undefined;
    await maybeCompact({
      readContextItems: async requestedSession => {
        const snapshot = await this.history.readSnapshot(requestedSession);
        snapshotVersion = snapshot.version;
        return snapshot.items;
      },
      getMessagesByIds: (requestedSession, ids) => this.history.getMessagesByIds(requestedSession, ids),
      replaceRangeWithSummary: async (requestedSession, fromOrdinal, toOrdinal, summary) => {
        if (snapshotVersion === undefined) throw new ChatDataError('conflict');
        const result = await this.history.compact(requestedSession, {
          idempotency_key: writeKey('compact', runId, requestedSession, snapshotVersion, fromOrdinal, toOrdinal),
          expected_version: snapshotVersion,
          from_ordinal: fromOrdinal, to_ordinal: toOrdinal,
          content: summary.content, tokens: summary.tokens, source_ids: summary.sourceIds, parent_ids: summary.parentIds ?? [],
        });
        return result.summary_id;
      },
    }, sessionId, this.summarizer, this.tokens, this.config, () => undefined, 94);
  }

  tools(): AgentTool[] {
    return [{
      name: 'expand_summary',
      description: 'Retrieve the original messages behind a summary in the current authorized session.',
      inputSchema: { summary_id: z.string().regex(/^[A-Za-z0-9_.:-]{1,256}$/) },
      handler: async input => {
        const parsed = z.object({ summary_id: z.string().regex(/^[A-Za-z0-9_.:-]{1,256}$/) }).strict().safeParse(input);
        if (!parsed.success) throw new ChatDataError('invalid_request');
        const scope = await this.client.sessionScope();
        const summary = await this.history.getSummaryById(scope.session_id, parsed.data.summary_id);
        const messages = await this.history.getMessagesByIds(scope.session_id, summary.sourceIds);
        const text = [
          `--- Expanded summary ${parsed.data.summary_id} ---`,
          `Time range: ${summary.earliestAt} to ${summary.latestAt}`,
          `Source messages: ${messages.length}`, '',
          ...messages.flatMap(message => [`[${message.ts}] ${message.role}:`, message.content, '']),
          '--- End expanded summary ---',
        ].join('\n');
        return { content: [{ type: 'text', text }] };
      },
    }];
  }
}
