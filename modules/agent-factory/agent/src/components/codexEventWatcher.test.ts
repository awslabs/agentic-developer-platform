/**
 * Unit tests for CodexEventWatcher (issue #2884).
 *
 * Covers the impact-table invariants:
 *  - partial JSONL line across two writes → parsed once complete, no garbage
 *  - burst of events between ticks → coalesced into one block; consecutive
 *    same-type collapsed with × count
 *  - no events file / empty file → zero sink calls, zero errors (inert path)
 *  - malformed JSON line → dropped silently; subsequent lines still parse
 *  - dispose → no further sink calls, no open handles (fake timers)
 *  - sink throw (PATCH failure) → swallowed, watcher continues
 *  - summaries only → full command output never forwarded
 *  - session id emitted once for correlation
 */

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { CodexEventWatcher } from './codexEventWatcher';

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

class FakeStreamer {
  blocks: string[][] = [];
  onCodexActivity(lines: string[]): void {
    this.blocks.push(lines);
  }
}

class ThrowingStreamer {
  calls = 0;
  onCodexActivity(_lines: string[]): void {
    this.calls++;
    throw new Error('PATCH failed');
  }
}

class FakeComment {
  lines: string[] = [];
  appendActivity(line: string): void {
    this.lines.push(line);
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;
let eventsFile: string;

function makeWatcher(overrides: Partial<ConstructorParameters<typeof CodexEventWatcher>[0]> = {}) {
  const streamer = new FakeStreamer();
  const comment = new FakeComment();
  const warns: string[] = [];
  const watcher = new CodexEventWatcher({
    eventsFile,
    checkRunStreamer: streamer,
    liveComment: comment,
    pollIntervalMs: 1_000,
    log: (m) => warns.push(m),
    ...overrides,
  });
  return { watcher, streamer, comment, warns };
}

function write(content: string): void {
  fs.writeFileSync(eventsFile, content, 'utf8');
}

function append(content: string): void {
  fs.appendFileSync(eventsFile, content, 'utf8');
}

const THREAD = '{"type":"thread.started","thread_id":"019f-abc"}\n';
function exec(cmd: string): string {
  return `{"type":"item.completed","item":{"type":"command_execution","command":${JSON.stringify(cmd)},"exit_code":0}}\n`;
}
function edit(p: string, kind = 'add'): string {
  return `{"type":"item.completed","item":{"type":"file_change","changes":[{"path":${JSON.stringify(p)},"kind":"${kind}"}]}}\n`;
}
function reasoning(text: string): string {
  return `{"type":"item.completed","item":{"type":"reasoning","text":${JSON.stringify(text)}}}\n`;
}
function agentMsg(text: string): string {
  return `{"type":"item.completed","item":{"type":"agent_message","text":${JSON.stringify(text)}}}\n`;
}
function turnCompleted(input: number, cached: number, output: number, reasoning_output: number): string {
  return `{"type":"turn.completed","usage":{"input_tokens":${input},"cached_input_tokens":${cached},"output_tokens":${output},"reasoning_output_tokens":${reasoning_output}}}\n`;
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  jest.useFakeTimers();
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-events-'));
  eventsFile = path.join(tmpDir, 'current.jsonl');
});

afterEach(() => {
  jest.useRealTimers();
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    /* best-effort */
  }
});

// ---------------------------------------------------------------------------
// Inert default
// ---------------------------------------------------------------------------

