import { once } from 'events';
import { generateKeyPairSync, sign, createHash } from 'node:crypto';
import { query } from '@anthropic-ai/claude-agent-sdk';
import { createPolicyQuery, policyBody, ModelPolicyRefused } from './model-policy-runtime';
import { workerAwsCredentialProvider } from './lib/runIdentity';
import { resilientQuery } from './utils/resilientQuery';

jest.mock('@anthropic-ai/claude-agent-sdk', () => ({ query: jest.fn() }));
jest.mock('./lib/runIdentity', () => ({
  workerAwsCredentialProvider: jest.fn(async () => async () => ({ accessKeyId: 'test', secretAccessKey: 'test', sessionToken: 'test' })),
  gatewaySigningRegion: () => 'us-east-1',
  readIdentityToken: jest.fn(() => 'test-pod'),
  workerIdentityHeaders: () => ({ 'X-Adp-Run-Credential': 'test-run', 'X-Adp-Workload-Token': 'test-pod' }),
}));

const keys = generateKeyPairSync('ed25519');
const originalEnv = process.env;
const originalFetch = global.fetch;
const legacy = { prompt: 'test', options: { model: 'legacy-model', fallbackModel: 'fallback-model', env: { ANTHROPIC_CUSTOM_HEADERS: 'X-Existing: retained' } } };
const model = 'gateway-model';
let responsePolicy: any;
let mutate: ((doc: any) => void) | undefined;
let nonceOverride: string | undefined;

function signedReply(nonce: string, policy = responsePolicy, context?: any) {
  const result = { nonce, invocation_id: 'run-a', tenant_id: 'tenant-a', attempt: 2, model_policy: policy, ...(context ? { context } : {}) };
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
    invocation_id: 'run-a', tenant_id: 'tenant-a', persona: 'developer', runtime_posture: posture, compatibility_class: 'claude-agent-sdk', harness_contract_revision: '0.3.283',
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
    expect(Object.keys(request).sort()).toEqual(process.env.ADP_MODEL_POLICY_SOURCE === 'chat'
      ? ['envelope_digest', 'invocation_id', 'model_policy_contract', 'nonce'] : ['model_policy_contract', 'nonce']);
    const doc = signedReply(nonceOverride ?? request.nonce);
    mutate?.(doc);
    return new Response(JSON.stringify(doc));
  });
  (query as jest.Mock).mockImplementation(() => ({ async *[Symbol.asyncIterator]() { yield { type: 'result', subtype: 'success' }; }, close: jest.fn() }));
});

afterEach(() => { process.env = originalEnv; global.fetch = originalFetch; });

