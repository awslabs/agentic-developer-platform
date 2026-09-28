/**
 * Resilient wrapper around the Claude Agent SDK query() call.
 *
 * Handles transient failures (rate limits, network errors, fetch failures)
 * by retrying the query with exponential backoff. On retry:
 * - TRUE session resume (issue #2079): we capture the `session_id` emitted by
 *   the SDK on the first attempt and, on retry, pass `options.resume = sessionId`
 *   so the SDK reloads the FULL conversation history and the agent literally
 *   continues — it remembers what it already read, decided, and posted. The
 *   retry prompt becomes a short continuation nudge (from `resumeContext`)
 *   rather than the entire original task prompt.
 *   Requires `persistSession: true` at the call site (the SDK can only resume
 *   sessions it persisted to disk). If no session_id was captured (e.g. the
 *   stream stalled before the init message), we fall back to a best-effort
 *   prompt-prefix so the agent is at least told it is resuming.
 * - A progress-aware stall guard detects when retries fail to advance beyond
 *   the prior attempt's high-water mark and aborts the loop early.
 *
 * Issue #2079: Previously retries restarted from scratch (no conversation
 * memory), causing duplicate plan posts and guaranteed non-termination on
 * long tasks that hit even a single idle timeout.
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
  /**
   * Maximum time (ms) to wait for the next SDK message before treating
   * the stream as stalled and triggering a retry. Resets on every yielded
   * message (including tool_progress). Default: 600_000 (10 minutes).
   */
  idleTimeoutMs?: number;
  /**
   * Optional callback that generates the retry prompt on attempts 2+.
   * Called with the attempt number (2+) and the total number of messages
   * yielded across all prior attempts.
   *
   * Behaviour depends on whether a session_id was captured on a prior attempt:
   * - TRUE resume (session_id captured): the SDK reloads the full conversation
   *   via `options.resume`, so the returned string is used ALONE as a short
   *   continuation nudge (NOT concatenated with the original task prompt — the
   *   agent already has the task in its resumed history).
   * - Fallback (no session_id, e.g. stalled before init): the returned string
   *   is prepended to the original prompt as a best-effort resume preamble.
   *
   * Issue #2079: prevents duplicate Implementation Plan posts and wasted
   * compute on long-running agent tasks.
   */
  resumeContext?: (attemptNumber: number, priorMessagesYielded: number) => string;
  /** Optional logger — receives retry lifecycle messages. */
  log?: (msg: string) => void;
}

/**
 * Extract the SDK session id from a stream message, if present. The SDK emits
 * `session_id` on its `system`/`initialize` message (and carries it on
 * subsequent messages). We read it defensively since the union of message
 * shapes is wide and we only need the field when it exists.
 */