describe('inert by default', () => {
  it('no events file → zero sink calls, zero warnings', () => {
    const { watcher, streamer, comment, warns } = makeWatcher();
    watcher.start();
    jest.advanceTimersByTime(5_000); // five polls, no file
    watcher.dispose();
    expect(streamer.blocks).toHaveLength(0);
    expect(comment.lines).toHaveLength(0);
    expect(warns).toHaveLength(0);
  });

  it('empty file → zero sink calls, zero warnings', () => {
    write('');
    const { watcher, streamer, comment, warns } = makeWatcher();
    watcher.start();
    jest.advanceTimersByTime(3_000);
    watcher.dispose();
    expect(streamer.blocks).toHaveLength(0);
    expect(comment.lines).toHaveLength(0);
    expect(warns).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Partial-line safety
// ---------------------------------------------------------------------------

describe('partial-line safety', () => {
  it('a line split across two writes is parsed once, only when complete', () => {
    const { watcher, streamer } = makeWatcher();
    watcher.start();

    // Write the first half of a command_execution line (no trailing newline).
    const full = exec('/bin/bash -lc ls');
    const half = full.slice(0, 40);
    const rest = full.slice(40);

    write(THREAD + half);
    jest.advanceTimersByTime(1_000);
    // The partial line must NOT have been forwarded as garbage.
    const flat1 = streamer.blocks.flat();
    expect(flat1.some((l) => l.includes('exec'))).toBe(false);

    append(rest);
    jest.advanceTimersByTime(1_000);
    const flat2 = streamer.blocks.flat();
    expect(flat2.some((l) => l.includes('exec: /bin/bash -lc ls'))).toBe(true);

    watcher.dispose();
  });
});

// ---------------------------------------------------------------------------
// Coalescing + collapse
// ---------------------------------------------------------------------------

describe('coalescing', () => {
  it('a burst between ticks is forwarded as ONE block to the streamer', () => {
    const { watcher, streamer } = makeWatcher();
    watcher.start();
    write(THREAD + reasoning('plan') + edit('a.py') + exec('pytest'));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();
    // One poll consumed all events → exactly one block.
    expect(streamer.blocks).toHaveLength(1);
  });

  it('consecutive same-type events collapse to "kind × N"', () => {
    const { watcher, streamer } = makeWatcher();
    watcher.start();
    write(THREAD + exec('a') + exec('b') + exec('c') + exec('d'));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();
    const flat = streamer.blocks.flat();
    expect(flat.some((l) => l.includes('exec × 4'))).toBe(true);
    // Individual exec lines are collapsed away.
    expect(flat.some((l) => l.includes('exec: a'))).toBe(false);
  });

  it('distinct consecutive types are NOT collapsed', () => {
    const { watcher, streamer } = makeWatcher();
    watcher.start();
    write(THREAD + exec('a') + edit('f.py') + exec('b'));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();
    const flat = streamer.blocks.flat();
    expect(flat.some((l) => l.includes('exec: a'))).toBe(true);
    expect(flat.some((l) => l.includes('edit: add f.py'))).toBe(true);
    expect(flat.some((l) => l.includes('exec: b'))).toBe(true);
    expect(flat.some((l) => l.includes('×'))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Malformed input
// ---------------------------------------------------------------------------

describe('malformed input', () => {
  it('drops a non-JSON line silently and still parses subsequent lines', () => {
    const { watcher, streamer, warns } = makeWatcher();
    watcher.start();
    write(THREAD + 'this is not json at all\n' + agentMsg('done'));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();
    const flat = streamer.blocks.flat();
    expect(flat.some((l) => l.includes('this is not json'))).toBe(false);
    expect(flat.some((l) => l.includes('note: done'))).toBe(true);
    // Dropping a bad line is not an error condition → no warning.
    expect(warns).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Dispose
// ---------------------------------------------------------------------------

describe('dispose', () => {
  it('after dispose, later writes produce no further sink calls', () => {
    const { watcher, streamer } = makeWatcher();
    watcher.start();
    write(THREAD + exec('first'));
    jest.advanceTimersByTime(1_000);
    const before = streamer.blocks.length;

    watcher.dispose();
    append(exec('second'));
    jest.advanceTimersByTime(10_000);
    expect(streamer.blocks.length).toBe(before);
  });
});

// ---------------------------------------------------------------------------
// Never-throw on sink failure
// ---------------------------------------------------------------------------

describe('sink failure resilience', () => {
  it('swallows a streamer throw and keeps polling', () => {
    const throwing = new ThrowingStreamer();
    const comment = new FakeComment();
    const warns: string[] = [];
    const watcher = new CodexEventWatcher({
      eventsFile,
      checkRunStreamer: throwing,
      liveComment: comment,
      pollIntervalMs: 1_000,
      log: (m) => warns.push(m),
    });
    watcher.start();

    write(THREAD + exec('one'));
    jest.advanceTimersByTime(1_000);
    append(exec('two'));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();

    // Streamer was invoked and threw both times, but the watcher survived and
    // the live comment still received the lines.
    expect(throwing.calls).toBeGreaterThanOrEqual(2);
    expect(comment.lines.length).toBeGreaterThan(0);
    // Only one WARN (further errors suppressed).
    expect(warns.length).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Secret hygiene: summaries only
// ---------------------------------------------------------------------------

describe('secret hygiene', () => {
  it('forwards the command string but never a command OUTPUT field', () => {
    const { watcher, streamer } = makeWatcher();
    watcher.start();
    // command_execution carrying an aggregated_output (Codex 0.142.5 sometimes
    // includes captured stdout). The watcher must forward only `command`.
    write(
      THREAD +
        '{"type":"item.completed","item":{"type":"command_execution",' +
        '"command":"printenv","exit_code":0,"aggregated_output":"AWS_SECRET_ACCESS_KEY=supersecret"}}\n',
    );
    jest.advanceTimersByTime(1_000);
    watcher.dispose();
    const flat = streamer.blocks.flat().join('\n');
    expect(flat).toContain('exec: printenv');
    expect(flat).not.toContain('supersecret');
  });
});

// ---------------------------------------------------------------------------
// Correlation: session id emitted once
// ---------------------------------------------------------------------------

describe('session correlation', () => {
  it('emits the Codex session id exactly once across polls', () => {
    const { watcher, streamer } = makeWatcher();
    watcher.start();
    write(THREAD + exec('a'));
    jest.advanceTimersByTime(1_000);
    append(exec('b'));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();
    const flat = streamer.blocks.flat();
    const sessionLines = flat.filter((l) => l.includes('session 019f-abc'));
    expect(sessionLines).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Truncation restart (new delegation resets the stable file)
// ---------------------------------------------------------------------------

describe('file truncation between delegations', () => {
  it('restarts its view when the file shrinks and forwards the new stream', () => {
    const { watcher, streamer } = makeWatcher();
    watcher.start();
    write(THREAD + exec('first-delegation'));
    jest.advanceTimersByTime(1_000);

    // Second delegation truncates and rewrites (tee without -a).
    write('{"type":"thread.started","thread_id":"second"}\n' + exec('second-delegation'));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();

    const flat = streamer.blocks.flat();
    expect(flat.some((l) => l.includes('exec: second-delegation'))).toBe(true);
    expect(flat.some((l) => l.includes('session second'))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Usage tracking (issue #2970): turn.completed → getTotalUsage()
// ---------------------------------------------------------------------------

describe('usage tracking (issue #2970)', () => {
  it('accumulates last-seen usage per session and sums across two sessions', () => {
    const { watcher } = makeWatcher();
    watcher.start();

    // First session: two turn.completed events (cumulative within session).
    // The second supersedes the first for that session.
    write(
      THREAD +
      turnCompleted(100, 10, 20, 5) +
      exec('work') +
      turnCompleted(200, 20, 40, 10),
    );
    jest.advanceTimersByTime(1_000);

    // Second delegation (new session via file truncation + new thread.started).
    write(
      '{"type":"thread.started","thread_id":"session-2"}\n' +
      turnCompleted(300, 30, 60, 15),
    );
    jest.advanceTimersByTime(1_000);
    watcher.dispose();

    const usage = watcher.getTotalUsage();
    // Session 1 last-seen: input=200, output=40 (reasoning is a subset of output, not added)
    // Session 2 last-seen: input=300, output=60
    // Total: input=500, output=100
    expect(usage.inputTokens).toBe(500);
    expect(usage.outputTokens).toBe(100);
  });

  it('returns zeros when no turn.completed events have been seen', () => {
    const { watcher } = makeWatcher();
    watcher.start();
    write(THREAD + exec('something'));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();

    const usage = watcher.getTotalUsage();
    expect(usage.inputTokens).toBe(0);
    expect(usage.outputTokens).toBe(0);
  });

  it('handles malformed/missing usage gracefully — returns 0, no throw', () => {
    const { watcher, warns } = makeWatcher();
    watcher.start();

    // turn.completed with no usage field, usage with wrong types, and null usage
    write(
      THREAD +
      '{"type":"turn.completed"}\n' +
      '{"type":"turn.completed","usage":null}\n' +
      '{"type":"turn.completed","usage":{"input_tokens":"not_a_number","output_tokens":true}}\n',
    );
    jest.advanceTimersByTime(1_000);
    watcher.dispose();

    const usage = watcher.getTotalUsage();
    expect(usage.inputTokens).toBe(0);
    expect(usage.outputTokens).toBe(0);
    // Malformed usage is silently handled, not a warning
    expect(warns).toHaveLength(0);
  });

  it('tracks usage even when turn.completed arrives before thread.started', () => {
    const { watcher } = makeWatcher();
    watcher.start();

    // No thread.started → sessionId is null → falls back to __unknown__ key
    write(turnCompleted(500, 0, 100, 50));
    jest.advanceTimersByTime(1_000);
    watcher.dispose();

    const usage = watcher.getTotalUsage();
    expect(usage.inputTokens).toBe(500);
    expect(usage.outputTokens).toBe(100); // reasoning excluded — subset of output_tokens
  });

  it('drains unpolled events on getTotalUsage (no poll tick needed)', () => {
    const { watcher } = makeWatcher();
    watcher.start();

    // Write a turn.completed but do NOT advance timers — the poll loop has
    // not seen it yet. getTotalUsage must still count it.
    write(THREAD + turnCompleted(700, 0, 30, 10));

    const usage = watcher.getTotalUsage();
    expect(usage.inputTokens).toBe(700);
    expect(usage.outputTokens).toBe(30);
    watcher.dispose();
  });
});
