import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { readIdentityToken } from '../lib/projectedWorkloadToken';
import { SandboxDataRuntime } from './sandbox-data';
import { executeSandboxTurn } from './sandbox-turn';
import { withSessionHeartbeat } from './sandbox-session-heartbeat';
import { executeSandboxQualification } from './sandbox-qualification';
import { SANDBOX_GATEWAY_URL } from './sandbox-gateway-binding';
import { decideTurnModel } from './sandbox-model-decision';

export const BOOTSTRAP_ADMISSION_WINDOW_MS = 90_000;
export const BOOTSTRAP_ADMISSION_ATTEMPTS = 360;

const sensitiveEnvironment = [
  'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
  'AWS_PROFILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE',
  'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE',
  'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
  'AWS_CONTAINER_AUTHORIZATION_TOKEN', 'AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE',
  'VAULT_INTERNAL_API_KEY', 'BG_GITHUB_APP_PRIVATE_KEY',
];

async function admittedClient(
  env: NodeJS.ProcessEnv,
  workloadToken: () => Promise<string> = async () => readIdentityToken(env.ADP_WORKLOAD_TOKEN_FILE!),
  admission: { now?: () => number; sleep?: (ms: number) => Promise<void>; signal?: AbortSignal; clientSignal?: AbortSignal } = {},
) {
  if (
    env.ADP_CHAT_DATA_ENABLED !== 'true' ||
    env.ADP_CHAT_MODEL_POLICY_ENABLED !== 'true' ||
    env.CONTEXT_STRATEGY !== 'gateway' ||
    env.MEMORY_STRATEGY !== 'gateway' ||
    env.ARTIFACT_STRATEGY !== 'gateway' ||
    env.ADP_WORKLOAD_TOKEN_FILE !== '/var/run/adp-model/token' ||
    !env.ADP_CHAT_DATA_URL ||
    (env.ADP_RUN_ID !== undefined && env.ADP_CHAT_DATA_URL !== SANDBOX_GATEWAY_URL) ||
    sensitiveEnvironment.some(key => env[key] !== undefined)
  ) {
    throw new Error('Chat sandbox requires credential-free scoped identity and gateway ports');
  }
  admission.signal?.throwIfAborted();
  const client = new ChatDataClient({ baseUrl: env.ADP_CHAT_DATA_URL, workloadToken,
    signal: admission.clientSignal ?? admission.signal, crewRunId: env.ADP_RUN_ID });
  const now = admission.now ?? (() => performance.now());
  const sleep = admission.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  // The supervisor admits this pod only after it has observed it Running (up to its 60 s
  // start deadline) and may retry admission for a few seconds while the gateway catches
  // up; this bounded window must comfortably outlast that handoff, so it is 90 s rather
  // than a tighter bound. An unadmitted pod is still torn down by the supervisor.
  const deadline = now() + BOOTSTRAP_ADMISSION_WINDOW_MS;
  let scope: Awaited<ReturnType<ChatDataClient['sessionScope']>> | undefined;
  for (let attempt = 0; attempt <= BOOTSTRAP_ADMISSION_ATTEMPTS; attempt++) {
    try {
      scope = await client.sessionScope();
      break;
    } catch (error) {
      const remaining = deadline - now();
      if (!(error instanceof ChatDataError) || error.code !== 'denied' || error.status !== 404 || attempt === BOOTSTRAP_ADMISSION_ATTEMPTS || remaining <= 0) {
        throw error;
      }
      await sleep(Math.min(250, remaining));
    }
  }
  if (!scope) throw new Error('Chat sandbox admission unavailable');
  if (env.ADP_RUN_ID === undefined || client.qualificationRequired()) return { client, scope };
  const timeout = AbortSignal.timeout(10_000);
  const signal = admission.signal ? AbortSignal.any([admission.signal, timeout]) : timeout;
  const modelDeadline = now() + 10_000;
  // The turn is not accepted yet: a refused policy decision accepts it first, records the
  // reasoned failure, and only then exits (#7968).
  const model = await decideTurnModel(client, {
    turnAccepted: false, signal, abort: admission.signal, sleep,
    retryTransient: async error => {
      const remaining = modelDeadline - now();
      if (signal.aborted || error.code !== 'unavailable' || remaining <= 0) return false;
      await sleep(Math.min(250, remaining));
      return true;
    },
  });
  if (model.runId !== scope.run_id || !model.modelId || model.generation < 1) throw new ChatDataError('scope_mismatch');
  return { client, scope, model };
}

async function prepareClient(client: ChatDataClient, scope: Awaited<ReturnType<ChatDataClient['sessionScope']>>,
  readyModel?: Awaited<ReturnType<ChatDataClient['modelDecision']>>) {
  if (client.qualificationRequired()) throw new ChatDataError('scope_mismatch');
  const turn = await client.nextTurn();
  const model = readyModel ?? await decideTurnModel(client, { turnAccepted: true });
  if (model.runId !== scope.run_id || !model.modelId || model.generation < 1) {
    throw new Error('Chat sandbox model scope unavailable');
  }
  const data = new SandboxDataRuntime(client, scope, turn);
  return { client, turn, data, model, context: await data.prepare() };
}

