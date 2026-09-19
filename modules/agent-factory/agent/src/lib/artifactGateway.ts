/** Fixed-purpose uploads; only the gateway chooses the bucket and own-run key. */
import { createHash } from 'node:crypto';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import { gatewaySigningRegion, workerAwsCredentials, workerIdentityHeaders } from './runIdentity';

export type ArtifactKind = 'transcript' | 'spill' | 'comment' | 'git-changes' | 'git-manifest';
export const MAX_ARTIFACT_BYTES = 8 * 1024 * 1024;
export function protectedArtifactRun(): boolean {
  return process.env.ADP_AGENT_AUTHORITY_ENABLED?.toLowerCase() === 'true';
}

export async function uploadRunArtifact(kind: ArtifactKind, content: string | Buffer): Promise<{ key: string; uri: string }> {
  try {
    if (!protectedArtifactRun() || !['transcript', 'spill', 'comment', 'git-changes', 'git-manifest'].includes(kind)) throw new Error();
    const body = Buffer.isBuffer(content) ? content : Buffer.from(content);
    if (!body.length || body.length > MAX_ARTIFACT_BYTES) throw new Error();
    const base = process.env.ADP_GATEWAY_ENDPOINT || '';
    const endpoint = new URL(base);
    if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw new Error();
    endpoint.pathname = endpoint.pathname.replace(/\/+$/, '') + '/internal/v1/agent/self/artifacts/' + kind;
    const signer = new SignatureV4({ credentials: workerAwsCredentials(), region: gatewaySigningRegion(base), service: 'execute-api', sha256: Hash.bind(null, 'sha256') });
    const signed = await signer.sign({
      method: 'POST', hostname: endpoint.hostname, protocol: endpoint.protocol, path: endpoint.pathname,
      headers: { host: endpoint.host, 'content-type': 'application/octet-stream', ...workerIdentityHeaders() }, body,
    });
    const response = await fetch(endpoint, { method: 'POST', headers: signed.headers, body, redirect: 'error', signal: AbortSignal.timeout(35_000) });
    if (!response.ok) { await response.body?.cancel(); throw new Error(); }
    const chunks: Uint8Array[] = [];
    let size = 0;
    const reader = response.body?.getReader();
    if (!reader) throw new Error();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 4096) throw new Error();
        chunks.push(value);
      }
    } finally { await reader.cancel(); reader.releaseLock(); }
    const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (typeof result.key !== 'string' || typeof result.uri !== 'string' || !result.uri.startsWith('s3://') || !result.uri.endsWith('/' + result.key)
        || result.sha256 !== createHash('sha256').update(body).digest('hex')) throw new Error();
    return { key: result.key, uri: result.uri };
  } catch {
    // Underlying transport exceptions may carry signed headers or credentials.
    throw new Error('Own-run artifact upload unavailable (maximum 8 MiB)');
  }
}
