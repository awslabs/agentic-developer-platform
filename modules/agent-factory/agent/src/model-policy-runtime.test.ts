import { generateKeyPairSync, sign, createHash } from 'node:crypto';
import { query } from '@anthropic-ai/claude-agent-sdk';
import { createPolicyQuery, policyBody, ModelPolicyRefused } from './model-policy-runtime';
import { workerAwsCredentialProvider } from './lib/runIdentity';
import { resilientQuery } from './utils/resilientQuery';

jest.mock('@anthropic-ai/claude-agent-sdk', () => ({ query: jest.fn() }));
jest.mock('./lib/runIdentity', () => ({
  workerAwsCredentialProvider: jest.fn(async () => async () => ({ accessKeyId: 'test', secretAccessKey: 'test', sessionToken: 'test' })),
  gatewaySigningRegion: () => 'us-east-1',
  workerIdentityHeaders: () => ({ 'X-Adp-Run-Credential': 'test-run', 'X-Adp-Workload-Token': 'test-pod' }),
}));

const keys = generateKeyPairSync('ed25519');
const originalEnv = process.env;
const originalFetch = global.fetch;
const legacy = { prompt: 'test', options: { model: 'legacy-model', fallbackModel: 'fallback-model' } };
const model = 'gateway-model';
let responsePolicy: any;
let mutate: ((doc: any) => void) | undefined;
let nonceOverride: string | undefined;

function signedReply(nonce: string, policy = responsePolicy) {
  const result = { nonce, invocation_id: 'run-a', tenant_id: 'tenant-a', attempt: 2, model_policy: policy };
  const now = Math.floor(Date.now() / 1000) * 1000;
  const iso = (offset: number) => new Date(now + offset).toISOString().replace('.000Z', 'Z');
  const payload = { v: 'adpe1', alg: 'ed25519', kid: 'test', iss: 'adp-gateway-control', aud: 'adp-agent-model-policy',
    tenant_id: 'tenant-a', principal: 'run-a#2', target_run_id: 'run-a', target_generation: 2,
    action: 'model_policy_response', command_id: nonce, grant_id: 'grant-a', revocation_epoch: 1,
    body_digest: createHash('sha256').update(policyBody(result)).digest('hex'),
    iat: iso(0), nbf: iso(0), exp: iso(30000) };
  const bytes = Buffer.from(JSON.stringify(payload));
  const signature = sign(null, Buffer.concat([Buffer.from('adpe1.'), bytes]), keys.privateKey);
  return { result, assertion: `adpe1.${bytes.toString('base64url')}.${signature.toString('base64url')}` };
}

function policy(posture: string) {
  return { posture, posture_verified: true, status: 'proposed', decision: { schema_version: 1,
    invocation_id: 'run-a', tenant_id: 'tenant-a', persona: 'developer', runtime_posture: posture,
    resolved_model_id: model } };
}

beforeEach(() => {
  jest.clearAllMocks();
  process.env = { ...originalEnv, ADP_AGENT_AUTHORITY_ENABLED: 'true', ADP_MESSAGE_ID: 'run-a',
    ADP_TENANT_ID: 'tenant-a', ADP_RUN_ATTEMPT: '2', AGENT_TYPE: 'developer',
    ADP_AGENT_CONTROL_ENDPOINT: 'https://api123.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent',
    ADP_CONTROL_ENVELOPE_KEYS: JSON.stringify({ test: keys.publicKey.export({ type: 'spki', format: 'pem' }) }) };
  delete process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE;
  responsePolicy = policy('enforcing'); mutate = undefined; nonceOverride = undefined;
  global.fetch = jest.fn(async (_url, init) => {
    const request = JSON.parse(init!.body as string);
    expect(Object.keys(request).sort()).toEqual(['model_policy_contract', 'nonce']);
    const doc = signedReply(nonceOverride ?? request.nonce);
    mutate?.(doc);
    return new Response(JSON.stringify(doc));
  });
  (query as jest.Mock).mockImplementation(() => ({ async *[Symbol.asyncIterator]() { yield { type: 'result', subtype: 'success' }; }, close: jest.fn() }));
});

afterEach(() => { process.env = originalEnv; global.fetch = originalFetch; });

it('reaches the real SDK boundary with the gateway model and no fallback', async () => {
  const session = await createPolicyQuery(legacy);
  for await (const _ of session) { /* consume mocked SDK */ }
  const options = (query as jest.Mock).mock.calls[0][0].options;
  expect(options.model).toBe(model);
  expect(options.env.ANTHROPIC_MODEL).toBe(model);
  expect(options.fallbackModel).toBeUndefined();
  expect(legacy.options.model).toBe('legacy-model');
});

it.each(['report_only', 'disabled'])('preserves exact legacy options under verified %s', async posture => {
  responsePolicy = { posture, posture_verified: true, status: 'unavailable', reason: 'snapshot_missing' };
  await createPolicyQuery(legacy);
  expect(query).toHaveBeenCalledWith(legacy);
});

it.each(['report_only', 'disabled', undefined, 'future'])('refuses an unsigned posture change to %s before query', async posture => {
  mutate = doc => { doc.result.model_policy.posture = posture; };
  await expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(query).not.toHaveBeenCalled();
});

it('refuses a signed response replayed from another launch', async () => {
  nonceOverride = '0'.repeat(64);
  await expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(query).not.toHaveBeenCalled();
});

it('refuses a missing proof even when environment telemetry claims report-only', async () => {
  process.env.ADP_MODEL_POLICY_POSTURE = 'report_only';
  mutate = doc => { delete doc.assertion; };
  await expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(query).not.toHaveBeenCalled();
});

it('obtains another decision on a real SDK retry and observes rollback', async () => {
  (query as jest.Mock).mockImplementationOnce(() => ({
    async *[Symbol.asyncIterator]() { responsePolicy = policy('report_only'); throw new Error('rate limit'); yield undefined; }, close: jest.fn(),
  }));
  for await (const _ of resilientQuery({ queryParams: legacy, maxRetries: 1, baseDelayMs: 1, maxDelayMs: 1, log: () => {} })) { /* consume */ }
  expect(fetch).toHaveBeenCalledTimes(2);
  expect((query as jest.Mock).mock.calls.map(call => call[0].options.model)).toEqual([model, 'legacy-model']);
});

it('does not retry a policy refusal as an SDK transient error', async () => {
  mutate = doc => { doc.result.model_policy.status = 'unavailable'; };
  await expect((async () => {
    for await (const _ of resilientQuery({ queryParams: legacy, maxRetries: 5, baseDelayMs: 1, log: () => {} })) { /* consume */ }
  })()).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(query).not.toHaveBeenCalled();
});

it('bounds a hung credential provider and never launches after it eventually resolves', async () => {
  jest.useFakeTimers();
  let finish!: (value: any) => void;
  (workerAwsCredentialProvider as jest.Mock).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  try {
    const launch = expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
    await jest.advanceTimersByTimeAsync(10000);
    await launch;
    finish(async () => ({ accessKeyId: 'test', secretAccessKey: 'test', sessionToken: 'test' }));
    await jest.advanceTimersByTimeAsync(1);
    expect(fetch).not.toHaveBeenCalled();
    expect(query).not.toHaveBeenCalled();
  } finally { jest.useRealTimers(); }
});

it('fails closed against a gateway without the SDK decision endpoint', async () => {
  global.fetch = jest.fn(async () => new Response('', { status: 404 }));
  await expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(query).not.toHaveBeenCalled();
});
