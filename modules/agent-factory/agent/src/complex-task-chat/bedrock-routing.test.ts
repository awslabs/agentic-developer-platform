import { spawn } from 'node:child_process';
import { withChatBedrockRouting } from './bedrock-routing';
import { BedrockSummarizer } from './context/summarize/bedrock-summarizer';
import { BedrockRuntimeClient } from '@aws-sdk/client-bedrock-runtime';

jest.mock('node:child_process', () => ({ spawn: jest.fn() }));
jest.mock('@aws-sdk/client-bedrock-runtime', () => ({ BedrockRuntimeClient: jest.fn(), InvokeModelCommand: jest.fn() }));

const task = { task_id: 'task', session_id: 'session', message: 'hello', user_id: 'user', tenant_id: 'tenant', message_id: 'registered-run' };
let savedEnv: NodeJS.ProcessEnv;

beforeEach(() => {
  savedEnv = process.env;
  process.env = {};
  jest.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true } as Response);
});

afterEach(() => {
  process.env = savedEnv;
  jest.restoreAllMocks();
  jest.clearAllMocks();
});

test.each([
  {},
  { ADP_CHAT_DATA_ENABLED: 'true', ADP_CHAT_MODEL_POLICY_ENABLED: 'true' },
  { ADP_BEDROCK_VIA: 'gateway', SIGV4_PROXY_TARGET: 'https://gateway.example.test' },
  { AWS_ROLE_ARN: 'arn:aws:iam::000000000000:role/worker', AWS_WEB_IDENTITY_TOKEN_FILE: '/var/run/secrets/token' },
  { AWS_ACCESS_KEY_ID: 'synthetic', AWS_SECRET_ACCESS_KEY: 'synthetic' },
])('never invokes a legacy model runner or proxy with environment %j', async environment => {
  Object.assign(process.env, environment);
  const run = jest.fn(async () => 'model output');
  await expect(withChatBedrockRouting({ ...task, agent_type: 'developer' }, run, '{"message_id":"registered-run"}'))
    .rejects.toThrow('Credentialed chat model routing retired');
  expect(run).not.toHaveBeenCalled();
  expect(spawn).not.toHaveBeenCalled();
  expect(globalThis.fetch).not.toHaveBeenCalled();
  expect(process.env.ANTHROPIC_BEDROCK_BASE_URL).toBeUndefined();
});

test('summarizer still refuses direct Bedrock without run routing', () => {
  expect(() => new BedrockSummarizer()).toThrow('requires the run routing proxy');
  expect(BedrockRuntimeClient).not.toHaveBeenCalled();
});
