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
  MAX_SENTINEL_BYTES,
  MAX_SENTINEL_ENVELOPE_LENGTH,
  MAX_SENTINEL_REASON_LENGTH,
  MAX_SIGNED_BODY_LENGTH,
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

/**
 * A signed request body, base64 — the field the reason is derived from.
 *
 * Every valid sentinel carries one: the reader refuses a document without it,
 * because a sentinel whose reason cannot be bound to the gateway's signature is a
 * sentinel whose reason is self-asserted. The bytes here are ordinary JSON and
 * carry no credential; nothing in this file signs anything, so no test here
 * asserts that a body is *authorized* — that is `test_abort_authorization.py`,
 * which generates keys per run.
 */
const SIGNED_BODY = Buffer.from(
  JSON.stringify({ command_id: 'cmd-0001', reason: 'wrong branch' }),
  'utf8',
).toString('base64');

/** The minimum a writer must supply for the reader to accept the result. */
const VALID_INPUT = { binding: BINDING, commandId: 'cmd-1', signedBodyBase64: SIGNED_BODY };

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
    const written = writeAbortSentinel(VALID_INPUT, { sentinelPath });
    expect(written).toBe(true);

    const read = readAbortSentinel(BINDING, { sentinelPath });
    expect(read).not.toBeNull();
    expect(read).toMatchObject({
      version: ABORT_SENTINEL_VERSION,
      run_id: 'run-abc',
      generation: 4,
      command_id: 'cmd-1',
      // The signed bytes survive verbatim. They are the preimage of the envelope's
      // `body_digest` claim, so any re-encoding between write and read would break
      // the digest comparison the finalizer depends on.
      signed_body_base64: SIGNED_BODY,
      // Recorded because the live authority recheck accepted this command, not
      // merely because an envelope existed.
      delivery: 'accepted',
    });
    expect(Date.parse(read!.requested_at)).not.toBeNaN();
    // No `reason` key. The reason lives only inside the signed bytes; a field of
    // that name on the document is what let fabricated text be attributed to a
    // human, so its absence is part of the contract rather than an omission.
    expect(read).not.toHaveProperty('reason');
  });

  it('lands atomically under the final name, leaving no temp file behind', () => {
    writeAbortSentinel(VALID_INPUT, { sentinelPath });

    // A leftover `.tmp` would mean the rename did not happen or cleanup failed;
    // either way a reader could later see a partial document.
    const strays = readdirSync(directory).filter((name) => name.includes('.tmp'));
    expect(strays).toEqual([]);
    expect(JSON.parse(readFileSync(sentinelPath, 'utf8')).run_id).toBe('run-abc');
  });

  it('writes owner-only permissions', () => {
    writeAbortSentinel(VALID_INPUT, { sentinelPath });
    // The signed body is operator-supplied text and the file names the run; no
    // other pod user has any business reading it.
    expect(statSync(sentinelPath).mode & 0o077).toBe(0);
  });

  it('overwrites an earlier sentinel so a re-issued abort does not stack files', () => {
    writeAbortSentinel(VALID_INPUT, { sentinelPath });
    writeAbortSentinel({ ...VALID_INPUT, commandId: 'cmd-2' }, { sentinelPath });
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
      delivery: 'accepted',
      signed_body_base64: SIGNED_BODY,
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
      delivery: 'accepted',
      signed_body_base64: SIGNED_BODY,
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
      delivery: 'accepted',
      signed_body_base64: SIGNED_BODY,
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
      { binding: binding as AbortSentinelBinding, commandId: 'cmd-1', signedBodyBase64: SIGNED_BODY },
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
      VALID_INPUT,
      { sentinelPath: join(directory, 'missing-dir', 'sentinel.json') },
    );
    expect(written).toBe(false);
  });
});

