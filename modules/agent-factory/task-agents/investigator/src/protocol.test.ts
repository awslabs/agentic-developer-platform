/**
 * Contract conformance for the process protocol — Task API T5 (#5798).
 *
 * Every assertion here is driven by T0's published fixtures, consumed unchanged.
 * Covers T5-AC04 (the agent stays inside its scoped model/credential/budget
 * grant and consumes input and cancellation per the frozen contract) and the
 * refusal half of T5-AC05 (malformed output cannot be reported as success).
 */

import assert from 'node:assert/strict';
import { test, describe } from 'node:test';

import { asLine, loadFixture, loadFixtures } from './fixtures.js';
import {
  assertChildFrame,
  assertInvestigatorReport,
  encodeChildFrame,
  MAX_FRAME_BYTES,
  MAX_PROGRESS_EVENT_BYTES,
  parseHostFrame,
  ProtocolViolation,
  type ChildFrame,
} from './protocol.js';

const TASK_ID = 'tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20';
const UUID = '1e14b86d-50d6-45e6-9cc9-5f4cc30365aa';
const REPORT_ID = '9a734313-2f31-484b-9349-d52c41ad2497';

describe('published valid fixtures are accepted', () => {
  // The host-side frames: whatever the contract says the host may send, this
  // child must be able to read. A rejection here means the agent would fail a
  // run the platform considers well-formed.
  for (const fixture of [
    loadFixture('valid', 'process-start-frame.json'),
    loadFixture('valid', 'process-start-artifact-reference-frame.json'),
    loadFixture('valid', 'process-artifact-chunk-frame.json'),
    loadFixture('valid', 'process-cancel-frame.json'),
    loadFixture('valid', 'process-model-result-unknown-frame.json'),
  ]) {
    test(`${fixture.name} parses as a host frame`, () => {
      const frame = parseHostFrame(asLine(fixture));
      assert.equal(frame.type, fixture.body['type']);
      assert.equal(frame.task_id, fixture.body['task_id']);
    });
  }

  // The child-side frames: the fixtures are the shapes this agent is expected to
  // emit, so the outbound validator must accept them verbatim.
  for (const fixture of [
    loadFixture('valid', 'process-progress-frame.json'),
    loadFixture('valid', 'process-model-request-frame.json'),
    loadFixture('valid', 'process-input-required-frame.json'),
    loadFixture('valid', 'process-result-frame.json'),
  ]) {
    test(`${fixture.name} passes outbound validation`, () => {
      assert.doesNotThrow(() => assertChildFrame(fixture.body as unknown as ChildFrame));
    });
  }
});

