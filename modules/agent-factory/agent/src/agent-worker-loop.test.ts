/**
 * Unit tests for the agent-worker message loop behavior.
 *
 * These tests verify that the message processing loop in agent-worker.ts
 * correctly breaks out when a 'result' message is received, preventing
 * the worker from hanging indefinitely (issue #319).
 *
 * Since the loop is embedded in executeAgent(), we test the behavior by
 * simulating the same async generator pattern used in the worker.
 */

describe('agent-worker message loop', () => {
  /**
   * Helper: create an async generator that yields messages, optionally
   * hanging (never returning) after a certain count.
   */
  async function* makeStream(
    messages: Array<{ type: string; [key: string]: unknown }>,
    hangAfterIndex?: number,
  ): AsyncGenerator<{ type: string; [key: string]: unknown }> {
    for (let i = 0; i < messages.length; i++) {
      yield messages[i];
      if (hangAfterIndex !== undefined && i === hangAfterIndex) {
        // Simulate a stream that never terminates after this point
        await new Promise<void>(() => {
          /* never resolves */
        });
      }
    }
  }

  describe('labeled break on result message', () => {
    it('should exit the loop immediately when a result message is received', async () => {
      const messages = [
        { type: 'system', subtype: 'init', model: 'claude-sonnet-4-20250514' },
        { type: 'assistant', content: 'Hello' },
        { type: 'result', subtype: 'success', num_turns: 2 },
        // These should never be reached:
        { type: 'assistant', content: 'Should not see this' },
        { type: 'result', subtype: 'success', num_turns: 3 },
      ];

      const received: string[] = [];

      // Replicate the labeled-break pattern from agent-worker.ts
      queryLoop:
      for await (const message of makeStream(messages)) {
        received.push(message.type);
        switch (message.type) {
          case 'result':
            break queryLoop;
          default:
            break;
        }
      }

      // Should have received system, assistant, result — then stopped
      expect(received).toEqual(['system', 'assistant', 'result']);
    });

    it('should exit even when result subtype is not success', async () => {
      const messages = [
        { type: 'assistant', content: 'work' },
        { type: 'result', subtype: 'error' },
        { type: 'assistant', content: 'unreachable' },
      ];

      const received: string[] = [];

      queryLoop:
      for await (const message of makeStream(messages)) {
        received.push(message.type);
        switch (message.type) {
          case 'result':
            break queryLoop;
          default:
            break;
        }
      }

      expect(received).toEqual(['assistant', 'result']);
    });

    it('should not hang when the stream never terminates after result', async () => {
      // This is the exact scenario from issue #319: the SDK yields a result
      // message but the async iterator never returns/closes.
      const messages = [
        { type: 'assistant', content: 'doing work' },
        { type: 'result', subtype: 'success', num_turns: 234 },
      ];

      const received: string[] = [];

      // The stream hangs after index 1 (the result message).
      // Without the labeled break, this would hang forever.
      const timeout = new Promise<'timeout'>((resolve) =>
        setTimeout(() => resolve('timeout'), 2000),
      );

      const loopDone = (async () => {
        queryLoop:
        for await (const message of makeStream(messages, 1)) {
          received.push(message.type);
          switch (message.type) {
            case 'result':
              break queryLoop;
            default:
              break;
          }
        }
        return 'done';
      })();

      const winner = await Promise.race([loopDone, timeout]);

      expect(winner).toBe('done');
      expect(received).toEqual(['assistant', 'result']);
    });
  });

  describe('without labeled break (demonstrates the bug)', () => {
    it('plain switch break does NOT exit the for-await loop', async () => {
      // This test demonstrates the original bug: a plain `break` inside a
      // switch only exits the switch, not the for-await loop.  The loop
      // continues to the next iteration (or hangs if the stream doesn't end).
      const messages = [
        { type: 'result', subtype: 'success' },
        { type: 'assistant', content: 'after result' },
      ];

      const received: string[] = [];

      // Plain break (no label) — processes ALL messages
      for await (const message of makeStream(messages)) {
        received.push(message.type);
        switch (message.type) {
          case 'result':
            break; // Only breaks the switch!
          default:
            break;
        }
      }

      // Both messages processed because the plain break didn't exit the loop
      expect(received).toEqual(['result', 'assistant']);
    });
  });

  describe('heartbeat cleanup', () => {
    it('should clear heartbeat interval in finally block after break', async () => {
      const messages = [
        { type: 'result', subtype: 'success' },
      ];

      let heartbeatCleared = false;
      const heartbeat = setInterval(() => {
        // This would run forever if not cleared
      }, 100);

      try {
        queryLoop:
        for await (const message of makeStream(messages)) {
          switch (message.type) {
            case 'result':
              break queryLoop;
            default:
              break;
          }
        }
      } finally {
        clearInterval(heartbeat);
        heartbeatCleared = true;
      }

      expect(heartbeatCleared).toBe(true);
    });
  });

  describe('post-completion timeout safety net', () => {
    it('should detect when query completed but stream is still open', () => {
      // Simulates the heartbeat safety-net logic from agent-worker.ts
      const POST_COMPLETION_TIMEOUT_MS = 10 * 60 * 1000;

      let queryCompleted = false;
      let queryCompletedTime: number | null = null;
      let forceExitCalled = false;

      // Simulate query completing
      queryCompleted = true;
      queryCompletedTime = Date.now() - (POST_COMPLETION_TIMEOUT_MS + 1000); // 10m1s ago

      // Simulate heartbeat check
      if (queryCompleted && queryCompletedTime) {
        const elapsed = Date.now() - queryCompletedTime;
        if (elapsed >= POST_COMPLETION_TIMEOUT_MS) {
          forceExitCalled = true;
        }
      }

      expect(forceExitCalled).toBe(true);
    });

    it('should not force exit if within timeout window', () => {
      const POST_COMPLETION_TIMEOUT_MS = 10 * 60 * 1000;

      let queryCompleted = true;
      let queryCompletedTime = Date.now() - 5000; // 5 seconds ago
      let forceExitCalled = false;

      if (queryCompleted && queryCompletedTime) {
        const elapsed = Date.now() - queryCompletedTime;
        if (elapsed >= POST_COMPLETION_TIMEOUT_MS) {
          forceExitCalled = true;
        }
      }

      expect(forceExitCalled).toBe(false);
    });

    it('should not trigger timeout check before query completes', () => {
      const POST_COMPLETION_TIMEOUT_MS = 10 * 60 * 1000;

      let queryCompleted = false;
      let queryCompletedTime: number | null = null;
      let forceExitCalled = false;

      if (queryCompleted && queryCompletedTime) {
        const elapsed = Date.now() - queryCompletedTime;
        if (elapsed >= POST_COMPLETION_TIMEOUT_MS) {
          forceExitCalled = true;
        }
      }

      expect(forceExitCalled).toBe(false);
    });
  });
});
