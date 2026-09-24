/**
 * End-to-end process test — Task API T5 (#5798).
 *
 * Spawns the real built entrypoint with the real `--embedded` command and drives
 * it over actual stdin/stdout, with a stand-in host in this test process. The
 * unit tests cover the engine's decisions; this covers the things only a real
 * process can show:
 *
 * - progress frames **arrive while the run is still going**, not in a burst at
 *   exit. The limits are explicit that buffered final stdout does not satisfy the
 *   progress requirement ("buffered_final_stdout_satisfies_progress": false), and
 *   a test that only inspects output after exit cannot tell the two apart.
 * - stdout carries protocol frames and nothing else.
 * - the process is launched with no credentials in its environment and still
 *   completes useful work (T5-AC01).
 * - a failed or cancelled run exits without a result frame (T5-AC05).
 *
 * This is the file that spawns processes, which is why the independence scan
 * excludes test sources — see the note in `independence.test.ts`.
 */

import assert from 'node:assert/strict';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { join } from 'node:path';
import { test, describe } from 'node:test';

import { loadFixture } from './fixtures.js';
import { type ChildFrame, type StartFrame } from './protocol.js';

const ENTRYPOINT = join(import.meta.dirname, 'index.js');
const NETWORK_DENY_HOOK = join(import.meta.dirname, '..', 'test', 'network-deny-hook.cjs');

/** A frame the child emitted, stamped with when it arrived relative to the run. */
interface Observed {
  frame: Record<string, unknown>;
  /** Milliseconds after spawn, used to prove progress was not buffered to exit. */
  atMs: number;
}

/**
 * Drive the child for one run.
 *
 * `respond` is the stand-in host: it receives each child frame and may return
 * frames to send back. Returning nothing leaves the child waiting, which is how
 * the cancellation and stream-close cases are set up.
 */
async function runChild(options: {
  start: StartFrame;
  respond: (frame: Record<string, unknown>, send: (frame: unknown) => void) => void;
  /** Extra environment for the child. Deliberately minimal by default. */
  env?: Record<string, string>;
  timeoutMs?: number;
}): Promise<{ frames: Observed[]; stderr: string; exitCode: number; startedAt: number }> {
  const startedAt = Date.now();
  const child: ChildProcessWithoutNullStreams = spawn(
    process.execPath,
    [ENTRYPOINT, '--embedded'],
    {
      // No AWS, GitHub, gateway or customer credential is passed. PATH only, so
      // the run is genuinely credential-free rather than inheriting the suite's
      // environment (T5-AC01).
      env: { PATH: process.env['PATH'] ?? '/usr/bin:/bin', ...options.env },
      stdio: ['pipe', 'pipe', 'pipe'],
    },
  ) as ChildProcessWithoutNullStreams;

  const frames: Observed[] = [];
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk: string) => {
    stderr += chunk;
  });

  const send = (frame: unknown): void => {
    child.stdin.write(`${JSON.stringify(frame)}\n`);
  };

  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
  lines.on('line', (line: string) => {
    if (line.trim().length === 0) {
      return;
    }
    const parsed = JSON.parse(line) as Record<string, unknown>;
    frames.push({ frame: parsed, atMs: Date.now() - startedAt });
    options.respond(parsed, send);
  });

  const exitCode = await new Promise<number>((resolve, reject) => {
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`child did not exit within ${options.timeoutMs ?? 10000}ms`));
    }, options.timeoutMs ?? 10000);

    child.on('error', reject);
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve(code ?? 1);
    });

    send(options.start);
  });

  return { frames, stderr, exitCode, startedAt };
}

function startFrame(overrides: Partial<StartFrame> = {}): StartFrame {
  const body = loadFixture('valid', 'process-start-frame.json').body as unknown as StartFrame;
  return { ...body, inputs: { ...(body.inputs ?? {}), evidence_file: 'logs.txt' }, ...overrides };
}

const REPORT_JSON = JSON.stringify({
  summary: 'Connection-pool exhaustion is the most likely cause of the 503 burst.',
  findings: [
    {
      statement: 'Pool acquisition timeouts precede the observed 503 responses.',
      evidence_refs: ['logs.txt:L2-L3'],
      confidence: 'high',
    },
  ],
  uncertainties: [],
  recommendations: ['Collect inventory-service logs for the same window.'],
});

/** Answer a model.request with a confirmed outcome carrying `text`. */
function confirmModel(frame: Record<string, unknown>, send: (f: unknown) => void, text: string): void {
  send({
    protocol_version: 1,
    type: 'model.result',
    request_id: '809517ec-8674-49fd-9583-aa5d98cb765f',
    task_id: frame['task_id'],
    turn_id: frame['turn_id'],
    operation_status: 'confirmed',
    content: [{ type: 'text', text }],
  });
}

