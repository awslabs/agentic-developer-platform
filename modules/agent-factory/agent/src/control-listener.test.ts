import { newAttemptId } from './control-runtime';
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
import { PauseGate } from './pause-gate';
import { ClaudeControlAdapter } from './harnesses/claude-control';
import { applyControlCommand, bindRuntimeTransitionsToStore } from './control-command-apply';
import { AddressInfo } from 'net';
import { generateKeyPairSync, sign as cryptoSign, createHash, type KeyObject } from 'crypto';
import { mkdtempSync, writeFileSync, renameSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

import {
  ControlListener,
  ENVELOPE_HEADER,
  MAX_BODY_BYTES,
  MAX_INSTRUCTION_CHARS,
  isAgentControlEnabled,
} from './control-listener';
import { ENVELOPE_AUDIENCE, ENVELOPE_ISSUER, ENVELOPE_VERSION } from './control-envelope';
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
const RUN_ID = 'run-under-test-001';
const KEY_ID = 'listener-test-key';

/**
 * A stand-in for the gateway's signing key — Issue #5028.
 *
 * Generated per test process and never leaves it. This is the *only* place in the
 * worker package that signs an envelope, and it exists solely so these tests can
 * produce authorized requests. Production has no counterpart: `control-envelope.ts`
 * verifies and cannot sign, which `control-envelope.test.ts` asserts directly.
 */
const GATEWAY_KEYS = generateKeyPairSync('ed25519');

/** The public half, in the shape the listener config takes. */
const ENVELOPE_KEYS: Map<string, KeyObject> = new Map([[KEY_ID, GATEWAY_KEYS.publicKey]]);

function isoSecond(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/**
 * Sign an envelope the way the gateway would.
 *
 * Every field is overridable so a test can produce a *validly signed* envelope
 * with wrong claims — a stale generation, another run's id, a digest of a
 * different body. Without that, a test named "refuses a stale generation" would
 * pass because the signature failed, proving nothing about the generation check.
 */
function signEnvelope(
  overrides: Record<string, unknown> = {},
  signingKey: KeyObject = GATEWAY_KEYS.privateKey,
): string {
  const now = Date.now();
  const payload: Record<string, unknown> = {
    v: ENVELOPE_VERSION,
    iss: ENVELOPE_ISSUER,
    aud: ENVELOPE_AUDIENCE,
    alg: 'ed25519',
    kid: KEY_ID,
    tenant_id: 'org-tenant-001',
    principal: 'inv-coordinator#1',
    target_run_id: RUN_ID,
    target_generation: GENERATION,
    action: 'pause',
    command_id: UUID_A,
    body_digest: createHash('sha256').update(Buffer.from('{}', 'utf8')).digest('hex'),
    grant_id: 'grant-coordinator-1',
    revocation_epoch: 1,
    iat: isoSecond(now),
    nbf: isoSecond(now),
    exp: isoSecond(now + 30_000),
    flow_id: 'flow-42',
    authority_reference_id: 'decision-abc',
    ...overrides,
  };
  const body = Buffer.from(JSON.stringify(payload), 'utf8');
  const signature = cryptoSign(null, Buffer.concat([Buffer.from(`${ENVELOPE_VERSION}.`), body]), signingKey);
  return `${ENVELOPE_VERSION}.${body.toString('base64url')}.${signature.toString('base64url')}`;
}

/**
 * The envelope a legitimate gateway would send for this exact request.
 *
 * Takes the body it will accompany, so the digest is over the same bytes the
 * request writes — which is the whole binding.
 */
function envelopeFor(action: ControlAction, commandId: string, body: string, overrides: Record<string, unknown> = {}): string {
  return signEnvelope({
    action,
    command_id: commandId,
    body_digest: createHash('sha256').update(Buffer.from(body, 'utf8')).digest('hex'),
    ...overrides,
  });
}

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
  options: { token?: string | null; generation?: number | null; body?: string; envelope?: string } = {},
): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const headers: Record<string, string> = {};
    if (options.token !== null) {
      headers.authorization = `Bearer ${options.token ?? TOKEN}`;
    }
    if (options.generation !== null && options.generation !== undefined) {
      headers['x-adp-control-generation'] = String(options.generation);
    }
    if (options.envelope !== undefined) {
      headers[ENVELOPE_HEADER] = options.envelope;
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
  // Issue #5028: defaults to a correctly-configured listener, so envelope
  // *misconfiguration* has to be asked for explicitly rather than being the
  // accidental state of every test.
  envelopeConfig: { runId?: string; envelopeKeys?: Map<string, KeyObject>; credentialFile?: string; envelopeKeysFile?: string } = {
    runId: RUN_ID,
    envelopeKeys: ENVELOPE_KEYS,
  },
) {
  const listener = new ControlListener({
    tokenExpiresAt,
    bindAddress: '127.0.0.1',
    port: await freePort(),
    token: TOKEN,
    generation: GENERATION,
    store,
    logger: () => {},
    ...envelopeConfig,
  });
  const outcome = await listener.start(env);
  if (!outcome.started) throw new Error(`listener did not start: ${outcome.reason}`);
  return { listener, port: outcome.port };
}

test('HTTP admission retains the exact proof for online delivery and blocks revoked work', async () => {
  let allowed = false;
  const revalidate = jest.fn(async () => allowed);
  const store = new ControlStateStore({ generation: GENERATION, supportedActions: new Set(['pause']), revalidate });
  const { listener, port } = await startListener(store);
  try {
    const body = JSON.stringify({ command_id: UUID_A });
    const envelope = envelopeFor('pause', UUID_A, body);
    const reply = await request(port, 'POST', '/agent/pause', { body, envelope });
    expect(reply.status).toBe(202);
    const effect = jest.fn();
    expect(store.markDelivered(UUID_A)).toBe(false);
    expect(await store.deliverAuthorized(UUID_A, effect)).toBe(false);
    expect(revalidate).toHaveBeenCalledWith({ envelope, action: 'pause', command_id: UUID_A,
      body_base64: Buffer.from(body).toString('base64'), principal: 'inv-coordinator#1',
      authorityKind: 'delegated_grant' }, GENERATION);
    expect(effect).not.toHaveBeenCalled();
    expect(store.lookup(UUID_A).status).toBe('rejected');
    allowed = true;
    expect(await store.deliverAuthorized(UUID_A, effect)).toBe(false);
  } finally { await listener.stop(); }
});

test('signed pause and resume preserve acceptance order across delayed revalidation', async () => {
  let releasePause!: (allowed: boolean) => void;
  const slowPause = new Promise<boolean>((resolve) => { releasePause = resolve; });
  const checked: string[] = [];
  const handedOff: string[] = [];
  const gate = new PauseGate({ settleTimeoutMs: 2_000 });
  const work = await gate.admit('Write');
  const store = new ControlStateStore({
    generation: GENERATION, supportedActions: new Set(['pause', 'resume']),
    revalidate: async (proof) => {
      checked.push(proof.action);
      return proof.action === 'pause' ? slowPause : true;
    },
  });
  const unitAttempt = newAttemptId();
  const unbind = bindRuntimeTransitionsToStore({ adapter: {
    currentAttempt: () => unitAttempt,
    activeWorkCount: () => gate.activeToolCount(),
    subscribe: (listener) => gate.subscribe(event => listener({ ...event, attemptId: unitAttempt })),
    isCancelled: () => false,
  }, store });
  const listener = new ControlListener({
    bindAddress: '127.0.0.1', port: await freePort(), token: TOKEN,
    tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
    generation: GENERATION, runId: RUN_ID, envelopeKeys: ENVELOPE_KEYS,
    store, logger: () => {},
    executor: async (action, commandId) => {
      handedOff.push(action);
      await applyControlCommand({ action, commandId, store, adapter: {
        requestPause: (options) => gate.requestPause(options),
        resumeFromPause: async () => { await gate.resume(); },
        // Unused: this case drives pause/resume only. Abort's own executor
        // behaviour is covered in `agent-worker-abort.test.ts` (#3963).
        cancel: () => {},
      } });
    },
  });
  const started = await listener.start(ENABLED_ENV);
  if (!started.started) throw new Error('listener failed');
  try {
    for (const [action, commandId] of [['pause', UUID_A], ['resume', UUID_B]] as const) {
      const body = JSON.stringify({ command_id: commandId });
      expect((await request(started.port, 'POST', `/agent/${action}`, {
        body, envelope: envelopeFor(action, commandId, body),
      })).status).toBe(202);
    }
    // The later proof would complete immediately if allowed to overtake pause.
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(checked).toEqual(['pause']);
    expect(handedOff).toEqual([]);
    releasePause(true);
    for (let i = 0; i < 100 && store.lookup(UUID_B).status !== 'applied'; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    expect(handedOff).toEqual(['pause', 'resume']);
    expect(gate.currentPhase()).toBe('running');
    expect(store.snapshot().state).toBe('running');
    expect(store.lookup(UUID_A).status).toBe('cancelled');
    expect(store.lookup(UUID_B).status).toBe('applied');
    expect(gate.activeToolCount()).toBe(1); // resume did not wait for tool settlement
  } finally {
    releasePause(false);
    gate.settle(work.ticket);
    gate.cancel();
    unbind();
    await listener.stop();
  }
});

describe('live credential rotation', () => {
  it.each(['missing', 'empty', 'unreadable', 'no-run-id'])('withholds controls for %s verification state', async (condition) => {
    const directory = mkdtempSync(join(tmpdir(), 'adp-control-readiness-'));
    const path = join(directory, 'keys.json');
    if (condition !== 'missing') writeFileSync(path, condition === 'empty' ? '{}' : JSON.stringify({
      [KEY_ID]: GATEWAY_KEYS.publicKey.export({ format: 'pem', type: 'spki' }),
    }));
    const store = makeStore({ supported: new Set<ControlAction>(['pause', 'resume']) });
    const { listener, port } = await startListener(store, ENABLED_ENV, isoSecond(Date.now() + 3600_000), {
      runId: condition === 'no-run-id' ? '' : RUN_ID,
      envelopeKeys: ENVELOPE_KEYS,
      // A directory is reliably unreadable as a key file, including under root.
      envelopeKeysFile: condition === 'unreadable' ? directory : path,
    });
    try {
      const state = (await request(port, 'GET', '/agent/state')).body;
      expect(state.capabilities.pause).toBe(false);
      expect(state.capabilities.resume).toBe(false);
      expect(state.verification_key_ids).toEqual([]);
    } finally {
      await listener.stop();
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it('reloads staged and retired public keys while preserving recorded command outcomes', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'adp-control-keys-'));
    const path = join(directory, 'keys.json');
    const next = generateKeyPairSync('ed25519');
    const oldPem = GATEWAY_KEYS.publicKey.export({ format: 'pem', type: 'spki' });
    const nextPem = next.publicKey.export({ format: 'pem', type: 'spki' });
    const write = (keys: Record<string, unknown>) => {
      writeFileSync(path + '.next', JSON.stringify(keys));
      renameSync(path + '.next', path);
    };
    write({ [KEY_ID]: oldPem });
    const store = makeStore({ supported: new Set<ControlAction>(['pause']) });
    const { listener, port } = await startListener(store, ENABLED_ENV, isoSecond(Date.now() + 3600_000), {
      runId: RUN_ID, envelopeKeys: ENVELOPE_KEYS, envelopeKeysFile: path,
    });
    const body = JSON.stringify({ command_id: UUID_A });
    const oldProof = envelopeFor('pause', UUID_A, body);
    const nextProof = signEnvelope({ kid: 'next', body_digest: createHash('sha256').update(body).digest('hex') }, next.privateKey);
    try {
      expect((await request(port, 'POST', '/agent/pause', { body, envelope: oldProof })).status).toBe(202);
      expect((await request(port, 'POST', '/agent/pause', { body, envelope: nextProof })).status).toBe(403);
      write({ [KEY_ID]: oldPem, next: nextPem });
      const ping = await request(port, 'GET', '/agent/ping');
      expect(ping.body.verification_key_ids).toEqual([KEY_ID, 'next'].sort());
      const staged = (await request(port, 'GET', '/agent/state')).body;
      expect(staged.verification_key_ids).toEqual([KEY_ID, 'next'].sort());
      expect(staged.capabilities.pause).toBe(true);
      expect((await request(port, 'POST', '/agent/pause', { body, envelope: nextProof })).status).toBe(200);
      write({ next: nextPem });
      expect((await request(port, 'GET', '/agent/state')).body.verification_key_ids).toEqual(['next']);
      expect((await request(port, 'POST', '/agent/pause', { body, envelope: oldProof })).status).toBe(403);
      expect((await request(port, 'POST', '/agent/pause', { body, envelope: nextProof })).status).toBe(200);
      rmSync(path);
      expect((await request(port, 'POST', '/agent/pause', { body, envelope: oldProof })).status).toBe(403);
      expect((await request(port, 'GET', '/agent/ping')).body.verification_key_ids).toEqual([]);
      expect((await request(port, 'GET', '/agent/state')).body.capabilities.pause).toBe(false);
    } finally {
      await listener.stop();
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it('reloads tokens on the same socket and preserves the journal with bounded overlap', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'adp-control-rotation-'));
    const path = join(directory, 'lease.json');
    const initial = Date.now();
    const clock = jest.spyOn(Date, 'now').mockReturnValue(initial);
    const oldToken = { token: TOKEN, epoch: 1, expires_at: isoSecond(initial + 60_000) };
    const nextToken = { token: 'next-control-token-0123456789abcdef', epoch: 2, expires_at: isoSecond(initial + 3600_000) };
    const write = (extra: Record<string, unknown>) => {
      writeFileSync(path + '.next', JSON.stringify({ version: 1, run_id: RUN_ID, generation: GENERATION, ...extra }), { mode: 0o600 });
      renameSync(path + '.next', path);
    };
    write({ current: oldToken });
    const store = makeStore({ supported: new Set<ControlAction>(['pause']) });
    store.submit('pause', UUID_A, 'existing-command');
    const { listener, port } = await startListener(store, ENABLED_ENV, isoSecond(initial + 60_000), {
      runId: RUN_ID, envelopeKeys: ENVELOPE_KEYS, credentialFile: path,
    });
    try {
      expect((await request(port, 'GET', '/agent/state')).status).toBe(200);
      write({ current: nextToken, previous: { ...oldToken, valid_until: isoSecond(initial + 30_000) }, staged_at: isoSecond(initial) });
      expect((await request(port, 'GET', '/agent/state', { token: nextToken.token })).status).toBe(200);
      expect((await request(port, 'GET', '/agent/state')).status).toBe(200);
      clock.mockReturnValue(initial + 31_000);
      expect((await request(port, 'GET', '/agent/state')).status).toBe(401);
      clock.mockReturnValue(initial + 61_000);
      expect((await request(port, 'GET', '/agent/state', { token: nextToken.token })).status).toBe(200);
      expect(store.submit('pause', UUID_A, 'existing-command').kind).toBe('replayed');
      expect(listener.boundPort()).toBe(port);
      write({ current: oldToken });
      expect((await request(port, 'GET', '/agent/state')).status).toBe(401);
      rmSync(path);
      expect((await request(port, 'GET', '/agent/state', { token: nextToken.token })).status).toBe(401);
    } finally {
      await listener.stop();
      clock.mockRestore();
      rmSync(directory, { recursive: true, force: true });
    }
  });
});

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
      ['active_tool_count', 'capabilities', 'commands', 'generation', 'state', 'updated_at', 'verification_key_ids'].sort(),
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
    const body = JSON.stringify({ command_id: UUID_A });
    const reply = await request(port, 'POST', '/agent/pause', {
      body,
      // This store supports pause, so #5028 requires an envelope. Supplying a
      // legitimate one keeps this test about payload validation rather than
      // silently converting it into an authorization test.
      envelope: envelopeFor('pause', UUID_A, body),
    });

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
      makeStore({ supported: new Set<ControlAction>(['pause', 'resume', 'steer', 'abort']) }),
    ));
  });
  afterEach(async () => {
    await listener.stop();
  });

  it('replays the recorded outcome for the same id and payload, without a second acceptance', async () => {
    const body = JSON.stringify({ command_id: UUID_A });
    // Both attempts carry their own freshly-signed envelope, which is what a
    // retrying gateway does — the envelope is per-request and short-lived, so a
    // retry re-authorizes rather than reusing the first authorization. The
    // *journal* is what makes the retry idempotent, and that is what this asserts.
    const first = await request(port, 'POST', '/agent/abort', { body, envelope: envelopeFor('abort', UUID_A, body) });
    const second = await request(port, 'POST', '/agent/abort', { body, envelope: envelopeFor('abort', UUID_A, body) });

    expect(first.status).toBe(202);
    // 200, not 202 — a retried abort stays one abort.
    expect(second.status).toBe(200);
    expect(second.body.command.accepted_at).toBe(first.body.command.accepted_at);
  });

  it('conflicts when the same command id is reused for another action with identical content', async () => {
    const body = JSON.stringify({ command_id: UUID_A });
    const pause = await request(port, 'POST', '/agent/pause', { body, envelope: envelopeFor('pause', UUID_A, body) });
    const resume = await request(port, 'POST', '/agent/resume', { body, envelope: envelopeFor('resume', UUID_A, body) });
    expect(pause.status).toBe(202);
    expect(resume.status).toBe(409);
    expect(resume.body).toEqual({ error: 'command_id_conflict' });
    const status = await request(port, 'POST', '/agent/pause', { body, envelope: envelopeFor('pause', UUID_A, body) });
    expect(status.body.command.action).toBe('pause');
  });

  it('conflicts on the same id with different content', async () => {
    const firstBody = JSON.stringify({ command_id: UUID_A, instruction: 'first intent' });
    await request(port, 'POST', '/agent/steer', {
      body: firstBody,
      envelope: envelopeFor('steer', UUID_A, firstBody),
    });

    // A genuinely authorized second command — same id, different instruction. The
    // 409 must come from the journal, not from the envelope: an authorized caller
    // reusing an id is a conflict, and reporting it as an authorization failure
    // would send them to debug the wrong thing.
    const conflictingBody = JSON.stringify({ command_id: UUID_A, instruction: 'different intent' });
    const conflicting = await request(port, 'POST', '/agent/steer', {
      body: conflictingBody,
      envelope: envelopeFor('steer', UUID_A, conflictingBody),
    });

    expect(conflicting.status).toBe(409);
  });

  it('returns 429 once the pending cap is reached', async () => {
    for (let i = 0; i < DEFAULT_MAX_PENDING; i += 1) {
      const id = `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
      const body = JSON.stringify({ command_id: id, instruction: `steer ${i}` });
      const reply = await request(port, 'POST', '/agent/steer', {
        body,
        envelope: envelopeFor('steer', id, body),
      });
      expect(reply.status).toBe(202);
    }

    const overflowBody = JSON.stringify({ command_id: UUID_B, instruction: 'one too many' });
    const overflow = await request(port, 'POST', '/agent/steer', {
      body: overflowBody,
      envelope: envelopeFor('steer', UUID_B, overflowBody),
    });

    expect(overflow.status).toBe(429);
  });

  it('lists accepted commands in submission order in state', async () => {
    const firstBody = JSON.stringify({ command_id: UUID_A, instruction: 'first' });
    const secondBody = JSON.stringify({ command_id: UUID_B, instruction: 'second' });
    await request(port, 'POST', '/agent/steer', { body: firstBody, envelope: envelopeFor('steer', UUID_A, firstBody) });
    await request(port, 'POST', '/agent/steer', { body: secondBody, envelope: envelopeFor('steer', UUID_B, secondBody) });

    const state = await request(port, 'GET', '/agent/state');

    expect(state.body.commands.map((c: any) => c.command_id)).toEqual([UUID_A, UUID_B]);
  });

  it('never echoes instruction text back through the state journal', async () => {
    const secret = 'sensitive-steering-text-marker';
    const body = JSON.stringify({ command_id: UUID_A, instruction: secret });
    // The envelope is essential to this test rather than incidental. Without one
    // the command is refused, nothing is journaled, and the assertion below passes
    // because there is no journal entry at all — a green test proving nothing
    // about redaction. Asserting the 202 first pins that the entry really exists.
    const accepted = await request(port, 'POST', '/agent/steer', {
      body,
      envelope: envelopeFor('steer', UUID_A, body),
    });
    expect(accepted.status).toBe(202);

    const state = await request(port, 'GET', '/agent/state');

    expect(state.body.commands).toHaveLength(1);
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

// ===========================================================================
// Gateway-signed command authorization over a real socket — Issue #5028 (AC5)
// ===========================================================================

/**
 * `control-envelope.test.ts` proves the verifier. This proves the *listener*
 * enforces it: over a bound socket, with a real bearer token, at the point where a
 * command would enter the journal.
 *
 * The distinction matters because every test here holds a valid pod token. That is
 * the threat model of AC2 carried through to the listener — a worker that read
 * another run's DynamoDB row has the token and satisfies `authenticate()`
 * completely. The envelope is the only thing between it and another run's journal.
 */
describe('command authorization envelope', () => {
  /** A store that supports the verbs, so the envelope gate is actually reached. */
  const supported = () => makeStore({ supported: new Set<ControlAction>(['pause', 'steer', 'abort']) });

  let listener: ControlListener;
  let port: number;
  let store: ControlStateStore;

  beforeEach(async () => {
    store = supported();
    ({ listener, port } = await startListener(store));
  });
  afterEach(async () => {
    await listener.stop();
  });

  const PAUSE_BODY = JSON.stringify({ command_id: UUID_A });

  async function pause(options: { envelope?: string; body?: string } = {}) {
    const body = options.body ?? PAUSE_BODY;
    return request(port, 'POST', '/agent/pause', { body, envelope: options.envelope });
  }

  it('accepts a correctly authorized command', async () => {
    // The positive case first: the refusals below would be worthless if the
    // authorized path did not work, and a suite of only refusals is satisfied by
    // a listener that refuses everything.
    const reply = await pause({ envelope: envelopeFor('pause', UUID_A, PAUSE_BODY) });

    expect(reply.status).toBe(202);
    expect(store.snapshot().commands).toHaveLength(1);
  });

  it('refuses a command with no envelope at all', async () => {
    const reply = await pause();

    expect(reply.status).toBe(403);
    // Nothing journaled. A refused command that still left a record would let an
    // unauthorized caller fill the journal and reach the pending cap, denying
    // control to the legitimate operator.
    expect(store.snapshot().commands).toEqual([]);
  });

  it('refuses a forged envelope signed with a key the pod does not trust', async () => {
    // The compromised-worker case: it generates its own key pair and signs a
    // perfectly-shaped envelope for a command it wants to run.
    const attacker = generateKeyPairSync('ed25519');
    const forged = envelopeFor('pause', UUID_A, PAUSE_BODY);
    const reforged = signEnvelope(
      { body_digest: createHash('sha256').update(Buffer.from(PAUSE_BODY, 'utf8')).digest('hex') },
      attacker.privateKey,
    );

    expect((await pause({ envelope: reforged })).status).toBe(403);
    // Sanity: the same claims signed by the real key are accepted, so the refusal
    // above is attributable to the signature and not to the claims.
    expect((await pause({ envelope: forged })).status).toBe(202);
  });

  it('refuses an envelope naming a different run', async () => {
    const envelope = envelopeFor('pause', UUID_A, PAUSE_BODY, { target_run_id: 'run-somebody-elses' });

    expect((await pause({ envelope })).status).toBe(403);
    expect(store.snapshot().commands).toEqual([]);
  });

  it('refuses an envelope bound to a previous generation of this run', async () => {
    // Pod-IP reuse and restart: an envelope authorized against the previous
    // generation must not land on the process that replaced it.
    const envelope = envelopeFor('pause', UUID_A, PAUSE_BODY, { target_generation: GENERATION - 1 });

    expect((await pause({ envelope })).status).toBe(403);
  });

  it('refuses an envelope for a different action presented at this path', async () => {
    // An authorized pause replayed at /agent/abort. Same run, same generation,
    // same command id, valid signature — only the verb differs.
    const envelope = envelopeFor('pause', UUID_A, PAUSE_BODY);
    const reply = await request(port, 'POST', '/agent/abort', { body: PAUSE_BODY, envelope });

    expect(reply.status).toBe(403);
  });

  it('refuses an envelope whose command id does not match the body', async () => {
    const envelope = envelopeFor('pause', UUID_B, PAUSE_BODY);

    expect((await pause({ envelope })).status).toBe(403);
  });

  it('refuses a body changed after authorization', async () => {
    // AC5's changed-body case. The envelope authorizes one instruction; the
    // request carries another. The digest is over raw bytes, so this cannot pass.
    const authorized = JSON.stringify({ command_id: UUID_A, instruction: 'summarize the findings' });
    const substituted = JSON.stringify({ command_id: UUID_A, instruction: 'push directly to main' });
    const envelope = envelopeFor('steer', UUID_A, authorized);

    const reply = await request(port, 'POST', '/agent/steer', { body: substituted, envelope });

    expect(reply.status).toBe(403);
    expect(store.snapshot().commands).toEqual([]);
  });

  it('refuses an expired envelope even though the pod token is still valid', async () => {
    // The two lifetimes are independent: a run-long token must not extend a
    // 30-second authorization.
    const past = Date.now() - 120_000;
    const envelope = envelopeFor('pause', UUID_A, PAUSE_BODY, {
      iat: isoSecond(past),
      nbf: isoSecond(past),
      exp: isoSecond(past + 30_000),
    });

    expect((await pause({ envelope })).status).toBe(403);
  });

  it('refuses an envelope claiming a longer life than policy allows', async () => {
    const now = Date.now();
    const envelope = envelopeFor('pause', UUID_A, PAUSE_BODY, {
      iat: isoSecond(now),
      nbf: isoSecond(now),
      exp: isoSecond(now + 6 * 60 * 60 * 1000),
    });

    expect((await pause({ envelope })).status).toBe(403);
  });

  it('does not double-accept an envelope replayed for a command already journaled', async () => {
    // AC5's replay case. The first request is authorized and accepted; the second
    // presents the identical envelope again. It is refused rather than replayed,
    // because the envelope's own validity window is the only thing that could
    // have permitted it and it was already spent on the first command.
    const envelope = envelopeFor('pause', UUID_A, PAUSE_BODY);
    const first = await pause({ envelope });
    expect(first.status).toBe(202);

    const replayed = await pause({ envelope });

    // 200 from the journal's idempotency path is the *correct* answer here: the
    // envelope is still within its window and re-authorizes the same command, and
    // the journal recognises it as the one already recorded. What must not happen
    // is a second acceptance.
    expect(replayed.status).toBe(200);
    expect(store.snapshot().commands).toHaveLength(1);
  });

  it('refuses an envelope that carries no signature at all', async () => {
    const [version, body] = envelopeFor('pause', UUID_A, PAUSE_BODY).split('.');

    expect((await pause({ envelope: `${version}.${body}.` })).status).toBe(403);
  });

  it('never reveals why an envelope was refused', async () => {
    // A caller able to distinguish "wrong run" from "bad signature" learns whether
    // the run it just named exists. All refusals must be one opaque answer.
    const wrongRun = await pause({ envelope: envelopeFor('pause', UUID_A, PAUSE_BODY, { target_run_id: 'run-probe' }) });
    const badSignature = await pause({
      envelope: signEnvelope(
        { body_digest: createHash('sha256').update(Buffer.from(PAUSE_BODY, 'utf8')).digest('hex') },
        generateKeyPairSync('ed25519').privateKey,
      ),
    });

    expect(wrongRun.status).toBe(badSignature.status);
    expect(wrongRun.body).toEqual(badSignature.body);
  });

  it('records the authorizing grant without the envelope or the token', async () => {
    // AC7: the decision must be auditable and must contain no secret.
    const logged: Array<{ level: string; message: string; context?: Record<string, unknown> }> = [];
    const local = new ControlListener({
      tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
      bindAddress: '127.0.0.1',
      port: await freePort(),
      token: TOKEN,
      generation: GENERATION,
      store: supported(),
      runId: RUN_ID,
      envelopeKeys: ENVELOPE_KEYS,
      logger: (level, message, context) => logged.push({ level, message, context }),
    });
    const outcome = await local.start(ENABLED_ENV);
    if (!outcome.started) throw new Error('listener did not start');

    try {
      const envelope = envelopeFor('pause', UUID_A, PAUSE_BODY);
      await request(outcome.port, 'POST', '/agent/pause', { body: PAUSE_BODY, envelope });

      const authorized = logged.find((entry) => entry.message === 'control command authorized');
      expect(authorized?.context).toMatchObject({
        principal: 'inv-coordinator#1',
        grant_id: 'grant-coordinator-1',
        authority_reference_id: 'decision-abc',
      });

      const serialized = JSON.stringify(logged);
      expect(serialized).not.toContain(TOKEN);
      expect(serialized).not.toContain(envelope);
    } finally {
      await local.stop();
    }
  });
});

describe('envelope enforcement boundaries', () => {
  const supportedSet = new Set<ControlAction>(['pause']);

  it('answers an unsupported verb with 501 rather than demanding an envelope', async () => {
    // The ordering choice in `requiresEnvelope`. A build where the verb does not
    // exist must say so; answering 403 would send an operator to debug key
    // distribution for a feature that was never implemented.
    const { listener, port } = await startListener(makeStore());
    try {
      const reply = await request(port, 'POST', '/agent/abort', { body: JSON.stringify({ command_id: UUID_A }) });
      expect(reply.status).toBe(501);
    } finally {
      await listener.stop();
    }
  });

  it('still rejects a malformed body before considering authorization', async () => {
    // Validation precedes the envelope check, so a caller with no envelope and a
    // broken body gets the actionable 400 rather than a 403 that hides the typo.
    const { listener, port } = await startListener(makeStore({ supported: supportedSet }));
    try {
      const reply = await request(port, 'POST', '/agent/pause', { body: JSON.stringify({ command_id: 'not-a-uuid' }) });
      expect(reply.status).toBe(400);
    } finally {
      await listener.stop();
    }
  });

  it('refuses supported-verb commands when the pod received no verification key', async () => {
    // Fail closed. A pod whose key distribution failed cannot check authorization,
    // and "cannot check" must mean "refuse" — the direction #4128 got wrong.
    const store = makeStore({ supported: supportedSet });
    const { listener, port } = await startListener(store, ENABLED_ENV, undefined, { runId: RUN_ID, envelopeKeys: new Map() });
    try {
      const body = JSON.stringify({ command_id: UUID_A });
      const reply = await request(port, 'POST', '/agent/pause', { body, envelope: envelopeFor('pause', UUID_A, body) });

      expect(reply.status).toBe(403);
      expect(store.snapshot().commands).toEqual([]);
    } finally {
      await listener.stop();
    }
  });

  it('refuses supported-verb commands when the pod does not know its own run id', async () => {
    // Without its own run id the listener cannot check the target binding, so the
    // envelope would degrade to "signed by the gateway for some run" — which is
    // exactly the cross-run authorization this closes.
    const store = makeStore({ supported: supportedSet });
    const { listener, port } = await startListener(store, ENABLED_ENV, undefined, { envelopeKeys: ENVELOPE_KEYS });
    try {
      const body = JSON.stringify({ command_id: UUID_A });
      const reply = await request(port, 'POST', '/agent/pause', { body, envelope: envelopeFor('pause', UUID_A, body) });

      expect(reply.status).toBe(403);
    } finally {
      await listener.stop();
    }
  });

  it('leaves the read paths reachable without an envelope', async () => {
    // Monitoring is authorized at the gateway, not here. Requiring an envelope for
    // a state read would break the read path for every legitimate observer while
    // protecting nothing — a read mutates no run.
    const { listener, port } = await startListener(makeStore({ supported: supportedSet }));
    try {
      expect((await request(port, 'GET', '/agent/ping')).status).toBe(200);
      expect((await request(port, 'GET', '/agent/state')).status).toBe(200);
    } finally {
      await listener.stop();
    }
  });

  it('refuses a supported verb presented with no envelope at all — the human-path gap (#3961)', async () => {
    // THE BLOCKER, made executable. Before this story `requiresEnvelope` returned
    // false for every verb because none was supported, so this state was
    // unreachable and untested. Enabling pause makes it the state the gateway's
    // *human* control path is actually in: `control_service._request_pod` sends
    // only `Authorization: Bearer <control token>` and
    // `X-Adp-Control-Generation` — it mints no envelope, because minting one
    // needs a `grant_id` and a `revocation_epoch` that a logged-in dashboard user
    // has no source for.
    //
    // The refusal below is CORRECT, and that is the point: a bearer token proves
    // the caller knows a secret the worker itself minted into its own DynamoDB
    // row, and the worker role can write any run's row. Weakening this gate to
    // let the dashboard through — e.g. enforcing only when the header happens to
    // be present — would admit any holder of a stolen token, since an attacker
    // simply omits the header. So the fix belongs on the gateway side, and until
    // it exists an end-to-end pause cannot work.
    const store = makeStore({ supported: supportedSet });
    const { listener, port } = await startListener(store);
    try {
      const reply = await request(port, 'POST', '/agent/pause', { body: JSON.stringify({ command_id: UUID_A }) });

      expect(reply.status).toBe(403);
      expect(reply.body).toEqual({ error: 'not_authorized' });
      // Nothing journaled, so a refused command cannot consume the pending cap
      // and deny control to the legitimate operator.
      expect(store.snapshot().commands).toEqual([]);
    } finally {
      await listener.stop();
    }
  });

  it('still refuses an unauthenticated request before reaching the envelope check', async () => {
    // Ordering: the token check runs first, so an anonymous caller gets 401 and
    // never reaches the parser or the verifier — the FR-1.5 property is unchanged.
    const { listener, port } = await startListener(makeStore({ supported: supportedSet }));
    try {
      const body = JSON.stringify({ command_id: UUID_A });
      const reply = await request(port, 'POST', '/agent/pause', {
        token: null,
        body,
        envelope: envelopeFor('pause', UUID_A, body),
      });
      expect(reply.status).toBe(401);
    } finally {
      await listener.stop();
    }
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

test.each(['detach', 'cancel', 'breach', 'deadline'] as const)('state projects live adapter availability through %s without weakening signed admission', async ending => {
  let deadline = Date.now() + 3600_000;
  const gate = new PauseGate({ deadlineAt: () => deadline });
  const adapter = new ClaudeControlAdapter({ pauseGate: gate });
  const store = new ControlStateStore({ generation: GENERATION, supportedActions: new Set(['pause', 'resume']),
    capabilityProvider: () => adapter.capabilities() });
  const { listener, port } = await startListener(store);
  let attempt: ReturnType<ReturnType<ClaudeControlAdapter['attemptInputFactory']>> | undefined;
  try {
    const state = async () => (await request(port, 'GET', '/agent/state')).body.capabilities;
    expect((await state()).pause).toBe(false);
    const body = JSON.stringify({ command_id: UUID_A });
    expect((await request(port, 'POST', '/agent/pause', { body })).status).toBe(403);
    attempt = adapter.attemptInputFactory()({ attemptNumber: 1, isResume: false, promptText: 'task' });
    await adapter.onAttemptHandle()({ attemptNumber: 1, session: { close() {} } });
    expect(await state()).toEqual({ pause: true, resume: true, steer: false, abort: false });
    if (ending === 'detach') await attempt.dispose();
    else if (ending === 'cancel') adapter.cancel('cancelled');
    else if (ending === 'deadline') deadline = Date.now();
    else {
      await gate.requestPause();
      const controller = new AbortController();
      controller.abort();
      await gate.admit('Read', controller.signal);
      expect(gate.barrierBreached()).toBe(true);
    }
    expect(await state()).toEqual({ pause: false, resume: false, steer: false, abort: false });
    expect((await request(port, 'POST', '/agent/pause', { body })).status).toBe(403);
  } finally { await attempt?.dispose(); await adapter.dispose(); await listener.stop(); gate.cancel(); }
});

test('a failed availability provider hides capabilities but cannot widen implemented verbs', () => {
  const store = new ControlStateStore({ generation: GENERATION, supportedActions: new Set(['pause']),
    capabilityProvider: () => { throw new Error('unavailable'); } });
  expect(Object.values(store.capabilities())).toEqual([false, false, false, false]);
  expect(store.isSupported('pause')).toBe(true);
  const overclaim = new ControlStateStore({ generation: GENERATION, supportedActions: new Set(['pause']),
    capabilityProvider: () => ({ pause: true, resume: true, steer: true, abort: true }) });
  expect(overclaim.capabilities()).toEqual({ pause: true, resume: false, steer: false, abort: false });
});

test.each([60_000, 1])('bounds delivered pauses through the signed listener and reserves resume (settle wait %i)', async settleTimeoutMs => {
  const cap = 3;
  const timers = new Set<ReturnType<typeof setTimeout>>();
  let peakTimers = 0;
  const gate = new PauseGate({ settleTimeoutMs, scheduler: {
    setTimer: (fn, ms) => {
      const handle = setTimeout(() => { timers.delete(handle); fn(); }, ms);
      timers.add(handle);
      peakTimers = Math.max(peakTimers, timers.size);
      return handle;
    },
    clearTimer: handle => { clearTimeout(handle as ReturnType<typeof setTimeout>); timers.delete(handle as ReturnType<typeof setTimeout>); },
  } });
  const work = await gate.admit('Read');
  const store = new ControlStateStore({ generation: GENERATION,
    supportedActions: new Set(['pause', 'resume']), maxPending: cap, maxTerminal: 5,
    revalidate: async () => true });
  let pauseStarts = 0;
  let liveExecutors = 0;
  let peakExecutors = 0;
  const listener = new ControlListener({
    bindAddress: '127.0.0.1', port: await freePort(), token: TOKEN,
    tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
    generation: GENERATION, runId: RUN_ID, envelopeKeys: ENVELOPE_KEYS,
    store, logger: () => {}, executor: async (action, commandId) => {
      liveExecutors += 1;
      peakExecutors = Math.max(peakExecutors, liveExecutors);
      if (action === 'pause') pauseStarts += 1;
      try {
        await applyControlCommand({ action, commandId, store, adapter: {
          requestPause: options => gate.requestPause(options),
          resumeFromPause: async () => { await gate.resume(); },
          cancel: () => {},
        } });
      } finally { liveExecutors -= 1; }
    },
  });
  const started = await listener.start(ENABLED_ENV);
  if (!started.started) throw new Error('listener failed');
  const send = async (action: ControlAction, id: string) => {
    const body = JSON.stringify({ command_id: id });
    return request(started.port, 'POST', `/agent/${action}`, { body, envelope: envelopeFor(action, id, body) });
  };
  try {
    const replies = [];
    for (let i = 0; i < 25; i += 1) {
      replies.push(await send('pause', `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`));
    }
    expect(replies.filter(reply => reply.status === 202)).toHaveLength(cap);
    expect(replies.slice(cap).map(reply => reply.body)).toEqual(Array(25 - cap).fill({ error: 'queue_full' }));
    expect(store.snapshot().commands).toHaveLength(cap);
    expect(store.snapshot().commands.every(command => command.status === 'delivered')).toBe(true);
    expect(pauseStarts).toBe(cap);
    expect(peakExecutors).toBeLessThanOrEqual(cap);
    expect(peakTimers).toBeLessThanOrEqual(cap + 1);
    expect((await send('resume', UUID_B)).status).toBe(202);
    for (let i = 0; i < 100 && (store.lookup(UUID_B).status !== 'applied' || liveExecutors > 0); i += 1) {
      await new Promise(resolve => setTimeout(resolve, 2));
    }
    expect(store.lookup(UUID_B).status).toBe('applied');
    expect(store.snapshot().commands.filter(command => command.action === 'pause').every(command => command.status === 'cancelled')).toBe(true);
    expect(gate.currentPhase()).toBe('running');
    expect(gate.activeToolCount()).toBe(1);
    expect(liveExecutors).toBe(0);
    expect(timers.size).toBe(0);
    expect(peakExecutors).toBeLessThanOrEqual(cap + 1);
    expect((await send('pause', UUID_A)).status).toBe(202);
  } finally {
    await gate.resume();
    gate.settle(work.ticket);
    gate.cancel();
    await listener.stop();
  }
});

test.each(['pause', 'resume'] as const)('retains %s executor capacity after early journal settlement', async action => {
  let now = 1000;
  let release!: () => void;
  const done = new Promise<void>(resolve => { release = resolve; });
  const store = new ControlStateStore({ generation: 1, supportedActions: new Set(['pause', 'resume']),
    maxPending: 1, maxTerminal: 0, terminalRetentionMs: 1, now: () => now });
  expect(store.submit(action, UUID_A, 'first').kind).toBe('accepted');
  store.markDelivered(UUID_A);
  const executor = store.executeDelivered(UUID_A, async () => {
    store.settle(UUID_A, 'applied');
    await done;
  });
  try {
    now += 100;
    expect(store.lookup(UUID_A).status).toBe('applied');
    expect(store.submit(action, UUID_B, 'next').kind).toBe('queue_full');
    expect(store.submit(action, UUID_A, 'first').kind).toBe('replayed');
    expect(store.submit(action, UUID_A, 'different').kind).toBe('conflict');
    const repeated = jest.fn(async () => {});
    await store.executeDelivered(UUID_A, repeated);
    await store.executeDelivered('missing', repeated);
    expect(repeated).not.toHaveBeenCalled();
  } finally { release(); await executor; }
  expect(store.lookup(UUID_A).status).toBe('unknown');
  expect(store.submit(action, UUID_B, 'next').kind).toBe('accepted');
});

test('unknown handoff outcomes are retained within the terminal cap rather than leaking live slots', async () => {
  const store = new ControlStateStore({ generation: GENERATION, supportedActions: new Set(['pause']),
    maxPending: 1, maxTerminal: 2, revalidate: async () => true });
  for (let i = 0; i < 10; i += 1) {
    const id = String(i);
    expect(store.submit('pause', id, id, { envelope: 'proof', action: 'pause', command_id: id, body_base64: '' }).kind).toBe('accepted');
    expect(await store.deliverAuthorized(id, () => { throw new Error('uncertain handoff'); })).toBe(false);
    expect(store.lookup(id).status).toBe('unknown');
  }
  expect(store.snapshot().commands).toHaveLength(2);
});

test('reserves only one resume during delayed signed delivery, with retries replayed', async () => {
  let release!: (allowed: boolean) => void;
  const checked = new Promise<boolean>(resolve => { release = resolve; });
  const store = new ControlStateStore({ generation: GENERATION, supportedActions: new Set(['pause', 'resume']),
    maxPending: 1, revalidate: async () => checked });
  const listener = new ControlListener({
    bindAddress: '127.0.0.1', port: await freePort(), token: TOKEN,
    tokenExpiresAt: new Date(Date.now() + 60_000).toISOString(),
    generation: GENERATION, runId: RUN_ID, envelopeKeys: ENVELOPE_KEYS,
    store, logger: () => {}, executor: async (_action, id) => { store.settle(id, 'applied'); },
  });
  const started = await listener.start(ENABLED_ENV);
  if (!started.started) throw new Error('listener failed');
  const send = (action: ControlAction, id: string) => {
    const body = JSON.stringify({ command_id: id });
    return request(started.port, 'POST', `/agent/${action}`, { body, envelope: envelopeFor(action, id, body) });
  };
  try {
    expect((await send('pause', UUID_A)).status).toBe(202);
    expect((await send('resume', UUID_B)).status).toBe(202);
    expect((await send('resume', UUID_B)).status).toBe(200);
    for (let i = 0; i < 20; i += 1) {
      expect((await send('resume', `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`)).status).toBe(429);
    }
    expect(store.snapshot().commands).toHaveLength(2);
  } finally { release(false); await listener.stop(); }
});


test.each(['human_session', 'delegated_grant'] as const)('steer attribution comes only from the verified %s envelope', async (authorityKind) => {
  const store = makeStore({ supported: new Set<ControlAction>(['steer']) });
  const { listener, port } = await startListener(store);
  try {
    const body = JSON.stringify({ command_id: UUID_A, instruction: 'change approach' });
    const claims = { principal: 'verified-actor-123', authority_kind: authorityKind,
      ...(authorityKind === 'human_session' ? { grant_id: undefined, revocation_epoch: undefined,
        authority_reference_id: undefined } : {}) };
    const reply = await request(port, 'POST', '/agent/steer', {
      body, envelope: envelopeFor('steer', UUID_A, body, claims),
    });
    expect(reply.status).toBe(202);
    expect(store.steeringOrigin(UUID_A)).toEqual({ principal: 'verified-actor-123', authorityKind });
    expect(JSON.stringify(store.snapshot())).not.toContain('verified-actor-123');
    const spoofed = JSON.stringify({ command_id: UUID_B, instruction: 'change approach', actor: 'forged-human' });
    const refused = await request(port, 'POST', '/agent/steer', {
      body: spoofed, envelope: envelopeFor('steer', UUID_B, spoofed, claims),
    });
    expect(refused.status).toBe(400);
    expect(store.steeringOrigin(UUID_B)).toBeNull();
  } finally { await listener.stop(); }
});
