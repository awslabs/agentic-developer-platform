/**
 * Compactor — leaf-only summarization for Phase 1.
 *
 * After recording a new turn, checks if the oldest contiguous messages
 * exceed the chunk threshold and triggers summarization.
 */
import { ContextStore, ContextItem, StoredMessage } from '../store/port';
import { Summarizer } from '../summarize/port';
import { TokenEstimator } from '../tokens/port';
import { LcmConfig } from './config';

export interface CompactionResult {
  triggered: boolean;
  summaryId?: string;
  compactedCount?: number;
}

/**
 * Run leaf compaction on a session if the oldest contiguous messages
 * exceed leafChunkTokens.
 */
export async function maybeCompact(
  store: ContextStore,
  sessionId: string,
  summarizer: Summarizer,
  tokens: TokenEstimator,
  config: LcmConfig,
  log: (msg: string) => void = console.log,
): Promise<CompactionResult> {
  const items = await store.readContextItems(sessionId);
  if (items.length === 0) return { triggered: false };

  // Find oldest contiguous raw messages outside the fresh tail
  const candidate = findCompactionCandidate(items, config.freshTailCount);
  if (!candidate || candidate.length === 0) return { triggered: false };

  // Prefer token counts joined from the item row (written in recordTurn).
  // Fallback: fetch the backing messages once and use their real stored tokens.
  const missingTokens = candidate.some(it => typeof it.tokens !== 'number');
  let messagesById: Map<string, StoredMessage> | null = null;
  if (missingTokens) {
    const ids = candidate.map(i => i.ref);
    const msgs = await store.getMessagesByIds(sessionId, ids);
    messagesById = new Map<string, StoredMessage>();
    for (let i = 0; i < ids.length && i < msgs.length; i++) {
      messagesById.set(ids[i], msgs[i]);
    }
  }

  const tokenFor = (it: ContextItem): number => {
    if (typeof it.tokens === 'number') return it.tokens;
    const m = messagesById?.get(it.ref);
    return m ? m.tokens : 0;
  };

  const totalTokens = candidate.reduce((sum, it) => sum + tokenFor(it), 0);
  if (totalTokens < config.leafChunkTokens) return { triggered: false };

  log(`[compactor] Compacting ${candidate.length} items (${totalTokens} tokens) for session ${sessionId}`);

  // Fetch the actual messages for summarization text
  const msgIds = candidate.map(i => i.ref);
  const messages = messagesById
    ? msgIds.map(id => messagesById!.get(id)).filter((m): m is StoredMessage => !!m)
    : await store.getMessagesByIds(sessionId, msgIds);

  const text = concatenateWithTimestamps(messages);

  // 3-level escalation
  let summary: string;
  try {
    summary = await summarizer.summarize({
      text,
      mode: 'normal',
      targetTokens: config.leafTargetTokens,
    });

    if (tokens.count(summary) >= totalTokens) {
      log('[compactor] Normal summary too large, trying aggressive');
      summary = await summarizer.summarize({
        text,
        mode: 'aggressive',
        targetTokens: Math.floor(config.leafTargetTokens / 2),
      });
    }

    if (tokens.count(summary) >= totalTokens) {
      log('[compactor] Aggressive summary too large, using deterministic truncate');
      summary = deterministicTruncate(text, 512);
    }
  } catch (err) {
    log(`[compactor] Summarization failed, skipping compaction: ${(err as Error).message}`);
    return { triggered: false };
  }

  const firstMsg = messages[0];
  const lastMsg = messages[messages.length - 1];
  const fromOrd = candidate[0].ordinal;
  const toOrd = candidate[candidate.length - 1].ordinal;

  // Atomic: write the summary + replacement item row in one transaction.
  const summaryId = await store.replaceRangeWithSummary(sessionId, fromOrd, toOrd, {
    depth: 0,
    kind: 'leaf',
    content: summary,
    sourceIds: msgIds,
    earliestAt: firstMsg?.ts ?? new Date().toISOString(),
    latestAt: lastMsg?.ts ?? new Date().toISOString(),
    tokens: tokens.count(summary),
  });

  log(`[compactor] Created summary ${summaryId} replacing ordinals ${fromOrd}-${toOrd}`);
  return { triggered: true, summaryId, compactedCount: candidate.length };
}

/**
 * Find the oldest contiguous raw messages that are outside the fresh tail.
 */
function findCompactionCandidate(
  items: ContextItem[],
  freshTailCount: number,
): ContextItem[] {
  // Count raw messages from the end to locate the tail boundary
  let rawCount = 0;
  let tailStart = items.length;
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].type === 'msg') {
      rawCount++;
      if (rawCount >= freshTailCount) {
        tailStart = i;
        break;
      }
    }
  }

  // Collect the contiguous msg block from the start, stopping at the first summary
  // (we only compact contiguous messages; summaries interrupt the run).
  const candidate: ContextItem[] = [];
  for (let i = 0; i < tailStart; i++) {
    const it = items[i];
    if (it.type === 'msg') {
      candidate.push(it);
    } else if (candidate.length > 0) {
      break;
    }
  }

  return candidate;
}

function concatenateWithTimestamps(messages: StoredMessage[]): string {
  return messages
    .map(m => `[${m.ts}] ${m.role}: ${m.content}`)
    .join('\n\n');
}

function deterministicTruncate(text: string, targetTokens: number): string {
  const targetChars = targetTokens * 4;
  if (text.length <= targetChars) return text;

  const halfTarget = Math.floor(targetChars / 2);
  const head = text.slice(0, halfTarget);
  const tail = text.slice(-halfTarget);
  return `${head}\n\n[... truncated ...]\n\n${tail}`;
}
