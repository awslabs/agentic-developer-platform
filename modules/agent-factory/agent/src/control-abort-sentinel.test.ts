/**
 * Abort sentinel contract — Issue #3963 (S4).
 *
 * The organising claim: **this channel may lose an abort, but it may never invent
 * one.** A sentinel that survives validation causes the finalizing half to delete
 * a queue message, revoke controls and conclude a check run as cancelled. If a
 * leftover or corrupt file could pass, an unrelated live run would be finalized
 * as deliberately stopped. So every test below that feeds the reader something
 * imperfect asserts `null` — "no abort happened" — and the only test that expects
 * a payload feeds it a sentinel written by the real writer for the matching run.
 *
 * Tests use a per-test temp directory rather than `/tmp` so a developer's live
 * `/tmp/adp-abort-sentinel.json` cannot make a run green or red, and so the
 * `rename` stays within one filesystem exactly as it does in the pod.
 */
import { mkdtempSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

import {
  ABORT_SENTINEL_PATH,
  ABORT_SENTINEL_VERSION,
  MAX_SENTINEL_ENVELOPE_LENGTH,
  MAX_SENTINEL_REASON_LENGTH,
  abortSentinelBindingFromEnv,
  boundSentinelReason,
  clearAbortSentinel,
  parseStrictGeneration,
  readAbortSentinel,
  validateAbortSentinel,
  writeAbortSentinel,
  type AbortSentinelBinding,
} from './control-abort-sentinel';

const BINDING: AbortSentinelBinding = { runId: 'run-abc', generation: 4 };

let directory: string;
let sentinelPath: string;

beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), 'adp-abort-test-'));
  sentinelPath = join(directory, 'adp-abort-sentinel.json');
});

afterEach(() => {
  rmSync(directory, { recursive: true, force: true });
});

/** Put arbitrary bytes at the sentinel path, bypassing the writer. */
const putRaw = (contents: string) => writeFileSync(sentinelPath, contents, 'utf8');

describe('a well-formed sentinel for this run', () => {
  it('round-trips through the writer and the reader', () => {
    const written = writeAbortSentinel(
      { binding: BINDING, commandId: 'cmd-1', reason: 'wrong branch' },
      { sentinelPath },
    );
    expect(written).toBe(true);

    const read = readAbortSentinel(BINDING, { sentinelPath });
    expect(read).not.toBeNull();
    expect(read).toMatchObject({
      version: ABORT_SENTINEL_VERSION,
      run_id: 'run-abc',
      generation: 4,
      command_id: 'cmd-1',
      reason: 'wrong branch',
    });
    expect(Date.parse(read!.requested_at)).not.toBeNaN();
  });

  it('lands atomically under the final name, leaving no temp file behind', () => {
    writeAbortSentinel({ binding: BINDING, commandId: 'cmd-1' }, { sentinelPath });

    // A leftover `.tmp` would mean the rename did not happen or cleanup failed;
    // either way a reader could later see a partial document.
    const strays = readdirSync(directory).filter((name) => name.includes('.tmp'));
    expect(strays).toEqual([]);
    expect(JSON.parse(readFileSync(sentinelPath, 'utf8')).run_id).toBe('run-abc');
  });

  it('writes owner-only permissions', () => {
    writeAbortSentinel({ binding: BINDING, commandId: 'cmd-1' }, { sentinelPath });
    // The reason text is operator-supplied and the file names the run; no other
    // pod user has any business reading it.
    expect(statSync(sentinelPath).mode & 0o077).toBe(0);
  });

  it('records a null reason when none was supplied, not an empty string', () => {
    writeAbortSentinel({ binding: BINDING, commandId: 'cmd-1' }, { sentinelPath });
    // A downstream `if (reason)` and a downstream `if (reason !== null)` must agree.
    expect(readAbortSentinel(BINDING, { sentinelPath })!.reason).toBeNull();
  });

  it('overwrites an earlier sentinel so a re-issued abort does not stack files', () => {
    writeAbortSentinel({ binding: BINDING, commandId: 'cmd-1' }, { sentinelPath });
    writeAbortSentinel({ binding: BINDING, commandId: 'cmd-2' }, { sentinelPath });
    // Last writer wins, and the reader sees exactly one abort — the idempotency
    // story (AC-A7) depends on there being one terminal signal, not a pile.
    expect(readAbortSentinel(BINDING, { sentinelPath })!.command_id).toBe('cmd-2');
  });
});

