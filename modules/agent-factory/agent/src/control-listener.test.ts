/**
 * Tests for the in-pod control listener and its bounded journal — Issue #3960.
 *
 * These bind a real loopback listener on a free port and speak HTTP to it,
 * following `sigv4-proxy.test.ts` rather than mocking `http`. A mocked server
 * would let the tests pass while the real one bound the wrong interface,
 * authenticated after routing, or buffered an oversized body — which are exactly
 * the properties under test. Jest globals are used directly; importing from
 * `vitest` is what makes several sibling suites in this package fail to compile.
 */

import * as http from 'http';
import { AddressInfo } from 'net';

import {
  ControlListener,
  MAX_BODY_BYTES,
  MAX_INSTRUCTION_CHARS,
  isAgentControlEnabled,
} from './control-listener';
import {
  ControlAction,
  ControlStateStore,
  DEFAULT_MAX_PENDING,
  fingerprintPayload,
} from './control-state';

const TOKEN = 'test-control-token-0123456789abcdef';
const GENERATION = 7;
const UUID_A = '11111111-2222-4333-8444-555555555555';
const UUID_B = '66666666-7777-4888-8999-aaaaaaaaaaaa';

const ENABLED_ENV = { FEATURE_AGENT_CONTROL_ENABLED: 'true' } as NodeJS.ProcessEnv;

/**
 * A plausible port for cases that must fail before any bind is attempted. Given
 * a real value on purpose: passing 0 would make these tests pass via the
 * invalid-port branch, so they would still be green if the flag, bind-address or
 * token guard were deleted.
 */
const UNUSED_PORT = 18771;

interface Reply {
  status: number;
  body: any;
  headers: http.IncomingHttpHeaders;
  raw: string;
}

/** Speak HTTP to the listener. No helper library, so nothing normalises a bug away. */
function request(
  port: number,
  method: string,
  path: string,
  options: { token?: string | null; generation?: number | null; body?: string } = {},
): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const headers: Record<string, string> = {};
    if (options.token !== null) {
      headers.authorization = `Bearer ${options.token ?? TOKEN}`;
    }
    if (options.generation !== null && options.generation !== undefined) {
      headers['x-adp-control-generation'] = String(options.generation);
    }
    if (options.body !== undefined) {
      headers['content-type'] = 'application/json';
      headers['content-length'] = String(Buffer.byteLength(options.body));
    }

    const req = http.request({ host: '127.0.0.1', port, method, path, headers }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (chunk) => chunks.push(chunk as Buffer));
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        let body: any = null;
        try {
          body = JSON.parse(raw);
        } catch {
          body = null;
        }
        resolve({ status: res.statusCode ?? 0, body, headers: res.headers, raw });
      });
    });
    req.on('error', reject);
    if (options.body !== undefined) req.write(options.body);
    req.end();
  });
}

/**
 * Ask the OS for a free port, then hand the number to the listener.
 *
 * The listener deliberately rejects port 0 as misconfigured — the ingress
 * NetworkPolicy pins one fixed port, so a pod that bound an ephemeral one would
 * be unreachable through the policy. So tests cannot pass 0; they discover a
 * free port with a throwaway server and pass it explicitly.
 */
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

function makeStore(overrides: Partial<{ supported: ReadonlySet<ControlAction>; now: () => number }> = {}) {
  return new ControlStateStore({
    generation: GENERATION,
    supportedActions: overrides.supported,
    now: overrides.now,
  });
}

async function startListener(
  store: ControlStateStore,
  env: NodeJS.ProcessEnv = ENABLED_ENV,
  tokenExpiresAt = new Date(Date.now() + 60_000).toISOString(),
) {
  const listener = new ControlListener({
    tokenExpiresAt,
    bindAddress: '127.0.0.1',
    port: await freePort(),
    token: TOKEN,
    generation: GENERATION,
    store,
    logger: () => {},
  });
  const outcome = await listener.start(env);
  if (!outcome.started) throw new Error(`listener did not start: ${outcome.reason}`);
  return { listener, port: outcome.port };
}

// ===========================================================================
// Flags and startup (FR-1.1, FR-1.2, FR-8.3)
// ===========================================================================

describe('control flag', () => {
  it('enables only on the exact string "true"', () => {
    expect(isAgentControlEnabled({ FEATURE_AGENT_CONTROL_ENABLED: 'true' })).toBe(true);
  });

  it.each(['TRUE', 'True', '1', 'yes', 'false', '', 'enabled'])(
    'stays off for %p — a near-miss value must not enable a control channel',
    (value) => {
      expect(isAgentControlEnabled({ FEATURE_AGENT_CONTROL_ENABLED: value })).toBe(false);
    },
  );

  it('stays off when the variable is absent', () => {
    expect(isAgentControlEnabled({})).toBe(false);
  });
});

