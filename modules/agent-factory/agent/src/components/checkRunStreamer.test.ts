/**
 * Unit tests for CheckRunStreamer.
 *
 * Covers:
 *  - Markdown rendering (header, plan, activity, tool details)
 *  - Truncation at the 60 KB threshold
 *  - Debounce: max 30 PATCHes, min 2s interval
 */

import { CheckRunStreamer, CheckRunStreamerConfig } from './checkRunStreamer';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal config used by most tests. */
function makeConfig(overrides: Partial<CheckRunStreamerConfig> = {}): CheckRunStreamerConfig {
  return {
    checkRunId: 42,
    repo: 'acme/adp',
    token: 'ghs_test',
    persona: 'developer',
    issueNumber: 411,
    model: 'global.anthropic.claude-sonnet-4-6',
    ...overrides,
  };
}

/** Build a minimal assistant turn payload. */
function turn(
  n: number,
  tools: Array<{ name: string; input?: Record<string, unknown> }> = [],
  text = '',
): Parameters<CheckRunStreamer['onTurn']>[0] {
  const content: Array<{ name?: string; input?: Record<string, unknown>; text?: string }> = [
    ...tools.map(t => ({ name: t.name, input: t.input ?? {} })),
  ];
  if (text) content.push({ text });
  return { turn: n, content, costUsd: n * 0.01 };
}

// ---------------------------------------------------------------------------
// buildMarkdown — structure
// ---------------------------------------------------------------------------

describe('CheckRunStreamer.buildMarkdown', () => {
  it('renders header with persona, issue, model, cost, turn, elapsed', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'Initial analysis'));
    const md = s.buildMarkdown('running');
    expect(md).toContain('## Agent: developer · issue #411');
    expect(md).toContain('**Model:** global.anthropic.claude-sonnet-4-6');
    expect(md).toContain('**Turn:** 1 / running');
    expect(md).toContain('**Elapsed:**');
  });

  it('captures first text as plan', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'I will read the issue and write code.'));
    const md = s.buildMarkdown('running');
    expect(md).toContain('### Plan');
    expect(md).toContain('I will read the issue and write code.');
  });

  it('does not overwrite plan with subsequent text turns', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'Plan: do X'));
    s.onTurn(turn(2, [], 'Plan: do Y'));
    const md = s.buildMarkdown('running');
    // Plan section must contain the FIRST text, not the second
    const planSection = md.match(/### Plan[\s\S]*?(?=### Reasoning|### Activity|$)/)?.[0] ?? '';
    expect(planSection).toContain('Plan: do X');
    expect(planSection).not.toContain('Plan: do Y');
  });

  it('renders Activity section with tool turns', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [{ name: 'Bash', input: { command: 'ls -la' } }]));
    s.onTurn(turn(2, [{ name: 'Read', input: { file_path: 'src/index.ts' } }]));
    const md = s.buildMarkdown('running');
    expect(md).toContain('### Activity');
    expect(md).toContain('Turn 2');
    expect(md).toContain('Turn 1');
  });

  it('marks the most-recent turn as open', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [{ name: 'Bash', input: { command: 'echo a' } }]));
    s.onTurn(turn(2, [{ name: 'Bash', input: { command: 'echo b' } }]));
    const md = s.buildMarkdown('running');
    // Newest (turn 2) should be open; turn 1 should be closed
    expect(md).toMatch(/<details open>/);
    expect(md).toMatch(/Turn 2/);
  });

  it('renders completed status correctly', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'Done'));
    const md = s.buildMarkdown('completed');
    expect(md).toContain('/ done');
  });

  it('renders Bash command in code block', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [{ name: 'Bash', input: { command: 'gh issue view 411' } }]));
    const md = s.buildMarkdown('running');
    expect(md).toContain('```bash');
    expect(md).toContain('gh issue view 411');
  });

  it('shows "Also:" line for multiple tools in a turn', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [
      { name: 'Bash', input: { command: 'ls' } },
      { name: 'Read', input: { file_path: 'foo.ts' } },
    ]));
    const md = s.buildMarkdown('running');
    expect(md).toContain('Also:');
    expect(md).toContain('Read');
  });

  it('renders thought in italics above tool block when turn has both', () => {
    const s = new CheckRunStreamer(makeConfig());
    // Inject a turn where content has both a tool call and a text block
    s.onTurn({
      turn: 5,
      content: [
        { name: 'Bash', input: { command: 'pytest tests/test_vault.py' } },
        { text: 'Let me verify the route works before writing tests.' },
      ],
      costUsd: 0.05,
    });
    const md = s.buildMarkdown('running');
    // Thought rendered in italics
    expect(md).toContain('_Let me verify the route works before writing tests._');
    // Tool code block still present
    expect(md).toContain('```bash');
    expect(md).toContain('pytest tests/test_vault.py');
    // Thought appears BEFORE the code block in the output
    const thoughtIdx = md.indexOf('_Let me verify');
    const codeIdx = md.indexOf('```bash');
    expect(thoughtIdx).toBeLessThan(codeIdx);
  });

  it('accumulates thoughts into a Reasoning section, one bullet per turn', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'First thought about the plan.'));
    s.onTurn(turn(2, [{ name: 'Bash', input: { command: 'ls' } }], 'Second thought before the tool.'));
    s.onTurn(turn(3, [], 'Third thought, text-only turn.'));
    const md = s.buildMarkdown('running');
    expect(md).toContain('### Reasoning');
    expect(md).toContain('- First thought about the plan.');
    expect(md).toContain('- Second thought before the tool.');
    expect(md).toContain('- Third thought, text-only turn.');
    // Reasoning section must appear before Activity section
    const reasoningIdx = md.indexOf('### Reasoning');
    const activityIdx = md.indexOf('### Activity');
    expect(reasoningIdx).toBeLessThan(activityIdx);
  });
});

