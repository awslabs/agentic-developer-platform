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
  claim(trigger?: ProbeTrigger): Promise<ProbeClaim>;
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
    this.root = `${gatewayEndpoint.replace(/\/+$/, '')}${BASE_PATH}`;
  }

  private async post<T>(path: string, payload: unknown): Promise<T> {
    const endpoint = `${this.root}${path}`;
    const body = JSON.stringify(payload);
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: await signedHeaders(endpoint, body),
      body,
      signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) {
      const detail = (await response.text()).slice(0, 500);
      throw new Error(`probe Gateway ${path || '/claim'} returned ${response.status}: ${detail}`);
    }
    return await response.json() as T;
  }

  claim(trigger: ProbeTrigger = 'scheduled'): Promise<ProbeClaim> {
    return this.post<ProbeClaim>('/claim', { trigger });
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
