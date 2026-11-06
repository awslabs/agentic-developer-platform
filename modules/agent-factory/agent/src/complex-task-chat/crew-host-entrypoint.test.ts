import { generateKeyPairSync } from 'node:crypto';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { currentCrewHostAuthority } from './crew-host-entrypoint';

const now = 1_800_000_000;
const publicKey = generateKeyPairSync('ed25519').publicKey.export({ type: 'spki', format: 'pem' }).toString();
const fields = {
  run_id: '00000000-0000-4000-8000-000000000001', tenant_id: 'tenant-a', user_id: 'owner-a',
  box_id: 'box-a', instance_ref: 'i-current', private_ip: '10.4.0.3', profile_revision: 'crew-r1',
  restore_manifest_version: 1, lease_generation: 2,
};

test.each(['valid', 'fresh', 'expired', 'retired', 'wrong_profile', 'unverified', 'no_keys', 'writable', 'writable_parent', 'wrong_owner'])
('host custody loads only matching current generation and verified profile: %s', state => {
  const directory = mkdtempSync(join(tmpdir(), 'crew-receipt-'));
  const authorityFile = join(directory, 'host.json');
  const readyFile = join(directory, 'ready.json');
  const profileFile = join(directory, 'profile.json');
  try {
    const authority = { ...fields, lease_expires_at: state === 'expired' ? now - 1 : now + 300,
      gateway_fingerprint: 'a'.repeat(64), verification_keys: state === 'no_keys' ? {} : { gateway: publicKey } };
    // A first host (#8204) restores nothing: manifest version 0 is a valid authority, not a stale one.
    if (state === 'fresh') authority.restore_manifest_version = 0;
    const ready = { ...fields, restore_manifest_version: authority.restore_manifest_version,
      instance_ref: state === 'retired' ? 'i-retired' : fields.instance_ref };
    writeFileSync(authorityFile, JSON.stringify(authority), { mode: 0o600 });
    if (state !== 'unverified') writeFileSync(readyFile, JSON.stringify(ready), { mode: 0o600 });
    writeFileSync(profileFile, JSON.stringify({ revision: state === 'wrong_profile' ? 'crew-r2' : 'crew-r1' }));
    if (state === 'writable') chmodSync(readyFile, 0o666);
    if (state === 'writable_parent') chmodSync(directory, 0o777);
    const uid = state === 'wrong_owner' ? -1 : process.getuid!();
    if (state === 'valid' || state === 'fresh') {
      const current = currentCrewHostAuthority(authorityFile, readyFile, profileFile, now, uid);
      expect(current).toMatchObject({ runId: fields.run_id, tenantId: 'tenant-a', userId: 'owner-a',
        generation: 2, privateIP: '10.4.0.3', gatewayFingerprint: 'a'.repeat(64) });
      expect(current.keys.size).toBe(1);
    } else {
      expect(() => currentCrewHostAuthority(authorityFile, readyFile, profileFile, now, uid)).toThrow();
    }
  } finally { rmSync(directory, { recursive: true, force: true }); }
});


test('host bundles that only need the trusted receipts never import the dashboard entry point', () => {
  // esbuild inlines whatever an entry imports; crew-host-entrypoint.ts runs the dashboard when it is
  // require.main, which is true inside any bundle. Dev 2026-10-19 (#8223): the bootstrap's --configure
  // and the model supervisor exited 1 silently because of exactly that.
  const { readFileSync: read } = require('node:fs') as typeof import('node:fs');
  for (const entry of ['crew-host-bootstrap.ts', 'crew-host-model-entrypoint.ts']) {
    const source = read(join(__dirname, entry), 'utf8');
    expect(source).not.toMatch(/from '\.\/crew-host-entrypoint'/);
  }
  const trusted = read(join(__dirname, 'crew-host-trusted.ts'), 'utf8');
  expect(trusted).not.toMatch(/require\.main/);
  expect(trusted).not.toMatch(/crew-host-ingress/);
});
