const assert = require('node:assert/strict');
const { readFile, unlink } = require('node:fs/promises');
const { randomBytes } = require('node:crypto');
const { executeCrewTurn } = require('../dist/complex-task-chat/crew-turn');
const { modelRequestSchema } = require('../dist/complex-task-chat/gateway/chat-model-contract');

async function smoke(modelId, content, expectToolError = false) {
  let calls = 0;
  let toolResultSeen = false;
  let published = 0;
  let recorded;
  const file = `/tmp/workspace/crew-sdk-${randomBytes(8).toString('hex')}.txt`;
  const client = {
    sessionMode: async () => 'persistent',
    withSignal() { return this; },
    invokeModel: async (_operation, request, _listener, deliverResponse) => {
      modelRequestSchema.parse(request);
      assert.equal(deliverResponse, true);
      calls++;
      const toolResult = request.messages.flatMap(message => Array.isArray(message.content) ? message.content : [])
        .find(block => block.type === 'tool_result');
      if (toolResult) {
        toolResultSeen = true;
        assert.equal(Boolean(toolResult.is_error), expectToolError);
      }
      if (content !== undefined && !toolResult) {
        assert(request.tools.some(entry => entry.name === 'mcp__workspace__write_file'));
        return { modelId, content: [{ type: 'tool_use', id: 'tool_smoke', name: 'mcp__workspace__write_file',
          input: { file_path: file, content } }], stopReason: 'tool_use', usage: { input_tokens: 5, output_tokens: 3 } };
      }
      return { modelId, content: [{ type: 'text', text: 'Hello.' }],
        stopReason: 'end_turn', usage: { input_tokens: 5, output_tokens: 3 } };
    },
    publishToolCall: async (toolCallId, name, args) => {
      assert.equal(toolCallId, 'tool_smoke');
      assert.equal(name, 'mcp__workspace__write_file');
      assert.deepEqual(args, { file_path: file, content });
      published++;
    },
    submitTurnResult: async () => assert.fail('SDK turn failed'),
  };
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20_000);
  try {
    await executeCrewTurn({ client, model: { modelId },
      context: { userMessage: content === undefined ? 'Say hello.' : 'Write the supplied content to the test file.',
        messages: [], memories: [], attachments: [] },
      data: { tools: () => [], record: async reply => { recorded = reply; } },
    }, controller.signal);
    assert.equal(recorded, 'Hello.');
    assert.equal(calls, content === undefined ? 1 : 2);
    assert.equal(published, content === undefined ? 0 : 1);
    assert.equal(toolResultSeen, content !== undefined);
    if (content !== undefined && !expectToolError) assert.equal(await readFile(file, 'utf8'), content);
    else await assert.rejects(readFile(file), { code: 'ENOENT' });
    console.log(`SDK executor smoke passed: ${modelId}, ${content === undefined ? 'text' : expectToolError ? 'tool error' : 'tool result'}`);
  } finally {
    clearTimeout(timeout);
    await unlink(file).catch(() => undefined);
  }
}

async function main() {
  const models = ['global.anthropic.claude-sonnet-4-6', 'global.anthropic.claude-opus-4-5-20251101-v1:0',
    'global.anthropic.claude-haiku-4-5-20251001-v1:0'];
  for (const modelId of models) {
    await smoke(modelId);
    await smoke(modelId, 'hello');
  }
  await smoke(models[0], 'x'.repeat(32_000));
  await smoke(models[0], '界'.repeat(6_000));
  await smoke(models[0], 'x'.repeat(32_001), true);
}

main().catch(error => { console.error(String(error).slice(0, 500)); process.exitCode = 1; });
