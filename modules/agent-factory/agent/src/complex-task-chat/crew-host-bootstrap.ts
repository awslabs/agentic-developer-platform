import { spawnSync } from 'node:child_process';
import { chmodSync, chownSync, closeSync, constants, fchownSync, fstatSync, ftruncateSync, lstatSync, openSync, readFileSync, writeFileSync, writeSync } from 'node:fs';
import { join } from 'node:path';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { CREW_MODEL_SHA256, renderCrewConfig } from './crew-kirocrew';
import { crewGatewayEndpoint, discoverCrewHostRestore, isFreshCrewHostScope, readCrewInstanceId,
  withCrewHostRestoreGrant, type HostRestoreGrant, type HostRestoreScope } from './crew-host-bootstrap-client';
import { readTrustedJson } from './crew-host-trusted';

const ROOT = '/run/adp';
const PROFILE = '/opt/adp/crew/current/profile.json';
const MODEL = '/opt/adp/crew/current/models/qwen3-embedding-0.6b-q8_0.gguf';
const DEFAULT_AGENT = '/opt/adp/crew/current/default-agent.json';
const IMAGE = '/opt/adp/crew/current/host/crew-host-bootstrap.js';
const PROVISIONER = '/opt/adp/crew/restore_crew_host.py';

/** The mounted home holds data this box never provisioned; the operator must replace or clear it. */
export class CrewHomeUnknownProvenanceError extends Error {
  constructor(detail?: string) {
    super(detail ? `Crew home has unknown provenance: ${detail}` : 'Crew home has unknown provenance');
    this.name = 'CrewHomeUnknownProvenanceError';
  }
}

export type ProvisionMode = 'ensure' | 'probe' | 'restore' | 'resume';
export type EnsureVerdict = 'ready' | 'resumed' | 'needs_grant' | 'restored';
export type ProvisionRequest = { mode: ProvisionMode; scope: Record<string, unknown>; grant: HostRestoreGrant | null; model_id: string };

function trustedDirectory(path: string, uid: number): void {
  const info = lstatSync(path);
  if (!info.isDirectory() || info.uid !== uid || (info.mode & 0o022) !== 0) {
    throw new Error('Untrusted Crew host directory');
  }
}

function samePublication(path: string, value: string, uid: number, groupId: number): boolean {
  const handle = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = fstatSync(handle);
    return info.isFile() && info.uid === uid && info.gid === groupId && (info.mode & 0o027) === 0 &&
      readFileSync(handle, 'utf8') === value;
  } finally { closeSync(handle); }
}

function renewedPublication(path: string, value: string, uid: number, groupId: number): boolean {
  let next: Record<string, unknown>;
  try { next = JSON.parse(value) as Record<string, unknown>; } catch { return false; }
  if (!next || typeof next !== 'object' || typeof next.lease_expires_at !== 'number') return false;
  const handle = openSync(path, constants.O_RDWR | constants.O_NOFOLLOW);
  try {
    const info = fstatSync(handle);
    if (!info.isFile() || info.uid !== uid || info.gid !== groupId || (info.mode & 0o027) !== 0) return false;
    let previous: Record<string, unknown>;
    try { previous = JSON.parse(readFileSync(handle, 'utf8')) as Record<string, unknown>; } catch { return false; }
    if (!previous || typeof previous !== 'object' || typeof previous.lease_expires_at !== 'number') return false;
    if (previous.lease_expires_at >= next.lease_expires_at) return false;
    const same = (a: Record<string, unknown>, b: Record<string, unknown>) =>
      JSON.stringify({ ...a, lease_expires_at: 0 }) === JSON.stringify({ ...b, lease_expires_at: 0 });
    if (!same(previous, next)) return false;
    ftruncateSync(handle, 0);
    writeSync(handle, value, 0);
    return true;
  } finally { closeSync(handle); }
}

