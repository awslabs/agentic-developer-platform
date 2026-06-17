/**
 * Run-Completion Event — Issue #1580
 *
 * Emits a structured, always-on event at the end of every agent run,
 * capturing objective signals (files changed, turns, duration, outcome,
 * skills) independent of whether the agent wrote prose learnings.
 *
 * Design principles:
 * - Always-on: fires on EVERY run (success or failure), not gated on prose
 * - Objective: outcome derived from signals, not agent self-report
 * - Non-blocking: emission failure NEVER fails the agent's actual task
 * - Secret-safe: reuses SECRET_PATTERNS from experience-save-hook
 * - Size-bounded: per-field caps prevent unbounded growth
 * - Feature-flagged: RUN_COMPLETION_EVENT_ENABLED (default false)
 *
 * References:
 * - Design: docs/design-notes/1580-run-completion-event.md
 * - Prior art: experience-save-hook.ts (#1294)
 * - Identity: personal-context-headers.ts (#1289)
 */

import { extractLearnings, containsSecret } from './experience-save-hook';

// ============================================================================
// Configuration
// ============================================================================

/** Feature flag — event emission is off unless explicitly enabled. */
export function isRunCompletionEventEnabled(): boolean {
  return (process.env.RUN_COMPLETION_EVENT_ENABLED ?? 'false') === 'true';
}

// ============================================================================
// Types
// ============================================================================

/** Persona enum matching AGENT_TYPE values. */
export type AgentPersona = 'developer' | 'operations' | 'architect' | 'reviewer' | 'product';

/** Outcome derived from objective signals, NEVER from agent self-report. */
export type RunOutcome = 'success' | 'failure' | 'partial';

/** Learning type taxonomy (borrowed from gbrain gstack for sink compatibility). */
export type LearningType =
  | 'pattern'
  | 'pitfall'
  | 'operational'
  | 'investigation'
  | 'architecture'
  | 'tool'
  | 'preference';

/** Structured error encountered during the run. */
export interface ErrorEncountered {
  summary: string;
  resolved: boolean;
}

/** PR opened by this run. */
export interface PrInfo {
  number: number;
  state: string;
}

/** Beads task tracking info. */
export interface BeadsInfo {
  task_id: string;
  status: string;
}

/** Test run results (best-effort extraction). */
export interface TestResults {
  ran: boolean;
  passed?: number;
  failed?: number;
}

/**
 * RunCompletionEvent — emitted once per agent run at the completion block.
 *
 * Schema versioned for forward compatibility. Consumers must tolerate
 * unknown fields (open schema) and missing optional fields.
 */
export interface RunCompletionEvent {
  /** Schema version for forward compatibility. */
  schema_version: '1';

  /** Unique run identifier. Matches CloudWatch log stream naming. */
  run_id: string;

  /** Timestamp of event emission (ISO 8601). */
  timestamp: string;

  // ── Identity ──────────────────────────────────────────────────────────
  /** Agent persona that ran. */
  persona: AgentPersona;

  /** Cognito subject (owner) — from trusted dispatch metadata. */
  owner_sub?: string;

  /** Tenant ID — from trusted dispatch metadata. */
  tenant_id?: string;

  // ── Task context ──────────────────────────────────────────────────────
  issue: {
    number: number;
    title: string;
    repo: string;
    component?: string;
  };

  // ── Outcome (objective — derived, NOT self-reported) ──────────────────
  /** Derived from agentSucceeded + beads status. Never from agent prose. */
  outcome: RunOutcome;

  /** Number of SDK turns (assistant messages). */
  turns: number;

  /** Wall-clock duration of the agent run in milliseconds. */
  duration_ms: number;

  // ── Artifact signals (objective) ──────────────────────────────────────
  /** Files touched during the run (from git diff). Capped at 50. */
  files_touched: string[];

  /** PR opened by this run (if any). */
  pr?: PrInfo;

  /** Beads task tracking. */
  beads?: BeadsInfo;

  /** Skills discovered/invoked during the run. */
  skills_used: string[];

