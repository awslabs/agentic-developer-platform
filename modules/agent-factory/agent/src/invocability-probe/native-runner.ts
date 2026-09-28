/** Native SDK request qualification. Capture locally before the durable paid boundary. */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { ProbeGateway, SigV4ProbeGateway, ProbeCompletion, ProbeStart } from './gateway-client';
import { invokeTaskResponses, validResponsesProbe } from './task-responses';
import manifest from './native-probe-manifest.json';
import reports from './report-probe-manifest.json';

const execute = promisify(execFile);
type Capture = (persona: string, model: string) => Promise<{ body: string; digest: string }>;
type Invoke = (start: ProbeStart, body: string, timeout: number) => ReturnType<typeof invokeTaskResponses>;
const capture: Capture = async (persona, model) => {
  const cli = Object.hasOwn(reports.personas, persona) ? '/app/codex-harness/scripts/report-probe-cli.mjs' : '/app/codex-reviewer/scripts/native-probe-cli.mjs';
  const result = await execute(process.execPath, [cli, persona, model], {
    timeout: 30000, maxBuffer: 131072,
    // The child needs local executable paths only; it receives no worker credentials.
    env: { PATH: process.env.PATH, TMPDIR: process.env.TMPDIR, TZ: 'UTC' },
  });
  return JSON.parse(result.stdout);
};

export function validNativeResponse(response: any, persona: string): boolean {
  if (persona === 'agent-codex-developer' || Object.hasOwn(reports.personas, persona)) return validResponsesProbe(response, false);
  if (!response || response.object !== 'response' || response.status !== 'completed' || typeof response.id !== 'string' || !response.id ||
      response.error != null || response.incomplete_details != null || !Array.isArray(response.output)) return false;
  const output = response.output.filter((item: any) => item?.type !== 'reasoning');
  if (output.length !== 1 || output[0]?.type !== 'message' || output[0].role !== 'assistant' ||
      output[0].status !== 'completed' || !Array.isArray(output[0].content) || output[0].content.length !== 1 || output[0].content[0]?.type !== 'output_text' || typeof output[0].content[0].text !== 'string') return false;
  try {
    const verdict = JSON.parse(output[0].content[0].text);
    return Object.keys(verdict).sort().join(',') === 'findings,summary,validationGaps,verdict' &&
      verdict.verdict === 'approve' && typeof verdict.summary === 'string' && /^OK\.?$/.test(verdict.summary) &&
      Array.isArray(verdict.findings) && verdict.findings.length === 0 &&
      Array.isArray(verdict.validationGaps) && verdict.validationGaps.length === 0;
  } catch { return false; }
}

export async function runNativeProbe(persona: string, gateway: ProbeGateway = new SigV4ProbeGateway(),
  collect: Capture = capture, invoke: Invoke = invokeTaskResponses) {
  const profiles = { ...manifest.personas, ...reports.personas } as Record<string, Record<string, string>>;
  if (!Object.hasOwn(profiles, persona)) throw new Error('Unknown native probe persona');
  const claim = await gateway.claim('scheduled', undefined, persona);
  if (!claim.claimed) return claim;
  const digest = profiles[persona][claim.model_id];
  if (!Number.isFinite(Number(claim.max_budget_usd)) || Number(claim.max_budget_usd) <= 0 || !digest || claim.compatibility_class !== 'codex-sdk' || claim.harness_contract_revision !== manifest.sdk ||
      claim.expected_request_shape_sha256 !== digest || !Number.isInteger(claim.timeout_seconds) ||
      claim.timeout_seconds < 1 || claim.timeout_seconds > 300 || (!Number.isFinite(Date.parse(claim.lease_expires_at)) || Date.parse(claim.lease_expires_at) <= Date.now())) {
    throw new Error('Native claim does not match local SDK contract');
  }
  const emitted = await collect(persona, claim.model_id);
  if (emitted.digest !== digest || createHash('sha256').update(emitted.body).digest('hex') !== digest) {
    throw new Error('Native SDK capture does not match admitted request');
  }
  // Capture never receives provider credentials, and a capture mismatch cannot consume a paid attempt.
  const start = await gateway.start(claim.slot_id, claim.lease_token, digest);
  const completion: ProbeCompletion = { outcome: 'error', request_shape_sha256: digest,
    provider_request_id: null, error_code: 'no_request_emitted.start_mismatch' };
  const expiry = Date.parse(start.credentials_expires_at);
  if (start.slot_id !== claim.slot_id || start.model_id !== claim.model_id || !/^\d{12}$/.test(start.account_id) ||
      !start.access_key_id || !start.secret_access_key || !start.session_token || !Number.isFinite(expiry) || expiry <= Date.now()) {
    return gateway.complete(claim.slot_id, claim.lease_token, completion);
  }
  completion.error_code = 'provider_attempt_unconfirmed';
  try {
    const receipt = await invoke(start, emitted.body, claim.timeout_seconds);
    completion.provider_request_id = receipt.requestId?.trim() || null;
    if (receipt.body.length > 65536) throw new Error('Oversize native response');
    if (receipt.status === 200 && completion.provider_request_id &&
        validNativeResponse(JSON.parse(Buffer.from(receipt.body).toString('utf8')), persona)) {
      completion.outcome = 'proven'; completion.error_code = null;
    }
  } catch { /* Uncertainty never permits a retry or an invented provider receipt. */ }
  return gateway.complete(claim.slot_id, claim.lease_token, completion);
}
