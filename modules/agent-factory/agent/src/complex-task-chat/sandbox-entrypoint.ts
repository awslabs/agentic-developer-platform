import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { readIdentityToken } from '../lib/projectedWorkloadToken';
import { SandboxDataRuntime } from './sandbox-data';
import { executeSandboxTurn } from './sandbox-turn';
import { withSessionHeartbeat } from './sandbox-session-heartbeat';

const sensitiveEnvironment = [
  'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
  'AWS_PROFILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE',
  'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE',
  'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
  'AWS_CONTAINER_AUTHORIZATION_TOKEN', 'AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE',
  'VAULT_INTERNAL_API_KEY', 'BG_GITHUB_APP_PRIVATE_KEY',
];

export async function prepareSandboxTurn(
  env: NodeJS.ProcessEnv,
  workloadToken: () => Promise<string> = async () => readIdentityToken(env.ADP_WORKLOAD_TOKEN_FILE!),
  admission: { now?: () => number; sleep?: (ms: number) => Promise<void>; signal?: AbortSignal } = {},
): Promise<{
  client: ChatDataClient;
  turn: Awaited<ReturnType<ChatDataClient['nextTurn']>>;
  data: SandboxDataRuntime;
  context: Awaited<ReturnType<SandboxDataRuntime['prepare']>>;
}> {
  if (
    env.ADP_CHAT_DATA_ENABLED !== 'true' ||
    env.ADP_CHAT_MODEL_POLICY_ENABLED !== 'true' ||
    env.CONTEXT_STRATEGY !== 'gateway' ||
    env.MEMORY_STRATEGY !== 'gateway' ||
    env.ARTIFACT_STRATEGY !== 'gateway' ||
    env.ADP_WORKLOAD_TOKEN_FILE !== '/var/run/adp-model/token' ||
    !env.ADP_CHAT_DATA_URL ||
    sensitiveEnvironment.some(key => env[key] !== undefined)
  ) {
    throw new Error('Chat sandbox requires credential-free scoped identity and gateway ports');
  }
  admission.signal?.throwIfAborted();
  const client = new ChatDataClient({ baseUrl: env.ADP_CHAT_DATA_URL, workloadToken, signal: admission.signal });
  const now = admission.now ?? (() => performance.now());
  const sleep = admission.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const deadline = now() + 20_000;
  let scope: Awaited<ReturnType<ChatDataClient['sessionScope']>> | undefined;
  for (let attempt = 0; attempt <= 80; attempt++) {
    try {
      scope = await client.sessionScope();
      break;
    } catch (error) {
      const remaining = deadline - now();
      if (!(error instanceof ChatDataError) || error.code !== 'denied' || error.status !== 404 || attempt === 80 || remaining <= 0) {
        throw error;
      }
      await sleep(Math.min(250, remaining));
    }
  }
  if (!scope) throw new Error('Chat sandbox admission unavailable');
  const turn = await client.nextTurn();
  const model = await client.modelDecision();
  if (model.runId !== scope.run_id || !model.modelId || model.generation < 1) {
    throw new Error('Chat sandbox model scope unavailable');
  }
  const data = new SandboxDataRuntime(client, scope, turn);
  return { client, turn, data, context: await data.prepare() };
}

export async function startSandboxTurn(env: NodeJS.ProcessEnv = process.env, signal?: AbortSignal): Promise<void> {
  let prepared = await prepareSandboxTurn(env, undefined, { signal });
  let retained: Awaited<ReturnType<typeof executeSandboxTurn>> | undefined;
  for (;;) {
    let following: Awaited<ReturnType<ChatDataClient['nextMailboxTurn']>> = null;
    await withSessionHeartbeat(prepared.client, async scopedSignal => {
      retained = await executeSandboxTurn(prepared, scopedSignal, retained);
      if (await prepared.client.sessionMode() !== 'persistent') return;
      const sequence = prepared.turn.session_sequence;
      if (!sequence) throw new ChatDataError('invalid_response');
      while (!following) {
        scopedSignal.throwIfAborted();
        following = await prepared.client.nextMailboxTurn(sequence);
      }
    }, signal);
    if (!following) return;
    signal?.throwIfAborted();
    const client = prepared.client;
    await client.admitMailboxTurn(following);
    const scope = await client.sessionScope();
    const turn = await client.nextTurn();
    const model = await client.modelDecision();
    if (model.runId !== scope.run_id || !model.modelId || model.generation < 1) throw new ChatDataError('scope_mismatch');
    const data = new SandboxDataRuntime(client, scope, turn);
    prepared = { client, turn, data, context: await data.prepare() };
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
  });
}
