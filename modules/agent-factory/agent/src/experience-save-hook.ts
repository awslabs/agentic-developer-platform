/**
 * Experience-Save Post-Task Hook — Issue #1294
 *
 * After an agent task completes, this hook extracts substantive learnings
 * from the agent's output (the "### Learnings" section) and persists them
 * to the personal-context store via the Context MCP Server's `experience`
 * tool (action=save).
 *
 * Design principles:
 * - Gated by PERSONAL_CONTEXT_SAVE_ENABLED (default false)
 * - Non-blocking: save failures are logged but never fail the parent task
 * - No-secrets guard: learnings containing secret patterns are skipped
 * - Per-task cap: at most MAX_LEARNINGS_PER_TASK saves (default 5)
 * - Uses trusted identity headers from #1.2 (never from agent input)
 *
 * References:
 * - Design: #1283 (Section 2 — write path / save hook)
 * - Identity: #1289 (personal-context-headers.ts)
 * - Experience tool: modules/agent-context/personal_context/experience_tool.py
 */

import { PersonalContextHeaders } from './complex-task-chat/personal-context-headers';

// ============================================================================
// Configuration (read at call time for testability)
// ============================================================================

/** Feature flag — hook is off unless explicitly enabled. */
export function isExperienceSaveEnabled(): boolean {
  return (process.env.PERSONAL_CONTEXT_SAVE_ENABLED ?? 'false') === 'true';
}

/** Maximum learnings to save per task (prevents noise). */
export function getMaxLearningsPerTask(): number {
  return Math.max(1, parseInt(process.env.PERSONAL_CONTEXT_MAX_LEARNINGS ?? '5', 10));
}

/** URL of the Context MCP Server. */
function getContextMcpServerUrl(): string {
  return process.env.CONTEXT_MCP_SERVER_URL ?? '';
}

// ============================================================================
// Secret-detection patterns (shared with sanitizeMemory in agent-worker.ts)
// ============================================================================