function expectLegacyCall(index = 0) {
  const actual = (query as jest.Mock).mock.calls[index][0];
  expect(actual.prompt).toBe(legacy.prompt);
  expect(actual.options.model).toBe(legacy.options.model);
  expect(actual.options.fallbackModel).toBe(legacy.options.fallbackModel);
  expect(actual.options.env.ANTHROPIC_CUSTOM_HEADERS).toMatch(/^X-Existing: retained\nX-Adp-Model-Evidence: [0-9a-f]{64}$/);
  expect(legacy.options.env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Existing: retained');
}

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
  expectLegacyCall();
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
  const headers = (query as jest.Mock).mock.calls.map(call => call[0].options.env.ANTHROPIC_CUSTOM_HEADERS);
  expect(headers[0]).not.toBe(headers[1]);
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

it('binds a chat SDK launch to its registered envelope and pod proof', async () => {
  process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
  process.env.ADP_CHAT_MODEL_POLICY_ENABLED = 'true';
  process.env.ADP_MODEL_POLICY_SOURCE = 'chat';
  process.env.ADP_AGENT_CONTROL_ENDPOINT += '/chat';
  process.env.ADP_MODEL_ROOT_ENVELOPE_DIGEST = 'a'.repeat(64);
  await createPolicyQuery(legacy);
  const init = (fetch as jest.Mock).mock.calls[0][1];
  expect(JSON.parse(init.body)).toMatchObject({ invocation_id: 'run-a', envelope_digest: 'a'.repeat(64) });
  expect(init.headers['X-Adp-Workload-Token']).toBe('test-pod');
  expect(init.headers['X-Adp-Run-Credential']).toBeUndefined();
  expect((query as jest.Mock).mock.calls[0][0].options.model).toBe(model);
});

it('cannot launch chat without the registered envelope digest', async () => {
  process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
  process.env.ADP_CHAT_MODEL_POLICY_ENABLED = 'true';
  process.env.ADP_MODEL_POLICY_SOURCE = 'chat';
  process.env.ADP_AGENT_CONTROL_ENDPOINT += '/chat';
  delete process.env.ADP_MODEL_ROOT_ENVELOPE_DIGEST;
  await expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(fetch).not.toHaveBeenCalled();
  expect(query).not.toHaveBeenCalled();
});

function setupArc() {
  process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
  process.env.ADP_ARC_MODEL_POLICY_ENABLED = 'true';
  process.env.ADP_AGENT_CONTROL_ENDPOINT += '/arc';
  process.env.AWS_ROLE_ARN = 'arn:aws:iam::123456789012:role/runner';
  process.env.AWS_WEB_IDENTITY_TOKEN_FILE = '/tmp/arc-token';
  process.env.ACTIONS_ID_TOKEN_REQUEST_URL = 'https://pipelines.actions.githubusercontent.com/oidc?api-version=2.0';
  process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN = 'github-request-token';
  process.env.GITHUB_REPOSITORY_ID = '123';
  process.env.GITHUB_RUN_ID = '456';
  process.env.GITHUB_RUN_ATTEMPT = '1';
  process.env.GITHUB_WORKFLOW_REF = 'org/repo/.github/workflows/agent-developer.yml@refs/heads/main';
  const context = { persona: 'developer', github_actions: { repository_id: '123', run_id: '456', run_attempt: '1', workflow_ref: process.env.GITHUB_WORKFLOW_REF } };
  global.fetch = jest.fn(async (url, init) => {
    if (new URL(String(url)).hostname === 'pipelines.actions.githubusercontent.com') {
      expect(new URL(String(url)).searchParams.get('audience')).toBe('adp-agent-model-policy');
      expect(init?.headers).toEqual({ Authorization: 'Bearer github-request-token' });
      return new Response(JSON.stringify({ value: 'github-oidc' }));
    }
    const request = JSON.parse(init!.body as string);
    expect(Object.keys(request).sort()).toEqual(['github_oidc_token', 'model_policy_contract', 'nonce']);
    const proof = JSON.parse(Buffer.from((init!.headers as any)['X-Adp-Producer-Proof'], 'base64').toString());
    expect(proof['x-adp-work-invocation']).toBe(createHash('sha256').update(policyBody(request)).digest('hex'));
    expect(proof.authorization).toContain('x-adp-work-invocation;');
    expect(proof.authorization).not.toContain('x-amz-content-sha256;');
    const doc = signedReply(request.nonce, responsePolicy, context);
    mutate?.(doc);
    return new Response(JSON.stringify(doc));
  });
  return context;
}

it('binds ARC OIDC and temporary IRSA proof before consuming a signed SDK model', async () => {
  setupArc();
  await createPolicyQuery(legacy);
  expect(query).toHaveBeenCalledTimes(1);
  expect((query as jest.Mock).mock.calls[0][0].options.model).toBe(model);
});

it('ARC obtains a new identity and decision for retries and preserves report-only legacy options', async () => {
  setupArc();
  responsePolicy = policy('report_only');
  await createPolicyQuery(legacy);
  await createPolicyQuery(legacy);
  expect(fetch).toHaveBeenCalledTimes(4);
  expectLegacyCall(0);
  expectLegacyCall(1);
});

it('refuses signed ARC authority from another workflow even in report-only', async () => {
  setupArc().github_actions.run_id = '999';
  responsePolicy = policy('report_only');
  await expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(query).not.toHaveBeenCalled();
});

it('does not send a GitHub request credential to an arbitrary host', async () => {
  setupArc();
  process.env.ACTIONS_ID_TOKEN_REQUEST_URL = 'https://attacker.example/oidc';
  await expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(fetch).not.toHaveBeenCalled();
  expect(query).not.toHaveBeenCalled();
});

it('discovers only public verification keys at the configured gateway origin for chat', async () => {
  process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
  process.env.ADP_CHAT_MODEL_POLICY_ENABLED = 'true';
  process.env.ADP_MODEL_POLICY_SOURCE = 'chat';
  process.env.ADP_AGENT_CONTROL_ENDPOINT += '/chat';
  process.env.ADP_MODEL_ROOT_ENVELOPE_DIGEST = 'a'.repeat(64);
  delete process.env.ADP_CONTROL_ENVELOPE_KEYS;
  const normalFetch = global.fetch;
  global.fetch = jest.fn(async (url, init) => String(url).endsWith('/model-policy-keys')
    ? new Response(JSON.stringify({ keys: { test: keys.publicKey.export({ type: 'spki', format: 'pem' }) } }))
    : normalFetch(url, init));
  await createPolicyQuery(legacy);
  expect((query as jest.Mock).mock.calls[0][0].options.model).toBe(model);
  expect((fetch as jest.Mock).mock.calls[1][0].href).toBe('https://api123.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent/model-policy-keys');
});

it.each([{ compatibility_class: 'codex-sdk' }, { harness_contract_revision: 'unsupported' }])('refuses a signed enforcing choice for a different actual harness: %j', async change => {
  responsePolicy.decision = { ...responsePolicy.decision, ...change };
  await expect(createPolicyQuery(legacy)).rejects.toBeInstanceOf(ModelPolicyRefused);
  expect(query).not.toHaveBeenCalled();
});


it('replaces prior evidence headers while preserving unrelated SDK headers', async () => {
  responsePolicy = policy('report_only');
  const params = { ...legacy, options: { ...legacy.options, env: { ANTHROPIC_CUSTOM_HEADERS:
    'X-Existing: retained\nx-adp-model-evidence: old\nX-Adp-Model-Evidence: stale' } } };
  await createPolicyQuery(params);
  const headers = (query as jest.Mock).mock.calls[0][0].options.env.ANTHROPIC_CUSTOM_HEADERS;
  expect(headers).toMatch(/^X-Existing: retained\nX-Adp-Model-Evidence: [0-9a-f]{64}$/);
});

it('emits fresh shadow selection at each SDK admission and joins the issued usage nonce', async () => {
  const logs = jest.spyOn(console, 'info').mockImplementation(() => {});
  try {
    process.env.ADP_DISPATCH_CHANNEL = 'github';
    process.env.ADP_DISPATCH_TRIGGER = 'mention';
    responsePolicy = policy('report_only');
    responsePolicy.decision = { ...responsePolicy.decision, principal_kind: 'service_account', principal_id: 'canonical-service',
      policy_revision: 'policy-1', posture_revision: 3, snapshot_digest: 'a'.repeat(64), resolution_source: 'principal-mapping' };
    await createPolicyQuery(legacy);
    responsePolicy.decision.posture_revision = 4;
    await createPolicyQuery(legacy);
    const events = logs.mock.calls.filter(call => typeof call[0] === 'string' && call[0].startsWith('PMM09_MODEL_SHADOW '))
      .map(call => JSON.parse(call[0].slice('PMM09_MODEL_SHADOW '.length)));
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ phase: 'sdk_admission', principal_kind: 'service_account', principal_id: 'canonical-service',
      legacy_model: 'legacy-model', actual_model: 'legacy-model', proposed_model: model, runtime_posture: 'report_only', posture_revision: 3,
      channel: 'github', trigger: 'mention' });
    expect(events[1].posture_revision).toBe(4);
    expect(events[0].model_decision_id).not.toBe(events[1].model_decision_id);
    events.forEach((event, index) => expect((query as jest.Mock).mock.calls[index][0].options.env.ANTHROPIC_CUSTOM_HEADERS)
      .toContain(`X-Adp-Model-Evidence: ${event.model_decision_id}`));
    expect(JSON.stringify(events)).not.toContain('test-run');
  } finally { logs.mockRestore(); }
});

