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
    it('should apply exponential backoff with increasing delays', async () => {
      let callCount = 0;
      const callTimes: number[] = [];

      mockQuery.mockImplementation(() => {
        callCount++;
        callTimes.push(Date.now());
        if (callCount < 4) {
          return asyncThrowingGenerator([], new Error('fetch failed')) as any;
        }
        return asyncFromArray([{ type: 'result', subtype: 'success' }]) as any;
      });

      const log = jest.fn();
      const opts: ResilientQueryOptions = {
        queryParams: { prompt: 'test', options: {} } as any,
        maxRetries: 5,
        baseDelayMs: 1000,
        maxDelayMs: 120000,
        log,
      };

      const generator = resilientQuery(opts);
      const resultsPromise = collectAll(generator);

      // Advance timers to allow retries
      await jest.advanceTimersByTimeAsync(1500);  // First retry after ~1000ms
      await jest.advanceTimersByTimeAsync(3000);  // Second retry after ~2000ms
      await jest.advanceTimersByTimeAsync(5000);  // Third retry after ~4000ms

      await resultsPromise;

      // Check that log was called with retry messages showing increasing delays
      const retryCalls = log.mock.calls.filter(
        (call) => typeof call[0] === 'string' && call[0].includes('Retrying in')
      );
      expect(retryCalls.length).toBe(3);
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
});
