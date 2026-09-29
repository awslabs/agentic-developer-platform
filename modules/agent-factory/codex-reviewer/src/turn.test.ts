import assert from "node:assert/strict";
import test from "node:test";
import { interruptedTransport, runResumableTurn } from "./turn.js";

const completed = { items: [], finalResponse: "verified", usage: {
  input_tokens: 1, output_tokens: 1, cached_input_tokens: 0, cache_write_input_tokens: 0, reasoning_output_tokens: 0,
} };

test("an interrupted turn resumes the existing thread with the same schema and deadline", async () => {
  const options = { signal: new AbortController().signal, outputSchema: { type: "object" } };
  let calls = 0;
  const result = await runResumableTurn({ id: "retained-thread", run: async (prompt, actual) => {
    assert.equal(actual, options);
    if (++calls === 1) throw new Error("stream disconnected before completion: idle timeout waiting for SSE");
    assert.match(String(prompt), /Preserve completed work/);
    return completed;
  } }, "review", options, async () => {});
  assert.equal(result, completed);
  assert.equal(calls, 2);
});

test("a truncated SDK event resumes once, but repeated failure exits", async () => {
  let calls = 0;
  await assert.rejects(runResumableTurn({ id: "thread", run: async () => {
    calls++;
    throw new Error("Failed to parse item: truncated event", { cause: new SyntaxError("Unterminated string") });
  } }, "repair", {}, async () => {}), /Failed to parse/);
  assert.equal(calls, 2);
});

test("missing turn completion is never accepted as a successful repair", async () => {
  let calls = 0;
  await assert.rejects(runResumableTurn({ id: "thread", run: async () => {
    calls++; return { ...completed, usage: null };
  } }, "repair", {}, async () => {}), /missing turn.completed/);
  assert.equal(calls, 2);
});

test("provider rejection, failed checks and invalid verdicts never trigger transport recovery", async () => {
  for (const message of ["This request has been flagged for potentially high-risk cyber activity",
    "stream disconnected before completion: policy rejection", "required checks failed",
    "Codex did not report functional and security review completion", "HTTP 401 Unauthorized", "quota exceeded"]) {
    let calls = 0;
    await assert.rejects(runResumableTurn({ id: "thread", run: async () => {
      calls++; throw new Error(message);
    } }, "review", {}, async () => assert.fail("must not wait")));
    assert.equal(calls, 1);
  }
  assert.equal(interruptedTransport(new SyntaxError("invalid verdict JSON")), false);
});

test("no fresh thread is launched when identity is missing or the deadline expired", async () => {
  let calls = 0;
  await assert.rejects(runResumableTurn({ id: null, run: async () => {
    calls++; throw new Error("stream disconnected before completion");
  } }, "review", {}, async () => assert.fail("no thread to resume")));
  assert.equal(calls, 1);
  const abort = new AbortController();
  await assert.rejects(runResumableTurn({ id: "thread", run: async () => {
    abort.abort(); throw new Error("stream disconnected before completion");
  } }, "review", { signal: abort.signal }, async () => assert.fail("deadline expired")));
});

test('reviewer streams activity before completion while retaining its structured verdict', async () => {
  const seen: string[] = [];
  const result = await runResumableTurn({ id: 'review-session',
    run: async () => assert.fail('must use streaming SDK'),
    runStreamed: async () => ({ events: (async function* () {
      yield { type: 'thread.started' as const, thread_id: 'review-session' };
      assert.deepEqual(seen, ['thread.started']);
      yield { type: 'item.started' as const, item: { type: 'command_execution' as const,
        id: 'command', command: 'npm test', status: 'in_progress' as const, aggregated_output: '' } };
      assert.equal(seen.at(-1), 'item.started');
      yield { type: 'item.completed' as const, item: { type: 'agent_message' as const,
        id: 'final', text: '{"verdict":"approve"}' } };
      yield { type: 'turn.completed' as const, usage: completed.usage };
    })() }),
  }, 'review', { outputSchema: { type: 'object' } }, undefined, undefined, event => { seen.push(event.type); });
  assert.equal(result.finalResponse, '{"verdict":"approve"}');
  assert.equal(result.usage, completed.usage);
});
