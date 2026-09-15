/** Online, uncached authorization immediately before a queued SDK handoff. */
import { readIdentityToken as readToken, workerAwsCredentialProvider } from './lib/runIdentity';
import { performance } from 'node:perf_hooks';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import { MAX_REVALIDATION_MS, type QueuedAuthorization } from './control-authorization';

export async function revalidateQueuedCommand(proof: Readonly<QueuedAuthorization>, generation: number): Promise<boolean> {
  const started = performance.now();
  try {
    const result = await postRevalidation({ body: JSON.stringify(proof), started });
    return performance.now() - started < MAX_REVALIDATION_MS && result.allowed === true &&
      result.command_id === proof.command_id && result.generation === generation && result.max_round_trip_ms === MAX_REVALIDATION_MS;
  } catch { return false; } // No cached approval and no credential-bearing exception logs.
}

/** The proof supplies only a body; the destination is the platform's IAM API. */
async function postRevalidation(request: { body: string; started: number }): Promise<Record<string, unknown>> {
  const base = process.env.ADP_AGENT_CONTROL_ENDPOINT;
  if (process.env.ADP_AGENT_AUTHORITY_ENABLED !== 'true' || !base ||
    !/^https:\/\/[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?\/[A-Za-z0-9_-]+\/internal\/v1\/agent\/?$/.test(base)) {
    throw new Error('invalid authority endpoint');
  }
  const url = new URL(base.replace(/\/$/, '') + '/revalidate');
  const { body, started } = request;
  const signer = new SignatureV4({ credentials: await workerAwsCredentialProvider(), region: process.env.AWS_REGION || 'us-east-1',
    service: 'execute-api', sha256: Hash.bind(null, 'sha256') });
  const signed = await signer.sign({ method: 'POST', protocol: url.protocol, hostname: url.hostname, path: url.pathname,
    headers: { host: url.host, 'content-type': 'application/json',
      'X-Adp-Run-Credential': readToken(process.env.ADP_RUN_CREDENTIAL_FILE),
      'X-Adp-Workload-Token': readToken(process.env.ADP_WORKLOAD_TOKEN_FILE) }, body });
  const remaining = MAX_REVALIDATION_MS - (performance.now() - started);
  if (remaining <= 0) throw new Error('authorization deadline exceeded');
  const response = await fetch(url, { method: 'POST', headers: signed.headers, body, redirect: 'error',
    signal: AbortSignal.timeout(Math.max(1, Math.floor(remaining))) });
  if (!response.ok) throw new Error('authorization refused');
  return await response.json() as Record<string, unknown>;
}
