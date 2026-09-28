/**
 * LcmContext — LCM-inspired context manager composing the four internal ports.
 *
 * Phase 1: leaf-only compaction, chronological eviction, char-based token estimation.
 */
import { ContextManager, SDKMessage, AssemblyMeta, AgentTool } from '../types';
import { ContextStore, HeaderAlreadyExistsError } from '../store/port';
import { Summarizer } from '../summarize/port';
import { EvictionPolicy } from '../eviction/port';
import { TokenEstimator } from '../tokens/port';
import { LcmConfig, loadLcmConfig } from './config';
import { resolveContextItems, splitByTail, sanitizeMessages } from './assembler';
import { maybeCompact } from './compactor';
import { createExpandSummaryTool } from './expand-summary';
import { assertValidSessionId } from '../../session-id';

// Concrete implementations
import { DynamoContextStore } from '../store/dynamo-store';
import { BedrockSummarizer } from '../summarize/bedrock-summarizer';
import { ChronologicalEviction } from '../eviction/chronological';
import { CharBasedEstimator } from '../tokens/char-estimator';

export class LcmContext implements ContextManager {
  constructor(
    private readonly store: ContextStore,
    private readonly summarizer: Summarizer,
    private readonly evictor: EvictionPolicy,
    private readonly tokens: TokenEstimator,
    private readonly config: LcmConfig,
  ) {}

  async assemble(input: {
    sessionId: string;
    userMessage: string;
    tokenBudget: number;
  }): Promise<{ messages: SDKMessage[]; meta: AssemblyMeta }> {
    // Check if session exists (defensive: absent header = empty session)
    const header = await this.store.getSessionHeader(input.sessionId);
    if (!header) {
      return {
        messages: [],
        meta: { rawMessageCount: 0, summaryCount: 0, estimatedTokens: 0, compactionTriggered: false },
      };
    }

    const items = await this.store.readContextItems(input.sessionId);
    if (items.length === 0) {
      return {
        messages: [],
        meta: { rawMessageCount: 0, summaryCount: 0, estimatedTokens: 0, compactionTriggered: false },
      };
    }

    const resolved = await resolveContextItems(this.store, input.sessionId, items, this.tokens);
    const { freshTail, evictable } = splitByTail(resolved, this.config.freshTailCount);

    const tailTokens = freshTail.reduce((sum, r) => sum + r.tokens, 0);
    const userMsgTokens = this.tokens.count(input.userMessage);
    const remaining = input.tokenBudget - tailTokens - userMsgTokens;

    const kept = remaining > 0
      ? this.evictor.pick(evictable, remaining, input.userMessage)
      : [];

    const allKept = [...kept, ...freshTail];
    const messages = sanitizeMessages(allKept.map(r => r.message));

    const rawMessageCount = allKept.filter(r => r.type === 'message').length;
    const summaryCount = allKept.filter(r => r.type === 'summary').length;
    const estimatedTokens = allKept.reduce((sum, r) => sum + r.tokens, 0);

    return {
      messages,
      meta: { rawMessageCount, summaryCount, estimatedTokens, compactionTriggered: false },
    };
  }

  async record(input: {
    sessionId: string;
    userMessage: SDKMessage;
    assistantMessage: SDKMessage;
  }): Promise<void> {
    const now = new Date();
    const ts = now.toISOString();
    const ttlEpoch = Math.floor(now.getTime() / 1000) + this.config.sessionTtlSeconds;

    // One atomic write: user msg + user item + assistant msg + assistant item + header refresh
    await this.store.recordTurn({
      sessionId: input.sessionId,
      userMessage: {
        role: input.userMessage.role,
        content: input.userMessage.content,
        ts,
        tokens: this.tokens.count(input.userMessage.content),
      },
      assistantMessage: {
        role: input.assistantMessage.role,
        content: input.assistantMessage.content,
        ts,
        tokens: this.tokens.count(input.assistantMessage.content),
      },
      ttl: ttlEpoch,
      lastActivityAt: ts,
    });

    // Best-effort compaction (non-blocking on failure)
    try {
      await maybeCompact(this.store, input.sessionId, this.summarizer, this.tokens, this.config);
    } catch (err) {
      console.error(`[lcm-context] Compaction failed (non-blocking): ${(err as Error).message}`);
    }
  }

