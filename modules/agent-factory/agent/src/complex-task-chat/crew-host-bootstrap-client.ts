import { X509Certificate } from 'node:crypto';
import { Hash } from '@smithy/hash-node';
import { SignatureV4 } from '@smithy/signature-v4';
import { z } from 'zod';

type Credentials = { accessKeyId: string; secretAccessKey: string; sessionToken?: string };

const instanceId = /^i-[0-9a-f]{8,17}$/;
const uuid = z.string().uuid();
const scopeSchema = z.object({
  run_id: uuid, tenant_id: z.string().min(1), user_id: z.string().min(1),
  box_id: z.string().min(1), instance_ref: z.string().regex(instanceId),
  private_ip: z.string().min(1), lease_generation: z.number().int().positive(),
  lease_expires_at: z.number().int().positive(),
  // A first host has nothing to restore (#8171): the gateway issues a fresh scope with no
  // checkpoint pins and manifest version 0; a restore scope pins all three together.
  checkpoint_id: z.string().min(1).nullable(), manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/).nullable(),
  restore_manifest_version: z.number().int().nonnegative(), deployment_id: z.string().min(1),
  client_certificate: z.string().startsWith('-----BEGIN CERTIFICATE-----'),
  gateway_fingerprint: z.string().regex(/^[a-f0-9]{64}$/),
  verification_keys: z.record(z.string(), z.string()),
}).strict().refine(scope => (scope.checkpoint_id === null) === (scope.manifest_sha256 === null) &&
  (scope.checkpoint_id === null) === (scope.restore_manifest_version === 0),
{ message: 'Crew host restore scope mixes fresh and pinned fields' });

const grantSchema = z.object({ id: z.string().min(1), restore_token: z.string().min(32),
  crew_version: z.literal('0.11.2'), schema_version: z.literal(1),
  manifest_version: z.number().int().positive(), objects: z.record(z.string(), z.unknown()),
}).strict();

export type HostRestoreScope = z.infer<typeof scopeSchema>;
export type HostRestoreGrant = z.infer<typeof grantSchema>;

/** A fresh scope: the owner has no verified checkpoint, so the host starts Crew on an empty home. */
export function isFreshCrewHostScope(scope: HostRestoreScope): boolean {
  return scope.checkpoint_id === null;
}

export function crewGatewayEndpoint(gatewayExecuteArn: string) {
  const match = /^arn:aws:execute-api:([a-z0-9-]+):[0-9]{12}:([a-z0-9]{8,20})\/([A-Za-z0-9_-]+)$/.exec(gatewayExecuteArn);
  if (!match) throw new Error('Approved Crew gateway unavailable');
  const [, region, api, stage] = match;
  return { region, hostname: `${api}.execute-api.${region}.amazonaws.com`, stage };
}

export async function readCrewInstanceId(send: typeof fetch = fetch): Promise<string> {
  const base = 'http://169.254.169.254/latest';
  const timeout = AbortSignal.timeout(2_000);
  const tokenResponse = await send(`${base}/api/token`, { method: 'PUT', redirect: 'error', signal: timeout,
    headers: { 'x-aws-ec2-metadata-token-ttl-seconds': '60' } });
  const token = await tokenResponse.text();
  if (!tokenResponse.ok || !token || token.length > 1024) throw new Error('Crew instance metadata unavailable');
  const response = await send(`${base}/meta-data/instance-id`, { redirect: 'error', signal: AbortSignal.timeout(2_000),
    headers: { 'x-aws-ec2-metadata-token': token } });
  const current = await response.text();
  if (!response.ok || !instanceId.test(current)) throw new Error('Crew instance identity unavailable');
  return current;
}

export async function signedCrewRestoreRequest(options: {
  gatewayExecuteArn: string; credentials: () => Promise<Credentials>; send?: typeof fetch;
}, operation: 'context' | 'grant' | 'release', body: Record<string, unknown>, runId?: string): Promise<unknown> {
  const { hostname, stage, region } = crewGatewayEndpoint(options.gatewayExecuteArn);
  if (runId !== undefined && !uuid.safeParse(runId).success) throw new Error('Crew run identity unavailable');
  if ((operation === 'context') !== (runId === undefined)) throw new Error('Crew restore scope unavailable');
  const credentials = await options.credentials();
  if (!credentials.sessionToken) throw new Error('Temporary Crew instance credentials required');
  const signer = new SignatureV4({ credentials, region, service: 'execute-api', sha256: Hash.bind(null, 'sha256') });
  const path = `/${stage}/agent/crew/host-restore/${operation}`;
  const payload = JSON.stringify(body);
  const signed = await signer.sign({ method: 'POST', protocol: 'https:', hostname, path, body: payload,
    headers: { host: hostname, 'content-type': 'application/json', ...(runId ? { 'x-agent-runid': runId } : {}) } });
  const response = await (options.send ?? fetch)(`https://${hostname}${path}`, {
    method: 'POST', body: payload, redirect: 'error', signal: AbortSignal.timeout(15_000), headers: signed.headers,
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error(`Crew host restore ${operation} refused (${response.status})`);
  }
  const chunks: Uint8Array[] = [];
  let length = 0;
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Crew host restore response unavailable');
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      length += next.value.length;
      if (length > 16_384) throw new Error('Crew host restore response too large');
      chunks.push(next.value);
    }
  } finally { reader.releaseLock(); }
  return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
}

export async function discoverCrewHostRestore(options: Parameters<typeof signedCrewRestoreRequest>[0] & {
  currentInstance: () => Promise<string>;
}): Promise<HostRestoreScope> {
  const instance = await options.currentInstance();
  if (!instanceId.test(instance)) throw new Error('Crew instance identity unavailable');
  const scope = scopeSchema.parse(await signedCrewRestoreRequest(options, 'context', {}));
  if (scope.instance_ref !== instance ||
      new X509Certificate(scope.client_certificate).fingerprint256.replaceAll(':', '').toLowerCase() !== scope.gateway_fingerprint ||
      Object.keys(scope.verification_keys).length === 0) throw new Error('Crew host restore identity mismatch');
  return scope;
}

export async function withCrewHostRestoreGrant(options: Parameters<typeof signedCrewRestoreRequest>[0],
  scope: HostRestoreScope, restore: (grant: HostRestoreGrant) => Promise<void>): Promise<void> {
  if (scope.checkpoint_id === null) throw new Error('Crew host restore grant requires a pinned checkpoint');
  const grant = grantSchema.parse(await signedCrewRestoreRequest(options, 'grant',
    { checkpoint_id: scope.checkpoint_id }, scope.run_id));
  try {
    if (grant.id !== scope.checkpoint_id || grant.manifest_version !== scope.restore_manifest_version) {
      throw new Error('Crew host restore checkpoint changed');
    }
    await restore(grant);
  } finally {
    const release = await signedCrewRestoreRequest(options, 'release',
      { checkpoint_id: scope.checkpoint_id, restore_token: grant.restore_token }, scope.run_id);
    if (!release || typeof release !== 'object' || (release as Record<string, unknown>).status !== 'released') {
      throw new Error('Crew host restore release unconfirmed');
    }
  }
}