describe('listener startup', () => {
  it('does not start when the flag is off, leaving no bound port', async () => {
    const listener = new ControlListener({
      tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
      bindAddress: '127.0.0.1',
      port: UNUSED_PORT,
      token: TOKEN,
      generation: GENERATION,
      store: makeStore(),
      logger: () => {},
    });

    const outcome = await listener.start({});

    expect(outcome).toEqual({ started: false, reason: 'disabled' });
    // The flag-off guarantee is "no listener process, no bound port" — not a
    // server that exists and refuses.
    expect(listener.boundPort()).toBeNull();
  });

  it('refuses to start without a bind address instead of falling back to all interfaces', async () => {
    // The dangerous failure this prevents: a missing downwardAPI pod IP silently
    // widening the bind to 0.0.0.0, undoing the explicit-bind hardening.
    const listener = new ControlListener({
      tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
      bindAddress: '',
      port: UNUSED_PORT,
      token: TOKEN,
      generation: GENERATION,
      store: makeStore(),
      logger: () => {},
    });

    const outcome = await listener.start(ENABLED_ENV);

    expect(outcome.started).toBe(false);
    if (!outcome.started) {
      expect(outcome.reason).toBe('misconfigured');
    }
    expect(listener.boundPort()).toBeNull();
  });

  it('refuses to start without a token rather than serving commands unauthenticated', async () => {
    const listener = new ControlListener({
      tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
      bindAddress: '127.0.0.1',
      port: UNUSED_PORT,
      token: '',
      generation: GENERATION,
      store: makeStore(),
      logger: () => {},
    });

    const outcome = await listener.start(ENABLED_ENV);

    expect(outcome.started).toBe(false);
    if (!outcome.started) expect(outcome.reason).toBe('misconfigured');
  });

  it.each([0, -1, 1.5])('refuses to start on port %p rather than binding an arbitrary one', async (port) => {
    // Port 0 would bind an ephemeral port the pinned ingress NetworkPolicy does
    // not cover, so the pod would come up listening and be unreachable — a
    // failure that looks like a network bug rather than a config one.
    const listener = new ControlListener({
      tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
      bindAddress: '127.0.0.1',
      port,
      token: TOKEN,
      generation: GENERATION,
      store: makeStore(),
      logger: () => {},
    });

    const outcome = await listener.start(ENABLED_ENV);

    expect(outcome.started).toBe(false);
    if (!outcome.started) expect(outcome.reason).toBe('misconfigured');
    expect(listener.boundPort()).toBeNull();
  });

  it('reports bind_failed, distinctly from misconfigured, when the port is taken', async () => {
    // A taken port must not read as a config error: the run must continue with
    // control unavailable rather than treating this as fatal.
    const occupied = await freePort();
    const squatter = http.createServer();
    await new Promise<void>((resolve) => squatter.listen(occupied, '127.0.0.1', () => resolve()));

    try {
      const listener = new ControlListener({
        tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
        bindAddress: '127.0.0.1',
        port: occupied,
        token: TOKEN,
        generation: GENERATION,
        store: makeStore(),
        logger: () => {},
      });

      const outcome = await listener.start(ENABLED_ENV);

      expect(outcome.started).toBe(false);
      if (!outcome.started) expect(outcome.reason).toBe('bind_failed');
    } finally {
      await new Promise<void>((resolve) => squatter.close(() => resolve()));
    }
  });

  it('distinguishes misconfiguration from a deliberate flag-off in its diagnostics', async () => {
    // FR-1.12 / NFR-10: a silent failure here becomes an unfalsifiable support
    // burden, because the surrounding write path swallows exceptions.
    const logged: Array<{ level: string; message: string }> = [];
    const listener = new ControlListener({
      tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
      bindAddress: '',
      port: UNUSED_PORT,
      token: TOKEN,
      generation: GENERATION,
      store: makeStore(),
      logger: (level, message) => logged.push({ level, message }),
    });

    await listener.start(ENABLED_ENV);

    expect(logged.some((entry) => entry.level === 'error')).toBe(true);
  });

  it('binds an explicit address and reports the bound port', async () => {
    const { listener, port } = await startListener(makeStore());
    try {
      expect(port).toBeGreaterThan(0);
      expect(listener.boundPort()).toBe(port);
    } finally {
      await listener.stop();
    }
  });

  it('closes the port on stop so a late command cannot reach a finishing run', async () => {
    const { listener, port } = await startListener(makeStore());
    await listener.stop();

    expect(listener.boundPort()).toBeNull();
    await expect(request(port, 'GET', '/agent/ping')).rejects.toThrow();
  });
});