function publish(path: string, value: string, uid: number, groupId: number): void {
  trustedDirectory(join(path, '..'), uid);
  let handle: number;
  try {
    handle = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  } catch (error) {
    // A bootstrap that failed after publishing (a refused restore, a slow host) is retried by
    // the controller on the next tick within the same boot; the identical scope is accepted,
    // a different one is still refused (#8223: "bootstrap publish not idempotent").
    if ((error as NodeJS.ErrnoException).code === 'EEXIST' && samePublication(path, value, uid, groupId)) return;
    // The same run with a later lease expiry is a renewal, not a replacement: a readiness retry
    // that outlived the receipt's expiry would otherwise leave a stale authority behind, and
    // the model service refuses a stale authority at start (dev 2026-10-20, #8223).
    if ((error as NodeJS.ErrnoException).code === 'EEXIST' && renewedPublication(path, value, uid, groupId)) return;
    throw error;
  }
  try {
    const created = fstatSync(handle);
    if (created.uid !== uid || created.gid !== groupId) fchownSync(handle, uid, groupId);
    writeFileSync(handle, value);
    const info = fstatSync(handle);
    if (info.uid !== uid || info.gid !== groupId || (info.mode & 0o027) !== 0) {
      throw new Error('Untrusted Crew host publication');
    }
  } finally { closeSync(handle); }
  chmodSync(path, 0o640);
}

export function publishCrewHostScope(scope: HostRestoreScope, gatewayExecuteArn: string, modelId: string,
  profileRevision: string, root = ROOT, uid = 0): void {
  crewGatewayEndpoint(gatewayExecuteArn);
  if (!/^[A-Za-z0-9_.:-]{1,256}$/.test(modelId) || !profileRevision || scope.lease_expires_at <= Date.now() / 1000) {
    throw new Error('Crew host profile unavailable');
  }
  trustedDirectory(root, uid);
  trustedDirectory(join(root, 'crew-host'), uid);
  const key = lstatSync(join(root, 'crew-host', 'server.key'));
  if (!key.isFile() || key.uid !== uid || (key.mode & 0o027) !== 0) {
    throw new Error('Crew host certificate unavailable');
  }
  const rootInfo = lstatSync(root);
  if (rootInfo.gid !== key.gid) chownSync(root, uid, key.gid);
  chmodSync(root, 0o750);
  const authority = {
    run_id: scope.run_id, tenant_id: scope.tenant_id, user_id: scope.user_id, box_id: scope.box_id,
    instance_ref: scope.instance_ref, private_ip: scope.private_ip, profile_revision: profileRevision,
    restore_manifest_version: scope.restore_manifest_version, lease_generation: scope.lease_generation,
    lease_expires_at: scope.lease_expires_at, gateway_fingerprint: scope.gateway_fingerprint,
    verification_keys: scope.verification_keys,
  };
  publish(join(root, 'crew-host', 'client-ca.pem'), scope.client_certificate, uid, key.gid);
  publish(join(root, 'crew-host.json'), JSON.stringify(authority), uid, key.gid);
  publish(join(root, 'crew-model.json'), JSON.stringify({ run_id: scope.run_id,
    lease_generation: scope.lease_generation, gateway_execute_arn: gatewayExecuteArn, model_id: modelId }), uid, key.gid);
}

export function provisionCrewHost(request: ProvisionRequest): string {
  const result = spawnSync('python3', [PROVISIONER], {
    input: JSON.stringify(request), encoding: 'utf8', timeout: 180_000, maxBuffer: 4096,
  });
  if (result.status !== 0 || result.error) {
    // The helper names the refusal class and its fixed message on stderr (e.g.
    // "InvalidCheckpoint: untrusted Crew host directory"); carry both so the controller log and
    // the box status say why (dev 2026-10-20: a bare "Error" hid the home-ownership refusal).
    const refusal = /refused: ([A-Za-z]+)(?:: ([^\n]{1,200}))?/.exec(result.stderr ?? '');
    const detail = refusal?.[1];
    if (detail === 'CrewHomeUnknownProvenance') throw new CrewHomeUnknownProvenanceError(refusal?.[2]);
    const reason = result.error ? result.error.message : detail ? `${detail}${refusal?.[2] ? `: ${refusal[2]}` : ''}` : `exit ${result.status}`;
    throw new Error(`Crew host ${request.mode} failed (${reason})`);
  }
  return result.stdout ?? '';
}

