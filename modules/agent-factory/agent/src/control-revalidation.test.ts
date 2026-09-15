import { ControlStateStore } from './control-state';
import type { QueuedAuthorization } from './control-authorization';
import { revalidateQueuedCommand } from './control-revalidation';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

jest.mock('@aws-sdk/credential-provider-node', () => ({ defaultProvider: () => async () => ({
  accessKeyId: 'TEST_ACCESS_KEY', secretAccessKey: 'test-only-secret', sessionToken: 'test-session',
}) }));

const proof: QueuedAuthorization = { envelope: 'signed-proof', action: 'steer', command_id: 'command', body_base64: 'e30=' };

function journal(revalidate?: (p: Readonly<QueuedAuthorization>, generation: number) => Promise<boolean>) {
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
    expect(await revalidateQueuedCommand(proof, 7)).toBe(true);
    const first = send.mock.calls[0][1];
    expect(first.redirect).toBe('error');
    expect(first.body).toBe(JSON.stringify(proof));
    expect(first.headers.authorization).toContain('x-adp-run-credential;x-adp-workload-token');
    expect(first.headers['X-Adp-Run-Credential']).toBe('credential-one');
    writeFileSync(process.env.ADP_RUN_CREDENTIAL_FILE!, 'credential-two\n');
    writeFileSync(process.env.ADP_WORKLOAD_TOKEN_FILE!, 'pod-two\n');
    expect(await revalidateQueuedCommand(proof, 7)).toBe(true);
    expect(send.mock.calls[1][1].headers['X-Adp-Run-Credential']).toBe('credential-two');
    expect(send.mock.calls[1][1].headers['X-Adp-Workload-Token']).toBe('pod-two');
  });
  test('proof content cannot select the HTTP destination', async () => {
    expect(await revalidateQueuedCommand({ ...proof, envelope: 'https://169.254.169.254/' }, 7)).toBe(true);
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
    expect(await revalidateQueuedCommand(proof, 7)).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });
  test.each(['disabled', 'no-endpoint', 'http', 'credentials', 'query', 'fragment'])('refuses %s configuration', async (kind) => {
    if (kind === 'disabled') process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
    if (kind === 'no-endpoint') delete process.env.ADP_AGENT_CONTROL_ENDPOINT;
    if (kind === 'http') process.env.ADP_AGENT_CONTROL_ENDPOINT = 'http://gateway.test';
    if (kind === 'credentials') process.env.ADP_AGENT_CONTROL_ENDPOINT = 'https://user:password@gateway.test';
    if (kind === 'query') process.env.ADP_AGENT_CONTROL_ENDPOINT += '?key=value';
    if (kind === 'fragment') process.env.ADP_AGENT_CONTROL_ENDPOINT += '#fragment';
    expect(await revalidateQueuedCommand(proof, 7)).toBe(false);
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
    expect(await revalidateQueuedCommand(proof, 7)).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });
  test.each(['unavailable', 'wrong-command', 'wrong-generation', 'denied', 'timeout', 'malformed'])('refuses %s response', async (kind) => {
    if (kind === 'timeout') send.mockRejectedValue(new Error('timeout'));
    else send.mockResolvedValue({ ok: kind !== 'unavailable', json: async () => {
      if (kind === 'malformed') throw new Error('invalid JSON');
      return { allowed: kind !== 'denied', command_id: kind === 'wrong-command' ? 'another' : 'command',
        generation: kind === 'wrong-generation' ? 8 : 7, max_round_trip_ms: 1000 };
    }} as Response);
    expect(await revalidateQueuedCommand(proof, 7)).toBe(false);
  });
});
