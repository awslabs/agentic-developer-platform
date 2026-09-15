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
 *
 * Scope note (issue #4186): the resume described above is IN-PROCESS only —
 * it recovers a stalled stream within the life of one pod. It is not
 * cross-pod resume: if the pod dies, the captured session id dies with it.
 * `onSessionId` (Phase 1 of #4186) exists so the caller can record the id
 * durably; the cross-pod resume branch that would consume it is Phase 3 and
 * is NOT implemented here.
 *
 * Live-control lifecycle (issue #3962): three OPTIONAL hooks let the Claude
 * control adapter own one attempt's transport without this file learning
 * anything about control semantics — `attemptInputFactory` (fresh open input
 * per attempt), `onAttemptHandle` (access to the live query handle) and
 * `cancellation` (typed intentional stop). All three are absent for the 17
 * existing callers, and when absent every code path below is byte-identical to
 * the pre-#3962 behaviour: same query params, same prompt construction, same
 * one-time fail-soft `onSessionId`, same `session.close()`. That is a hard
 * requirement, not a nicety — this wrapper is on the critical path of every
 * agent run in the platform, and a control feature that is disabled everywhere
 * must not be able to change how ordinary runs behave.
 */
import { query } from '@anthropic-ai/claude-agent-sdk';
import { isControlCancellation } from '../control-runtime';

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
  /**
   * Optional callback fired ONCE, the first time the SDK surfaces a
   * `session_id` on the stream (issue #4186, Phase 1).
   *
   * The captured session id is otherwise process-local: it is the input to
   * `options.resume` on an in-process retry and nothing else can see it. A
   * pod that dies takes the id with it, so a replacement pod has no way to
   * locate the conversation even though the SDK persisted it. This callback
   * is the escape hatch — the caller records the id somewhere durable.
   *
   * Called with the id while the stream is still running (not after), because
   * that is when the id is needed: a run that never reaches its `result`
   * message is exactly the run whose id matters most.
   *
   * Contract: this callback MUST NOT be able to break the run. It is invoked
   * inside a try/catch and a throw is logged and swallowed.
   */
  onSessionId?: (sessionId: string) => void;
  /** Optional logger — receives retry lifecycle messages. */
  log?: (msg: string) => void;
  /**
   * Optional per-attempt input factory (issue #3962), consumed only by the
   * Claude control adapter.
   *
   * Called BEFORE every `query()` — attempt 1, true-resume retries and
   * fallback retries alike. There is no path that builds a query without
   * calling it, because the one thing a steering channel cannot survive is an
   * attempt whose input nobody is holding: the operator's instruction would be
   * accepted, queued, and then delivered into a transport that the retry
   * already replaced.
   *
   * Returns a fresh open iterable plus a disposer. Fresh each time is required —
   * an async iterable that a prior `query()` already consumed is exhausted, so
   * reusing it would silently produce an attempt that can never receive input.
   *
   * `isResume` lets the adapter distinguish continuation from initial context:
   * a resumed attempt already has the task in its reloaded history and needs
   * only pending input, while a fallback attempt must preserve the initial task
   * prompt. The disposer is invoked exactly once per attempt in the `finally`
   * that closes the session, so no attempt's input outlives its transport.
   */
  attemptInputFactory?: (context: {
    attemptNumber: number;
    isResume: boolean;
    /** The prompt this attempt would otherwise send (task or continuation nudge). */
    promptText: string;
  }) => { input: AsyncIterable<unknown>; dispose: () => void | Promise<void> };
  /**
   * Optional callback receiving the live query handle for each attempt
   * (issue #3962).
   *
   * Fired immediately after `query()` returns and before the stream is
   * consumed, so the adapter can publish the new attempt endpoint and
   * invalidate the previous one at the moment the swap actually happens rather
   * than inferring it later. Fail-soft: a throw is logged and swallowed, since
   * an observability or control sink must never take a run down.
   */
  onAttemptHandle?: (handle: { attemptNumber: number; session: unknown }) => void;
  /**
   * Optional typed cancellation source (issue #3962).
   *
   * Checked at every point where this loop would otherwise commit to more work:
   * before constructing a query, after backoff, and when classifying an error.
   * `isCancelled()` is polled rather than awaited so a cancel issued during
   * backoff is observed the instant the sleep resolves.
   *
   * The critical rule: a cancellation NEVER enters retryable-error-text
   * classification. Cancellations carry words like "aborted" that the pattern
   * list matches, so without this the wrapper would treat a deliberate abort as
   * a transient blip and start a fresh attempt — turning "stop" into "restart".
   */
  cancellation?: {
    isCancelled: () => boolean;
    /** The typed error to throw. Defaults to the caller's cancellation error. */
    error?: () => Error;
  };
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
    onSessionId,
    log = console.log,
    attemptInputFactory,
    onAttemptHandle,
    cancellation,
  } = opts;

  /** Typed cancellation error, never routed through error-text classification. */
  const cancellationError = (): Error =>
    cancellation?.error?.() ?? new Error('resilient query cancelled by control runtime');

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
    // Cancelled before this attempt is built: stop here. Checked at the top of
    // the loop so a cancel arriving during the previous backoff sleep cannot be
    // followed by another query — "cancel during backoff starts no new attempt"
    // is enforced structurally rather than by hoping the timing works out.
    if (cancellation?.isCancelled()) {
      log('   ⛔ Cancelled before query construction — no further attempts');
      throw cancellationError();
    }
    attempt++;
    let messagesThisAttempt = 0;
    // This attempt's input disposer, if a factory supplied one.
    let disposeAttemptInput: (() => void | Promise<void>) | undefined;
    try {
      // On retry (attempt > 1), continue the prior conversation rather than
      // re-running the task from scratch.
      let effectiveParams = queryParams;
      // Whether this attempt is a TRUE resume (SDK reloads history) or a
      // fallback/initial attempt (prompt carries the task).
      let isResumeAttempt = false;
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
          isResumeAttempt = true;
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

      // Issue #3962: build this attempt's input BEFORE the query, on every
      // attempt including resumes and fallbacks. When no factory is supplied
      // `effectiveParams` is untouched, so the 17 legacy callers send exactly
      // the params they always sent.
      if (attemptInputFactory) {
        const promptText = typeof (effectiveParams as { prompt?: unknown }).prompt === 'string'
          ? (effectiveParams as { prompt: string }).prompt
          : '';
        const attemptInput = attemptInputFactory({ attemptNumber: attempt, isResume: isResumeAttempt, promptText });
        disposeAttemptInput = attemptInput.dispose;
        // The adapter's iterable replaces the prompt: the Claude SDK accepts
        // `string | AsyncIterable<SDKUserMessage>`, and the streaming form is
        // what allows a second turn to be delivered into a live attempt. The
        // cast is confined to this Claude-specific helper — the neutral contract
        // never exposes an iterable.
        effectiveParams = { ...effectiveParams, prompt: attemptInput.input } as typeof queryParams;
      }

      // Last check before committing to a query: a cancellation that landed
      // while the input factory ran must not produce a live attempt.
      if (cancellation?.isCancelled()) {
        log('   ⛔ Cancelled during query construction — no query started');
        throw cancellationError();
      }

      const session = query(effectiveParams);
      // Publish the handle before consuming the stream, so the adapter swaps its
      // current attempt endpoint at the moment the swap happens.
      if (onAttemptHandle) {
        try {
          onAttemptHandle({ attemptNumber: attempt, session });
        } catch (err) {
          log(`   ⚠️  onAttemptHandle callback threw (ignored): ${(err as Error)?.message ?? err}`);
        }
      }
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
              // Issue #4186 (Phase 1): let the id escape the process so a
              // replacement pod could locate this conversation. Fail-soft by
              // contract — a broken observability sink must never take the
              // agent run down with it.
              if (onSessionId) {
                try {
                  onSessionId(sid);
                } catch (err) {
                  log(`   ⚠️  onSessionId callback threw (ignored): ${(err as Error)?.message ?? err}`);
                }
              }
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
        //
        // Issue #3962 preserves this on every path, including idle timeout. The
        // attempt's input is disposed AFTER the session closes: disposing first
        // would end the iterable underneath a still-live query.
        session.close();
      }
      // Stream completed successfully — we're done.
      return;
    } catch (err) {
      const error = err as Error;

      // Issue #3962: a typed cancellation exits before ANY error-text
      // classification. This ordering is the whole safety property — cancellation
      // messages contain words like "aborted" that RETRYABLE_PATTERNS matches, so
      // classifying first would convert a deliberate stop into a fresh attempt.
      if (isControlCancellation(error) || cancellation?.isCancelled()) {
        log(`⛔ Control cancellation — not retrying: ${error.message}`);
        throw isControlCancellation(error) ? error : cancellationError();
      }

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
    } finally {
      // Issue #3962: dispose this attempt's input on EVERY exit from the attempt
      // — success, retry, throw, or generator abandonment by the consumer. Once
      // per attempt: the local is cleared so a second pass through this block
      // (e.g. a throw during disposal) cannot double-dispose.
      const dispose = disposeAttemptInput;
      disposeAttemptInput = undefined;
      if (dispose) {
        try {
          await dispose();
        } catch (err) {
          log(`   ⚠️  attempt input disposal threw (ignored): ${(err as Error)?.message ?? err}`);
        }
      }
    }
  }
}
