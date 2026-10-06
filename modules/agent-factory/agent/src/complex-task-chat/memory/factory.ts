/**
 * Factory for MemoryProvider — selects implementation based on env vars.
 *
 * MEMORY_STRATEGY env var:
 *   - "null" (default): NullMemoryProvider
 *   - "dynamo": DynamoMemoryProvider (requires MEMORY_TABLE)
 */
import { MemoryProvider } from './types';
import { NullMemoryProvider } from './null-memory';
import { GatewayMemoryProvider } from './gateway-memory';
import { ChatDataClient } from '../gateway/chat-data-client';

export function buildMemoryProvider(env: Record<string, string | undefined> = process.env, gateway?: { client: ChatDataClient }): MemoryProvider {
  const strategy = env.MEMORY_STRATEGY ?? 'null';
  if (env.ADP_CHAT_DATA_ENABLED === 'true' && strategy !== 'gateway') {
    throw new Error('Scoped chat data requires MEMORY_STRATEGY=gateway');
  }

  switch (strategy) {
    case 'gateway':
      if (env.ADP_CHAT_DATA_ENABLED !== 'true' || !gateway) {
        throw new Error('Gateway memory requires enabled scoped chat data and a workload-bound client');
      }
      return new GatewayMemoryProvider(gateway.client);

    case 'null':
      return new NullMemoryProvider();

    case 'dynamo': {
      const { DynamoMemoryProvider } = require('./dynamo-memory');
      const tableName = env.MEMORY_TABLE;
      if (!tableName) throw new Error('MEMORY_TABLE env var is required for MEMORY_STRATEGY=dynamo');
      return new DynamoMemoryProvider(tableName, env.AWS_REGION ?? 'us-east-1');
    }

    default:
      throw new Error(`Unknown MEMORY_STRATEGY: ${strategy}. Valid: null, dynamo, gateway`);
  }
}
