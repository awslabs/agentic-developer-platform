/** Online, uncached authorization immediately before a queued SDK handoff. */
import { readIdentityToken as readToken, workerAwsCredentialProvider, gatewaySigningRegion } from './lib/runIdentity';
import { performance } from 'node:perf_hooks';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import { MAX_REVALIDATION_MS, type QueuedAuthorization, type RevalidationOutcome } from './control-authorization';

/**
 * Re-check a queued command, and carry back the gateway's abort receipt (#3963).
 *
 * The decision itself is unchanged — same bounded round trip, same four equality
 * checks, same fail-closed `catch`. What changed is that the response's
 * `abort_receipt` is no longer dropped.
 *
 * Why it must not be dropped: the receipt is the gateway's signed statement that it
 * *accepted* this abort, minted only after abort intent was durably recorded. The
 * process that later reports the aborted outcome and deletes the queue message is
 * not this one, so without the receipt it has nothing to check and has to trust a
 * field the pod wrote about itself. That was review finding 1 — a fabricated
 * `delivery: "accepted"` beside a genuine issuance envelope was accepted as proof
 * of live delivery, because an envelope proves only that an operator asked.
 *
 * The receipt is returned verbatim and judged nowhere in this process. Its value is
 * that it is signed with a key no worker holds, and this image has no signing path
 * at all; inspecting or normalizing it here would add nothing and could only
 * corrupt the bytes its signature covers.
 */
export async function revalidateQueuedCommand(
  proof: Readonly<QueuedAuthorization>,
  generation: number,
): Promise<RevalidationOutcome> {
  const started = performance.now();
  try {
    const result = await postRevalidation({ body: JSON.stringify(proof), started });
    const allowed = performance.now() - started < MAX_REVALIDATION_MS && result.allowed === true &&
      result.command_id === proof.command_id && result.generation === generation && result.max_round_trip_ms === MAX_REVALIDATION_MS;
    // Only surfaced on an allowed decision. A receipt accompanying a refusal would
    // be a contradiction the gateway does not produce, and passing one through
    // would hand the sentinel writer proof for a command that was just denied.
    const abortReceipt = allowed && typeof result.abort_receipt === 'string' && result.abort_receipt.length > 0
      ? result.abort_receipt
      : null;
    return { allowed, abortReceipt };
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
  const signer = new SignatureV4({ credentials: await workerAwsCredentialProvider(), region: gatewaySigningRegion(url.href),
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