  // ── Best-effort fields (may be empty) ─────────────────────────────────
  /** Structured error extraction (best-effort). */
  errors_encountered: ErrorEncountered[];

  /** Test run results (best-effort extraction). */
  tests?: TestResults;

  /** The agent's own ### Learnings prose (optional, scrubbed). */
  agent_narrative?: string;

  /** Learning type classification (optional, heuristic). */
  learning_type?: LearningType;
}

// ============================================================================
// Caps — prevent unbounded event size
// ============================================================================

/** Maximum files listed in files_touched. */
const MAX_FILES_TOUCHED = 50;

/** Maximum errors listed. */
const MAX_ERRORS = 5;

/** Maximum characters per error summary. */
const MAX_ERROR_SUMMARY_LENGTH = 200;

/** Maximum characters for agent_narrative. */
const MAX_NARRATIVE_LENGTH = 2000;

/** Maximum characters for run_id. */
const MAX_RUN_ID_LENGTH = 128;

// ============================================================================
// Builder input
// ============================================================================

export interface BuildRunCompletionEventInput {
  /** Run ID (e.g. LOG_STREAM value). */
  runId?: string;

  /** Agent persona. */
  persona: string;

  /** Owner sub from trusted dispatch. */
  ownerSub?: string;

  /** Tenant ID from trusted dispatch. */
  tenantId?: string;

  /** Issue number. */
  issueNumber: number;

  /** Issue title. */
  issueTitle: string;

  /** Repository (owner/repo format). */
  repo: string;

  /** Detected component label. */
  component?: string;

  /** Whether the agent run succeeded (from try/catch). */
  agentSucceeded: boolean;

  /** Beads task status (if tracked). */
  beadsTaskId?: string;
  beadsStatus?: string;

  /** Number of turns completed. */
  turns: number;

  /** Run duration in milliseconds. */
  durationMs: number;

  /** Files changed (from git diff). */
  filesTouched: string[];

  /** PR info if one was opened. */
  pr?: PrInfo;

  /** Skills discovered/invoked. */
  skillsUsed: string[];

  /** Agent's full output text (for narrative extraction). */
  agentOutput?: string;

  /** Pre-extracted errors (best-effort). */
  errorsEncountered?: ErrorEncountered[];

  /** Test results (best-effort). */
  tests?: TestResults;
}

// ============================================================================
// Public API
// ============================================================================

/**
 * Derive outcome from objective signals.
 * Never uses agent self-report.
 */
export function deriveOutcome(params: {
  agentSucceeded: boolean;
  beadsStatus?: string;
}): RunOutcome {
  if (!params.agentSucceeded) return 'failure';

  if (params.beadsStatus === 'blocked' || params.beadsStatus === 'failed') {
    return 'partial';
  }

  return 'success';
}

/**
 * Scrub secrets from a text field. Returns scrubbed text.
 * Reuses the same patterns as experience-save-hook.ts.
 */
export function scrubField(text: string): string {
  if (!text) return text;
  if (containsSecret(text)) {
    return '[REDACTED — contains secret pattern]';
  }
  return text;
}

/**
 * Build a RunCompletionEvent from objective signals available at the
 * agent-worker completion point.
 *
 * All fields are bounded and scrubbed. This function never throws —
 * any error in building individual fields is swallowed and the field
 * is omitted or set to a safe default.
 */
