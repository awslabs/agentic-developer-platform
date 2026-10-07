import { maintainSessionLease } from './sandbox-session-lease';
import { buildSandboxPod } from './sandbox-pod';
import { gatewayBinding } from './sandbox-gateway.fixture';

const scope = { run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' as const };

test('renews only the bound persistent session and stops on cancellation', async () => {
  const abort = new AbortController();
  const client = { renewSession: jest.fn(async () => scope) };
  await maintainSessionLease(client, scope, abort.signal, async () => {
    if (client.renewSession.mock.calls.length === 2) abort.abort();
  });
  expect(client.renewSession).toHaveBeenCalledTimes(2);
});

test.each([
  { ...scope, run_id: 'another-run' },
  { ...scope, session_id: 'another-session' },
  { ...scope, session_mode: 'ephemeral' as const },
])('refuses renewed authority outside the original binding: %j', async reply => {
  const client = { renewSession: jest.fn(async () => reply) };
  await expect(maintainSessionLease(client, scope, new AbortController().signal, async () => {}))
    .rejects.toThrow('binding changed');
});

test('refuses renewal failure instead of using cached authority', async () => {
  const client = { renewSession: jest.fn(async (): Promise<typeof scope> => { throw new Error('gateway unavailable'); }) };
  await expect(maintainSessionLease(client, scope, new AbortController().signal, async () => {}))
    .rejects.toThrow('gateway unavailable');
});

test('persistent pod is session-labelled without a hidden one-shot deadline', () => {
  const pod = buildSandboxPod({ runId: 'run-a', sessionId: 'session-a', sessionMode: 'persistent',
    image: `registry.test/chat@sha256:${'a'.repeat(64)}`, gatewayUrl: 'https://gateway.example.test' },
    gatewayBinding);
  expect(pod.spec).not.toHaveProperty('activeDeadlineSeconds');
  expect(pod.metadata.labels['adp.io/session-hash']).toMatch(/^[a-f0-9]{64}$/);
  expect(pod.metadata.labels['adp.io/run-hash']).toMatch(/^[a-f0-9]{64}$/);
});