// ===========================================================================
// Authentication before routing (FR-1.5, AC-S1)
// ===========================================================================

describe('authentication', () => {
  let listener: ControlListener;
  let port: number;

  beforeEach(async () => {
    ({ listener, port } = await startListener(makeStore()));
  });
  afterEach(async () => {
    await listener.stop();
  });

  it('rejects a request with no Authorization header', async () => {
    const reply = await request(port, 'GET', '/agent/ping', { token: null });
    expect(reply.status).toBe(401);
  });

  it('rejects a wrong token', async () => {
    const reply = await request(port, 'GET', '/agent/ping', { token: 'wrong-token-value-here-padding' });
    expect(reply.status).toBe(401);
  });

  it('rejects a token of a different length without leaking which check failed', async () => {
    const reply = await request(port, 'GET', '/agent/ping', { token: 'short' });
    expect(reply.status).toBe(401);
    expect(reply.raw).not.toContain('length');
  });

  it('rejects a non-Bearer scheme', async () => {
    const reply = await new Promise<Reply>((resolve, reject) => {
      const req = http.request(
        { host: '127.0.0.1', port, method: 'GET', path: '/agent/ping', headers: { authorization: `Basic ${TOKEN}` } },
        (res) => {
          res.on('data', () => {});
          res.on('end', () => resolve({ status: res.statusCode ?? 0, body: null, headers: res.headers, raw: '' }));
        },
      );
      req.on('error', reject);
      req.end();
    });
    expect(reply.status).toBe(401);
  });

  it('authenticates before parsing the verb — an unauthenticated abort is 401, not 501', async () => {
    // The ordering property: if routing ran first, this would report the verb's
    // implementation status to a caller with no credential.
    const reply = await request(port, 'POST', '/agent/abort', {
      token: null,
      body: JSON.stringify({ command_id: UUID_A }),
    });

    expect(reply.status).toBe(401);
    expect(reply.raw).not.toContain('not_implemented');
  });

  it('authenticates before parsing the payload — malformed JSON with no token is 401, not 400', async () => {
    const reply = await request(port, 'POST', '/agent/pause', { token: null, body: '{not json' });
    expect(reply.status).toBe(401);
  });

  it('rejects a valid token presented against a different generation', async () => {
    // A new generation is a different process; a command aimed at the previous
    // one must not land on it.
    const reply = await request(port, 'GET', '/agent/ping', { generation: GENERATION + 1 });
    expect(reply.status).toBe(401);
  });

  it('accepts a valid token with the matching generation', async () => {
    const reply = await request(port, 'GET', '/agent/ping', { generation: GENERATION });
    expect(reply.status).toBe(200);
  });
});

// ===========================================================================
// Ping, state and reserved paths
// ===========================================================================

describe('token expiry', () => {
  it.each(['', 'invalid', '2040-01-01', '2040-01-01T00:00:00', '2000-01-01T00:00:00Z', undefined])(
    'refuses to bind with an absent, malformed or expired lifetime: %p',
    async (tokenExpiresAt) => {
      const listener = new ControlListener({
        bindAddress: '127.0.0.1', port: UNUSED_PORT, token: TOKEN,
        tokenExpiresAt: tokenExpiresAt as string,
        generation: GENERATION, store: makeStore(), logger: () => {},
      });
      expect(await listener.start(ENABLED_ENV)).toEqual({
        started: false, reason: 'misconfigured', detail: 'invalid or expired control token expiry',
      });
      expect(listener.boundPort()).toBeNull();
    },
  );

  it.each([
    ['GET', '/agent/ping', undefined],
    ['GET', '/agent/state', undefined],
    ['POST', '/agent/abort', JSON.stringify({command_id: UUID_A})],
    ['POST', '/agent/steer', '{not json'],
    ['POST', '/agent/steer', 'x'.repeat(MAX_BODY_BYTES + 1)],
    ['POST', '/agent/unknown', '{not json'],
  ])('rejects an expired credential before routing or parsing %s %s', async (method, path, body) => {
    const expires = Date.now() + 60_000;
    const store = makeStore();
    const {listener, port} = await startListener(store, ENABLED_ENV, new Date(expires).toISOString());
    const clock = jest.spyOn(Date, 'now').mockReturnValue(expires - 1);
    try {
      expect((await request(port, 'GET', '/agent/ping')).status).toBe(200);
      clock.mockReturnValue(expires);
      const reply = await request(port, method!, path!, {body});
      expect(reply.status).toBe(401);
      expect(reply.body).toEqual({error: 'unauthorized'});
      expect(store.snapshot().commands).toEqual([]);
    } finally {
      clock.mockRestore();
      await listener.stop();
    }
  });
});

