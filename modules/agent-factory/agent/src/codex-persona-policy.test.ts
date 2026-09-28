import { generateKeyPairSync, sign, createHash } from 'node:crypto';
import { codexPersonaOperation, admitCodexPersonaModel, codexPolicyBody, CodexPersonaAdmissionRefused } from './codex-persona-policy';
import { workerAwsCredentialProvider } from './lib/runIdentity';

jest.mock('./lib/runIdentity', () => ({
  workerAwsCredentialProvider: jest.fn(async () => async () => ({ accessKeyId: 'test', secretAccessKey: 'test', sessionToken: 'test' })),
  gatewaySigningRegion: () => 'us-east-1',
  workerIdentityHeaders: () => ({ 'X-Adp-Run-Credential': 'test-run', 'X-Adp-Workload-Token': 'test-pod' }),
}));
const keys = generateKeyPairSync('ed25519');
const originalEnv = process.env;
const originalFetch = global.fetch;
const model = 'gateway-model';
let responsePolicy: any;
let mutate: ((doc: any) => void) | undefined;
let nonceOverride: string | undefined;

function signedReply(nonce: string, policy = responsePolicy, context?: any) {
  const result = { nonce, invocation_id: 'run-a', tenant_id: 'tenant-a', attempt: 2, model_policy: policy, context: context ?? { codex_persona: { version: 1, persona: 'agent-codex-architect' } } };
  const now = Math.floor(Date.now() / 1000) * 1000;
  const iso = (offset: number) => new Date(now + offset).toISOString().replace('.000Z', 'Z');
  const payload = { v: 'adpe1', alg: 'ed25519', kid: 'test', iss: 'adp-gateway-control', aud: 'adp-agent-model-policy',
    tenant_id: 'tenant-a', principal: 'run-a#2', target_run_id: 'run-a', target_generation: 2,
    action: 'model_policy_response', command_id: nonce, grant_id: 'grant-a', revocation_epoch: 1,
    body_digest: createHash('sha256').update(codexPolicyBody(result)).digest('hex'),
    iat: iso(0), nbf: iso(0), exp: iso(30000) };
  const bytes = Buffer.from(JSON.stringify(payload));
  const signature = sign(null, Buffer.concat([Buffer.from('adpe1.'), bytes]), keys.privateKey);
  return { result, assertion: `adpe1.${bytes.toString('base64url')}.${signature.toString('base64url')}` };
}

function policy(posture: string) {
  return { posture, posture_verified: true, status: 'proposed', decision: { schema_version: 1,
    invocation_id: 'run-a', tenant_id: 'tenant-a', persona: 'agent-codex-architect', runtime_posture: posture, compatibility_class: 'codex-sdk', harness_contract_revision: '0.155.1',
    snapshot_digest: "a".repeat(64), resolved_model_id: model } };
}

beforeEach(() => {
  jest.clearAllMocks();
  process.env = { ...originalEnv, ADP_AGENT_AUTHORITY_ENABLED: 'true', ADP_MESSAGE_ID: 'run-a',
    ADP_TENANT_ID: 'tenant-a', ADP_RUN_ATTEMPT: '2', AGENT_TYPE: 'agent-codex-architect',
    ADP_AGENT_CONTROL_ENDPOINT: 'https://api123.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent',
    ADP_CONTROL_ENVELOPE_KEYS: JSON.stringify({ test: keys.publicKey.export({ type: 'spki', format: 'pem' }) }) };
  delete process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE;
  responsePolicy = policy('enforcing'); mutate = undefined; nonceOverride = undefined;
  global.fetch = jest.fn(async (_url, init) => {
    const request = JSON.parse(init!.body as string);
    expect(Object.keys(request).sort()).toEqual(process.env.ADP_MODEL_POLICY_SOURCE === 'chat'
      ? ['envelope_digest', 'invocation_id', 'model_policy_contract', 'nonce'] : ['model_policy_contract', 'nonce']);
    const doc = signedReply(nonceOverride ?? request.nonce);
    mutate?.(doc);
    return new Response(JSON.stringify(doc));
  });
});

