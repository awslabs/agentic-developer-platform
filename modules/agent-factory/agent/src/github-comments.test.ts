/**
 * Tests for github-comments.ts — LiveStatusComment edit-in-place pattern.
 */
import {
  LiveStatusComment,
  StageDefinition,
  createWorkerStages,
  createSkillAgentStages,
} from './github-comments';

// ─── Mock fetch ──────────────────────────────────────────────────────────────

const mockFetch = jest.fn();
(global as any).fetch = mockFetch;

function mockFetchResponse(status: number, body: Record<string, unknown> = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function makeOptions(overrides: Record<string, unknown> = {}) {
  return {
    owner: 'test-org',
    repo: 'test-repo',
    issueNumber: 42,
    token: 'ghp_test123',
    minUpdateIntervalMs: 5000,
    log: jest.fn(),
    ...overrides,
  };
}

function makeStages(): StageDefinition[] {
  return [
    { label: 'Stage 1', status: 'pending' },
    { label: 'Stage 2', status: 'pending' },
    { label: 'Stage 3', status: 'pending' },
  ];
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('LiveStatusComment', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    mockFetch.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  describe('post()', () => {
    it('creates a comment and stores the comment ID', async () => {
      mockFetch.mockResolvedValueOnce(mockFetchResponse(201, { id: 999 }));

      const comment = new LiveStatusComment(makeStages(), makeOptions());
      const id = await comment.post();

      expect(id).toBe(999);
      expect(comment.getCommentId()).toBe(999);
      expect(mockFetch).toHaveBeenCalledTimes(1);

      const [url, opts] = mockFetch.mock.calls[0];
      expect(url).toBe('https://api.github.com/repos/test-org/test-repo/issues/42/comments');
      expect(opts.method).toBe('POST');
      expect(opts.headers.Authorization).toBe('token ghp_test123');

      const body = JSON.parse(opts.body);
      expect(body.body).toContain('Agent running');
      expect(body.body).toContain('[ ] Stage 1');
      expect(body.body).toContain('[ ] Stage 2');
      expect(body.body).toContain('[ ] Stage 3');
    });

    it('throws on API failure', async () => {
      mockFetch.mockResolvedValueOnce(mockFetchResponse(403, { message: 'Forbidden' }));

      const comment = new LiveStatusComment(makeStages(), makeOptions());
      await expect(comment.post()).rejects.toThrow('Failed to post status comment: 403');
    });
  });

  describe('transition()', () => {
    it('updates stage status and schedules a comment update', async () => {
      mockFetch
        .mockResolvedValueOnce(mockFetchResponse(201, { id: 100 }))  // post
        .mockResolvedValue(mockFetchResponse(200));                    // updates

      const comment = new LiveStatusComment(makeStages(), makeOptions());
      await comment.post();
      mockFetch.mockClear();

      // Advance time past min interval
      jest.advanceTimersByTime(5001);

      comment.transition(0, 'in_progress', 'Starting stage 1');

      // Should fire immediately since we're past the min interval
      await Promise.resolve(); // let microtask queue flush
      expect(mockFetch).toHaveBeenCalledTimes(1);

      const [url, opts] = mockFetch.mock.calls[0];
      expect(url).toContain('/issues/comments/100');
      expect(opts.method).toBe('PATCH');

      const body = JSON.parse(opts.body);
      expect(body.body).toContain('[~] Stage 1');
      expect(body.body).toContain('running');
      expect(body.body).toContain('Latest: Starting stage 1');
    });

    it('rate-limits updates to minUpdateIntervalMs', async () => {
      mockFetch
        .mockResolvedValueOnce(mockFetchResponse(201, { id: 100 }))
        .mockResolvedValue(mockFetchResponse(200));

      const comment = new LiveStatusComment(makeStages(), makeOptions({ minUpdateIntervalMs: 5000 }));
      await comment.post();
      mockFetch.mockClear();

      // Rapid transitions without advancing timers
      comment.transition(0, 'in_progress');
      comment.transition(0, 'complete');
      comment.transition(1, 'in_progress');

      // Only one update should be scheduled (pending)
      await Promise.resolve();

      // Advance past the debounce window
      jest.advanceTimersByTime(5000);
      await Promise.resolve();

      // Should have made exactly 1 PATCH call (debounced)
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      // Should reflect the LATEST state
      expect(body.body).toContain('[x] Stage 1');
      expect(body.body).toContain('[~] Stage 2');
    });

    it('ignores out-of-bounds stage index', async () => {
      mockFetch.mockResolvedValueOnce(mockFetchResponse(201, { id: 100 }));

      const comment = new LiveStatusComment(makeStages(), makeOptions());
      await comment.post();

      // Should not throw
      comment.transition(-1, 'complete');
      comment.transition(99, 'complete');

      const stages = comment.getStages();
      expect(stages.every(s => s.status === 'pending')).toBe(true);
    });

    it('records startedAt on in_progress and completedAt on complete', async () => {
      mockFetch.mockResolvedValueOnce(mockFetchResponse(201, { id: 100 }));

      const now = Date.now();
      const comment = new LiveStatusComment(makeStages(), makeOptions());
      await comment.post();

      comment.transition(0, 'in_progress');
      const stages1 = comment.getStages();
      expect(stages1[0].startedAt).toBeGreaterThanOrEqual(now);
      expect(stages1[0].completedAt).toBeUndefined();

      comment.transition(0, 'complete');
      const stages2 = comment.getStages();
      expect(stages2[0].completedAt).toBeGreaterThanOrEqual(stages2[0].startedAt!);
    });
  });

  describe('finalizeSuccess()', () => {
    it('replaces comment body with success summary', async () => {
      mockFetch
        .mockResolvedValueOnce(mockFetchResponse(201, { id: 200 }))
        .mockResolvedValue(mockFetchResponse(200));

      const stages = makeStages();
      stages[0].status = 'complete';
      stages[0].startedAt = 1000;
      stages[0].completedAt = 3000;
      stages[1].status = 'complete';
      stages[1].startedAt = 3000;
      stages[1].completedAt = 8000;
      stages[2].status = 'complete';
      stages[2].startedAt = 8000;
      stages[2].completedAt = 10000;

      const comment = new LiveStatusComment(stages, makeOptions());
      await comment.post();
      mockFetch.mockClear();

      await comment.finalizeSuccess({
        prUrl: 'https://github.com/org/repo/pull/99',
        artifacts: ['report.md', 'coverage.html'],
        durationMs: 45000,
        details: 'All tests pass.',
      });

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const body = JSON.parse(mockFetch.mock.calls[0][1].body).body as string;
      expect(body).toContain('Agent run ended');
      expect(body).toContain('45s');
      expect(body).toContain('https://github.com/org/repo/pull/99');
      expect(body).toContain('report.md');
      expect(body).toContain('coverage.html');
      expect(body).toContain('[x] Stage 1');
      expect(body).toContain('All tests pass.');
    });
  });

  it('keeps incomplete stages and late caveats visible, using the real run clock', async () => {
    mockFetch.mockResolvedValueOnce(mockFetchResponse(201, { id: 200 }))
      .mockResolvedValue(mockFetchResponse(200));
    const comment = new LiveStatusComment([
      { label: 'Setup', status: 'complete' }, // no startedAt: original <1s bug
      { label: 'Review', status: 'in_progress' },
      { label: 'Deploy', status: 'pending' },
      { label: 'Browser checks', status: 'skipped' },
    ], makeOptions());
    await comment.post();
    jest.advanceTimersByTime(83 * 60 * 1000);
    const report = 'One story merged, four in review. '.repeat(30) + 'Dispatch is blocked; deployment not checked.';
    await comment.finalizeSuccess({ details: report });
    const body = JSON.parse(mockFetch.mock.calls.at(-1)![1].body).body;
    expect(body).toContain('1h 23m');
    expect(body).toContain(report);
    expect(body).toContain('[ ] Deploy (not run)');
    expect(body).toContain('[ ] Browser checks (skipped)');
    expect(body).toContain('Review (completion not recorded)');
    expect(body).not.toContain('[x] Deploy');
    expect(comment.getCommentUrl()).toBe('https://github.com/test-org/test-repo/issues/42#issuecomment-200');
  });

  it('reports publication failure so the worker can use its fallback', async () => {
    mockFetch.mockResolvedValueOnce(mockFetchResponse(201, { id: 200 }))
      .mockResolvedValue(mockFetchResponse(403));
    const comment = new LiveStatusComment(makeStages(), makeOptions());
    await comment.post();
    await expect(comment.finalizeSuccess({})).rejects.toThrow('Comment update failed: 403');
  });

  it('waits for an earlier progress update before publishing the final outcome', async () => {
    let finishProgress!: (response: Response) => void;
    mockFetch.mockResolvedValueOnce(mockFetchResponse(201, { id: 200 }))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { finishProgress = resolve; }))
      .mockResolvedValue(mockFetchResponse(200));
    const comment = new LiveStatusComment(makeStages(), makeOptions());
    await comment.post();
    jest.advanceTimersByTime(5001);
    comment.transition(0, 'in_progress');
    const finalized = comment.finalizeSuccess({ details: 'Review still blocked.' });
    expect(mockFetch).toHaveBeenCalledTimes(2);
    finishProgress(mockFetchResponse(200));
    await finalized;
    expect(mockFetch).toHaveBeenCalledTimes(3);
    expect(JSON.parse(mockFetch.mock.calls[2][1].body).body).toContain('Review still blocked.');
    comment.appendActivity('late event');
    await comment.flush();
    jest.advanceTimersByTime(60000);
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it('reports an operator stop as pending finalization and stops later progress updates', async () => {
    mockFetch.mockResolvedValueOnce(mockFetchResponse(201, { id: 301 }))
      .mockResolvedValue(mockFetchResponse(200));
    const comment = new LiveStatusComment(makeStages(), makeOptions());
    await comment.post();
    comment.setExplanation('Waiting for foreground work to finish.');
    await comment.finalizeAbortRequested();
    const body = JSON.parse(mockFetch.mock.calls.at(-1)![1].body).body as string;
    expect(body).toContain('Agent stopping');
    expect(body).toContain('Finalization is in progress');
    expect(body).toContain('Waiting for foreground work to finish.');
    expect(body).not.toContain('Failed');
    const count = mockFetch.mock.calls.length;
    comment.appendActivity('late progress');
    await comment.flush();
    jest.advanceTimersByTime(60000);
    expect(mockFetch).toHaveBeenCalledTimes(count);
  });

  describe('finalizeFailure()', () => {
    it('replaces comment body with failure summary', async () => {
      mockFetch
        .mockResolvedValueOnce(mockFetchResponse(201, { id: 300 }))
        .mockResolvedValue(mockFetchResponse(200));

      const stages = makeStages();
      stages[0].status = 'complete';
      stages[0].startedAt = 1000;
      stages[0].completedAt = 2000;
      stages[1].status = 'in_progress';
      stages[1].startedAt = 2000;

      const comment = new LiveStatusComment(stages, makeOptions());
      await comment.post();
      mockFetch.mockClear();

      await comment.finalizeFailure({
        error: 'TypeError: Cannot read property "x" of undefined',
        stackExcerpt: 'at Object.<anonymous> (src/foo.ts:42:5)\nat Module._compile',
        suggestedNextSteps: ['Check input validation', 'Re-run with debug logging'],
        durationMs: 12000,
      });

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const body = JSON.parse(mockFetch.mock.calls[0][1].body).body as string;
      expect(body).toContain('Agent Failed');
      expect(body).toContain('12s');
      expect(body).toContain('TypeError');
      expect(body).toContain('src/foo.ts:42:5');
      expect(body).toContain('Check input validation');
      expect(body).toContain('FAILED HERE');
      expect(body).toContain('[~] Stage 2');
    });
  });

  describe('flush()', () => {
    it('immediately updates the comment bypassing rate limit', async () => {
      mockFetch
        .mockResolvedValueOnce(mockFetchResponse(201, { id: 400 }))
        .mockResolvedValue(mockFetchResponse(200));

      const comment = new LiveStatusComment(makeStages(), makeOptions());
      await comment.post();
      mockFetch.mockClear();

      comment.transition(0, 'in_progress');
      // Don't advance timers — flush should work immediately
      await comment.flush();

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const body = JSON.parse(mockFetch.mock.calls[0][1].body).body as string;
      expect(body).toContain('[~] Stage 1');
    });
  });
});

describe('Factory helpers', () => {
  it('createWorkerStages exposes only observable lifecycle stages', () => {
    const stages = createWorkerStages();
    expect(stages).toHaveLength(2);
    expect(stages.every(s => s.status === 'pending')).toBe(true);
    expect(stages.map(s => s.label)).toEqual([
      'Setup', 'Development run',
    ]);
  });

  it('createSkillAgentStages returns 4 pending stages', () => {
    const stages = createSkillAgentStages();
    expect(stages).toHaveLength(4);
    expect(stages.every(s => s.status === 'pending')).toBe(true);
    expect(stages.map(s => s.label)).toEqual([
      'Planning', 'Approval', 'Execution', 'Finalize',
    ]);
  });
});

it.each(['reviewer', 'architect', 'aidlc', 'operations'])('does not invent implementation or PR stages for %s', persona => {
  const labels = createWorkerStages(persona).map(s => s.label);
  expect(labels).not.toContain('Implement');
  expect(labels).not.toContain('PR');
  expect(labels).toHaveLength(2);
});

describe('live implementation explanations', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-09-12T08:00:00Z'));
    mockFetch.mockReset();
    mockFetch.mockResolvedValueOnce(mockFetchResponse(201, { id: 321 }))
      .mockResolvedValue(mockFetchResponse(200));
  });
  afterEach(() => { jest.clearAllTimers(); jest.useRealTimers(); });

  const lastBody = () => JSON.parse(mockFetch.mock.calls.at(-1)![1].body).body as string;

  it('publishes through the existing throttle and retains the explanation timestamp across heartbeats', async () => {
    const comment = new LiveStatusComment(makeStages(), makeOptions());
    await comment.post();
    const explanation = 'The gateway forwards chunks as they arrive.\n\nThe test checks first-event delivery before completion.';
    comment.setExplanation(explanation);
    comment.appendActivity('Bash  run the regression suite');
    expect(mockFetch).toHaveBeenCalledTimes(1);
    jest.advanceTimersByTime(5000);
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(lastBody()).toContain(explanation);
    expect(lastBody().indexOf('### Agent explanation')).toBeLessThan(lastBody().indexOf('### Progress'));
    expect(lastBody()).toContain('_Reported 2026-09-12T08:00:00.000Z_');
    jest.advanceTimersByTime(30000);
    expect(lastBody()).toContain('_Reported 2026-09-12T08:00:00.000Z_');
    expect(lastBody()).toContain(explanation);
  });

  it('coalesces updates, ignores empty or duplicate text, and separates explanations from the rolling tool log', async () => {
    const comment = new LiveStatusComment(makeStages(), makeOptions());
    await comment.post();
    comment.setExplanation('Initial hypothesis.');
    comment.setExplanation('The first test disproved the initial hypothesis.');
    comment.setExplanation('   ');
    for (let i = 0; i < 20; i++) comment.appendActivity(`tool ${i}`);
    jest.advanceTimersByTime(5000);
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(lastBody()).toContain('The first test disproved the initial hypothesis.');
    expect(lastBody()).not.toContain('Initial hypothesis.');
    comment.setExplanation('The first test disproved the initial hypothesis.');
    jest.advanceTimersByTime(5000);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('bounds Unicode displays with a marker and preserves the last explanation on failure', async () => {
    const comment = new LiveStatusComment(makeStages(), makeOptions());
    await comment.post();
    comment.setExplanation('界🌍'.repeat(10000));
    await comment.flush();
    expect(Buffer.byteLength(lastBody())).toBeLessThan(60 * 1024);
    expect(lastBody()).toContain('Explanation shortened');
    expect(lastBody()).not.toContain('\ufffd');
    comment.setExplanation('The job failed before collecting any tests. Coverage remains unmeasured.');
    await comment.finalizeFailure({ error: 'collection failed', durationMs: 10000 });
    expect(lastBody()).toContain('Coverage remains unmeasured.');
    const count = mockFetch.mock.calls.length;
    comment.setExplanation('Late message must not replace the outcome.');
    jest.advanceTimersByTime(30000);
    expect(mockFetch).toHaveBeenCalledTimes(count);
  });
  it('preserves unchecked tasks on successful execution and withholds secrets', async () => {
    const comment = new LiveStatusComment(makeStages(), makeOptions());
    await comment.post();
    comment.setTaskChecklist('☐ Pending validation AKIAABCDEFGHIJKLMNOP');
    await comment.flush();
    expect(lastBody()).toContain('Checklist omitted');
    expect(lastBody()).not.toContain('AKIA');
    comment.setTaskChecklist('☑ Implementation\n☐ Validation');
    await comment.finalizeSuccess({ details: 'PR handed off; validation remains.' });
    expect(lastBody()).toContain('☐ Validation');
    const count = mockFetch.mock.calls.length;
    comment.setTaskChecklist('☑ Validation');
    jest.advanceTimersByTime(30000);
    expect(mockFetch).toHaveBeenCalledTimes(count);
  });

});
