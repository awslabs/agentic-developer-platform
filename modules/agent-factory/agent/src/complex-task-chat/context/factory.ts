/**
 * Factory for ContextManager — selects implementation based on env vars.
 *
 * CONTEXT_STRATEGY env var:
 *   - "noop" (default): NoopContextManager
 *   - "lcm": LcmContext (requires CONTEXT_TABLE + Bedrock access)
 */
import { ContextManager } from './types';
import { NoopContextManager } from './noop-context';

export function buildContextManager(env: Record<string, string | undefined> = process.env): ContextManager {
  const strategy = env.CONTEXT_STRATEGY ?? 'noop';

  switch (strategy) {
    case 'noop':
      return new NoopContextManager();

    case 'lcm': {
      // Lazy import to avoid pulling DDB/Bedrock deps when not needed
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const { createLcmContext } = require('./lcm/lcm-context');
      return createLcmContext(env);
    }

    default:
      throw new Error(`Unknown CONTEXT_STRATEGY: ${strategy}. Valid: noop, lcm`);
  }
}