it.each(['disabled', 'enforcing'])('does not emit report-only shadow data under %s', async posture => {
  const logs = jest.spyOn(console, 'info').mockImplementation(() => {});
  try {
    responsePolicy = policy(posture);
    await createPolicyQuery(legacy);
    expect(logs.mock.calls.some(call => String(call[0]).startsWith('PMM09_MODEL_SHADOW '))).toBe(false);
  } finally { logs.mockRestore(); }
});

it('keeps App keys out of real child processes after policy env merges and retries', async () => {
  const { spawnSdkWithoutAppKey } = await import('./github-runtime-auth');
  process.env.GH_APP_PRIVATE_KEY = 'test-only-parent-key';
  const observations: boolean[] = [];
  let attempts = 0;
  (query as jest.Mock).mockImplementation((params: any) => ({
    async *[Symbol.asyncIterator]() {
      const child = params.options.spawnClaudeCodeProcess({
        command: process.execPath,
        args: ['-e', 'process.stdout.write(String(!(process.env.GH_APP_PRIVATE_KEY || process.env.GH_APP_KEY)))'],
        env: params.options.env,
        signal: new AbortController().signal,
      });
      let output = ''; child.stdout.on('data', (data: Buffer) => { output += data; });
      expect((await once(child, 'close'))[0]).toBe(0);
      observations.push(output === 'true');
      if (++attempts === 1) throw new Error('rate limit');
      yield { type: 'result', subtype: 'success' };
    },
    close: jest.fn(),
  }));
  const params = { ...legacy, options: { ...legacy.options,
    env: { ...legacy.options.env, GH_APP_KEY: 'test-only-option-key' },
    spawnClaudeCodeProcess: spawnSdkWithoutAppKey,
  } };
  for await (const _ of resilientQuery({ queryParams: params, maxRetries: 1, baseDelayMs: 1, maxDelayMs: 1, log: () => {} })) { /* policy preparation on each retry */ }
  const resumed = await createPolicyQuery({ ...params, options: { ...params.options, resume: 'test-session' } });
  for await (const _ of resumed) { /* final spawn on a resumed session */ }
  expect(observations).toEqual([true, true, true]);
});