export async function prepareSandboxTurn(
  env: NodeJS.ProcessEnv,
  workloadToken?: () => Promise<string>,
  admission: { now?: () => number; sleep?: (ms: number) => Promise<void>; signal?: AbortSignal } = {},
) {
  const { client, scope, model } = await admittedClient(env, workloadToken, admission);
  return prepareClient(client, scope, model);
}

export async function startSandboxTurn(env: NodeJS.ProcessEnv = process.env, signal?: AbortSignal): Promise<void> {
  if (env.ADP_EXECUTOR !== undefined && !['sandbox-loop', 'claude-code', 'kirocrew'].includes(env.ADP_EXECUTOR)) {
    throw new Error(`Unknown ADP_EXECUTOR: ${env.ADP_EXECUTOR}`);
  }
  const lifetime = new AbortController();
  try { await executeSandboxLoop(env, signal, env.ADP_EXECUTOR === 'kirocrew' ? lifetime.signal : signal); }
  finally { lifetime.abort(); }
}

async function executeSandboxLoop(env: NodeJS.ProcessEnv, signal?: AbortSignal, clientSignal?: AbortSignal): Promise<void> {
  const { client, scope, model } = await admittedClient(env, undefined, { signal, clientSignal });
  if (client.qualificationRequired()) {
    try {
      await client.nextTurn();
      await executeSandboxQualification(client, signal);
    } catch (error) {
      if (!signal?.aborted) await client.submitTurnResult({ outcome: 'failed' }).catch(() => undefined);
      throw error;
    }
    return;
  }
  let prepared = await prepareClient(env.ADP_EXECUTOR === 'kirocrew' && signal ? client.withSignal(signal) : client, scope, model);
  let crew: Awaited<ReturnType<typeof import('./crew-kirocrew').startKiroCrew>> | undefined;
  let adapter: Awaited<ReturnType<typeof import('./crew-model-adapter').startCrewModelAdapter>> | undefined;
  let custody: Awaited<ReturnType<typeof import('./crew-dashboard-custody').createCrewDashboardCustody>> | undefined;
  let consolidated = false;
  const consolidate = async (signal?: AbortSignal) => {
    const deadline = AbortSignal.timeout(90_000);
    const stopSignal = signal ? AbortSignal.any([signal, deadline]) : deadline;
    const stopClient = client.withSignal(stopSignal);
    const stopModel = await stopClient.modelDecision(stopSignal);
    if (stopModel.runId !== prepared.model.runId || stopModel.generation !== prepared.model.generation ||
        stopModel.modelId !== prepared.model.modelId) throw new ChatDataError('scope_mismatch');
    const stopAdapter = await (await import('./crew-model-adapter')).startCrewModelAdapter(stopClient, stopModel.modelId, stopSignal, false);
    try {
      const work = async (scopedSignal: AbortSignal) => {
        stopAdapter.bindTurn(stopClient, stopModel.modelId, scopedSignal, false);
        await (await import('./crew-kirocrew')).consolidateKiroCrew(env.KIROCREW_HOME!, stopModel.modelId, stopAdapter, undefined, scopedSignal);
      };
      if (signal) await work(stopSignal);
      else await withSessionHeartbeat(stopClient, work, stopSignal);
      consolidated = true;
    } finally { await stopAdapter.close(); }
  };
  try {
    if (env.ADP_EXECUTOR === 'kirocrew') {
      if (await client.sessionMode() !== 'persistent' || !env.ADP_RUN_ID || env.KIROCREW_HOME !== '/tmp/workspace' ||
          env.ADP_CREW_SESSION_MAP !== '/tmp/workspace/adp-crew-session-map') {
        throw new ChatDataError('scope_mismatch');
      }
      adapter = await (await import('./crew-model-adapter')).startCrewModelAdapter(
        client.withSignal(signal ?? new AbortController().signal), prepared.model.modelId, signal);
      if (!env.ADP_CREW_POD_IP || env.ADP_CREW_CUSTODY_PORT !== '5477' ||
          (env.ADP_CREW_SCOPE !== undefined && env.ADP_CREW_SCOPE !== 'user')) {
        throw new ChatDataError('scope_mismatch');
      }
      crew = await (await import('./crew-kirocrew')).startKiroCrew(env.KIROCREW_HOME, prepared.model.modelId,
        signal, { adapter, toolsRunId: env.ADP_RUN_ID, ...(env.ADP_CREW_DASHBOARD_ORIGIN || env.ADP_CREW_SCOPE === 'user' ?
          { podIP: env.ADP_CREW_POD_IP } : {}),
          ...(env.ADP_CREW_DASHBOARD_ORIGIN ? { dashboardOrigin: env.ADP_CREW_DASHBOARD_ORIGIN } : {}) });
      if (env.ADP_CREW_DASHBOARD_ORIGIN) {
        custody = (await import('./crew-dashboard-custody')).createCrewDashboardCustody(
          client, env.ADP_RUN_ID, env.ADP_CREW_POD_IP, env.KIROCREW_HOME);
        await custody.listen(5477);
      }
    }
  let retained: Awaited<ReturnType<typeof executeSandboxTurn>> | undefined;
  for (;;) {
    let following: Awaited<ReturnType<ChatDataClient['nextMailboxTurn']>> = null;
    await withSessionHeartbeat(prepared.client, async scopedSignal => {
      if (env.ADP_EXECUTOR === 'claude-code') {
        if (await prepared.client.sessionMode() !== 'persistent' || !env.ADP_RUN_ID) throw new ChatDataError('scope_mismatch');
        await (await import('./crew-turn')).executeCrewTurn(prepared, scopedSignal);
      } else if (env.ADP_EXECUTOR === 'kirocrew') {
        consolidated = false;
        adapter!.bindTurn(prepared.client, prepared.model.modelId, scopedSignal);
        try {
          await (await import('./crew-kirocrew-bridge')).executeKiroCrewTurn({ ...prepared, beforeReceipt: async () => {
            adapter!.suspend();
            await consolidate(scopedSignal);
          } }, env.KIROCREW_HOME!,
            env.ADP_CREW_SESSION_MAP!, undefined, scopedSignal);
        } catch (error) {
          if (!scopedSignal.aborted) {
            const { CrewExecutorDownError } = await import('./crew-kirocrew-bridge');
            await prepared.client.submitTurnResult({ outcome: 'failed',
              ...(error instanceof CrewExecutorDownError ? { error_class: 'executor_down' as const } : {}) }).catch(() => undefined);
          }
          throw error;
        } finally {
          adapter!.suspend();
        }
      } else {
        retained = await executeSandboxTurn(prepared, scopedSignal, retained);
      }
      if (await prepared.client.sessionMode() !== 'persistent') return;
      const sequence = prepared.turn.session_sequence;
      if (!sequence) throw new ChatDataError('invalid_response');
      while (!following) {
        scopedSignal.throwIfAborted();
        try { following = await prepared.client.nextMailboxTurn(sequence); }
        catch (error) {
          if (env.ADP_EXECUTOR === 'kirocrew') scopedSignal.throwIfAborted();
          throw error;
        }
      }
    }, signal);
    if (!following) return;
    signal?.throwIfAborted();
    await client.admitMailboxTurn(following);
    const turnClient = env.ADP_EXECUTOR === 'kirocrew' && signal ? client.withSignal(signal) : client;
    const scope = await turnClient.sessionScope();
    const turn = await turnClient.nextTurn();
    const model = await decideTurnModel(turnClient, { turnAccepted: true, abort: signal });
    if (model.runId !== scope.run_id || !model.modelId || model.generation < 1) throw new ChatDataError('scope_mismatch');
    const data = new SandboxDataRuntime(turnClient, scope, turn);
    prepared = { client: turnClient, turn, data, model, context: await data.prepare() };
  }
  } catch (error) {
    if (!(signal?.aborted && error === signal.reason)) throw error;
  } finally {
    let stopFailure: unknown;
    try {
      if (signal?.aborted && crew && adapter && env.KIROCREW_HOME && !consolidated) await consolidate();
    } catch (error) { stopFailure = error; }
    finally { adapter?.suspend(); }
    try {
      if (signal?.aborted && crew && adapter && env.KIROCREW_HOME) {
        const { recordCrewStop } = await import('./crew-stop-result');
        const receipt = await recordCrewStop(env.KIROCREW_HOME, stopFailure ? 'failed' : 'completed');
        console.log('ADP_CREW_STOP_RESULT', JSON.stringify(receipt));
      }
    } finally {
      try { await custody?.close(); }
      finally {
        crew?.kill('SIGTERM');
        await adapter?.close();
      }
    }
  }
}

if (require.main === module) {
  const cancellation = new AbortController();
  const cancel = () => cancellation.abort();
  process.once('SIGTERM', cancel);
  process.once('SIGINT', cancel);
  startSandboxTurn(process.env, cancellation.signal).catch(error => {
    console.error((error as Error).message);
    process.exitCode = 1;
  }).finally(() => {
    process.removeListener('SIGTERM', cancel);
    process.removeListener('SIGINT', cancel);
    // Exit as soon as the turn result is recorded: an open keep-alive socket or stray timer
    // would otherwise keep the container, and with it the pod, alive until the supervisor's
    // delete and its grace period catch up (#8047).
    process.exit(process.exitCode ?? 0);
  });
}
