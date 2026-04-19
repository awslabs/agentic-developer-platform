/**
 * Factory for MemoryProvider — selects implementation based on env vars.
 *
 * MEMORY_STRATEGY env var:
 *   - "null" (default): NullMemoryProvider
 *   - "dynamo": DynamoMemoryProvider (requires MEMORY_TABLE)
 */
import { MemoryProvider } from './types';
import { NullMemoryProvider } from './null-memory';

export function buildMemoryProvider(env: Record<string, string | undefined> = process.env): MemoryProvider {
  const strategy = env.MEMORY_STRATEGY ?? 'null';

  switch (strategy) {
    case 'null':
      return new NullMemoryProvider();

    case 'dynamo': {
      const { DynamoMemoryProvider } = require('./dynamo-memory');
      const tableName = env.MEMORY_TABLE;
      if (!tableName) throw new Error('MEMORY_TABLE env var is required for MEMORY_STRATEGY=dynamo');
      return new DynamoMemoryProvider(tableName, env.AWS_REGION ?? 'us-east-1');
    }

    default:
      throw new Error(`Unknown MEMORY_STRATEGY: ${strategy}. Valid: null, dynamo`);
  }
}