// ---------------------------------------------------------------------------
// buildMarkdown — truncation
// ---------------------------------------------------------------------------

describe('CheckRunStreamer truncation', () => {
  it('does not truncate a short document', () => {
    const s = new CheckRunStreamer(makeConfig());
    for (let i = 1; i <= 5; i++) {
      s.onTurn(turn(i, [{ name: 'Bash', input: { command: `echo ${i}` } }]));
    }
    const md = s.buildMarkdown('running');
    expect(Buffer.byteLength(md, 'utf8')).toBeLessThanOrEqual(60 * 1024);
    // All turns present
    for (let i = 1; i <= 5; i++) {
      expect(md).toContain(`Turn ${i}`);
    }
  });

  it('truncates to fit within 60 KB and adds hidden-count marker', () => {
    const s = new CheckRunStreamer(makeConfig());
    // Each turn's tool input is ~500 chars; 200 turns × 500 chars = ~100 KB
    const longCmd = 'x'.repeat(500);
    for (let i = 1; i <= 200; i++) {
      s.onTurn(turn(i, [{ name: 'Bash', input: { command: longCmd } }]));
    }
    const md = s.buildMarkdown('running');
    expect(Buffer.byteLength(md, 'utf8')).toBeLessThanOrEqual(60 * 1024);
    expect(md).toContain('turns hidden');
  });

  it('always keeps the most recent turns when truncating', () => {
    const s = new CheckRunStreamer(makeConfig());
    const longCmd = 'x'.repeat(500);
    for (let i = 1; i <= 200; i++) {
      s.onTurn(turn(i, [{ name: 'Bash', input: { command: longCmd } }]));
    }
    const md = s.buildMarkdown('running');
    // Turn 200 (most recent) must be present
    expect(md).toContain('Turn 200');
  });
});

// ---------------------------------------------------------------------------
// Debounce — patch-count cap
// ---------------------------------------------------------------------------

describe('CheckRunStreamer debounce / patch cap', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('never fires more than 30 PATCHes even with many rapid turns', async () => {
    const patches: unknown[] = [];
    global.fetch = jest.fn().mockImplementation(async () => {
      patches.push(1);
      return { ok: true } as Response;
    }) as unknown as typeof fetch;

    const s = new CheckRunStreamer(makeConfig());

    // Simulate 100 rapid turns, each 2+ seconds apart
    for (let i = 1; i <= 100; i++) {
      s.onTurn(turn(i, [{ name: 'Bash', input: { command: `cmd ${i}` } }]));
      jest.advanceTimersByTime(2100);
      // Let promises run
      await Promise.resolve();
    }

    s.destroy();

    // Flush any remaining timers
    jest.runAllTimers();
    await Promise.resolve();

    expect(patches.length).toBeLessThanOrEqual(30);
  });

  it('respects min 2s interval between patches', async () => {
    const patchTimes: number[] = [];
    let now = 0;
    const origDateNow = Date.now;
    Date.now = () => now;

    global.fetch = jest.fn().mockImplementation(async () => {
      patchTimes.push(now);
      return { ok: true } as Response;
    }) as unknown as typeof fetch;

    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));

    // Two rapid turns with only 500ms between them
    s.onTurn(turn(1, [{ name: 'Bash', input: { command: 'cmd1' } }]));
    now += 500;
    s.onTurn(turn(2, [{ name: 'Read', input: { file_path: 'a.ts' } }]));

    // Advance past the 2s debounce
    now += 2000;
    jest.advanceTimersByTime(2000);
    await Promise.resolve();

    s.destroy();
    jest.runAllTimers();
    await Promise.resolve();

    // All intervals between consecutive patches should be >= 2000ms
    for (let i = 1; i < patchTimes.length; i++) {
      expect(patchTimes[i] - patchTimes[i - 1]).toBeGreaterThanOrEqual(2000);
    }

    Date.now = origDateNow;
  });
});