describe('the reader refuses anything it cannot prove belongs to this run', () => {
  it('returns null when no sentinel exists — the overwhelmingly common case', () => {
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
  });

  it('returns null for a truncated document instead of throwing', () => {
    // Exactly what a torn read looks like. A throw here would propagate into
    // teardown and could cost the queue acknowledgement entirely.
    putRaw('{"version":1,"run_id":"run-a');
    expect(() => readAbortSentinel(BINDING, { sentinelPath })).not.toThrow();
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
  });

  it('returns null when the path is a directory rather than a file', () => {
    mkdirSync(sentinelPath);
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
  });

  it.each([
    ['a different run', { run_id: 'run-other', generation: 4 }],
    ['a superseded generation', { run_id: 'run-abc', generation: 3 }],
    ['a future generation', { run_id: 'run-abc', generation: 5 }],
  ])('returns null for %s', (_label, override) => {
    putRaw(JSON.stringify({
      version: ABORT_SENTINEL_VERSION,
      command_id: 'cmd-1',
      requested_at: '2026-09-23T00:00:00Z',
      reason: null,
      ...override,
    }));
    // Generation is equality, not "at least": an abort aimed at an attempt that
    // already ended must not finalize the attempt that replaced it.
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
  });

  it.each([
    ['a newer schema version', { version: ABORT_SENTINEL_VERSION + 1 }],
    ['an older schema version', { version: ABORT_SENTINEL_VERSION - 1 }],
    ['a missing command id', { command_id: undefined }],
    ['an empty command id', { command_id: '' }],
    ['a missing requested_at', { requested_at: undefined }],
    ['a non-integer generation', { generation: 4.5 }],
    ['a string generation', { generation: '4' }],
    ['a missing run id', { run_id: undefined }],
  ])('returns null for %s', (_label, override) => {
    putRaw(JSON.stringify({
      version: ABORT_SENTINEL_VERSION,
      run_id: BINDING.runId,
      generation: BINDING.generation,
      command_id: 'cmd-1',
      requested_at: '2026-09-23T00:00:00Z',
      ...override,
    }));
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
  });

  it.each([
    ['a JSON array', '[]'],
    ['a bare string', '"aborted"'],
    ['a bare number', '1'],
    ['JSON null', 'null'],
    ['empty bytes', ''],
  ])('returns null for %s', (_label, raw) => {
    // `typeof null === 'object'` and arrays are objects too; both would reach
    // property access on a non-record without the explicit shape check.
    putRaw(raw);
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
  });

  it('ignores unknown extra fields on an otherwise valid sentinel', () => {
    // Forward tolerance within a version: an extra field is not a reason to
    // discard a correctly bound abort.
    putRaw(JSON.stringify({
      version: ABORT_SENTINEL_VERSION,
      run_id: BINDING.runId,
      generation: BINDING.generation,
      command_id: 'cmd-1',
      requested_at: '2026-09-23T00:00:00Z',
      reason: null,
      future_field: 'ignored',
    }));
    expect(readAbortSentinel(BINDING, { sentinelPath })!.command_id).toBe('cmd-1');
  });
});

describe('the writer refuses to write an unbindable sentinel', () => {
  it.each([
    ['a blank run id', { runId: '', generation: 4 }],
    ['generation zero', { runId: 'run-abc', generation: 0 }],
    ['a negative generation', { runId: 'run-abc', generation: -1 }],
    ['a fractional generation', { runId: 'run-abc', generation: 2.5 }],
    ['a NaN generation', { runId: 'run-abc', generation: Number.NaN }],
  ])('returns false and writes nothing for %s', (_label, binding) => {
    const logged: string[] = [];
    const written = writeAbortSentinel(
      { binding: binding as AbortSentinelBinding, commandId: 'cmd-1' },
      { sentinelPath, log: (_level, message) => logged.push(message) },
    );
    // A sentinel no reader can validate is worse than none: it would sit in
    // `/tmp` looking like evidence while never being honoured.
    expect(written).toBe(false);
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
    expect(logged.join(' ')).toContain('abort sentinel not written');
  });

  it('returns false rather than throwing when the path is unwritable', () => {
    // The caller must be able to distinguish "recorded" from "not recorded" in
    // order to avoid claiming an abort outcome that was never persisted.
    const written = writeAbortSentinel(
      { binding: BINDING, commandId: 'cmd-1' },
      { sentinelPath: join(directory, 'missing-dir', 'sentinel.json') },
    );
    expect(written).toBe(false);
  });
});

