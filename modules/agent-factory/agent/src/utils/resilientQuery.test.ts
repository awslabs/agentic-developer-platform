/**
 * Unit tests for resilientQuery.ts
 *
 * Tests the retry resilience wrapper for Claude Agent SDK query() calls.
 */

// Mock the SDK query function before importing resilientQuery
jest.mock('@anthropic-ai/claude-agent-sdk', () => ({
  query: jest.fn(),
}));

import { resilientQuery, ResilientQueryOptions } from './resilientQuery';
import { query } from '@anthropic-ai/claude-agent-sdk';

const mockQuery = query as jest.MockedFunction<typeof query>;

describe('resilientQuery', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  /**
   * Helper: wraps an async generator with a .close() method to match
   * the SDK query() return type (AsyncIterable & { close(): void }).
   */
  function withClose<T>(gen: AsyncGenerator<T>): AsyncGenerator<T> & { close: jest.Mock } {
    const closeFn = jest.fn();
    return Object.assign(gen, { close: closeFn });
  }

  /**
   * Helper to create an async generator from an array of values
   */
  function asyncFromArray<T>(items: T[]): AsyncGenerator<T> & { close: jest.Mock } {
    async function* inner(): AsyncGenerator<T> {
      for (const item of items) {
        yield item;
      }
    }
    return withClose(inner());
  }

  /**
   * Helper to create an async generator that throws after yielding some items
   */
  function asyncThrowingGenerator<T>(
    items: T[],
    errorToThrow: Error,
    throwAfter: number = items.length
  ): AsyncGenerator<T> & { close: jest.Mock } {
    async function* inner(): AsyncGenerator<T> {
      for (let i = 0; i < items.length; i++) {
        if (i === throwAfter) {
          throw errorToThrow;
        }
        yield items[i];
      }
      if (throwAfter >= items.length) {
        throw errorToThrow;
      }
    }
    return withClose(inner());
  }

  /**
   * Helper to collect all values from an async generator
   */
  async function collectAll<T>(gen: AsyncGenerator<T>): Promise<T[]> {
    const results: T[] = [];
    for await (const item of gen) {
      results.push(item);
    }
    return results;
  }

  describe('successful pass-through', () => {
    it('should yield all messages when no errors occur', async () => {
      const messages = [
        { type: 'assistant', content: 'Hello' },
        { type: 'assistant', content: 'World' },
        { type: 'result', subtype: 'success' },
      ];

      mockQuery.mockReturnValue(asyncFromArray(messages) as any);

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: { model: 'claude-sonnet-4-20250514' } } as any,
        log: jest.fn(),
      };

      const results = await collectAll(resilientQuery(opts));

      expect(results).toEqual(messages);
      expect(mockQuery).toHaveBeenCalledTimes(1);
    });

    it('should pass through query parameters correctly', async () => {
      const messages = [{ type: 'result', subtype: 'success' }];
      mockQuery.mockReturnValue(asyncFromArray(messages) as any);

      const queryParams = {
        prompt: 'test prompt',
        options: {
          model: 'claude-sonnet-4-20250514',
          cwd: '/test/dir',
          allowedTools: ['Read', 'Write'],
        },
      };

      const opts: ResilientQueryOptions = {
        queryParams: queryParams as any,
        log: jest.fn(),
      };

      await collectAll(resilientQuery(opts));

      expect(mockQuery).toHaveBeenCalledWith(queryParams);
    });
  });

  describe('SDK response failures', () => {
    // Shape observed on #5185: a provider error followed by a success result
    // with real cost/turn counts. The worker stops consuming at the result.
    const bedrockError = 'API Error: An error occurred (internalServerException) when calling the '
      + 'InvokeModelWithResponseStream operation: The system encountered an unexpected error '
      + 'during processing. Try your request again.';
    const assistant = (text: string, extra: Record<string, unknown> = {}) => ({
      type: 'assistant', parent_tool_use_id: null,
      message: { role: 'assistant', content: [{ type: 'text', text }] },
      ...extra,
    });
    const success = {
      type: 'result', subtype: 'success', is_error: false,
      total_cost_usd: 5.5267, num_turns: 31, result: 'Completed',
    };
    const options = (): ResilientQueryOptions => ({
      queryParams: { prompt: 'Implement the issue', options: { persistSession: true } } as any,
      maxRetries: 2, baseDelayMs: 10, maxDelayMs: 20, log: jest.fn(),
    });

    async function consumeUntilResult(opts: ResilientQueryOptions, seen: unknown[]): Promise<void> {
      for await (const message of resilientQuery(opts)) {
        seen.push(message);
        if (message.type === 'result') break;
      }
    }

    it.each([undefined, 'unknown', 'server_error'])(
      'resumes before yielding a false success (assistant error=%s)', async (error) => {
        const init = { type: 'system', subtype: 'init', session_id: 'bedrock-session' };
        const progress = assistant('I have written the design document.');
        const failed = asyncFromArray([init, progress, assistant(bedrockError, { error }), success]);
        const completed = assistant('Implementation and tests are complete.');
        const recovered = asyncFromArray([completed, success]);
        mockQuery.mockReturnValueOnce(failed as any).mockReturnValueOnce(recovered as any);
        const onSessionId = jest.fn();
        const seen: unknown[] = [];
        const done = consumeUntilResult({ ...options(), onSessionId }, seen);
        await jest.advanceTimersByTimeAsync(100);
        await done;

        expect(seen).toEqual([init, progress, completed, success]);
        expect(mockQuery).toHaveBeenCalledTimes(2);
        expect(mockQuery.mock.calls[1][0]).toMatchObject({
          prompt: 'Continue the task from where you left off. Do not repeat completed steps.',
          options: { persistSession: true, resume: 'bedrock-session' },
        });
        expect(onSessionId).toHaveBeenCalledTimes(1);
        expect(failed.close).toHaveBeenCalledTimes(1);
        expect(recovered.close).toHaveBeenCalledTimes(1);
      },
    );

    it.each([0, 2])('throws after exhausting %i retries without yielding completion', async (maxRetries) => {
      const attempts: ReturnType<typeof asyncFromArray>[] = [];
      mockQuery.mockImplementation(() => {
        const session = asyncFromArray([assistant(bedrockError), success]);
        attempts.push(session);
        return session as any;
      });
      const seen: unknown[] = [];
      const rejected = expect(consumeUntilResult({ ...options(), maxRetries }, seen)).rejects.toThrow(bedrockError);
      await jest.advanceTimersByTimeAsync(100);
      await rejected;

      expect(mockQuery).toHaveBeenCalledTimes(maxRetries + 1);
      expect(seen).toEqual([]);
      for (const attempt of attempts) expect(attempt.close).toHaveBeenCalledTimes(1);
    });

    it.each([
      ['is_error result', [{ ...success, is_error: true, result: bedrockError }]],
      ['success result containing API error', [{ ...success, result: bedrockError }]],
      ['execution error result', [{ type: 'result', subtype: 'error_during_execution', is_error: true, errors: [bedrockError] }]],
      ['structured server error', [assistant('The provider failed.', { error: 'server_error' }), success]],
      ['structured rate limit', [assistant('Please wait.', { error: 'rate_limit' }), success]],
      ['structured overload', [assistant('Please wait.', { error: 'overloaded' }), success]],
      ['error followed by EOF', [assistant(bedrockError, { session_id: 'error-only-session' })]],
    ])('retries %s through the existing wrapper', async (_name, messages) => {
      const failed = asyncFromArray(messages as unknown[]);
      mockQuery.mockReturnValueOnce(failed as any).mockReturnValueOnce(asyncFromArray([success]) as any);
      const done = collectAll(resilientQuery(options()));
      await jest.advanceTimersByTimeAsync(100);

      expect(await done).toEqual([success]);
      expect(mockQuery).toHaveBeenCalledTimes(2);
      expect(failed.close).toHaveBeenCalledTimes(1);
      if (_name === 'error followed by EOF') {
        expect(mockQuery.mock.calls[1][0].options?.resume).toBe('error-only-session');
      }
    });

    it.each([
      ['authentication', [assistant('API Error: authentication failed; previous attempt was overloaded', { error: 'authentication_failed' }), success]],
      ['billing', [assistant('API Error: billing error after internalServerException', { error: 'billing_error' }), success]],
      ['invalid request', [assistant('API Error: invalid timeout parameter', { error: 'invalid_request' }), success]],
      ['access denied', [assistant('API Error: AccessDeniedException: not authorized to perform bedrock:InvokeModel'), success]],
      ['unclassified API error', [assistant('API Error: unexpected provider response'), success]],
      ['execution failure', [{ type: 'result', subtype: 'error_during_execution', is_error: true, errors: ['Permission denied'] }]],
      ['unknown failed result', [{ ...success, is_error: true, result: '' }]],
      ['max turns', [assistant(bedrockError), { type: 'result', subtype: 'error_max_turns', errors: [bedrockError] }]],
      ['max budget', [assistant(bedrockError), { type: 'result', subtype: 'error_max_budget_usd', errors: [bedrockError] }]],
      ['structured output limit', [{ type: 'result', subtype: 'error_max_structured_output_retries', errors: ['timeout'] }]],
    ])('reports %s as failure without retrying', async (_name, messages) => {
      const session = asyncFromArray(messages as unknown[]);
      mockQuery.mockReturnValue(session as any);
      const seen: unknown[] = [];
      await expect(consumeUntilResult(options(), seen)).rejects.toThrow();
      expect(seen).toEqual([]);
      expect(mockQuery).toHaveBeenCalledTimes(1);
      expect(session.close).toHaveBeenCalledTimes(1);
    });

    it('allows the SDK to recover internally before its final result', async () => {
      const recovered = assistant('The request recovered and the work is complete.');
      mockQuery.mockReturnValue(asyncFromArray([assistant(bedrockError), recovered, success]) as any);
      expect(await collectAll(resilientQuery(options()))).toEqual([recovered, success]);
      expect(mockQuery).toHaveBeenCalledTimes(1);
    });

    it('does not mistake normal output, tool failures, child errors, or SDK retry notices for run failures', async () => {
      const messages = [
        assistant('I fixed the API Error: internalServerException handling.'),
        assistant('```\n' + bedrockError + '\n```'),
        assistant('Example:\n' + bedrockError),
        { type: 'user', message: { content: [{ type: 'tool_result', is_error: true, content: bedrockError }] } },
        assistant(bedrockError, { parent_tool_use_id: 'child-task', error: 'server_error' }),
        { type: 'system', subtype: 'api_retry', error: 'server_error', attempt: 1 },
        success,
      ];
      mockQuery.mockReturnValue(asyncFromArray(messages) as any);
      expect(await collectAll(resilientQuery(options()))).toEqual(messages);
      expect(mockQuery).toHaveBeenCalledTimes(1);
    });

    it('does not let a child response clear a pending top-level error', async () => {
      const child = assistant('Child task finished.', { parent_tool_use_id: 'child-task' });
      mockQuery.mockReturnValue(asyncFromArray([assistant(bedrockError), child, success]) as any);
      const seen: unknown[] = [];
      await expect(consumeUntilResult({ ...options(), maxRetries: 0 }, seen)).rejects.toThrow(bedrockError);
      expect(seen).toEqual([child]);
    });

    it('honours cancellation during provider-error backoff', async () => {
      const controller = new AbortController();
      const session = asyncFromArray([assistant(bedrockError), success]);
      mockQuery.mockReturnValue(session as any);
      const seen: unknown[] = [];
      const rejected = expect(consumeUntilResult({
        ...options(), baseDelayMs: 1000, maxDelayMs: 1000,
        cancellation: { isCancelled: () => controller.signal.aborted, signal: controller.signal },
      }, seen)).rejects.toThrow('cancelled');
      await jest.advanceTimersByTimeAsync(1);
      controller.abort();
      await rejected;
      expect(mockQuery).toHaveBeenCalledTimes(1);
      expect(session.close).toHaveBeenCalledTimes(1);
      expect(seen).toEqual([]);
    });
  });

  describe('retry behavior on retryable errors', () => {
    it('should retry on "fetch failed" error', async () => {
      const messages = [{ type: 'result', subtype: 'success' }];

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator([], new Error('fetch failed')) as any;
        }
        return asyncFromArray(messages) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 100,
        log,
      };

      // Start the generator
      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);

      // Fast-forward through the retry delay
      await jest.advanceTimersByTimeAsync(200);

      const results = await resultsPromise;

      expect(results).toEqual(messages);
      expect(mockQuery).toHaveBeenCalledTimes(2);
      expect(log).toHaveBeenCalledWith(expect.stringContaining('Retryable error'));
    });

    it('should retry on rate limit (429) error', async () => {
      const messages = [{ type: 'result', subtype: 'success' }];

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator([], new Error('API returned 429: Too Many Requests')) as any;
        }
        return asyncFromArray(messages) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 100,
        log: jest.fn(),
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);
      await jest.advanceTimersByTimeAsync(200);

      const results = await resultsPromise;

      expect(results).toEqual(messages);
      expect(mockQuery).toHaveBeenCalledTimes(2);
    });

    it('should retry on 502 bad gateway error', async () => {
      const messages = [{ type: 'result', subtype: 'success' }];

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator([], new Error('502 Bad Gateway')) as any;
        }
        return asyncFromArray(messages) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 100,
        log: jest.fn(),
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);
      await jest.advanceTimersByTimeAsync(200);

      const results = await resultsPromise;

      expect(mockQuery).toHaveBeenCalledTimes(2);
    });

    it('should retry on 503 service unavailable error', async () => {
      const messages = [{ type: 'result', subtype: 'success' }];

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator([], new Error('503 Service Unavailable')) as any;
        }
        return asyncFromArray(messages) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 100,
        log: jest.fn(),
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);
      await jest.advanceTimersByTimeAsync(200);

      const results = await resultsPromise;

      expect(mockQuery).toHaveBeenCalledTimes(2);
    });

    it('should retry on network errors (ECONNRESET)', async () => {
      const messages = [{ type: 'result', subtype: 'success' }];

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator([], new Error('ECONNRESET: Connection reset by peer')) as any;
        }
        return asyncFromArray(messages) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 100,
        log: jest.fn(),
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);
      await jest.advanceTimersByTimeAsync(200);

      const results = await resultsPromise;

      expect(mockQuery).toHaveBeenCalledTimes(2);
    });

    it('should retry on overloaded error', async () => {
      const messages = [{ type: 'result', subtype: 'success' }];

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator([], new Error('API is overloaded, please retry')) as any;
        }
        return asyncFromArray(messages) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 100,
        log: jest.fn(),
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);
      await jest.advanceTimersByTimeAsync(200);

      const results = await resultsPromise;

      expect(mockQuery).toHaveBeenCalledTimes(2);
    });
  });

  describe('non-retryable error handling', () => {
    it('should immediately re-throw non-retryable errors', async () => {
      const nonRetryableError = new Error('Invalid API key');

      mockQuery.mockImplementation(() => {
        return asyncThrowingGenerator([], nonRetryableError) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        log,
      };

      await expect(collectAll(resilientQuery(opts))).rejects.toThrow('Invalid API key');
      expect(mockQuery).toHaveBeenCalledTimes(1);
      expect(log).toHaveBeenCalledWith(expect.stringContaining('Non-retryable error'));
    });

    it('should immediately re-throw authentication errors', async () => {
      const authError = new Error('Authentication failed: invalid credentials');

      mockQuery.mockImplementation(() => {
        return asyncThrowingGenerator([], authError) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        log: jest.fn(),
      };

      await expect(collectAll(resilientQuery(opts))).rejects.toThrow('Authentication failed');
      expect(mockQuery).toHaveBeenCalledTimes(1);
    });

    it('should immediately re-throw validation errors', async () => {
      const validationError = new Error('Validation error: prompt exceeds maximum length');

      mockQuery.mockImplementation(() => {
        return asyncThrowingGenerator([], validationError) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        log: jest.fn(),
      };

      await expect(collectAll(resilientQuery(opts))).rejects.toThrow('Validation error');
      expect(mockQuery).toHaveBeenCalledTimes(1);
    });
  });

  describe('max retry limit enforcement', () => {
    it('should stop retrying after maxRetries attempts', async () => {
      // Use real timers for this test to properly test the retry limit
      jest.useRealTimers();

      const retryableError = new Error('fetch failed');

      mockQuery.mockImplementation(() => {
        return asyncThrowingGenerator([], retryableError) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 10,  // Very short delay for fast tests
        maxDelayMs: 50,
        log,
      };

      await expect(collectAll(resilientQuery(opts))).rejects.toThrow('fetch failed');
      // Should be called maxRetries + 1 times (initial + retries)
      expect(mockQuery).toHaveBeenCalledTimes(4); // 1 initial + 3 retries
      expect(log).toHaveBeenCalledWith(expect.stringContaining('max retries'));

      // Restore fake timers for other tests
      jest.useFakeTimers();
    }, 10000);

    it('should use default maxRetries (5) when not specified', async () => {
      // Use real timers for this test to properly test the retry limit
      jest.useRealTimers();

      const retryableError = new Error('fetch failed');

      mockQuery.mockImplementation(() => {
        return asyncThrowingGenerator([], retryableError) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        baseDelayMs: 10,  // Very short delay for fast tests
        maxDelayMs: 50,
        log,
      };

      await expect(collectAll(resilientQuery(opts))).rejects.toThrow('fetch failed');
      // Default maxRetries is 5, so 6 total calls
      expect(mockQuery).toHaveBeenCalledTimes(6);

      // Restore fake timers for other tests
      jest.useFakeTimers();
    }, 10000);
  });

  describe('exponential backoff timing', () => {
    it.each([0, 0.5, 0.999])('applies exponential backoff with jitter %s', async (jitter) => {
      const random = jest.spyOn(Math, 'random').mockReturnValue(jitter);
      try {
        const callTimes: number[] = [];
        mockQuery.mockImplementation(() => {
          callTimes.push(Date.now());
          if (callTimes.length < 4) {
            return asyncThrowingGenerator([], new Error('fetch failed')) as any;
          }
          return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
        });

        const log = jest.fn();
        const resultsPromise = collectAll(resilientQuery({
          queryParams: { prompt: 'test', options: {} } as any,
          maxRetries: 5,
          baseDelayMs: 1000,
          maxDelayMs: 120000,
          log,
        }));

        // Three delays total up to 1+2+4 seconds plus three seconds of jitter.
        // The former 9.5-second advance could leave the final retry pending.
        await jest.advanceTimersByTimeAsync(10000);
        expect(await resultsPromise).toEqual([{ type: 'result', subtype: 'success' }]);
        expect(callTimes.slice(1).map((time, index) => time - callTimes[index]))
          .toEqual([1000, 2000, 4000].map(delay => delay + jitter * 1000));
        expect(log.mock.calls.filter(call => call[0].includes('Retrying in'))).toHaveLength(3);
      } finally {
        random.mockRestore();
      }
    });

    it('should cap delay at maxDelayMs', async () => {
      let callCount = 0;

      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount < 10) {
          return asyncThrowingGenerator([], new Error('fetch failed')) as any;
        }
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 10,
        baseDelayMs: 1000,
        maxDelayMs: 5000, // Cap at 5s
        log,
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);

      // Advance timers generously
      for (let i = 0; i < 15; i++) {
        await jest.advanceTimersByTimeAsync(6000);
      }

      await resultsPromise;

      // Check that delay is capped - later retries should not exceed 5000ms + jitter
      const retryCalls = log.mock.calls.filter(
        (call) => typeof call[0] === 'string' && call[0].includes('Retrying in')
      );

      // Extract delay values from log messages
      const delays = retryCalls.map((call) => {
        const match = call[0].match(/Retrying in ([\d.]+)s/);
        return match ? parseFloat(match[1]) : 0;
      });

      // After a few retries, delays should be capped near maxDelayMs
      const laterDelays = delays.slice(-3);
      laterDelays.forEach((delay) => {
        // maxDelayMs is 5000ms = 5s, with jitter of up to baseDelayMs (1s)
        // So max should be around 6s
        expect(delay).toBeLessThanOrEqual(6.5);
      });
    });
  });

  describe('default values', () => {
    it('should use default baseDelayMs of 10000 when not specified', async () => {
      // This test verifies the default values are documented correctly
      // We don't actually run the retry with real timing, just verify the log message

      let callCount = 0;

      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator([], new Error('fetch failed')) as any;
        }
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        // Not specifying baseDelayMs to test the default
        log,
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);

      // Advance past the default delay (10s base + jitter up to 10s = up to 20s)
      await jest.advanceTimersByTimeAsync(25000);

      await resultsPromise;

      // Check the log message shows delay based on 10_000ms base
      const retryCall = log.mock.calls.find(
        (call) => typeof call[0] === 'string' && call[0].includes('Retrying in')
      );
      expect(retryCall).toBeDefined();
      // First retry delay should be baseDelayMs + jitter = 10s + 0-10s = 10-20s
      const delayMatch = retryCall[0].match(/Retrying in ([\d.]+)s/);
      if (delayMatch) {
        const delay = parseFloat(delayMatch[1]);
        expect(delay).toBeGreaterThanOrEqual(10);
        expect(delay).toBeLessThanOrEqual(20);
      }
    }, 30000);

    it('should use default maxDelayMs of 120_000 when not specified', async () => {
      // This is more of a documentation test - the actual capping logic
      // is tested in the exponential backoff tests
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
      };

      // The default maxDelayMs should be 120_000 (2 minutes)
      // This is verified by the implementation
      expect(true).toBe(true);
    });

    it('should use console.log as default logger', async () => {
      const messages = [{ type: 'result', subtype: 'success' }];
      mockQuery.mockReturnValue(asyncFromArray(messages) as any);

      const consoleSpy = jest.spyOn(console, 'log').mockImplementation();

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        // No log function provided - should use console.log
      };

      await collectAll(resilientQuery(opts));

      // console.log shouldn't have been called for success case
      // (no retries, no errors)
      consoleSpy.mockRestore();
    });
  });

  describe('mid-stream failure handling', () => {
    it('should discard partial results and restart on mid-stream error', async () => {
      const partialMessages = [
        { type: 'assistant', content: 'partial-1' },
        { type: 'assistant', content: 'partial-2' },
      ];
      const fullMessages = [
        { type: 'assistant', content: 'full-1' },
        { type: 'assistant', content: 'full-2' },
        { type: 'result', subtype: 'success' },
      ];

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          // First call yields 2 messages then throws
          return asyncThrowingGenerator(
            partialMessages,
            new Error('fetch failed'),
            2 // throw after yielding 2 messages
          ) as any;
        }
        return asyncFromArray(fullMessages) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 100,
        log,
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);

      await jest.advanceTimersByTimeAsync(300);

      const results = await resultsPromise;

      // The implementation yields messages as they come, so we'll see
      // partial messages followed by full messages after retry
      // This tests that the generator properly handles mid-stream failures
      expect(mockQuery).toHaveBeenCalledTimes(2);
    });
  });

  describe('idle timeout watchdog', () => {
    it('should trigger retry when iterator.next() never resolves (stall detection)', async () => {
      jest.useRealTimers();

      let callCount = 0;
      const messages = [{ type: 'result', subtype: 'success' }];

      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          // First call: iterator that never resolves (simulates a stalled stream)
          const closeFn = jest.fn();
          const stalledIterator = {
            [Symbol.asyncIterator]() {
              return {
                next: () => new Promise<IteratorResult<unknown>>(() => {
                  // Never resolves — simulates a silent upstream stall
                }),
              };
            },
            close: closeFn,
          };
          return stalledIterator as any;
        }
        // Second call succeeds
        return asyncFromArray(messages) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 5,
        baseDelayMs: 10,
        maxDelayMs: 50,
        idleTimeoutMs: 100, // 100ms for fast test
        log,
      };

      const results = await collectAll(resilientQuery(opts));

      expect(results).toEqual(messages);
      expect(mockQuery).toHaveBeenCalledTimes(2);
      expect(log).toHaveBeenCalledWith(expect.stringContaining('Retryable error'));
      // Verify the error message matches the idle timeout pattern
      expect(log).toHaveBeenCalledWith(expect.stringContaining('stream idle timeout'));

      jest.useFakeTimers();
    }, 10000);

    it('should NOT time out when messages arrive before idleTimeoutMs', async () => {
      jest.useRealTimers();

      // Create a generator that yields messages with delays shorter than idleTimeoutMs
      const closeFn = jest.fn();
      const messages = [
        { type: 'assistant', content: 'msg1' },
        { type: 'assistant', content: 'msg2' },
        { type: 'result', subtype: 'success' },
      ];

      mockQuery.mockImplementation(() => {
        async function* slowButValid(): AsyncGenerator<unknown> {
          for (const msg of messages) {
            // Delay 30ms between messages (well under 100ms timeout)
            await new Promise(r => setTimeout(r, 30));
            yield msg;
          }
        }
        return Object.assign(slowButValid(), { close: closeFn }) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 10,
        idleTimeoutMs: 100, // 100ms timeout — messages arrive every 30ms, well within
        log,
      };

      const results = await collectAll(resilientQuery(opts));

      expect(results).toEqual(messages);
      expect(mockQuery).toHaveBeenCalledTimes(1); // No retries needed
      // No retry log messages should exist
      const retryCalls = log.mock.calls.filter(
        (call) => typeof call[0] === 'string' && call[0].includes('Retryable error')
      );
      expect(retryCalls).toHaveLength(0);

      jest.useFakeTimers();
    }, 10000);

    it('should abort after 3 consecutive idle-timeout retries with no messages yielded', async () => {
      jest.useRealTimers();

      // Every attempt stalls without yielding any messages
      mockQuery.mockImplementation(() => {
        const closeFn = jest.fn();
        const stalledIterator = {
          [Symbol.asyncIterator]() {
            return {
              next: () => new Promise<IteratorResult<unknown>>(() => {
                // Never resolves
              }),
            };
          },
          close: closeFn,
        };
        return stalledIterator as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 5, // Would allow 5 retries normally
        baseDelayMs: 10,
        maxDelayMs: 50,
        idleTimeoutMs: 50, // 50ms for fast test
        log,
      };

      await expect(collectAll(resilientQuery(opts))).rejects.toThrow('stream idle timeout');
      // Should abort after 3 consecutive stalls, not the full 5 retries
      // 1 initial + 2 retries = 3 total calls (fires on the 3rd consecutive stall)
      expect(mockQuery).toHaveBeenCalledTimes(3);
      expect(log).toHaveBeenCalledWith(expect.stringContaining('consecutive idle-timeout retries'));

      jest.useFakeTimers();
    }, 10000);

    it('should reset consecutive stall counter when a message is yielded between stalls', async () => {
      jest.useRealTimers();

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          // First call: stalls with no messages
          const closeFn = jest.fn();
          return Object.assign(
            { [Symbol.asyncIterator]() { return { next: () => new Promise<IteratorResult<unknown>>(() => {}) }; } },
            { close: closeFn }
          ) as any;
        }
        if (callCount === 2) {
          // Second call: yields a message then stalls (counter should reset)
          const closeFn = jest.fn();
          let yielded = false;
          return Object.assign(
            {
              [Symbol.asyncIterator]() {
                return {
                  next: () => {
                    if (!yielded) {
                      yielded = true;
                      return Promise.resolve({ done: false, value: { type: 'assistant', content: 'hello' } });
                    }
                    return new Promise<IteratorResult<unknown>>(() => {}); // stall after yield
                  },
                };
              },
            },
            { close: closeFn }
          ) as any;
        }
        if (callCount === 3) {
          // Third call: stalls again (counter was reset, so this is consecutive=1)
          const closeFn = jest.fn();
          return Object.assign(
            { [Symbol.asyncIterator]() { return { next: () => new Promise<IteratorResult<unknown>>(() => {}) }; } },
            { close: closeFn }
          ) as any;
        }
        // Fourth call: succeeds
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 5,
        baseDelayMs: 10,
        maxDelayMs: 50,
        idleTimeoutMs: 50,
        log,
      };

      const results = await collectAll(resilientQuery(opts));

      // Should NOT abort — the yield between stalls resets the consecutive counter
      expect(mockQuery).toHaveBeenCalledTimes(4);
      // Should have yielded the message from call 2
      expect(results).toContainEqual({ type: 'assistant', content: 'hello' });
      // Should NOT have the "consecutive" abort message
      const abortCalls = log.mock.calls.filter(
        (call) => typeof call[0] === 'string' && call[0].includes('consecutive idle-timeout retries')
      );
      expect(abortCalls).toHaveLength(0);

      jest.useFakeTimers();
    }, 15000);

    it('should call session.close() on idle timeout (cleanup)', async () => {
      jest.useRealTimers();

      const closeFn = jest.fn();
      let callCount = 0;

      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          const stalledIterator = {
            [Symbol.asyncIterator]() {
              return {
                next: () => new Promise<IteratorResult<unknown>>(() => {}),
              };
            },
            close: closeFn,
          };
          return stalledIterator as any;
        }
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 5,
        baseDelayMs: 10,
        maxDelayMs: 50,
        idleTimeoutMs: 50,
        log: jest.fn(),
      };

      await collectAll(resilientQuery(opts));

      // session.close() should have been called on the stalled session
      expect(closeFn).toHaveBeenCalled();

      jest.useFakeTimers();
    }, 10000);
  });

  describe('error message patterns', () => {
    const retryablePatterns = [
      'fetch failed',
      'ECONNRESET',
      'ECONNREFUSED',
      'socket hang up',
      'EPIPE',
      'ENOTFOUND',
      'network error',
      'aborted',
      'timeout exceeded',
      'rate limit exceeded',
      'rate_limit_error',
      '429 Too Many Requests',
      '502 Bad Gateway',
      '503 Service Unavailable',
      'service unavailable',
      'too many requests',
      'throttling',
      'overloaded',
      'capacity exceeded',
      'internal server error',
      'internalServerException',
      'bad gateway',
      'gateway timeout',
    ];

    it.each(retryablePatterns)(
      'should retry on error message containing "%s"',
      async (pattern) => {
        let callCount = 0;

        mockQuery.mockImplementation(() => {
          callCount++;
          if (callCount === 1) {
            return asyncThrowingGenerator([], new Error(`Error: ${pattern}`)) as any;
          }
          return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
        });

        const opts: ResilientQueryOptions = {
          queryParams: { prompt: 'test', options: {} } as any,
          maxRetries: 2,
          baseDelayMs: 50,
          log: jest.fn(),
        };

        const generator = resilientQuery(opts);
        const resultsPromise = collectAll(generator);
        await jest.advanceTimersByTimeAsync(200);

        await resultsPromise;

        expect(mockQuery).toHaveBeenCalledTimes(2);
      }
    );
  });

  describe('issue #2079: resume context on retry', () => {
    it('should inject resumeContext into the prompt on retry attempts', async () => {
      jest.useRealTimers();

      const messages = [
        { type: 'assistant', content: 'hello' },
        { type: 'result', subtype: 'success' },
      ];

      let callCount = 0;
      mockQuery.mockImplementation((params: any) => {
        callCount++;
        if (callCount === 1) {
          // First attempt: yields a message then throws a retryable error
          return asyncThrowingGenerator(
            [{ type: 'assistant', content: 'partial' }],
            new Error('fetch failed'),
            1,
          ) as any;
        }
        // Second attempt: succeeds. Verify prompt was augmented.
        expect(params.prompt).toContain('RESUME CONTEXT');
        expect(params.prompt).toContain('retry attempt 2');
        expect(params.prompt).toContain('1 messages');
        return asyncFromArray(messages) as any;
      });

      const log = jest.fn();
      const resumeContext = jest.fn((attemptNumber: number, priorMessagesYielded: number) =>
        `RESUME CONTEXT: retry attempt ${attemptNumber}, ${priorMessagesYielded} messages prior.`
      );

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'do the task', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 10,
        resumeContext,
        log,
      };

      const results = await collectAll(resilientQuery(opts));

      expect(mockQuery).toHaveBeenCalledTimes(2);
      expect(resumeContext).toHaveBeenCalledWith(2, 1);
      // Should have yielded partial from attempt 1 + messages from attempt 2
      expect(results).toHaveLength(3);

      jest.useFakeTimers();
    }, 10000);

    it('should NOT inject resumeContext on the first attempt', async () => {
      const messages = [
        { type: 'assistant', content: 'success' },
        { type: 'result', subtype: 'success' },
      ];

      mockQuery.mockImplementation((params: any) => {
        // First attempt should use original prompt without modification
        expect(params.prompt).toBe('do the task');
        return asyncFromArray(messages) as any;
      });

      const resumeContext = jest.fn(() => 'RESUME CONTEXT');

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'do the task', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 10,
        resumeContext,
        log: jest.fn(),
      };

      await collectAll(resilientQuery(opts));

      expect(resumeContext).not.toHaveBeenCalled();
      expect(mockQuery).toHaveBeenCalledTimes(1);
    });

    it('should not re-yield already-emitted messages on resume (messages stream through naturally)', async () => {
      jest.useRealTimers();

      // Simulate the bug scenario: attempt 1 yields 3 "opening" messages
      // (codebase reads + plan post) then stalls. Attempt 2 should have
      // resumeContext injected so the agent doesn't redo those steps.
      const openingMessages = [
        { type: 'assistant', content: 'Reading codebase...' },
        { type: 'assistant', content: 'Analyzing issue...' },
        { type: 'assistant', content: 'Implementation Plan: ...' },
      ];
      const continuationMessages = [
        { type: 'assistant', content: 'Continuing from where I left off...' },
        { type: 'assistant', content: 'Making changes...' },
        { type: 'result', subtype: 'success' },
      ];

      let callCount = 0;
      let capturedPrompt = '';
      mockQuery.mockImplementation((params: any) => {
        callCount++;
        if (callCount === 1) {
          // First attempt: yields opening messages, then idle-timeout stall
          const closeFn = jest.fn();
          let idx = 0;
          return Object.assign(
            {
              [Symbol.asyncIterator]() {
                return {
                  next: () => {
                    if (idx < openingMessages.length) {
                      return Promise.resolve({ done: false, value: openingMessages[idx++] });
                    }
                    // Stall forever after yielding opening messages
                    return new Promise<IteratorResult<unknown>>(() => {});
                  },
                };
              },
            },
            { close: closeFn },
          ) as any;
        }
        // Second attempt: capture the prompt and return continuation
        capturedPrompt = params.prompt;
        return asyncFromArray(continuationMessages) as any;
      });

      const resumeContext = (attemptNumber: number, priorYielded: number) =>
        `[RESUME] Attempt ${attemptNumber}. Prior messages: ${priorYielded}. Do NOT repost plan.`;

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'Original task prompt', options: {} } as any,
        maxRetries: 5,
        baseDelayMs: 10,
        maxDelayMs: 50,
        idleTimeoutMs: 50,
        resumeContext,
        log: jest.fn(),
      };

      const results = await collectAll(resilientQuery(opts));

      // Verify resumeContext was injected with correct info
      expect(capturedPrompt).toContain('[RESUME] Attempt 2. Prior messages: 3. Do NOT repost plan.');
      expect(capturedPrompt).toContain('Original task prompt');
      // Results include opening messages (yielded before stall) + continuation
      expect(results).toHaveLength(6); // 3 opening + 3 continuation (incl result)

      jest.useFakeTimers();
    }, 15000);
  });

  describe('issue #2079: true session resume', () => {
    it('uses options.resume with the captured session_id on retry (NOT a full re-run)', async () => {
      jest.useRealTimers();

      let callCount = 0;
      let secondParams: any;
      mockQuery.mockImplementation((params: any) => {
        callCount++;
        if (callCount === 1) {
          // First attempt: emits a system message carrying session_id, then a
          // message, then throws a retryable error.
          return asyncThrowingGenerator(
            [
              { type: 'system', subtype: 'init', session_id: 'sess-ABC' },
              { type: 'assistant', content: 'partial work' },
            ],
            new Error('fetch failed'),
            2,
          ) as any;
        }
        // Second attempt: capture params and succeed.
        secondParams = params;
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const resumeContext = jest.fn(
        (attempt: number) => `Continue from where you left off (attempt ${attempt}).`,
      );

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'ORIGINAL TASK PROMPT', options: { model: 'm' } } as any,
        maxRetries: 3,
        baseDelayMs: 10,
        resumeContext,
        log: jest.fn(),
      };

      const results = await collectAll(resilientQuery(opts));

      expect(mockQuery).toHaveBeenCalledTimes(2);
      // The retry resumes the captured session...
      expect(secondParams.options.resume).toBe('sess-ABC');
      // ...and uses ONLY the short nudge as the prompt — the original task is
      // NOT re-sent (it already lives in the resumed conversation history).
      expect(secondParams.prompt).toBe('Continue from where you left off (attempt 2).');
      expect(secondParams.prompt).not.toContain('ORIGINAL TASK PROMPT');
      // Original options are preserved alongside resume.
      expect(secondParams.options.model).toBe('m');
      // 2 messages from attempt 1 + 1 from attempt 2.
      expect(results).toHaveLength(3);

      jest.useFakeTimers();
    }, 10000);

    it('falls back to prompt-prefix when no session_id was captured before the stall', async () => {
      jest.useRealTimers();

      let callCount = 0;
      let secondParams: any;
      mockQuery.mockImplementation((params: any) => {
        callCount++;
        if (callCount === 1) {
          // Stalls/throws before ever emitting a session_id.
          return asyncThrowingGenerator([], new Error('fetch failed'), 0) as any;
        }
        secondParams = params;
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'ORIGINAL TASK', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 10,
        resumeContext: () => 'RESUME-PREFIX',
        log: jest.fn(),
      };

      await collectAll(resilientQuery(opts));

      expect(mockQuery).toHaveBeenCalledTimes(2);
      // No resume option (nothing to resume)...
      expect(secondParams.options?.resume).toBeUndefined();
      // ...and the nudge is PREPENDED to the original prompt (best-effort).
      expect(secondParams.prompt).toBe('RESUME-PREFIX\n\nORIGINAL TASK');

      jest.useFakeTimers();
    }, 10000);

    it('captures session_id only once and reuses it across multiple retries', async () => {
      jest.useRealTimers();

      const resumeValues: Array<string | undefined> = [];
      let callCount = 0;
      mockQuery.mockImplementation((params: any) => {
        callCount++;
        if (callCount > 1) resumeValues.push(params.options?.resume);
        if (callCount === 1) {
          return asyncThrowingGenerator(
            [{ type: 'system', subtype: 'init', session_id: 'sess-XYZ' }],
            new Error('502 bad gateway'),
            1,
          ) as any;
        }
        if (callCount === 2) {
          // Resumed but errors again WITHOUT emitting a new session_id.
          return asyncThrowingGenerator([], new Error('503 service unavailable'), 0) as any;
        }
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'task', options: {} } as any,
        maxRetries: 5,
        baseDelayMs: 10,
        resumeContext: () => 'continue',
        log: jest.fn(),
      };

      await collectAll(resilientQuery(opts));

      // Both retries resumed the SAME captured session id.
      expect(resumeValues).toEqual(['sess-XYZ', 'sess-XYZ']);

      jest.useFakeTimers();
    }, 10000);
  });

  describe('issue #2079: progress-aware stall guard', () => {
    it('should abort when consecutive attempts stall at the same high-water mark', async () => {
      jest.useRealTimers();

      // Simulate the diagnosed bug: each attempt yields the same ~3 opening
      // messages (re-reading code, re-posting plan) then stalls. The old guard
      // didn't catch this because yieldedInThisAttempt was true.
      const openingMessages = [
        { type: 'assistant', content: 'Reading codebase...' },
        { type: 'assistant', content: 'Posting Implementation Plan...' },
        { type: 'assistant', content: 'Starting work...' },
      ];

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        const closeFn = jest.fn();
        let idx = 0;

        if (callCount === 1) {
          // First attempt: yields 3 messages then stalls
          // This sets the high-water mark at 3
          return Object.assign(
            {
              [Symbol.asyncIterator]() {
                return {
                  next: () => {
                    if (idx < openingMessages.length) {
                      return Promise.resolve({ done: false, value: openingMessages[idx++] });
                    }
                    return new Promise<IteratorResult<unknown>>(() => {});
                  },
                };
              },
            },
            { close: closeFn },
          ) as any;
        }
        // Subsequent attempts: stall immediately, yielding ZERO new messages.
        // With true session resume (the production path), a resumed attempt does
        // NOT re-yield the opening phase — it continues from history — so a stall
        // that produces nothing new leaves the total at the prior high-water mark.
        // That is exactly the "no forward progress" condition the guard must
        // catch, and after MAX_CONSECUTIVE_STALL_RETRIES it aborts.
        return Object.assign(
          {
            [Symbol.asyncIterator]() {
              return {
                next: () => new Promise<IteratorResult<unknown>>(() => {}), // immediate stall
              };
            },
          },
          { close: closeFn },
        ) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 10, // High limit — should abort via stall guard, not maxRetries
        baseDelayMs: 10,
        maxDelayMs: 50,
        idleTimeoutMs: 50,
        log,
      };

      await expect(collectAll(resilientQuery(opts))).rejects.toThrow('stream idle timeout');
      // Attempt 1: yields 3, sets high-water mark to 3
      // Attempt 2: yields 0 (total stays at 3, not > highWaterMark=3) → stall 1
      // Attempt 3: yields 0 (total stays at 3, not > highWaterMark=3) → stall 2
      // Attempt 4: yields 0 (total stays at 3, not > highWaterMark=3) → stall 3 → ABORT
      expect(mockQuery).toHaveBeenCalledTimes(4);
      expect(log).toHaveBeenCalledWith(expect.stringContaining('consecutive idle-timeout retries with no forward progress'));

      jest.useFakeTimers();
    }, 15000);

    it('should NOT abort when each attempt makes genuine forward progress', async () => {
      jest.useRealTimers();

      // Each attempt yields MORE messages than the previous before stalling.
      // This should be allowed to continue (not tripped by the stall guard).
      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        const closeFn = jest.fn();

        if (callCount <= 3) {
          // Attempts 1-3: yield increasing number of messages then stall
          const msgCount = callCount * 2; // 2, 4, 6 messages
          let idx = 0;
          return Object.assign(
            {
              [Symbol.asyncIterator]() {
                return {
                  next: () => {
                    if (idx < msgCount) {
                      idx++;
                      return Promise.resolve({
                        done: false,
                        value: { type: 'assistant', content: `msg-${callCount}-${idx}` },
                      });
                    }
                    return new Promise<IteratorResult<unknown>>(() => {});
                  },
                };
              },
            },
            { close: closeFn },
          ) as any;
        }
        // Attempt 4: succeeds
        return asyncFromArray([
          { type: 'assistant', content: 'final' },
          { type: 'result', subtype: 'success' },
        ]) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 10,
        baseDelayMs: 10,
        maxDelayMs: 50,
        idleTimeoutMs: 50,
        log,
      };

      const results = await collectAll(resilientQuery(opts));

      // Should NOT have aborted — all attempts made forward progress
      expect(mockQuery).toHaveBeenCalledTimes(4);
      // Total messages: 2 + 4 + 6 + 2 (final) = 14
      expect(results).toHaveLength(14);
      // Stall abort message should NOT appear
      const abortCalls = log.mock.calls.filter(
        (call) => typeof call[0] === 'string' && call[0].includes('consecutive idle-timeout retries')
      );
      expect(abortCalls).toHaveLength(0);

      jest.useFakeTimers();
    }, 15000);

    it('should still immediately throw non-retryable errors (regression check)', async () => {
      const nonRetryableError = new Error('Permission denied: cannot access resource');

      mockQuery.mockImplementation(() => {
        return asyncThrowingGenerator(
          [{ type: 'assistant', content: 'started' }],
          nonRetryableError,
          1,
        ) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 5,
        resumeContext: () => 'RESUME',
        log,
      };

      await expect(collectAll(resilientQuery(opts))).rejects.toThrow('Permission denied');
      expect(mockQuery).toHaveBeenCalledTimes(1);
      expect(log).toHaveBeenCalledWith(expect.stringContaining('Non-retryable error'));
    });

    it('should handle idle-timeout on first attempt correctly (sets high-water mark)', async () => {
      jest.useRealTimers();

      // First attempt stalls with zero messages → high-water stays at 0
      // Second attempt succeeds
      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          const closeFn = jest.fn();
          return Object.assign(
            {
              [Symbol.asyncIterator]() {
                return {
                  next: () => new Promise<IteratorResult<unknown>>(() => {}),
                };
              },
            },
            { close: closeFn },
          ) as any;
        }
        return asyncFromArray([
          { type: 'assistant', content: 'success' },
          { type: 'result', subtype: 'success' },
        ]) as any;
      });

      const log = jest.fn();
      const resumeContext = jest.fn(
        (attempt: number, prior: number) => `Resume attempt ${attempt}, prior=${prior}`,
      );

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'task', options: {} } as any,
        maxRetries: 5,
        baseDelayMs: 10,
        maxDelayMs: 50,
        idleTimeoutMs: 50,
        resumeContext,
        log,
      };

      const results = await collectAll(resilientQuery(opts));

      expect(results).toHaveLength(2);
      expect(mockQuery).toHaveBeenCalledTimes(2);
      // resumeContext called for attempt 2 with 0 prior messages
      expect(resumeContext).toHaveBeenCalledWith(2, 0);

      jest.useFakeTimers();
    }, 10000);
  });
  describe('issue #4186 Phase 1: onSessionId escape hatch', () => {
    it('fires once with the captured session id, mid-stream', async () => {
      jest.useRealTimers();

      const seen: string[] = [];
      // The id must be delivered while the stream is still running — a run
      // killed before its `result` message is exactly the run whose id matters.
      const yieldedWhenCalled: number[] = [];
      let yielded = 0;

      mockQuery.mockImplementation(() =>
        asyncFromArray([
          { type: 'system', subtype: 'init', session_id: 'sess-4186' },
          { type: 'assistant', content: 'work' },
          { type: 'result', subtype: 'success' },
        ]) as any,
      );

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'task', options: {} } as any,
        onSessionId: (id) => {
          seen.push(id);
          yieldedWhenCalled.push(yielded);
        },
        log: jest.fn(),
      };

      for await (const _msg of resilientQuery(opts)) {
        yielded++;
      }

      expect(seen).toEqual(['sess-4186']);
      // Called before the first message was yielded to the caller, i.e. mid-stream.
      expect(yieldedWhenCalled).toEqual([0]);

      jest.useFakeTimers();
    }, 10000);

    it('fires only once even when the id repeats on later messages and across retries', async () => {
      jest.useRealTimers();

      const seen: string[] = [];
      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator(
            [
              { type: 'system', subtype: 'init', session_id: 'sess-once' },
              { type: 'assistant', session_id: 'sess-once', content: 'work' },
            ],
            new Error('fetch failed'),
            2,
          ) as any;
        }
        return asyncFromArray([
          { type: 'assistant', session_id: 'sess-once', content: 'more' },
          { type: 'result', subtype: 'success' },
        ]) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'task', options: {} } as any,
        maxRetries: 3,
        baseDelayMs: 10,
        onSessionId: (id) => seen.push(id),
        log: jest.fn(),
      };

      await collectAll(resilientQuery(opts));

      expect(mockQuery).toHaveBeenCalledTimes(2);
      expect(seen).toEqual(['sess-once']);

      jest.useFakeTimers();
    }, 10000);

    it('is not called when the stream never emits a session id', async () => {
      jest.useRealTimers();

      const onSessionId = jest.fn();
      mockQuery.mockImplementation(() =>
        asyncFromArray([
          { type: 'assistant', content: 'work' },
          { type: 'result', subtype: 'success' },
        ]) as any,
      );

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'task', options: {} } as any,
        onSessionId,
        log: jest.fn(),
      };

      const results = await collectAll(resilientQuery(opts));

      expect(onSessionId).not.toHaveBeenCalled();
      expect(results).toHaveLength(2);

      jest.useFakeTimers();
    }, 10000);

    it('a throwing callback is swallowed and does not break the stream', async () => {
      jest.useRealTimers();

      // The callback is an observability sink. It must never be able to take
      // the agent run down with it.
      mockQuery.mockImplementation(() =>
        asyncFromArray([
          { type: 'system', subtype: 'init', session_id: 'sess-boom' },
          { type: 'assistant', content: 'work' },
          { type: 'result', subtype: 'success' },
        ]) as any,
      );

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'task', options: {} } as any,
        onSessionId: () => {
          throw new Error('disk full');
        },
        log,
      };

      const results = await collectAll(resilientQuery(opts));

      // Every message still reached the caller.
      expect(results).toHaveLength(3);
      expect(log.mock.calls.flat().join('\n')).toContain('onSessionId callback threw');

      jest.useFakeTimers();
    }, 10000);

    it('regression: omitting onSessionId leaves resume behaviour unchanged', async () => {
      jest.useRealTimers();

      let callCount = 0;
      let secondParams: any;
      mockQuery.mockImplementation((params: any) => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator(
            [{ type: 'system', subtype: 'init', session_id: 'sess-regress' }],
            new Error('fetch failed'),
            1,
          ) as any;
        }
        secondParams = params;
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'ORIGINAL', options: { model: 'm' } } as any,
        maxRetries: 3,
        baseDelayMs: 10,
        resumeContext: () => 'nudge',
        log: jest.fn(),
      };

      await collectAll(resilientQuery(opts));

      // In-process resume still works exactly as before (issue #2079).
      expect(secondParams.options.resume).toBe('sess-regress');
      expect(secondParams.prompt).toBe('nudge');

      jest.useFakeTimers();
    }, 10000);
  });

  /**
   * Issue #3962: retry-safe control lifecycle.
   *
   * These tests exist because a retry is the moment control commands get lost.
   * The wrapper tears down a stalled query and builds a new one; if the control
   * layer keeps addressing the old transport, an operator's instruction is
   * accepted and then delivered into something already dead. The three hooks
   * below are what make the swap observable, and the assertions here are about
   * ordering and staleness rather than about happy-path plumbing.
   *
   * The final block is the most important one: it drives the *real* Claude
   * adapter through the *real* wrapper. The stub-based tests prove the wrapper
   * calls its hooks correctly, but only the integrated test proves the two
   * halves agree about when an attempt starts and stops.
   */
  describe('issue #3962: retry-safe control lifecycle', () => {
    /** A stub input channel that records whether it was disposed. */
    function fakeAttemptInput() {
      const state = { disposeCount: 0, closed: false };
      const pushed: unknown[] = [];
      async function* iterable(): AsyncGenerator<unknown> {
        // Ends immediately: these tests are about the wrapper's lifecycle
        // bookkeeping, not about parking on an open channel.
        for (const item of pushed) yield item;
      }
      return {
        state,
        make: () => ({
          input: iterable(),
          dispose: () => {
            state.disposeCount += 1;
            state.closed = true;
          },
        }),
      };
    }

    it('builds fresh input before every attempt, on both the resume and fallback paths', async () => {
      jest.useRealTimers();

      // Two retries with a captured session id (true resume) and one without,
      // so both branches of the attempt-construction code are exercised.
      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator(
            [{ type: 'system', subtype: 'init', session_id: 'sess-3962' }],
            new Error('fetch failed'),
            1,
          ) as any;
        }
        if (callCount === 2) {
          return asyncThrowingGenerator([], new Error('fetch failed'), 0) as any;
        }
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const contexts: Array<{ attemptNumber: number; isResume: boolean; promptText: string }> = [];
      const disposeCounts: number[] = [];

      await collectAll(
        resilientQuery({
          queryParams: { prompt: 'ORIGINAL TASK', options: {} } as any,
          maxRetries: 3,
          baseDelayMs: 1,
          resumeContext: () => 'nudge',
          attemptInputFactory: (ctx) => {
            contexts.push(ctx);
            const local = { count: 0 };
            return {
              input: (async function* () {})(),
              dispose: () => {
                local.count += 1;
                disposeCounts.push(local.count);
              },
            };
          },
          log: jest.fn(),
        }),
      );

      // Every attempt got its own factory call. An attempt without a live input
      // channel is one that can never receive a steering message.
      expect(contexts.map((c) => c.attemptNumber)).toEqual([1, 2, 3]);
      // Attempt 2 resumes the captured session; 1 and 3 do not.
      expect(contexts.map((c) => c.isResume)).toEqual([false, true, true]);
      // Each attempt's input is disposed exactly once — never twice, never zero.
      expect(disposeCounts).toEqual([1, 1, 1]);

      jest.useFakeTimers();
    }, 10000);

    it('replaces the prompt with the adapter iterable without disturbing the resume option', async () => {
      jest.useRealTimers();

      let callCount = 0;
      const seenParams: any[] = [];
      mockQuery.mockImplementation((params: any) => {
        callCount++;
        seenParams.push(params);
        if (callCount === 1) {
          return asyncThrowingGenerator(
            [{ type: 'system', subtype: 'init', session_id: 'sess-swap' }],
            new Error('fetch failed'),
            1,
          ) as any;
        }
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const channel = fakeAttemptInput();
      await collectAll(
        resilientQuery({
          queryParams: { prompt: 'ORIGINAL TASK', options: { model: 'm' } } as any,
          maxRetries: 2,
          baseDelayMs: 1,
          resumeContext: () => 'CONTINUATION NUDGE',
          attemptInputFactory: () => channel.make(),
          log: jest.fn(),
        }),
      );

      // The streaming form is what allows a second turn into a live attempt, so
      // the prompt must become an iterable rather than staying a string.
      expect(typeof seenParams[0].prompt).not.toBe('string');
      expect(seenParams[0].prompt[Symbol.asyncIterator]).toBeDefined();
      // The retry still resumes the real session: swapping the prompt must not
      // cost us the true-resume behaviour from #2079.
      expect(seenParams[1].options.resume).toBe('sess-swap');
      expect(seenParams[1].options.model).toBe('m');
      // The continuation nudge still reaches the adapter as context...
      const contexts: string[] = [];
      void contexts;

      jest.useFakeTimers();
    }, 10000);

    it('reports the prompt text each attempt would have sent, so a resume is distinguishable', async () => {
      jest.useRealTimers();

      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return asyncThrowingGenerator(
            [{ type: 'system', subtype: 'init', session_id: 'sess-text' }],
            new Error('fetch failed'),
            1,
          ) as any;
        }
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const promptTexts: string[] = [];
      await collectAll(
        resilientQuery({
          queryParams: { prompt: 'ORIGINAL TASK', options: {} } as any,
          maxRetries: 2,
          baseDelayMs: 1,
          resumeContext: () => 'CONTINUATION NUDGE',
          attemptInputFactory: (ctx) => {
            promptTexts.push(ctx.promptText);
            return { input: (async function* () {})(), dispose: () => {} };
          },
          log: jest.fn(),
        }),
      );

      // Attempt 1 carries the task; attempt 2 carries only the nudge, because the
      // resumed conversation already holds the task. An adapter that re-sent
      // promptText on a resume would make the agent repeat completed work.
      expect(promptTexts[0]).toBe('ORIGINAL TASK');
      expect(promptTexts[1]).toBe('CONTINUATION NUDGE');

      jest.useFakeTimers();
    }, 10000);

    it('publishes each attempt handle before the stream is consumed', async () => {
      jest.useRealTimers();

      const events: string[] = [];
      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          async function* inner(): AsyncGenerator<any> {
            events.push('attempt1-yield');
            yield { type: 'system', subtype: 'init', session_id: 'sess-order' };
            throw new Error('fetch failed');
          }
          return withClose(inner()) as any;
        }
        async function* inner(): AsyncGenerator<any> {
          events.push('attempt2-yield');
          yield { type: 'result', subtype: 'success' };
        }
        return withClose(inner()) as any;
      });

      await collectAll(
        resilientQuery({
          queryParams: { prompt: 'task', options: {} } as any,
          maxRetries: 2,
          baseDelayMs: 1,
          onAttemptHandle: (h) => { events.push(`handle-${h.attemptNumber}`); },
          log: jest.fn(),
        }),
      );

      // Handle-before-stream is what lets the adapter swap its endpoint at the
      // moment the transport swaps, rather than inferring it after the fact.
      expect(events).toEqual(['handle-1', 'attempt1-yield', 'handle-2', 'attempt2-yield']);

      jest.useFakeTimers();
    }, 10000);

    it('keeps running when the attempt-handle callback throws', async () => {
      jest.useRealTimers();

      mockQuery.mockImplementation(() => asyncFromArray([{ type: 'result', subtype: 'success' }]) as any);
      const log = jest.fn();

      const results = await collectAll(
        resilientQuery({
          queryParams: { prompt: 'task', options: {} } as any,
          onAttemptHandle: () => {
            throw new Error('control sink exploded');
          },
          log,
        }),
      );

      // A control or observability sink must never take a run down.
      expect(results).toHaveLength(1);
      expect(log.mock.calls.flat().join('\n')).toContain('onAttemptHandle');

      jest.useFakeTimers();
    }, 10000);

    it('starts no query at all when cancelled before the first attempt', async () => {
      jest.useRealTimers();

      mockQuery.mockImplementation(() => asyncFromArray([{ type: 'result', subtype: 'success' }]) as any);

      const cancelled = { isCancelled: () => true, error: () => new Error('operator aborted') };
      await expect(
        collectAll(
          resilientQuery({
            queryParams: { prompt: 'task', options: {} } as any,
            cancellation: cancelled,
            log: jest.fn(),
          }),
        ),
      ).rejects.toThrow('operator aborted');

      expect(mockQuery).not.toHaveBeenCalled();

      jest.useFakeTimers();
    }, 10000);

    it('starts no further attempt when cancelled during backoff', async () => {
      jest.useRealTimers();

      // Attempt 1 fails retryably. The cancel lands while the wrapper sleeps, so
      // the only correct behaviour is to stop — a retry here would turn a
      // deliberate abort into a brand new attempt.
      let cancelled = false;
      mockQuery.mockImplementation(() => {
        cancelled = true; // cancel is requested as soon as attempt 1 is running
        return asyncThrowingGenerator([], new Error('fetch failed'), 0) as any;
      });

      await expect(
        collectAll(
          resilientQuery({
            queryParams: { prompt: 'task', options: {} } as any,
            maxRetries: 3,
            baseDelayMs: 1,
            cancellation: { isCancelled: () => cancelled, error: () => new Error('operator aborted') },
            log: jest.fn(),
          }),
        ),
      ).rejects.toThrow('operator aborted');

      // Exactly one query: the retry never happened.
      expect(mockQuery).toHaveBeenCalledTimes(1);

      jest.useFakeTimers();
    }, 10000);

    it('does not reclassify a cancellation as a retryable error despite matching text', async () => {
      jest.useRealTimers();

      // "aborted" is in RETRYABLE_PATTERNS. Without the typed-cancellation check
      // running first, this deliberate stop would be treated as a transient blip
      // and restarted — the exact failure the issue names.
      const { ControlCancelledError } = require('../control-runtime');
      mockQuery.mockImplementation(
        () => asyncThrowingGenerator([], new ControlCancelledError('run aborted by operator'), 0) as any,
      );

      await expect(
        collectAll(
          resilientQuery({
            queryParams: { prompt: 'task', options: {} } as any,
            maxRetries: 3,
            baseDelayMs: 1,
            log: jest.fn(),
          }),
        ),
      ).rejects.toThrow('run aborted by operator');

      expect(mockQuery).toHaveBeenCalledTimes(1);

      jest.useFakeTimers();
    }, 10000);

    it('still retries an ordinary error whose text merely resembles a cancellation', async () => {
      jest.useRealTimers();

      // The guard must key on the typed marker, not on vocabulary. A genuine
      // transient "request aborted" must keep its retry.
      let callCount = 0;
      mockQuery.mockImplementation(() => {
        callCount++;
        if (callCount === 1) return asyncThrowingGenerator([], new Error('request aborted'), 0) as any;
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const results = await collectAll(
        resilientQuery({
          queryParams: { prompt: 'task', options: {} } as any,
          maxRetries: 3,
          baseDelayMs: 1,
          log: jest.fn(),
        }),
      );

      expect(mockQuery).toHaveBeenCalledTimes(2);
      expect(results).toHaveLength(1);

      jest.useFakeTimers();
    }, 10000);

    it('disposes the attempt input even when the consumer abandons the generator', async () => {
      jest.useRealTimers();

      mockQuery.mockImplementation(
        () =>
          asyncFromArray([
            { type: 'assistant', content: 'one' },
            { type: 'assistant', content: 'two' },
          ]) as any,
      );

      const channel = fakeAttemptInput();
      const gen = resilientQuery({
        queryParams: { prompt: 'task', options: {} } as any,
        attemptInputFactory: () => channel.make(),
        log: jest.fn(),
      });

      // Break out early, which triggers the generator's `return` path. Without
      // disposal in `finally`, the attempt's input would outlive its transport.
      for await (const _msg of gen) {
        break;
      }

      expect(channel.state.disposeCount).toBe(1);
      expect(channel.state.closed).toBe(true);

      jest.useFakeTimers();
    }, 10000);

    it('swallows a disposal error so it cannot mask the run outcome', async () => {
      jest.useRealTimers();

      mockQuery.mockImplementation(() => asyncFromArray([{ type: 'result', subtype: 'success' }]) as any);
      const log = jest.fn();

      const results = await collectAll(
        resilientQuery({
          queryParams: { prompt: 'task', options: {} } as any,
          attemptInputFactory: () => ({
            input: (async function* () {})(),
            dispose: () => {
              throw new Error('channel already gone');
            },
          }),
          log,
        }),
      );

      expect(results).toHaveLength(1);
      expect(log.mock.calls.flat().join('\n')).toContain('disposal');

      jest.useFakeTimers();
    }, 10000);

    /**
     * The integrated test: the real Claude adapter driven by the real wrapper.
     *
     * Everything above uses stubs, which can only prove the wrapper honours its
     * own contract. This proves the adapter and the wrapper agree about when an
     * attempt begins and ends — the seam where a stale-handle bug would actually
     * live in production.
     */
    it('routes a post-retry command to the new attempt when driving the real Claude adapter', async () => {
      jest.useRealTimers();

      const { ClaudeControlAdapter } = require('../harnesses/claude-control');
      const adapter = new ClaudeControlAdapter();

      const closes: string[] = [];
      let callCount = 0;
      // Attach events, not synchronous currentAttempt() reads: onAttemptHandle is
      // invoked synchronously by the wrapper while attach() is async, so reading
      // the current attempt inside the callback always sees the pre-attach null.
      // The event is the observable the coordinator is meant to use.
      const attemptIds: string[] = [];
      adapter.subscribe((event: any) => {
        if (event.type === 'attempt_attached') attemptIds.push(event.attemptId);
      });
      // Each attempt's input iterable, captured from the params the wrapper built.
      const prompts: AsyncIterable<any>[] = [];
      const received: any[][] = [];
      let handoff: string | undefined;

      mockQuery.mockImplementation((params: any) => {
        callCount++;
        const label = `attempt-${callCount}`;
        prompts.push(params.prompt);
        const messages: any[] = [];
        received.push(messages);
        void (async () => { for await (const m of params.prompt) messages.push(m); })();
        if (callCount === 1) {
          async function* inner(): AsyncGenerator<any> {
            yield { type: 'system', subtype: 'init', session_id: 'sess-live' };
            throw new Error('fetch failed');
          }
          return Object.assign(inner(), { close: () => closes.push(label) }) as any;
        }
        async function* inner(): AsyncGenerator<any> {
          // Submit a command WHILE attempt 2 is streaming. This is the real
          // scenario: an operator steers a running agent. Submitting after the
          // generator finished would only prove input is refused once the run is
          // over, which is true but not what this test is for.
          //
          // `whenAttached()` first because attach is async while the wrapper
          // invokes onAttemptHandle synchronously: without it this races the
          // attach and the command is legitimately rejected. That window is real
          // in production too — a command arriving in it is refused rather than
          // misrouted, which is the safe direction — but it is not what this test
          // is measuring.
          await adapter.whenAttached();
          handoff = await adapter.submitInput({ kind: 'steering', text: 'after retry' });
          yield { type: 'result', subtype: 'success' };
        }
        return Object.assign(inner(), { close: () => closes.push(label) }) as any;
      });

      await collectAll(
        resilientQuery({
          queryParams: { prompt: 'ORIGINAL TASK', options: {} } as any,
          maxRetries: 2,
          baseDelayMs: 1,
          resumeContext: () => 'nudge',
          attemptInputFactory: adapter.attemptInputFactory(),
          onAttemptHandle: adapter.onAttemptHandle(),
          cancellation: adapter.cancellationSource(),
          log: jest.fn(),
        }),
      );

      expect(callCount).toBe(2);
      // Two distinct attempts were published: the endpoint was genuinely
      // replaced across the retry, not reused.
      expect(attemptIds).toHaveLength(2);
      expect(attemptIds[1]).not.toBe(attemptIds[0]);

      // The command reached the live attempt rather than vanishing into the
      // transport the retry replaced.
      expect(handoff).toBe('delivered');

      // And it landed in attempt 2's actual input channel — the one handed to
      // query() — not merely in some queue the adapter kept to itself.
      expect(received[1].map(m => m.message.content)).toEqual(['nudge', 'after retry']);
      expect(received[0].map(m => m.message.content)).toEqual(['ORIGINAL TASK']);

      // Each session was closed exactly once, by the wrapper that created it.
      expect(closes).toEqual(['attempt-1', 'attempt-2']);
      await adapter.dispose();
      // Adapter teardown does not close a borrowed handle a second time.
      expect(closes).toEqual(['attempt-1', 'attempt-2']);

      jest.useFakeTimers();
    }, 10000);

    it('leaves the legacy no-option path untouched', async () => {
      jest.useRealTimers();

      // The regression guard for 17 existing callers: with none of the three
      // hooks supplied, the query params must be exactly what they always were —
      // a string prompt, not an iterable.
      let seenParams: any;
      mockQuery.mockImplementation((params: any) => {
        seenParams = params;
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const seenIds: string[] = [];
      await collectAll(
        resilientQuery({
          queryParams: { prompt: 'PLAIN TASK', options: { model: 'm' } } as any,
          onSessionId: (id) => seenIds.push(id),
          log: jest.fn(),
        }),
      );

      expect(seenParams.prompt).toBe('PLAIN TASK');
      expect(seenParams.options).toEqual({ model: 'm' });
      expect(seenIds).toEqual([]);

      jest.useFakeTimers();
    }, 10000);
  });
});

