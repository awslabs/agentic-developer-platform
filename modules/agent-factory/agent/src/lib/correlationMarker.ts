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
 * and ADP_IS_HUMAN_ROOTED env vars. No-op if:
 *   - Marker already present (first 500 chars scanned)
 *   - Required env vars are missing
 */
export function prependCorrelationMarker(body: string): string {
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

  const marker = `<!-- adp-correlation:${correlationId} adp-root-human:${rootHumanId} adp-is-human-rooted:${isHumanRooted} -->\n`;
  return marker + body;
}