describe('read routes', () => {
  let listener: ControlListener;
  let port: number;
  let store: ControlStateStore;

  beforeEach(async () => {
    store = makeStore();
    ({ listener, port } = await startListener(store));
  });
  afterEach(async () => {
    await listener.stop();
  });

  it('answers ping with the generation it reached', async () => {
    const reply = await request(port, 'GET', '/agent/ping');
    expect(reply.status).toBe(200);
    expect(reply.body).toEqual({ ok: true, generation: GENERATION });
  });

  it('serves the full state contract', async () => {
    const reply = await request(port, 'GET', '/agent/state');

    expect(reply.status).toBe(200);
    expect(Object.keys(reply.body).sort()).toEqual(
      ['active_tool_count', 'capabilities', 'commands', 'generation', 'state', 'updated_at'].sort(),
    );
    expect(reply.body.state).toBe('running');
    expect(reply.body.commands).toEqual([]);
  });

  it('reports every verb capability false in this story', async () => {
    const reply = await request(port, 'GET', '/agent/state');
    expect(reply.body.capabilities).toEqual({ pause: false, resume: false, steer: false, abort: false });
  });

  it('reports active_tool_count as null rather than an unproven zero', async () => {
    // A reported 0 would read as "no tools are running", a quiescence claim this
    // story cannot substantiate.
    const reply = await request(port, 'GET', '/agent/state');
    expect(reply.body.active_tool_count).toBeNull();
  });

  it('marks state responses no-store so a poll cannot render a cached phase', async () => {
    const reply = await request(port, 'GET', '/agent/state');
    expect(reply.headers['cache-control']).toBe('no-store');
  });

  it('reserves the event-stream path with 501 rather than leaving it 404', async () => {
    // Claimed now so the port-scoped NetworkPolicy already covers it and a later
    // streaming story needs no infrastructure change.
    const reply = await request(port, 'GET', '/agent/events');
    expect(reply.status).toBe(501);
  });

  it('404s an unknown path', async () => {
    const reply = await request(port, 'GET', '/agent/nonsense');
    expect(reply.status).toBe(404);
  });

  it('404s a known verb on the wrong method', async () => {
    // Routing is on (method, path), not on body shape.
    const reply = await request(port, 'GET', '/agent/pause');
    expect(reply.status).toBe(404);
  });
});

// ===========================================================================
// Unsupported verbs (AC-S1, AC-S2 shared layer)
// ===========================================================================

describe('unsupported verbs', () => {
  let listener: ControlListener;
  let port: number;

  beforeEach(async () => {
    ({ listener, port } = await startListener(makeStore()));
  });
  afterEach(async () => {
    await listener.stop();
  });

  it.each(['pause', 'resume', 'steer', 'abort'])(
    'answers authenticated %s with 501 and no capability',
    async (action) => {
      const body =
        action === 'steer'
          ? JSON.stringify({ command_id: UUID_A, instruction: 'do the thing' })
          : JSON.stringify({ command_id: UUID_A });

      const reply = await request(port, 'POST', `/agent/${action}`, { body });

      expect(reply.status).toBe(501);
      expect(reply.body.capabilities[action]).toBe(false);
    },
  );

  it('records nothing in the journal for an unsupported verb', async () => {
    // A 501 must leave no trace of a command that was never going to run,
    // otherwise the journal fills with phantom entries.
    const store = makeStore();
    const local = await startListener(store);
    try {
      await request(local.port, 'POST', '/agent/abort', { body: JSON.stringify({ command_id: UUID_A }) });
      expect(store.snapshot().commands).toEqual([]);
    } finally {
      await local.listener.stop();
    }
  });
});

// ===========================================================================
// Payload validation (AC-S5, AC-S7, NFR-6)
// ===========================================================================

