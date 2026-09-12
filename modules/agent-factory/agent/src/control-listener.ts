/**
 * In-pod HTTP control listener — Issue #3960.
 *
 * The gateway's only way to reach a running agent. Every design decision here is
 * a rejection of the pattern the pod's existing health listener uses, because a
 * health endpoint and a command endpoint have opposite threat models: the health
 * server binds all interfaces with no authentication, which is right for a
 * kubelet probe and catastrophic for a channel that can abort someone's run.
 *
 * The four independent controls, none of which is sufficient alone:
 *
 * 1. **The listener does not exist when the flag is off.** Not "exists and
 *    refuses" — no process, no bound port, no queue, no control fields written
 *    (FR-1.1, FR-8.5). Nothing to attack and nothing to misconfigure.
 * 2. **It binds the pod IP explicitly**, never `0.0.0.0` implicitly (FR-1.2).
 *    An implicit bind is how a listener intended for one interface becomes
 *    reachable on all of them.
 * 3. **Every request is authenticated before it is routed** (FR-1.5). The token
 *    check runs before path dispatch, before body reads, and before JSON
 *    parsing, so an unauthenticated caller cannot reach the parser at all — no
 *    payload-shaped attack surface without a valid credential.
 * 4. **A NetworkPolicy restricts the port to the gateway** — shipped in the same
 *    change as this listener, never as follow-up hardening (FR-1.9).
 *
 * The bind address is emphatically *not* the boundary (NFR-5). Any pod in the
 * cluster can reach any pod IP absent a policy, so the token and the policy carry
 * the security; the explicit bind just removes a gratuitous extra interface.
 *
 * Routing is on `(method, path)` and reserves `/agent/events` so a later
 * streaming read-side needs no policy change (FR-1.3, FR-1.10). Responses are
 * written through a small helper rather than a single buffer-then-`end()` path,
 * so streaming remains available to that story (FR-1.4).
 */

import * as http from 'http';
import { timingSafeEqual } from 'crypto';
import { AddressInfo } from 'net';

import {
  ControlAction,
  ControlStateStore,
  fingerprintPayload,
} from './control-state';

/** Verbs this build can perform. Empty in S1 — the foundation ships before the verbs. */
const SUPPORTED_ACTIONS: ReadonlySet<ControlAction> = new Set<ControlAction>();

/** Verb path segments the listener recognises, supported or not. */
const KNOWN_ACTIONS: readonly ControlAction[] = ['pause', 'resume', 'steer', 'abort'];

/**
 * Reserved for a future streaming read-side (ADR-9). Declared now so the
 * port-scoped, verb-blind NetworkPolicy already covers it: adding a path later
 * must not require an infrastructure change, because a policy edit is the step
 * most likely to be forgotten and the one whose absence is least visible.
 */
const RESERVED_EVENTS_PATH = '/agent/events';

/** Maximum request body. Matches the gateway's cap so neither side is the weak link. */
export const MAX_BODY_BYTES = 16 * 1024;

/** Bound instruction and reason length, mirroring the gateway's schema. */
export const MAX_INSTRUCTION_CHARS = 4000;
export const MAX_REASON_CHARS = 1000;

/**
 * Strict, function-form flag reader — Issue #3960 (FR-8.3).
 *
 * A function rather than a module-level constant so tests can vary the
 * environment without reloading the module, and so the value is read when the
 * listener starts rather than when the file is first imported. `env` is a
 * parameter for the same reason: a test should not have to mutate
 * `process.env` to assert the flag-off path.
 *
 * Strict means only the exact string `'true'` enables. Anything else — a typo, an
 * empty string, `'1'`, absence — resolves to off. The worker's flag is read
 * independently of the gateway's: a gateway flag alone must never be able to
 * start a listener in a pod (revival-design §3).
 */
export function isAgentControlEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.FEATURE_AGENT_CONTROL_ENABLED === 'true';
}

export interface ControlListenerConfig {
  /** Pod IP to bind. Required — an absent value must fail, never fall back to a wildcard. */
  bindAddress: string;
  port: number;
  /** Per-run bearer token, self-minted by the entrypoint. */
  token: string;
  /** Run generation. A request declaring a different generation is stale. */
  generation: number;
  store: ControlStateStore;
  /** Structured log sink. Injected so tests observe diagnostics without stdout capture. */
  logger?: (level: string, message: string, context?: Record<string, unknown>) => void;
}

