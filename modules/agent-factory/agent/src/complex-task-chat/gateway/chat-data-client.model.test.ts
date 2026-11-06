import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { ChatDataClient } from './chat-data-client';
import { policyBody } from '../../model-policy-body';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const keys = generateKeyPairSync('ed25519');
const binding = {
  capability: 'synthetic.scoped.capability', run_id: 'run-a', session_id: 'session-a',
  attempt: 1, lease_generation: 1, expires_at: NOW / 1000 + 300,
};
const model = 'global.anthropic.claude-sonnet-5';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function signedReply(nonce: string, changes: Record<string, unknown> = {}) {
  const result = {
    nonce, invocation_id: 'run-a', tenant_id: 'tenant-a', attempt: 1, context: { lease_generation: 1 },
    model_policy: {
      posture: 'enforcing', posture_verified: true, status: 'proposed',
      decision: { runtime_posture: 'enforcing', invocation_id: 'run-a', tenant_id: 'tenant-a',
        principal_kind: 'human', principal_id: 'human', resolved_model_id: model },
      assertion: 'synthetic-inner-assertion',
    },
    ...changes,
  };
  const iso = (offset: number) => new Date(NOW + offset).toISOString().replace('.000Z', 'Z');
  const payload = {
    v: 'adpe1', alg: 'ed25519', kid: 'test', iss: 'adp-gateway-control', aud: 'adp-agent-model-policy',
    tenant_id: 'tenant-a', principal: 'run-a#1', target_run_id: 'run-a', target_generation: 1,
    action: 'model_policy_response', command_id: nonce, grant_id: 'grant-a', revocation_epoch: 1,
    body_digest: createHash('sha256').update(policyBody(result)).digest('hex'),
    iat: iso(0), nbf: iso(0), exp: iso(30000),
  };
  const body = Buffer.from(JSON.stringify(payload));
  const signature = sign(null, Buffer.concat([Buffer.from('adpe1.'), body]), keys.privateKey);
  return { result, assertion: `adpe1.${body.toString('base64url')}.${signature.toString('base64url')}` };
}

const publishedKeys = { keys: { test: keys.publicKey.export({ type: 'spki', format: 'pem' }) } };

