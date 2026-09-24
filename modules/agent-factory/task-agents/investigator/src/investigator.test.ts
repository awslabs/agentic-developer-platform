/**
 * Behavioural tests for the investigation — Task API T5 (#5798).
 *
 * Driven by a deterministic in-process host, so every frame the agent emits and
 * every model outcome it handles is observable without spawning a process or
 * reaching a real model. That is what makes ordering assertions — progress
 * *before* the result (T5-AC02) — possible at all.
 *
 * Covers T5-AC01 (useful work with no GitHub anything), T5-AC02 (substantive
 * authored progress before the run ends), T5-AC04 (scoped limits, input and
 * cancellation) and T5-AC05 (failure and malformed output cannot be reported as
 * success).
 */

import assert from 'node:assert/strict';
import { test, describe } from 'node:test';

import { TaskControlAdapter, isControlCancellation } from './control.js';
import { loadFixture } from './fixtures.js';
import {
  investigate,
  ModelOutcomeUnknownError,
  ModelRejectedError,
  OUTPUT_TOKEN_CEILING,
  TURN_CEILING,
  type HostBridge,
  type ModelOutcome,
} from './investigator.js';
import { InvalidAgentOutputError } from './report.js';
import { assertChildFrame, type Stage, type StartFrame } from './protocol.js';

/** The published start fixture, used unchanged as the task input. */
function startFixture(overrides: Partial<StartFrame> = {}): StartFrame {
  const body = loadFixture('valid', 'process-start-frame.json').body as unknown as StartFrame;
  return { ...body, ...overrides };
}

interface Recorded {
  progress: Array<{ message: string; stage: Stage }>;
  modelCalls: Array<{ messages: unknown[]; system: string; maxTokens: number }>;
  prompts: string[];
}

/**
 * A scripted host.
 *
 * Returns queued outcomes in order, so a test can describe an exact sequence —
 * a malformed reply, then an unknown outcome — and assert what the agent does
 * with it.
 */
function scriptedHost(
  outcomes: ModelOutcome[],
  options: { answer?: string | null } = {},
): { host: HostBridge; recorded: Recorded } {
  const recorded: Recorded = { progress: [], modelCalls: [], prompts: [] };
  let index = 0;

  const host: HostBridge = {
    async progress(message, stage) {
      recorded.progress.push({ message, stage });
    },
    async model(request) {
      recorded.modelCalls.push(request);
      const outcome = outcomes[index];
      index += 1;
      if (outcome === undefined) {
        throw new Error('the agent requested more model calls than the test scripted');
      }
      return outcome;
    },
    async askCaller(prompt) {
      recorded.prompts.push(prompt);
      const answer = options.answer;
      return answer === undefined || answer === null
        ? null
        : { kind: 'steering', text: answer, command_id: 'aa48ec29-3a5c-4dc4-9617-62fe5652fd7a' };
    },
  };
  return { host, recorded };
}

const GOOD_REPORT = JSON.stringify({
  summary: 'Connection-pool exhaustion is the most likely cause of the 503 burst.',
  findings: [
    {
      statement: 'Pool acquisition timeouts precede the observed 503 responses.',
      evidence_refs: ['logs.txt:L2-L3'],
      confidence: 'high',
    },
  ],
  uncertainties: ['Upstream service logs for the window were not supplied.'],
  recommendations: ['Collect inventory-service logs for 14:05-14:40 UTC.'],
});

/** The fixture's inputs name `checkout-api`, so artifacts are named `logs.txt`. */
function startWithNamedArtifact(): StartFrame {
  const base = startFixture();
  return {
    ...base,
    inputs: { ...(base.inputs ?? {}), evidence_file: 'logs.txt' },
  };
}

describe('T5-AC01: useful work with no GitHub or credential dependency', () => {
  test('produces a cited report from caller-supplied evidence alone', async () => {
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    const outcome = await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());

    assert.equal(outcome.report.findings.length, 1);
    assert.deepEqual(outcome.report.findings[0]?.evidence_refs, ['logs.txt:L2-L3']);
    assert.equal(outcome.report.evidence_refs[0]?.source, 'artifact');
    assert.ok(outcome.report.summary.length > 0);
    assert.equal(recorded.modelCalls.length, 1);
  });

  test('the model request names no model, endpoint, region or key', async () => {
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());

    // The agent may only supply messages, a system prompt and a token bound.
    const call = recorded.modelCalls[0];
    assert.deepEqual(Object.keys(call ?? {}).sort(), ['maxTokens', 'messages', 'system']);
  });

});