/**
 * Start outcome. `disabled` and `misconfigured` are distinct on purpose: the
 * first is a deliberate operating state, the second is a bug that must be
 * visible. Collapsing them would let a missing pod IP look like an intentional
 * flag-off and disappear from the record (FR-1.12, NFR-10).
 */
export type StartOutcome =
  | { started: true; port: number; address: string }
  | { started: false; reason: 'disabled' | 'misconfigured' | 'bind_failed'; detail?: string };

export class ControlListener {
  private server: http.Server | null = null;
  private readonly config: ControlListenerConfig;
  private readonly tokenBuffer: Buffer;
  private readonly log: (level: string, message: string, context?: Record<string, unknown>) => void;

  constructor(config: ControlListenerConfig) {
    this.config = config;
    this.tokenBuffer = Buffer.from(config.token ?? '', 'utf8');
    this.log =
      config.logger ??
      ((level, message, context) => {
        // Context is stringified rather than spread so a future field cannot
        // widen the log line unnoticed. The token is never placed in context by
        // any call site in this file.
        console.log(JSON.stringify({ level, message, component: 'control-listener', ...context }));
      });
  }

  /**
   * Start the listener, or report precisely why it did not start.
   *
   * Configuration is validated before the socket is opened. Notably an absent
   * bind address is `misconfigured`, never a fallback to all interfaces: silently
   * widening the bind to recover from a missing downwardAPI value is how a
   * hardening decision gets undone by an unrelated deployment change.
   */
  async start(env: NodeJS.ProcessEnv = process.env): Promise<StartOutcome> {
    if (!isAgentControlEnabled(env)) {
      // No server object, no port, no journal activity. The flag-off path must
      // leave the ordinary run byte-identical in its observable behaviour.
      return { started: false, reason: 'disabled' };
    }
    if (!this.config.bindAddress) {
      this.log('error', 'control listener not started: no pod IP to bind', { reason: 'missing_bind_address' });
      return { started: false, reason: 'misconfigured', detail: 'missing bind address' };
    }
    if (!this.config.token) {
      // An unauthenticated command endpoint is strictly worse than no endpoint.
      this.log('error', 'control listener not started: no control token', { reason: 'missing_token' });
      return { started: false, reason: 'misconfigured', detail: 'missing control token' };
    }
    if (!Number.isInteger(this.config.port) || this.config.port <= 0) {
      this.log('error', 'control listener not started: invalid port', { reason: 'invalid_port' });
      return { started: false, reason: 'misconfigured', detail: 'invalid port' };
    }

    const server = http.createServer((req, res) => {
      // Errors are caught here rather than allowed to reach the process: an
      // unhandled throw in a request handler would take down a multi-hour run,
      // which is precisely the outcome NFR-6 forbids for a malformed payload.
      this.handle(req, res).catch((error) => {
        this.log('warn', 'control request handling failed', { error: String(error) });
        if (!res.headersSent) {
          this.writeJson(res, 500, { error: 'internal_error' });
        } else {
          res.end();
        }
      });
    });

    // A socket-level error must degrade control to unavailable, not kill the run.
    server.on('error', (error) => {
      this.log('warn', 'control listener socket error', { error: String(error) });
    });

    return new Promise<StartOutcome>((resolve) => {
      const onError = (error: Error) => {
        this.log('error', 'control listener failed to bind', { error: String(error) });
        resolve({ started: false, reason: 'bind_failed', detail: String(error) });
      };
      server.once('error', onError);
      // Explicit host argument — the second parameter is the entire point.
      server.listen(this.config.port, this.config.bindAddress, () => {
        server.removeListener('error', onError);
        this.server = server;
        const address = server.address() as AddressInfo | null;
        const boundPort = address ? address.port : this.config.port;
        this.log('info', 'control listener started', {
          port: boundPort,
          generation: this.config.generation,
        });
        resolve({ started: true, port: boundPort, address: this.config.bindAddress });
      });
    });
  }