describe('published invalid fixtures are refused', () => {
  // Each of these encodes a boundary rather than a typo, and the reason is
  // asserted (not just "it threw") so a test cannot pass because the frame was
  // rejected for some unrelated reason.
  const cases: Array<{ file: string; expect: RegExp; boundary: string }> = [
    { file: 'process-artifact-chunk-credential.json', expect: /forbidden field run_credential/, boundary: 'chunk transport is closed and bounded' },
    { file: 'process-artifact-chunk-invalid-base64.json', expect: /base64/, boundary: 'chunk transport is closed and bounded' },
    { file: 'process-artifact-chunk-oversize.json', expect: /permitted range/, boundary: 'chunk transport is closed and bounded' },
    { file: 'process-artifact-chunk-sequence-zero.json', expect: /permitted range/, boundary: 'chunk transport is closed and bounded' },

    {
      file: 'process-start-frame-carries-credentials.json',
      expect: /forbidden field (aws_access_key_id|aws_secret_access_key|github_token)/,
      boundary: 'the child receives task content only, never a credential',
    },
    {
      file: 'process-cancel-frame-unintentional.json',
      expect: /intentional: true/,
      boundary: 'cancellation must assert intent so it cannot be retried as a fault',
    },
  ];

  for (const { file, expect, boundary } of cases) {
    test(`${file} is refused — ${boundary}`, () => {
      const fixture = loadFixture('invalid', file);
      assert.throws(
        () => parseHostFrame(asLine(fixture)),
        (error: unknown) => {
          assert.ok(error instanceof ProtocolViolation, 'refusal must be a ProtocolViolation');
          assert.match(error.message, expect);
          return true;
        },
      );
    });
  }

  const outbound: Array<{ file: string; expect: RegExp; boundary: string }> = [
    {
      file: 'process-progress-frame-leaks-reasoning.json',
      expect: /forbidden field (percent_complete|reasoning|thinking)/,
      boundary: 'private reasoning and fabricated percentages stay off the public surface',
    },
    {
      file: 'process-model-request-selects-model.json',
      expect: /forbidden field (model|endpoint|region)/,
      boundary: 'the host holds the model binding, not the child',
    },
    {
      file: 'process-result-frame-claims-host-fields.json',
      expect: /forbidden field (total_usd|turns_used)/,
      boundary: 'the child reports content; the host owns the ledger',
    },
  ];

  for (const { file, expect, boundary } of outbound) {
    test(`${file} cannot be emitted — ${boundary}`, () => {
      const fixture = loadFixture('invalid', file);
      assert.throws(
        () => assertChildFrame(fixture.body as unknown as ChildFrame),
        (error: unknown) => {
          assert.ok(error instanceof ProtocolViolation);
          assert.match(error.message, expect);
          return true;
        },
      );
    });
  }

  test('result-finding-without-evidence.json is not a reportable report', () => {
    // T5-AC05 and the design's grounding rule: a confident claim with no
    // provenance belongs in uncertainties, and must never reach a result frame.
    const fixture = loadFixture('invalid', 'result-finding-without-evidence.json');
    assert.throws(
      () => assertInvestigatorReport(fixture.body),
      (error: unknown) => {
        assert.ok(error instanceof ProtocolViolation);
        assert.match(error.message, /at least one evidence_ref/);
        return true;
      },
    );
  });
});

describe('fixture corpus coverage', () => {
  test('every published process-protocol fixture is exercised by name', () => {
    // Guards against the corpus growing without these tests noticing. A new
    // process fixture is a new contract rule, and silently ignoring it would let
    // the package drift from the contract while the suite stayed green.
    const published = [
      ...loadFixtures('valid', 'process-').map((f) => `valid/${f.name}`),
      ...loadFixtures('invalid', 'process-').map((f) => `invalid/${f.name}`),
    ].sort();

    assert.deepEqual(published, [
      'invalid/process-artifact-chunk-credential.json',
      'invalid/process-artifact-chunk-invalid-base64.json',
      'invalid/process-artifact-chunk-oversize.json',
      'invalid/process-artifact-chunk-sequence-zero.json',
      'invalid/process-cancel-frame-unintentional.json',
      'invalid/process-model-request-selects-model.json',
      'invalid/process-progress-frame-leaks-reasoning.json',
      'invalid/process-result-frame-claims-host-fields.json',
      'invalid/process-start-frame-carries-credentials.json',
      'valid/process-artifact-chunk-frame.json',
      'valid/process-cancel-frame.json',
      'valid/process-input-required-frame.json',
      'valid/process-model-request-frame.json',
      'valid/process-model-result-unknown-frame.json',
      'valid/process-progress-frame.json',
      'valid/process-result-frame.json',
      'valid/process-start-artifact-reference-frame.json',
      'valid/process-start-frame.json',
    ]);
  });

  test('fixtures claiming T5 ownership are all consumed here', () => {
    const t5 = [...loadFixtures('valid', 'process-'), ...loadFixtures('invalid', 'process-')].filter(
      (f) => f.meta.owner === 'T5',
    );
    assert.ok(t5.length >= 6, 'expected the T5-owned process fixtures to be present');
  });
});

