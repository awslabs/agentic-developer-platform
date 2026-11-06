import { createHash } from 'node:crypto';

/**
 * Bump whenever a normalization rule below changes: the gateway and agent
 * manifests carry this value and both sides refuse a manifest generated under
 * another revision.
 *
 * v5 (#7993): Claude Code 0.3.283 emits its probe context as an "# Environment"
 * block (shell, kernel version, model, deferred tools, token budget) followed by
 * the USD budget reminder and the date. Opus/Sonnet 5 place that block in a
 * mid-conversation system message; the other models split it into separate
 * `<system-reminder>` user blocks without a trailing newline. The v4 rules only
 * matched the 0.3.220 wrappers, so shell, kernel, budget and date leaked into
 * every digest and no committed manifest could equal a live probe.
 */
export const REQUEST_SHAPE_NORMALIZATION = 'claude-code-probe-context-v5';

/** The SDK beta under which Claude Code moves its context into messages[1] (role system). */
const MID_CONVERSATION_SYSTEM_BETA = 'mid-conversation-system-2026-04-07';
const PROBE_SESSION_ID = '00000000-0000-4000-8000-000000000002';

/**
 * Only the zero-spend, equal-remaining reminder for a positive budget is
 * normalized. The runner receives the gateway's per-attempt budget unchanged;
 * the generator uses a loopback-only budget. Spend updates, unequal remaining
 * amounts and a zero budget stay in the digest.
 */
const SDK_INITIAL_BUDGET_REMINDER = /USD budget: \$0\/\$(\d+(?:\.\d+)?); \$\1 remaining/g;
/** Emitted from the wall clock even when CLAUDE_CODE_OVERRIDE_DATE is set. */
const SDK_DATE_REMINDER = /Today's date is (\d{4}-\d{2}-\d{2})\./g;
/** Host facts of the pod/node the SDK happens to run on; the platform line stays covered. */
const SDK_ENVIRONMENT_HEADING = /^(?:<system-reminder>\n)?# Environment\n/;
const SDK_ENVIRONMENT_HOST = /\n - Shell: [^\n]*\n - OS Version: [^\n]*/g;

interface ContextRule {
  name: string;
  pattern: RegExp;
  replacement: string;
  /** Additional acceptance of one regex match (e.g. a positive budget). */
  accept?: (match: RegExpMatchArray) => boolean;
  /** Restrict the rule to text blocks of a known shape. */
  applies?: (text: string) => boolean;
}

const PROBE_CONTEXT_RULES: readonly ContextRule[] = [
  {
    name: 'initial budget reminder',
    pattern: SDK_INITIAL_BUDGET_REMINDER,
    accept: (match) => Number.isFinite(Number(match[1])) && Number(match[1]) > 0,
    replacement: 'USD budget: $0/$<ADP_PROBE_BUDGET>; $<ADP_PROBE_BUDGET> remaining',
  },
  {
    name: 'current date reminder',
    pattern: SDK_DATE_REMINDER,
    replacement: "Today's date is <ADP_PROBE_DATE>.",
  },
  {
    name: 'environment host lines',
    pattern: SDK_ENVIRONMENT_HOST,
    applies: (text) => SDK_ENVIRONMENT_HEADING.test(text),
    replacement: '\n - Shell: <ADP_PROBE_SHELL>\n - OS Version: <ADP_PROBE_OS_VERSION>',
  },
];

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

/**
 * The SDK writes its probe context into the first (user) message and, under the
 * mid-conversation-system beta, into a second system-role message. Nothing else
 * (later turns, the top-level system prompt, tool declarations) is ever scanned.
 */
function contextMessageIndexes(root: JsonObject): number[] {
  const messages = root.messages;
  if (!Array.isArray(messages) || messages.length === 0) return [];
  const indexes = [0];
  const second = messages[1];
  if (Array.isArray(root.anthropic_beta) && root.anthropic_beta.includes(MID_CONVERSATION_SYSTEM_BETA)
      && isObject(second) && second.role === 'system') {
    indexes.push(1);
  }
  return indexes;
}

interface ContextMatch { messageIndex: number; blockIndex: number; match: RegExpMatchArray }

function matchesOf(rule: ContextRule, text: string): RegExpMatchArray[] {
  if (rule.applies && !rule.applies(text)) return [];
  return [...text.matchAll(rule.pattern)].filter((match) => rule.accept?.(match) ?? true);
}

function applyRule(root: JsonObject, rule: ContextRule): JsonObject {
  const messages = root.messages as unknown[];
  const found: ContextMatch[] = [];
  for (const messageIndex of contextMessageIndexes(root)) {
    const message = messages[messageIndex];
    if (!isObject(message) || !Array.isArray(message.content)) continue;
    message.content.forEach((block, blockIndex) => {
      if (!isObject(block) || typeof block.text !== 'string') return;
      for (const match of matchesOf(rule, block.text)) found.push({ messageIndex, blockIndex, match });
    });
  }
  // A rule fires only for exactly one occurrence. Zero means the SDK changed its
  // context shape; more than one means the probe's own prompt or the runtime
  // injected another copy. Both must surface as a shape mismatch, not be hidden.
  if (found.length !== 1) return root;
  const [{ messageIndex, blockIndex, match }] = found;
  const message = messages[messageIndex] as JsonObject;
  const content = [...(message.content as unknown[])];
  const block = content[blockIndex] as JsonObject;
  const text = block.text as string;
  const start = match.index ?? 0;
  content[blockIndex] = {
    ...block,
    text: `${text.slice(0, start)}${rule.replacement}${text.slice(start + match[0].length)}`,
  };
  const nextMessages = [...messages];
  nextMessages[messageIndex] = { ...message, content };
  return { ...root, messages: nextMessages };
}

/** Normalize only the SDK's per-installation identifier, never account/session identity. */
function normalizeProbeDevice(value: unknown): unknown {
  if (!isObject(value) || !isObject(value.metadata) || typeof value.metadata.user_id !== 'string') return value;
  const fields = value.metadata;
  try {
    const identity = JSON.parse(fields.user_id as string) as Record<string, unknown>;
    if (!isObject(identity)
        || Object.keys(identity).sort().join(',') !== 'account_uuid,device_id,session_id'
        || identity.account_uuid !== ''
        || identity.session_id !== PROBE_SESSION_ID
        || typeof identity.device_id !== 'string' || !/^[0-9a-f]{64}$/.test(identity.device_id)) return value;
    return { ...value, metadata: { ...fields, user_id: JSON.stringify({
      ...identity, device_id: '<ADP_PROBE_DEVICE>',
    }) } };
  } catch {
    return value;
  }
}

/**
 * Remove the run-specific probe context Claude Code injects into its request
 * (random device id, initial USD budget reminder, wall-clock date, host shell
 * and kernel version) so that the committed manifest, in-image generation and a
 * live probe produce the same digest. Provider model, tool declarations, token
 * limits, thinking configuration, prompts, account and session identity remain
 * covered. Each rule fires for exactly one occurrence or not at all.
 */
export function normalizeRequestShape(value: unknown): unknown {
  value = normalizeProbeDevice(value);
  if (!isObject(value) || !Array.isArray(value.messages)) return value;
  let root: JsonObject = value;
  for (const rule of PROBE_CONTEXT_RULES) root = applyRule(root, rule);
  return root;
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
