/**
 * Vault Gateway Client — signed internal-endpoint caller for vault operations.
 *
 * Calls the gateway's /internal/v1/* credential endpoints on behalf of the
 * chat-agent. Authentication is via the X-Internal-Api-Key shared secret.
 *
 * Issue #137: Vault Phase 4
 */

import { validateBaseUrl } from '../../lib/url-guard';

export interface VaultClientConfig {
  /** Base URL of the gateway (e.g. http://bedrockgateway.adp-gateway:8080) */
  baseUrl: string;
  /** Shared secret for internal API authentication */
  apiKey: string;
}

export interface ProxyRequestInput {
  user_id: string;
  agent_id: string;
  task_id: string;
  service: string;
  label?: string;
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: unknown;
  invocation_id?: string;
}

export interface ProxyResponse {
  status: number;
  headers: Record<string, string>;
  body: string;
  provenance_id: string;
}

export interface MaterializeInput {
  user_id: string;
  agent_id: string;
  task_id: string;
  service: string;
  label?: string;
  invocation_id?: string;
}

export interface MaterializeResponse {
  materialize_url: string;
  expires_at: string;
  provenance_id: string;
}

export interface RawReadInput {
  user_id: string;
  agent_id: string;
  task_id: string;
  service: string;
  label?: string;
  purpose?: string;
  invocation_id?: string;
}

export interface RawReadResponse {
  value: string;
  credential_type: string;
  provenance_id: string;
}

export interface AssumeRoleInput {
  user_id: string;
  agent_id: string;
  task_id: string;
  service: string;
  label?: string;
  purpose?: string;
  invocation_id?: string;
}

export interface AssumeRoleResponse {
  profile_name: string;
  access_key_id: string;
  secret_access_key: string;
  session_token: string;
  expiration: string;
  region: string;
  provenance_id: string;
}

export interface CredentialMetadata {
  id: string;
  service: string;
  label: string;
  credential_type: string;
  expires_at: string | null;
  last_used_at: string | null;
  scope?: string;
}

export class VaultGatewayClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;

  constructor(config: VaultClientConfig) {
    // SSRF guard: validate + pin to configured internal gateway host (#3582).
    // allowHttp: internal cluster communication uses plain HTTP.
    const parsed = new URL(config.baseUrl);
    validateBaseUrl(config.baseUrl, { allowHttp: true, pinHost: parsed.hostname });
    this.baseUrl = config.baseUrl.replace(/\/$/, '');
    this.apiKey = config.apiKey;
  }

  async proxyRequest(input: ProxyRequestInput): Promise<ProxyResponse> {
    const resp = await this.post('/internal/v1/proxy-request', input);
    return resp as ProxyResponse;
  }

  async materialize(input: MaterializeInput): Promise<MaterializeResponse> {
    const resp = await this.post('/internal/v1/credential-materialize', input);
    return resp as MaterializeResponse;
  }

  async rawRead(input: RawReadInput): Promise<RawReadResponse> {
    const resp = await this.post('/internal/v1/credential-raw-read', input);
    return resp as RawReadResponse;
  }

  async assumeRole(input: AssumeRoleInput): Promise<AssumeRoleResponse> {
    const resp = await this.post('/internal/v1/credential-assume-role', input);
    return resp as AssumeRoleResponse;
  }

  async listCredentials(userId: string, invocationId?: string): Promise<CredentialMetadata[]> {
    let url = `${this.baseUrl}/internal/v1/user-credentials?user_id=${encodeURIComponent(userId)}`;
    if (invocationId) {
      url += `&invocation_id=${encodeURIComponent(invocationId)}`;
    }
    const resp = await fetch(url, {
      method: 'GET',
      headers: {
        'X-Internal-Api-Key': this.apiKey,
        'Content-Type': 'application/json',
      },
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(`Vault gateway GET failed (${resp.status}): ${text}`);
    }
    return (await resp.json()) as CredentialMetadata[];
  }

  private async post(path: string, body: unknown): Promise<unknown> {
    const url = `${this.baseUrl}${path}`;
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'X-Internal-Api-Key': this.apiKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(`Vault gateway POST ${path} failed (${resp.status}): ${text}`);
    }
    return resp.json();
  }
}
