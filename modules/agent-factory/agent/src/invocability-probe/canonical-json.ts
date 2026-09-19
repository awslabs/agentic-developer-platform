import { createHash } from 'node:crypto';

export const REQUEST_SHAPE_NORMALIZATION = 'claude-code-current-date-reminder-v1';
const SDK_DATE_REMINDER = /# currentDate\nToday's date is (\d{4}-\d{2}-\d{2})\./g;

/**
 * Claude Code 0.3.220 injects the wall-clock date into one text block of the
 * first request message (the block index differs across model families),
 * even when CLAUDE_CODE_OVERRIDE_DATE is set. The date is non-semantic probe
 * context, so normalize only that exact reminder. Every other byte remains in
 * the digest; if the SDK moves or reformats the reminder, the digest fails
 * closed until the new body is reviewed.
 */
export function normalizeRequestShape(value: unknown): unknown {
  const root = value as { messages?: unknown };
  if (!root || typeof root !== 'object' || !Array.isArray(root.messages)) return value;
  const first = root.messages[0] as { content?: unknown } | undefined;
  if (!first || typeof first !== 'object' || !Array.isArray(first.content)) return value;
  const candidates = first.content.flatMap((block, index) => {
    const reminder = block as { text?: unknown } | undefined;
    if (!reminder || typeof reminder !== 'object' || typeof reminder.text !== 'string') return [];
    const text = reminder.text;
    return [...text.matchAll(SDK_DATE_REMINDER)].map(() => ({ index, reminder, text }));
  });
  if (candidates.length !== 1) return value;
  const { index, reminder, text } = candidates[0];
  const normalizedText = text.replace(
    SDK_DATE_REMINDER,
    "# currentDate\nToday's date is <ADP_PROBE_DATE>.",
  );
  const content = [...first.content];
  content[index] = { ...reminder, text: normalizedText };
  const messages = [...root.messages];
  messages[0] = { ...first, content };
  return { ...(value as Record<string, unknown>), messages };
}

/**
 * RFC-8785-style ordering for the JSON values emitted by the Claude SDK.
 *
 * The SDK body contains only ordinary JSON primitives.  Sorting object keys
 * recursively (while retaining array order) gives the probe a stable digest
 * independent of insignificant whitespace and property insertion order.
 */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') {
    return JSON.stringify(value);
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('request body contains a non-finite number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(',')}]`;
  }
  if (typeof value === 'object') {
    const object = value as Record<string, unknown>;
    const fields = Object.keys(object)
      .sort()
      .filter((key) => object[key] !== undefined)
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`);
    return `{${fields.join(',')}}`;
  }
  throw new Error(`request body contains unsupported JSON value: ${typeof value}`);
}

export function requestShapeSha256(body: Buffer | string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body.toString());
  } catch (error) {
    throw new Error(`Claude SDK emitted a non-JSON Bedrock request: ${(error as Error).message}`);
  }
  return createHash('sha256').update(canonicalJson(normalizeRequestShape(parsed)), 'utf8').digest('hex');
}