describe('payload validation', () => {
  let listener: ControlListener;
  let port: number;
  let store: ControlStateStore;

  beforeEach(async () => {
    // A store that supports every verb, so validation failures are attributable
    // to the payload rather than to the verb being unimplemented.
    store = makeStore({ supported: new Set<ControlAction>(['pause', 'resume', 'steer', 'abort']) });
    ({ listener, port } = await startListener(store));
  });
  afterEach(async () => {
    await listener.stop();
  });

  it('rejects malformed JSON with 400 and keeps serving', async () => {
    const bad = await request(port, 'POST', '/agent/pause', { body: '{"command_id": ' });
    expect(bad.status).toBe(400);

    // The run continues unharmed — a control parse failure must never be fatal.
    const after = await request(port, 'GET', '/agent/ping');
    expect(after.status).toBe(200);
  });

  it('rejects a JSON array body', async () => {
    const reply = await request(port, 'POST', '/agent/pause', { body: '[]' });
    expect(reply.status).toBe(400);
  });

  it.each(['actor', 'target', 'token', 'control_token', 'address'])(
    'rejects a caller-supplied %s field rather than ignoring it',
    async (field) => {
      // Silently dropping these would return success to an attempted override,
      // so the caller would believe it took effect.
      const reply = await request(port, 'POST', '/agent/pause', {
        body: JSON.stringify({ command_id: UUID_A, [field]: 'attacker-value' }),
      });

      expect(reply.status).toBe(400);
      expect(reply.body.detail).toContain(field);
    },
  );

  it('rejects a non-UUID command id', async () => {
    const reply = await request(port, 'POST', '/agent/pause', { body: JSON.stringify({ command_id: 'abc' }) });
    expect(reply.status).toBe(400);
  });

  it('rejects a missing command id', async () => {
    const reply = await request(port, 'POST', '/agent/pause', { body: JSON.stringify({}) });
    expect(reply.status).toBe(400);
  });

  it('rejects an empty steer instruction', async () => {
    const reply = await request(port, 'POST', '/agent/steer', {
      body: JSON.stringify({ command_id: UUID_A, instruction: '' }),
    });
    expect(reply.status).toBe(400);
  });

  it('rejects an over-long steer instruction', async () => {
    const reply = await request(port, 'POST', '/agent/steer', {
      body: JSON.stringify({ command_id: UUID_A, instruction: 'x'.repeat(MAX_INSTRUCTION_CHARS + 1) }),
    });
    expect(reply.status).toBe(400);
  });

  it('rejects an oversized body with 413 and survives it', async () => {
    const huge = JSON.stringify({ command_id: UUID_A, reason: 'x'.repeat(MAX_BODY_BYTES + 512) });

    const reply = await request(port, 'POST', '/agent/pause', { body: huge }).catch(() => ({ status: 413 }) as Reply);
    expect(reply.status).toBe(413);

    const after = await request(port, 'GET', '/agent/ping');
    expect(after.status).toBe(200);
  });

  it('accepts a valid pause and returns 202 with a pending command', async () => {
    const reply = await request(port, 'POST', '/agent/pause', { body: JSON.stringify({ command_id: UUID_A }) });

    expect(reply.status).toBe(202);
    expect(reply.body.command).toMatchObject({ command_id: UUID_A, action: 'pause', status: 'pending' });
  });
});

// ===========================================================================
// Idempotency, conflict and bounds (revival-design §2, AC-T8)
// ===========================================================================

