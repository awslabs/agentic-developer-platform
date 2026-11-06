import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { chmodSync, lstatSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CrewHomeUnknownProvenanceError, bootstrapCrewHost, publishCrewHostScope, type ProvisionRequest } from './crew-host-bootstrap';

const gatewayExecuteArn = 'arn:aws:execute-api:us-east-1:000000000000:abcdefghij/prod';
const runId = '00000000-0000-4000-8000-000000000001';
const instanceRef = 'i-0123456789abcdef0';
const credentials = async () => ({ accessKeyId: 'example', secretAccessKey: 'example', sessionToken: 'temporary' });

const temporary = mkdtempSync(join(tmpdir(), 'crew-client-'));
let certificate = '';
try {
  const file = join(temporary, 'client.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
    '-subj', '/CN=crew-client', '-keyout', join(temporary, 'client.key'), '-out', file], { stdio: 'ignore' });
  certificate = readFileSync(file, 'utf8');
} finally { rmSync(temporary, { recursive: true, force: true }); }

const fingerprint = createHash('sha256').update(Buffer.from(certificate.split('-----BEGIN CERTIFICATE-----')[1]
  .split('-----END CERTIFICATE-----')[0].replace(/\s+/g, ''), 'base64')).digest('hex');
const scope = { run_id: runId, tenant_id: 'tenant', user_id: 'owner', box_id: 'box', instance_ref: instanceRef,
  private_ip: '10.2.3.4', lease_generation: 2, lease_expires_at: 4_000_000_000,
  checkpoint_id: 'checkpoint', manifest_sha256: 'a'.repeat(64), restore_manifest_version: 7,
  deployment_id: 'deployment', client_certificate: certificate, gateway_fingerprint: fingerprint,
  verification_keys: { current: 'public-key' } };

test('only public bootstrap material is readable by the agent and an existing scope cannot be replaced', () => {
  const root = mkdtempSync(join(tmpdir(), 'crew-public-'));
  try {
    chmodSync(root, 0o700);
    const trust = join(root, 'crew-host');
    mkdirSync(trust, { mode: 0o750 });
    writeFileSync(join(trust, 'server.key'), 'private-on-box', { mode: 0o640 });
    publishCrewHostScope(scope, gatewayExecuteArn, 'us.anthropic.example-v1:0', 'profile-r1', root, lstatSync(root).uid);
    expect(lstatSync(root).mode & 0o777).toBe(0o750);
    for (const file of [join(root, 'crew-host.json'), join(root, 'crew-model.json'), join(trust, 'client-ca.pem')]) {
      expect(lstatSync(file).mode & 0o777).toBe(0o640);
      expect(readFileSync(file, 'utf8')).not.toContain('PRIVATE KEY');
    }
    expect(JSON.parse(readFileSync(join(root, 'crew-host.json'), 'utf8'))).toMatchObject({
      run_id: runId, lease_generation: 2, profile_revision: 'profile-r1', gateway_fingerprint: fingerprint,
    });
    // The identical scope may be republished (a retried bootstrap within one boot, #8223); a
    // different one is still refused.
    expect(() => publishCrewHostScope(scope, gatewayExecuteArn, 'us.anthropic.example-v1:0', 'profile-r1', root,
      lstatSync(root).uid)).not.toThrow();
    expect(() => publishCrewHostScope({ ...scope, lease_generation: 3 }, gatewayExecuteArn, 'us.anthropic.example-v1:0',
      'profile-r1', root, lstatSync(root).uid)).toThrow();
    expect(() => publishCrewHostScope(scope, gatewayExecuteArn, 'us.anthropic.other-v1:0', 'profile-r1', root,
      lstatSync(root).uid)).toThrow();
  } finally { rmSync(root, { recursive: true, force: true }); }
});

const expectedScope = (overrides: Record<string, unknown> = {}) => ({
  run_id: runId, tenant_id: 'tenant', user_id: 'owner', box_id: 'box', instance_ref: instanceRef,
  private_ip: '10.2.3.4', profile_revision: 'profile-r1', lease_generation: 2,
  restore_manifest_version: 7, checkpoint_id: 'checkpoint', manifest_sha256: 'a'.repeat(64), ...overrides,
});