describe('reason bounding', () => {
  it('truncates an over-long reason to the cap', () => {
    const written = boundSentinelReason('x'.repeat(MAX_SENTINEL_REASON_LENGTH + 50));
    // The reason reaches a GitHub comment, so it is capped at the boundary
    // rather than trusted to be short.
    expect(written!.length).toBe(MAX_SENTINEL_REASON_LENGTH);
  });

  it('collapses newlines and surrounding whitespace', () => {
    // Prevents an operator-supplied reason from breaking the layout of the
    // terminal comment it is interpolated into.
    expect(boundSentinelReason('  wrong\n\nbranch  ')).toBe('wrong branch');
  });

  it.each([['null', null], ['undefined', undefined], ['empty', ''], ['whitespace', '   ']])(
    'maps %s to null rather than an empty string',
    (_label, input) => {
      expect(boundSentinelReason(input as string | null | undefined)).toBeNull();
    },
  );

  it('bounds a reason that arrives over-long inside a stored sentinel', () => {
    // Validation re-bounds on read: a sentinel written by some other path does
    // not get to smuggle an unbounded reason into a comment.
    const long = 'y'.repeat(MAX_SENTINEL_REASON_LENGTH + 100);
    const validated = validateAbortSentinel({
      version: ABORT_SENTINEL_VERSION,
      run_id: BINDING.runId,
      generation: BINDING.generation,
      command_id: 'cmd-1',
      requested_at: '2026-09-23T00:00:00Z',
      reason: long,
    }, BINDING);
    expect(validated!.reason!.length).toBe(MAX_SENTINEL_REASON_LENGTH);
  });

  it('maps a non-string reason to null instead of coercing it', () => {
    const validated = validateAbortSentinel({
      version: ABORT_SENTINEL_VERSION,
      run_id: BINDING.runId,
      generation: BINDING.generation,
      command_id: 'cmd-1',
      requested_at: '2026-09-23T00:00:00Z',
      reason: { injected: true },
    }, BINDING);
    // `String({})` would put "[object Object]" in an operator-facing comment.
    expect(validated!.reason).toBeNull();
  });
});

describe('binding resolution from the environment', () => {
  it('reads the run id and generation the control listener was configured with', () => {
    expect(abortSentinelBindingFromEnv({
      ADP_CONTROL_RUN_ID: 'run-abc',
      ADP_CONTROL_GENERATION: '4',
    })).toEqual({ runId: 'run-abc', generation: 4 });
  });

  it.each([
    ['no control variables at all', {}],
    ['a missing run id', { ADP_CONTROL_GENERATION: '4' }],
    ['a blank run id', { ADP_CONTROL_RUN_ID: '   ', ADP_CONTROL_GENERATION: '4' }],
    ['a missing generation', { ADP_CONTROL_RUN_ID: 'run-abc' }],
    ['a non-numeric generation', { ADP_CONTROL_RUN_ID: 'run-abc', ADP_CONTROL_GENERATION: 'abc' }],
    ['a zero generation', { ADP_CONTROL_RUN_ID: 'run-abc', ADP_CONTROL_GENERATION: '0' }],
  ])('returns null for %s', (_label, env) => {
    // A run with no control channel has no binding, so the writer refuses
    // rather than emitting a file that can never be validated.
    expect(abortSentinelBindingFromEnv(env as NodeJS.ProcessEnv)).toBeNull();
  });

  it('trims a padded run id so it matches the value the reader compares against', () => {
    expect(abortSentinelBindingFromEnv({
      ADP_CONTROL_RUN_ID: ' run-abc ',
      ADP_CONTROL_GENERATION: '4',
    })).toEqual({ runId: 'run-abc', generation: 4 });
  });
});

