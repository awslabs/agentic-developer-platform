import { ControlStateStore } from './control-state';
import type { QueuedAuthorization, RevalidationOutcome } from './control-authorization';
import { revalidateQueuedCommand } from './control-revalidation';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

jest.mock('@aws-sdk/credential-provider-web-identity', () => ({ fromTokenFile: () => async () => ({
  accessKeyId: 'TEST_ACCESS_KEY', secretAccessKey: 'test-only-secret', sessionToken: 'test-session',
}) }));

const proof: QueuedAuthorization = { envelope: 'signed-proof', action: 'steer', command_id: 'command', body_base64: 'e30=' };

/**
 * The transport's decision, reduced to the boolean these tests are about.
 *
 * `revalidateQueuedCommand` returns a {@link RevalidationOutcome} since #3963: an
 * abort decision has to carry the gateway's signed acceptance receipt back to the
 * process that finalizes the run, because that process is not this one and would
 * otherwise have to trust a field the pod wrote about itself (review finding 1).
 *
 * Every assertion below predates that and is about whether the command may proceed,
 * so the shape is normalized here rather than restated at ~20 call sites. Receipt
 * carriage gets its own tests at the end of this block — asserting `.toBe(false)` on
 * the object form would have passed trivially and hidden both behaviours.
 */
async function allowed(
  outcome: Promise<RevalidationOutcome>,
): Promise<boolean> {
  const result = await outcome;
  return typeof result === 'object' && result !== null ? result.allowed : result;
}

/** The receipt a decision carried, if any. */
async function receiptOf(outcome: Promise<RevalidationOutcome>): Promise<string | null> {
  const result = await outcome;
  return typeof result === 'object' && result !== null ? result.abortReceipt ?? null : null;
}

function journal(revalidate?: (p: Readonly<QueuedAuthorization>, generation: number) => Promise<RevalidationOutcome>) {
  const store = new ControlStateStore({ generation: 7, supportedActions: new Set(['steer']), revalidate });
  store.submit('steer', 'command', 'digest', proof);
  return store;
}

test('checks original proof and generation immediately before one SDK handoff', async () => {
  const check = jest.fn(async () => true);
  const store = journal(check);
  const effect = jest.fn();
  expect(store.markDelivered('command')).toBe(false);
  expect(store.settle('command', 'applied')).toBe(false);
  expect(await store.deliverAuthorized('command', effect)).toBe(true);
  expect(check).toHaveBeenCalledWith(proof, 7);
  expect(effect).toHaveBeenCalledTimes(1);
  expect(await store.deliverAuthorized('command', effect)).toBe(false);
  expect(store.submit('steer', 'command', 'digest', proof).kind).toBe('replayed');
  expect(store.lookup('command').status).toBe('delivered');
  expect(JSON.stringify(store.snapshot())).not.toContain('signed-proof');
});

test.each(['revoked', 'offline', 'unconfigured'])('%s refuses without applying and retains rejection on retry', async (kind) => {
  const store = journal(kind === 'unconfigured' ? undefined : async () => {
    if (kind === 'offline') throw new Error('unavailable');
    return false;
  });
  const effect = jest.fn();
  expect(await store.deliverAuthorized('command', effect)).toBe(false);
  expect(effect).not.toHaveBeenCalled();
  expect(store.lookup('command').status).toBe('rejected');
  expect(store.submit('steer', 'command', 'digest', proof).kind).toBe('replayed');
  expect(store.markDelivered('command')).toBe(false);
});

test('concurrent delivery cannot execute twice; cancellation while checking wins', async () => {
  let resolve!: (allowed: boolean) => void;
  const store = journal(() => new Promise<boolean>((done) => { resolve = done; }));
  const effect = jest.fn();
  const pending = store.deliverAuthorized('command', effect);
  expect(await store.deliverAuthorized('command', effect)).toBe(false);
  store.settle('command', 'cancelled', 'flow cancelled');
  resolve(true);
  expect(await pending).toBe(false);
  expect(effect).not.toHaveBeenCalled();
  expect(store.lookup('command').status).toBe('cancelled');
});

test('an approval delayed beyond one second cannot cause a handoff', async () => {
  const store = journal(async () => { await new Promise((resolve) => setTimeout(resolve, 1010)); return true; });
  const effect = jest.fn();
  expect(await store.deliverAuthorized('command', effect)).toBe(false);
  expect(effect).not.toHaveBeenCalled();
  expect(store.lookup('command').status).toBe('rejected');
});

test('an exception at the SDK boundary records unknown and cannot replay', async () => {
  const store = journal(async () => true);
  const effect = jest.fn(() => { throw new Error('lost acknowledgement'); });
  expect(await store.deliverAuthorized('command', effect)).toBe(false);
  expect(store.lookup('command').status).toBe('unknown');
  expect(await store.deliverAuthorized('command', effect)).toBe(false);
  expect(effect).toHaveBeenCalledTimes(1);
});

