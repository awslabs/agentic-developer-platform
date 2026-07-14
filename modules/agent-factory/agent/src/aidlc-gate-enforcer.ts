/**
 * AIDLC Gate Enforcer — deterministic enforcement of the gate protocol.
 *
 * Ensures that an AIDLC-flagged run cannot exit with:
 * 1. Uncommitted aidlc/ state (silent workflow loss)
 * 2. An unposted pending gate comment (unaudited advance)
 *
 * This module is called from agent-worker.ts at finalize time, gated on
 * AIDLC_ENABLED. Non-AIDLC runs never invoke this code.
 *
 * Issue #3231, EPIC #3158 — hardening wave.
 */

import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface EnforcerDeps {
  cwd: string;
  issueNumber: string;
  repoOwner: string;
  repoName: string;
  log: (level: string, msg: string, meta?: Record<string, unknown>) => void;
  execCommand: (command: string, useAppToken?: boolean) => Promise<string>;
  postComment: (body: string) => Promise<void>;
}

export interface EnforcerResult {
  committed: boolean;
  gateCommentPosted: boolean;
  stage: string | null;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Run the AIDLC gate enforcement protocol. Safe to call on any run —
 * returns early with no-ops if AIDLC is not detected or there's nothing to enforce.
 */
export async function enforceAidlcGate(deps: EnforcerDeps): Promise<EnforcerResult> {
  const result: EnforcerResult = { committed: false, gateCommentPosted: false, stage: null };

  // Step 1: commit dirty aidlc/ state
  result.committed = await commitDirtyAidlcState(deps);

  // Step 2: ensure gate comment exists for any pending stage
  const gateResult = await ensureGateComment(deps);
  result.gateCommentPosted = gateResult.posted;
  result.stage = gateResult.stage;

  return result;
}

// ---------------------------------------------------------------------------
// Step 1: Commit dirty aidlc/ state
// ---------------------------------------------------------------------------

/**
 * If `git status --porcelain aidlc/` shows uncommitted changes, commit and push them.
 * Returns true if a commit was made, false otherwise.
 */
export async function commitDirtyAidlcState(deps: EnforcerDeps): Promise<boolean> {
  const { cwd, log } = deps;

  const status = execGitSync('git status --porcelain aidlc/', cwd);
  if (!status) {
    log('INFO', '[aidlc-gate-enforcer] aidlc/ state is clean — no enforcement commit needed');
    return false;
  }

  log('INFO', '[aidlc-gate-enforcer] Dirty aidlc/ state detected — committing (enforced)', {
    dirtyFiles: status.split('\n').length,
  });

  const ts = new Date().toISOString();
  const message = `aidlc: checkpoint ${ts} (enforced)`;

  try {
    execGitSync('git add aidlc/', cwd);
    execGitSync(`git commit -m "${message}"`, cwd);
    execGitSync('git push', cwd);
    log('INFO', '[aidlc-gate-enforcer] Enforced commit pushed successfully');
    return true;
  } catch (err) {
    log('WARN', `[aidlc-gate-enforcer] Enforced commit/push failed: ${(err as Error).message}`);
    // Best-effort: don't throw — the run should still complete
    return false;
  }
}

// ---------------------------------------------------------------------------
// Step 2: Ensure gate comment exists for pending stage
// ---------------------------------------------------------------------------

/**
 * Parse aidlc state files for a pending gate stage. If found and no gate marker
 * comment exists on the issue, post a fallback gate comment.
 */
export async function ensureGateComment(deps: EnforcerDeps): Promise<{ posted: boolean; stage: string | null }> {
  const { cwd, issueNumber, log, execCommand, postComment } = deps;

  // Find the pending gate stage from aidlc state
  const pendingStage = findPendingGateStage(cwd);
  if (!pendingStage) {
    log('INFO', '[aidlc-gate-enforcer] No pending gate stage found — no fallback comment needed');
    return { posted: false, stage: null };
  }

  log('INFO', `[aidlc-gate-enforcer] Pending gate detected: stage="${pendingStage}"`, { stage: pendingStage });

  // Check if marker comment already exists on the issue
  const markerExists = await checkGateMarkerExists(pendingStage, deps);
  if (markerExists) {
    log('INFO', `[aidlc-gate-enforcer] Gate marker already exists for stage="${pendingStage}" — no duplicate needed`);
    return { posted: false, stage: pendingStage };
  }

  // Post fallback gate comment
  log('INFO', `[aidlc-gate-enforcer] Posting fallback gate comment for stage="${pendingStage}"`);
  const fallbackBody = buildFallbackGateComment(pendingStage);

  try {
    await postComment(fallbackBody);
    log('INFO', '[aidlc-gate-enforcer] Fallback gate comment posted successfully');
    return { posted: true, stage: pendingStage };
  } catch (err) {
    log('WARN', `[aidlc-gate-enforcer] Failed to post fallback gate comment: ${(err as Error).message}`);
    return { posted: false, stage: pendingStage };
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Find a pending gate stage by parsing aidlc state files.
 * Looks for `**Waiting For**: Human input` pattern in aidlc-state.md files.
 */
export function findPendingGateStage(cwd: string): string | null {
  // Search for aidlc-state.md in known locations:
  // - aidlc/spaces/**/aidlc-state.md (new multi-space layout)
  // - aidlc-docs/aidlc-state.md (legacy layout)
  const candidates = [
    ...globSync('aidlc/spaces/**/aidlc-state.md', cwd),
    ...globSync('aidlc-docs/aidlc-state.md', cwd),
  ];

  for (const relPath of candidates) {
    const fullPath = path.join(cwd, relPath);
    if (!fs.existsSync(fullPath)) continue;

    const content = fs.readFileSync(fullPath, 'utf-8');

    // Check if waiting for human input (gate pending)
    const waitingMatch = content.match(/\*\*Waiting For\*\*:\s*Human input/i);
    if (!waitingMatch) continue;

    // Extract the current stage name
    const stageMatch = content.match(/\*\*Stage\*\*:\s*(.+)/);
    if (stageMatch) {
      // Normalize stage name to kebab-case for the marker
      const rawStage = stageMatch[1].trim();
      return normalizeStageId(rawStage);
    }
  }

  return null;
}

/**
 * Normalize a stage name to kebab-case (e.g. "Requirements Analysis" → "requirements-analysis").
 */
export function normalizeStageId(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * Check if a gate marker comment already exists on the issue.
 * Searches for `<!-- aidlc-gate:<stage> -->` in issue comments.
 */
async function checkGateMarkerExists(stage: string, deps: EnforcerDeps): Promise<boolean> {
  const { issueNumber, execCommand, log } = deps;
  const marker = `<!-- aidlc-gate:${stage} -->`;

  try {
    const commentsJson = await execCommand(
      `gh issue view ${issueNumber} --json comments --jq '.comments[].body'`,
    );
    return commentsJson.includes(marker);
  } catch (err) {
    log('WARN', `[aidlc-gate-enforcer] Failed to check gate marker (assuming absent): ${(err as Error).message}`);
    return false;
  }
}

/**
 * Build the fallback gate comment body.
 * Uses the same structure the persona would post, but clearly marked as enforcer-generated.
 */
function buildFallbackGateComment(stage: string): string {
  return `<!-- aidlc-gate:${stage} -->
## \u{1f6d1} Gate: ${stage}

**This gate comment was posted automatically by the AIDLC gate enforcer** because the agent run completed without posting a gate comment for the pending stage.

### Status
- Stage \`${stage}\` artifacts have been committed to the branch
- Awaiting human approval before the next stage can begin

### Reply Options
- **approve** — advance to the next stage
- **feedback: [your notes]** — request revisions to this stage's output
- **skip** — skip this stage and advance

> ⚠️ Emoji reactions and checkbox ticks do NOT trigger advancement — only reply comments are read.
`;
}

/**
 * Synchronous git command execution (used for the commit path which must be
 * synchronous to avoid race conditions with process exit).
 */
function execGitSync(command: string, cwd: string): string {
  try {
    return execSync(command, {
      cwd,
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: 30_000,
    }).trim();
  } catch (err) {
    const error = err as { stdout?: string; stderr?: string; message: string };
    // git status returns empty string when clean (exit 0)
    if (error.stdout !== undefined) return error.stdout.trim();
    throw err;
  }
}

/**
 * Glob for files matching a pattern relative to cwd.
 * Returns relative paths.
 */
function globSync(pattern: string, cwd: string): string[] {
  try {
    // Use find-based approach for portability
    const fullPattern = path.join(cwd, pattern);
    const dir = path.dirname(fullPattern);
    const basename = path.basename(fullPattern);

    if (!fs.existsSync(dir.split('*')[0].replace(/\/$/, ''))) {
      return [];
    }

    // Use simple recursive search for the aidlc-state.md files
    const results: string[] = [];
    findFilesRecursive(cwd, pattern, results);
    return results;
  } catch {
    return [];
  }
}

/**
 * Simple recursive file finder matching a glob-like pattern.
 * Supports ** for recursive directory matching.
 */
function findFilesRecursive(basePath: string, pattern: string, results: string[]): void {
  const parts = pattern.split('/');
  findRecursiveImpl(basePath, parts, '', results);
}

function findRecursiveImpl(basePath: string, parts: string[], currentRel: string, results: string[]): void {
  if (parts.length === 0) return;

  const [head, ...rest] = parts;
  const currentAbs = path.join(basePath, currentRel);

  if (!fs.existsSync(currentAbs) || !fs.statSync(currentAbs).isDirectory()) return;

  if (head === '**') {
    // Match zero or more directories
    // Try matching the rest at this level (zero directories)
    findRecursiveImpl(basePath, rest, currentRel, results);
    // Try matching in subdirectories
    const entries = fs.readdirSync(currentAbs, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.isDirectory()) {
        const subRel = currentRel ? `${currentRel}/${entry.name}` : entry.name;
        // Keep ** active for deeper directories
        findRecursiveImpl(basePath, parts, subRel, results);
      }
    }
  } else if (rest.length === 0) {
    // Last part — match files
    const entries = fs.readdirSync(currentAbs, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.name === head || matchWildcard(entry.name, head)) {
        const relPath = currentRel ? `${currentRel}/${entry.name}` : entry.name;
        results.push(relPath);
      }
    }
  } else {
    // Intermediate directory part
    const entries = fs.readdirSync(currentAbs, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.isDirectory() && (entry.name === head || matchWildcard(entry.name, head))) {
        const subRel = currentRel ? `${currentRel}/${entry.name}` : entry.name;
        findRecursiveImpl(basePath, rest, subRel, results);
      }
    }
  }
}

function matchWildcard(name: string, pattern: string): boolean {
  if (pattern === '*') return true;
  // Simple wildcard: convert to regex
  const regex = new RegExp('^' + pattern.replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
  return regex.test(name);
}
