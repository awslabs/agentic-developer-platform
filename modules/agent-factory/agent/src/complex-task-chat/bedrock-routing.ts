import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import type { TaskPayload } from './sqs-client';

/** One message per pod: set up routing before constructing any model client. */
export async function withChatBedrockRouting<T>(task: TaskPayload, run: () => Promise<T>): Promise<T> {
  const mode = (process.env.ADP_BEDROCK_VIA || 'gateway').trim().toLowerCase();
  if (mode !== 'gateway') {
    throw new Error('Chat Bedrock calls require ADP_BEDROCK_VIA=gateway to enforce the user routing rule.');
  }
  if (!task.message_id || !task.tenant_id || !process.env.SIGV4_PROXY_TARGET) {
    throw new Error('Cannot start Bedrock routing without a registered run, tenant, and gateway target.');
  }
  const port = process.env.SIGV4_PROXY_PORT || '9090';
  const endpoint = `http://127.0.0.1:${port}`;
  const proxyEnv = { ...process.env, SIGV4_PROXY_PORT: port, TENANT_ID: task.tenant_id, ADP_MESSAGE_ID: task.message_id };
  // The gateway authenticates the pod. User AWS credentials belong to tools,
  // while the gateway selects the model account from the registered run owner.
  for (const key of ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE']) {
    delete (proxyEnv as NodeJS.ProcessEnv)[key];
  }
  const proxy = spawn(process.execPath, [join(__dirname, '..', 'sigv4-proxy.js')], { env: proxyEnv, stdio: 'inherit' });
  let proxyError: Error | undefined;
  proxy.on('error', error => { proxyError = error; });
  const updates: NodeJS.ProcessEnv = {
    CLAUDE_CODE_USE_BEDROCK: '1',
    ANTHROPIC_BEDROCK_BASE_URL: endpoint,
    ANTHROPIC_BASE_URL: undefined,
    CLAUDE_CODE_SKIP_BEDROCK_AUTH: '1',
    CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD: '1',
  };
  const previous = Object.fromEntries(Object.keys(updates).map(key => [key, process.env[key]]));
  try {
    let healthy = false;
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      if (proxyError || proxy.exitCode !== null) {
        throw new Error('Bedrock gateway proxy failed to start.');
      }
      try {
        const response = await fetch(`${endpoint}/__health`, { signal: AbortSignal.timeout(500) });
        healthy = response.ok;
        await response.body?.cancel();
      } catch { /* Retry readiness; never bypass the gateway. */ }
      if (healthy) break;
      await delay(100);
    }
    if (!healthy) throw new Error('Bedrock gateway proxy did not become ready.');
    for (const [key, value] of Object.entries(updates)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    return await run();
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    proxy.kill('SIGTERM');
  }
}
