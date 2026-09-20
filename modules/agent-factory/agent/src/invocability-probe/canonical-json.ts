import { createHash } from 'node:crypto';

export const REQUEST_SHAPE_NORMALIZATION = 'claude-code-probe-date-device-v2';
const SDK_DATE_REMINDER = /# currentDate\nToday's date is (\d{4}-\d{2}-\d{2})\./g;

/** Normalize only the SDK's per-installation identifier, never account/session identity. */
function normalizeProbeDevice(value: unknown): unknown {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const root = value as Record<string, unknown>;
  const metadata = root.metadata;
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return value;
  const fields = metadata as Record<string, unknown>;
  if (typeof fields.user_id !== 'string') return value;
  try {
    const identity = JSON.parse(fields.user_id) as Record<string, unknown>;
    if (!identity || typeof identity !== 'object' || Array.isArray(identity)
        || Object.keys(identity).sort().join(',') !== 'account_uuid,device_id,session_id'
        || identity.account_uuid !== ''
        || identity.session_id !== '00000000-0000-4000-8000-000000000002'
        || typeof identity.device_id !== 'string' || !/^[0-9a-f]{64}$/.test(identity.device_id)) return value;
    return { ...root, metadata: { ...fields, user_id: JSON.stringify({
      ...identity, device_id: '<ADP_PROBE_DEVICE>',
    }) } };
  } catch {
    return value;
  }
}

/**
 * Claude Code 0.3.220 injects the wall-clock date into one text block of the
 * first request message (the block index differs across model families),
 * even when CLAUDE_CODE_OVERRIDE_DATE is set. The date is non-semantic probe
 * context. Its device identifier is also random in fresh containers; normalize
 * only the known anonymous, fixed-session probe identity shape. All model,
 * tool, token, account and session fields remain covered by the digest.
 */
export function normalizeRequestShape(value: unknown): unknown {
  value = normalizeProbeDevice(value);
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