describe('unknown-outcome and limit discipline', () => {
  test('an unknown model outcome must carry null content', () => {
    // The contract pairs unknown with null content. Accepting unknown alongside
    // content would let a partial or stale completion be read as an answer,
    // which is exactly the honest-unknown rule this protects.
    const line = JSON.stringify({
      protocol_version: 1,
      type: 'model.result',
      request_id: UUID,
      task_id: TASK_ID,
      turn_id: '2048c8ca-b910-43c8-861e-a092beb53813',
      operation_status: 'unknown',
      content: [{ type: 'text', text: 'partial' }],
    });
    assert.throws(() => parseHostFrame(line), /unknown model outcome must carry null content/);
  });

  test('a confirmed model outcome must carry content', () => {
    const line = JSON.stringify({
      protocol_version: 1,
      type: 'model.result',
      request_id: UUID,
      task_id: TASK_ID,
      turn_id: '2048c8ca-b910-43c8-861e-a092beb53813',
      operation_status: 'confirmed',
      content: null,
    });
    assert.throws(() => parseHostFrame(line), /confirmed model outcome must carry content/);
  });

  test('start limits above the fixed pilot ceilings are refused', () => {
    // limits.json fixes 8 turns and 4096 output tokens per turn. A host frame
    // claiming more is not a larger grant to honour — it is a frame that does not
    // match the contract the grant was issued under.
    const fixture = loadFixture('valid', 'process-start-frame.json');
    const overreaching = {
      ...fixture.body,
      limits: { max_turns: 99, max_output_tokens_per_turn: 4096 },
    };
    assert.throws(() => parseHostFrame(JSON.stringify(overreaching)), /outside its permitted range/);
  });

  test('a turn number beyond the turn ceiling is refused', () => {
    const line = JSON.stringify({
      protocol_version: 1,
      type: 'turn',
      request_id: UUID,
      task_id: TASK_ID,
      turn_id: '2048c8ca-b910-43c8-861e-a092beb53813',
      turn_number: 9,
      messages: [{ command_id: 'f6071829-3a4b-4c5d-9f70-819203142536', text: 'continue' }],
    });
    assert.throws(() => parseHostFrame(line), /turn_number is outside its permitted range/);
  });


  test('nested protocol objects reject unknown fields', () => {
    const fixture = loadFixture('valid', 'process-start-frame.json');
    const artifact = (fixture.body['artifacts'] as Array<Record<string, unknown>>)[0];
    const withArtifactField = {
      ...fixture.body,
      artifacts: [{ ...artifact, download_url: 'https://example.invalid/evidence' }],
    };
    assert.throws(
      () => parseHostFrame(JSON.stringify(withArtifactField)),
      /unrecognised field download_url on a start frame artifact/,
    );

    const withLimitField = {
      ...fixture.body,
      limits: { ...(fixture.body['limits'] as object), max_usd: 2 },
    };
    assert.throws(
      () => parseHostFrame(JSON.stringify(withLimitField)),
      /unrecognised field max_usd on a start frame limits/,
    );
  });

  test('a turn rejects duplicate commands and unknown message fields', () => {
    const message = {
      command_id: 'f6071829-3a4b-4c5d-9f70-819203142536',
      text: 'continue',
    };
    const base = {
      protocol_version: 1,
      type: 'turn',
      request_id: UUID,
      task_id: TASK_ID,
      turn_id: '2048c8ca-b910-43c8-861e-a092beb53813',
      turn_number: 2,
    };
    assert.throws(
      () => parseHostFrame(JSON.stringify({ ...base, messages: [message, message] })),
      /cannot list the same command_id more than once/,
    );
    assert.throws(
      () => parseHostFrame(JSON.stringify({ ...base, messages: [{ ...message, authority: 'admin' }] })),
      /unrecognised field authority on a turn message/,
    );
  });

  test('model stop reasons respect the schema character bound', () => {
    const line = JSON.stringify({
      protocol_version: 1,
      type: 'model.result',
      request_id: UUID,
      task_id: TASK_ID,
      turn_id: '2048c8ca-b910-43c8-861e-a092beb53813',
      operation_status: 'confirmed',
      content: [],
      stop_reason: 'x'.repeat(65),
    });
    assert.throws(() => parseHostFrame(line), /stop_reason exceeds its 64-character bound/);
  });
});