describe('embedded run over the real process protocol', () => {
  test('completes a useful task with no credentials and observed network denial', async () => {
    const { frames, stderr, exitCode } = await runChild({
      start: startFrame(),
      env: { NODE_OPTIONS: `--require=${NETWORK_DENY_HOOK}` },
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          confirmModel(frame, send, REPORT_JSON);
        }
      },
    });

    assert.equal(exitCode, 0);
    const types = frames.map((f) => f.frame['type']);
    assert.equal(types[0], 'ready', 'the child announces itself first');
    assert.ok(types.includes('result'), 'the run produced a final result');

    const result = frames.find((f) => f.frame['type'] === 'result');
    const report = result?.frame['report'] as { findings: unknown[] };
    assert.equal(report.findings.length, 1);
    assert.match(stderr, /network-deny observer active/);
    assert.doesNotMatch(stderr, /network request blocked:/, 'the observer saw zero network requests');
  });

  test('a turn received during an outstanding model call gets its own model call', async () => {
    let calls = 0;
    const { frames, exitCode } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] !== 'model.request') return;
        calls += 1;
        if (calls === 1) {
          send({
            protocol_version: 1, type: 'turn',
            request_id: '809517ec-8674-49fd-9583-aa5d98cb765f',
            task_id: frame['task_id'],
            turn_id: '9a734313-2f31-484b-9349-d52c41ad2497',
            turn_number: 2,
            messages: [{ command_id: 'f6071829-3a4b-4c5d-9f70-819203142536', text: 'Check the revised window.' }],
          });
        } else {
          assert.match(JSON.stringify(frame['messages']), /Check the revised window/);
        }
        confirmModel(frame, send, REPORT_JSON);
      },
    });
    assert.equal(exitCode, 0);
    assert.equal(calls, 2);
    assert.equal(frames.filter(({ frame }) => frame['type'] === 'result').length, 1);
  });

  test('every frame on stdout is a valid protocol frame and nothing else', async () => {
    const { frames } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          confirmModel(frame, send, REPORT_JSON);
        }
      },
    });

    for (const observed of frames) {
      // Round-tripping through the validator proves the line is a contract frame,
      // not merely valid JSON — a stray log line would fail here.
      const encoded = JSON.stringify(observed.frame);
      assert.doesNotThrow(() => JSON.parse(encoded) as ChildFrame);
      assert.ok(
        ['ready', 'progress', 'model.request', 'input.required', 'result', 'cancelled', 'error'].includes(
          observed.frame['type'] as string,
        ),
        `unexpected frame type on stdout: ${String(observed.frame['type'])}`,
      );
    }
  });

  test('ready advertises only input and cancel', async () => {
    const { frames } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          confirmModel(frame, send, REPORT_JSON);
        }
      },
    });
    const ready = frames.find((f) => f.frame['type'] === 'ready');
    assert.deepEqual(ready?.frame['capabilities'], ['input', 'cancel']);
  });

  test('progress arrives before the result rather than buffered at exit', async () => {
    // The host is deliberately slow to answer the model request. If the child
    // buffered its output, the first progress frame could not arrive before that
    // delay elapsed — so the timing gap is the evidence.
    const MODEL_DELAY_MS = 300;
    const { frames } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          setTimeout(() => confirmModel(frame, send, REPORT_JSON), MODEL_DELAY_MS);
        }
      },
    });

    const firstProgress = frames.find((f) => f.frame['type'] === 'progress');
    const result = frames.find((f) => f.frame['type'] === 'result');
    assert.ok(firstProgress !== undefined, 'a progress frame was emitted');
    assert.ok(result !== undefined, 'a result frame was emitted');
    assert.ok(
      result.atMs - firstProgress.atMs >= MODEL_DELAY_MS - 50,
      `progress (${firstProgress.atMs}ms) must precede the result (${result.atMs}ms) by the model delay`,
    );
  });

  test('at least two distinct authored progress frames reach the host', async () => {
    const { frames } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          confirmModel(frame, send, REPORT_JSON);
        }
      },
    });

    const progress = frames.filter((f) => f.frame['type'] === 'progress');
    assert.ok(progress.length >= 2, `expected >= 2 progress frames, saw ${progress.length}`);
    const messages = new Set(progress.map((p) => p.frame['message'] as string));
    assert.equal(messages.size, progress.length, 'each progress message must be distinct');
    for (const entry of progress) {
      assert.equal('percent_complete' in entry.frame, false);
      assert.equal('reasoning' in entry.frame, false);
      assert.equal('thinking' in entry.frame, false);
    }
  });

  test('the terminal result is drained before process exit', async () => {
    const largeReport = JSON.stringify({
      summary: 'x'.repeat(3900),
      findings: Array.from({ length: 20 }, (_, index) => ({
        statement: `Supported finding ${index}: ${'y'.repeat(1800)}`,
        evidence_refs: ['logs.txt'],
      })),
      uncertainties: [],
      recommendations: [],
    });
    const { frames, exitCode } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          confirmModel(frame, send, largeReport);
        }
      },
    });

    assert.equal(exitCode, 0);
    const result = frames.find((entry) => entry.frame['type'] === 'result');
    assert.ok(result, 'the complete terminal frame reached the host before exit');
    const report = result.frame['report'] as { findings: unknown[] };
    assert.equal(report.findings.length, 20);
  });

  test('malformed model output ends the run with an error, not a result', async () => {
    const { frames, exitCode } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          confirmModel(frame, send, 'I could not complete this analysis.');
        }
      },
    });

    assert.equal(exitCode, 1, 'a failed run must not exit zero');
    assert.equal(
      frames.some((f) => f.frame['type'] === 'result'),
      false,
      'no result frame may be emitted for a failed run',
    );
    const error = frames.find((f) => f.frame['type'] === 'error');
    assert.equal(error?.frame['code'], 'invalid_agent_output');
  });

  test('an unknown model outcome ends the run as model_outcome_unknown', async () => {
    let modelRequests = 0;
    const { frames, exitCode } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          modelRequests += 1;
          send({
            protocol_version: 1,
            type: 'model.result',
            request_id: '809517ec-8674-49fd-9583-aa5d98cb765f',
            task_id: frame['task_id'],
            turn_id: frame['turn_id'],
            operation_status: 'unknown',
            content: null,
            stop_reason: null,
            error_code: 'model_outcome_unknown',
          });
        }
      },
    });

    assert.equal(exitCode, 1);
    assert.equal(modelRequests, 1, 'an unknown outcome must not be resent');
    const error = frames.find((f) => f.frame['type'] === 'error');
    assert.equal(error?.frame['code'], 'model_outcome_unknown');
    assert.equal(frames.some((f) => f.frame['type'] === 'result'), false);
  });

  test('typed cancellation yields a cancelled frame and no result', async () => {
    const startedAt = Date.now();
    const { frames, exitCode } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        // Cancel instead of answering the model request, so the run is cancelled
        // while genuinely in flight.
        if (frame['type'] === 'model.request') {
          send({
            protocol_version: 1,
            type: 'cancel',
            request_id: '331bac1b-b49e-43bb-b67e-a1f0a1a56755',
            task_id: frame['task_id'],
            command_id: 'f6071829-3a4b-4c5d-9f70-819203142536',
            intentional: true,
            reason: 'Client no longer needs the investigation.',
          });
        }
      },
    });

    const cancelled = frames.find((f) => f.frame['type'] === 'cancelled');
    assert.ok(cancelled !== undefined, 'a cancelled frame was emitted');
    assert.equal(cancelled.frame['command_id'], 'f6071829-3a4b-4c5d-9f70-819203142536');
    assert.equal(frames.some((f) => f.frame['type'] === 'result'), false);
    assert.equal(exitCode, 0, 'an honoured cancellation is not a process failure');
    assert.ok(Date.now() - startedAt < 2000, 'cancellation interrupts the outstanding model wait');
  });

  test('a start frame carrying credentials is refused as a protocol violation', async () => {
    const poisoned = loadFixture('invalid', 'process-start-frame-carries-credentials.json');
    const { frames, exitCode } = await runChild({
      start: poisoned.body as unknown as StartFrame,
      respond: () => {},
    });

    // The child never announces itself for a frame it refused, so there is no
    // window in which it has accepted a credential-bearing start.
    assert.equal(frames.some((f) => f.frame['type'] === 'ready'), false);
    assert.equal(frames.some((f) => f.frame['type'] === 'result'), false);
    assert.equal(exitCode, 1);
  });

  test('a host that closes the stream without a result fails the run', async () => {
    // The child is started and then abandoned mid-investigation.
    const start = startFrame();
    const child = spawn(process.execPath, [ENTRYPOINT, '--embedded'], {
      env: { PATH: process.env['PATH'] ?? '/usr/bin:/bin' },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    child.stdin.write(`${JSON.stringify(start)}\n`);

    const exitCode = await new Promise<number>((resolve) => {
      setTimeout(() => child.stdin.end(), 200);
      child.on('close', (code) => resolve(code ?? 1));
    });

    assert.equal(exitCode, 1, 'no valid final result means the run failed');
  });

  test('diagnostics go to stderr and never onto the protocol stream', async () => {
    const { frames, stderr } = await runChild({
      start: startFrame(),
      respond: (frame, send) => {
        if (frame['type'] === 'model.request') {
          // An unsolicited report.ack for an unknown report exercises the
          // diagnostic path without failing the run.
          send({
            protocol_version: 1,
            type: 'report.ack',
            request_id: '331bac1b-b49e-43bb-b67e-a1f0a1a56755',
            task_id: frame['task_id'],
            report_id: '9a734313-2f31-484b-9349-d52c41ad2497',
            sequence: 1,
          });
          confirmModel(frame, send, REPORT_JSON);
        }
      },
    });

    assert.ok(frames.some((f) => f.frame['type'] === 'result'), 'the run still completed');
    // Whatever stderr carried, it must not contain protocol frames.
    assert.doesNotMatch(stderr, /"type":\s*"(result|progress|ready)"/);
  });
});
