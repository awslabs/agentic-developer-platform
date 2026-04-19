/**
 * Assembler — converts stored context items into SDK messages for prompt injection.
 *
 * Resolves item references (msg or sum) into ResolvedItems with message content
 * and token estimates, ready for eviction and prompt building.
 */
import { SDKMessage, ResolvedItem } from '../types';
import { ContextStore, ContextItem, StoredMessage, StoredSummary } from '../store/port';
import { TokenEstimator } from '../tokens/port';
import { formatSummaryXml } from './summary-format';

export interface AssembleResult {
  resolved: ResolvedItem[];
}

export async function resolveContextItems(
  store: ContextStore,
  sessionId: string,
  items: ContextItem[],
  tokens: TokenEstimator,
): Promise<ResolvedItem[]> {
  const resolved: ResolvedItem[] = [];

  // Batch-collect message and summary IDs
  const msgIds: string[] = [];
  const sumIds: string[] = [];

  for (const item of items) {
    if (item.type === 'msg') msgIds.push(item.ref);
    else if (item.type === 'sum') sumIds.push(item.ref);
  }

  // Batch fetch messages
  const msgMap = new Map<string, StoredMessage>();
  if (msgIds.length > 0) {
    const messages = await store.getMessagesByIds(sessionId, msgIds);
    // Map by position since getMessagesByIds returns in order of input
    for (let i = 0; i < msgIds.length && i < messages.length; i++) {
      msgMap.set(msgIds[i], messages[i]);
    }
  }

  // Fetch summaries (individually since we need IDs)
  const sumMap = new Map<string, StoredSummary>();
  for (const sumId of sumIds) {
    const sum = await store.getSummaryById(sessionId, sumId);
    if (sum) sumMap.set(sumId, sum);
  }

  // Build resolved items
  for (const item of items) {
    if (item.type === 'msg') {
      const msg = msgMap.get(item.ref);
      if (!msg) continue;
      const message: SDKMessage = { role: msg.role, content: msg.content };
      resolved.push({
        ordinal: item.ordinal,
        message,
        tokens: msg.tokens || tokens.count(msg.content),
        type: 'message',
        id: item.ref,
      });
    } else if (item.type === 'sum') {
      const sum = sumMap.get(item.ref);
      if (!sum) continue;
      const xml = formatSummaryXml({
        summaryId: item.ref,
        kind: sum.kind,
        depth: sum.depth,
        earliestAt: sum.earliestAt,
        latestAt: sum.latestAt,
        content: sum.content,
      });
      const message: SDKMessage = { role: 'user', content: xml };
      resolved.push({
        ordinal: item.ordinal,
        message,
        tokens: sum.tokens || tokens.count(xml),
        type: 'summary',
        id: item.ref,
        summaryId: item.ref,
      });
    }
  }

  return resolved;
}

/**
 * Split resolved items into fresh tail (protected from eviction) and evictable.
 * The tail is the last N raw messages. Summaries in the tail ARE evictable.
 */
export function splitByTail(
  resolved: ResolvedItem[],
  freshTailCount: number,
): { freshTail: ResolvedItem[]; evictable: ResolvedItem[] } {
  if (resolved.length === 0) return { freshTail: [], evictable: [] };

  // Count raw messages from the end
  let tailStart = resolved.length;
  let rawCount = 0;
  for (let i = resolved.length - 1; i >= 0; i--) {
    if (resolved[i].type === 'message') {
      rawCount++;
      if (rawCount >= freshTailCount) {
        tailStart = i;
        break;
      }
    }
    if (i === 0) tailStart = 0;
  }

  const freshTail = resolved.slice(tailStart);
  const evictable = resolved.slice(0, tailStart);

  return { freshTail, evictable };
}

/**
 * Sanitize tool-use pairing: ensure every tool_result has its tool_use.
 * Drop orphans. This is a safety net for when eviction splits a pair.
 */
export function sanitizeMessages(messages: SDKMessage[]): SDKMessage[] {
  // For Phase 1, we don't have tool_use/tool_result in the SDK message format.
  // Just pass through; this will be enhanced when tool tracking is added.
  return messages;
}
