import { Hash } from '@smithy/hash-node';
import { SignatureV4 } from '@smithy/signature-v4';
import { gatewaySigningRegion, workerAwsCredentialProvider } from '../lib/runIdentity';

const BASE_PATH = '/internal/v1/persona-model-probes';

export type ProbeTrigger = 'scheduled';

export type ProbeClaim = {
  claimed: false;
  reason: string;
} | {
  claimed: true;
  task_probe_json?: string | null;
  slot_id: string;
  lease_token: string;
  model_id: string;
  compatibility_class: string;
  harness_contract_revision: string;
  expected_request_shape_sha256: string;
  max_budget_usd: string;
  timeout_seconds: number;
  lease_expires_at: string;
};

export interface ProbeStart {
  slot_id: string;
  model_id: string;
  account_id: string;
  region: string;
  access_key_id: string;
  secret_access_key: string;
  session_token: string;
  credentials_expires_at: string;
}

export interface ProbeCompletion {
  outcome: 'proven' | 'refused' | 'error';
  request_shape_sha256: string;
  provider_request_id: string | null;
  error_code: string | null;
}

export interface ProbeCompleteResponse {
  slot_id: string;
  status: 'completed';
  evidence_recorded: boolean;
}

export interface ProbeGateway {
  claim(trigger?: ProbeTrigger, taskPersona?: string): Promise<ProbeClaim>;
  start(slotId: string, leaseToken: string, requestShapeSha256: string): Promise<ProbeStart>;
  complete(slotId: string, leaseToken: string, completion: ProbeCompletion): Promise<ProbeCompleteResponse>;
}

async function signedHeaders(endpoint: string, body: string): Promise<Record<string, string>> {
  const url = new URL(endpoint);
  const signer = new SignatureV4({
    credentials: await workerAwsCredentialProvider(),
    region: gatewaySigningRegion(endpoint),
    service: 'execute-api',
    sha256: Hash.bind(null, 'sha256'),
  });
  const signed = await signer.sign({
    method: 'POST',
    protocol: url.protocol,
    hostname: url.hostname,
    port: url.port ? Number(url.port) : undefined,
    path: url.pathname,
    headers: { host: url.host, 'content-type': 'application/json' },
    body,
  });
  return signed.headers as Record<string, string>;
}

export class SigV4ProbeGateway implements ProbeGateway {
  private readonly root: string;

  constructor(gatewayEndpoint = process.env.ADP_GATEWAY_ENDPOINT ?? '') {
    if (!gatewayEndpoint) throw new Error('ADP_GATEWAY_ENDPOINT is required for invocability probes');
    // Same destination policy as the other own-run Gateway callers (artifactGateway,
    // knowledgeBridge): the probe endpoint is an operator-configured API Gateway origin,
    // so require https and reject inline credentials, query and fragment. A base path is
    // preserved deliberately — API Gateway stage URLs carry one (e.g. /dev).
    let parsed: URL;
    try {
      parsed = new URL(gatewayEndpoint);
    } catch {
      throw new Error('ADP_GATEWAY_ENDPOINT is not a valid URL');
    }
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error('ADP_GATEWAY_ENDPOINT must be an https URL without credentials, query or fragment');
    }
    this.root = `${gatewayEndpoint.replace(/\/+$/, '')}${BASE_PATH}`;
  }

  private async post<T>(path: string, payload: unknown): Promise<T> {
    const endpoint = `${this.root}${path}`;
    const body = JSON.stringify(payload);
    // endpoint is the configured Gateway origin validated in the constructor (https only,
    // no inline credentials/query/fragment) plus a static probe path. redirect:'error'
    // stops the Gateway from relocating the call: the SigV4 headers include
    // x-amz-security-token, which fetch() forwards across origins on a redirect (#5603).
    // nosemgrep: tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: await signedHeaders(endpoint, body),
      body,
      redirect: 'error',
      signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) {
      const detail = (await response.text()).slice(0, 500);
      throw new Error(`probe Gateway ${path || '/claim'} returned ${response.status}: ${detail}`);
    }
    return await response.json() as T;
  }

  claim(trigger: ProbeTrigger = 'scheduled', taskPersona?: string): Promise<ProbeClaim> {
    return this.post<ProbeClaim>('/claim', { trigger, ...(taskPersona ? { task_persona: taskPersona } : {}) });
  }

  start(slotId: string, leaseToken: string, requestShapeSha256: string): Promise<ProbeStart> {
    return this.post<ProbeStart>(`/${encodeURIComponent(slotId)}/start`, {
      lease_token: leaseToken,
      request_shape_sha256: requestShapeSha256,
    });
  }

  complete(slotId: string, leaseToken: string, completion: ProbeCompletion): Promise<ProbeCompleteResponse> {
    return this.post<ProbeCompleteResponse>(`/${encodeURIComponent(slotId)}/complete`, {
      lease_token: leaseToken,
      ...completion,
    });
  }
}
