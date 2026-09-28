/**
 * Integration-ish tests for LcmContext against an in-memory ContextStore fake.
 *
 * Covers the gaps the code reviewer flagged:
 *   - record() → assemble() round-trip preserves turn order.
 *   - record() performs a single atomic recordTurn() call (not three separate writes).
 *   - maybeCompact triggers replaceRangeWithSummary when chunk threshold is met.
 *   - assertOwnership uses createSessionHeader (conditional put) on first access,
 *     and is a no-op when the same user re-enters.
 *   - assertOwnership rejects a different user.
 */
import { LcmContext } from './lcm-context';
import { LcmConfig } from './config';
import {
  ContextStore,
  ContextItem,
  SessionHeader,
  StoredMessage,
  StoredSummary,
  HeaderAlreadyExistsError,
} from '../store/port';
import { Summarizer } from '../summarize/port';
import { EvictionPolicy } from '../eviction/port';
import { TokenEstimator } from '../tokens/port';
import { ResolvedItem } from '../types';

// ─── In-memory fake store ───────────────────────────────────────────────────

interface RecordedCall {
  op: 'recordTurn' | 'createSessionHeader' | 'refreshSessionHeader' | 'replaceRangeWithSummary';
  args: unknown;
}

class InMemoryStore implements ContextStore {
  public calls: RecordedCall[] = [];
  private messages = new Map<string, Map<string, StoredMessage>>();
  private summaries = new Map<string, Map<string, StoredSummary>>();
  private items = new Map<string, ContextItem[]>();
  private headers = new Map<string, SessionHeader>();
  private ordCounter = new Map<string, number>();

  async recordTurn(input: {
    sessionId: string;
    userMessage: StoredMessage;
    assistantMessage: StoredMessage;
    ttl: number;
    lastActivityAt: string;
  }) {
    this.calls.push({ op: 'recordTurn', args: input });

    const header = this.headers.get(input.sessionId);
    if (!header) throw new Error('header missing — recordTurn before createSessionHeader');

    const ordStart = this.ordCounter.get(input.sessionId) ?? 0;
    const userOrdinal = ordStart;
    const assistantOrdinal = ordStart + 1;
    const userMessageId = `msg_u_${userOrdinal}`;
    const assistantMessageId = `msg_a_${assistantOrdinal}`;

    const msgs = this.messages.get(input.sessionId) ?? new Map();
    msgs.set(userMessageId, input.userMessage);
    msgs.set(assistantMessageId, input.assistantMessage);
    this.messages.set(input.sessionId, msgs);

    const items = this.items.get(input.sessionId) ?? [];
    items.push({ ordinal: userOrdinal, type: 'msg', ref: userMessageId, tokens: input.userMessage.tokens });
    items.push({ ordinal: assistantOrdinal, type: 'msg', ref: assistantMessageId, tokens: input.assistantMessage.tokens });
    this.items.set(input.sessionId, items);

    this.ordCounter.set(input.sessionId, assistantOrdinal + 1);

    header.lastActivityAt = input.lastActivityAt;
    header.ttl = input.ttl;

    return { userMessageId, assistantMessageId, userOrdinal, assistantOrdinal };
  }

  async appendSummary(sessionId: string, sum: StoredSummary): Promise<string> {
    const summaryId = `sum_${sessionId}_${(this.summaries.get(sessionId)?.size ?? 0).toString(16)}`;
    const sums = this.summaries.get(sessionId) ?? new Map();
    sums.set(summaryId, sum);
    this.summaries.set(sessionId, sums);
    return summaryId;
  }

  async readContextItems(sessionId: string): Promise<ContextItem[]> {
    return [...(this.items.get(sessionId) ?? [])].sort((a, b) => a.ordinal - b.ordinal);
  }

  async replaceRangeWithSummary(
    sessionId: string,
    fromOrd: number,
    toOrd: number,
    sum: StoredSummary,
  ): Promise<string> {
    this.calls.push({ op: 'replaceRangeWithSummary', args: { sessionId, fromOrd, toOrd, sum } });

    const summaryId = `sum_${sessionId}_${((this.summaries.get(sessionId)?.size ?? 0) + 1).toString(16)}`;
    const sums = this.summaries.get(sessionId) ?? new Map();
    sums.set(summaryId, sum);
    this.summaries.set(sessionId, sums);

    const items = (this.items.get(sessionId) ?? []).filter(
      it => it.ordinal < fromOrd || it.ordinal > toOrd,
    );
    items.push({ ordinal: fromOrd, type: 'sum', ref: summaryId, tokens: sum.tokens });
    items.sort((a, b) => a.ordinal - b.ordinal);
    this.items.set(sessionId, items);

    return summaryId;
  }

  async getMessagesByIds(sessionId: string, ids: string[]): Promise<StoredMessage[]> {
    const src = this.messages.get(sessionId) ?? new Map();
    return ids.map(id => src.get(id)).filter((m): m is StoredMessage => !!m);
  }

