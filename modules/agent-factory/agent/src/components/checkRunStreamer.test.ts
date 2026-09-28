/**
 * Unit tests for CheckRunStreamer.
 *
 * Covers:
 *  - Markdown rendering (header, plan, activity, tool details)
 *  - Truncation at the 60 KB threshold
 *  - Decaying-interval throttle (no lifetime freeze), circuit-breaker, marker
 */

import { CheckRunStreamer, CheckRunStreamerConfig, computeCodexCostUsd, CODEX_INPUT_PER_1K, CODEX_OUTPUT_PER_1K } from './checkRunStreamer';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal config used by most tests. */
function makeConfig(overrides: Partial<CheckRunStreamerConfig> = {}): CheckRunStreamerConfig {
  return {
    checkRunId: 42,
    repo: 'acme/adp',
    tokenProvider: () => 'ghs_test',
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

  it('shows the first explanation once without inferring a plan from arbitrary text', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'I will read the issue and write code.'));
    const md = s.buildMarkdown('running');
    expect(md).not.toContain('### Plan');
    expect(md).toContain('I will read the issue and write code.');
    expect(md.split('I will read the issue and write code.')).toHaveLength(2);
  });

  it('retains revised approaches in explanation order', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'Plan: do X'));
    s.onTurn(turn(2, [], 'Plan: do Y'));
    const md = s.buildMarkdown('running');
    expect(md).toContain('Plan: do X');
    expect(md).toContain('Plan: do Y');
    expect(md.indexOf('Plan: do X')).toBeLessThan(md.indexOf('Plan: do Y'));
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

  it('renders an explanation above tool details without repeating it in the tool block', () => {
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
    expect(md).toContain('### Implementation updates');
    expect(md).toContain('Let me verify the route works before writing tests.');
    const activity = md.slice(md.indexOf('### Activity'));
    expect(activity).not.toContain('Let me verify');
    // Tool code block still present
    expect(md).toContain('```bash');
    expect(md).toContain('pytest tests/test_vault.py');
    // Thought appears BEFORE the code block in the output
    const thoughtIdx = md.indexOf('### Implementation updates');
    const codeIdx = md.indexOf('```bash');
    expect(thoughtIdx).toBeLessThan(codeIdx);
  });

  it('preserves explanatory paragraphs in order before technical activity', () => {
    const s = new CheckRunStreamer(makeConfig());
    s.onTurn(turn(1, [], 'First thought about the plan.'));
    s.onTurn(turn(2, [{ name: 'Bash', input: { command: 'ls' } }], 'Second thought before the tool.'));
    s.onTurn(turn(3, [], 'Third thought, text-only turn.'));
    const md = s.buildMarkdown('running');
    expect(md).toContain('### Implementation updates');
    expect(md).toContain('#### Update 1\n\nFirst thought about the plan.');
    expect(md).toContain('#### Update 2\n\nSecond thought before the tool.');
    expect(md).toContain('#### Update 3\n\nThird thought, text-only turn.');
    // Reasoning section must appear before Activity section
    const reasoningIdx = md.indexOf('### Implementation updates');
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

// ---------------------------------------------------------------------------
// Codex cost display (issue #2970)
// ---------------------------------------------------------------------------

describe('CheckRunStreamer Codex cost display (issue #2970)', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    global.fetch = jest.fn().mockResolvedValue({ ok: true } as Response) as unknown as typeof fetch;
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('renders Codex cost in header and summary when codexCostUsd > 0', () => {
    const summaries: string[] = [];
    global.fetch = jest.fn().mockImplementation(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { output: { summary: string } };
      summaries.push(body.output.summary);
      return { ok: true } as Response;
    }) as unknown as typeof fetch;

    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onTurn({ turn: 1, content: [{ text: 'plan' }], costUsd: 2.0733, codexCostUsd: 1.45 });
    const md = s.buildMarkdown('running');

    // Header line includes both costs
    expect(md).toContain('$2.0733 (Claude) + ~$1.4500 (Codex est.)');

    // Fire the PATCH to check the summary line
    s.onResult({ costUsd: 2.0733, codexCostUsd: 1.45 });
    expect(summaries.length).toBeGreaterThan(0);
    const lastSummary = summaries[summaries.length - 1];
    expect(lastSummary).toContain('$2.0733 (Claude) + ~$1.4500 (Codex est.)');

    s.destroy();
  });

  it('renders byte-identical to pre-#2970 output when codexCostUsd is 0', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onTurn({ turn: 1, content: [{ text: 'plan' }], costUsd: 0.5 });
    const md = s.buildMarkdown('running');

    // No Codex mention at all
    expect(md).not.toContain('Codex');
    expect(md).not.toContain('Claude)');
    // Plain cost format
    expect(md).toContain('**Cost:** $0.5000');

    s.destroy();
  });

  it('renders byte-identical when codexCostUsd is not provided (undefined)', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onTurn({ turn: 1, content: [{ text: 'plan' }], costUsd: 0.25 });
    const md = s.buildMarkdown('running');

    expect(md).not.toContain('Codex');
    expect(md).toContain('**Cost:** $0.2500');

    s.destroy();
  });

  it('onResult updates codexCostUsd for the final PATCH', () => {
    const payloads: Array<{ text: string }> = [];
    global.fetch = jest.fn().mockImplementation(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { output: { text: string } };
      payloads.push({ text: body.output.text });
      return { ok: true } as Response;
    }) as unknown as typeof fetch;

    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onTurn({ turn: 1, content: [{ text: 'work' }], costUsd: 1.0 });
    // No Codex cost during the run
    jest.advanceTimersByTime(5_000);
    const midRunText = payloads[payloads.length - 1]?.text ?? '';
    expect(midRunText).not.toContain('Codex');

    // At result time, Codex cost arrives
    s.onResult({ costUsd: 1.5, codexCostUsd: 3.2 });
    const finalText = payloads[payloads.length - 1]?.text ?? '';
    expect(finalText).toContain('$1.5000 (Claude) + ~$3.2000 (Codex est.)');

    s.destroy();
  });
});