describe('scoped model decision transport', () => {
  let fetchMock: jest.SpiedFunction<typeof fetch>;
  let workloadToken: jest.Mock<Promise<string>, []>;
  let client: ChatDataClient;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    fetchMock = jest.spyOn(globalThis, 'fetch');
    workloadToken = jest.fn(async () => 'sandbox.workload.token');
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken });
  });

  afterEach(() => jest.restoreAllMocks());

  function approved(changes: Record<string, unknown> = {}) {
    fetchMock.mockResolvedValueOnce(json(binding)).mockImplementationOnce(async (_url, options) => {
      const body = JSON.parse(options!.body as string);
      expect(body).toEqual({ run_id: 'run-a', session_id: 'session-a', nonce: expect.stringMatching(/^[a-f0-9]{64}$/), model_policy_contract: 1 });
      return json(signedReply(body.nonce, changes));
    }).mockResolvedValueOnce(json(publishedKeys));
  }

  it('derives run and generation only from the exchange, then verifies the signed model selection', async () => {
    approved();
    await expect(client.modelDecision()).resolves.toEqual({ modelId: model, runId: 'run-a', tenantId: 'tenant-a', userId: 'human', generation: 1,
      posture: 'enforcing', postureVerified: true });
    expect(fetchMock.mock.calls[1]).toEqual(['https://gateway.example.test/v1/chat/model/decision', expect.objectContaining({
      redirect: 'error', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic.scoped.capability',
        'X-Adp-Workload-Token': 'sandbox.workload.token' },
    })]);
    expect(fetchMock.mock.calls[2][0]).toBe('https://gateway.example.test/v1/chat/model/keys');
    expect(workloadToken).toHaveBeenCalledTimes(2);
  });

  it.each([
    [{ attempt: 2 }, 'denied'],
    [{ tenant_id: 'tenant-b' }, 'denied'],
    [{ invocation_id: 'run-b' }, 'denied'],
    [{ context: { lease_generation: 2 } }, 'denied'],
    [{ model_policy: { posture: 'report_only', posture_verified: true, status: 'proposed', decision: {} } }, 'invalid_response'],
  ])('rejects signed scope or policy substitution (%j)', async (changes, code) => {
    approved(changes);
    await expect(client.modelDecision()).rejects.toMatchObject({ code });
  });

  // Chat model-policy posture (#7997): advisory and off decisions are decisions; the
  // sandbox runs the resolved model and surfaces the posture, verification and reason.
  const decisionUnder = (runtime_posture: string) => ({ runtime_posture, invocation_id: 'run-a', tenant_id: 'tenant-a',
    principal_kind: 'human', principal_id: 'human', resolved_model_id: model });

  it.each([
    ['advisory', 'report_only', false, 'probing_disabled'],
    ['advisory', 'report_only', false, 'evidence_stale'],
    ['advisory', 'report_only', true, undefined],
    ['off', 'disabled', true, undefined],
    ['off', 'disabled', false, 'probing_disabled'],
  ])('accepts a %s decision (runtime %s, verified %s, reason %s) and reports the posture', async (posture, runtime, verified, reason) => {
    approved({ model_policy: { posture, posture_verified: verified, status: 'proposed', assertion: 'synthetic-inner-assertion',
      ...(reason ? { rejection_reason: reason } : {}), decision: decisionUnder(runtime) } });
    await expect(client.modelDecision()).resolves.toEqual({ modelId: model, runId: 'run-a', tenantId: 'tenant-a', userId: 'human', generation: 1,
      posture, postureVerified: verified, ...(reason ? { rejectionReason: reason } : {}) });
  });

  it.each([
    ['an unverified enforcing decision', { posture: 'enforcing', posture_verified: false, status: 'proposed', decision: decisionUnder('enforcing') }],
    ['an advisory decision whose hop claims to be enforcing', { posture: 'advisory', posture_verified: true, status: 'proposed', decision: decisionUnder('enforcing') }],
    ['an off decision whose hop claims report_only', { posture: 'off', posture_verified: true, status: 'proposed', decision: decisionUnder('report_only') }],
    ['an advisory refusal dressed as a proposal', { posture: 'advisory', posture_verified: false, status: 'unavailable', reason: 'probing_disabled', decision: decisionUnder('report_only') }],
    ['an advisory decision with free-text reason', { posture: 'advisory', posture_verified: false, status: 'proposed', rejection_reason: 'Traceback at db', decision: decisionUnder('report_only') }],
    ['an unknown posture', { posture: 'permissive', posture_verified: true, status: 'proposed', decision: decisionUnder('disabled') }],
  ])('refuses %s', async (_label, model_policy) => {
    approved({ model_policy });
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('refuses a signed decision without a human owner', async () => {
    approved({ model_policy: { posture: 'enforcing', posture_verified: true, status: 'proposed',
      decision: { runtime_posture: 'enforcing', invocation_id: 'run-a', tenant_id: 'tenant-a',
        principal_kind: 'service_account', principal_id: 'another-owner', resolved_model_id: model } } });
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('accepts a second session lease bound to a fresh run attempt', async () => {
    const nextLease = { ...binding, lease_generation: 2 };
    fetchMock.mockResolvedValueOnce(json(nextLease)).mockImplementationOnce(async (_url, options) =>
      json(signedReply(JSON.parse(options!.body as string).nonce, { context: { lease_generation: 2 } })))
      .mockResolvedValueOnce(json(publishedKeys));
    await expect(client.modelDecision()).resolves.toEqual({ modelId: model, runId: 'run-a', tenantId: 'tenant-a', userId: 'human', generation: 2,
      posture: 'enforcing', postureVerified: true });
  });

  it('refuses an absent signature and a wrong verification key', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ result: signedReply('a'.repeat(64)).result }));
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    fetchMock.mockReset();
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken });
    fetchMock.mockResolvedValueOnce(json(binding)).mockImplementationOnce(async (_url, options) =>
      json(signedReply(JSON.parse(options!.body as string).nonce)))
      .mockResolvedValueOnce(json({ keys: { other: publishedKeys.keys.test } }));
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'denied' });
  });

  it('refuses a bootstrap without lease generation before any model request', async () => {
    const { lease_generation: _unused, ...oldBinding } = binding;
    fetchMock.mockResolvedValueOnce(json(oldBinding));
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('refuses a bootstrap without run attempt before any model request', async () => {
    const { attempt: _unused, ...oldBinding } = binding;
    fetchMock.mockResolvedValueOnce(json(oldBinding));
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not select an older generation after refreshing a capability', async () => {
    approved();
    await client.modelDecision();
    jest.spyOn(Date, 'now').mockReturnValue(NOW + 280_000);
    fetchMock.mockResolvedValueOnce(json({ ...binding, lease_generation: 2, expires_at: NOW / 1000 + 580 }));
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('cancels an in-flight model readiness request without contacting a second route', async () => {
    fetchMock.mockResolvedValueOnce(json(binding));
    await client.sessionScope();
    const cancellation = new AbortController();
    fetchMock.mockImplementationOnce(async (_url, options) => {
      expect(options?.signal).toBeDefined();
      cancellation.abort();
      options!.signal!.throwIfAborted();
      throw new Error('unexpected request');
    });
    await expect(client.modelDecision(cancellation.signal)).rejects.toMatchObject({ code: 'unavailable' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not send a model request when the sandbox workload token disappears', async () => {
    workloadToken.mockResolvedValueOnce('sandbox.workload.token').mockRejectedValue(new Error('missing'));
    fetchMock.mockResolvedValueOnce(json(binding));
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'unavailable' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('refused or unavailable model-policy decision body (#7968)', () => {
  let fetchMock: jest.SpiedFunction<typeof fetch>;
  let client: ChatDataClient;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    fetchMock = jest.spyOn(globalThis, 'fetch');
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'sandbox.workload.token' });
  });
  afterEach(() => jest.restoreAllMocks());

  it('exposes the gateway reason, persona, model and retry hint on the unavailable error', async () => {
    // The binding wrapper re-requests one unavailable answer before giving up; the refusal
    // the sandbox finally sees is the gateway's own structured body, not an anonymous outage.
    const detail = { error: 'chat_model_policy_unavailable', reason: 'probing_disabled', persona: 'developer', model_id: null, retryable: false };
    fetchMock.mockResolvedValueOnce(json(binding)).mockImplementation(async () => json({ detail }, 503));
    await expect(client.modelDecision()).rejects.toMatchObject({ code: 'unavailable', status: 503, policy: detail });
    expect(fetchMock.mock.calls.slice(1).every(([url]) => String(url).endsWith('/v1/chat/model/decision'))).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each([
    { detail: { error: 'chat_authority_unavailable' } },
    { detail: { error: 'chat_model_policy_unavailable', reason: 'Traceback at db-internal', retryable: false } },
    { detail: { error: 'chat_model_policy_unavailable', reason: 'probing_disabled', retryable: 'yes' } },
    { detail: { error: 'chat_model_policy_unavailable', reason: 'probing_disabled', retryable: false, token: 'leak' } },
    'not json',
  ])('keeps an unstructured or malformed 503 as a plain outage: %j', async body => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockImplementation(async () => typeof body === 'string'
      ? new Response(body, { status: 503, headers: { 'Content-Type': 'text/plain' } }) : json(body, 503));
    const failure = await client.modelDecision().catch(error => error);
    expect(failure).toMatchObject({ code: 'unavailable', status: 503 });
    expect(failure.policy).toBeUndefined();
  });
});
