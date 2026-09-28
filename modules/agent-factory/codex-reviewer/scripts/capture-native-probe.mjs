/** Capture native SDK shapes locally. Never forwards requests or returns model output. */
import { Codex } from '@openai/codex-sdk';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { reviewOutputSchema } from '../dist/contracts.js';

export async function captureNativeProbe(model, persona) {
  if (!['developer', 'reviewer'].includes(persona)) throw new Error('Unknown native probe persona');
  const root = await mkdtemp(join(tmpdir(), 'adp-native-probe-'));
  const home = join(root, 'home');
  const workspace = join(root, 'workspace');
  await mkdir(home, { mode: 0o700 });
  await mkdir(join(home, '.codex'), { mode: 0o700 });
  await mkdir(workspace, { mode: 0o700 });
  const controller = new AbortController();
  let body;
  let failure;
  let requests = 0;
  const server = createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === '/v1/responses') {
      response.writeHead(405).end(); return; // Reject SDK WebSocket negotiation; use HTTP capture.
    }
    try {
      const chunks = []; let bytes = 0;
      for await (const chunk of request) {
        if ((bytes += chunk.length) > 262144) throw new Error('Probe capture too large');
        chunks.push(chunk);
      }
      if (request.method !== 'POST' || request.url !== '/v1/responses' || ++requests !== 1) throw new Error(`Unexpected SDK request: ${request.method} ${request.url} (${requests})`);
      body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch (error) { failure = error; }
    // No successful response enters the SDK, so it cannot dispatch any tool.
    response.writeHead(403, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ error: { code: 'probe_capture_only', message: 'Local request capture; no model execution' } }));
    controller.abort();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const codex = new Codex({ baseUrl: `http://127.0.0.1:${server.address().port}/v1`, apiKey: 'invalid-local-probe-key',
      config: { developer_instructions: 'ADP native SDK invocability probe. Reply OK without calling tools.',
      },
      env: { PATH: process.env.PATH, HOME: home, CODEX_HOME: join(home, '.codex'),
        XDG_CONFIG_HOME: join(home, '.config'), XDG_CACHE_HOME: join(home, '.cache'),
        TMPDIR: root, TZ: 'UTC', GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' },
    });
    const thread = codex.startThread({ workingDirectory: workspace, skipGitRepoCheck: true,
      model, modelReasoningEffort: 'high', sandboxMode: 'danger-full-access', approvalPolicy: 'never',
      networkAccessEnabled: persona === 'developer', webSearchMode: 'disabled', threadSource: `adp-agent-codex-${persona}` });
    let rejected = false; let sdkError;
    try {
      await thread.run(persona === 'reviewer'
        ? 'Return an approve verdict with summary OK, empty findings and empty validationGaps. Do not call tools.'
        : 'Reply OK. Do not call tools.', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]),
          ...(persona === 'reviewer' ? { outputSchema: reviewOutputSchema } : {}) });
    } catch (error) { rejected = true; sdkError = error; }
    if (!rejected || failure || !body || requests !== 1 || body.model !== model || body.stream !== true) {
      throw failure ?? new Error(`Native SDK did not produce one rejected probe request: ${String(sdkError).slice(0, 2000)}`);
    }
    return { body, root };
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
}
