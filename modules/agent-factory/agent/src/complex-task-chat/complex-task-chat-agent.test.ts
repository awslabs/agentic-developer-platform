/**
 * Wiring tests for the live chat worker's store composition (#6932, WIRE-t1).
 *
 * Exercises the retained store builder and the worker entrypoint. The worker
 * refuses to start while a delegated sandbox and supervisor are unavailable.
 * No network: AWS SDK clients and the model runner are mocked, the token file
 * is a temp file, and fetch is spied to prove nothing is called at construction.
 */
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

jest.mock('./run-query', () => ({ runQuery: jest.fn() }));
jest.mock('@aws-sdk/client-bedrock-runtime', () => ({ BedrockRuntimeClient: jest.fn(() => ({ send: jest.fn() })), InvokeModelCommand: jest.fn() }));
jest.mock('@aws-sdk/client-dynamodb', () => ({ DynamoDBClient: jest.fn(() => ({})) }));
jest.mock('@aws-sdk/lib-dynamodb', () => {
  const actual = jest.requireActual('@aws-sdk/lib-dynamodb');
  return { ...actual, DynamoDBDocumentClient: { from: jest.fn(() => ({ send: jest.fn() })) } };
});
jest.mock('@aws-sdk/client-s3', () => ({ S3Client: jest.fn(() => ({ send: jest.fn() })), PutObjectCommand: jest.fn(), GetObjectCommand: jest.fn() }));
jest.mock('@aws-sdk/s3-request-presigner', () => ({ getSignedUrl: jest.fn() }));
jest.mock('./gateway/chat-data-client', () => {
  const actual = jest.requireActual('./gateway/chat-data-client');
  return { ...actual, ChatDataClient: jest.fn((config: unknown) => new actual.ChatDataClient(config)) };
});

import { buildChatStores, main } from './complex-task-chat-agent';
import { ChatDataClient } from './gateway/chat-data-client';
import { GatewayContextManager } from './context/gateway-context';
import { GatewayMemoryProvider } from './memory/gateway-memory';
import { GatewayArtifactStore } from './artifacts/gateway-artifact-store';
import { GatewayDraftStore } from './draft/gateway-draft-store';

const task = { task_id: 'task-1', session_id: 'session-a', message: 'hello', user_id: 'user-a', tenant_id: 'tenant-a' };
const currentEnv = {
  CONTEXT_STRATEGY: 'lcm', CONTEXT_TABLE: 'chat-context',
  MEMORY_STRATEGY: 'dynamo', MEMORY_TABLE: 'chat-memory',
  ARTIFACT_STRATEGY: 's3', ARTIFACTS_BUCKET: 'chat-artifacts', ARTIFACTS_TABLE: 'chat-artifacts-table',
  ADP_WORKLOAD_TOKEN_FILE: '/var/run/adp-model/token',
};

let dir: string;
let tokenFile: string;
let scopedEnv: Record<string, string | undefined>;
let fetchMock: jest.SpiedFunction<typeof fetch>;
let savedEnv: NodeJS.ProcessEnv;

beforeEach(() => {
  savedEnv = process.env;
  // The run routing proxy is up before stores are built (withChatBedrockRouting).
  process.env = { ...savedEnv, ANTHROPIC_BEDROCK_BASE_URL: 'http://127.0.0.1:9090' };
  dir = mkdtempSync(join(tmpdir(), 'adp-6932-'));
  tokenFile = join(dir, 'token');
  writeFileSync(tokenFile, 'projected.workload.token\n');
  scopedEnv = {
    ...currentEnv,
    ADP_CHAT_DATA_ENABLED: 'true',
    CONTEXT_STRATEGY: 'gateway', MEMORY_STRATEGY: 'gateway', ARTIFACT_STRATEGY: 'gateway',
    ADP_CHAT_DATA_URL: 'https://gateway.example.test',
    ADP_WORKLOAD_TOKEN_FILE: tokenFile,
  };
  fetchMock = jest.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('no network in wiring tests'));
  jest.mocked(ChatDataClient).mockClear();
});

afterEach(() => {
  process.env = savedEnv;
  rmSync(dir, { recursive: true, force: true });
  jest.restoreAllMocks();
});

