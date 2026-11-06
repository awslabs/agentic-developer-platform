/**
 * Trusted host receipts shared by the three host bundles (dashboard, model supervisor,
 * bootstrap). This module has NO entry-point side effects on purpose: each bundle is built
 * with esbuild from its own entry file, and anything it imports is inlined. When the
 * bootstrap and the model supervisor imported these helpers from `crew-host-entrypoint.ts`,
 * that file's main-module guard started `runCrewHostDashboard()` inside every bundle:
 * `--configure` as the agent user and the model service exited 1 with no output while the
 * real work had succeeded (dev 2026-10-19, #8223).
 */
import { constants, closeSync, fstatSync, openSync, readFileSync, statSync } from 'node:fs';
import { dirname } from 'node:path';
import { z } from 'zod';
import { parseVerificationKeys } from '../control-envelope';

export const HOST_AUTHORITY = '/run/adp/crew-host.json';
export const HOST_READY = '/run/adp/crew-ready';

export function readTrustedJson(path: string, trustedUid: number): Record<string, unknown> {
  const parent = statSync(dirname(path));
  if (parent.uid !== trustedUid || (parent.mode & 0o022) !== 0) throw new Error('untrusted Crew host directory');
  const handle = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = fstatSync(handle);
    if (!stat.isFile() || stat.uid !== trustedUid || (stat.mode & 0o022) !== 0) throw new Error('untrusted Crew host receipt');
    const value: unknown = JSON.parse(readFileSync(handle, 'utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid Crew host receipt');
    return value as Record<string, unknown>;
  } finally { closeSync(handle); }
}

const authoritySchema = z.object({
  run_id: z.string().uuid(), tenant_id: z.string().min(1), user_id: z.string().min(1),
  box_id: z.string().min(1), instance_ref: z.string().min(1), private_ip: z.string().min(1),
  profile_revision: z.string().min(1), // 0 for a first host (#8171/#8204: nothing to restore); a pinned restore is >= 1.
  restore_manifest_version: z.number().int().nonnegative(),
  lease_generation: z.number().int().positive(), lease_expires_at: z.number().int().positive(),
  gateway_fingerprint: z.string().regex(/^[0-9a-f]{64}$/),
  verification_keys: z.record(z.string(), z.string()),
}).strict();

export function currentCrewHostAuthority(authorityFile = HOST_AUTHORITY, readyFile = HOST_READY,
  profileFile = '/opt/adp/crew/current/profile.json', nowSeconds = Math.floor(Date.now() / 1000), trustedUid = 0) {
  const authority = authoritySchema.parse(readTrustedJson(authorityFile, trustedUid));
  const ready = readTrustedJson(readyFile, trustedUid);
  const profile = readTrustedJson(profileFile, trustedUid);
  const bound = ['run_id', 'tenant_id', 'user_id', 'box_id', 'instance_ref', 'private_ip',
    'profile_revision', 'restore_manifest_version', 'lease_generation'];
  if (authority.lease_expires_at <= nowSeconds ||
      bound.some(field => ready[field] !== authority[field as keyof typeof authority]) ||
      profile.revision !== authority.profile_revision) {
    throw new Error('Crew host authority is stale or unverified');
  }
  const keys = parseVerificationKeys(JSON.stringify(authority.verification_keys));
  if (keys.size === 0) throw new Error('Crew host verification keys unavailable');
  return {
    runId: authority.run_id, tenantId: authority.tenant_id, userId: authority.user_id,
    generation: authority.lease_generation, privateIP: authority.private_ip,
    gatewayFingerprint: authority.gateway_fingerprint, keys,
  };
}

