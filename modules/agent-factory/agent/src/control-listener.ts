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
import { timingSafeEqual, type KeyObject } from 'crypto';
import { AddressInfo } from 'net';

import {
  ControlAction,
  ControlStateStore,
  fingerprintPayload,
} from './control-state';
import { type ControlEnvelope, type EnvelopeFailure, verifyEnvelope } from './control-envelope';
import { ControlCredentials } from './control-credentials';
import { readControlKeyring } from './control-keyring';
import { IMPLEMENTED_CONTROL_VERBS } from './control-runtime';

/**
 * Verbs this build can perform.
 *
 * Issue #3962: re-exported from the control runtime rather than declared here.
 * It used to be its own empty set, which made it a second answer to the question
 * `IMPLEMENTED_CONTROL_VERBS` already answers — two sets that agreed only by
 * both being empty, and would have to be widened in lockstep by every story that
 * adds a verb. An alias cannot drift.
 *
 * The worker no longer reads this constant at all: it derives the store's set
 * from the adapter via `listenerActionsFor`, so the verbs on the wire are a
 * consequence of the transport that exists rather than a claim beside it. This
 * export is kept for the callers and gates that reference the name.
 */
const SUPPORTED_ACTIONS: ReadonlySet<ControlAction> = IMPLEMENTED_CONTROL_VERBS;

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

/**
 * Header carrying the gateway's authorization envelope — Issue #5028.
 *
 * A separate header from `Authorization` because the two answer different
 * questions and have different lifetimes: the bearer token says "you know this
 * run's secret" and lives for the run, the envelope says "the control service
 * authorized this exact command" and lives for 30 seconds. Folding the second
 * into the first would tie the short-lived per-command authorization to the
 * long-lived credential's plumbing.
 */
