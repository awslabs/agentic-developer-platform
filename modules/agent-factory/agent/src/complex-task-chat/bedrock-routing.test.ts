import { EventEmitter } from 'node:events';
import { spawn } from 'node:child_process';
import { withChatBedrockRouting } from './bedrock-routing';
import { BedrockSummarizer } from './context/summarize/bedrock-summarizer';
import { BedrockRuntimeClient } from '@aws-sdk/client-bedrock-runtime';

jest.mock('node:child_process', () => ({ spawn: jest.fn() }));
jest.mock('@aws-sdk/client-bedrock-runtime', () => ({ BedrockRuntimeClient: jest.fn(), InvokeModelCommand: jest.fn() }));

const task = { task_id: 'task', session_id: 'session', message: 'hello', user_id: 'user', tenant_id: 'tenant', message_id: 'registered-run' };
let savedEnv: NodeJS.ProcessEnv;
let proxy: EventEmitter & { exitCode: number | null; kill: jest.Mock };
beforeEach(() => {
  savedEnv = process.env;
  process.env = { SIGV4_PROXY_TARGET: 'https://gateway.example/agent', AWS_ROLE_ARN: 'pod-role', AWS_ACCESS_KEY_ID: 'tool-key' };
  proxy = Object.assign(new EventEmitter(), { exitCode: null, kill: jest.fn() });
  (spawn as jest.Mock).mockReturnValue(proxy);
  jest.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true } as Response);
});
afterEach(() => {
  process.env = savedEnv;
  jest.restoreAllMocks();
  jest.clearAllMocks();
});

test('Claude and summarizer use the same run proxy; tool credentials cannot sign gateway requests', async () => {
  await withChatBedrockRouting(task, async () => {
    expect(process.env.CLAUDE_CODE_USE_BEDROCK).toBe('1');
    expect(process.env.ANTHROPIC_BEDROCK_BASE_URL).toBe('http://127.0.0.1:9090');
    expect(process.env.ANTHROPIC_BASE_URL).toBeUndefined();
    new BedrockSummarizer();
    expect(BedrockRuntimeClient).toHaveBeenCalledWith(expect.objectContaining({ endpoint: 'http://127.0.0.1:9090' }));
  });
  const env = (spawn as jest.Mock).mock.calls[0][2].env;
  expect(env.ADP_MESSAGE_ID).toBe('registered-run');
  expect(env.TENANT_ID).toBe('tenant');
  expect(env.AWS_ROLE_ARN).toBe('pod-role');
  expect(env.AWS_ACCESS_KEY_ID).toBeUndefined();
  expect(process.env.ANTHROPIC_BEDROCK_BASE_URL).toBeUndefined();
  expect(proxy.kill).toHaveBeenCalledWith('SIGTERM');
});

test.each(['direct', 'platform', 'unknown'])('rejects gateway bypass mode %s before inference', async mode => {
  process.env.ADP_BEDROCK_VIA = mode;
  const run = jest.fn();
  await expect(withChatBedrockRouting(task, run)).rejects.toThrow('require ADP_BEDROCK_VIA=gateway');
  expect(run).not.toHaveBeenCalled();
  expect(spawn).not.toHaveBeenCalled();
});

test('missing registration cannot start inference', async () => {
  const run = jest.fn();
  await expect(withChatBedrockRouting({ ...task, message_id: undefined }, run)).rejects.toThrow('registered run');
  expect(run).not.toHaveBeenCalled();
});

test('dead proxy stops inference without direct fallback', async () => {
  proxy.exitCode = 1;
  const run = jest.fn();
  await expect(withChatBedrockRouting(task, run)).rejects.toThrow('failed to start');
  expect(run).not.toHaveBeenCalled();
  expect(proxy.kill).toHaveBeenCalled();
});

test('summarizer cannot call ambient Bedrock without run routing', () => {
  expect(() => new BedrockSummarizer()).toThrow('requires the run routing proxy');
  expect(BedrockRuntimeClient).not.toHaveBeenCalled();
});

test('run failure cleans up routing state', async () => {
  await expect(withChatBedrockRouting(task, async () => { throw new Error('task failed'); })).rejects.toThrow('task failed');
  expect(proxy.kill).toHaveBeenCalled();
  expect(process.env.ANTHROPIC_BEDROCK_BASE_URL).toBeUndefined();
});
