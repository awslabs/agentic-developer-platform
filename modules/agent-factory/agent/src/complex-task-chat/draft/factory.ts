/**
 * Factory for DraftStore — selects implementation based on env vars (#6932).
 *
 * There is no DRAFT_STRATEGY: the draft row has always ridden the chat-context
 * table, so the store follows the scoped-data switch instead.
 *
 *   - ADP_CHAT_DATA_ENABLED=true: GatewayDraftStore over the workload-bound
 *     ChatDataClient. Requires the injected client + session; no fallback to
 *     the direct table when the scoped adapter is missing.
 *   - otherwise: the pre-existing dynamo builder (CONTEXT_TABLE => DynamoDraftStore,
 *     no table => NoopDraftStore), byte-for-byte what the worker did before.
 */
import { ChatDataClient } from '../gateway/chat-data-client';
import { buildDraftStore as buildDirectDraftStore } from './dynamo-draft-store';
import { GatewayDraftStore } from './gateway-draft-store';
import { DraftStore } from './port';

export function buildDraftStore(
  env: Record<string, string | undefined> = process.env,
  gateway?: { client: ChatDataClient; sessionId: string },
  ttl?: number,
): DraftStore {
  if (env.ADP_CHAT_DATA_ENABLED === 'true') {
    if (!gateway?.client || !gateway.sessionId) {
      throw new Error('Gateway drafts require enabled scoped chat data, a workload-bound client and a session');
    }
    return new GatewayDraftStore(gateway.client, gateway.sessionId);
  }
  return buildDirectDraftStore(env, ttl);
}
