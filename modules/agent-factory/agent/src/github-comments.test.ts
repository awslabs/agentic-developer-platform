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
      expect(body).toContain('Agent Complete');
      expect(body).toContain('45s');
      expect(body).toContain('https://github.com/org/repo/pull/99');
      expect(body).toContain('report.md');
      expect(body).toContain('coverage.html');
      expect(body).toContain('[x] Stage 1');
      expect(body).toContain('All tests pass.');
    });
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
  it('createWorkerStages returns 6 pending stages', () => {
    const stages = createWorkerStages();
    expect(stages).toHaveLength(6);
    expect(stages.every(s => s.status === 'pending')).toBe(true);
    expect(stages.map(s => s.label)).toEqual([
      'Setup', 'Analyze', 'Plan', 'Implement', 'Verify', 'PR',
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
