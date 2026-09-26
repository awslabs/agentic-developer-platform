/** Bounded Task Messages qualification. No retries after durable start. */
import { createHash } from 'node:crypto';
import { BedrockRuntimeClient, InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';
import { ProbeGateway, SigV4ProbeGateway, ProbeCompletion, ProbeStart } from './gateway-client';
import profiles from './task-profiles.json';

type Receipt = { body: Uint8Array; requestId?: string; status?: number };
type Invoke = (start: ProbeStart, body: string, timeout: number) => Promise<Receipt>;
const invoke: Invoke = async (start, body, timeout) => {
  const client = new BedrockRuntimeClient({ region: start.region, maxAttempts: 1, credentials: {
    accessKeyId: start.access_key_id, secretAccessKey: start.secret_access_key, sessionToken: start.session_token,
  } });
  try {
    const response = await client.send(new InvokeModelCommand({ modelId: start.model_id,
      contentType: 'application/json', accept: 'application/json', body: Buffer.from(body) }),
    { abortSignal: AbortSignal.timeout(timeout * 1000) });
    return { body: response.body, requestId: response.$metadata.requestId, status: response.$metadata.httpStatusCode };
  } finally { client.destroy(); }
};

export async function runTaskProbe(persona: string, gateway: ProbeGateway = new SigV4ProbeGateway(), call: Invoke = invoke) {
  const profile = profiles[persona as keyof typeof profiles];
  if (!profile) throw new Error('Unknown Task probe persona');
  const claim = await gateway.claim('scheduled', persona);
  if (!claim.claimed) return claim;
  const digest = createHash('sha256').update(profile.body).digest('hex');
  if (claim.compatibility_class !== 'anthropic_messages' || claim.harness_contract_revision !== profile.revision ||
      claim.task_probe_json !== profile.body || claim.expected_request_shape_sha256 !== digest || digest !== profile.digest ||
      !Number.isInteger(claim.timeout_seconds) || claim.timeout_seconds < 1 || claim.timeout_seconds > 300) {
    throw new Error('Task probe claim does not match the local bounded profile');
  }
  const start = await gateway.start(claim.slot_id, claim.lease_token, digest);
  // Refuse inconsistent credentials/destination metadata before any provider request.
  const credentialsExpiry = Date.parse(start.credentials_expires_at);
  if (start.slot_id !== claim.slot_id || start.model_id !== claim.model_id || !/^[a-z0-9-]+$/.test(start.region) ||
      !start.access_key_id || !start.secret_access_key || !start.session_token ||
      !Number.isFinite(credentialsExpiry) || credentialsExpiry <= Date.now()) {
    await gateway.complete(claim.slot_id, claim.lease_token, { outcome: 'error', request_shape_sha256: digest,
      provider_request_id: null, error_code: 'no_request_emitted.start_mismatch' });
    throw new Error('Task probe start identity mismatch');
  }
  let completion: ProbeCompletion = { outcome: 'error', request_shape_sha256: digest,
    provider_request_id: null, error_code: 'provider_response_unconfirmed' };
  try {
    const receipt = await call(start, profile.body, claim.timeout_seconds);
    completion.provider_request_id = receipt.requestId?.trim() || null;
    if (receipt.body.length > 65536) throw new Error('oversize_response');
    const response = JSON.parse(Buffer.from(receipt.body).toString('utf8'));
    const content = Array.isArray(response.content) ? response.content : [];
    const valid = persona === 'agent-task-investigator'
      ? content.some((part: any) => part.type === 'text' && typeof part.text === 'string' && part.text.trim() === 'OK')
      : content.some((part: any) => part.type === 'tool_use' && part.name === 'task_probe' && part.input?.value === 'OK');
    if (receipt.status === 200 && completion.provider_request_id && valid) {
      completion.outcome = 'proven'; completion.error_code = null;
    }
  } catch {
    // Transport uncertainty is never retried or promoted to a provider receipt.
    completion.error_code = 'provider_attempt_unconfirmed';
  }
  return gateway.complete(claim.slot_id, claim.lease_token, completion);
}