export async function bootstrapCrewHost(gatewayExecuteArn: string, modelId: string, options: {
  credentials?: ReturnType<typeof defaultProvider>; send?: typeof fetch; currentInstance?: () => Promise<string>;
  profile?: () => string; publish?: typeof publishCrewHostScope;
  provision?: (request: ProvisionRequest) => string;
} = {}): Promise<'ready' | 'restored' | 'resumed'> {
  const transport = { gatewayExecuteArn, credentials: options.credentials ?? defaultProvider(), send: options.send };
  const scope = await discoverCrewHostRestore({ ...transport, currentInstance: options.currentInstance ?? readCrewInstanceId });
  const profile = options.profile?.() ?? readTrustedJson(PROFILE, 0).revision as string;
  (options.publish ?? publishCrewHostScope)(scope, gatewayExecuteArn, modelId, profile);
  const provision = options.provision ?? provisionCrewHost;
  const request = (mode: ProvisionMode, grant: HostRestoreGrant | null): ProvisionRequest => ({ mode, scope: {
    run_id: scope.run_id, tenant_id: scope.tenant_id, user_id: scope.user_id,
    box_id: scope.box_id, instance_ref: scope.instance_ref, private_ip: scope.private_ip,
    profile_revision: profile, lease_generation: scope.lease_generation,
    restore_manifest_version: scope.restore_manifest_version, checkpoint_id: scope.checkpoint_id,
    manifest_sha256: scope.manifest_sha256,
  }, grant, model_id: modelId });
  // One idempotent host command (#8459, epic #8456): the restorer decides between "already
  // ready", "resume the home this box provisioned" (/run/adp is tmpfs, the workspace disk is
  // not), "initialise an empty home" and "refuse an occupied home of unknown provenance". The
  // only thing left to this side is fetching the restore grant when the restorer asks for it.
  const verdict = provision(request('ensure', null)).trim() as EnsureVerdict | string;
  if (verdict === 'ready' || verdict === 'resumed' || verdict === 'restored') return verdict;
  if (verdict !== 'needs_grant') throw new Error(`Crew host ensure answered ${verdict || 'nothing'}`);
  let outcome: EnsureVerdict | string = '';
  await withCrewHostRestoreGrant(transport, scope, async grant => { outcome = provision(request('ensure', grant)).trim(); });
  if (outcome !== 'restored') throw new Error(`Crew host ensure with grant answered ${outcome || 'nothing'}`);
  return 'restored';
}

async function main() {
  // `--configure <model> [dashboard-origin]`, run as the agent user by restore_crew_host.py. The
  // origin is the gateway's Crew edge: Kiro Crew admits only Host headers derived from
  // `dashboard.url`, and the browser arrives through that edge, not 127.0.0.1 (#8223 cause 20).
  if (process.argv[2] === '--configure' && (process.argv.length === 4 || process.argv.length === 5) && process.getuid?.() !== 0) {
    await renderCrewConfig('/mnt/crew', process.argv[3], MODEL, CREW_MODEL_SHA256, DEFAULT_AGENT, process.argv[4]);
    return;
  }
  if (process.getuid?.() !== 0 || process.argv.length !== 6 || process.argv[2] !== '--gateway-arn' ||
      process.argv[4] !== '--model-id') throw new Error('Crew host bootstrap requires root and approved inputs');
  const outcome = await bootstrapCrewHost(process.argv[3], process.argv[5]);
  // The controller accepts exactly `crew-bootstrap-ok`; the outcome goes to stderr for the SSM log.
  process.stderr.write(`crew-bootstrap: ${outcome}\n`);
  process.stdout.write('crew-bootstrap-ok\n');
}

if (require.main === module) {
  main().catch(error => {
    // Name AND message: messages are fixed constants from this bundle or the restorer, never
    // paths or secrets; the controller keeps the stderr tail as the refusal reason (#8223).
    const name = error instanceof Error ? error.name || error.constructor.name : 'Error';
    const message = error instanceof Error && error.message ? `: ${error.message.replace(/\s+/g, ' ').slice(0, 300)}` : '';
    process.stderr.write(`Crew host bootstrap refused: ${name}${message}\n`);
    process.exitCode = 1;
  });
}