describe('shared cross-language vectors', () => {
  /**
   * The same fixture `tests/test_abort_sentinel.py` evaluates, run through this
   * runtime's real validator.
   *
   * The previous cross-language check compared *source text* for the shared
   * constants. It could not have caught the divergence it was there to catch: the
   * constants matched exactly while Python honoured `version: true` (because
   * `True == 1` there) and this side rejected it. Matching declarations do not
   * prove matching behaviour, so the contract is pinned by running both
   * validators over one set of documents.
   */
  const vectors = JSON.parse(
    readFileSync(join(__dirname, '__fixtures__', 'abort-sentinel-vectors.json'), 'utf8'),
  ) as {
    version: number;
    path: string;
    max_reason_length: number;
    max_envelope_length: number;
    binding: { run_id: string; generation: number };
    vectors: Array<{
      name: string;
      accept: boolean;
      document: unknown;
      expect_reason?: string | null;
      expect_envelope?: string | null;
      note: string;
    }>;
    generation_binding_vectors: Array<{
      name: string;
      raw: string;
      expect: number | null;
      note?: string;
    }>;
  };

  const fixtureBinding: AbortSentinelBinding = {
    runId: vectors.binding.run_id,
    generation: vectors.binding.generation,
  };

  it('pins the shared constants against this reader', () => {
    expect(vectors.version).toBe(ABORT_SENTINEL_VERSION);
    expect(vectors.path).toBe(ABORT_SENTINEL_PATH);
    expect(vectors.max_reason_length).toBe(MAX_SENTINEL_REASON_LENGTH);
    expect(vectors.max_envelope_length).toBe(MAX_SENTINEL_ENVELOPE_LENGTH);
  });

  it.each(vectors.vectors.map((vector) => [vector.name, vector] as const))(
    'agrees with the Python reader on %s',
    (_name, vector) => {
      const validated = validateAbortSentinel(vector.document, fixtureBinding);

      if (vector.accept) {
        expect(validated).not.toBeNull();
        expect(validated!.run_id).toBe(fixtureBinding.runId);
        expect(validated!.generation).toBe(fixtureBinding.generation);
        expect(validated!.reason).toBe(vector.expect_reason ?? null);
        // The normalization both readers have to agree on. Neither judges the
        // signature here, so what is pinned is which values survive as a token
        // and which collapse to exactly `null` — the value the Python finalizer
        // treats as "no proof, refuse the abort". A shape that survived on one
        // side only would mean one runtime refusing an authorized abort or
        // handing a non-token to a verifier.
        expect(validated!.envelope).toBe(vector.expect_envelope ?? null);
      } else {
        // The note explains which real failure this vector stands for.
        expect(validated).toBeNull();
      }
    },
  );

  it.each(vectors.generation_binding_vectors.map((vector) => [vector.name, vector] as const))(
    'parses the env generation %s identically to Python',
    (_name, vector) => {
      expect(parseStrictGeneration(vector.raw)).toBe(vector.expect);
    },
  );

  it.each(
    vectors.vectors
      .filter((vector) => !vector.accept && vector.document !== null && typeof vector.document === 'object' && !Array.isArray(vector.document))
      .map((vector) => [vector.name, vector] as const),
  )('rejects %s through the file reader too, not just the validator', (_name, vector) => {
    // The validator holds the shared rules, but the run path calls the file
    // reader. A rule enforced only in the validator protects nothing real.
    putRaw(JSON.stringify(vector.document));

    expect(readAbortSentinel(fixtureBinding, { sentinelPath })).toBeNull();
  });
});

describe('clearAbortSentinel', () => {
  it('removes an existing sentinel', () => {
    writeAbortSentinel({ binding: BINDING, commandId: 'cmd-1' }, { sentinelPath });
    clearAbortSentinel({ sentinelPath });
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
  });

  it('is a no-op when no sentinel exists', () => {
    expect(() => clearAbortSentinel({ sentinelPath })).not.toThrow();
  });
});