  async getSummaryById(sessionId: string, summaryId: string): Promise<StoredSummary | null> {
    return this.summaries.get(sessionId)?.get(summaryId) ?? null;
  }

  async getSessionHeader(sessionId: string): Promise<SessionHeader | null> {
    return this.headers.get(sessionId) ?? null;
  }

  async createSessionHeader(
    header: Omit<SessionHeader, 'createdAt'> & { createdAt?: string },
  ): Promise<void> {
    this.calls.push({ op: 'createSessionHeader', args: header });
    if (this.headers.has(header.sessionId)) {
      throw new HeaderAlreadyExistsError(header.sessionId);
    }
    this.headers.set(header.sessionId, {
      sessionId: header.sessionId,
      ownerUserId: header.ownerUserId,
      tenantId: header.tenantId,
      orgId: header.orgId,
      teamId: header.teamId,
      departmentId: header.departmentId,
      accountType: header.accountType,
      createdAt: header.createdAt ?? new Date().toISOString(),
      lastActivityAt: header.lastActivityAt,
      status: header.status,
      ttl: header.ttl,
    });
  }

  async refreshSessionHeader(sessionId: string, lastActivityAt: string, ttl: number): Promise<void> {
    this.calls.push({ op: 'refreshSessionHeader', args: { sessionId, lastActivityAt, ttl } });
    const h = this.headers.get(sessionId);
    if (!h) throw new Error('refreshSessionHeader without prior create');
    h.lastActivityAt = lastActivityAt;
    h.ttl = ttl;
  }
}

// ─── Stub collaborators ─────────────────────────────────────────────────────

const fixedSummarizer: Summarizer = {
  async summarize({ text }): Promise<string> {
    // Deterministic short summary — ~20 tokens when char-estimated at 4 chars/token
    return `[SUMMARY] ${text.slice(0, 40)}...`;
  },
};

const charEstimator: TokenEstimator = {
  count: (text: string) => Math.ceil(text.length / 4),
};

const chronoEvictor: EvictionPolicy = {
  pick: (evictable: ResolvedItem[], budget: number): ResolvedItem[] => {
    // keep newest, drop oldest
    const kept: ResolvedItem[] = [];
    let used = 0;
    for (let i = evictable.length - 1; i >= 0; i--) {
      const item = evictable[i];
      if (used + item.tokens > budget) break;
      kept.unshift(item);
      used += item.tokens;
    }
    return kept;
  },
};

