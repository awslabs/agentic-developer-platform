/**
 * Recall-at-task-start hook — Issue #1293 (EPIC #1287)
 *
 * Pre-task hook that retrieves the user's most relevant prior learnings
 * via the `experience` recall tool on the Context MCP Server. The recalled
 * learnings are injected into the agent's system prompt as a clearly-labeled
 * "prior experience" section.
 *
 * Behavior:
 * - Gated by PERSONAL_CONTEXT_RECALL_ENABLED (default: false).
 * - Uses trusted identity headers from #1.2 (X-Owner-Sub, X-Tenant-Id).
 * - Graceful degradation: errors/timeouts → proceed without recall (never blocks task).
 * - Token cap: truncates recalled content to a configurable budget.
 *
 * Security: recall query is derived from the task message (user input), but the
 * identity headers come from trusted dispatch metadata (SQS envelope), never from
 * agent/LLM input. The Context MCP Server enforces owner-scoped isolation.
 */

import { PersonalContextIdentity, getPersonalContextHeaders } from './personal-context-headers';

/**
 * Feature flag: enable recall-at-task-start. Default off until validated.
 */
export const RECALL_ENABLED =
  (process.env.PERSONAL_CONTEXT_RECALL_ENABLED ?? 'false') === 'true';

/**
 * Maximum token budget for recalled content. Prevents prompt bloat.
 * Configurable via env var; default 800 tokens (~3200 chars).
 */
const RECALL_TOKEN_CAP = Number(process.env.PERSONAL_CONTEXT_RECALL_TOKEN_CAP ?? 800);

/**
 * Timeout for the recall HTTP call (ms). Must be short — recall is best-effort.
 */
const RECALL_TIMEOUT_MS = Number(process.env.PERSONAL_CONTEXT_RECALL_TIMEOUT_MS ?? 3000);

/**
 * Context MCP Server URL. Internal K8s service address.
 */
const CONTEXT_MCP_URL =
  process.env.CONTEXT_MCP_SERVER_URL ??
  'http://context-mcp-server.agent-context.svc.cluster.local:8080';

/**
 * Maximum number of recall results to request.
 */
const RECALL_LIMIT = 5;

/**
 * A single recalled learning entry from the experience tool.
 */
export interface RecalledLearning {
  id: string;
  content: string;
  persona: string;
  learning_type: string;
  confidence: number;
  decay_score: number;
  score: number;
  visibility: string;
  created_at: string;
}

/**
 * Result of the recall-at-task-start hook.
 */
export interface RecallResult {
  /** Whether recall was attempted (false when disabled or no identity). */
  attempted: boolean;
  /** Recalled learnings (empty on error/timeout/disabled). */
  learnings: RecalledLearning[];
  /** Formatted prompt section to inject (empty string if no learnings). */
  promptSection: string;
  /** Warning message if recall failed (for logging). */
  warning?: string;
}

/**
 * Perform the recall-at-task-start hook.
 *
 * Call this AFTER personal-context identity is established and BEFORE system
 * prompt assembly. Returns a formatted prompt section to inject.
 *
 * @param identity - Trusted personal-context identity (from dispatch metadata)
 * @param taskQuery - Task message/query to use as the recall query
 * @param persona - Agent persona name (for persona-scoped recall)
 * @returns RecallResult with formatted prompt section
 */
export async function recallAtTaskStart(
  identity: PersonalContextIdentity | null,
  taskQuery: string,
  persona: string,
): Promise<RecallResult> {
  // Gate: feature flag
  if (!RECALL_ENABLED) {
    return { attempted: false, learnings: [], promptSection: '' };
  }

  // Gate: identity required for recall
  if (!identity) {
    return {
      attempted: false,
      learnings: [],
      promptSection: '',
      warning: 'No personal-context identity available; skipping recall',
    };
  }

  const headers = getPersonalContextHeaders(identity);
  if (!headers) {
    return {
      attempted: false,
      learnings: [],
      promptSection: '',
      warning: 'Could not build personal-context headers; skipping recall',
    };
  }

  // Derive query from the task message (truncate to avoid oversized request)
  const query = taskQuery.slice(0, 500).trim();
  if (!query) {
    return {
      attempted: true,
      learnings: [],
      promptSection: '',
      warning: 'Empty task query; skipping recall',
    };
  }

  try {
    const learnings = await callRecall(headers, query, persona);
    const promptSection = formatRecallSection(learnings);
    return { attempted: true, learnings, promptSection };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return {
      attempted: true,
      learnings: [],
      promptSection: '',
      warning: `Recall failed (non-fatal): ${message}`,
    };
  }
}

/**
 * Call the experience recall endpoint on the Context MCP Server.
 *
 * @internal Exported for testing only.
 */
export async function callRecall(
  headers: { 'X-Owner-Sub': string; 'X-Tenant-Id': string },
  query: string,
  persona: string,
): Promise<RecalledLearning[]> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), RECALL_TIMEOUT_MS);

  try {
    const response = await fetch(`${CONTEXT_MCP_URL}/tools/call`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Owner-Sub': headers['X-Owner-Sub'],
        'X-Tenant-Id': headers['X-Tenant-Id'],
      },
      body: JSON.stringify({
        name: 'experience',
        arguments: {
          action: 'recall',
          persona,
          query,
          limit: RECALL_LIMIT,
        },
      }),
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(`Context MCP Server returned ${response.status}: ${response.statusText}`);
    }

    const data = (await response.json()) as { status?: string; results?: unknown[] };

    // The experience tool returns { status: "ok", query, results: [...], total }
    if (data.status !== 'ok' || !Array.isArray(data.results)) {
      return [];
    }

    return data.results as RecalledLearning[];
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * Format recalled learnings into an XML prompt section.
 *
 * Respects the token cap: iteratively adds learnings until the cap is reached.
 * Each learning is labeled with confidence and a caveat that these are recalled
 * memories that may be stale.
 */
export function formatRecallSection(learnings: RecalledLearning[]): string {
  if (learnings.length === 0) {
    return '';
  }

  const parts: string[] = [];
  parts.push('<prior-experience>');
  parts.push(
    'The following are relevant learnings recalled from your prior experience with this user. ' +
    'These are possibly-stale memories — weigh them as context, do not blindly trust them.',
  );

  let tokenCount = estimateTokens(parts.join('\n'));
  // Reserve tokens for closing tag
  const closingTag = '</prior-experience>';
  const closingTokens = estimateTokens(closingTag);

  for (const learning of learnings) {
    const entry = formatLearningEntry(learning);
    const entryTokens = estimateTokens(entry);

    if (tokenCount + entryTokens + closingTokens > RECALL_TOKEN_CAP) {
      break;
    }

    parts.push(entry);
    tokenCount += entryTokens;
  }

  // Only emit if at least one learning was included
  if (parts.length <= 2) {
    // Only header lines, no actual learnings fit
    return '';
  }

  parts.push(closingTag);
  return parts.join('\n');
}

/**
 * Format a single learning entry as XML.
 */
function formatLearningEntry(learning: RecalledLearning): string {
  const confidence = Math.round(learning.confidence * 100);
  return (
    `  <learning id="${learning.id}" confidence="${confidence}%" relevance="${Math.round(learning.score * 100)}%">\n` +
    `    ${learning.content}\n` +
    `  </learning>`
  );
}

/**
 * Estimate token count from text (4 chars per token approximation).
 */
function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}
