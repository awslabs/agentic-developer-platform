/**
 * GitHub-token gatekeeper client for the Node agent runtime.
 *
 * Issue #4272. The platform GitHub App private key used to be exported into this
 * process as GH_APP_PRIVATE_KEY so that TokenManager could re-mint installation
 * tokens before the 1-hour expiry (#1502). That handed every agent run — and
 * every shell it spawns — an App-level credential able to mint tokens for any
 * org that installed the App. Now the key stays in the gateway and this client
 * asks it to mint on the run's behalf.
 *
 * Mirrors lib/provenanceClient.ts's transport selection (SigV4 via
 * ADP_GATEWAY_ENDPOINT, shared secret otherwise) and the Python twin in
 * agent-worker-image/lib/gateway_credential_client.py.
 *
 * Two deliberate design choices, both load-bearing:
 *
 *  - **No local-mint fallback.** If the gatekeeper is unreachable this throws.
 *    A fallback would require the private key to still be in the environment,
 *    which is exactly what this change removes.
 *  - **No tenant in the request.** The gateway resolves the tenant from the
 *    run's webhook-events row and refuses to mint for an installation the run
 *    is not bound to, so a prompt-injected run cannot ask for another org.
 */

/** The gateway's GithubInstallationTokenResponse. */
import { workerIdentityHeaders, workerAwsCredentialProvider } from './runIdentity';

interface GithubInstallationTokenResponse {
  token?: string;
  expires_at?: string;
}

export interface BrokeredToken {
  token: string;
  /** GitHub's own expiry, parsed. Never a local `now + 1h` guess. */
  expiresAt: Date;
}

/**
 * Return true when the GitHub-token gatekeeper is enabled for this run.
 *
 * Set by entrypoint.py from the pod's ADP_GH_TOKEN_BROKER_ENABLED. Default off
 * means today's behavior (local mint from the exported key) is unchanged.
 */
export function isBrokerEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  const raw = (env.ADP_GH_TOKEN_BROKER_ENABLED || '').toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes';
}

/**
 * Sign a request with SigV4 using the pod's ambient (IRSA) credentials.
 *
 * Same shape as provenanceClient.ts's signer; the packages are already in the
 * prod dependency tree. Imported lazily so the shared-secret path costs nothing.
 */
async function sigv4Headers(
  endpoint: string,
  body: string,
  region: string,
  identityHeaders: Record<string, string> = {},
): Promise<Record<string, string>> {
  const { SignatureV4 } = await import('@smithy/signature-v4');
  const { Hash } = await import('@smithy/hash-node');

  const url = new URL(endpoint);
  const signer = new SignatureV4({
    credentials: await workerAwsCredentialProvider(),
    region,
    service: 'execute-api',
    sha256: Hash.bind(null, 'sha256'),
  });

  const signed = await signer.sign({
    method: 'POST',
    protocol: url.protocol,
    hostname: url.hostname,
    path: url.pathname,
    query: {},
    headers: {
      'Content-Type': 'application/json',
      host: url.hostname,
      ...identityHeaders,
    },
    body,
  });

  return signed.headers as Record<string, string>;
}

export interface BrokerRequest {
  installationId: string;
  repoOwner: string;
  repoName: string;
  /** The run's invocation id. Defaults to ADP_MESSAGE_ID. */
  invocationId?: string;
  purpose?: string;
}

/**
 * Mint a repo-scoped installation token via the gateway gatekeeper.
 *
 * In SigV4 mode the call targets the API Gateway `/internal/{proxy+}` route,
 * NOT `/agent/{proxy+}`. Issue #4343: `/agent/{proxy+}` integrates with the EDGE
 * ALB, where issue #4010's `edge-internal-deny` Ingress patch answers any
 * `/internal/*` path with `403 "Not available from the edge"` — so riding the
 * `/agent` prefix killed every run at the mint step. `/internal/{proxy+}` is
 * already wired to the separate internal-plane ALB and is not denied. Do not
 * re-add the `/agent` segment here.
 *
 * @throws if the gateway is not configured, the call fails, or the response is
 *   missing either the token or its expiry. Loud by design: a run that cannot
 *   refresh must fail visibly rather than drift onto a dying token.
 */
export async function fetchBrokeredToken(req: BrokerRequest): Promise<BrokeredToken> {
  const gatewayEndpoint = (process.env.ADP_GATEWAY_ENDPOINT || '').replace(/\/+$/, '');
  const gatewayUrl = (process.env.VAULT_GATEWAY_URL || '').replace(/\/+$/, '');
  const apiKey = process.env.VAULT_INTERNAL_API_KEY || '';
  const region = process.env.AWS_REGION || 'us-east-1';

  const useSigv4 = Boolean(gatewayEndpoint);

  let baseUrl: string;
  if (useSigv4) {
    // No `/agent` segment: this is an `/internal/*` endpoint and must hit the
    // API Gateway `/internal/{proxy+}` route (internal-plane ALB). See the
    // function doc — the `/agent` route's edge ALB 403s `/internal/*` (#4343).
    baseUrl = gatewayEndpoint;
  } else if (gatewayUrl && apiKey) {
    baseUrl = gatewayUrl;
  } else {
    throw new Error(
      '[TokenBroker] Gateway not configured (need ADP_GATEWAY_ENDPOINT, or ' +
        'VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY). Refusing to fall back to a local mint.',
    );
  }

  const endpoint = `${baseUrl}/internal/v1/github-installation-token`;
  const authority = process.env.ADP_AGENT_AUTHORITY_ENABLED === 'true';
  if (authority) {
    const url = new URL(endpoint);
    if (!useSigv4 || url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
      throw new Error('Worker credential broker requires HTTPS and SigV4');
    }
  }
  const payload: Record<string, unknown> = {
    installation_id: Number(req.installationId),
    repo_owner: req.repoOwner,
    repo_name: req.repoName,
    purpose: req.purpose || 'agent run GitHub token refresh (broker)',
  };
  const invocationId = req.invocationId || process.env.ADP_MESSAGE_ID || '';
  if (invocationId) {
    payload.invocation_id = invocationId;
  }
  const body = JSON.stringify(payload);

  const headers: Record<string, string> = useSigv4
    ? await sigv4Headers(endpoint, body, region, authority ? workerIdentityHeaders() : {})
    : { 'X-Internal-Api-Key': apiKey, 'Content-Type': 'application/json' };

  const resp = await fetch(endpoint, {
    method: 'POST',
    headers,
    body,
    redirect: 'error',
    signal: AbortSignal.timeout(15000),
  });

  if (!resp.ok) {
    let detail = '';
    try {
      detail = await resp.text();
    } catch {
      /* body unavailable — the status alone is the signal */
    }
    throw new Error(`[TokenBroker] Gateway returned ${resp.status} minting installation token: ${detail}`);
  }

  const result = (await resp.json()) as GithubInstallationTokenResponse;
  if (!result.token) {
    throw new Error('[TokenBroker] Gateway response missing \'token\'');
  }
  if (!result.expires_at) {
    // A token we cannot schedule a refresh for is not usable — guessing the
    // expiry is what makes runs die unpredictably at the 1-hour mark.
    throw new Error('[TokenBroker] Gateway response missing \'expires_at\'');
  }

  const expiresAt = new Date(result.expires_at);
  if (Number.isNaN(expiresAt.getTime())) {
    throw new Error(`[TokenBroker] Gateway returned unparseable expires_at: ${result.expires_at}`);
  }

  return { token: result.token, expiresAt };
}