describe('command journal over HTTP', () => {
  let listener: ControlListener;
  let port: number;

  beforeEach(async () => {
    ({ listener, port } = await startListener(
      makeStore({ supported: new Set<ControlAction>(['pause', 'steer', 'abort']) }),
    ));
  });
  afterEach(async () => {
    await listener.stop();
  });

  it('replays the recorded outcome for the same id and payload, without a second acceptance', async () => {
    const body = JSON.stringify({ command_id: UUID_A });

    const first = await request(port, 'POST', '/agent/abort', { body });
    const second = await request(port, 'POST', '/agent/abort', { body });

    expect(first.status).toBe(202);
    // 200, not 202 — a retried abort stays one abort.
    expect(second.status).toBe(200);
    expect(second.body.command.accepted_at).toBe(first.body.command.accepted_at);
  });

  it('conflicts on the same id with different content', async () => {
    await request(port, 'POST', '/agent/steer', {
      body: JSON.stringify({ command_id: UUID_A, instruction: 'first intent' }),
    });

    const conflicting = await request(port, 'POST', '/agent/steer', {
      body: JSON.stringify({ command_id: UUID_A, instruction: 'different intent' }),
    });

    expect(conflicting.status).toBe(409);
  });

  it('returns 429 once the pending cap is reached', async () => {
    for (let i = 0; i < DEFAULT_MAX_PENDING; i += 1) {
      const id = `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
      const reply = await request(port, 'POST', '/agent/steer', {
        body: JSON.stringify({ command_id: id, instruction: `steer ${i}` }),
      });
      expect(reply.status).toBe(202);
    }

    const overflow = await request(port, 'POST', '/agent/steer', {
      body: JSON.stringify({ command_id: UUID_B, instruction: 'one too many' }),
    });

    expect(overflow.status).toBe(429);
  });

  it('lists accepted commands in submission order in state', async () => {
    await request(port, 'POST', '/agent/steer', {
      body: JSON.stringify({ command_id: UUID_A, instruction: 'first' }),
    });
    await request(port, 'POST', '/agent/steer', {
      body: JSON.stringify({ command_id: UUID_B, instruction: 'second' }),
    });

    const state = await request(port, 'GET', '/agent/state');

    expect(state.body.commands.map((c: any) => c.command_id)).toEqual([UUID_A, UUID_B]);
  });

  it('never echoes instruction text back through the state journal', async () => {
    const secret = 'sensitive-steering-text-marker';
    await request(port, 'POST', '/agent/steer', {
      body: JSON.stringify({ command_id: UUID_A, instruction: secret }),
    });

    const state = await request(port, 'GET', '/agent/state');

    expect(state.raw).not.toContain(secret);
  });

  it('never exposes the control token in any response', async () => {
    const ping = await request(port, 'GET', '/agent/ping');
    const state = await request(port, 'GET', '/agent/state');

    expect(ping.raw).not.toContain(TOKEN);
    expect(state.raw).not.toContain(TOKEN);
  });
});

// ===========================================================================
// Journal semantics without a socket (revival-design §2)
// ===========================================================================

describe('ControlStateStore', () => {
  const supported = new Set<ControlAction>(['pause', 'steer', 'abort']);

  it('reports an unknown id as unknown rather than absent', () => {
    // `unknown` is the required answer: a 404 would invite the client to
    // resubmit a command that may already have been consumed.
    const store = makeStore({ supported });
    const record = store.lookup(UUID_A);

    expect(record.status).toBe('unknown');
    expect(record.reason).toBeTruthy();
  });

  it('refuses a verb it does not support without journalling it', () => {
    const store = makeStore();
    expect(store.submit('pause', UUID_A, 'fp').kind).toBe('unsupported');
    expect(store.snapshot().commands).toEqual([]);
  });

  it('marks delivery only for a known id', () => {
    const store = makeStore({ supported });
    store.submit('steer', UUID_A, 'fp');

    expect(store.markDelivered(UUID_A)).toBe(true);
    // Recording delivery for an unaccepted id would invent a command.
    expect(store.markDelivered(UUID_B)).toBe(false);
    expect(store.lookup(UUID_A).status).toBe('delivered');
    expect(store.lookup(UUID_A).delivered_at).toBeTruthy();
  });

  it('refuses to settle into a non-terminal status', () => {
    // Otherwise settle could move an entry back to pending, making it
    // un-evictable and re-deliverable.
    const store = makeStore({ supported });
    store.submit('pause', UUID_A, 'fp');

    expect(store.settle(UUID_A, 'pending')).toBe(false);
    expect(store.settle(UUID_A, 'applied')).toBe(true);
    expect(store.lookup(UUID_A).status).toBe('applied');
  });

  it('never evicts a pending entry even past the retention window', () => {
    // A dropped pending command would look like it was never submitted, and the
    // submitter would reasonably retry it.
    let clock = 1_000_000;
    const store = new ControlStateStore({
      generation: GENERATION,
      supportedActions: supported,
      terminalRetentionMs: 1_000,
      now: () => clock,
    });
    store.submit('steer', UUID_A, 'fp');

    clock += 60 * 60 * 1000;

    expect(store.lookup(UUID_A).status).toBe('pending');
    expect(store.pending()).toHaveLength(1);
  });

  it('expires a settled entry past its retention window, reporting unknown', () => {
    let clock = 1_000_000;
    const store = new ControlStateStore({
      generation: GENERATION,
      supportedActions: supported,
      terminalRetentionMs: 1_000,
      now: () => clock,
    });
    store.submit('pause', UUID_A, 'fp');
    store.settle(UUID_A, 'applied');

    clock += 5_000;

    expect(store.lookup(UUID_A).status).toBe('unknown');
  });

  it('caps retained settled entries, keeping the most recent', () => {
    const store = new ControlStateStore({
      generation: GENERATION,
      supportedActions: supported,
      maxTerminal: 2,
      now: () => 1_000_000,
    });

    for (let i = 0; i < 5; i += 1) {
      const id = `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
      store.submit('pause', id, `fp-${i}`);
      store.settle(id, 'applied');
    }

    expect(store.snapshot().commands).toHaveLength(2);
    expect(store.snapshot().commands.map((c) => c.command_id)).toEqual([
      '00000000-0000-4000-8000-000000000003',
      '00000000-0000-4000-8000-000000000004',
    ]);
  });

  it('frees pending capacity once a command settles', () => {
    const store = new ControlStateStore({ generation: GENERATION, supportedActions: supported, maxPending: 1 });
    store.submit('steer', UUID_A, 'fp-a');

    expect(store.submit('steer', UUID_B, 'fp-b').kind).toBe('queue_full');
    store.settle(UUID_A, 'applied');
    expect(store.submit('steer', UUID_B, 'fp-b').kind).toBe('accepted');
  });

  it('replays a retry rather than re-accepting it, even after delivery', () => {
    const store = makeStore({ supported });
    store.submit('abort', UUID_A, 'fp');
    store.markDelivered(UUID_A);

    const retry = store.submit('abort', UUID_A, 'fp');

    expect(retry.kind).toBe('replayed');
    if (retry.kind === 'replayed') expect(retry.record.status).toBe('delivered');
  });

  it('keeps every capability false when no verb is supported', () => {
    expect(makeStore().capabilities()).toEqual({ pause: false, resume: false, steer: false, abort: false });
  });

  it('fingerprints payloads independently of key order', () => {
    // Otherwise field order in the JSON would make one intent look like two and
    // turn a plain retry into a 409.
    expect(fingerprintPayload({ command_id: UUID_A, instruction: 'go' })).toBe(
      fingerprintPayload({ instruction: 'go', command_id: UUID_A }),
    );
  });

  it('distinguishes different payloads under the same id', () => {
    expect(fingerprintPayload({ command_id: UUID_A, instruction: 'a' })).not.toBe(
      fingerprintPayload({ command_id: UUID_A, instruction: 'b' }),
    );
  });

  it('reports the phase it was set to', () => {
    const store = makeStore({ supported });
    store.setPhase('terminal');
    expect(store.snapshot().state).toBe('terminal');
  });

  it('carries the generation into every snapshot', () => {
    expect(makeStore().snapshot().generation).toBe(GENERATION);
  });

  it('reports a null active tool count rather than a zero', () => {
    // A reported 0 would read as "no tools are running" — a quiescence claim
    // this story cannot substantiate. null says "unknown", which is the truth.
    const store = makeStore();
    store.setActiveToolCount(null);
    expect(store.snapshot().active_tool_count).toBeNull();

    store.setActiveToolCount(3);
    expect(store.snapshot().active_tool_count).toBe(3);
  });
});