describe('the writer refuses a sentinel its own reader would reject', () => {
  /**
   * The writer's boolean IS the operator-facing claim.
   *
   * `applyControlCommand` settles the abort command `applied` — "recorded for
   * finalization" — on `true`, and `unknown` on `false`. So a document that lands
   * on disk and then fails validation is worse than no document at all: the
   * operator is told the abort was recorded, while the finalizer finds nothing it
   * can honour and classifies the run by exit code. A deliberate stop reported as
   * a crash is the one outcome this story exists to remove.
   */
  it('refuses a document that would exceed the byte ceiling, rather than storing an unreadable one', () => {
    // A body the LISTENER accepts: two known fields with JSON whitespace between
    // them, under its 16 KiB `MAX_BODY_BYTES` cap. `validatePayload` checks the key
    // set and the reason length, so this is a legal abort request — which is what
    // made this reachable rather than theoretical. With the ceiling at a flat 8192
    // the writer returned `true` here and the reader then returned `null`.
    const body = `{"command_id":"11111111-2222-3333-4444-555555555555",${' '.repeat(15000)}"reason":"wrong branch"}`;
    expect(Buffer.byteLength(body, 'utf8')).toBeLessThan(16 * 1024);
    const signedBodyBase64 = Buffer.from(body, 'utf8').toString('base64');

    const logged: string[] = [];
    const written = writeAbortSentinel(
      { binding: BINDING, commandId: 'cmd-1', signedBodyBase64 },
      { sentinelPath, log: (_level, message) => logged.push(message) },
    );

    // Accepted, because the derived ceiling now covers what a legal request
    // produces. The assertion that matters is the agreement below, not the verdict.
    expect(written).toBe(true);
    expect(readAbortSentinel(BINDING, { sentinelPath })).not.toBeNull();
    expect(logged).toEqual([]);
  });

  it('never returns true for a document the reader will refuse', () => {
    // The property, stated directly and independently of any particular limit:
    // writer and reader agree. Driven with signed bodies straddling the field bound
    // so the pair is exercised on both sides of it, and with the envelope at its own
    // ceiling so the two large fields are near-maximal together — the combination
    // that overflowed a flat file ceiling.
    for (const bodyLength of [4, 100, MAX_SIGNED_BODY_LENGTH - 4, MAX_SIGNED_BODY_LENGTH, MAX_SIGNED_BODY_LENGTH + 4]) {
      for (const envelopeLength of [0, 13, MAX_SENTINEL_ENVELOPE_LENGTH]) {
        clearAbortSentinel({ sentinelPath });
        const written = writeAbortSentinel(
          {
            binding: BINDING,
            commandId: 'cmd-1',
            signedBodyBase64: 'A'.repeat(bodyLength),
            envelope: 'e'.repeat(envelopeLength),
          },
          { sentinelPath },
        );
        const read = readAbortSentinel(BINDING, { sentinelPath });
        // `true` must mean readable. (`false` with a readable leftover would be a
        // separate bug; the writer refuses before opening the temp file, so a
        // refusal leaves whatever was there before — here, nothing.)
        expect({ bodyLength, envelopeLength, written, readable: read !== null })
          .toEqual({ bodyLength, envelopeLength, written, readable: written });
      }
    }
  });

  it('refuses to write when no signed body is available', () => {
    const logged: string[] = [];
    const written = writeAbortSentinel(
      { binding: BINDING, commandId: 'cmd-1' },
      { sentinelPath, log: (_level, message) => logged.push(message) },
    );
    // Without the signed bytes the finalizer cannot bind the reason to the
    // gateway's signature, so the reader refuses the document. Refusing to write it
    // reports the same outcome honestly instead of looking like success.
    expect(written).toBe(false);
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
    expect(logged.join(' ')).toContain('signed request body was not available');
  });
});

