/**
 * Factory for ContextManager — selects implementation based on env vars.
 *
 * CONTEXT_STRATEGY env var:
 *   - "noop" (default): NoopContextManager
 *   - "lcm": LcmContext (requires CONTEXT_TABLE + Bedrock access)
 */
import { ContextManager } from './types';
import { NoopContextManager } from './noop-context';
import { ChatDataClient } from '../gateway/chat-data-client';
import { GatewayContextManager } from './gateway-context';
import { loadLcmConfig } from './lcm/config';
import { Summarizer } from './summarize/port';

export function buildContextManager(
  env: Record<string, string | undefined> = process.env,
  gateway?: { client: ChatDataClient; summarizer: Summarizer },
): ContextManager {
  const strategy = env.CONTEXT_STRATEGY ?? 'noop';
  if (env.ADP_CHAT_DATA_ENABLED === 'true' && strategy !== 'gateway') {
    throw new Error('Scoped chat data requires CONTEXT_STRATEGY=gateway');
  }

  switch (strategy) {
    case 'gateway':
      if (env.ADP_CHAT_DATA_ENABLED !== 'true' || !gateway?.client || !gateway.summarizer) {
        throw new Error('Gateway context requires enabled scoped chat data, a workload-bound client and a summarizer');
      }
      return new GatewayContextManager(gateway.client, gateway.summarizer, loadLcmConfig(env));

    case 'noop':
      return new NoopContextManager();

    case 'lcm': {
      // Lazy import to avoid pulling DDB/Bedrock deps when not needed
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const { createLcmContext } = require('./lcm/lcm-context');
      return createLcmContext(env);
    }

    default:
      throw new Error(`Unknown CONTEXT_STRATEGY: ${strategy}. Valid: noop, lcm, gateway`);
  }
}