export function buildRunCompletionEvent(input: BuildRunCompletionEventInput): RunCompletionEvent {
  // Validate persona (default to 'developer' if unknown)
  const validPersonas: AgentPersona[] = ['developer', 'operations', 'architect', 'reviewer', 'product'];
  const persona = validPersonas.includes(input.persona as AgentPersona)
    ? (input.persona as AgentPersona)
    : 'developer';

  // Derive outcome from objective signals
  const outcome = deriveOutcome({
    agentSucceeded: input.agentSucceeded,
    beadsStatus: input.beadsStatus,
  });

  // Cap and scrub files_touched
  const filesTouched = (input.filesTouched || [])
    .slice(0, MAX_FILES_TOUCHED)
    .map(f => scrubField(f))
    .filter(f => f && !f.startsWith('[REDACTED'));

  // Cap and scrub errors
  const errorsEncountered = (input.errorsEncountered || [])
    .slice(0, MAX_ERRORS)
    .map(e => ({
      summary: scrubField(e.summary.slice(0, MAX_ERROR_SUMMARY_LENGTH)),
      resolved: e.resolved,
    }))
    .filter(e => !e.summary.startsWith('[REDACTED'));

  // Extract and cap agent narrative (from ### Learnings prose)
  let agentNarrative: string | undefined;
  if (input.agentOutput) {
    const learnings = extractLearnings(input.agentOutput);
    if (learnings.length > 0) {
      const joined = learnings.join('\n- ');
      const prefixed = `- ${joined}`;
      const capped = prefixed.slice(0, MAX_NARRATIVE_LENGTH);
      // Only include if the full narrative doesn't contain secrets
      if (!containsSecret(capped)) {
        agentNarrative = capped;
      }
    }
  }

  // Build the event
  const event: RunCompletionEvent = {
    schema_version: '1',
    run_id: (input.runId || `agent-${persona}-issue-${input.issueNumber}-${Date.now()}`).slice(0, MAX_RUN_ID_LENGTH),
    timestamp: new Date().toISOString(),
    persona,
    issue: {
      number: input.issueNumber,
      title: input.issueTitle,
      repo: input.repo,
      ...(input.component ? { component: input.component } : {}),
    },
    outcome,
    turns: Math.max(0, input.turns),
    duration_ms: Math.max(0, input.durationMs),
    files_touched: filesTouched,
    skills_used: input.skillsUsed || [],
    errors_encountered: errorsEncountered,
  };

  // Optional fields
  if (input.ownerSub) event.owner_sub = input.ownerSub;
  if (input.tenantId) event.tenant_id = input.tenantId;
  if (input.pr) event.pr = input.pr;
  if (input.beadsTaskId) {
    event.beads = { task_id: input.beadsTaskId, status: input.beadsStatus || 'unknown' };
  }
  if (input.tests) event.tests = input.tests;
  if (agentNarrative) event.agent_narrative = agentNarrative;

  return event;
}

/**
 * Collect files touched via git diff.
 * Returns repo-relative paths, capped at MAX_FILES_TOUCHED.
 * Never throws — returns empty array on failure.
 */
export async function getFilesTouched(cwd: string): Promise<string[]> {
  try {
    const { execSync } = await import('child_process');

    // Committed changes on this branch vs origin/main + uncommitted
    const committed = execSync(
      'git diff --name-only origin/main..HEAD 2>/dev/null || echo ""',
      { cwd, encoding: 'utf-8', timeout: 10_000 },
    ).trim();
    const uncommitted = execSync(
      'git diff --name-only HEAD 2>/dev/null || echo ""',
      { cwd, encoding: 'utf-8', timeout: 10_000 },
    ).trim();

    const all = [...committed.split('\n'), ...uncommitted.split('\n')]
      .filter(f => f.length > 0);
    return [...new Set(all)].slice(0, MAX_FILES_TOUCHED);
  } catch {
    return [];
  }
}

/**
 * Emit the run-completion event.
 *
 * Phase 1: structured log to CloudWatch (observable via Insights).
 * Future phases: POST to sink endpoint or SQS publish.
 *
 * NEVER throws — all errors are swallowed and logged as warnings.
 */
export async function emitRunCompletionEvent(
  event: RunCompletionEvent,
  log: (level: string, msg: string, ctx?: Record<string, unknown>) => void,
): Promise<void> {
  try {
    // Phase 1: structured log (queryable via CloudWatch Insights)
    log('INFO', '[run-completion-event] Event emitted', {
      run_completion_event: event,
    });
  } catch (err) {
    // Non-blocking: log failure must never escape
    try {
      log('WARN', `[run-completion-event] Emission failed: ${(err as Error).message}`);
    } catch {
      // Last resort: even logging failed — silently swallow
    }
  }
}