  /**
   * Stop the listener.
   *
   * Called during terminal teardown so the port closes before the pod exits and
   * a late request cannot be accepted against a run that is finishing.
   */
  async stop(): Promise<void> {
    const server = this.server;
    if (!server) return;
    this.server = null;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  /** The bound port, or null when not listening. Used by registration. */
  boundPort(): number | null {
    if (!this.server) return null;
    const address = this.server.address() as AddressInfo | null;
    return address ? address.port : null;
  }

  /**
   * Handle one request: authenticate, then route.
   *
   * The ordering is the security property. Authentication precedes path parsing,
   * body reading and JSON parsing, so no unauthenticated request reaches any
   * parser. A listener that parsed first and authenticated second would expose
   * its whole payload surface to anything that can open a socket.
   */
  private async handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    if (!this.authenticate(req)) {
      // Deliberately uninformative: no hint about whether the token was absent,
      // malformed, or simply wrong, and no indication that this run exists.
      this.writeJson(res, 401, { error: 'unauthorized' });
      return;
    }

    const method = req.method ?? 'GET';
    const path = (req.url ?? '/').split('?')[0];

    if (method === 'GET' && path === '/agent/ping') {
      this.writeJson(res, 200, { ok: true, generation: this.config.generation });
      return;
    }
    if (method === 'GET' && path === '/agent/state') {
      // A state read touches no SDK object and starts no assistant turn — it is
      // served entirely from recorded state (revival-design §2).
      this.writeJson(res, 200, this.config.store.snapshot());
      return;
    }
    if (path === RESERVED_EVENTS_PATH) {
      // Reserved, not implemented. 501 rather than 404 so the path is visibly
      // claimed: a 404 would invite a later story to mount something else here.
      this.writeJson(res, 501, { error: 'not_implemented', detail: 'event stream is reserved' });
      return;
    }

    const action = this.actionFromPath(method, path);
    if (action) {
      await this.handleCommand(action, req, res);
      return;
    }

    this.writeJson(res, 404, { error: 'not_found' });
  }

  /**
   * Constant-time bearer-token comparison plus a generation check.
   *
   * `timingSafeEqual` requires equal lengths, so length is compared first — and
   * that comparison leaks only the token's length, which is fixed for all runs.
   * A naive `===` would leak the token a byte at a time to a caller who can time
   * responses.
   *
   * The generation header makes a token replayed against a *later* generation of
   * the same run fail even though the token bytes still match: a new generation
   * is a different process, and a command aimed at the previous one must not land
   * on it.
   */
  private authenticate(req: http.IncomingMessage): boolean {
    const header = req.headers.authorization;
    if (typeof header !== 'string' || !header.startsWith('Bearer ')) {
      return false;
    }
    const presented = Buffer.from(header.slice('Bearer '.length), 'utf8');
    if (presented.length !== this.tokenBuffer.length) {
      return false;
    }
    if (!timingSafeEqual(presented, this.tokenBuffer)) {
      return false;
    }

    const declared = req.headers['x-adp-control-generation'];
    if (typeof declared === 'string' && declared.length > 0) {
      if (Number.parseInt(declared, 10) !== this.config.generation) {
        return false;
      }
    }
    return true;
  }

  /** Map `POST /agent/<verb>` to a known verb, or null. */
  private actionFromPath(method: string, path: string): ControlAction | null {
    if (method !== 'POST') return null;
    const match = /^\/agent\/([a-z]+)$/.exec(path);
    if (!match) return null;
    const candidate = match[1] as ControlAction;
    return KNOWN_ACTIONS.includes(candidate) ? candidate : null;
  }

  /**
   * Handle an authenticated command.
   *
   * Validation runs before the supported-verb check, so a malformed body is a 400
   * even for a verb this build cannot perform. That order is what makes the
   * "malformed payload returns 400 and the run continues unharmed" guarantee
   * testable now rather than only once a verb ships (AC-S5).
   */
  private async handleCommand(
    action: ControlAction,
    req: http.IncomingMessage,
    res: http.ServerResponse,
  ): Promise<void> {
    let raw: Buffer;
    try {
      raw = await this.readBoundedBody(req);
    } catch (error) {
      if ((error as Error).message === 'body_too_large') {
        this.writeJson(res, 413, { error: 'payload_too_large' });
        return;
      }
      this.writeJson(res, 400, { error: 'bad_request' });
      return;
    }

    let payload: Record<string, unknown>;
    try {
      const parsed = JSON.parse(raw.toString('utf8'));
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('not_an_object');
      }
      payload = parsed as Record<string, unknown>;
    } catch {
      // A malformed body is a client error, logged at warn and nothing more. The
      // run continues: a control-channel parse failure must never be fatal to
      // hours of unrelated work (NFR-6).
      this.writeJson(res, 400, { error: 'invalid_json' });
      return;
    }