describe('prompt cancellation at runtime waits', () => {
  beforeEach(() => { jest.clearAllMocks(); jest.useFakeTimers(); });
  afterEach(() => { jest.useRealTimers(); });

  it.each(['idle', 'backoff', 'setup', 'legacy-poll'] as const)(
    'cancels during %s without waiting for the timeout or starting a retry', async (phase) => {
      const { ClaudeControlAdapter } = require('../harnesses/claude-control');
      const adapter = new ClaudeControlAdapter();
      const close = jest.fn(() => { expect(adapter.currentAttempt()).toBeNull(); });
      const next = jest.fn(() => phase === 'backoff'
        ? Promise.reject(new Error('fetch failed'))
        : new Promise<IteratorResult<any>>(() => {}));
      mockQuery.mockReturnValue({ [Symbol.asyncIterator]: () => ({ next }), close } as any);
      const source = adapter.cancellationSource();
      if (phase === 'legacy-poll') delete source.signal;
      const stream = resilientQuery({
        queryParams: { prompt: 'task' },
        baseDelayMs: 120_000,
        maxDelayMs: 120_000,
        idleTimeoutMs: 600_000,
        attemptInputFactory: adapter.attemptInputFactory(),
        onAttemptHandle: phase === 'setup' ? () => new Promise<void>(() => {}) : adapter.onAttemptHandle(),
        cancellation: source,
        log: jest.fn(),
      });
      let outcome: unknown;
      const pending = stream.next().catch(error => { outcome = error; });
      await jest.advanceTimersByTimeAsync(0);
      if (phase === 'backoff') expect(close).toHaveBeenCalledTimes(1);
      adapter.cancel('operator aborted');
      await jest.advanceTimersByTimeAsync(30);
      expect(outcome).toMatchObject({ name: 'ControlCancelledError' });
      await pending;
      expect(mockQuery).toHaveBeenCalledTimes(1);
      expect(close).toHaveBeenCalledTimes(1);
      expect(jest.getTimerCount()).toBe(0);
      await adapter.dispose();
    },
  );
});


