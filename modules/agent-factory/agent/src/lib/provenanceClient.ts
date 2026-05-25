/**
 * Provenance client for the Node agent runtime.
 *
 * Posts action provenance records to the gateway's /internal/v1/provenance
 * endpoint after successful outbound GitHub actions. Fail-soft.
 *
 * Phase 2-d of EPIC #779.
 */

export interface ProvenancePayload {
  actorUserId: string;
  triggeredBy: string | null;
  rootHumanId: string;
  isHumanRooted: boolean;
  actionKind: string;
  sourceEvent: string;
  correlationId: string;
  orgId?: string | null;
}

/**
 * Post an action provenance record to the gateway. Fail-soft.
 * Returns the provenance_id on success, or null on failure.
 */
export async function postProvenance(payload: ProvenancePayload): Promise<string | null> {
  const gatewayUrl = (process.env.VAULT_GATEWAY_URL || '').replace(/\/+$/, '');
  const apiKey = process.env.VAULT_INTERNAL_API_KEY || '';

  if (!gatewayUrl || !apiKey) {
    return null; // Not configured — fail-safe
  }

  const endpoint = `${gatewayUrl}/internal/v1/provenance`;
  const body = JSON.stringify({
    actor_user_id: payload.actorUserId,
    triggered_by: payload.triggeredBy,
    root_human_id: payload.rootHumanId,
    is_human_rooted: payload.isHumanRooted,
    action_kind: payload.actionKind,
    source_event: payload.sourceEvent,
    correlation_id: payload.correlationId,
    org_id: payload.orgId ?? null,
  });

  try {
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'X-Internal-Api-Key': apiKey,
        'Content-Type': 'application/json',
      },
      body,
      signal: AbortSignal.timeout(10000),
    });

    if (!resp.ok) {
      console.warn(`[provenance] Gateway returned ${resp.status}`);
      return null;
    }

    const result = await resp.json() as { provenance_id?: string };
    return result.provenance_id ?? null;
  } catch (err) {
    console.warn(`[provenance] Failed to post (non-fatal): ${(err as Error).message}`);
    return null;
  }
}