describe('T5-AC02: substantive progress before the run finishes', () => {
  test('emits at least two distinct authored updates before the report', async () => {
    // limits.json requires two distinct authored progress markers; a heartbeat or
    // buffered final output explicitly does not satisfy this.
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());

    assert.ok(recorded.progress.length >= 2, 'expected at least two progress updates');
    const messages = new Set(recorded.progress.map((p) => p.message));
    assert.equal(messages.size, recorded.progress.length, 'each update must be distinct');
  });

  test('the first update is authored from the evidence, not a placeholder', async () => {
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());

    const first = recorded.progress[0];
    assert.equal(first?.stage, 'evidence_inventory');
    // Names what was actually supplied: one artifact, its byte count, two inputs.
    assert.match(first?.message ?? '', /1 artifact\(s\)/);
    assert.match(first?.message ?? '', /logs\.txt/);
  });

  test('progress stages follow the designed sequence', async () => {
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());

    assert.deepEqual(
      recorded.progress.map((p) => p.stage),
      ['evidence_inventory', 'analysis', 'synthesis'],
    );
  });

  test('progress carries no private reasoning and passes frame validation', async () => {
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());

    for (const entry of recorded.progress) {
      // Every authored message must be emittable as a contract-valid frame.
      assert.doesNotThrow(() =>
        assertChildFrame({
          protocol_version: 1,
          type: 'progress',
          request_id: '1e14b86d-50d6-45e6-9cc9-5f4cc30365aa',
          task_id: 'tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20',
          report_id: '9a734313-2f31-484b-9349-d52c41ad2497',
          message: entry.message,
          stage: entry.stage,
        }),
      );
      assert.doesNotMatch(entry.message, /chain of thought|internal deliberation/i);
      assert.doesNotMatch(entry.message, /\d+%/, 'no fabricated completion percentage');
    }
  });
});

describe('T5-AC04: scoped limits, input and cancellation', () => {
  test('clamps caller limits to the fixed pilot ceilings', async () => {
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await investigate(
      { ...startWithNamedArtifact(), limits: { max_output_tokens_per_turn: OUTPUT_TOKEN_CEILING } },
      host,
      new TaskControlAdapter(),
    );
    assert.equal(recorded.modelCalls[0]?.maxTokens, OUTPUT_TOKEN_CEILING);
  });

  test('a lower caller limit is honoured rather than raised to the ceiling', async () => {
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await investigate(
      { ...startWithNamedArtifact(), limits: { max_turns: 1, max_output_tokens_per_turn: 512 } },
      host,
      new TaskControlAdapter(),
    );
    assert.equal(recorded.modelCalls[0]?.maxTokens, 512);
    assert.equal(recorded.modelCalls.length, 1);
  });

  test('never exceeds the turn ceiling even when clarification is needed', async () => {
    // Every reply is ungroundable, so the agent would keep going if unbounded.
    const ungrounded = JSON.stringify({
      summary: 'Unclear.',
      findings: [{ statement: 'Something failed.', evidence_refs: ['nonexistent.txt'] }],
      uncertainties: [],
      recommendations: [],
    });
    const outcomes: ModelOutcome[] = Array.from({ length: TURN_CEILING + 2 }, () => ({
      status: 'confirmed' as const,
      text: ungrounded,
    }));
    const { host, recorded } = scriptedHost(outcomes, { answer: 'Use the same logs.' });

    await investigate({ ...startWithNamedArtifact(), limits: { max_turns: 2 } }, host, new TaskControlAdapter());
    assert.ok(recorded.modelCalls.length <= 2, `requested ${recorded.modelCalls.length} turns`);
  });

  test('a follow-up input is consumed once, in one turn', async () => {
    const control = new TaskControlAdapter();
    const commandId = 'f6071829-3a4b-4c5d-9f70-819203142536';
    assert.equal(control.admit({ kind: 'steering', text: 'Also check config.', command_id: commandId }), 'delivered');
    // A redelivered command must not be queued a second time.
    assert.equal(control.admit({ kind: 'steering', text: 'Also check config.', command_id: commandId }), 'rejected');

    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await investigate(startWithNamedArtifact(), host, control);

    const rendered = JSON.stringify(recorded.modelCalls[0]?.messages ?? []);
    const occurrences = rendered.split('Also check config.').length - 1;
    assert.equal(occurrences, 1, 'the command must appear exactly once in the conversation');
    assert.equal(control.hasUnconsumedInput(), false);
    assert.deepEqual(control.consumedCommandIds(), [commandId]);
  });

  test('cancellation stops the run with a typed error, not a retryable one', async () => {
    const control = new TaskControlAdapter();
    control.cancel('Client no longer needs the investigation.', 'f6071829-3a4b-4c5d-9f70-819203142536');
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);

    await assert.rejects(
      () => investigate(startWithNamedArtifact(), host, control),
      (error: unknown) => {
        // The structural marker is what keeps a deliberate stop out of the
        // generic retry path, which would otherwise start a fresh attempt.
        assert.equal(isControlCancellation(error), true);
        return true;
      },
    );
    assert.equal(recorded.modelCalls.length, 0, 'a cancelled run must not spend a model turn');
  });

  test('cancellation mid-run prevents further model calls', async () => {
    const control = new TaskControlAdapter();
    const { host, recorded } = scriptedHost([
      { status: 'confirmed', text: GOOD_REPORT },
      { status: 'confirmed', text: GOOD_REPORT },
    ]);
    const cancelling: HostBridge = {
      ...host,
      async model(request) {
        control.cancel('cancelled during the turn');
        return await host.model(request);
      },
    };

    await assert.rejects(() => investigate(startWithNamedArtifact(), cancelling, control), (error: unknown) => {
      assert.equal(isControlCancellation(error), true);
      return true;
    });
    assert.equal(recorded.modelCalls.length, 1);
  });

  test('input is refused once cancellation has latched', () => {
    const control = new TaskControlAdapter();
    control.cancel('stopping');
    assert.equal(control.admit({ kind: 'steering', text: 'one more thing' }), 'rejected');
  });

  test('v1 reports pause as unavailable rather than silently ignoring it', () => {
    const control = new TaskControlAdapter();
    assert.deepEqual(control.capabilities(), ['input', 'cancel']);
    assert.equal(control.requestPause().outcome, 'unavailable');
    assert.equal(control.resumeFromPause().outcome, 'unavailable');
  });
});

