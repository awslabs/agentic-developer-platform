import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { crewGatewayEndpoint, discoverCrewHostRestore, readCrewInstanceId,
  signedCrewRestoreRequest, withCrewHostRestoreGrant } from './crew-host-bootstrap-client';

const gatewayExecuteArn = 'arn:aws:execute-api:us-east-1:000000000000:abcdefghij/prod';
const runId = '00000000-0000-4000-8000-000000000001';
const instanceRef = 'i-0123456789abcdef0';
const credentials = async () => ({ accessKeyId: 'example-key', secretAccessKey: 'example-secret', sessionToken: 'temporary' });

function publicCertificate() {
  const directory = mkdtempSync(join(tmpdir(), 'crew-bootstrap-test-'));
  try {
    const cert = join(directory, 'client.pem');
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
      '-subj', '/CN=crew-client', '-keyout', join(directory, 'client.key'), '-out', cert], { stdio: 'ignore' });
    return readFileSync(cert, 'utf8');
  } finally { rmSync(directory, { recursive: true, force: true }); }
}

const certificate = publicCertificate();
const fingerprint = createHash('sha256').update(Buffer.from(certificate.split('-----BEGIN CERTIFICATE-----')[1]
  .split('-----END CERTIFICATE-----')[0].replace(/\s+/g, ''), 'base64')).digest('hex');
const scope = {
  run_id: runId, tenant_id: 'tenant', user_id: 'owner', box_id: 'box', instance_ref: instanceRef,
  private_ip: '10.2.3.4', lease_generation: 2, lease_expires_at: 4_000_000_000,
  checkpoint_id: 'checkpoint', manifest_sha256: 'a'.repeat(64),
  restore_manifest_version: 7, deployment_id: 'deployment', client_certificate: certificate,
  gateway_fingerprint: fingerprint, verification_keys: { current: 'public-key' },
};

test('the on-box caller gets its own scope and pins the returned gateway client certificate', async () => {
  const send = jest.fn(async (_url, init) => {
    expect(init?.method).toBe('POST');
    expect(init?.redirect).toBe('error');
    expect(init?.headers).toHaveProperty('authorization');
    expect(init?.headers).not.toHaveProperty('x-agent-runid');
    return new Response(JSON.stringify(scope));
  }) as unknown as typeof fetch;
  const found = await discoverCrewHostRestore({ gatewayExecuteArn, credentials,
    currentInstance: async () => instanceRef, send });
  expect(found).toEqual(scope);
  expect(jest.mocked(send).mock.calls[0][0]).toBe('https://abcdefghij.execute-api.us-east-1.amazonaws.com/prod/agent/crew/host-restore/context');
  await expect(discoverCrewHostRestore({ gatewayExecuteArn, credentials,
    currentInstance: async () => 'i-aaaaaaaaaaaaaaaaa', send })).rejects.toThrow('identity mismatch');
  const wrong = { ...scope, gateway_fingerprint: '0'.repeat(64) };
  const tampered = jest.fn(async () => new Response(JSON.stringify(wrong))) as unknown as typeof fetch;
  await expect(discoverCrewHostRestore({ gatewayExecuteArn, credentials,
    currentInstance: async () => instanceRef, send: tampered })).rejects.toThrow('identity mismatch');
});

test('grant and release are signed for the discovered run even if restore fails', async () => {
  const calls: Array<{ url: string; body: string; headers: Record<string, string> }> = [];
  const grant = { id: 'checkpoint', restore_token: 'a'.repeat(32), crew_version: '0.11.2', schema_version: 1,
    manifest_version: 7, objects: { archive: {}, manifest: {} } };
  const send = jest.fn(async (url, init) => {
    calls.push({ url: String(url), body: String(init?.body), headers: init?.headers as Record<string, string> });
    return new Response(JSON.stringify(String(url).endsWith('/grant') ? grant : { id: 'checkpoint', status: 'released' }));
  }) as unknown as typeof fetch;
  await expect(withCrewHostRestoreGrant({ gatewayExecuteArn, credentials, send }, scope,
    async received => { expect(received).toEqual(grant); throw new Error('restore failed'); })).rejects.toThrow('restore failed');
  expect(calls.map(call => call.url.split('/').pop())).toEqual(['grant', 'release']);
  expect(calls.every(call => call.headers['x-agent-runid'] === runId && call.headers.authorization)).toBe(true);
  expect(JSON.parse(calls[1].body)).toEqual({ checkpoint_id: 'checkpoint', restore_token: 'a'.repeat(32) });
  expect(jest.mocked(send)).toHaveBeenCalledTimes(2);
});

