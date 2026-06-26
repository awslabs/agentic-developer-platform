/**
 * Correlation marker helper for outbound GitHub actions (TypeScript).
 *
 * Mirrors the Python `lib/correlation_marker.py` in the worker image.
 * Prepends an invisible HTML comment containing correlation context to
 * comment/PR bodies so downstream webhook handlers can trace provenance.
 *
 * Phase 2-d of EPIC #779.
 */

/**
 * Idempotently prepend an HTML correlation marker to a comment/PR body.
 *
 * Reads correlation context from ADP_CORRELATION_ID, ADP_ROOT_HUMAN_ID,
 * ADP_IS_HUMAN_ROOTED, ADP_MESSAGE_ID, and ADP_CHAIN_DEPTH env vars.
 * No-op if:
 *   - Marker already present (first 500 chars scanned)
 *   - Required env vars are missing
 *
 * @param body - The comment or PR body text.
 * @param dispatch_persona - Optional. When set, includes `adp-dispatch:<persona>`
 *   in the marker, signaling an intentional cross-issue bot→bot dispatch.
 *   Status/boilerplate comments MUST NOT pass this parameter (issue #2149).
 */
export function prependCorrelationMarker(body: string, dispatch_persona?: string): string {
  // Idempotency: don't double-prepend
  if (body.slice(0, 500).includes('<!-- adp-correlation:')) {
    return body;
  }

  const correlationId = process.env.ADP_CORRELATION_ID;
  const rootHumanId = process.env.ADP_ROOT_HUMAN_ID;
  const isHumanRooted = process.env.ADP_IS_HUMAN_ROOTED || 'false';

  if (!correlationId || !rootHumanId) {
    return body; // Nothing to inject — fail-safe
  }

  // Build marker parts — optional fields appended only when present.
  const parts: string[] = [
    `adp-correlation:${correlationId}`,
    `adp-root-human:${rootHumanId}`,
    `adp-is-human-rooted:${isHumanRooted}`,
  ];

  const invocation = process.env.ADP_MESSAGE_ID;
  if (invocation) {
    parts.push(`adp-invocation:${invocation}`);
  }

  const chainDepth = process.env.ADP_CHAIN_DEPTH;
  if (chainDepth) {
    parts.push(`adp-chain-depth:${chainDepth}`);
  }

  // Issue #2149: dispatch marker for intentional cross-issue bot→bot triggers.
  if (dispatch_persona) {
    parts.push(`adp-dispatch:${dispatch_persona}`);
  }

  const marker = `<!-- ${parts.join(' ')} -->\n`;
  return marker + body;
}
