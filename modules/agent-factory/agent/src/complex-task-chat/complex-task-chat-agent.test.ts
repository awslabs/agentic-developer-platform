/**
 * Wiring tests for the live chat worker's store composition (#6932, WIRE-t1).
 *
 * Exercises `buildChatStores`, the exact function the message loop calls, so the
 * flag-off path proves today's direct stores are untouched and the flag-on path
 * proves every store is gateway-backed through ONE workload-bound client.
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

import { buildChatStores } from './complex-task-chat-agent';
import { ChatDataClient } from './gateway/chat-data-client';
import { NoopContextManager } from './context/noop-context';
import { LcmContext } from './context/lcm/lcm-context';
import { GatewayContextManager } from './context/gateway-context';
import { NullMemoryProvider } from './memory/null-memory';
import { DynamoMemoryProvider } from './memory/dynamo-memory';
import { GatewayMemoryProvider } from './memory/gateway-memory';
import { NoopArtifactStore } from './artifacts/noop-artifact-store';
import { S3ArtifactStore } from './artifacts/s3-artifact-store';
import { GatewayArtifactStore } from './artifacts/gateway-artifact-store';
import { DynamoDraftStore, NoopDraftStore } from './draft/dynamo-draft-store';
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

describe('buildChatStores with scoped chat data off', () => {
  it.each([undefined, 'false', '1', 'TRUE'])('keeps the current direct stores when ADP_CHAT_DATA_ENABLED=%s', async flag => {
    const stores = await buildChatStores({ ...currentEnv, ADP_CHAT_DATA_ENABLED: flag }, task);
    expect(stores.context).toBeInstanceOf(LcmContext);
    expect(stores.memory).toBeInstanceOf(DynamoMemoryProvider);
    expect(stores.artifacts).toBeInstanceOf(S3ArtifactStore);
    expect(stores.draftStore).toBeInstanceOf(DynamoDraftStore);
    expect(ChatDataClient).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('keeps the default no-op stores when nothing is configured', async () => {
    const stores = await buildChatStores({}, task);
    expect(stores.context).toBeInstanceOf(NoopContextManager);
    expect(stores.memory).toBeInstanceOf(NullMemoryProvider);
    expect(stores.artifacts).toBeInstanceOf(NoopArtifactStore);
    expect(stores.draftStore).toBeInstanceOf(NoopDraftStore);
    expect(ChatDataClient).not.toHaveBeenCalled();
  });

  it('ignores gateway URL and token settings while the switch is off', async () => {
    const stores = await buildChatStores({ ...currentEnv, ADP_CHAT_DATA_URL: 'https://gateway.example.test', ADP_WORKLOAD_TOKEN_FILE: '/nonexistent' }, task);
    expect(stores.context).toBeInstanceOf(LcmContext);
    expect(ChatDataClient).not.toHaveBeenCalled();
  });
});

describe('buildChatStores with scoped chat data on', () => {
  it('builds all four gateway-backed stores through one client bound to the task session', async () => {
    const stores = await buildChatStores(scopedEnv, task);
    expect(stores.context).toBeInstanceOf(GatewayContextManager);
    expect(stores.memory).toBeInstanceOf(GatewayMemoryProvider);
    expect(stores.artifacts).toBeInstanceOf(GatewayArtifactStore);
    expect(stores.draftStore).toBeInstanceOf(GatewayDraftStore);

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
