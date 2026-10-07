import { readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { buildSync } from 'esbuild';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { prepareSandboxTurn, startSandboxTurn } from './sandbox-entrypoint';
import { SandboxDataRuntime } from './sandbox-data';

jest.mock('../lib/projectedWorkloadToken', () => ({ readIdentityToken: jest.fn(() => 'sandbox.workload.token') }));

const env: NodeJS.ProcessEnv = {
  ADP_CHAT_DATA_ENABLED: 'true',
  ADP_CHAT_MODEL_POLICY_ENABLED: 'true',
  ADP_CHAT_DATA_URL: 'https://gateway.example.test',
  CONTEXT_STRATEGY: 'gateway',
  MEMORY_STRATEGY: 'gateway',
  ARTIFACT_STRATEGY: 'gateway',
  ADP_WORKLOAD_TOKEN_FILE: '/var/run/adp-model/token',
};
const accepted = {
  ref: `user_${'a'.repeat(64)}`,
  message: { role: 'user', content: 'Review this', ts: '2026-10-04T12:00:00Z', tokens: 3, parts: [] },
};

describe('fixed-template sandbox executable', () => {
  let nextTurn: jest.SpiedFunction<ChatDataClient['nextTurn']>;
  let modelDecision: jest.SpiedFunction<ChatDataClient['modelDecision']>;
  let sessionScope: jest.SpiedFunction<ChatDataClient['sessionScope']>;
  let prepareData: jest.SpiedFunction<SandboxDataRuntime['prepare']>;

  beforeEach(() => {
    jest.spyOn(ChatDataClient.prototype, 'submitTurnResult').mockResolvedValue();
    jest.spyOn(ChatDataClient.prototype, 'renewSession').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a', session_mode: 'ephemeral' });
    jest.spyOn(ChatDataClient.prototype, 'sessionMode').mockResolvedValue('ephemeral');
    nextTurn = jest.spyOn(ChatDataClient.prototype, 'nextTurn').mockResolvedValue(accepted as Awaited<ReturnType<ChatDataClient['nextTurn']>>);
    modelDecision = jest.spyOn(ChatDataClient.prototype, 'modelDecision').mockResolvedValue({
      modelId: 'approved-model', runId: 'run-a', tenantId: 'tenant-a', generation: 1,
    });
    sessionScope = jest.spyOn(ChatDataClient.prototype, 'sessionScope').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a' });
    prepareData = jest.spyOn(SandboxDataRuntime.prototype, 'prepare').mockResolvedValue({
      messages: [], protectedMessageCount: 0, memories: [], attachments: [], userMessage: accepted.message.content,
      meta: { rawMessageCount: 0, summaryCount: 0, estimatedTokens: 0, compactionTriggered: false },
    });
  });
  afterEach(() => jest.restoreAllMocks());

  it('packages the admitted executable in the agent image with the sandbox-only Node command', () => {
    const root = join(__dirname, '..', '..');
    const script = join(root, 'chat-sandbox-entrypoint');
    expect(statSync(script).mode & 0o111).not.toBe(0);
    expect(readFileSync(script, 'utf8')).toContain('exec node /app/dist/chat-sandbox-entrypoint.js');
    expect(readFileSync(script, 'utf8')).toContain('mkdir -p /tmp/workspace');
    expect(readFileSync(join(root, 'Dockerfile.sandbox'), 'utf8')).toContain('COPY --chown=agent:agent agent/chat-sandbox-entrypoint ./chat-sandbox-entrypoint');
    expect(readFileSync(join(root, 'Dockerfile.sandbox'), 'utf8')).toContain('/app/dist/chat-sandbox-entrypoint.js');
    expect(readFileSync(join(root, 'Dockerfile.sandbox'), 'utf8')).not.toMatch(/COPY.*\/app\/dist\s/);
    expect(readFileSync(join(root, 'Dockerfile'), 'utf8')).not.toContain('COPY --chown=agent:agent agent/chat-sandbox-entrypoint');
  });

  it('bundles only scoped gateway data ports, decision verification and projected-token reading', () => {
    const root = join(__dirname, '..', '..');
    const result = buildSync({
      entryPoints: [join(__dirname, 'sandbox-entrypoint.ts')],
      bundle: true, platform: 'node', target: 'node22', format: 'cjs', packages: 'external',
      write: false, metafile: true, absWorkingDir: root,
    });
    expect(Object.keys(result.metafile!.inputs).map(path => relative(root, path)).sort()).toEqual([
      'src/complex-task-chat/artifacts/gateway-artifact-store.ts',
      'src/complex-task-chat/context/eviction/chronological.ts',
      'src/complex-task-chat/context/gateway-context.ts',
      'src/complex-task-chat/context/lcm/assembler.ts',
      'src/complex-task-chat/context/lcm/compactor.ts',
      'src/complex-task-chat/context/lcm/config.ts',
      'src/complex-task-chat/context/lcm/summary-format.ts',
      'src/complex-task-chat/context/store/gateway-history.ts',
      'src/complex-task-chat/context/summarize/gateway-summarizer.ts',
      'src/complex-task-chat/context/tokens/char-estimator.ts',
      'src/complex-task-chat/gateway/chat-data-client.ts',
      'src/complex-task-chat/gateway/chat-model-contract.ts',
      'src/complex-task-chat/gateway/chat-model-stream.ts',
      'src/complex-task-chat/memory/gateway-memory.ts',
      'src/complex-task-chat/memory/tools.ts',
      'src/complex-task-chat/sandbox-data.ts',
      'src/complex-task-chat/sandbox-entrypoint.ts',
      'src/complex-task-chat/sandbox-session-heartbeat.ts',
      'src/complex-task-chat/sandbox-turn.ts',
      'src/control-envelope.ts',
      'src/invocability-probe/canonical-json.ts',
      'src/lib/projectedWorkloadToken.ts',
      'src/lib/url-guard.ts',
      'src/model-policy-body.ts',
    ]);
    expect(result.metafile!.outputs[Object.keys(result.metafile!.outputs)[0]].imports.map(item => item.path))
      .toEqual(expect.arrayContaining(['zod', 'node:fs']));
    const allowedImports = new Set(['zod', 'node:crypto', 'node:fs', 'crypto', 'fs', 'fs/promises', 'path']);
    expect(result.metafile!.outputs[Object.keys(result.metafile!.outputs)[0]].imports.every(item => allowedImports.has(item.path))).toBe(true);
  });

  it('bootstraps only from projected workload identity and the protected gateway turn', async () => {
    const { client, turn, data, context } = await prepareSandboxTurn(env, async () => 'sandbox.workload.token');
    expect(client).toBeInstanceOf(ChatDataClient);
    expect(turn).toEqual(accepted);
    expect(nextTurn).toHaveBeenCalledTimes(1);
    expect(modelDecision).toHaveBeenCalledTimes(1);
    expect(sessionScope).toHaveBeenCalledTimes(1);
    expect(data).toBeInstanceOf(SandboxDataRuntime);
    expect(context.userMessage).toBe(accepted.message.content);
    expect(prepareData).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['scoped data disabled', { ADP_CHAT_DATA_ENABLED: 'false' }],
    ['model policy disabled', { ADP_CHAT_MODEL_POLICY_ENABLED: 'false' }],
    ['direct history store', { CONTEXT_STRATEGY: 'lcm' }],
    ['substituted token mount', { ADP_WORKLOAD_TOKEN_FILE: '/var/run/secrets/kubernetes.io/serviceaccount/token' }],
    ['platform role', { AWS_ROLE_ARN: 'arn:aws:iam::123456789012:role/platform' }],
    ['provider web identity', { AWS_WEB_IDENTITY_TOKEN_FILE: '/var/run/secrets/eks.amazonaws.com/serviceaccount/token' }],
    ['AWS static key', { AWS_ACCESS_KEY_ID: 'forged' }],
    ['node credentials', { AWS_CONTAINER_CREDENTIALS_FULL_URI: 'http://169.254.170.2/creds' }],
    ['vault key', { VAULT_INTERNAL_API_KEY: 'forged' }],
    ['direct endpoint', { ADP_CHAT_DATA_URL: 'http://metadata.example.test' }],
  ])('refuses %s without reading the mailbox or loading a model', async (_name, change) => {
    await expect(prepareSandboxTurn({ ...env, ...change }, async () => 'sandbox.workload.token')).rejects.toThrow();
    expect(nextTurn).not.toHaveBeenCalled();
    expect(modelDecision).not.toHaveBeenCalled();
  });

  it('refuses a model policy decision bound to another run', async () => {
    modelDecision.mockResolvedValue({ modelId: 'approved-model', runId: 'run-other', tenantId: 'tenant-a', generation: 1 });
    await expect(prepareSandboxTurn(env, async () => 'sandbox.workload.token')).rejects.toThrow('model scope unavailable');
  });

  it('waits only for initial admission and reads the owner turn once', async () => {
    sessionScope.mockRejectedValueOnce(new ChatDataError('denied', 404));
    sessionScope.mockRejectedValueOnce(new ChatDataError('denied', 404));
    let elapsed = 0;
    const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
    await expect(prepareSandboxTurn(env, async () => 'sandbox.workload.token', {
      now: () => elapsed, sleep,
    })).resolves.toMatchObject({ turn: accepted });
    expect(sessionScope).toHaveBeenCalledTimes(3);
    expect(sleep).toHaveBeenCalledTimes(2);
    expect(nextTurn).toHaveBeenCalledTimes(1);
    expect(modelDecision).toHaveBeenCalledTimes(1);
  });

  it('expires a missing admission without ever reading model or turn data', async () => {
    sessionScope.mockRejectedValue(new ChatDataError('denied', 404));
    let elapsed = 0;
    const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
    await expect(prepareSandboxTurn(env, async () => 'sandbox.workload.token', {
      now: () => elapsed, sleep,
    })).rejects.toMatchObject({ code: 'denied', status: 404 });
    expect(elapsed).toBe(20_000);
    expect(sessionScope).toHaveBeenCalledTimes(81);
    expect(nextTurn).not.toHaveBeenCalled();
    expect(modelDecision).not.toHaveBeenCalled();
  });

  it.each([
    ['invalid workload', new ChatDataError('denied', 401)],
    ['gateway outage', new ChatDataError('unavailable', 503)],
  ])('does not retry %s before admission', async (_name, failure) => {
    sessionScope.mockRejectedValue(failure);
    const sleep = jest.fn();
    await expect(prepareSandboxTurn(env, async () => 'sandbox.workload.token', { sleep })).rejects.toBe(failure);
    expect(sessionScope).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
    expect(nextTurn).not.toHaveBeenCalled();
  });

  it('never retries a refused owner turn or model decision after admission', async () => {
    nextTurn.mockRejectedValueOnce(new ChatDataError('denied', 404));
    const sleep = jest.fn();
    await expect(prepareSandboxTurn(env, async () => 'sandbox.workload.token', { sleep })).rejects.toMatchObject({ code: 'denied' });
    expect(nextTurn).toHaveBeenCalledTimes(1);
    expect(modelDecision).not.toHaveBeenCalled();
    expect(sleep).not.toHaveBeenCalled();
  });

  it('exits with an error instead of invoking a direct model when delegated transport refuses', async () => {
    const invoke = jest.spyOn(ChatDataClient.prototype, 'invokeModel').mockRejectedValue(new ChatDataError('denied', 403));
    const record = jest.spyOn(SandboxDataRuntime.prototype, 'record');
    await expect(startSandboxTurn(env)).rejects.toMatchObject({ code: 'denied' });
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(record).not.toHaveBeenCalled();
    expect(nextTurn).toHaveBeenCalledTimes(1);
    expect(modelDecision).toHaveBeenCalledTimes(1);
  });

  it('executes one delegated turn and waits for its scoped history write before exiting', async () => {
    const invoke = jest.spyOn(ChatDataClient.prototype, 'invokeModel').mockResolvedValue({
      content: [{ type: 'text', text: 'Owner reply' }], stopReason: 'end_turn', modelId: 'approved-model',
      usage: { input_tokens: 3, output_tokens: 2, estimated_usd: '0.01' },
    });
    const record = jest.spyOn(SandboxDataRuntime.prototype, 'record').mockResolvedValue();
    await expect(startSandboxTurn(env)).resolves.toBeUndefined();
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke.mock.calls[0].slice(2)).toEqual([undefined, true]);
    expect(record).toHaveBeenCalledWith('Owner reply');
  });

  it('reports only a failure outcome and preserves the original error when reporting also fails', async () => {
    const failure = new ChatDataError('incomplete');
    jest.spyOn(ChatDataClient.prototype, 'invokeModel').mockRejectedValue(failure);
    const result = jest.spyOn(ChatDataClient.prototype, 'submitTurnResult').mockRejectedValue(new Error('private reporting detail'));
    await expect(startSandboxTurn(env)).rejects.toBe(failure);
    expect(result).toHaveBeenCalledWith({ outcome: 'failed' });
    expect(result).toHaveBeenCalledTimes(1);
  });

  it('does not bootstrap an already cancelled turn', async () => {
    await expect(startSandboxTurn(env, AbortSignal.abort())).rejects.toThrow();
    expect(sessionScope).not.toHaveBeenCalled();
    expect(nextTurn).not.toHaveBeenCalled();
  });

  it('does not start a turn with unavailable scoped context', async () => {
    prepareData.mockRejectedValueOnce(new ChatDataError('incomplete'));
    await expect(prepareSandboxTurn(env, async () => 'sandbox.workload.token')).rejects.toMatchObject({ code: 'incomplete' });
    expect(prepareData).toHaveBeenCalledTimes(1);
  });
});