describe('T5-AC05: failure cannot be reported as success', () => {
  test('an unknown model outcome stops the run and is never resent', async () => {
    const { host, recorded } = scriptedHost([{ status: 'unknown' }, { status: 'confirmed', text: GOOD_REPORT }]);
    await assert.rejects(
      () => investigate(startWithNamedArtifact(), host, new TaskControlAdapter()),
      ModelOutcomeUnknownError,
    );
    // The second scripted outcome must go untouched: an unknown outcome may not
    // be retried, because the first call may already have been billed and run.
    assert.equal(recorded.modelCalls.length, 1);
  });

  test('a rejected grant fails the run', async () => {
    const { host } = scriptedHost([
      { status: 'rejected', code: 'budget_exceeded', message: 'the task budget is exhausted' },
    ]);
    await assert.rejects(
      () => investigate(startWithNamedArtifact(), host, new TaskControlAdapter()),
      ModelRejectedError,
    );
  });

  test('unparseable model output fails rather than yielding an empty report', async () => {
    const { host } = scriptedHost([{ status: 'confirmed', text: 'I was unable to complete this.' }]);
    await assert.rejects(
      () => investigate(startWithNamedArtifact(), host, new TaskControlAdapter()),
      InvalidAgentOutputError,
    );
  });

  test('output missing a summary fails rather than being patched up', async () => {
    const { host } = scriptedHost([
      { status: 'confirmed', text: JSON.stringify({ findings: [], uncertainties: [] }) },
    ]);
    await assert.rejects(
      () => investigate(startWithNamedArtifact(), host, new TaskControlAdapter()),
      InvalidAgentOutputError,
    );
  });

  test('an ungrounded finding is demoted, never returned as a cited finding', async () => {
    const fabricated = JSON.stringify({
      summary: 'A deployment caused the outage.',
      findings: [
        { statement: 'A deployment at 14:20 introduced the regression.', evidence_refs: [], confidence: 'high' },
        { statement: 'The rollback succeeded.', evidence_refs: ['deploy-log.txt'], confidence: 'high' },
      ],
      uncertainties: [],
      recommendations: [],
    });
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: fabricated }], {
      answer: null,
    });
    const outcome = await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());

    assert.equal(outcome.report.findings.length, 0, 'neither claim has supplied evidence');
    assert.equal(outcome.report.uncertainties.length, 2);
    assert.match(outcome.report.uncertainties.join(' '), /deployment at 14:20/);
    // deploy-log.txt was never supplied, so the dangling citation is named.
    assert.match(outcome.report.uncertainties.join(' '), /not found in the supplied material/);
    // Having nothing grounded is exactly when the caller is asked.
    assert.equal(recorded.prompts.length, 1);
  });

  test('a clarification answer is used when the caller supplies one', async () => {
    const ungrounded = JSON.stringify({
      summary: 'Unclear.',
      findings: [{ statement: 'Unsupported claim.', evidence_refs: [] }],
      uncertainties: [],
      recommendations: [],
    });
    const { host, recorded } = scriptedHost(
      [
        { status: 'confirmed', text: ungrounded },
        { status: 'confirmed', text: GOOD_REPORT },
      ],
      { answer: 'The relevant window is 14:10 to 14:12 in the supplied log.' },
    );
    const outcome = await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());

    assert.equal(recorded.prompts.length, 1);
    assert.equal(recorded.modelCalls.length, 2);
    assert.match(
      JSON.stringify(recorded.modelCalls[1]?.messages ?? []),
      /The relevant window is 14:10 to 14:12/,
    );
    assert.equal(outcome.report.findings.length, 1);
    assert.equal(outcome.askedForClarification, true);
    assert.ok(recorded.progress.some((p) => p.stage === 'clarification'));
  });

  test('a report returned to the host is always contract-valid', async () => {
    const { host } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    const outcome = await investigate(startWithNamedArtifact(), host, new TaskControlAdapter());
    assert.doesNotThrow(() =>
      assertChildFrame({
        protocol_version: 1,
        type: 'result',
        request_id: '7d1d96bb-29d0-43dc-9272-0a722889a128',
        task_id: 'tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20',
        report: outcome.report,
      }),
    );
  });
});

