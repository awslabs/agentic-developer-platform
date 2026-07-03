/**
 * Unit tests for CheckRunStreamer.
 *
 * Covers:
 *  - Markdown rendering (header, plan, activity, tool details)
 *  - Truncation at the 60 KB threshold
 *  - Decaying-interval throttle (no lifetime freeze), circuit-breaker, marker
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
// Throttle — decaying-interval PATCH cadence (no lifetime freeze)
// ---------------------------------------------------------------------------

describe('CheckRunStreamer decaying-interval throttle', () => {
  /** Records the mocked Date.now at every PATCH plus the request payload. */
  let patchTimes: number[];
  let patchPayloads: Array<{ title: string; text: string }>;

  function installFetchMock(): void {
    patchTimes = [];
    patchPayloads = [];
    global.fetch = jest.fn().mockImplementation(async (_url: string, init: RequestInit) => {
      patchTimes.push(Date.now());
      const body = JSON.parse(init.body as string) as { output: { title: string; text: string } };
      patchPayloads.push({ title: body.output.title, text: body.output.text });
      return { ok: true } as Response;
    }) as unknown as typeof fetch;
  }

  beforeEach(() => {
    jest.useFakeTimers();
    installFetchMock();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  /** Gaps (ms) between consecutive PATCHes. */
  function gaps(): number[] {
    const out: number[] = [];
    for (let i = 1; i < patchTimes.length; i++) out.push(patchTimes[i] - patchTimes[i - 1]);
    return out;
  }

  it('fires at ~5s cadence early in a run (mid-turn polling)', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    // A long tool call at the start of the run should poll every ~5s.
    s.onToolProgress('Bash');
    jest.advanceTimersByTime(30_000); // 30s of a long tool call
    s.destroy();

    expect(patchTimes.length).toBeGreaterThanOrEqual(5); // ~6 patches over 30s
    for (const g of gaps()) expect(g).toBe(5_000);
  });

  it('decays to ~45s cadence after 15 simulated minutes', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    jest.advanceTimersByTime(16 * 60_000); // age the run past 15 min
    s.onToolProgress('Bash'); // start a long tool call in the slow regime
    jest.advanceTimersByTime(3 * 60_000); // 3 more minutes
    s.destroy();

    expect(patchTimes.length).toBeGreaterThanOrEqual(3); // ~4 patches over 3 min
    for (const g of gaps()) expect(g).toBe(45_000);
    // The 2s-era cadence would have produced ~90 patches in 3 min — assert we
    // are decisively NOT doing that.
    expect(patchTimes.length).toBeLessThan(10);
  });

  it('mid-turn poller uses the decayed interval, not a hardcoded 2s', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    jest.advanceTimersByTime(20 * 60_000); // deep into the slow regime
    s.onToolProgress('Bash');
    jest.advanceTimersByTime(90_000); // 90s long tool call
    s.destroy();

    // 90s at 45s cadence → ~2 patches; at 2s it would be ~45.
    expect(patchTimes.length).toBeLessThanOrEqual(3);
    for (const g of gaps()) expect(g).toBe(45_000);
  });

  it('still fires a PATCH for a turn arriving after 40 minutes (no lifetime freeze)', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));

    // Generate a chatty first several minutes so the OLD 30-patch lifetime cap
    // would be long exhausted (a turn every second for ~7 min).
    for (let i = 0; i < 420; i++) {
      s.onTurn(turn(i + 1, [{ name: 'Bash', input: { command: `cmd ${i}` } }]));
      jest.advanceTimersByTime(1_000);
    }
    expect(patchTimes.length).toBeGreaterThan(30); // old cap would have frozen here

    // Now jump to 40 minutes elapsed and deliver one more turn.
    jest.advanceTimersByTime(40 * 60_000);
    const before = patchTimes.length;
    s.onTurn(turn(9999, [{ name: 'Bash', input: { command: 'late' } }]));
    jest.advanceTimersByTime(45_000); // slow-regime interval
    s.destroy();

    expect(patchTimes.length).toBeGreaterThan(before); // the late turn still updated the page
  });

  it('keeps a 60-minute chatty run under the circuit-breaker without warning', () => {
    const warnings: string[] = [];
    const s = new CheckRunStreamer(makeConfig({ log: (m) => warnings.push(m) }));

    // A turn every second for 60 minutes — the throttle governs the cadence.
    for (let i = 0; i < 60 * 60; i++) {
      s.onTurn(turn(i + 1, [{ name: 'Bash', input: { command: `cmd ${i}` } }]));
      jest.advanceTimersByTime(1_000);
    }
    s.destroy();

    expect(patchTimes.length).toBeLessThan(150); // under MAX_PATCHES
    expect(warnings.some((w) => w.includes('circuit-breaker'))).toBe(false);
  });

  it('trips the circuit-breaker with a single WARN on a pathologically long run', () => {
    const warnings: string[] = [];
    const s = new CheckRunStreamer(makeConfig({ log: (m) => warnings.push(m) }));

    // A ~2.5h chatty run exceeds the ~136-patch worst case for 60 min and
    // trips the 150 breaker.
    for (let i = 0; i < 150 * 60; i++) {
      s.onTurn(turn(i + 1, [{ name: 'Bash', input: { command: `cmd ${i}` } }]));
      jest.advanceTimersByTime(1_000);
    }
    s.destroy();

    expect(patchTimes.length).toBeLessThanOrEqual(150); // capped
    const breakerWarns = warnings.filter((w) => w.includes('circuit-breaker'));
    expect(breakerWarns.length).toBe(1); // warned exactly once
  });

  it('onResult final PATCH renders completed status and writes the final file', () => {
    const fs = require('fs');
    const FINAL_PATH = '/tmp/adp-check-run-final.md';
    try { fs.unlinkSync(FINAL_PATH); } catch { /* ignore */ }

    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onTurn(turn(1, [], 'Implementing the fix'));
    s.onResult({ costUsd: 0.12, turns: 1 });

    // The final immediate PATCH must render as completed, not running.
    const last = patchPayloads[patchPayloads.length - 1];
    expect(last.title).toContain('completed');
    expect(last.text).toContain('/ done');
    expect(last.text).not.toContain('/ running');

    // And it writes the transcript handoff file for entrypoint.py.
    expect(fs.existsSync(FINAL_PATH)).toBe(true);
    expect(fs.readFileSync(FINAL_PATH, 'utf8')).toContain('/ done');

    try { fs.unlinkSync(FINAL_PATH); } catch { /* ignore */ }
  });

  it('warns and does not throw when a PATCH request fails', () => {
    const warnings: string[] = [];
    global.fetch = jest.fn().mockRejectedValue(new Error('network down')) as unknown as typeof fetch;

    const s = new CheckRunStreamer(makeConfig({ log: (m) => warnings.push(m) }));
    expect(() => {
      s.onTurn(turn(1, [{ name: 'Bash', input: { command: 'ls' } }]));
      jest.advanceTimersByTime(5_000);
    }).not.toThrow();
    s.destroy();
  });

  it('clears all timers on destroy (no open handles)', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onTurn(turn(1, [{ name: 'Bash', input: { command: 'ls' } }])); // schedules a pending PATCH
    s.onToolProgress('Bash'); // schedules the mid-turn poller
    expect(jest.getTimerCount()).toBeGreaterThan(0);

    s.destroy();
    expect(jest.getTimerCount()).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Throttle marker — header hint that live updates are slowed
// ---------------------------------------------------------------------------

describe('CheckRunStreamer throttle marker', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('omits the throttle marker while the interval is 5s', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'plan'));
    const md = s.buildMarkdown('running');
    expect(md).not.toContain('Live updates throttled');
  });

  it('shows the throttle marker once the interval reaches 15s', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'plan'));
    jest.advanceTimersByTime(3 * 60_000); // 3 min elapsed → 15s interval
    const md = s.buildMarkdown('running');
    expect(md).toContain('Live updates throttled to every 15s');
  });

  it('shows the 45s interval in the marker deep into a run', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'plan'));
    jest.advanceTimersByTime(20 * 60_000); // 20 min elapsed → 45s interval
    const md = s.buildMarkdown('running');
    expect(md).toContain('Live updates throttled to every 45s');
  });

  it('never shows the throttle marker on the completed page', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'plan'));
    jest.advanceTimersByTime(30 * 60_000);
    const md = s.buildMarkdown('completed');
    expect(md).not.toContain('Live updates throttled');
  });
});