export const ENVELOPE_HEADER = 'x-adp-control-authorization';

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
  /** UTC expiry persisted with this token's registration by the entrypoint. */
  tokenExpiresAt: string;
  /** Atomically replaced supervisor lease. When set, absence refuses all requests. */
  credentialFile?: string;
  /** Run generation. A request declaring a different generation is stale. */
  generation: number;
  store: ControlStateStore;
  /** Structured log sink. Injected so tests observe diagnostics without stdout capture. */
  logger?: (level: string, message: string, context?: Record<string, unknown>) => void;

  /**
   * Apply an accepted command to the running agent — Issue #3961.
   *
   * The seam between the wire and the harness. Absent for a run with no adapter
   * (or before any verb was implemented), in which case accepted commands stay
   * `pending` — see {@link applyAccepted} for why that, and not `rejected`.
   *
   * Called after the 202 is written, deliberately: `pause` waits for admitted
   * tool work to reach a boundary, and a synchronous apply would hold the socket
   * open for the whole settle timeout. The journal carries the outcome, and the
   * dashboard already polls state.
   *
   * Invoked *through* the journal's delivery gate rather than directly, so an
   * envelope-bearing command is revalidated against the gateway first. The
   * executor itself must therefore settle the command it is handed.
   */
  executor?: (action: ControlAction, commandId: string) => Promise<void>;

  /**
   * This run's own id — Issue #5028.
   *
   * The listener's independently-known target identity, used to check the
   * envelope's `target_run_id`. It comes from this pod's own environment, not
   * from the request, which is the entire reason the binding means anything: a
   * value read out of the request would match itself.
   */
  runId?: string;

  /**
   * Ed25519 verification keys by key id — Issue #5028.
   *
   * Public keys only. A worker that could sign would be able to authorize its own
   * commands, so there is no signing key in this process and no code path here
   * that would use one.
   *
   * An empty map means no envelope can verify. That is deliberate and is the
   * fail-closed direction: a pod that never received a key refuses live-control
   * commands rather than accepting them unverified.
   */
  envelopeKeys?: Map<string, KeyObject>;
  /** Public keys projected by Kubernetes; reloaded without a worker restart. */
  envelopeKeysFile?: string;
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
  // Serialize revalidation and executor *start* in journal acceptance order.
  // Never wait for pause settlement here: resume must be able to cancel it.
  private deliveryTail: Promise<void> = Promise.resolve();
  private readonly config: ControlListenerConfig;
  private readonly tokenExpiresAt: number;
  private readonly credentials?: ControlCredentials;
  private readonly log: (level: string, message: string, context?: Record<string, unknown>) => void;

  constructor(config: ControlListenerConfig) {
    this.config = config;
    if (config.credentialFile) this.credentials = new ControlCredentials(config.credentialFile, config.runId ?? '', config.generation);
    // Accept the UTC format the registration writer emits, never an implicit
    // local date or an unbounded credential when configuration is absent.
    this.tokenExpiresAt = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(config.tokenExpiresAt ?? '')
      ? Date.parse(config.tokenExpiresAt)
      : Number.NaN;
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
    if (!Number.isFinite(this.tokenExpiresAt) || this.tokenExpiresAt <= Date.now()) {
      this.log('error', 'control listener not started: invalid or expired token lifetime', { reason: 'invalid_token_expiry' });
      return { started: false, reason: 'misconfigured', detail: 'invalid or expired control token expiry' };
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
      this.writeJson(res, 200, {
        ok: true, generation: this.config.generation,
        ...(this.config.envelopeKeysFile ? { verification_key_ids: [...this.verificationKeys().keys()].sort() } : {}),
      });
      return;
    }
    if (method === 'GET' && path === '/agent/state') {
      // A state read touches no SDK object and starts no assistant turn — it is
      // served entirely from recorded state (revival-design §2).
      const state = this.config.store.snapshot();
      const keyIds = this.verificationKeyIds();
      const ready = keyIds.length > 0;
      this.writeJson(res, 200, {
        ...state,
        verification_key_ids: keyIds,
        capabilities: {
          pause: ready && state.capabilities.pause,
          resume: ready && state.capabilities.resume,
          steer: ready && state.capabilities.steer,
          abort: ready && state.capabilities.abort,
        },
      });
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
    // The gateway also checks the registered expiry, but a caller with the pod
    // token must not bypass that limit by reaching this socket directly.
    const header = req.headers.authorization;
    if (typeof header !== 'string' || !header.startsWith('Bearer ')) {
      return false;
    }
    const presented = Buffer.from(header.slice('Bearer '.length), 'utf8');
    const tokens = this.credentials
      ? this.credentials.read()
      : [{ token: this.config.token, expiresAt: this.tokenExpiresAt }];
    if (!tokens.some(({ token, expiresAt }) => {
      const expected = Buffer.from(token, 'utf8');
      return Date.now() < expiresAt && presented.length === expected.length && timingSafeEqual(presented, expected);
    })) {
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
   *
   * Issue #5028 inserts envelope verification between validation and submission,
   * and only for verbs this build can actually perform. See
   * {@link requiresEnvelope} for why that condition rather than "always".
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

    // Issue #5028. Verified against `raw` — the exact bytes off the socket —
    // rather than a re-serialization of `payload`, so a body edited between
    // authorization and arrival cannot digest to the same value.
    let queuedAuthorization;
    if (this.requiresEnvelope(action)) {
      const authorization = this.verifyControlEnvelope(action, validation.commandId, raw, req);
      if (!authorization.ok) {
        // 403, not 401: the caller's *identity* was accepted (it passed
        // `authenticate`) and its *authorization* was not. A 401 here would tell
        // a caller with a valid token to go re-authenticate, which would not help
        // and would obscure the real cause.
        //
        // The reason is logged, never returned. A caller learning that its
        // envelope failed on `target_mismatch` rather than `bad_signature` learns
        // which run it just probed exists.
        this.log('warn', 'control command refused: envelope not verified', {
          action,
          reason: authorization.reason,
          generation: this.config.generation,
        });
        this.writeJson(res, 403, { error: 'not_authorized' });
        return;
      }
      this.log('info', 'control command authorized', {
        action,
        command_id: validation.commandId,
        // The audit trail the gateway's decision record joins against. No token,
        // no signature, no instruction text — just the identifiers.
        principal: authorization.envelope.principal,
        grant_id: authorization.envelope.grantId,
        revocation_epoch: authorization.envelope.revocationEpoch,
        authority_reference_id: authorization.envelope.authorityReferenceId,
      });
      queuedAuthorization = { envelope: req.headers[ENVELOPE_HEADER] as string, action,
        command_id: validation.commandId, body_base64: raw.toString('base64') };
    }

    const outcome = this.config.store.submit(action, validation.commandId, fingerprintPayload(payload), queuedAuthorization);
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
        // Respond first, then apply — Issue #3961. The HTTP contract is 202
        // "accepted", not "done": pause has to wait for admitted tool work to
        // reach a boundary, and holding the socket for that would turn a bounded
        // acknowledgement into a request that hangs for the settle timeout. The
        // journal is the durable record, and the dashboard polls state, so the
        // outcome reaches the operator either way.
        this.writeJson(res, 202, { command: outcome.record, state: this.config.store.snapshot().state });
        this.deliveryTail = this.deliveryTail.then(() => this.applyAccepted(action, validation.commandId));
        return;
    }
  }

  /**
   * Hand an accepted command to whatever can actually perform it — Issue #3961.
   *
   * Delivery goes through the journal, never straight to the executor, and the
   * method depends on whether the command carried an envelope:
   *
   * - proof-bearing commands go through `deliverAuthorized`, which re-checks the
   *   grant against the gateway immediately before handoff. Calling the executor
   *   directly would skip that re-check and execute an action whose authority may
   *   have been revoked since the 202 — the exact hidden-queue bypass
   *   `ClaudeAttemptEndpoint.deliver` documents as forbidden;
   * - unauthorized-path commands (no envelope required for this verb) go through
   *   `markDelivered`, which is the only transition `settle('applied', ...)` will
   *   accept afterwards.
   *
   * With **no executor** the command is left `pending` and nothing is settled.
   * That is deliberate: `pending` means "accepted, not yet acted on", which is
   * the truth for a run whose harness cannot perform the verb, and it keeps the
   * pending cap doing its job. Auto-rejecting here instead would silently drain
   * the queue and disable the 429 backpressure the cap exists to provide.
   */
  private async applyAccepted(action: ControlAction, commandId: string): Promise<void> {
    const executor = this.config.executor;
    if (!executor) {
      // Nothing to apply it with. Logged, not settled — see above.
      this.log('warn', 'control command accepted with no executor attached', { action, command_id: commandId });
      return;
    }
    try {
      const store = this.config.store;
      // Synchronous by contract: `deliverAuthorized` permits no `await` between
      // its bounded re-check and the handoff, so the executor is *started* here
      // and its failure is caught on the promise rather than by the try below,
      // which has already returned by then. Without this catch an executor
      // rejection would surface as an unhandled rejection and could take the
      // worker down over a control command.
      const run = () => {
        void store.executeDelivered(commandId, () => executor(action, commandId)).catch((err: unknown) => {
          this.log('warn', 'control executor failed', { action, command_id: commandId,
            detail: (err as Error)?.message ?? String(err) });
          store.settle(commandId, 'rejected', 'control executor failed');
        });
      };
      // `deliverAuthorized` returns false when the re-check fails, having already
      // settled the command `rejected`. Nothing more to do on that path.
      if (this.requiresEnvelope(action)) {
        await store.deliverAuthorized(commandId, run);
        return;
      }
      if (store.markDelivered(commandId)) run();
    } catch (err) {
      // An executor failure is the run's business, not the listener's: the
      // listener has already answered, and a throw here would become an
      // unhandled rejection that takes down a worker over a control command.
      this.log('warn', 'control executor failed', { action, command_id: commandId,
        detail: (err as Error)?.message ?? String(err) });
      this.config.store.settle(commandId, 'rejected', 'control executor failed');
    }
  }

  /**
   * Whether this verb must present a gateway envelope — Issue #5028.
   *
   * Every implemented verb requires a signed envelope. Unsupported verbs keep
   * their 501 contract because there is no effect to authorize. Deriving this
   * from the capability set ensures a newly implemented verb cannot accidentally
   * bypass verification through a separate opt-in list.
   */
  private requiresEnvelope(action: ControlAction): boolean {
    return this.config.store.isSupported(action);
  }

  /**
   * Verify the envelope against this pod's own independently-known facts.
   *
   * Every `expected` value below comes from this process or from this request's
   * own path and parsed body — never from the envelope. Passing an envelope-derived
   * value would make the corresponding binding check compare a claim to itself.
   */
  private verifyControlEnvelope(
    action: ControlAction,
    commandId: string,
    raw: Buffer,
    req: http.IncomingMessage,
  ): { ok: true; envelope: ControlEnvelope } | { ok: false; reason: EnvelopeFailure | 'not_configured' } {
    const runId = this.config.runId;
    const keys = this.verificationKeys();
    if (!runId || !keys || keys.size === 0) {
      // Fail closed. A pod that was never given its run id or its verification
      // keys cannot check authorization, and "cannot check" must mean "refuse",
      // not "allow". This is the direction the marker-signing fail-open (#4128)
      // got wrong, on a path with a smaller blast radius than this one.
      return { ok: false, reason: 'not_configured' };
    }

    const header = req.headers[ENVELOPE_HEADER];
    const token = Array.isArray(header) ? header[0] : header;

    const result = verifyEnvelope(token, keys, {
      runId,
      generation: this.config.generation,
      action,
      commandId,
      body: raw,
    });
    return result.ok ? { ok: true, envelope: result.envelope } : { ok: false, reason: result.reason };
  }

  private verificationKeys(): Map<string, KeyObject> {
    return this.config.envelopeKeysFile ? readControlKeyring(this.config.envelopeKeysFile) : (this.config.envelopeKeys ?? new Map());
  }

  private verificationKeyIds(): string[] {
    if (!this.config.runId?.trim()) return [];
    const ids = [...this.verificationKeys().keys()];
    // Report only bounded public identifiers. The gateway intersects these
    // with its current signer; a retired projection cannot advertise support.
    if (ids.length > 8 || ids.some((id) => !/^[A-Za-z0-9._-]{1,128}$/.test(id))) return [];
    return ids.sort();
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