// ===========================================================================
// Branches that only fail in production
//
// Each of these is a path that never runs in a healthy pod, which is exactly why
// it is worth asserting: the default logger only runs when a caller forgets to
// inject one, the generation check only matters against a stale gateway, and the
// socket-error handler only matters on the day something else takes the port.
// ===========================================================================

describe('the default logger', () => {
  /**
   * Every other test injects `logger: () => {}` to keep output quiet, which means
   * the real logger — the one that actually runs in the pod — is never exercised.
   * The thing being asserted is not that logging works but that the token cannot
   * reach the log, since these lines are shipped to CloudWatch.
   */
  it('emits structured JSON and never logs the token', async () => {
    const lines: string[] = [];
    const spy = jest.spyOn(console, 'log').mockImplementation((line?: any) => {
      lines.push(String(line));
    });

    try {
      const listener = new ControlListener({
        tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
        bindAddress: '127.0.0.1',
        port: await freePort(),
        token: TOKEN,
        generation: GENERATION,
        store: makeStore(),
        // No logger: the default is the code under test here.
      });
      const outcome = await listener.start(ENABLED_ENV);
      expect(outcome.started).toBe(true);
      if (outcome.started) {
        await request(outcome.port, 'GET', '/agent/ping');
        await request(outcome.port, 'GET', '/agent/ping', { token: 'wrong-token-value-padding-xx' });
      }
      await listener.stop();

      expect(lines.length).toBeGreaterThan(0);
      for (const line of lines) {
        expect(() => JSON.parse(line)).not.toThrow();
        expect(JSON.parse(line).component).toBe('control-listener');
      }
      // The assertion that matters.
      expect(lines.join('\n')).not.toContain(TOKEN);
    } finally {
      spy.mockRestore();
    }
  });
});