/**
 * A provisioner double: the first `ensure` answers `verdict` (`ready` | `resumed` | `needs_grant` | a
 * refusal for an unknown home), an `ensure` carrying a grant answers `restored`; records every request.
 */
function provisioner(verdict: string) {
  const calls: ProvisionRequest[] = [];
  const provision = jest.fn((request: ProvisionRequest) => {
    calls.push(request);
    if (verdict === 'unknown') throw new CrewHomeUnknownProvenanceError('Crew provisioning marker missing');
    return request.grant ? 'restored\n' : `${verdict}\n`;
  });
  return { provision, calls };
}

test('selected scope and grant reach the restorer, and the pin is released', async () => {
  const grant = { id: 'checkpoint', restore_token: 'a'.repeat(32), crew_version: '0.11.2', schema_version: 1,
    manifest_version: 7, objects: { archive: {}, manifest: {} } };
  const send = jest.fn(async (url) => new Response(JSON.stringify(String(url).endsWith('/context') ? scope
    : String(url).endsWith('/grant') ? grant : { id: 'checkpoint', status: 'released' }))) as unknown as typeof fetch;
  const publish = jest.fn();
  const { provision, calls } = provisioner('needs_grant');
  await expect(bootstrapCrewHost(gatewayExecuteArn, 'us.anthropic.example-v1:0', {
    credentials, send, currentInstance: async () => instanceRef, profile: () => 'profile-r1', publish, provision,
  })).resolves.toBe('restored');
  expect(publish).toHaveBeenCalledWith(scope, gatewayExecuteArn, 'us.anthropic.example-v1:0', 'profile-r1');
  expect(calls.map(call => call.mode)).toEqual(['ensure', 'ensure']);
  expect(calls[0]).toEqual({ mode: 'ensure', scope: expectedScope(), grant: null, model_id: 'us.anthropic.example-v1:0' });
  expect(calls[1]).toEqual({ mode: 'ensure', scope: expectedScope(), grant, model_id: 'us.anthropic.example-v1:0' });
  expect(jest.mocked(send).mock.calls.map(([url]) => String(url).split('/').pop())).toEqual(['context', 'grant', 'release']);
  await expect(bootstrapCrewHost(gatewayExecuteArn, 'us.anthropic.example-v1:0', {
    credentials, send, currentInstance: async () => 'i-aaaaaaaaaaaaaaaaa', profile: () => 'profile-r1', publish, provision,
  })).rejects.toThrow('identity mismatch');
  expect(publish).toHaveBeenCalledTimes(1);
  expect(provision).toHaveBeenCalledTimes(2);
});

test('a home this box provisioned before is resumed: no grant, no release, data untouched (#8223)', async () => {
  const send = jest.fn(async (url) => {
    if (!String(url).endsWith('/context')) throw new Error(`unexpected call ${url}`);
    return new Response(JSON.stringify(scope));
  }) as unknown as typeof fetch;
  const { provision, calls } = provisioner('resumed');
  await expect(bootstrapCrewHost(gatewayExecuteArn, 'us.anthropic.example-v1:0', {
    credentials, send, currentInstance: async () => instanceRef, profile: () => 'profile-r1', publish: jest.fn(), provision,
  })).resolves.toBe('resumed');
  expect(calls.map(call => call.mode)).toEqual(['ensure']);
  expect(calls[0]).toEqual({ mode: 'ensure', scope: expectedScope(), grant: null, model_id: 'us.anthropic.example-v1:0' });
  expect(jest.mocked(send).mock.calls.map(([url]) => String(url).split('/').pop())).toEqual(['context']);
});

