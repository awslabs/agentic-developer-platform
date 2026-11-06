import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join, relative } from 'node:path';
import { buildSync } from 'esbuild';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { prepareSandboxTurn, startSandboxTurn } from './sandbox-entrypoint';
import { SandboxDataRuntime } from './sandbox-data';
import * as qualification from './sandbox-qualification';
import * as crewModule from './crew-kirocrew';
import * as modelAdapter from './crew-model-adapter';
import * as custodyModule from './crew-dashboard-custody';
import * as crewBridge from './crew-kirocrew-bridge';
import * as stopModule from './crew-stop-result';

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
    jest.spyOn(stopModule, 'recordCrewStop').mockResolvedValue({ status: 'checkpoint_unverified',
      last_verified_checkpoint_id: null, last_verified_checkpoint_id_known: false, consolidation: 'completed' });
    jest.spyOn(ChatDataClient.prototype, 'renewSession').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a', session_mode: 'ephemeral' });
    jest.spyOn(ChatDataClient.prototype, 'sessionMode').mockResolvedValue('ephemeral');
    nextTurn = jest.spyOn(ChatDataClient.prototype, 'nextTurn').mockResolvedValue(accepted as Awaited<ReturnType<ChatDataClient['nextTurn']>>);
    modelDecision = jest.spyOn(ChatDataClient.prototype, 'modelDecision').mockResolvedValue({
      modelId: 'approved-model', runId: 'run-a', tenantId: 'tenant-a', userId: 'owner', generation: 1,
      posture: 'enforcing', postureVerified: true,
    });
    sessionScope = jest.spyOn(ChatDataClient.prototype, 'sessionScope').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a' });
    prepareData = jest.spyOn(SandboxDataRuntime.prototype, 'prepare').mockResolvedValue({
      messages: [], protectedMessageCount: 0, memories: [], attachments: [], userMessage: accepted.message.content,
      meta: { rawMessageCount: 0, summaryCount: 0, estimatedTokens: 0, compactionTriggered: false },
    });
  });
  afterEach(() => jest.restoreAllMocks());

  it('runs only the fixed executor when authenticated bootstrap selects qualification', async () => {
    jest.spyOn(ChatDataClient.prototype, 'qualificationRequired').mockReturnValue(true);
    const execute = jest.spyOn(qualification, 'executeSandboxQualification').mockResolvedValue();
    await startSandboxTurn(env);
    expect(nextTurn).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(modelDecision).not.toHaveBeenCalled();
    expect(prepareData).not.toHaveBeenCalled();
  });

  it('cannot fall back to model execution after qualification fails', async () => {
    jest.spyOn(ChatDataClient.prototype, 'qualificationRequired').mockReturnValue(true);
    jest.spyOn(qualification, 'executeSandboxQualification').mockRejectedValue(new ChatDataError('unavailable'));
    await expect(startSandboxTurn(env)).rejects.toMatchObject({ code: 'unavailable' });
    expect(ChatDataClient.prototype.submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed' });
    expect(modelDecision).not.toHaveBeenCalled();
    expect(prepareData).not.toHaveBeenCalled();
  });

  it.each([
    ['bogus', 'Unknown ADP_EXECUTOR: bogus'],
    ['kirocrew', 'Chat sandbox requires credential-free scoped identity and gateway ports'],
  ])('exits nonzero and logs a clear startup error for executor %s', (executor, message) => {
    const root = join(__dirname, '..', '..');
    const directory = mkdtempSync(join(root, '.sandbox-selector-'));
    try {
      const bundle = join(directory, 'entrypoint.cjs');
      buildSync({ entryPoints: [join(__dirname, 'sandbox-entrypoint.ts')], bundle: true,
        platform: 'node', target: 'node22', format: 'cjs', packages: 'external', outfile: bundle });
      const result = spawnSync(process.execPath, [bundle], {
        encoding: 'utf8', timeout: 5000, env: { ADP_EXECUTOR: executor },
      });
      expect(result.status).toBe(1);
      expect(result.stderr).toContain(message);
      expect(result.stdout).toBe('');
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
    expect(sessionScope).not.toHaveBeenCalled();
    expect(nextTurn).not.toHaveBeenCalled();
  });

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
      'src/complex-task-chat/crew-dashboard-custody.ts',
      'src/complex-task-chat/crew-kirocrew-bridge.ts',
      'src/complex-task-chat/crew-kirocrew.ts',
      'src/complex-task-chat/crew-model-adapter.ts',
      'src/complex-task-chat/crew-stop-result.ts',
      'src/complex-task-chat/crew-tools.ts',
      'src/complex-task-chat/crew-turn.ts',
      'src/complex-task-chat/draft/gateway-draft-store.ts',
      'src/complex-task-chat/draft/tools.ts',
      'src/complex-task-chat/gateway/answer-evidence.ts',
      'src/complex-task-chat/gateway/chat-data-client.ts',
      'src/complex-task-chat/gateway/chat-model-contract.ts',
      'src/complex-task-chat/gateway/chat-model-stream.ts',
      'src/complex-task-chat/memory/gateway-memory.ts',
      'src/complex-task-chat/memory/tools.ts',
      'src/complex-task-chat/platform/tools.ts',
      'src/complex-task-chat/qualification-contract.ts',
      'src/complex-task-chat/qualification-data.ts',
      'src/complex-task-chat/qualification-diagnostics.ts',
      'src/complex-task-chat/sandbox-data.ts',
      'src/complex-task-chat/sandbox-entrypoint.ts',
      'src/complex-task-chat/sandbox-gateway-binding.ts',
      'src/complex-task-chat/sandbox-gateway-network.ts',
      'src/complex-task-chat/sandbox-model-decision.ts',
      'src/complex-task-chat/sandbox-model-refusal.ts',
      'src/complex-task-chat/sandbox-qualification.ts',
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
    const allowedImports = new Set(['zod', '@anthropic-ai/claude-agent-sdk', 'node:crypto', 'node:fs', 'crypto', 'fs', 'fs/promises', 'path',
      'node:timers/promises', 'node:child_process', 'node:util', 'node:fs/promises', 'node:net', 'node:http', 'node:path']);
    expect(result.metafile!.outputs[Object.keys(result.metafile!.outputs)[0]].imports.every(item => allowedImports.has(item.path))).toBe(true);
    expect(Object.keys(result.metafile!.inputs).every(path => !path.includes('node_modules/@aws-sdk/') &&
      !path.includes('node_modules/@smithy/'))).toBe(true);
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

  it('refuses a persistent model endpoint outside the trusted gateway before bootstrap', async () => {
    await expect(prepareSandboxTurn({ ...env, ADP_RUN_ID: '00000000-0000-4000-8000-000000000001',
      ADP_CHAT_DATA_URL: 'https://bedrock-runtime.us-east-1.amazonaws.com' },
    async () => 'sandbox.workload.token')).rejects.toThrow('credential-free scoped identity and gateway ports');
    expect(sessionScope).not.toHaveBeenCalled();
    expect(nextTurn).not.toHaveBeenCalled();
  });

  it('refuses a model policy decision bound to another run', async () => {
    modelDecision.mockResolvedValue({ modelId: 'approved-model', runId: 'run-other', tenantId: 'tenant-a', userId: 'owner', generation: 1,
      posture: 'enforcing', postureVerified: true });
    await expect(prepareSandboxTurn(env, async () => 'sandbox.workload.token')).rejects.toThrow('model scope unavailable');
  });

  it('does not load the SDK for an ephemeral session requesting claude-code', async () => {
    await expect(startSandboxTurn({ ...env, ADP_EXECUTOR: 'claude-code' })).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(prepareData).toHaveBeenCalledTimes(1);
    expect(ChatDataClient.prototype.submitTurnResult).not.toHaveBeenCalled();
  });

  it.each(['dashboard', 'shared'] as const)('routes an admitted %s Crew turn to the bridge without a direct model', async mode => {
    jest.spyOn(ChatDataClient.prototype, 'sessionMode').mockResolvedValue('persistent');
    jest.spyOn(ChatDataClient.prototype, 'renewSession').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' });
    const stopOrder: string[] = [];
    const pendingMessages = Array.from({ length: 10 }, (_, index) => `pending-${index}`);
    jest.spyOn(stopModule, 'recordCrewStop').mockImplementation(async (_, consolidation) => {
      stopOrder.push('unverified');
      return { status: 'checkpoint_unverified', last_verified_checkpoint_id: null,
        last_verified_checkpoint_id_known: false, consolidation };
    });
    const close = jest.fn(async () => { stopOrder.push('adapter'); });
    jest.spyOn(modelAdapter, 'startCrewModelAdapter').mockResolvedValue({
      baseUrl: 'http://127.0.0.1:5477', secret: 'synthetic', gatewayError: undefined, close, bindTurn: jest.fn(), suspend: jest.fn(),
    });
    const custodyClose = jest.fn(async () => undefined);
    jest.spyOn(custodyModule, 'createCrewDashboardCustody').mockReturnValue({ listen: jest.fn(async () => undefined),
      close: custodyClose, address: jest.fn() });
    const kill = jest.fn(() => { stopOrder.push('gateway'); });
    const startCrew = jest.spyOn(crewModule, 'startKiroCrew').mockResolvedValue({ kill } as unknown as Awaited<ReturnType<typeof crewModule.startKiroCrew>>);
    const consolidate = jest.spyOn(crewModule, 'consolidateKiroCrew').mockImplementation(async () => {
      pendingMessages.splice(0);
      stopOrder.push('consolidation');
    });
    const invoke = jest.spyOn(ChatDataClient.prototype, 'invokeModel');
    const cancellation = new AbortController();
    nextTurn.mockResolvedValue({ ...accepted, session_sequence: 1 } as Awaited<ReturnType<ChatDataClient['nextTurn']>>);
    const execute = jest.spyOn(crewBridge, 'executeKiroCrewTurn').mockImplementation(async () => { cancellation.abort(); });
    jest.spyOn(console, 'error').mockImplementation(() => undefined);

    await expect(startSandboxTurn({ ...env, ADP_CHAT_DATA_URL: 'https://chat-sandbox-gateway.adp-gateway.svc:8443',
      ADP_RUN_ID: '00000000-0000-4000-8000-000000000001', ADP_EXECUTOR: 'kirocrew', KIROCREW_HOME: '/tmp/workspace',
      ADP_CREW_POD_IP: '10.1.2.3', ADP_CREW_CUSTODY_PORT: '5477',
      ...(mode === 'dashboard' ? { ADP_CREW_DASHBOARD_ORIGIN: 'https://crew.example.test' } : { ADP_CREW_SCOPE: 'user' }),
      ADP_CREW_SESSION_MAP: '/tmp/workspace/adp-crew-session-map' }, cancellation.signal)).resolves.toBeUndefined();
    expect(startCrew).toHaveBeenCalledWith('/tmp/workspace', 'approved-model', expect.anything(),
      expect.objectContaining({ podIP: '10.1.2.3' }));
    expect(startCrew.mock.calls[0][3]?.dashboardOrigin).toBe(mode === 'dashboard' ? 'https://crew.example.test' : undefined);
    expect(execute).toHaveBeenCalledWith(expect.objectContaining({ turn: expect.objectContaining(accepted) }), '/tmp/workspace',
      '/tmp/workspace/adp-crew-session-map', undefined, expect.anything());
    expect(ChatDataClient.prototype.submitTurnResult).not.toHaveBeenCalled();
    expect(invoke).not.toHaveBeenCalled();
    expect(kill).toHaveBeenCalledWith('SIGTERM');
    expect(consolidate).toHaveBeenCalledWith('/tmp/workspace', 'approved-model', expect.objectContaining({ secret: 'synthetic' }),
      undefined, expect.any(AbortSignal));
    expect(pendingMessages).toHaveLength(0);
    expect(startCrew.mock.calls[0][3]).toHaveProperty('toolsRunId', '00000000-0000-4000-8000-000000000001');
    expect(stopOrder).toEqual(['consolidation', 'adapter', 'unverified', 'gateway', 'adapter']);
    expect(close).toHaveBeenCalledTimes(2);
    expect(custodyClose).toHaveBeenCalledTimes(mode === 'dashboard' ? 1 : 0);
  });

  it('delivers a withheld steer as a separate Crew turn after the previous receipt', async () => {
    jest.spyOn(ChatDataClient.prototype, 'sessionMode').mockResolvedValue('persistent');
    jest.spyOn(ChatDataClient.prototype, 'renewSession').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' });
    jest.spyOn(modelAdapter, 'startCrewModelAdapter').mockResolvedValue({
      baseUrl: 'http://127.0.0.1:5477', secret: 'synthetic', gatewayError: undefined, close: jest.fn(async () => undefined),
      bindTurn: jest.fn(), suspend: jest.fn(),
    });
    jest.spyOn(crewModule, 'startKiroCrew').mockResolvedValue({ kill: jest.fn() } as unknown as
      Awaited<ReturnType<typeof crewModule.startKiroCrew>>);
    jest.spyOn(crewModule, 'consolidateKiroCrew').mockResolvedValue();
    const cancellation = new AbortController();
    const steer = { ...accepted, ref: `user_${'b'.repeat(64)}`, message: { ...accepted.message, content: 'Steer after this reply' } };
    nextTurn.mockResolvedValueOnce({ ...accepted, session_sequence: 1 } as Awaited<ReturnType<ChatDataClient['nextTurn']>>)
      .mockResolvedValueOnce({ ...steer, session_sequence: 2 } as Awaited<ReturnType<ChatDataClient['nextTurn']>>);
    const preparedContext = { messages: [], protectedMessageCount: 0, memories: [], attachments: [],
      meta: { rawMessageCount: 0, summaryCount: 0, estimatedTokens: 0, compactionTriggered: false } };
    prepareData.mockResolvedValueOnce({ ...preparedContext, userMessage: accepted.message.content })
      .mockResolvedValueOnce({ ...preparedContext, userMessage: steer.message.content });
    const events: string[] = [];
    const receipt = jest.spyOn(ChatDataClient.prototype, 'submitTurnResult').mockImplementation(async result => {
      events.push(`receipt:${result.outcome === 'completed' ? result.message_id : 'failed'}`);
    });
    const nextMailboxTurn = jest.spyOn(ChatDataClient.prototype, 'nextMailboxTurn').mockImplementation(async () => {
      events.push('mailbox');
      return { sequence: 2, turn_id: 'run-b', message: steer.message.content };
    });
    jest.spyOn(ChatDataClient.prototype, 'admitMailboxTurn').mockImplementation(async () => { events.push('admit'); });
    let active = false;
    const execute = jest.spyOn(crewBridge, 'executeKiroCrewTurn').mockImplementation(async prepared => {
      expect(active).toBe(false);
      active = true;
      events.push(`send:${prepared.context.userMessage}`);
      await prepared.client.submitTurnResult({ outcome: 'completed', message_id: prepared.turn.ref, reply_granularity: 'row' });
      active = false;
      if (prepared.turn.ref === steer.ref) cancellation.abort();
    });

    await expect(startSandboxTurn({ ...env, ADP_CHAT_DATA_URL: 'https://chat-sandbox-gateway.adp-gateway.svc:8443',
      ADP_RUN_ID: '00000000-0000-4000-8000-000000000001', ADP_EXECUTOR: 'kirocrew', KIROCREW_HOME: '/tmp/workspace',
      ADP_CREW_POD_IP: '10.1.2.3', ADP_CREW_CUSTODY_PORT: '5477',
      ADP_CREW_SESSION_MAP: '/tmp/workspace/adp-crew-session-map' }, cancellation.signal)).resolves.toBeUndefined();
    expect(events).toEqual([`send:${accepted.message.content}`, `receipt:${accepted.ref}`, 'mailbox', 'admit',
      `send:${steer.message.content}`, `receipt:${steer.ref}`]);
    expect(nextMailboxTurn).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledTimes(2);
    expect(receipt).toHaveBeenCalledTimes(2);
  });

  it('records an unverified stop when consolidation times out without a false Synced result', async () => {
    jest.spyOn(ChatDataClient.prototype, 'sessionMode').mockResolvedValue('persistent');
    jest.spyOn(ChatDataClient.prototype, 'renewSession').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' });
    const close = jest.fn(async () => undefined);
    jest.spyOn(modelAdapter, 'startCrewModelAdapter').mockResolvedValue({
      baseUrl: 'http://127.0.0.1:5477', secret: 'synthetic', gatewayError: undefined, close, bindTurn: jest.fn(), suspend: jest.fn(),
    });
    const kill = jest.fn();
    jest.spyOn(crewModule, 'startKiroCrew').mockResolvedValue({ kill } as unknown as Awaited<ReturnType<typeof crewModule.startKiroCrew>>);
    const consolidate = jest.spyOn(crewModule, 'consolidateKiroCrew').mockRejectedValue(new Error('Kiro Crew consolidation timed out'));
    const cancellation = new AbortController();
    nextTurn.mockResolvedValue({ ...accepted, session_sequence: 1 } as Awaited<ReturnType<ChatDataClient['nextTurn']>>);
    jest.spyOn(crewBridge, 'executeKiroCrewTurn').mockImplementation(async () => { cancellation.abort(); });

    await expect(startSandboxTurn({ ...env, ADP_CHAT_DATA_URL: 'https://chat-sandbox-gateway.adp-gateway.svc:8443',
      ADP_RUN_ID: '00000000-0000-4000-8000-000000000001', ADP_EXECUTOR: 'kirocrew', KIROCREW_HOME: '/tmp/workspace',
      ADP_CREW_POD_IP: '10.1.2.3', ADP_CREW_CUSTODY_PORT: '5477',
      ADP_CREW_SESSION_MAP: '/tmp/workspace/adp-crew-session-map' }, cancellation.signal))
      .resolves.toBeUndefined();
    expect(consolidate).toHaveBeenCalledTimes(1);
    expect(stopModule.recordCrewStop).toHaveBeenCalledWith('/tmp/workspace', 'failed');
    expect(kill).toHaveBeenCalledWith('SIGTERM');
    expect(close).toHaveBeenCalledTimes(2);
    expect(ChatDataClient.prototype.submitTurnResult).not.toHaveBeenCalledWith(expect.objectContaining({ outcome: 'completed' }));
  });

  it('records executor_down and exits instead of falling back to Claude Code when Crew dies', async () => {
    jest.spyOn(ChatDataClient.prototype, 'sessionMode').mockResolvedValue('persistent');
    jest.spyOn(ChatDataClient.prototype, 'renewSession').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' });
    jest.spyOn(modelAdapter, 'startCrewModelAdapter').mockResolvedValue({
      baseUrl: 'http://127.0.0.1:5477', secret: 'synthetic', gatewayError: undefined,
      close: jest.fn(async () => undefined), bindTurn: jest.fn(), suspend: jest.fn(),
    });
    jest.spyOn(crewModule, 'startKiroCrew').mockResolvedValue({ kill: jest.fn() } as unknown as
      Awaited<ReturnType<typeof crewModule.startKiroCrew>>);
    const execute = jest.spyOn(crewBridge, 'executeKiroCrewTurn').mockRejectedValue(new crewBridge.CrewExecutorDownError('Crew stream closed'));
    const invoke = jest.spyOn(ChatDataClient.prototype, 'invokeModel');
    nextTurn.mockResolvedValue({ ...accepted, session_sequence: 1 } as Awaited<ReturnType<ChatDataClient['nextTurn']>>);
    await expect(startSandboxTurn({ ...env, ADP_CHAT_DATA_URL: 'https://chat-sandbox-gateway.adp-gateway.svc:8443',
      ADP_RUN_ID: '00000000-0000-4000-8000-000000000001', ADP_EXECUTOR: 'kirocrew', KIROCREW_HOME: '/tmp/workspace',
      ADP_CREW_POD_IP: '10.1.2.3', ADP_CREW_CUSTODY_PORT: '5477',
      ADP_CREW_SESSION_MAP: '/tmp/workspace/adp-crew-session-map' })).rejects.toBeInstanceOf(crewBridge.CrewExecutorDownError);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(ChatDataClient.prototype.submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed', error_class: 'executor_down' });
    expect(invoke).not.toHaveBeenCalled();
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

  it('waits for the persistent model route before reading the first turn', async () => {
    const persistent = { ...env, ADP_RUN_ID: '00000000-0000-4000-8000-000000000001',
      ADP_CHAT_DATA_URL: 'https://chat-sandbox-gateway.adp-gateway.svc:8443' };
    modelDecision.mockRejectedValueOnce(new ChatDataError('unavailable', 503))
      .mockRejectedValueOnce(new ChatDataError('unavailable', 503));
    nextTurn.mockImplementationOnce(async () => {
      expect(modelDecision).toHaveBeenCalledTimes(3);
      return accepted as Awaited<ReturnType<ChatDataClient['nextTurn']>>;
    });
    let elapsed = 0;
    const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
    await expect(prepareSandboxTurn(persistent, async () => 'sandbox.workload.token', {
      now: () => elapsed, sleep,
    })).resolves.toMatchObject({ turn: accepted });
    expect(elapsed).toBe(500);
    expect(nextTurn).toHaveBeenCalledTimes(1);
    expect(modelDecision).toHaveBeenCalledTimes(3);
  });

  it('expires unavailable persistent model readiness within ten seconds without reading the turn', async () => {
    const persistent = { ...env, ADP_RUN_ID: '00000000-0000-4000-8000-000000000001',
      ADP_CHAT_DATA_URL: 'https://chat-sandbox-gateway.adp-gateway.svc:8443' };
    modelDecision.mockRejectedValue(new ChatDataError('unavailable', 503));
    let elapsed = 0;
    const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
    await expect(prepareSandboxTurn(persistent, async () => 'sandbox.workload.token', {
      now: () => elapsed, sleep,
    })).rejects.toMatchObject({ code: 'unavailable', status: 503 });
    expect(elapsed).toBe(10_000);
    expect(nextTurn).not.toHaveBeenCalled();
    expect(prepareData).not.toHaveBeenCalled();
  });

  it.each([new ChatDataError('denied', 403), new ChatDataError('denied', 404)])(
    'does not retry refused persistent model readiness (%s)', async failure => {
      const persistent = { ...env, ADP_RUN_ID: '00000000-0000-4000-8000-000000000001',
        ADP_CHAT_DATA_URL: 'https://chat-sandbox-gateway.adp-gateway.svc:8443' };
      modelDecision.mockRejectedValue(failure);
      const sleep = jest.fn();
      await expect(prepareSandboxTurn(persistent, async () => 'sandbox.workload.token', { sleep })).rejects.toBe(failure);
      expect(modelDecision).toHaveBeenCalledTimes(1);
      expect(nextTurn).not.toHaveBeenCalled();
      expect(sleep).not.toHaveBeenCalled();
    },
  );

  describe('refused or unavailable model-policy decision (#7968)', () => {
    const refusal = (reason: string, retryable = false) => new ChatDataError('unavailable', 503, undefined, undefined,
      { error: 'chat_model_policy_unavailable', reason, retryable, persona: 'developer', model_id: null });
    const persistent = { ...env, ADP_RUN_ID: '00000000-0000-4000-8000-000000000001',
      ADP_CHAT_DATA_URL: 'https://chat-sandbox-gateway.adp-gateway.svc:8443' };

    it('ends the owner turn with the reasoned failure instead of exiting silently', async () => {
      modelDecision.mockRejectedValue(refusal('probing_disabled'));
      await expect(startSandboxTurn(env)).rejects.toMatchObject({ name: 'SandboxModelDecisionError',
        message: 'The assistant cannot run this turn: the model policy decision was unavailable (probing_disabled). ' +
          'Ask the platform operator to enable model invocability probing.' });
      expect(nextTurn).toHaveBeenCalledTimes(1);
      expect(modelDecision).toHaveBeenCalledTimes(1);
      expect(ChatDataClient.prototype.submitTurnResult).toHaveBeenCalledTimes(1);
      expect(ChatDataClient.prototype.submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed',
        error_message: expect.stringContaining('model policy decision was unavailable (probing_disabled)') });
      expect(prepareData).not.toHaveBeenCalled();
    });

    it('accepts the persistent owner turn before recording the refusal, without the ten-second readiness wait', async () => {
      modelDecision.mockRejectedValue(refusal('not_permitted'));
      let elapsed = 0;
      const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
      await expect(prepareSandboxTurn(persistent, async () => 'sandbox.workload.token', { now: () => elapsed, sleep }))
        .rejects.toMatchObject({ name: 'SandboxModelDecisionError', failure: { reason: 'not_permitted', retryable: false } });
      expect(elapsed).toBe(0);
      expect(modelDecision).toHaveBeenCalledTimes(1);
      expect(nextTurn).toHaveBeenCalledTimes(1);
      expect(ChatDataClient.prototype.submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed',
        error_message: expect.stringContaining('(not_permitted). The selected model is not permitted for this persona.') });
      expect(prepareData).not.toHaveBeenCalled();
    });

    it('retries a transient refusal twice, two seconds apart, then records the retry hint', async () => {
      modelDecision.mockRejectedValue(refusal('runtime_posture_unavailable', true));
      let elapsed = 0;
      const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
      await expect(prepareSandboxTurn(persistent, async () => 'sandbox.workload.token', { now: () => elapsed, sleep }))
        .rejects.toMatchObject({ failure: { reason: 'runtime_posture_unavailable', retryable: true } });
      expect(sleep.mock.calls).toEqual([[2_000], [2_000]]);
      expect(modelDecision).toHaveBeenCalledTimes(3);
      expect(ChatDataClient.prototype.submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed',
        error_message: expect.stringContaining('(runtime_posture_unavailable). Please retry in a moment.') });
    });

    it('ends a later persistent mailbox turn the same way', async () => {
      jest.spyOn(ChatDataClient.prototype, 'sessionMode').mockResolvedValue('persistent');
      jest.spyOn(ChatDataClient.prototype, 'renewSession').mockResolvedValue({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' });
      jest.spyOn(ChatDataClient.prototype, 'nextMailboxTurn').mockResolvedValue({ sequence: 2, message_id: 'msg-2' } as never);
      jest.spyOn(ChatDataClient.prototype, 'admitMailboxTurn').mockResolvedValue(undefined as never);
      nextTurn.mockResolvedValue({ ...accepted, session_sequence: 1 } as Awaited<ReturnType<ChatDataClient['nextTurn']>>);
      jest.spyOn(ChatDataClient.prototype, 'invokeModel').mockResolvedValue({ stopReason: 'end_turn',
        content: [{ type: 'text', text: 'done' }] } as never);
      jest.spyOn(SandboxDataRuntime.prototype, 'record').mockResolvedValue();
      modelDecision.mockResolvedValueOnce({ modelId: 'approved-model', runId: 'run-a', tenantId: 'tenant-a', userId: 'owner', generation: 1,
        posture: 'enforcing', postureVerified: true })
        .mockRejectedValue(refusal('evidence_stale'));
      await expect(startSandboxTurn(persistent)).rejects.toMatchObject({ name: 'SandboxModelDecisionError',
        failure: { reason: 'evidence_stale' } });
      expect(nextTurn).toHaveBeenCalledTimes(2);
      expect(ChatDataClient.prototype.submitTurnResult).toHaveBeenLastCalledWith({ outcome: 'failed',
        error_message: expect.stringContaining('(evidence_stale). Ask the platform operator to enable model invocability probing.') });
    });
  });

  it('expires a missing admission without ever reading model or turn data', async () => {
    sessionScope.mockRejectedValue(new ChatDataError('denied', 404));
    let elapsed = 0;
    const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
    await expect(prepareSandboxTurn(env, async () => 'sandbox.workload.token', {
      now: () => elapsed, sleep,
    })).rejects.toMatchObject({ code: 'denied', status: 404 });
    // The window outlasts the supervisor's 60 s start deadline plus its bounded admit retries.
    expect(elapsed).toBe(90_000);
    expect(sessionScope).toHaveBeenCalledTimes(361);
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

  it.each([undefined, 'sandbox-loop'])('executes one delegated turn with selector %s and waits for its scoped history write', async executor => {
    const invoke = jest.spyOn(ChatDataClient.prototype, 'invokeModel').mockResolvedValue({
      content: [{ type: 'text', text: 'Owner reply' }], stopReason: 'end_turn', modelId: 'approved-model',
      usage: { input_tokens: 3, output_tokens: 2, estimated_usd: '0.01' },
    });
    const record = jest.spyOn(SandboxDataRuntime.prototype, 'record').mockResolvedValue();
    await expect(startSandboxTurn({ ...env, ADP_EXECUTOR: executor })).resolves.toBeUndefined();
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke.mock.calls[0].slice(2)).toEqual([undefined, true]);
    expect(record).toHaveBeenCalledWith('Owner reply');
    expect(nextTurn).toHaveBeenCalledTimes(1);
    expect(modelDecision).toHaveBeenCalledTimes(1);
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
