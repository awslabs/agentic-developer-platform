/**
 * Provenance client for the Node agent runtime.
 *
 * Posts action provenance records to the gateway's /internal/v1/provenance
 * endpoint after successful outbound GitHub actions. Fail-soft.
 *
 * Phase 2-d of EPIC #779.
 *
 * Issue #4029: this client had the same three defects as its Python twin — it
 * sent source_event as a string where the gateway requires an object, omitted
 * org_id where a non-null string is required (either alone 422s), and read a
 * 'provenance_id' response key the gateway has never returned. It also had NO
 * SigV4 path, so in IRSA-only environments (no VAULT_INTERNAL_API_KEY) it
 * returned null without attempting the write at all.
 *
 * The request body is pinned by a golden fixture shared with the Python worker
 * and gateway suites: contracts/provenance/v1/create-provenance-request.golden.json.
 * Do not change buildProvenanceBody without updating that fixture.
 */

export interface ProvenancePayload {
  actorUserId: string;
  triggeredBy: string | null;
  rootHumanId: string;
  isHumanRooted: boolean;
  actionKind: string;
  /** Structured event object — the column is JSONB; a string is rejected with 422. */
  sourceEvent: Record<string, unknown>;
  correlationId: string;
  /** Tenant the action is attributed to. Required, non-null: the column is NOT NULL. */
  orgId: string;
  parentInvocationId?: string | null;
}

/** The gateway's CreateProvenanceResponse. Note: `id`, never `provenance_id`. */
interface CreateProvenanceResponse {
  id?: string;
  created_at?: string;
}

/**
 * Build the request body for POST /internal/v1/provenance.
 *
 * Exported so the wire contract is assertable without mocking fetch — this is
 * what the shared golden fixture pins. Keys and types must match the gateway's
 * CreateProvenanceRequest exactly.
 */
export function buildProvenanceBody(payload: ProvenancePayload): Record<string, unknown> {
  return {
    actor_user_id: payload.actorUserId,
    triggered_by: payload.triggeredBy,
    root_human_id: payload.rootHumanId,
    is_human_rooted: payload.isHumanRooted,
    action_kind: payload.actionKind,
    source_event: payload.sourceEvent,
    correlation_id: payload.correlationId,
    org_id: payload.orgId,
    parent_invocation_id: payload.parentInvocationId ?? null,
  };
}

/**
 * Sign a request with SigV4 using the pod's ambient (IRSA) credentials.
 *
 * Mirrors the Python client's _sigv4_sign_request. Uses @smithy/signature-v4 +
 * @aws-sdk/credential-provider-node, which are already present in the prod
 * dependency tree (see src/sigv4-proxy.ts, which relies on the same packages).
 * Imported lazily so the shared-secret path costs nothing.
 */
async function sigv4Headers(
  endpoint: string,
  body: string,
  region: string,
): Promise<Record<string, string>> {
  const { SignatureV4 } = await import('@smithy/signature-v4');
  const { Hash } = await import('@smithy/hash-node');
  const { defaultProvider } = await import('@aws-sdk/credential-provider-node');

  const url = new URL(endpoint);
  const signer = new SignatureV4({
    credentials: defaultProvider(),
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
    },
    body,
  });

  return signed.headers as Record<string, string>;
}

/**
 * Post an action provenance record to the gateway. Fail-soft.
 *
 * Prefers SigV4 via API Gateway (ADP_GATEWAY_ENDPOINT) and falls back to the
 * legacy shared secret (VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY), matching
 * the Python client's transport selection.
 *
 * Returns the new row's id on success, or null on failure.
 */
export async function postProvenance(payload: ProvenancePayload): Promise<string | null> {
  const gatewayEndpoint = (process.env.ADP_GATEWAY_ENDPOINT || '').replace(/\/+$/, '');
  const gatewayUrl = (process.env.VAULT_GATEWAY_URL || '').replace(/\/+$/, '');
  const apiKey = process.env.VAULT_INTERNAL_API_KEY || '';
  const region = process.env.AWS_REGION || 'us-east-1';

  const useSigv4 = Boolean(gatewayEndpoint);

  let baseUrl: string;
  if (useSigv4) {
    baseUrl = `${gatewayEndpoint}/agent`;
  } else if (gatewayUrl && apiKey) {
    baseUrl = gatewayUrl;
  } else {
    return null; // Not configured — fail-safe
  }

  const endpoint = `${baseUrl}/internal/v1/provenance`;
  const body = JSON.stringify(buildProvenanceBody(payload));

  try {
    const headers: Record<string, string> = useSigv4
      ? await sigv4Headers(endpoint, body, region)
      : { 'X-Internal-Api-Key': apiKey, 'Content-Type': 'application/json' };

    const resp = await fetch(endpoint, {
      method: 'POST',
      headers,
      body,
      signal: AbortSignal.timeout(10000),
    });

    if (!resp.ok) {
      console.warn(`[provenance] Gateway returned ${resp.status}`);
      return null;
    }

    const result = await resp.json() as CreateProvenanceResponse;
    if (!result.id) {
      console.warn('[provenance] Response missing \'id\'');
      return null;
    }
    return result.id;
  } catch (err) {
    console.warn(`[provenance] Failed to post (non-fatal): ${(err as Error).message}`);
    return null;
  }
}