function extractSessionId(message: SDKStreamMessage): string | undefined {
  const sid = (message as { session_id?: unknown })?.session_id;
  return typeof sid === 'string' && sid.length > 0 ? sid : undefined;
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
 * retry — optionally with a resume context prefix so the agent doesn't
 * redo completed work.
 *
 * Non-retryable errors are re-thrown immediately.
 */
export async function* resilientQuery(opts: ResilientQueryOptions): AsyncGenerator<SDKStreamMessage> {
  const {
    queryParams,
    maxRetries = 5,
    baseDelayMs = 10_000,
    maxDelayMs = 120_000,
    idleTimeoutMs = 600_000,
    resumeContext,
    log = console.log,
  } = opts;

  let attempt = 0;
  // Total messages yielded across ALL attempts (cross-attempt progress).
  let totalMessagesYielded = 0;
  // High-water mark: the total messages yielded up to the point the PREVIOUS
  // attempt stalled. A new attempt must exceed this to count as "making progress."
  let highWaterMark = 0;
  // Consecutive stall retries: incremented when an attempt fails to advance
  // beyond the high-water mark. Reset when genuine forward progress is made.
  let consecutiveStallRetries = 0;
  const MAX_CONSECUTIVE_STALL_RETRIES = 3;
  // Session id captured from the SDK stream, used to TRULY resume on retry.
  let capturedSessionId: string | undefined;

  while (true) {
    attempt++;
    let messagesThisAttempt = 0;
    try {
      // On retry (attempt > 1), continue the prior conversation rather than
      // re-running the task from scratch.
      let effectiveParams = queryParams;
      if (attempt > 1) {
        const nudge = resumeContext?.(attempt, totalMessagesYielded);
        if (capturedSessionId) {
          // TRUE resume: the SDK reloads the full conversation history for this
          // session, so the agent remembers everything it already did. The
          // prompt for this turn is just a short continuation nudge (or, if the
          // caller gave none, a minimal default) — NOT the whole original task,
          // which already lives in the resumed history.
          const baseOptions = (queryParams as { options?: Record<string, unknown> }).options ?? {};
          effectiveParams = {
            ...queryParams,
            prompt: nudge ?? 'Continue the task from where you left off. Do not repeat completed steps.',
            options: { ...baseOptions, resume: capturedSessionId },
          } as typeof queryParams;
          log(`   ↩️  Resuming session ${capturedSessionId} (true SDK resume — full history reloaded)`);
        } else if (nudge && typeof queryParams.prompt === 'string') {
          // Fallback: no session_id was captured (the stream stalled before the
          // init message). Best-effort prompt-prefix so the agent is at least
          // told it is resuming. The SDK's query() prompt is
          // `string | AsyncIterable<SDKUserMessage>` — we can only prepend to
          // string prompts; async-iterable prompts are passed through unchanged.
          effectiveParams = {
            ...queryParams,
            prompt: nudge + '\n\n' + queryParams.prompt,
          };
          log(`   ⚠️  No session_id captured yet — falling back to prompt-prefix resume (best-effort)`);
        }
      }

      const session = query(effectiveParams);
      const iterator = (session as AsyncIterable<SDKStreamMessage>)[Symbol.asyncIterator]();
      try {
        while (true) {
          let idleTimer: ReturnType<typeof setTimeout> | undefined;
          const idle = new Promise<never>((_, reject) => {
            idleTimer = setTimeout(
              () => reject(new Error(`stream idle timeout: no SDK message for ${Math.round(idleTimeoutMs / 1000)}s`)),
              idleTimeoutMs,
            );
          });
          let result: IteratorResult<SDKStreamMessage>;
          try {
            result = await Promise.race([iterator.next(), idle]);
          } finally {
            clearTimeout(idleTimer);
          }
          if (result.done) break;
          // Capture the session id the first time the SDK surfaces it, so a
          // later retry can resume this exact conversation.
          if (!capturedSessionId) {
            const sid = extractSessionId(result.value);
            if (sid) {
              capturedSessionId = sid;
            }
          }
          messagesThisAttempt++;
          totalMessagesYielded++;
          yield result.value;
        }
      } finally {
        // Close the query to terminate the underlying Claude Code process.
        // Without this, background processes (sky, tail, etc.) keep the
        // async generator alive and the agent hangs after completion.
        // Also resolves/rejects the abandoned iterator.next() on idle timeout.
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

      // Progress-aware stall detection (issue #2079).
      //
      // Old logic: only counted stalls when zero messages were yielded in an
      // attempt (`!yieldedInThisAttempt`). This was defeated when the agent
      // re-did the same opening work on every retry (reading code, posting a
      // plan) — those messages counted as "progress" but were actually just
      // replaying work that had already been yielded to the caller.
      //
      // New logic: an attempt is a "stall" if it failed to advance the total
      // message count beyond the high-water mark set by the previous stall.
      // This catches the repeating-opening-phase loop regardless of how many
      // messages each individual attempt yields.
      const isIdleTimeout = error.message.includes('stream idle timeout');
      if (isIdleTimeout) {
        const madeForwardProgress = totalMessagesYielded > highWaterMark;
        if (madeForwardProgress) {
          // Genuine progress — update the high-water mark and reset counter.
          highWaterMark = totalMessagesYielded;
          consecutiveStallRetries = 0;
        } else {
          // No forward progress beyond the previous stall point.
          consecutiveStallRetries++;
          if (consecutiveStallRetries >= MAX_CONSECUTIVE_STALL_RETRIES) {
            log(`❌ ${MAX_CONSECUTIVE_STALL_RETRIES} consecutive idle-timeout retries with no forward progress (stuck at ${totalMessagesYielded} total messages) — aborting`);
            throw error;
          }
        }
      } else {
        // Non-idle-timeout errors (fetch failed, 502, etc.) don't affect the
        // stall counter — they're transient network issues, not persistent stalls.
      }

      // Exponential backoff with jitter
      const exponentialDelay = baseDelayMs * Math.pow(2, attempt - 1);
      const jitter = Math.random() * baseDelayMs;
      const delay = Math.min(exponentialDelay + jitter, maxDelayMs);

      log(`⚠️  Retryable error on attempt ${attempt}/${maxRetries}: ${error.message}`);
      log(`   Total messages yielded: ${totalMessagesYielded}, high-water mark: ${highWaterMark}`);
      log(`   Retrying in ${(delay / 1000).toFixed(1)}s...`);

      await new Promise(resolve => setTimeout(resolve, delay));

      const mode = capturedSessionId ? 'resuming' : 'restarting';
      log(`🔄 ${mode} query (attempt ${attempt + 1}/${maxRetries + 1})...`);
    }
  }
}
