import { createHash } from 'node:crypto';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { startSandboxTurn } from './sandbox-entrypoint';
import { SandboxDataRuntime } from './sandbox-data';
import * as crew from './crew-kirocrew';
import * as bridge from './crew-kirocrew-bridge';
import * as stop from './crew-stop-result';

jest.mock('../lib/projectedWorkloadToken', () => ({ readIdentityToken: jest.fn(() => 'synthetic.workload.token') }));

const origin = 'https://chat-sandbox-gateway.adp-gateway.svc:8443';
const env = { ADP_CHAT_DATA_ENABLED: 'true', ADP_CHAT_MODEL_POLICY_ENABLED: 'true', ADP_CHAT_DATA_URL: origin,
  CONTEXT_STRATEGY: 'gateway', MEMORY_STRATEGY: 'gateway', ARTIFACT_STRATEGY: 'gateway',
  ADP_WORKLOAD_TOKEN_FILE: '/var/run/adp-model/token', ADP_RUN_ID: '00000000-0000-4000-8000-000000000001', ADP_EXECUTOR: 'kirocrew',
  KIROCREW_HOME: '/tmp/workspace', ADP_CREW_SESSION_MAP: '/tmp/workspace/adp-crew-session-map',
  ADP_CREW_POD_IP: '10.1.2.3', ADP_CREW_CUSTODY_PORT: '5477' };