const testConfig: LcmConfig = {
  freshTailCount: 2,
  leafChunkTokens: 20, // small so we can trigger compaction cheaply
  leafTargetTokens: 10,
  summaryModel: 'test-model',
  summaryEndpoint: 'bedrock',
  summaryTimeoutMs: 60_000,
  maxTurnsPerCompaction: 1,
  incrementalMaxDepth: 0,
  leafMinFanout: 8,
  condensedMinFanout: 4,
  condensedTargetTokens: 2000,
  sessionTtlSeconds: 90 * 86400,
};

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('LcmContext integration (in-memory store)', () => {
  function makeCtx() {
    const store = new InMemoryStore();
    const ctx = new LcmContext(store, fixedSummarizer, chronoEvictor, charEstimator, testConfig);
    return { ctx, store };
  }

  it('record() uses a single atomic recordTurn (not three writes)', async () => {
    const { ctx, store } = makeCtx();

    await ctx.assertOwnership('s1', 'user-1');
    store.calls.length = 0;

    await ctx.record({
      sessionId: 's1',
      userMessage: { role: 'user', content: 'hello' },
      assistantMessage: { role: 'assistant', content: 'hi there' },
    });

    const writes = store.calls.filter(c => c.op === 'recordTurn');
    expect(writes).toHaveLength(1);
    // And no separate header upsert — recordTurn is the atomic unit.
    expect(store.calls.find(c => c.op === 'refreshSessionHeader')).toBeUndefined();
  });

  it('round-trip: record → assemble returns the turns in order', async () => {
    const { ctx } = makeCtx();

    await ctx.assertOwnership('s1', 'user-1');
    await ctx.record({
      sessionId: 's1',
      userMessage: { role: 'user', content: 'first question' },
      assistantMessage: { role: 'assistant', content: 'first answer' },
    });
    await ctx.record({
      sessionId: 's1',
      userMessage: { role: 'user', content: 'second question' },
      assistantMessage: { role: 'assistant', content: 'second answer' },
    });

    const { messages, meta } = await ctx.assemble({
      sessionId: 's1',
      userMessage: 'third turn',
      tokenBudget: 10_000,
    });

    // 4 messages (2 turns × user+assistant). Fresh tail count is 2 here; at
    // budget 10k everything fits anyway so we see them all.
    expect(messages).toHaveLength(4);
    expect(messages[0]).toEqual({ role: 'user', content: 'first question' });
    expect(messages[1]).toEqual({ role: 'assistant', content: 'first answer' });
    expect(messages[2]).toEqual({ role: 'user', content: 'second question' });
    expect(messages[3]).toEqual({ role: 'assistant', content: 'second answer' });
    expect(meta.rawMessageCount).toBe(4);
  });

  it('maybeCompact triggers replaceRangeWithSummary when chunk threshold is crossed', async () => {
    const { ctx, store } = makeCtx();
    await ctx.assertOwnership('s1', 'user-1');

    // Enough turns to push ~40 raw tokens outside the 2-message fresh tail.
    // Each content string here is ~40 chars → ~10 tokens. 6 messages = ~60
    // tokens, 4 outside the tail = ~40 tokens, easily over leafChunkTokens=20.
    const big = 'aaaaaaaaaa bbbbbbbbbb cccccccccc dddddddddd';
    for (let i = 0; i < 3; i++) {
      await ctx.record({
        sessionId: 's1',
        userMessage: { role: 'user', content: big },
        assistantMessage: { role: 'assistant', content: big },
      });
    }

    const replaced = store.calls.filter(c => c.op === 'replaceRangeWithSummary');
    expect(replaced.length).toBeGreaterThanOrEqual(1);

    const items = await store.readContextItems('s1');
    // Should contain at least one summary entry.
    expect(items.some(i => i.type === 'sum')).toBe(true);
  });

  it('assertOwnership uses createSessionHeader on first access; idempotent on re-entry', async () => {
    const { ctx, store } = makeCtx();

    await ctx.assertOwnership('s1', 'user-1', 'acme');
    const createCalls = store.calls.filter(c => c.op === 'createSessionHeader');
    expect(createCalls).toHaveLength(1);
    expect((createCalls[0].args as { ownerUserId: string; tenantId?: string }).ownerUserId).toBe('user-1');
    expect((createCalls[0].args as { tenantId?: string }).tenantId).toBe('acme');

    // Re-entry with the same user: no additional createSessionHeader call.
    store.calls.length = 0;
    await ctx.assertOwnership('s1', 'user-1');
    expect(store.calls.filter(c => c.op === 'createSessionHeader')).toHaveLength(0);
  });

  it('assertOwnership rejects a different user on an existing session', async () => {
    const { ctx } = makeCtx();
    await ctx.assertOwnership('s1', 'user-1');

    await expect(ctx.assertOwnership('s1', 'user-2')).rejects.toThrow(/ownership mismatch/);
  });

  // Stage A (#184): team-aware ownership tests

  it('assertOwnership rejects cross-team access when both sides have teamId', async () => {
    const { ctx } = makeCtx();
    // Create session with team-alpha
    await ctx.assertOwnership('s-team', 'user-1', 'acme', {
      orgId: 'org-1',
      teamId: 'team-alpha',
    });

    // Same user, different team → reject
    await expect(
      ctx.assertOwnership('s-team', 'user-1', 'acme', {
        orgId: 'org-1',
        teamId: 'team-beta',
      }),
    ).rejects.toThrow(/team mismatch/);
  });

  it('assertOwnership allows when caller has no teamId (legacy compat)', async () => {
    const { ctx } = makeCtx();
    // Create session with team
    await ctx.assertOwnership('s-legacy1', 'user-1', 'acme', {
      orgId: 'org-1',
      teamId: 'team-alpha',
    });

    // Same user, no team → allow (legacy single-tenant mode)
    await expect(
      ctx.assertOwnership('s-legacy1', 'user-1', 'acme'),
    ).resolves.toBeUndefined();
  });

  it('assertOwnership allows when existing session has no teamId (legacy compat)', async () => {
    const { ctx } = makeCtx();
    // Create session without team (legacy)
    await ctx.assertOwnership('s-legacy2', 'user-1', 'acme');

    // Same user with team → allow (existing session is pre-multi-tenant)
    await expect(
      ctx.assertOwnership('s-legacy2', 'user-1', 'acme', {
        orgId: 'org-1',
        teamId: 'team-alpha',
      }),
    ).resolves.toBeUndefined();
  });

  it('assertOwnership allows same team access', async () => {
    const { ctx } = makeCtx();
    await ctx.assertOwnership('s-same-team', 'user-1', 'acme', {
      orgId: 'org-1',
      teamId: 'team-alpha',
    });

    // Same user, same team → allow
    await expect(
      ctx.assertOwnership('s-same-team', 'user-1', 'acme', {
        orgId: 'org-1',
        teamId: 'team-alpha',
      }),
    ).resolves.toBeUndefined();
  });

  it('assertOwnership stores identity claims in header on first access', async () => {
    const { ctx, store } = makeCtx();
    await ctx.assertOwnership('s-claims', 'user-1', 'acme', {
      orgId: 'org-1',
      teamId: 'team-alpha',
      departmentId: 'eng',
      accountType: 'human',
    });

    const header = await store.getSessionHeader('s-claims');
    expect(header).not.toBeNull();
    expect(header!.orgId).toBe('org-1');
    expect(header!.teamId).toBe('team-alpha');
    expect(header!.departmentId).toBe('eng');
    expect(header!.accountType).toBe('human');
  });
});