afterEach(() => { process.env = originalEnv; global.fetch = originalFetch; });

const admit = () => admitCodexPersonaModel('agent-codex-architect', new AbortController().signal);

it('uses the signed gateway model and obtains fresh evidence for each boundary', async () => {
  const first = await admit();
  const second = await admit();
  expect(first).toMatchObject({ model, persona: 'agent-codex-architect', runId: 'run-a', generation: 2 });
  expect(first.evidenceId).not.toBe(second.evidenceId);
  expect(Object.isFrozen(first)).toBe(true);
});

it.each(['disabled', 'future'])('refuses signed %s posture for a new persona', async posture => {
  responsePolicy = policy(posture);
  await expect(admit()).rejects.toBeInstanceOf(CodexPersonaAdmissionRefused);
});

it.each([
  ['persona', 'agent-codex-developer'], ['compatibility_class', 'claude-agent-sdk'],
  ['harness_contract_revision', '0.1'], ['invocation_id', 'another-run'],
  ['tenant_id', 'another-tenant'], ['schema_version', 2], ['snapshot_digest', 'invalid'],
  ['resolved_model_id', ''],
])('refuses a signed incompatible %s', async (key, value) => {
  responsePolicy.decision[key] = value;
  await expect(admit()).rejects.toBeInstanceOf(CodexPersonaAdmissionRefused);
});

it('refuses an unsigned change even when it names an otherwise admitted model', async () => {
  mutate = doc => { doc.result.model_policy.decision.resolved_model_id = 'forged'; };
  await expect(admit()).rejects.toBeInstanceOf(CodexPersonaAdmissionRefused);
});

it('refuses replay from another boundary', async () => {
  nonceOverride = '0'.repeat(64);
  await expect(admit()).rejects.toBeInstanceOf(CodexPersonaAdmissionRefused);
});

it('does not call the gateway for legacy routes or missing authority', async () => {
  await expect(admitCodexPersonaModel('agent-codex-developer', new AbortController().signal)).rejects.toThrow();
  delete process.env.ADP_AGENT_AUTHORITY_ENABLED;
  await expect(admit()).rejects.toThrow();
  expect(fetch).not.toHaveBeenCalled();
});

it('bounds a stalled credential provider and never sends its late request', async () => {
  let release!: () => void;
  (workerAwsCredentialProvider as jest.Mock).mockImplementationOnce(() => new Promise(resolve => { release = () => resolve(async () => ({ accessKeyId: 'test', secretAccessKey: 'test' })); }));
  const controller = new AbortController();
  const pending = admitCodexPersonaModel('agent-codex-architect', controller.signal);
  controller.abort();
  await expect(pending).rejects.toThrow();
  release();
  await new Promise(resolve => setImmediate(resolve));
  expect(fetch).not.toHaveBeenCalled();
});

it('refuses oversized responses', async () => {
  global.fetch = jest.fn(async () => new Response(' '.repeat(65537)));
  await expect(admit()).rejects.toThrow();
});

it('uses an explicit new-persona session without changing legacy report-only posture', async () => {
  responsePolicy = policy('report_only');
  expect((await admit()).model).toBe(model);
});
it('rejects the legacy model endpoint response without signed persona context', async () => {
  mutate = doc => { delete doc.result.context; };
  await expect(admit()).rejects.toThrow();
});

it('operation admission bounds credential acquisition and never sends a late request', async () => {
  let release!: () => void;
  (workerAwsCredentialProvider as jest.Mock).mockImplementationOnce(() => new Promise(resolve => {
    release = () => resolve(async () => ({ accessKeyId: 'test', secretAccessKey: 'test' }));
  }));
  const controller = new AbortController();
  const pending = codexPersonaOperation({ operation_id: 'fixture', request_digest: 'a'.repeat(64), action: 'claim', kind: 'tool' }, controller.signal);
  controller.abort();
  await expect(pending).rejects.toThrow();
  release();
  await new Promise(resolve => setImmediate(resolve));
  expect(fetch).not.toHaveBeenCalled();
});