describe('retained Crew model authority with the real HTTP adapter and data client', () => {
  const request = globalThis.fetch;
  const consolidate = crew.consolidateKiroCrew;
  let currentRun: string;
  let authority: 'active' | 'sealed' | 'expired' | 'fenced';
  let activeAdapter: { baseUrl: string; secret: string };
  let modelCalls: Array<{ run: string; generation: number; authorization: string; deliver: boolean; signal: AbortSignal }>;

  const generation = () => currentRun === 'run-a' ? 1 : 2;
  const model = () => currentRun === 'run-a' ? 'model-first' : 'model-second';
  const binding = () => ({ capability: `capability.${currentRun}`, run_id: currentRun, session_id: 'session-a',
    attempt: 1, lease_generation: generation(), session_mode: 'persistent', expires_at: Math.floor(Date.now() / 1000) + 300 });
  const infer = (adapter: { baseUrl: string; secret: string }, modelId = 'model-first') => request(`${adapter.baseUrl}/v1/messages`, {
    method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
    body: JSON.stringify({ model: modelId, max_tokens: 128, messages: [{ role: 'user', content: 'Extract memory' }] }),
  });

  beforeEach(() => {
    currentRun = 'run-a';
    authority = 'active';
    modelCalls = [];
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
      if (!String(url).startsWith(origin)) throw new Error('Unexpected gateway destination');
      const path = String(url).slice(origin.length);
      const body = JSON.parse(init!.body as string);
      const headers = new Headers(init?.headers);
      if (path === '/v1/chat/data/bootstrap') return Response.json(binding());
      if (path === '/v1/chat/data/session/admit') {
        expect(body.run_id).toBe('run-a');
        expect(headers.get('authorization')).toBe('Bearer capability.run-a');
        currentRun = 'run-b';
        authority = 'active';
        return Response.json(binding());
      }
      if (path === '/v1/chat/turn/next') return Response.json({ run_id: currentRun, session_id: 'session-a',
        lease_generation: generation(), session_sequence: generation(),
        turn: { ref: `user_${createHash('sha256').update(currentRun).digest('hex')}`,
          message: { role: 'user', content: currentRun, ts: '2026-10-13T12:00:00Z', tokens: 2, parts: [] } } });
      if (path === '/v1/chat/data/session/next') return Response.json({ run_id: currentRun, session_id: 'session-a',
        lease_generation: generation(), turn: { sequence: 2, turn_id: 'run-b', message: 'Follow up' } });
      if (path === '/v1/chat/data/session/state') return Response.json({ mode: 'persistent', sequence: generation(), health: 'active' });
      if (path === '/v1/chat/data/turn/result') {
        authority = 'sealed';
        return Response.json({ ...body, message_id: body.message_id ?? null,
          attempt: 1, lease_generation: generation(), sandbox_uid: 'pod-one', terminal: false });
      }
      if (path === '/v1/chat/model/invoke') {
        if (authority !== 'active' || body.run_id !== currentRun || headers.get('authorization') !== `Bearer capability.${currentRun}`) {
          return Response.json({ detail: { error: 'chat_scope_refused' } }, { status: 404 });
        }
        expect(init!.signal!.aborted).toBe(false);
        modelCalls.push({ run: body.run_id, generation: generation(), authorization: headers.get('authorization')!,
          deliver: body.deliver_response === true, signal: init!.signal! });
        return Response.json({ run_id: currentRun, session_id: 'session-a', operation_id: body.operation_id,
          request_digest: createHash('sha256').update(canonicalJson(body.request)).digest('hex'), model_id: model(),
          lease_generation: generation(), status: 'confirmed', handoff: 'confirmed', reservation_status: 'settled',
          usage: { input_tokens: 2, output_tokens: 1, estimated_usd: '0.001' }, automatic_replay_permitted: false,
          content: [{ type: 'text', text: 'Memory extracted' }], stop_reason: 'end_turn' });
      }
      throw new Error(`Unexpected gateway request: ${path}`);
    });
    jest.spyOn(ChatDataClient.prototype, 'modelDecision').mockImplementation(async function (this: ChatDataClient) {
      if (authority !== 'active') throw new ChatDataError('denied', 404);
      const scope = await this.sessionScope();
      return { modelId: model(), runId: scope.run_id, generation: generation(), tenantId: 'tenant-one', userId: 'owner-one',
        posture: 'enforcing' as const, postureVerified: true };
    });
    jest.spyOn(SandboxDataRuntime.prototype, 'prepare').mockResolvedValue({ messages: [], protectedMessageCount: 0,
      memories: [], attachments: [], userMessage: 'Reply',
      meta: { rawMessageCount: 0, summaryCount: 0, estimatedTokens: 0, compactionTriggered: false } });
    jest.spyOn(crew, 'startKiroCrew').mockImplementation(async (_home, _model, _signal, options) => {
      activeAdapter = options!.adapter!;
      return { kill: jest.fn() } as unknown as Awaited<ReturnType<typeof crew.startKiroCrew>>;
    });
    jest.spyOn(stop, 'recordCrewStop').mockImplementation(async (_home, consolidation) => ({
      status: 'checkpoint_unverified', last_verified_checkpoint_id: null, last_verified_checkpoint_id_known: false, consolidation,
    }));
  });
  afterEach(() => jest.restoreAllMocks());

  it('rebinds follow-up inference and consolidates after cancellation without reusing active-turn authority', async () => {
    const cancellation = new AbortController();
    jest.spyOn(bridge, 'executeKiroCrewTurn').mockImplementation(async prepared => {
      const response = await infer(activeAdapter);
      expect(response.status).toBe(200);
      expect((await response.json() as { model: string }).model).toBe(model());
      if (currentRun === 'run-a') {
        await prepared.beforeReceipt!();
        await prepared.client.submitTurnResult({ outcome: 'completed', message_id: 'reply-one', reply_granularity: 'row' });
      }
      else cancellation.abort();
    });
    const runner = jest.fn(async (_file, args, options) => {
      expect(args).toEqual(['consolidate', '--all']);
      expect(options.timeout).toBe(90_000);
      expect(options.signal.aborted).toBe(false);
      expect(cancellation.signal.aborted).toBe(currentRun === 'run-b');
      const oldResponse = await infer(activeAdapter);
      expect([400, 403]).toContain(oldResponse.status);
      await oldResponse.text();
      const response = await infer({ baseUrl: options.env.ANTHROPIC_BASE_URL, secret: options.env.ANTHROPIC_API_KEY }, options.env.ANTHROPIC_MODEL);
      expect(response.status).toBe(200);
      await response.text();
      return { stdout: '', stderr: '' };
    });
    jest.spyOn(crew, 'consolidateKiroCrew').mockImplementation((home, modelId, adapter, _runner, signal) =>
      consolidate(home, modelId, adapter, runner as unknown as Parameters<typeof consolidate>[3], signal));
    await expect(startSandboxTurn(env, cancellation.signal)).resolves.toBeUndefined();
    expect(modelCalls.map(({ signal: _signal, ...call }) => call)).toEqual([
      { run: 'run-a', generation: 1, authorization: 'Bearer capability.run-a', deliver: true },
      { run: 'run-a', generation: 1, authorization: 'Bearer capability.run-a', deliver: false },
      { run: 'run-b', generation: 2, authorization: 'Bearer capability.run-b', deliver: true },
      { run: 'run-b', generation: 2, authorization: 'Bearer capability.run-b', deliver: false },
    ]);
    expect(runner).toHaveBeenCalledTimes(2);
    expect(stop.recordCrewStop).toHaveBeenCalledWith('/tmp/workspace', 'completed');
  });

  it('uses completed pre-receipt consolidation when SIGTERM arrives during sealed idle waiting', async () => {
    const cancellation = new AbortController();
    jest.spyOn(bridge, 'executeKiroCrewTurn').mockImplementation(async prepared => {
      await prepared.beforeReceipt!();
      await prepared.client.submitTurnResult({ outcome: 'completed', message_id: 'reply-one', reply_granularity: 'row' });
    });
    jest.spyOn(ChatDataClient.prototype, 'nextMailboxTurn').mockImplementation(async () => {
      expect(authority).toBe('sealed');
      cancellation.abort();
      return null;
    });
    const runner = jest.fn(async (_file, _args, options) => {
      expect(authority).toBe('active');
      const response = await infer({ baseUrl: options.env.ANTHROPIC_BASE_URL, secret: options.env.ANTHROPIC_API_KEY });
      expect(response.status).toBe(200);
      await response.text();
      return { stdout: '', stderr: '' };
    });
    jest.spyOn(crew, 'consolidateKiroCrew').mockImplementation((home, modelId, adapter, _runner, signal) =>
      consolidate(home, modelId, adapter, runner as unknown as Parameters<typeof consolidate>[3], signal));
    await expect(startSandboxTurn(env, cancellation.signal)).resolves.toBeUndefined();
    expect(runner).toHaveBeenCalledTimes(1);
    expect(modelCalls).toHaveLength(1);
    expect(modelCalls[0].deliver).toBe(false);
    expect(ChatDataClient.prototype.modelDecision).toHaveBeenCalledTimes(2);
    expect(stop.recordCrewStop).toHaveBeenCalledWith('/tmp/workspace', 'completed');
  });

  it.each(['sealed', 'expired', 'fenced'] as const)('does not revive %s authority for shutdown', async state => {
    const cancellation = new AbortController();
    jest.spyOn(bridge, 'executeKiroCrewTurn').mockImplementation(async () => {
      const response = await infer(activeAdapter);
      expect(response.status).toBe(200);
      await response.text();
      authority = state;
      cancellation.abort();
    });
    const consolidation = jest.spyOn(crew, 'consolidateKiroCrew');
    await expect(startSandboxTurn(env, cancellation.signal)).resolves.toBeUndefined();
    expect(consolidation).not.toHaveBeenCalled();
    expect(modelCalls).toHaveLength(1);
    expect(stop.recordCrewStop).toHaveBeenCalledWith('/tmp/workspace', 'failed');
  });

  it('fails consolidation if authority is fenced after shutdown admission', async () => {
    const cancellation = new AbortController();
    jest.spyOn(bridge, 'executeKiroCrewTurn').mockImplementation(async () => { cancellation.abort(); });
    const runner = jest.fn(async (_file, _args, options) => {
      authority = 'fenced';
      const response = await infer({ baseUrl: options.env.ANTHROPIC_BASE_URL, secret: options.env.ANTHROPIC_API_KEY });
      expect(response.status).toBe(403);
      await response.text();
      throw new Error('Model authority refused');
    });
    jest.spyOn(crew, 'consolidateKiroCrew').mockImplementation((home, modelId, adapter, _runner, signal) =>
      consolidate(home, modelId, adapter, runner as unknown as Parameters<typeof consolidate>[3], signal));
    await expect(startSandboxTurn(env, cancellation.signal)).resolves.toBeUndefined();
    expect(runner).toHaveBeenCalledTimes(1);
    expect(modelCalls).toHaveLength(0);
    expect(stop.recordCrewStop).toHaveBeenCalledWith('/tmp/workspace', 'failed');
  });
});
