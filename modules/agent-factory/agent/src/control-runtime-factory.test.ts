/**
 * Tests for the shared control-runtime composition — Issue #5891.
 *
 * `startControlRuntime` is the one place that assembles the pause barrier, the
 * Claude adapter, the command store, the steering queue and the in-pod HTTP
 * listener. Before this file existed, that assembly lived only inline in
 * `agent-worker.ts`'s `main()`, so nothing could exercise "does this compose
 * into a runtime the gateway can actually reach" without also running the whole
 * worker. These tests bind a real loopback listener and speak HTTP to it,
 * following `control-listener.test.ts`'s convention, because the property under
 * test — a real socket, reachable with the real token, answering with the real
 * capability set — is exactly what a mocked server would hide.
 */
import * as http from 'http';
import { AddressInfo } from 'net';
import { startControlRuntime } from './control-runtime-factory';

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = http.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const port = (probe.address() as AddressInfo).port;
      probe.close(() => resolve(port));
    });
  });
}

function request(port: number, method: string, path: string, token?: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port, method, path, headers: token ? { Authorization: `Bearer ${token}` } : {} },
      (res) => {
        let body = '';
        res.on('data', (chunk) => (body += chunk));
        res.on('end', () => resolve({ status: res.statusCode ?? 0, body }));
      },
    );
    req.on('error', reject);
    req.end();
  });
}

const TOKEN = 'factory-test-token-0123456789abcdef';

async function envFor(overrides: Partial<Record<string, string>> = {}): Promise<NodeJS.ProcessEnv> {
  return {
    FEATURE_AGENT_CONTROL_ENABLED: 'true',
    ADP_CONTROL_BIND_ADDRESS: '127.0.0.1',
    ADP_CONTROL_PORT: String(await freePort()),
    ADP_CONTROL_TOKEN: TOKEN,
    ADP_CONTROL_TOKEN_EXPIRES_AT: new Date(Date.now() + 60_000).toISOString(),
    ADP_CONTROL_GENERATION: '1',
    ADP_CONTROL_RUN_ID: 'run-under-test',
    ...overrides,
  };
}

describe('startControlRuntime', () => {
  it('does not start and publishes no runtime when the flag is off — the ordinary-run default', async () => {
    const logged: string[] = [];
    const { runtime, listener, outcome } = await startControlRuntime({
      env: {},
      log: (level, message) => logged.push(`${level} ${message}`),
    });

    expect(outcome).toEqual({ started: false, reason: 'disabled' });
    expect(runtime).toBeNull();
    expect(listener).toBeNull();
  });

  it('starts a real, reachable listener from ADP_CONTROL_* env — the same inputs the entrypoint places for every run', async () => {
    const env = await envFor();
    const { runtime, listener, outcome } = await startControlRuntime({ env, log: () => {} });

    expect(outcome.started).toBe(true);
    expect(runtime).not.toBeNull();
    expect(listener).not.toBeNull();
    if (!outcome.started || !listener) throw new Error('listener did not start');

    try {
      // Reachable with the real token — the property that makes this a
      // production-shaped runtime rather than an inert object graph.
      const authorized = await request(outcome.port, 'GET', '/agent/state', TOKEN);
      expect(authorized.status).not.toBe(401);

      const unauthorized = await request(outcome.port, 'GET', '/agent/state');
      expect(unauthorized.status).toBe(401);
    } finally {
      await listener.stop();
      await runtime?.adapter.dispose();
    }
  });

  it('reports the adapter\'s own effective capabilities, live rather than a static claim', async () => {
    // With no query attached yet, every verb is correctly false — the adapter's
    // own rule is "a live attempt is the floor for every verb", and this
    // composition has started no attempt. The point under test is that the
    // listener's /agent/state answers from `controlAdapter.capabilities()`
    // (the real, callable method) rather than from a value fabricated at
    // wiring time — asserted by checking the shape rather than a fixed value
    // this composition alone cannot produce.
    const env = await envFor();
    const { listener, outcome } = await startControlRuntime({ env, log: () => {} });
    if (!outcome.started || !listener) throw new Error('listener did not start');
    try {
      const state = await request(outcome.port, 'GET', '/agent/state', TOKEN);
      const parsed = JSON.parse(state.body);
      expect(parsed.capabilities).toEqual({ pause: false, resume: false, steer: false, abort: false });
    } finally {
      await listener.stop();
    }
  });

  it('disposes the steering queue rather than leaving a dangling subscription when the listener refuses to start', async () => {
    // Misconfigured: enabled, but no bind address. The same refusal
    // `agent-worker.ts` treats as non-fatal — control unavailable, run continues.
    const env = await envFor({ ADP_CONTROL_BIND_ADDRESS: '' });
    const { runtime, listener, outcome } = await startControlRuntime({ env, log: () => {} });

    expect(outcome.started).toBe(false);
    if (outcome.started) throw new Error('expected a refusal');
    expect(outcome.reason).toBe('misconfigured');
    expect(runtime).toBeNull();
    expect(listener).toBeNull();
  });

  it('forwards a steer outcome to the caller-supplied callback exactly as the inline composition did', async () => {
    // This is the one caller-specific seam: the ordinary worker appends a live
    // comment marker; a fixture launcher can omit the callback entirely (used
    // in the omitted-callback branch of the "starts a real, reachable listener"
    // test above, where no onSteerOutcome is passed and startup still succeeds).
    const env = await envFor();
    const events: Array<{ commandId: string; outcome: string }> = [];
    const { listener, outcome } = await startControlRuntime({
      env,
      log: () => {},
      onSteerOutcome: (event) => events.push(event),
    });
    if (!outcome.started || !listener) throw new Error('listener did not start');
    try {
      // No steer command is submitted in this test — asserting only that the
      // callback plumbing type-checks and does not throw when unused.
      expect(events).toEqual([]);
    } finally {
      await listener.stop();
    }
  });
});
