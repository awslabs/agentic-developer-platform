import { createHash } from 'node:crypto';
import type { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { reconcileSupervisorDelivery, restoreSupervisorTurn } from './sandbox-supervisor-completion';
import { registeredSandboxAssignment } from './sandbox-supervisor-admission';
import type { SandboxPodApi } from './sandbox-launcher';
import { sandboxCreationName } from './sandbox-pod';
import { gatewayBinding } from './sandbox-gateway.fixture';

const image = `registry.example.test/chat@sha256:${'a'.repeat(64)}`;
const identity = { roleArn: 'arn:aws:iam::000000000000:role/chat-supervisor', tokenFile: '/var/run/secrets/chat-supervisor/token', region: 'us-east-1' };
const load = jest.fn(() => async () => ({ accessKeyId: 'TESTKEY', secretAccessKey: 'TESTSECRET', sessionToken: 'TESTSESSION' })) as unknown as typeof fromTokenFile;
const assignment = (runId: string) => registeredSandboxAssignment(JSON.stringify({ message_id: runId, session_id: 'session-a',
  task_id: `task-${runId}`, session_generation: 1, session_mode: 'persistent' }), image, 'https://gateway.example.test');
const pod = (runId: string) => ({ name: sandboxCreationName(runId), uid: `pod-uid-${runId}` });
const scope = (runId: string) => ({ run_id: runId, session_id: 'session-a', task_id: `task-${runId}`, session_generation: 1 });
const reserved = (runId: string) => ({ ...scope(runId), state: 'creation_reserved', session_mode: 'persistent',
  pod_name: pod(runId).name, attempt: 1, image_digest: image.split('@')[1] });
const admitted = (runId: string) => ({ ...reserved(runId), state: 'admitted', sandbox_uid: pod(runId).uid,
  lease_generation: 1, session_run_id: runId, recovery_required: true });
const terminal = (runId: string, interrupted: boolean) => ({ run_id: runId, session_id: 'session-a', attempt: 1, lease_generation: 1,
  sandbox_uid: pod(runId).uid, outcome: interrupted ? 'interrupted' : 'completed', message_id: interrupted ? null : 'reply',
  retryable: false, automatic_replay_permitted: false, accounting_status: interrupted ? 'unresolved' : 'settled', terminal: true, finalized_at: 100 });
const completed = (runId: string, interrupted: boolean) => ({ ...scope(runId), attempt: 1, lease_generation: 1, sandbox_uid: pod(runId).uid,
  processing_lock_released: true, input_acknowledgement_ready: true, completed_at: 100,
  delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(terminal(runId, interrupted))).digest('hex')}` });

test.each([
  { ...reserved('run-a'), pod_name: pod('foreign').name },
  { ...reserved('run-a'), sandbox_uid: 'invalid/uid' },
  { ...reserved('run-a'), lease_generation: 1 },
  { ...reserved('run-a'), image_digest: `sha256:${'b'.repeat(64)}` },
  { ...reserved('run-a'), session_mode: 'ephemeral' },
  { ...reserved('run-a'), attempt: 0 },
  { ...admitted('run-a'), recovery_required: false },
  { ...scope('run-a'), state: 'session_recovery', recovery: { ...scope('run-a'), envelope_digest: 'a'.repeat(64), image_digest: image.split('@')[1] } },
  { ...scope('run-a'), state: 'session_recovery', recovery: { ...scope('run-b'), session_id: 'foreign', envelope_digest: 'a'.repeat(64), image_digest: image.split('@')[1] } },
])('rejects substituted recovery bindings %#', async receipt => {
  const send = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response(JSON.stringify(receipt))) as jest.MockedFunction<typeof fetch>;
  await expect(restoreSupervisorTurn(assignment('run-a'), identity, send, load, {})).rejects.toThrow();
});

test.each(['creation_reserved', 'creation_bound', 'admitted', 'session_recovery'])('reconciles %s without replaying the expired turn', async state => {
  const events: string[] = [];
  let previousCompleted = false;
  const target = state === 'session_recovery' ? 'run-b' : 'run-a';
  const api: SandboxPodApi = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async template => {
      const runId = template.metadata.name === pod('run-a').name ? 'run-a' : 'run-b';
      events.push(`${runId}:create`);
      return { metadata: { ...template.metadata, ...pod(runId) }, spec: template.spec };
    }),
    remove: jest.fn(async name => { events.push(`${name}:remove`); }),
  };
  const send = jest.fn(async (url, init) => {
    const request = JSON.parse(init!.body as string);
    const runId = request.run_id;
    const operation = String(url).split('/').pop()!;
    events.push(`${runId}:${operation}`);
    let response: unknown;
    if (operation === 'resume') {
      response = state === 'creation_bound' ? { ...reserved(runId), sandbox_uid: pod(runId).uid } :
        state === 'creation_reserved' || (runId === 'run-b' && previousCompleted) ? reserved(runId) :
        runId === 'run-b' ? { ...scope(runId), state: 'session_recovery', recovery: { ...scope('run-a'),
          envelope_digest: assignment('run-a').envelopeDigest, image_digest: image.split('@')[1] } } : admitted(runId);
    } else if (operation === 'admit') response = { run_id: runId, session_id: 'session-a', lease_generation: 1, session_mode: 'persistent' };
    else if (operation === 'exit') response = { run_id: runId, pod_uid: pod(runId).uid, terminated: true };
    else if (operation === 'teardown') response = { run_id: runId, pod_uid: pod(runId).uid, removed: true };
    else if (operation === 'finalize') response = terminal(runId, true);
    else if (operation === 'complete') { previousCompleted = true; response = completed(runId, true); }
    else if (operation === 'commit') response = { terminal: terminal(runId, false), completion: completed(runId, false) };
    else throw new Error(`Unexpected ${operation}`);
    return new Response(JSON.stringify(response));
  }) as jest.MockedFunction<typeof fetch>;
  await reconcileSupervisorDelivery(assignment(target), api, identity, send, load, {});
  expect(events).not.toContain('run-a:reserve');
  if (state === 'creation_bound') {
    expect(api.create).not.toHaveBeenCalled();
    expect(events).toContain('run-a:admit');
  }
  if (!['creation_reserved', 'creation_bound'].includes(state)) {
    expect(events).not.toContain('run-a:create');
    expect(events).not.toContain('run-a:admit');
    expect(events).not.toContain('run-a:commit');
    expect(events.indexOf(`${pod('run-a').name}:remove`)).toBeLessThan(events.indexOf('run-a:exit'));
    expect(events).toContain('run-a:complete');
  }
  if (state === 'session_recovery') expect(events.indexOf('run-a:complete')).toBeLessThan(events.indexOf('run-b:create'));
});