// ---------------------------------------------------------------------------
// computeCodexCostUsd pricing utility (issue #2970)
// ---------------------------------------------------------------------------

describe('computeCodexCostUsd (issue #2970)', () => {
  it('computes expected cost at pinned GPT-5.5 rates', () => {
    // 1000 input tokens × $0.0055/1K = $0.0055
    // 1000 output tokens × $0.033/1K = $0.033
    const cost = computeCodexCostUsd(1000, 1000);
    expect(cost).toBeCloseTo(0.0385, 6);
  });

  it('returns 0 for zero tokens', () => {
    expect(computeCodexCostUsd(0, 0)).toBe(0);
  });

  it('matches hand-calculated cost for a realistic Codex run', () => {
    // Smoke #2956: ~110 openai calls = $17.39
    // Typical heavy run: ~2M input, ~300K output
    // 2_000_000 / 1000 * 0.0055 = $11.00
    // 300_000 / 1000 * 0.033 = $9.90
    // Total ≈ $20.90
    const cost = computeCodexCostUsd(2_000_000, 300_000);
    expect(cost).toBeCloseTo(11.0 + 9.9, 2);
  });

  it('uses the correct pinned rates from gateway pricing.py', () => {
    // Verify the constants match what we expect
    expect(CODEX_INPUT_PER_1K).toBe(0.0055);
    expect(CODEX_OUTPUT_PER_1K).toBe(0.033);
  });
});

