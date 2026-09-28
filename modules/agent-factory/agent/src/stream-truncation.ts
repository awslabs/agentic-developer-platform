/**
 * Detects when a model response was severed mid-stream.
 *
 * When the connection to the model is dropped part-way through a streaming
 * response, the Claude Agent SDK does NOT throw. It degrades gracefully:
 * it inserts a sentinel string as the assistant's final text and still emits
 * a `result` message with `subtype: 'success'` and normal-looking turn/cost
 * numbers. That means the harness cannot detect the failure from the result
 * `subtype`, nor from the `$0.00 / 1-turn` infra-failure guard (#2883) — a
 * dropped long turn can show 40+ turns and dollars of spend.
 *
 * Left untreated, the harness posts a "Done / no changes needed" completion
 * whose Summary IS the error string, silently abandons the work, and looks
 * successful to any re-dispatch loop (observed three times on #4450, ~$7
 * wasted). Treating a truncated stream as a failure lets the run be retried
 * and reported honestly.
 *
 * These markers are the exact strings the SDK emits on a mid-stream drop.
 */
export const TRUNCATED_STREAM_MARKERS = [
  'Connection closed mid-response',
  'The response above may be incomplete',
] as const;

/**
 * Returns true if `text` looks like a truncated/severed model stream result.
 * Safe on null/undefined/empty input (returns false).
 */
export function isTruncatedStreamResult(text: string | null | undefined): boolean {
  if (!text) return false;
  return TRUNCATED_STREAM_MARKERS.some((marker) => text.includes(marker));
}