const SECRET_PATTERNS: RegExp[] = [
  /(?:AKIA|ASIA)[A-Z0-9]{16}/,                          // AWS access key IDs
  /ghp_[A-Za-z0-9]{36,}/,                               // GitHub PATs
  /ghs_[A-Za-z0-9]{36,}/,                               // GitHub App installation tokens
  /ghu_[A-Za-z0-9]{36,}/,                               // GitHub user-to-server tokens
  /-----BEGIN[A-Z ]*PRIVATE KEY-----/,                   // PEM keys (opening)
  /eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/, // JWTs
  /sk-[A-Za-z0-9]{32,}/,                                 // OpenAI/Anthropic API keys
  /xox[bpras]-[A-Za-z0-9-]{10,}/,                        // Slack tokens
  /(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['"][^'"]{8,}['"]/i, // key=value secrets
  /(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}/i,  // unquoted secrets
];

// ============================================================================
// Types
// ============================================================================

export interface ExperienceSaveConfig {
  /** The agent's full response text (should contain a Learnings section). */
  agentOutput: string;
  /** Agent persona (e.g. 'developer', 'architect'). */
  persona: string;
  /** Trusted identity headers from dispatch metadata (#1.2). */
  identityHeaders: PersonalContextHeaders | null;
  /** Optional context metadata (issue number, PR, run ID). */
  taskContext?: Record<string, string>;
  /** Logger function. */
  log?: (level: string, message: string, context?: Record<string, unknown>) => void;
}

export interface SaveResult {
  /** Number of learnings successfully saved. */
  saved: number;
  /** Number skipped (secrets, empty, cap exceeded). */
  skipped: number;
  /** Error messages for failed saves (if any). */
  errors: string[];
}

// ============================================================================
// Public API
// ============================================================================

/**
 * Extract learnings from agent output and save them to personal-context.
 *
 * This is the top-level hook function called from the agent-worker's
 * completion path. It is intentionally non-throwing: any error is caught,
 * logged, and reported in the result.
 */
export async function saveExperienceLearnings(config: ExperienceSaveConfig): Promise<SaveResult> {
  const { agentOutput, persona, identityHeaders, taskContext, log } = config;
  const result: SaveResult = { saved: 0, skipped: 0, errors: [] };

  // Gate 1: feature flag (read at call time for testability)
  if (!isExperienceSaveEnabled()) {
    return result;
  }

  // Gate 2: identity must be present (fail-closed)
  if (!identityHeaders) {
    log?.('WARN', '[experience-save] No identity headers — skipping save (fail-closed)');
    return result;
  }

  // Gate 3: Context MCP Server URL must be configured
  const serverUrl = getContextMcpServerUrl();
  if (!serverUrl) {
    log?.('WARN', '[experience-save] CONTEXT_MCP_SERVER_URL not set — skipping save');
    return result;
  }

  // Extract learnings from the agent output
  const learnings = extractLearnings(agentOutput);
  if (learnings.length === 0) {
    log?.('INFO', '[experience-save] No learnings found in agent output');
    return result;
  }

  // Apply per-task cap
  const maxLearnings = getMaxLearningsPerTask();
  const capped = learnings.slice(0, maxLearnings);
  const cappedCount = learnings.length - capped.length;
  if (cappedCount > 0) {
    result.skipped += cappedCount;
    log?.('INFO', `[experience-save] Capped: ${cappedCount} learnings over limit of ${maxLearnings}`);
  }

  // Save each learning (skip those containing secrets)
  for (const learning of capped) {
    if (containsSecret(learning)) {
      result.skipped++;
      log?.('INFO', '[experience-save] Skipped learning containing secret pattern');
      continue;
    }

    try {
      await callExperienceSave({
        content: learning,
        persona,
        identityHeaders,
        context: taskContext,
        serverUrl,
      });
      result.saved++;
    } catch (err) {
      const msg = (err as Error).message || 'Unknown error';
      result.errors.push(msg);
      log?.('WARN', `[experience-save] Save failed: ${msg}`);
    }
  }

  log?.('INFO', `[experience-save] Done: saved=${result.saved}, skipped=${result.skipped}, errors=${result.errors.length}`);
  return result;
}

// ============================================================================
// Extraction
// ============================================================================

/**
 * Parse the "### Learnings" section from the agent's end-of-task output.
 *
 * The agent-worker prompt instructs agents to include a "### Learnings"
 * section in their completion summary. This parser extracts individual
 * bullet points from that section.
 *
 * Returns an array of learning strings (trimmed, non-empty).
 */
export function extractLearnings(text: string): string[] {
  if (!text) return [];

  // Match the Learnings section heading (### Learnings or ## Learnings)
  // and capture content until the next heading or end of text.
  const learningsMatch = text.match(
    /#{2,3}\s+Learnings\s*\n([\s\S]*?)(?=\n#{2,3}\s|\n```\s*$|$)/i,
  );

  if (!learningsMatch) return [];

  const section = learningsMatch[1];

  // Extract bullet points (lines starting with - or *)
  const bullets = section
    .split('\n')
    .map(line => line.replace(/^\s*[-*]\s*/, '').trim())
    .filter(line => line.length > 0)
    // Skip lines that are just template placeholders
    .filter(line => !line.startsWith('[') || !line.endsWith(']'));

  return bullets;
}

// ============================================================================
// No-Secrets Guard
// ============================================================================

/**
 * Check if text contains a likely secret/credential pattern.
 *
 * Uses the same patterns as sanitizeMemory in agent-worker.ts to ensure
 * consistency. Returns true if any pattern matches (learning should be skipped).
 */
export function containsSecret(text: string): boolean {
  return SECRET_PATTERNS.some(pattern => pattern.test(text));
}

// ============================================================================
// MCP Client Call
// ============================================================================

interface ExperienceSavePayload {
  content: string;
  persona: string;
  identityHeaders: PersonalContextHeaders;
  context?: Record<string, string>;
  serverUrl: string;
}

/**
 * Call the Context MCP Server's experience tool with action=save.
 *
 * Uses HTTP POST to /call endpoint with identity headers injected.
 */
async function callExperienceSave(payload: ExperienceSavePayload): Promise<void> {
  const { content, persona, identityHeaders, context, serverUrl } = payload;

  const body = JSON.stringify({
    name: 'experience',
    arguments: {
      action: 'save',
      persona,
      content,
      learning_type: 'task_learning',
      context: context ?? {},
      visibility: 'private',
    },
  });

  const response = await fetch(`${serverUrl}/call`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Owner-Sub': identityHeaders['X-Owner-Sub'],
      'X-Tenant-Id': identityHeaders['X-Tenant-Id'],
    },
    body,
  });

  if (!response.ok) {
    const text = await response.text().catch(() => 'no body');
    throw new Error(`Experience save failed: HTTP ${response.status} — ${text}`);
  }
}