describe('frame envelope discipline', () => {
  test('a frame with an unrecognised field is refused', () => {
    const fixture = loadFixture('valid', 'process-start-frame.json');
    const extended = { ...fixture.body, extra_field: 'unexpected' };
    assert.throws(() => parseHostFrame(JSON.stringify(extended)), /unrecognised field extra_field/);
  });

  test('a mismatched protocol version is refused rather than guessed at', () => {
    const fixture = loadFixture('valid', 'process-start-frame.json');
    const future = { ...fixture.body, protocol_version: 2 };
    assert.throws(() => parseHostFrame(JSON.stringify(future)), /unsupported protocol_version/);
  });

  test('a malformed task_id is refused', () => {
    const fixture = loadFixture('valid', 'process-start-frame.json');
    const bad = { ...fixture.body, task_id: 'task-1234' };
    assert.throws(() => parseHostFrame(JSON.stringify(bad)), /task_id does not match/);
  });

  test('non-JSON input is refused without crashing the process', () => {
    assert.throws(() => parseHostFrame('not json at all'), /not valid JSON/);
    assert.throws(() => parseHostFrame('[1,2,3]'), /not a JSON object/);
    assert.throws(() => parseHostFrame('{"protocol_version":1}'), /no string type/);
  });

  test('an oversized host frame is refused before parsing', () => {
    const line = JSON.stringify({ type: 'start', pad: 'x'.repeat(MAX_FRAME_BYTES) });
    assert.throws(() => parseHostFrame(line), /exceeds the 65536-byte bound/);
  });

  test('ready advertises only capabilities this agent implements', () => {
    // v1 permits input and cancel only. Advertising pause or resume would tell a
    // caller yes for a verb with nothing behind it.
    assert.throws(
      () =>
        assertChildFrame({
          protocol_version: 1,
          type: 'ready',
          request_id: UUID,
          task_id: TASK_ID,
          capabilities: ['input', 'pause'],
        } as unknown as ChildFrame),
      /only implemented input\/cancel capabilities/,
    );
  });

  test('an oversized progress message is refused', () => {
    assert.throws(
      () =>
        encodeChildFrame({
          protocol_version: 1,
          type: 'progress',
          request_id: UUID,
          task_id: TASK_ID,
          report_id: REPORT_ID,
          message: 'x'.repeat(MAX_PROGRESS_EVENT_BYTES + 1),
        }),
      /exceeds/,
    );
  });

  test('encoded frames are newline-delimited single lines', () => {
    // The host reads one JSON object per line incrementally. An embedded newline
    // would split one frame into two unparseable halves.
    const line = encodeChildFrame({
      protocol_version: 1,
      type: 'progress',
      request_id: UUID,
      task_id: TASK_ID,
      report_id: REPORT_ID,
      message: 'Inventoried 1 artifact.\nSecond line of authored text.',
      stage: 'evidence_inventory',
    });
    assert.equal(line.endsWith('\n'), true);
    assert.equal(line.trimEnd().includes('\n'), false);
  });
});

describe('report shape', () => {
  test('the published valid report is accepted', () => {
    const fixture = loadFixture('valid', 'process-result-frame.json');
    assert.doesNotThrow(() => assertInvestigatorReport(fixture.body['report']));
  });

  test('an evidence_ref citing a fetched source is refused', () => {
    // There is no fetch: provenance is limited to what the caller supplied. A
    // "url" source would imply the investigator retrieved something it cannot.
    assert.throws(
      () =>
        assertInvestigatorReport({
          summary: 'Summary.',
          findings: [],
          uncertainties: [],
          recommendations: [],
          evidence_refs: [{ ref: 'https://example.com/page', source: 'url' }],
        }),
      /caller-supplied provenance/,
    );
  });

  test('a report with unknown fields is refused', () => {
    assert.throws(
      () =>
        assertInvestigatorReport({
          summary: 'Summary.',
          findings: [],
          uncertainties: [],
          recommendations: [],
          evidence_refs: [],
          confidence_score: 0.9,
        }),
      /unknown field confidence_score/,
    );
  });

  test('a non-object report is refused rather than coerced', () => {
    for (const candidate of [null, 'a summary', 42, []]) {
      assert.throws(() => assertInvestigatorReport(candidate), ProtocolViolation);
    }
  });
});