  /**
   * Idempotent ownership assertion with a race-safe create path.
   *
   * - If header exists and ownerUserId matches → no-op (+ team check).
   * - If header exists and ownerUserId differs → throw.
   * - If header does not exist → conditional put (attribute_not_exists). If two
   *   concurrent callers race, one wins; the loser re-reads and re-verifies.
   *
   * Tenant identity is mandatory. Existing headers missing it are quarantined;
   * ownership is never inferred from a matching user id alone.
   */
  async assertOwnership(sessionId: string, userId: string, tenantId?: string, identity?: {
    orgId?: string;
    teamId?: string;
    departmentId?: string;
    accountType?: string;
  }): Promise<void> {
    // #5660 (A07): `sessionId` originates from the client and becomes both a
    // store key and an artifact path segment. Refuse a bad shape before any read
    // or write, as the artifact store and sweeper already do.
    assertValidSessionId(sessionId);
    if (!tenantId) {
      console.warn(`[lcm] OWNERSHIP REFUSED session=${sessionId} caller=${userId}: missing tenant identity`);
      throw new Error(`Session tenant identity is required for ${sessionId}`);
    }

    const existing = await this.store.getSessionHeader(sessionId);
    if (existing) {
      this.verifyHeaderOwner(existing, sessionId, userId, tenantId, identity);
      return;
    }

    const now = new Date();
    try {
      await this.store.createSessionHeader({
        sessionId,
        ownerUserId: userId,
        tenantId,
        orgId: identity?.orgId,
        teamId: identity?.teamId,
        departmentId: identity?.departmentId,
        accountType: identity?.accountType,
        createdAt: now.toISOString(),
        lastActivityAt: now.toISOString(),
        status: 'active',
        ttl: Math.floor(now.getTime() / 1000) + this.config.sessionTtlSeconds,
      });
    } catch (err) {
      if (err instanceof HeaderAlreadyExistsError) {
        // We lost the race. Re-read and re-verify.
        const header = await this.store.getSessionHeader(sessionId);
        if (!header) {
          // Extremely unlikely (deleted between our race and re-read). Propagate.
          throw err;
        }
        // Same check as the existing-header path — shared so the two cannot
        // drift apart and leave the race a way around ownership.
        this.verifyHeaderOwner(header, sessionId, userId, tenantId, identity);
        return;
      }
      throw err;
    }
  }

  /**
   * Verify a stored session header belongs to the caller.
   *
   * Used by both the existing-header and race-loser paths so a concurrent create
   * cannot bypass a check the direct path applies. Refusals are logged before
   * throwing so a reviewer can see the control working.
   *
   * #5660 (A07): tenant identity is exact and mandatory. A header without it is
   * legacy data with unverified ownership and remains quarantined.
   */
  private verifyHeaderOwner(
    header: { ownerUserId: string; tenantId?: string; orgId?: string; teamId?: string },
    sessionId: string,
    userId: string,
    tenantId?: string,
    identity?: { orgId?: string; teamId?: string },
  ): void {
    // Every refusal is logged for audit before it throws. Messages keep their
    // existing wording: this is an agent-internal error, not a client response,
    // and callers (including tests) distinguish the three causes by it.
    const refuse = (message: string): never => {
      console.warn(`[lcm] OWNERSHIP REFUSED session=${sessionId} caller=${userId}: ${message}`);
      throw new Error(message);
    };

    if (header.ownerUserId !== userId) {
      refuse(
        `Session ownership mismatch: session ${sessionId} is owned by ${header.ownerUserId}, not ${userId}`,
      );
    }
    if (!header.tenantId || header.tenantId !== tenantId) {
      refuse(
        `Session tenant mismatch: session ${sessionId} belongs to tenant ${header.tenantId ?? 'unknown'}, caller is tenant ${tenantId}`,
      );
    }
    if ((header.orgId ?? '') !== (identity?.orgId ?? '')) {
      refuse(
        `Session organization mismatch: session ${sessionId} belongs to organization ${header.orgId ?? 'none'}, caller is organization ${identity?.orgId ?? 'none'}`,
      );
    }
    if ((header.teamId ?? '') !== (identity?.teamId ?? '')) {
      refuse(
        `Session team mismatch: session ${sessionId} belongs to team ${header.teamId ?? 'none'}, caller is team ${identity?.teamId ?? 'none'}`,
      );
    }
  }

  tools(): AgentTool[] {
    return [createExpandSummaryTool(this.store)];
  }
}

/**
 * Factory helper to create LcmContext from env vars.
 */
export function createLcmContext(env: Record<string, string | undefined> = process.env): LcmContext {
  const config = loadLcmConfig(env);
  const tableName = env.CONTEXT_TABLE;
  if (!tableName) throw new Error('CONTEXT_TABLE env var is required for CONTEXT_STRATEGY=lcm');

  const region = env.AWS_REGION ?? 'us-east-1';
  const store = new DynamoContextStore(tableName, region);
  const summarizer = new BedrockSummarizer(config.summaryModel, region);
  const evictor = new ChronologicalEviction();
  const tokens = new CharBasedEstimator();

  return new LcmContext(store, summarizer, evictor, tokens, config);
}