describe('independent explanation archive', () => {
  const fs = require('fs');
  const archivePath = '/tmp/adp-run-transcript.md';
  const githubPath = '/tmp/adp-check-run-final.md';
  let writes: Map<string, string>;

  beforeEach(() => {
    jest.useFakeTimers();
    writes = new Map();
    jest.spyOn(fs, 'writeFileSync').mockImplementation((file: unknown, content: unknown) => {
      writes.set(String(file), String(content));
    });
    jest.spyOn(fs, 'renameSync').mockImplementation((from: unknown, to: unknown) => {
      writes.set(String(to), writes.get(String(from))!);
      writes.delete(String(from));
    });
    jest.spyOn(fs, 'unlinkSync').mockImplementation((file: unknown) => { writes.delete(String(file)); });
    global.fetch = jest.fn().mockResolvedValue({ ok: true } as Response) as unknown as typeof fetch;
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('retains every explanation block and late caveat beyond both old clipping limits', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    const explanations: string[] = [];
    for (let i = 1; i <= 30; i++) {
      const text = `Explanation ${i}: ` + 'The gateway forwards each event. '.repeat(110);
      explanations.push(text.trim());
      s.onTurn({ turn: i, content: [{ type: 'text', text }, { type: 'text', text: `Caveat ${i}: acceptance has not run.` }] });
    }
    s.onTurn({ turn: 31, content: [{ type: 'thinking', text: 'PRIVATE_THINKING' }, { type: 'text', text: 'Review remains pending.' }] });
    s.onResult({});
    const archive = writes.get(archivePath)!;
    const github = writes.get(githubPath)!;
    expect(Buffer.byteLength(archive)).toBeGreaterThan(60 * 1024);
    expect(Buffer.byteLength(github)).toBeLessThanOrEqual(60 * 1024);
    for (let i = 0; i < explanations.length; i++) {
      expect(archive).toContain(explanations[i]);
      expect(archive).toContain(`Caveat ${i + 1}: acceptance has not run.`);
    }
    expect(archive.indexOf('### Update 1\n')).toBeLessThan(archive.indexOf('### Update 30\n'));
    expect(archive).toContain('Review remains pending.');
    expect(archive).not.toContain('PRIVATE_THINKING');
    expect(archive).not.toContain('<details');
    expect(github).toContain('Review remains pending.');
    expect(github).toContain('GitHub display truncated');
    s.destroy();
  });

  it('keeps a multi-paragraph explanation intact on GitHub when it fits', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    const explanation = 'Why this mechanism works. '.repeat(40) + '\n\nLimit: the browser path is not verified.';
    s.onTurn(turn(1, [], explanation));
    expect(s.buildMarkdown('running')).toContain(explanation);
    s.destroy();
  });

  it('bounds even one oversized Unicode explanation on GitHub while archiving it intact', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    const explanation = '🌍界'.repeat(30_000) + '\nFinal caveat.';
    s.onTurn(turn(1, [], explanation));
    s.destroy();
    expect(Buffer.byteLength(writes.get(githubPath)!)).toBeLessThanOrEqual(60 * 1024);
    expect(writes.get(githubPath)).not.toContain('\ufffd');
    expect(writes.get(archivePath)).toContain(explanation);
  });

  it('archives on result even after GitHub updates have exhausted their circuit-breaker', () => {
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onToolProgress('Bash');
    jest.advanceTimersByTime(3 * 60 * 60 * 1000);
    expect(global.fetch).toHaveBeenCalledTimes(150);
    s.onTurn(turn(1, [], 'The final explanation still needs to be retained.'));
    s.onResult({});
    expect(writes.get(archivePath)).toContain('The final explanation still needs to be retained.');
    expect(global.fetch).toHaveBeenCalledTimes(150);
    s.destroy();
  });

  it('writes the archive even when the independent GitHub handoff write fails', () => {
    (fs.writeFileSync as jest.Mock).mockImplementation((file: string, content: string) => {
      if (file === `${githubPath}.tmp`) throw new Error('display write failed');
      writes.set(file, content);
    });
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onTurn(turn(1, [{ name: 'Bash', input: { command: 'echo ```code```' } }], 'Explain the change.'));
    expect(() => s.destroy()).not.toThrow();
    expect(writes.get(archivePath)).toContain('Explain the change.');
    expect(writes.get(archivePath)).toContain('````\nBash: echo ```code```\n````');
  });

  it('does not expose a partially written archive as a complete transcript', () => {
    (fs.writeFileSync as jest.Mock).mockImplementation((file: string, content: string) => {
      if (file === `${archivePath}.tmp`) {
        writes.set(file, content.slice(0, 50));
        throw new Error('disk full');
      }
      writes.set(file, content);
    });
    const s = new CheckRunStreamer(makeConfig({ log: () => {} }));
    s.onTurn(turn(1, [], 'An explanation whose ending must survive.'));
    expect(() => s.destroy()).not.toThrow();
    expect(writes.has(archivePath)).toBe(false);
    expect(writes.has(`${archivePath}.tmp`)).toBe(false);
    expect(writes.get(githubPath)).toContain('An explanation whose ending must survive.');
  });
});
