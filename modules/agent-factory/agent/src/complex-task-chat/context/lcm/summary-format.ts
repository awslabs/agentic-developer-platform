/**
 * XML summary wrapper format for agent-facing injection.
 *
 * Summaries are injected as synthetic user messages with XML metadata
 * so the model can reason about scope and call expand_summary(id) when needed.
 */

export interface SummaryFormatInput {
  summaryId: string;
  kind: 'leaf' | 'condensed';
  depth: number;
  earliestAt: string;
  latestAt: string;
  content: string;
  descendantCount?: number;
}

export function formatSummaryXml(input: SummaryFormatInput): string {
  const descendantCount = input.descendantCount ?? 0;
  return [
    `<summary id="${input.summaryId}" kind="${input.kind}" depth="${input.depth}" descendant_count="${descendantCount}"`,
    `         earliest_at="${input.earliestAt}" latest_at="${input.latestAt}">`,
    `  <content>`,
    `    ${input.content}`,
    `    Expand for details about: exact commands, error strings, intermediate state`,
    `  </content>`,
    `</summary>`,
  ].join('\n');
}

/**
 * Parse a summary ID to extract the session ID.
 * Format: sum_<sessionId>_<hash>
 */
export function parseSessionFromSummaryId(summaryId: string): string | null {
  const parts = summaryId.split('_');
  if (parts.length < 3 || parts[0] !== 'sum') return null;
  // Session ID is everything between first and last underscore segment
  return parts.slice(1, -1).join('_');
}