    const validation = this.validatePayload(action, payload);
    if (!validation.ok) {
      this.writeJson(res, 400, { error: 'invalid_request', detail: validation.detail });
      return;
    }

    const outcome = this.config.store.submit(action, validation.commandId, fingerprintPayload(payload));
    switch (outcome.kind) {
      case 'unsupported':
        this.writeJson(res, 501, { error: 'not_implemented', action, capabilities: this.config.store.capabilities() });
        return;
      case 'conflict':
        // Same id, different content. 409 rather than overwriting: see
        // control-state.ts for why guessing which intent wins is unsafe.
        this.writeJson(res, 409, { error: 'command_id_conflict' });
        return;
      case 'queue_full':
        this.writeJson(res, 429, { error: 'queue_full' });
        return;
      case 'replayed':
        // 200, not 202: nothing new was accepted, and the recorded outcome is
        // returned as-is. A 202 here would read as a second acceptance.
        this.writeJson(res, 200, { command: outcome.record, state: this.config.store.snapshot().state });
        return;
      case 'accepted':
        this.writeJson(res, 202, { command: outcome.record, state: this.config.store.snapshot().state });
        return;
    }
  }

  /**
   * Validate a command payload strictly.
   *
   * Unknown fields are rejected rather than ignored, and `actor`, `target` and
   * `token` are named in the diagnostic because they are the fields a caller
   * would send to try to override attribution or the transport destination.
   * Silently dropping them would return success to an attempted override, so the
   * caller would believe it took effect (AC-S7).
   */
  private validatePayload(
    action: ControlAction,
    payload: Record<string, unknown>,
  ): { ok: true; commandId: string } | { ok: false; detail: string } {
    const allowed = action === 'steer' ? ['command_id', 'instruction'] : ['command_id', 'reason'];
    for (const key of Object.keys(payload)) {
      if (!allowed.includes(key)) {
        return { ok: false, detail: `unexpected field: ${key}` };
      }
    }

    const commandId = payload.command_id;
    if (typeof commandId !== 'string' || !isUuid(commandId)) {
      return { ok: false, detail: 'command_id must be a UUID' };
    }

    if (action === 'steer') {
      const instruction = payload.instruction;
      if (typeof instruction !== 'string' || instruction.length === 0) {
        return { ok: false, detail: 'instruction is required' };
      }
      if (instruction.length > MAX_INSTRUCTION_CHARS) {
        return { ok: false, detail: 'instruction is too long' };
      }
    } else if (payload.reason !== undefined) {
      const reason = payload.reason;
      if (typeof reason !== 'string' || reason.length > MAX_REASON_CHARS) {
        return { ok: false, detail: 'reason is invalid' };
      }
    }

    return { ok: true, commandId };
  }

  /**
   * Read the body, aborting as soon as the cap is exceeded.
   *
   * The check is inside the chunk loop, not after it: a post-hoc length check
   * would have already buffered the whole hostile body in memory before deciding
   * to reject it.
   */
  private readBoundedBody(req: http.IncomingMessage): Promise<Buffer> {
    return new Promise<Buffer>((resolve, reject) => {
      const chunks: Buffer[] = [];
      let total = 0;
      req.on('data', (chunk: Buffer) => {
        total += chunk.length;
        if (total > MAX_BODY_BYTES) {
          reject(new Error('body_too_large'));
          req.destroy();
          return;
        }
        chunks.push(chunk);
      });
      req.on('end', () => resolve(Buffer.concat(chunks)));
      req.on('error', (error) => reject(error));
    });
  }

  /**
   * Write a JSON response.
   *
   * Headers and body are written separately rather than through a single
   * `end(payload)` call, keeping the streaming path open for the reserved event
   * stream (FR-1.4). `no-store` because control state is live and a cached
   * response would show a stale phase.
   */
  private writeJson(res: http.ServerResponse, status: number, body: unknown): void {
    const payload = JSON.stringify(body);
    res.writeHead(status, {
      'content-type': 'application/json',
      'cache-control': 'no-store',
    });
    res.write(payload);
    res.end();
  }
}

/** UUID format check for command ids — the idempotency key space. */
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

export { SUPPORTED_ACTIONS };