describe('worker online transport', () => {
  let directory: string;
  let previous: NodeJS.ProcessEnv;
  let send: jest.SpyInstance;
  beforeEach(() => {
    previous = { ...process.env };
    directory = mkdtempSync(join(tmpdir(), 'adp-control-check-'));
    process.env.ADP_AGENT_AUTHORITY_ENABLED = 'true';
    process.env.ADP_WORKER_IRSA_ROLE_ARN = 'arn:aws:iam::123456789012:role/worker';
    process.env.ADP_WORKER_IRSA_TOKEN_FILE = join(directory, 'irsa');
    process.env.AWS_REGION = 'us-west-2';
    process.env.AWS_ACCESS_KEY_ID = 'CUSTOMER_TASK_KEY';
    process.env.ADP_AGENT_CONTROL_ENDPOINT = 'https://example.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent';
    process.env.ADP_RUN_CREDENTIAL_FILE = join(directory, 'credential');
    process.env.ADP_WORKLOAD_TOKEN_FILE = join(directory, 'proof');
    writeFileSync(process.env.ADP_RUN_CREDENTIAL_FILE, 'credential-one\n');
    writeFileSync(process.env.ADP_WORKLOAD_TOKEN_FILE, 'pod-one\n');
    send = jest.spyOn(global, 'fetch').mockResolvedValue({ ok: true, json: async () => ({
      allowed: true, command_id: 'command', generation: 7, max_round_trip_ms: 1000,
    }) } as Response);
  });
  afterEach(() => {
    send.mockRestore();
    process.env = previous;
    rmSync(directory, { recursive: true, force: true });
  });
  test('signs both identity headers and rereads rotated credentials', async () => {
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(true);
    const first = send.mock.calls[0][1];
    expect(first.redirect).toBe('error');
    expect(first.body).toBe(JSON.stringify(proof));
    expect(first.headers.authorization).toContain('x-adp-run-credential;x-adp-workload-token');
    expect(first.headers.authorization).toContain('Credential=TEST_ACCESS_KEY/');
    expect(first.headers.authorization).toContain('/us-east-1/execute-api/');
    expect(first.headers['X-Adp-Run-Credential']).toBe('credential-one');
    writeFileSync(process.env.ADP_RUN_CREDENTIAL_FILE!, 'credential-two\n');
    writeFileSync(process.env.ADP_WORKLOAD_TOKEN_FILE!, 'pod-two\n');
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(true);
    expect(send.mock.calls[1][1].headers['X-Adp-Run-Credential']).toBe('credential-two');
    expect(send.mock.calls[1][1].headers['X-Adp-Workload-Token']).toBe('pod-two');
  });
  test('private verified attribution does not change the gateway wire schema', async () => {
    expect(await allowed(revalidateQueuedCommand({ ...proof, principal: 'verified-actor',
      authorityKind: 'human_session' }, 7))).toBe(true);
    const sent = (global.fetch as jest.Mock).mock.calls[0][1];
    expect(JSON.parse(sent.body)).toEqual(proof);
  });

  test('proof content cannot select the HTTP destination', async () => {
    expect(await allowed(revalidateQueuedCommand({ ...proof, envelope: 'https://169.254.169.254/' }, 7))).toBe(true);
    expect(String(send.mock.calls[0][0])).toBe(
      'https://example.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent/revalidate',
    );
  });
  test.each(['missing', 'empty', 'whitespace', 'oversized', 'directory'])('refuses %s identity without an environment fallback', async (kind) => {
    process.env.ADP_RUN_CREDENTIAL = 'must-not-fall-back';
    const file = process.env.ADP_RUN_CREDENTIAL_FILE!;
    if (kind === 'missing') delete process.env.ADP_RUN_CREDENTIAL_FILE;
    else if (kind === 'directory') process.env.ADP_RUN_CREDENTIAL_FILE = directory;
    else writeFileSync(file, kind === 'empty' ? '' : kind === 'whitespace' ? 'two tokens' : 'x'.repeat(20000));
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });
  test.each(['disabled', 'no-endpoint', 'http', 'credentials', 'query', 'fragment'])('refuses %s configuration', async (kind) => {
    if (kind === 'disabled') process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
    if (kind === 'no-endpoint') delete process.env.ADP_AGENT_CONTROL_ENDPOINT;
    if (kind === 'http') process.env.ADP_AGENT_CONTROL_ENDPOINT = 'http://gateway.test';
    if (kind === 'credentials') process.env.ADP_AGENT_CONTROL_ENDPOINT = 'https://user:password@gateway.test';
    if (kind === 'query') process.env.ADP_AGENT_CONTROL_ENDPOINT += '?key=value';
    if (kind === 'fragment') process.env.ADP_AGENT_CONTROL_ENDPOINT += '#fragment';
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });
  test.each([
    'https://169.254.169.254/latest/meta-data',
    'https://[::1]/dev/internal/v1/agent',
    'https://gateway.internal/dev/internal/v1/agent',
    'https://api.execute-api.us-east-1.amazonaws.com.attacker.test/dev/internal/v1/agent',
    'https://api.execute-api.us-east-1.amazonaws.com:8443/dev/internal/v1/agent',
    'https://api.execute-api.us-east-1.amazonaws.com/dev/internal/v1/credential-raw-read',
    'https://api.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent/../credential-raw-read',
    'https://api.execute-api.us-east-1.amazonaws.com/dev/internal/v1/%61gent',
  ])('refuses a destination outside the platform IAM route: %s', async (endpoint) => {
    process.env.ADP_AGENT_CONTROL_ENDPOINT = endpoint;
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });
  test.each(['unavailable', 'wrong-command', 'wrong-generation', 'denied', 'timeout', 'malformed'])('refuses %s response', async (kind) => {
    if (kind === 'timeout') send.mockRejectedValue(new Error('timeout'));
    else send.mockResolvedValue({ ok: kind !== 'unavailable', json: async () => {
      if (kind === 'malformed') throw new Error('invalid JSON');
      return { allowed: kind !== 'denied', command_id: kind === 'wrong-command' ? 'another' : 'command',
        generation: kind === 'wrong-generation' ? 8 : 7, max_round_trip_ms: 1000 };
    }} as Response);
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(false);
  });

  /**
   * The gateway's abort receipt survives the transport — Issue #3963 finding 1.
   *
   * The receipt is the gateway's own signed statement that it accepted this abort,
   * minted only after durable abort intent was persisted. The process that reports
   * the terminal outcome and deletes the queue message is a different one, so if the
   * receipt is dropped here that process has nothing to check and must fall back to
   * believing a field the pod wrote about itself.
   *
   * That is the bug these tests exist for: the response was previously reduced to a
   * boolean and the receipt discarded, so the sentinel writer stamped an unsigned
   * `delivery: "accepted"` literal instead — which any code in the pod could write,
   * since the agent holds a `Bash` tool.
   */
  const RECEIPT = 'adpe1.eyJhY3Rpb24iOiJhYm9ydF9hY2NlcHRlZCJ9.cmVjZWlwdC1zaWduYXR1cmU';

  /** Respond as the gateway does, with whatever receipt value the case needs. */
  const respondWith = (fields: Record<string, unknown>) => {
    send.mockResolvedValue({ ok: true, json: async () => ({
      allowed: true, command_id: 'command', generation: 7, max_round_trip_ms: 1000, ...fields,
    }) } as Response);
  };

  test('carries an accepted abort receipt back verbatim', async () => {
    respondWith({ abort_receipt: RECEIPT });
    // Verbatim, and judged nowhere in this process: the bytes are what the signature
    // covers, so normalizing or re-encoding them here could only invalidate the one
    // artifact the pod cannot manufacture.
    expect(await receiptOf(revalidateQueuedCommand(proof, 7))).toBe(RECEIPT);
  });

  test.each([
    ['absent', {}],
    ['empty', { abort_receipt: '' }],
    ['a non-string', { abort_receipt: { token: RECEIPT } }],
    ['a number', { abort_receipt: 42 }],
    ['null', { abort_receipt: null }],
  ])('reports no receipt when the gateway sends %s', async (_label, fields) => {
    respondWith(fields);
    // Exactly `null`, so "the gateway did not attest an acceptance" is one state
    // rather than several shapes the sentinel writer would have to classify. The
    // decision itself still stands — a pause has no receipt and needs none.
    expect(await receiptOf(revalidateQueuedCommand(proof, 7))).toBeNull();
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(true);
  });

  test('never surfaces a receipt alongside a refusal', async () => {
    // A contradiction the gateway does not produce, refused anyway. Passing it
    // through would hand the sentinel writer acceptance proof for a command that was
    // just denied — the precise outcome the receipt exists to make impossible.
    send.mockResolvedValue({ ok: true, json: async () => ({
      allowed: false, command_id: 'command', generation: 7, max_round_trip_ms: 1000,
      abort_receipt: RECEIPT,
    }) } as Response);
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(false);
    expect(await receiptOf(revalidateQueuedCommand(proof, 7))).toBeNull();
  });

  test('drops the receipt when the decision arrives too late to be trusted', async () => {
    // A stale-generation response is refused, and a refusal carries no receipt. The
    // receipt must not outlive the decision it belongs to: the finalizer would
    // otherwise hold gateway-signed acceptance for a command this run was told it
    // could not apply.
    send.mockResolvedValue({ ok: true, json: async () => ({
      allowed: true, command_id: 'command', generation: 8, max_round_trip_ms: 1000,
      abort_receipt: RECEIPT,
    }) } as Response);
    expect(await allowed(revalidateQueuedCommand(proof, 7))).toBe(false);
    expect(await receiptOf(revalidateQueuedCommand(proof, 7))).toBeNull();
  });
});
