/**
 * Resilient wrapper around the Claude Agent SDK query() call.
 *
 * Handles transient failures (rate limits, network errors, fetch failures)
 * by retrying the entire query with exponential backoff. When a failure
 * occurs mid-stream, collected messages from the current attempt are
 * discarded and the query restarts from scratch — the SDK doesn't support
 * resuming a partial stream.
 */
import { query } from '@anthropic-ai/claude-agent-sdk';

/** The shape of a single message yielded by query(). */
export type SDKStreamMessage = Awaited<ReturnType<typeof query>> extends AsyncIterable<infer T> ? T : never;

export interface ResilientQueryOptions {
  /** Parameters forwarded to the SDK query() call. */
  queryParams: Parameters<typeof query>[0];
  /** Max number of retry attempts (default: 5). */
  maxRetries?: number;
  /** Base delay in ms for exponential backoff (default: 10000). */
  baseDelayMs?: number;
  /** Maximum delay cap in ms (default: 120000 = 2 min). */
  maxDelayMs?: number;
  /** Optional logger — receives retry lifecycle messages. */
  log?: (msg: string) => void;
}

const RETRYABLE_PATTERNS = [
  'fetch failed',
  'econnreset',
  'econnrefused',
  'socket hang up',
  'epipe',
  'enotfound',
  'network',
  'aborted',
  'timeout',
  'rate limit',
  'rate_limit',
  '429',
  '502',
  '503',
  'service unavailable',
  'too many requests',
  'throttl',
  'overloaded',
  'capacity',
  'internal server error',
  'bad gateway',
  'gateway timeout',
];

function isRetryableError(err: unknown): boolean {
  const message = ((err as Error)?.message || String(err)).toLowerCase();
  return RETRYABLE_PATTERNS.some(p => message.includes(p));
}

/**
 * Wraps the SDK query() in a retry loop. On each attempt the full async
 * iterator is consumed and messages are yielded to the caller. If the
 * stream throws a retryable error, we wait with exponential backoff and
 * restart the query from scratch.
 *
 * Non-retryable errors are re-thrown immediately.
 */
export async function* resilientQuery(opts: ResilientQueryOptions): AsyncGenerator<SDKStreamMessage> {
  const {
    queryParams,
    maxRetries = 5,
    baseDelayMs = 10_000,
    maxDelayMs = 120_000,
    log = console.log,
  } = opts;

  let attempt = 0;

  while (true) {
    attempt++;
    try {
      const session = query(queryParams);
      try {
        for await (const message of session) {
          yield message;
        }
      } finally {
        // Close the query to terminate the underlying Claude Code process.
        // Without this, background processes (sky, tail, etc.) keep the
        // async generator alive and the agent hangs after completion.
        session.close();
      }
      // Stream completed successfully — we're done.
      return;
    } catch (err) {
      const error = err as Error;
      const retryable = isRetryableError(error);

      if (!retryable || attempt > maxRetries) {
        log(`❌ Non-retryable error or max retries (${maxRetries}) exceeded: ${error.message}`);
        throw error;
      }

      // Exponential backoff with jitter
      const exponentialDelay = baseDelayMs * Math.pow(2, attempt - 1);
      const jitter = Math.random() * baseDelayMs;
      const delay = Math.min(exponentialDelay + jitter, maxDelayMs);

      log(`⚠️  Retryable error on attempt ${attempt}/${maxRetries}: ${error.message}`);
      log(`   Retrying in ${(delay / 1000).toFixed(1)}s...`);

      await new Promise(resolve => setTimeout(resolve, delay));

      log(`🔄 Restarting query (attempt ${attempt + 1}/${maxRetries + 1})...`);
    }
  }
}
