const assert = require('node:assert/strict');
const dns = require('node:dns');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const config = JSON.parse(process.argv[2]);
Date.now = () => config.now * 1000;
dns.lookup = (hostname, options, callback) => {
  if (typeof options === 'function') { callback = options; options = {}; }
  if (!['chat-gateway.test', '127.0.0.1'].includes(hostname)) throw new Error('Unexpected integration-test destination');
  process.nextTick(() => options?.all ? callback(null, [{ address: '127.0.0.1', family: 4 }]) : callback(null, '127.0.0.1', 4));
};
const source = name => path.join(config.agent, 'src/complex-task-chat', name);
const { startSandboxTurn } = require(source('sandbox-entrypoint.ts'));
const { SandboxDataRuntime } = require(source('sandbox-data.ts'));
const crew = require(source('crew-kirocrew.ts'));
const bridge = require(source('crew-kirocrew-bridge.ts'));
const stop = require(source('crew-stop-result.ts'));
require(path.join(config.agent, 'src/lib/projectedWorkloadToken.ts')).readIdentityToken = async () => 'sandbox-token';

async function main() {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'crew-lifecycle-'));
  const origin = 'https://chat-sandbox-gateway.adp-gateway.svc:8443';
  const request = globalThis.fetch;
  const events = [];
  const cancellation = new AbortController();
  let capability;
  let receipted = false;
  let modelRequestsAfterReceipt = 0;
  const terminate = () => { events.push('SIGTERM'); cancellation.abort(); };
  process.once('SIGTERM', terminate);
  globalThis.fetch = async (input, options) => {
    const url = String(input);
    if (!url.startsWith(origin)) return request(input, options);
    if (receipted && url.includes('/v1/chat/model/')) modelRequestsAfterReceipt++;
    if (url.endsWith('/session/next')) {
      assert(receipted);
      events.push('idle');
      setTimeout(() => process.kill(process.pid, 'SIGTERM'), 25);
    }
    const response = await request(url.replace(origin, config.url), options);
    if (url.endsWith('/bootstrap') && response.ok) capability = (await response.clone().json()).capability;
    if (url.endsWith('/turn/result') && response.ok) {
      assert.equal((await response.clone().json()).outcome, 'completed');
      receipted = true;
      events.push('receipt');
    }
    return response;
  };
  SandboxDataRuntime.prototype.prepare = async () => ({ messages: [], memories: [], attachments: [], userMessage: 'Reply' });
  crew.startKiroCrew = async () => ({ kill: () => events.push('stopped') });
  const execute = bridge.executeKiroCrewTurn;
  let sent = false;
  bridge.executeKiroCrewTurn = (prepared, _home, _map, _gateway, signal) => execute(prepared, home,
    path.join(home, 'adp-crew-session-map'), {
      createSession: async () => 'crew-one',
      read: async () => ({ running: false, queue_depth: 0, total: sent ? 2 : 0,
        messages: sent ? [{ role: 'assistant', content: 'Reply from Crew' }] : [] }),
      send: async () => { sent = true; },
      stream: async () => new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode(`event: chat_message\ndata: ${JSON.stringify({
          slot: 'crew-one', role: 'assistant', content: 'Reply from Crew', meta: { mid: 'one' },
        })}\n\n`));
      } })),
    }, signal);
  const consolidate = crew.consolidateKiroCrew;
  crew.consolidateKiroCrew = (_home, model, adapter, _runner, signal) => consolidate(home, model, adapter, async (_file, args, options) => {
    assert.deepEqual(args, ['consolidate', '--all']);
    assert.equal(options.signal.aborted, false);
    assert.equal(receipted, false);
    const response = await request(`${adapter.baseUrl}/v1/messages`, {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
      body: JSON.stringify({ model, max_tokens: 128, messages: [{ role: 'user', content: 'Consolidate ten pending messages' }] }),
    });
    assert.equal(response.status, 200, await response.text());
    events.push('consolidation');
    return { stdout: '', stderr: '' };
  }, signal);
  const recordStop = stop.recordCrewStop;
  stop.recordCrewStop = (_home, result) => recordStop(home, result);
  const log = console.log;
  console.log = () => {};
  try {
    await startSandboxTurn({ ADP_CHAT_DATA_ENABLED: 'true', ADP_CHAT_MODEL_POLICY_ENABLED: 'true', ADP_CHAT_DATA_URL: origin,
      CONTEXT_STRATEGY: 'gateway', MEMORY_STRATEGY: 'gateway', ARTIFACT_STRATEGY: 'gateway',
      ADP_WORKLOAD_TOKEN_FILE: '/var/run/adp-model/token', ADP_RUN_ID: config.crewRunId, ADP_EXECUTOR: 'kirocrew',
      KIROCREW_HOME: '/tmp/workspace', ADP_CREW_SESSION_MAP: '/tmp/workspace/adp-crew-session-map',
      ADP_CREW_POD_IP: '10.1.2.3', ADP_CREW_CUSTODY_PORT: '5477',
    }, cancellation.signal);
    const sealed = await request(`${config.url}/v1/chat/model/decision`, {
      method: 'POST', headers: { 'content-type': 'application/json', Authorization: `Bearer ${capability}`, 'X-Adp-Workload-Token': 'sandbox-token' },
      body: JSON.stringify({ run_id: 'run-user', session_id: 'session-a', nonce: 'a'.repeat(64), model_policy_contract: 1 }),
    });
    await sealed.text();
    return { events, modelRequestsAfterReceipt, sealedModelStatus: sealed.status,
      stop: JSON.parse(await fs.readFile(path.join(home, 'adp-crew-session-map', '.stop-result.json'), 'utf8')) };
  } finally {
    console.log = log;
    process.removeListener('SIGTERM', terminate);
    await fs.rm(home, { recursive: true, force: true });
  }
}
main().then(result => process.stdout.write(JSON.stringify(result))).catch(error => { console.error(error); process.exitCode = 1; });