test('changed checkpoint pin never invokes restore but still releases the reservation', async () => {
  const operations: string[] = [];
  const send = jest.fn(async (url) => {
    const operation = String(url).split('/').pop() ?? '';
    operations.push(operation);
    return new Response(JSON.stringify(operation === 'grant' ? {
      id: 'other-checkpoint', restore_token: 'a'.repeat(32), crew_version: '0.11.2', schema_version: 1,
      manifest_version: 7, objects: { archive: {}, manifest: {} },
    } : { id: 'other-checkpoint', status: 'released' }));
  }) as unknown as typeof fetch;
  const restore = jest.fn(async () => undefined);
  await expect(withCrewHostRestoreGrant({ gatewayExecuteArn, credentials, send }, scope, restore))
    .rejects.toThrow('checkpoint changed');
  expect(restore).not.toHaveBeenCalled();
  expect(operations).toEqual(['grant', 'release']);
});

test('unapproved origins, ambient credentials and replayed scope fail closed', async () => {
  expect(() => crewGatewayEndpoint('https://example.com')).toThrow();
  expect(() => crewGatewayEndpoint('arn:aws:execute-api:us-east-1:000000000000:abcdefghij/prod/extra')).toThrow();
  const send = jest.fn(async () => new Response('{}')) as unknown as typeof fetch;
  await expect(signedCrewRestoreRequest({ gatewayExecuteArn, credentials: async () => ({
    accessKeyId: 'example', secretAccessKey: 'example',
  }), send }, 'context', {})).rejects.toThrow('Temporary');
  await expect(signedCrewRestoreRequest({ gatewayExecuteArn, credentials, send }, 'grant', {}, 'wrong-run'))
    .rejects.toThrow('run identity');
  expect(send).not.toHaveBeenCalled();
});

test('instance id comes only from an IMDSv2 exchange', async () => {
  const send = jest.fn(async (url, init) => String(url).endsWith('/api/token')
    ? new Response('metadata-token') : new Response(instanceRef)) as unknown as typeof fetch;
  expect(await readCrewInstanceId(send)).toBe(instanceRef);
  expect(jest.mocked(send).mock.calls[0][1]?.method).toBe('PUT');
  expect(jest.mocked(send).mock.calls[1][1]?.headers).toEqual({ 'x-aws-ec2-metadata-token': 'metadata-token' });
  const refused = jest.fn(async (url) => String(url).endsWith('/api/token')
    ? new Response('metadata-token') : new Response('i-wrong')) as unknown as typeof fetch;
  await expect(readCrewInstanceId(refused)).rejects.toThrow('instance identity');
});

test('a scope that mixes fresh and pinned restore fields is refused, and a fresh scope cannot take a grant', async () => {
  for (const mixed of [
    { ...scope, checkpoint_id: null },
    { ...scope, checkpoint_id: null, manifest_sha256: null },
    { ...scope, restore_manifest_version: 0 },
    { ...scope, checkpoint_id: null, manifest_sha256: null, restore_manifest_version: -1 },
  ]) {
    const send = jest.fn(async () => new Response(JSON.stringify(mixed))) as unknown as typeof fetch;
    await expect(discoverCrewHostRestore({ gatewayExecuteArn, credentials, currentInstance: async () => instanceRef, send }))
      .rejects.toThrow();
  }
  const fresh = { ...scope, checkpoint_id: null, manifest_sha256: null, restore_manifest_version: 0 };
  const send = jest.fn(async () => new Response(JSON.stringify(fresh))) as unknown as typeof fetch;
  await expect(discoverCrewHostRestore({ gatewayExecuteArn, credentials, currentInstance: async () => instanceRef, send }))
    .resolves.toEqual(fresh);
  const restore = jest.fn();
  await expect(withCrewHostRestoreGrant({ gatewayExecuteArn, credentials, send }, fresh, restore))
    .rejects.toThrow('pinned checkpoint');
  expect(restore).not.toHaveBeenCalled();
  expect(send).toHaveBeenCalledTimes(1);
});