describe('follow-up admission races at completion', () => {
  for (const boundary of ['model', 'analysis', 'synthesis'] as const) {
    test(`consumes input arriving during ${boundary} before returning a report`, async () => {
      const control = new TaskControlAdapter();
      const commandId = 'f6071829-3a4b-4c5d-9f70-819203142536';
      let injected = false;
      const calls: unknown[][] = [];
      const inject = () => {
        if (!injected) {
          injected = true;
          assert.equal(control.admit({ kind: 'steering', text: 'Check the revised window.', command_id: commandId }), 'delivered');
        }
      };
      const host: HostBridge = {
        async model(request) {
          calls.push(structuredClone(request.messages));
          if (boundary === 'model') inject();
          return { status: 'confirmed', text: GOOD_REPORT };
        },
        async progress(_message, stage) {
          if (stage === boundary) inject();
        },
        async askCaller() { throw new Error('grounded reports must not request clarification'); },
      };
      const outcome = await investigate(startWithNamedArtifact(), host, control);
      assert.equal(outcome.turnsRequested, 2);
      assert.doesNotMatch(JSON.stringify(calls[0]), /revised window/);
      assert.equal(JSON.stringify(calls[1]).split('Check the revised window.').length - 1, 1);
      assert.deepEqual(control.consumedCommandIds(), [commandId]);
      assert.equal(control.hasUnconsumedInput(), false);
      assert.equal(control.admit({ kind: 'steering', text: 'After completion' }), 'rejected');
    });
  }

  test('exhausted turn budget fails without pretending late input was consumed', async () => {
    const control = new TaskControlAdapter();
    const { host, recorded } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    const racing: HostBridge = {
      ...host,
      async model(request) {
        control.admit({ kind: 'steering', text: 'Late input', command_id: 'late-command' });
        return host.model(request);
      },
    };
    await assert.rejects(
      investigate({ ...startWithNamedArtifact(), limits: { max_turns: 1 } }, racing, control),
      /turn budget was exhausted with admitted follow-up input still unconsumed/,
    );
    assert.equal(recorded.modelCalls.length, 1);
    assert.deepEqual(control.consumedCommandIds(), []);
    assert.equal(control.hasUnconsumedInput(), true);
  });

  test('cancellation during synthesis prevents a successful report', async () => {
    const control = new TaskControlAdapter();
    const { host } = scriptedHost([{ status: 'confirmed', text: GOOD_REPORT }]);
    await assert.rejects(investigate(startWithNamedArtifact(), {
      ...host,
      async progress(_message, stage) {
        if (stage === 'synthesis') control.cancel('cancel during synthesis');
      },
    }, control), isControlCancellation);
  });
});