describe('the reason-bounding rule', () => {
  /**
   * Nothing on this runtime's abort path calls `boundSentinelReason` any more —
   * the reason is derived from the signed body by the Python finalizer, which
   * applies its own `bound_sentinel_reason`. The rule is still pinned here because
   * both runtimes are required to implement it identically, and the shared
   * `reason_vectors` table below is evaluated against this function on this side
   * and against Python's on that one. A rule pinned on one side is not pinned.
   */
  it('truncates an over-long reason to the cap', () => {
    const written = boundSentinelReason('x'.repeat(MAX_SENTINEL_REASON_LENGTH + 50));
    // The reason reaches a GitHub comment, so it is capped at the boundary
    // rather than trusted to be short.
    expect(written!.length).toBe(MAX_SENTINEL_REASON_LENGTH);
  });

  it('collapses newlines and surrounding whitespace', () => {
    // Prevents an operator-supplied reason from breaking the layout of the
    // terminal comment it is interpolated into. Signed text is still hostile text:
    // a signature proves who wrote it, not that it is safe to print.
    expect(boundSentinelReason('  wrong\n\nbranch  ')).toBe('wrong branch');
  });

  it.each([['null', null], ['undefined', undefined], ['empty', ''], ['whitespace', '   ']])(
    'maps %s to null rather than an empty string',
    (_label, input) => {
      expect(boundSentinelReason(input as string | null | undefined)).toBeNull();
    },
  );

  it('maps a non-string reason to null instead of coercing it', () => {
    // `String({})` would put "[object Object]" in front of an operator, and the
    // two runtimes disagree about what coercion produces.
    expect(boundSentinelReason({ injected: true } as unknown as string)).toBeNull();
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
    max_signed_body_length: number;
    max_sentinel_bytes: number;
    max_safe_generation: number;
    accepted_delivery: string;
    signed_body_base64: string;
    binding: { run_id: string; generation: number };
    vectors: Array<{
      name: string;
      accept: boolean;
      document: unknown;
      expect_envelope?: string | null;
      note: string;
    }>;
    generation_binding_vectors: Array<{
      name: string;
      raw: string;
      expect: number | null;
      note?: string;
    }>;
    reason_vectors: Array<{
      name: string;
      body: unknown;
      expect: string | null;
      note?: string;
    }>;
    byte_boundary_vectors: Array<{
      name: string;
      pad_char: string;
      target_bytes: number;
      accept: boolean;
      note: string;
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
    expect(vectors.max_signed_body_length).toBe(MAX_SIGNED_BODY_LENGTH);
    expect(vectors.max_sentinel_bytes).toBe(MAX_SENTINEL_BYTES);
    expect(vectors.max_safe_generation).toBe(Number.MAX_SAFE_INTEGER);
    expect(vectors.accepted_delivery).toBe('accepted');
    // The file ceiling pinned as a *derivation* rather than as a literal: it has to
    // exceed the two fields the document must carry together. Restating the number
    // would have been satisfied by the broken value — a flat 8192, below the
    // 21852-character signed-body bound, which made the writer store documents this
    // reader then refused while telling the operator the abort was recorded.
    expect(vectors.max_sentinel_bytes)
      .toBeGreaterThan(vectors.max_signed_body_length + vectors.max_envelope_length);
  });

  it.each(vectors.vectors.map((vector) => [vector.name, vector] as const))(
    'agrees with the Python reader on %s',
    (_name, vector) => {
      const validated = validateAbortSentinel(vector.document, fixtureBinding);

      if (vector.accept) {
        expect(validated).not.toBeNull();
        expect(validated!.run_id).toBe(fixtureBinding.runId);
        expect(validated!.generation).toBe(fixtureBinding.generation);
        // No `reason` on the validated payload, on either side: it is derived from
        // the signed bytes by the finalizer, and `reason_vectors` below pins that
        // derivation. A document field of this name is what allowed fabricated text
        // to be attributed to a human.
        expect(validated).not.toHaveProperty('reason');
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

  it.each(vectors.reason_vectors.map((vector) => [vector.name, vector] as const))(
    'bounds the reason %s identically to Python',
    (_name, vector) => {
      // The reason itself is derived on the Python side only — this runtime writes
      // the signed bytes and never parses them. What has to agree is the *bounding*
      // applied to whatever comes out, so the same table drives Python's
      // `authorized_abort_reason` end-to-end and this runtime's bounding rule on the
      // value that function extracts. Divergence here would mean the two halves
      // disagreeing about what an operator's words are.
      const body = vector.body as { reason?: unknown };
      expect(boundSentinelReason(body.reason as string | null | undefined)).toBe(vector.expect);
    },
  );

  it.each(vectors.byte_boundary_vectors.map((vector) => [vector.name, vector] as const))(
    'applies the file ceiling in bytes for %s',
    (_name, vector) => {
      // Padding goes in an unknown extra field, which forward tolerance requires
      // both readers to ignore — so the only thing under test is the size rule.
      // The document is grown to land on exactly `target_bytes` of UTF-8.
      const base = {
        version: vectors.version,
        run_id: vectors.binding.run_id,
        generation: vectors.binding.generation,
        command_id: 'cmd-0001',
        requested_at: '2026-09-23T00:00:00Z',
        delivery: vectors.accepted_delivery,
        signed_body_base64: vectors.signed_body_base64,
        pad: '',
      };
      const padByteLength = Buffer.byteLength(vector.pad_char, 'utf8');
      const overhead = Buffer.byteLength(JSON.stringify(base), 'utf8');
      // Exact only when the target is reachable with a whole number of pad
      // characters, which is why the multibyte vectors target even byte counts.
      expect((vector.target_bytes - overhead) % padByteLength).toBe(0);
      const document = {
        ...base,
        pad: vector.pad_char.repeat((vector.target_bytes - overhead) / padByteLength),
      };
      const serialized = JSON.stringify(document);
      expect(Buffer.byteLength(serialized, 'utf8')).toBe(vector.target_bytes);

      putRaw(serialized);
      const read = readAbortSentinel(fixtureBinding, { sentinelPath });

      // `multibyte_at_ceiling` is the one that matters: ~16000 characters is under
      // any character-bounded read of 32092, so a text-mode read would accept the
      // one-past-ceiling case too. Both runtimes bound bytes before decoding.
      expect(read !== null).toBe(vector.accept);
    },
  );
});

describe('clearAbortSentinel', () => {
  it('removes an existing sentinel', () => {
    writeAbortSentinel(VALID_INPUT, { sentinelPath });
    clearAbortSentinel({ sentinelPath });
    expect(readAbortSentinel(BINDING, { sentinelPath })).toBeNull();
  });

  it('is a no-op when no sentinel exists', () => {
    expect(() => clearAbortSentinel({ sentinelPath })).not.toThrow();
  });
});
