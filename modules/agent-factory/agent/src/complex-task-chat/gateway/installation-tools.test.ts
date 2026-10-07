import { resilientQuery } from '../../utils/resilientQuery';
import { runQuery } from '../run-query';
import { buildToolSanitizers } from '../tool-sanitizers';
import { ChatDataClient, ChatDataError } from './chat-data-client';
import { installationToolsForTurn } from './installation-tools';

jest.mock('@anthropic-ai/claude-agent-sdk', () => ({
  tool: jest.fn(),
  createSdkMcpServer: jest.fn(() => ({})),
}));
jest.mock('../../utils/resilientQuery', () => ({ resilientQuery: jest.fn() }));

const CANARY = 'internal-canary-secret-123';

test('installation tools expose only fixed bounded gateway reads', async () => {
  const runRequest = jest.fn(async () => ({ status: 'unavailable' }));
  const client = { runRequest } as unknown as ChatDataClient;
  const tools = installationToolsForTurn(client);
  expect(tools.map(tool => tool.name)).toEqual(['installation_status', 'installation_failure']);
  for (const tool of tools) {
    expect(Object.keys(tool.inputSchema)).toEqual(['installation_id']);
    const result = await tool.handler({ installation_id: 1234, aws_action: 'get-object' });
    expect(result.content).toEqual([{ type: 'text', text: '{"status":"unavailable"}' }]);
  }
  expect(runRequest.mock.calls).toEqual([
    ['installation/status', { installation_id: 1234 }],
    ['installation/failure', { installation_id: 1234 }],
  ]);
});

test('tool errors never return sensitive exception messages', async () => {
  const client = { runRequest: jest.fn(async () => { throw new Error('canary-secret-123'); }) } as unknown as ChatDataClient;
  expect((await installationToolsForTurn(client)[0].handler({ installation_id: 1234 })).content[0].text).toBe('{"status":"unavailable"}');
  const refused = { runRequest: jest.fn(async () => { throw new ChatDataError('denied', 404); }) } as unknown as ChatDataClient;
  expect((await installationToolsForTurn(refused)[1].handler({ installation_id: 1234 })).content[0].text).toBe('{"status":"denied"}');
});

describe.each(['status', 'failure'])('installation_%s input summaries', kind => {
  const tools = installationToolsForTurn({} as ChatDataClient);
  const name = `installation_${kind}`;

  test.each([1, 1234, Number.MAX_SAFE_INTEGER])('retains only a valid installation ID: %s', installationId => {
    const sanitize = buildToolSanitizers(tools).get(name);
    const input = { installation_id: installationId, credential_ref: CANARY, path: CANARY, nested: { secret: CANARY } };
    expect(sanitize?.(input)).toEqual({ installation_id: installationId });
    expect(input.credential_ref).toBe(CANARY);
  });

  test.each([
    undefined, null, 0, -1, 1.5, Number.NaN, Infinity, Number.MAX_SAFE_INTEGER + 1,
    '1234', CANARY, { secret: CANARY }, [1234],
  ])('omits an invalid installation ID: %p', installationId => {
    const sanitize = buildToolSanitizers(tools).get(name);
    expect(sanitize?.({ installation_id: installationId, credential_ref: CANARY })).toEqual({});
  });

  describe.each(['', 'mcp__chat-agent-tools__'])('%s logging and progress', prefix => {
    test.each([
      ['unsupported credential reference', { installation_id: 1234, credential_ref: CANARY }],
      ['unsupported preferred summary field', { installation_id: 1234, path: CANARY }],
      ['secret in malformed installation ID', { installation_id: CANARY, credential_ref: CANARY }],
    ])('never emits %s', async (_label, input) => {
      const toolName = `${prefix}${name}`;
      (resilientQuery as jest.Mock).mockImplementationOnce(async function* () {
        yield { type: 'assistant', message: { content: [{ type: 'tool_use', name: toolName, input }] } };
        yield { type: 'result', usage: { input_tokens: 1, output_tokens: 1 }, num_turns: 1 };
      });
      const log = jest.fn();
      const onProgress = jest.fn();

      const result = await runQuery({
        systemPrompt: 'Inspect installation diagnostics.',
        history: [],
        userMessage: 'Show installation status.',
        tools,
        toolSanitizers: buildToolSanitizers(tools),
        log,
        onProgress,
      });

      expect(result.turnCount).toBe(1);
      expect(log).toHaveBeenCalledWith(`[run-query] turn 1 tool_use: ${toolName}`);
      expect(JSON.stringify(log.mock.calls)).not.toContain(CANARY);
      expect(onProgress).toHaveBeenCalledWith({
        type: 'tool_use', tool_name: toolName, input_summary: '', turn: 1,
      });
      expect(JSON.stringify(onProgress.mock.calls)).not.toContain(CANARY);
    });
  });
});