// ---------------------------------------------------------------------------
// destroy — must flush final markdown to disk so entrypoint.py can read it
// ---------------------------------------------------------------------------

describe('CheckRunStreamer.destroy', () => {
  it('writes final markdown to /tmp/adp-check-run-final.md', () => {
    const fs = require('fs');
    const FINAL_PATH = '/tmp/adp-check-run-final.md';

    // Clear any prior content
    try { fs.unlinkSync(FINAL_PATH); } catch { /* ignore */ }

    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'Analyzing the codebase'));
    s.onTurn(turn(2, [{ name: 'Bash', input: { command: 'ls' } }]));

    s.destroy();

    expect(fs.existsSync(FINAL_PATH)).toBe(true);
    const content = fs.readFileSync(FINAL_PATH, 'utf8');
    // Final file must reflect the completed status and contain both turns
    expect(content).toContain('## Agent: developer · issue #411');
    expect(content).toContain('Analyzing the codebase');
    expect(content).toContain('Bash');
    expect(content).toMatch(/Turn:\*\*\s*2\s*\/\s*done/);

    // Cleanup
    try { fs.unlinkSync(FINAL_PATH); } catch { /* ignore */ }
  });

  it('does not throw when the filesystem write fails', () => {
    const fs = require('fs');
    const origWrite = fs.writeFileSync;
    fs.writeFileSync = () => { throw new Error('disk full'); };

    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'plan'));

    // Must not throw even though the write fails
    expect(() => s.destroy()).not.toThrow();

    fs.writeFileSync = origWrite;
  });
});