test('an occupied home of unknown provenance is refused by name before any grant is requested', async () => {
  const send = jest.fn(async () => new Response(JSON.stringify(scope))) as unknown as typeof fetch;
  const { provision, calls } = provisioner('unknown');
  await expect(bootstrapCrewHost(gatewayExecuteArn, 'us.anthropic.example-v1:0', {
    credentials, send, currentInstance: async () => instanceRef, profile: () => 'profile-r1', publish: jest.fn(), provision,
  })).rejects.toBeInstanceOf(CrewHomeUnknownProvenanceError);
  expect(calls.map(call => call.mode)).toEqual(['ensure']);
  expect(jest.mocked(send).mock.calls.map(([url]) => String(url).split('/').pop())).toEqual(['context']);
  expect(new CrewHomeUnknownProvenanceError().name).toBe('CrewHomeUnknownProvenanceError');
});

test('a first host with no checkpoint is initialised fresh: no grant, no release, pins stay null', async () => {
  const fresh = { ...scope, checkpoint_id: null, manifest_sha256: null, restore_manifest_version: 0 };
  const send = jest.fn(async (url) => {
    if (!String(url).endsWith('/context')) throw new Error(`unexpected call ${url}`);
    return new Response(JSON.stringify(fresh));
  }) as unknown as typeof fetch;
  const publish = jest.fn();
  const { provision, calls } = provisioner('restored');
  await bootstrapCrewHost(gatewayExecuteArn, 'us.anthropic.example-v1:0', {
    credentials, send, currentInstance: async () => instanceRef, profile: () => 'profile-r1', publish, provision,
  });
  expect(publish).toHaveBeenCalledWith(fresh, gatewayExecuteArn, 'us.anthropic.example-v1:0', 'profile-r1');
  expect(calls.map(call => call.mode)).toEqual(['ensure']);
  expect(calls[0]).toEqual({ mode: 'ensure', scope: expectedScope({ restore_manifest_version: 0, checkpoint_id: null, manifest_sha256: null }),
    grant: null, model_id: 'us.anthropic.example-v1:0' });
  expect(jest.mocked(send).mock.calls.map(([url]) => String(url).split('/').pop())).toEqual(['context']);
});

test('a republished scope for the same run with a later lease expiry renews the receipts instead of being refused', () => {
  // dev 2026-10-20: readiness retries outlived the authority receipt's lease; the model service
  // then refused "stale" at start. Same run + later expiry is a renewal; anything else still refuses.
  const root = mkdtempSync(join(tmpdir(), 'crew-renew-'));
  try {
    chmodSync(root, 0o700);
    const trust = join(root, 'crew-host');
    mkdirSync(trust, { mode: 0o750 });
    writeFileSync(join(trust, 'server.key'), 'private-on-box', { mode: 0o640 });
    const uid = lstatSync(root).uid;
    publishCrewHostScope(scope, gatewayExecuteArn, 'us.anthropic.example-v1:0', 'profile-r1', root, uid);
    const later = { ...scope, lease_expires_at: scope.lease_expires_at + 600 };
    expect(() => publishCrewHostScope(later, gatewayExecuteArn, 'us.anthropic.example-v1:0', 'profile-r1', root, uid)).not.toThrow();
    expect(JSON.parse(readFileSync(join(root, 'crew-host.json'), 'utf8')).lease_expires_at).toBe(later.lease_expires_at);
    expect(lstatSync(join(root, 'crew-host.json')).mode & 0o777).toBe(0o640);
    // An EARLIER expiry, or a different run with a later expiry, is not a renewal.
    expect(() => publishCrewHostScope(scope, gatewayExecuteArn, 'us.anthropic.example-v1:0', 'profile-r1', root, uid)).toThrow();
    expect(() => publishCrewHostScope({ ...later, run_id: '99999999-2222-4333-8444-555555555555', lease_expires_at: later.lease_expires_at + 1 },
      gatewayExecuteArn, 'us.anthropic.example-v1:0', 'profile-r1', root, uid)).toThrow();
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('refusals carry the restorer\'s class and fixed message, not a bare Error', () => {
  expect(new CrewHomeUnknownProvenanceError().message).toBe('Crew home has unknown provenance');
  expect(new CrewHomeUnknownProvenanceError('Crew provisioning marker missing').message)
    .toBe('Crew home has unknown provenance: Crew provisioning marker missing');
  expect(new CrewHomeUnknownProvenanceError().name).toBe('CrewHomeUnknownProvenanceError');
});