describe('retired credentialed chat worker', () => {
  it.each([undefined, 'false', '1', 'TRUE'])('rejects direct stores when ADP_CHAT_DATA_ENABLED=%s', async flag => {
    await expect(buildChatStores({ ...currentEnv, ADP_CHAT_DATA_ENABLED: flag }, task))
      .rejects.toThrow('cannot build direct owner stores');
    expect(ChatDataClient).not.toHaveBeenCalled();
    expect(jest.requireMock('@aws-sdk/client-dynamodb').DynamoDBClient).not.toHaveBeenCalled();
    expect(jest.requireMock('@aws-sdk/client-s3').S3Client).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses to consume registered turns even with a scoped-data flag', async () => {
    for (const flag of [undefined, 'false', 'true']) {
      process.env.ADP_CHAT_DATA_ENABLED = flag;
      await expect(main()).rejects.toThrow('Credentialed chat worker retired');
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('buildChatStores with scoped chat data on', () => {
  it('builds all four gateway-backed stores through one client bound to the task session', async () => {
    const stores = await buildChatStores(scopedEnv, task);
    expect(stores.context).toBeInstanceOf(GatewayContextManager);
    expect(stores.memory).toBeInstanceOf(GatewayMemoryProvider);
    expect(stores.artifacts).toBeInstanceOf(GatewayArtifactStore);
    expect(stores.draftStore).toBeInstanceOf(GatewayDraftStore);
    expect(stores.activityTools?.map(tool => tool.name)).toEqual(['get_my_agent_work']);
    expect(stores.diagnostics).toBe(jest.mocked(ChatDataClient).mock.results[0].value);

    expect(ChatDataClient).toHaveBeenCalledTimes(1);
    expect(ChatDataClient).toHaveBeenCalledWith({ baseUrl: 'https://gateway.example.test', workloadToken: expect.any(Function) });
    // Construction alone performs no bootstrap or model call.
    expect(fetchMock).not.toHaveBeenCalled();

    // The session-scoped ports refuse any other session before touching the network.
    await expect(stores.draftStore.get('session-b')).rejects.toMatchObject({ code: 'scope_mismatch' });
    await expect(stores.artifacts.listBySession('session-b')).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('reads the projected token file on each exchange so rotation is honoured', async () => {
    await buildChatStores(scopedEnv, task);
    const { workloadToken } = jest.mocked(ChatDataClient).mock.calls[0][0];
    await expect(workloadToken()).resolves.toBe('projected.workload.token');
    writeFileSync(tokenFile, 'rotated.workload.token');
    await expect(workloadToken()).resolves.toBe('rotated.workload.token');
  });

  it('uses an injected token source and summarizer when supplied', async () => {
    const workloadToken = jest.fn(async () => 'injected.token');
    const summarizer = { summarize: jest.fn(async () => 'summary') };
    const stores = await buildChatStores({ ...scopedEnv, ADP_WORKLOAD_TOKEN_FILE: undefined }, task, { workloadToken, summarizer, workspaceRoot: '/tmp/other-workspace' });
    expect(stores.context).toBeInstanceOf(GatewayContextManager);
    expect(workloadToken).toHaveBeenCalledTimes(1);
    expect(ChatDataClient).toHaveBeenCalledWith({ baseUrl: 'https://gateway.example.test', workloadToken });
    expect(summarizer.summarize).not.toHaveBeenCalled();
  });
});

describe('buildChatStores fails closed when scoped chat data is on but misconfigured', () => {
  afterEach(() => {
    expect(ChatDataClient).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(['CONTEXT_STRATEGY', 'MEMORY_STRATEGY', 'ARTIFACT_STRATEGY'] as const)('rejects a direct %s instead of falling back', async key => {
    const direct = { CONTEXT_STRATEGY: 'lcm', MEMORY_STRATEGY: 'dynamo', ARTIFACT_STRATEGY: 's3' }[key];
    await expect(buildChatStores({ ...scopedEnv, [key]: direct }, task)).rejects.toThrow(`${key}=gateway`);
  });

  it('rejects an unset strategy', async () => {
    await expect(buildChatStores({ ...scopedEnv, MEMORY_STRATEGY: undefined }, task)).rejects.toThrow('MEMORY_STRATEGY=gateway');
  });

  it('rejects a missing token file path', async () => {
    await expect(buildChatStores({ ...scopedEnv, ADP_WORKLOAD_TOKEN_FILE: undefined }, task)).rejects.toThrow('ADP_WORKLOAD_TOKEN_FILE');
  });

  it('rejects an absent or empty token file before constructing anything', async () => {
    await expect(buildChatStores({ ...scopedEnv, ADP_WORKLOAD_TOKEN_FILE: join(dir, 'missing') }, task)).rejects.toThrow('readable workload token');
    writeFileSync(tokenFile, '');
    await expect(buildChatStores(scopedEnv, task)).rejects.toThrow('readable workload token');
  });

  it('rejects a failing injected token source', async () => {
    const workloadToken = jest.fn(async () => { throw new Error('sensitive path'); });
    const failure = buildChatStores(scopedEnv, task, { workloadToken });
    await expect(failure).rejects.toThrow('readable workload token');
    await expect(failure).rejects.not.toThrow('sensitive');
  });

  it.each([undefined, ''])('rejects a missing gateway origin (%j)', async url => {
    await expect(buildChatStores({ ...scopedEnv, ADP_CHAT_DATA_URL: url }, task)).rejects.toThrow('ADP_CHAT_DATA_URL');
  });

  it.each(['', 'bad session', 'a'.repeat(129), 'bad/session'])('rejects an unbindable session id %j', async session_id => {
    await expect(buildChatStores(scopedEnv, { session_id })).rejects.toThrow('session_id');
  });
});

describe('buildChatStores rejects an unsafe gateway origin', () => {
  it.each([
    'http://127.0.0.1:9090', 'http://gateway.example.test', 'https://gateway.example.test/agent',
    'https://gateway.example.test?x=1', 'https://user:secret@gateway.example.test', 'gateway.example.test',
  ])('refuses %s', async url => {
    await expect(buildChatStores({ ...scopedEnv, ADP_CHAT_DATA_URL: url }, task)).rejects.toThrow('https origin');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