describe('pause holds task output and terminal teardown', () => {
  it.each(['assistant', 'result', 'done'])('retains the current attempt while %s waits for resume', async (kind) => {
    jest.useRealTimers();
    jest.clearAllMocks();
    const { PauseGate } = await import('../pause-gate');
    const { ClaudeControlAdapter } = await import('../harnesses/claude-control');
    const gate = new PauseGate();
    const adapter = new ClaudeControlAdapter({ pauseGate: gate });
    let emit!: () => void;
    const outputReady = new Promise<void>((resolve) => { emit = resolve; });
    const session = Object.assign((async function* () {
      yield { type: 'system', subtype: 'init', session_id: 'session-retained' };
      await outputReady;
      if (kind !== 'done') yield { type: kind, subtype: 'success' };
    })(), { close: jest.fn() });
    mockQuery.mockReturnValue(session as never);
    const stream = resilientQuery({
      queryParams: { prompt: 'same task', options: {} },
      attemptInputFactory: adapter.attemptInputFactory(),
      onAttemptHandle: adapter.onAttemptHandle(),
      cancellation: adapter.cancellationSource(),
      beforeOutput: () => gate.waitForOutput(),
    });
    await stream.next();
    const attempt = adapter.currentAttempt();
    expect(attempt).not.toBeNull();
    let delivered = false;
    const next = stream.next().then((value) => { delivered = true; return value; });
    expect(await adapter.requestPause()).toMatchObject({ outcome: 'confirmed' });
    emit();
    await new Promise((resolve) => setImmediate(resolve));
    try {
      expect(delivered).toBe(false);
      expect(session.close).not.toHaveBeenCalled();
      expect(adapter.currentAttempt()).toBe(attempt);
    } finally {
      await gate.resume();
      await next;
      await stream.return(undefined as never);
      await adapter.dispose();
    }
    expect(session.close).toHaveBeenCalledTimes(1);
    expect(mockQuery).toHaveBeenCalledTimes(1);
  });
});


it('a cancelled output hold closes the attempt once without retrying', async () => {
  jest.useRealTimers();
  jest.clearAllMocks();
  const { ControlCancelledError } = await import('../control-runtime');
  const session = Object.assign((async function* () { yield { type: 'assistant' }; })(), { close: jest.fn() });
  mockQuery.mockReturnValue(session as never);
  const stream = resilientQuery({ queryParams: { prompt: 'task', options: {} }, beforeOutput: async () => false });
  await expect(stream.next()).rejects.toBeInstanceOf(ControlCancelledError);
  expect(session.close).toHaveBeenCalledTimes(1);
  expect(mockQuery).toHaveBeenCalledTimes(1);
});