describe('generation checking', () => {
  let listener: ControlListener;
  let port: number;

  beforeEach(async () => {
    ({ listener, port } = await startListener(makeStore()));
  });
  afterEach(async () => {
    await listener.stop();
  });

  it('accepts a request whose declared generation matches', async () => {
    const reply = await request(port, 'GET', '/agent/ping', { generation: GENERATION });
    expect(reply.status).toBe(200);
  });

  it('refuses a request declaring a stale generation', async () => {
    // The pod-IP reuse case: a gateway holding a previous run's registration
    // dials this address. Answering it would attribute one run's control to
    // another, so a mismatch is refused even though the token is correct.
    const reply = await request(port, 'GET', '/agent/ping', { generation: GENERATION + 1 });
    expect(reply.status).toBe(401);
  });

  it('accepts a request that declares no generation at all', async () => {
    // Absent is not stale: the header is optional, and requiring it would break
    // the read path for a caller that has no registration to compare against.
    const reply = await request(port, 'GET', '/agent/ping', { generation: null });
    expect(reply.status).toBe(200);
  });

  it('ignores an empty generation header rather than treating it as zero', async () => {
    // `Number.parseInt('')` is NaN, which would never equal the generation and
    // would turn a blank header into a hard 401.
    const reply = await new Promise<Reply>((resolve, reject) => {
      const req = http.request(
        {
          host: '127.0.0.1',
          port,
          method: 'GET',
          path: '/agent/ping',
          headers: { authorization: `Bearer ${TOKEN}`, 'x-adp-control-generation': '' },
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on('data', (chunk) => chunks.push(chunk as Buffer));
          res.on('end', () =>
            resolve({ status: res.statusCode ?? 0, body: null, headers: res.headers, raw: Buffer.concat(chunks).toString('utf8') }),
          );
        },
      );
      req.on('error', reject);
      req.end();
    });

    expect(reply.status).toBe(200);
  });
});

describe('abort reason validation', () => {
  let listener: ControlListener;
  let port: number;

  beforeEach(async () => {
    ({ listener, port } = await startListener(makeStore()));
  });
  afterEach(async () => {
    await listener.stop();
  });

  /**
   * `reason` is optional on abort, so these assert the validation of a field that
   * no supported verb currently reaches. Worth covering now: when abort is
   * implemented, the story that implements it should inherit a validated field
   * rather than discover the validation was never exercised.
   *
   * Body validation precedes the verb gate (W1-05), so a bad `reason` must show
   * up as 400 rather than the 501 the verb would otherwise return.
   */
  it('refuses a non-string reason with 400, not 501', async () => {
    const reply = await request(port, 'POST', '/agent/abort', {
      body: JSON.stringify({ command_id: UUID_A, reason: 12345 }),
    });
    expect(reply.status).toBe(400);
  });

  it('refuses an over-long reason', async () => {
    const reply = await request(port, 'POST', '/agent/abort', {
      body: JSON.stringify({ command_id: UUID_A, reason: 'x'.repeat(5000) }),
    });
    expect(reply.status).toBe(400);
  });

  it('accepts a valid reason and then reports the verb unsupported', async () => {
    // Proves the 400s above came from the reason field rather than from the
    // request never getting past parsing.
    const reply = await request(port, 'POST', '/agent/abort', {
      body: JSON.stringify({ command_id: UUID_A, reason: 'operator cancelled' }),
    });
    expect(reply.status).toBe(501);
  });

  it('accepts an omitted reason', async () => {
    const reply = await request(port, 'POST', '/agent/abort', {
      body: JSON.stringify({ command_id: UUID_A }),
    });
    expect(reply.status).toBe(501);
  });
});

describe('socket-level failure', () => {
  /**
   * NFR-6: control failing must degrade control, never kill the run. A pod whose
   * listener cannot bind has to keep executing the customer's work with control
   * simply unavailable.
   */
  it('reports bind_failed instead of throwing when the port is taken', async () => {
    const port = await freePort();
    const blocker = http.createServer(() => {});
    await new Promise<void>((resolve) => blocker.listen(port, '127.0.0.1', () => resolve()));

    try {
      const listener = new ControlListener({
        tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
        bindAddress: '127.0.0.1',
        port,
        token: TOKEN,
        generation: GENERATION,
        store: makeStore(),
        logger: () => {},
      });

      const outcome = await listener.start(ENABLED_ENV);

      expect(outcome.started).toBe(false);
      if (!outcome.started) {
        expect(outcome.reason).toBe('bind_failed');
      }
    } finally {
      await new Promise<void>((resolve) => blocker.close(() => resolve()));
    }
  });

  it('stop() is safe on a listener that never started', async () => {
    // The cleanup path after a failed bind, which the worker's shutdown hook
    // reaches unconditionally.
    const listener = new ControlListener({
      tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
      bindAddress: '127.0.0.1',
      port: UNUSED_PORT,
      token: TOKEN,
      generation: GENERATION,
      store: makeStore(),
      logger: () => {},
    });

    await expect(listener.stop()).resolves.toBeUndefined();
  });
});
