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
  const { cwd, log, postComment } = deps;

  // Find the pending gate stage from aidlc state
  const pending = findPendingGate(cwd, deps.issueNumber);
  const pendingStage = pending?.stage;
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
  const evidence = pending ? await gateEvidence(pending.statePath, deps) : undefined;
  const fallbackBody = buildFallbackGateComment(pendingStage, evidence);

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
  return findPendingGate(cwd)?.stage ?? null;
}

function findPendingGate(cwd: string, issueNumber?: string): { stage: string; statePath: string } | null {
  // Search for aidlc-state.md in known locations:
  // - aidlc/spaces/**/aidlc-state.md (new multi-space layout)
  // - aidlc-docs/aidlc-state.md (legacy layout)
  const candidates = [
    ...globSync('aidlc/spaces/**/aidlc-state.md', cwd),
    ...globSync('aidlc-docs/aidlc-state.md', cwd),
  ];

  // A fallback must describe this issue, not another intent in the checkout.
  const scoped = candidates.filter(p => !issueNumber || !/spaces\/issue-\d+\//.test(p)
    || p.startsWith(`aidlc/spaces/issue-${issueNumber}/`));
  scoped.sort((a, b) => Number(b.startsWith(`aidlc/spaces/issue-${issueNumber}/`))
    - Number(a.startsWith(`aidlc/spaces/issue-${issueNumber}/`)));
  for (const relPath of scoped) {
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
      return { stage: normalizeStageId(rawStage), statePath: relPath };
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
export const GATE_APPROVAL_EFFECTS: Record<string, string> = {
  'intent-capture': 'Approve the problem and scope so the next planned inception stage can begin.',
  'reverse-engineering': 'Approve the current-system assessment so requirements can be prepared.',
  'requirements-analysis': 'Approve the requirements so delivery planning can begin.',
  'delivery-planning': 'Approve the delivery plan so story issues and execution drafts can be prepared; execution still requires the loop-proposal gate.',
  'loop-proposal': 'Approve the reviewed execution scope and target environment, allowing delivery-loop issues to be created and construction/deployment to start, subject to existing checks.',
};

/** Link only to a clean, tracked revision confirmed present on GitHub. */
async function gateEvidence(statePath: string, deps: EnforcerDeps): Promise<string | undefined> {
  try {
    if (execGitSync('git status --porcelain aidlc/ aidlc-docs/', deps.cwd)) return undefined;
    const sha = execGitSync('git rev-parse HEAD', deps.cwd);
    if (!/^[a-f0-9]{40}$/.test(sha)) return undefined;
    const tracked = execGitSync('git ls-tree -r --name-only HEAD -- aidlc/ aidlc-docs/', deps.cwd).split('\n');
    if (!tracked.includes(statePath)) return undefined;
    const repo = `${encodeURIComponent(deps.repoOwner)}/${encodeURIComponent(deps.repoName)}`;
    const remoteSha = await deps.execCommand(`gh api repos/${repo}/commits/${sha} --jq .sha`);
    if (remoteSha.trim() !== sha) return undefined;
    const directory = path.posix.dirname(statePath).split('/').map(encodeURIComponent).join('/');
    return `[Review artifacts at revision ${sha.slice(0, 7)}](https://github.com/${repo}/tree/${sha}/${directory}).`;
  } catch (err) {
    deps.log('WARN', `[aidlc-gate-enforcer] Could not verify artifact publication: ${(err as Error).message}`);
    return undefined;
  }
}

export function buildFallbackGateComment(stage: string, evidence?: string): string {
  const effect = GATE_APPROVAL_EFFECTS[stage];
  const title = stage.replace(/-/g, ' ');
  return `<!-- aidlc-gate:${stage} -->
## Review needed: ${title}

The ${title} stage is waiting for your decision. The run ended without a gate brief, so the AIDLC gate enforcer posted this reminder automatically.

${evidence || 'Artifact publication has not been verified. Request a reviewable artifact link and revision before approving.'}
Review the proposed outcome, unresolved conditions and changes since your last approval. This fallback cannot assess whether the artifacts are complete.

${stage === 'loop-proposal' ? 'Confirm the execution scope and target environment in the proposal before approving. Skipping this final gate does not authorize construction.\n\n' : ''}### Your next action

${effect ? `- \`@agent-aidlc approve\` — ${effect}` : 'The approval effect for this stage is not known to the fallback reporter; request clarification in feedback.'}
- \`@agent-aidlc feedback: [your notes]\` — request revisions or missing evidence.
${effect && stage !== 'loop-proposal' ? '- `@agent-aidlc skip` — skip this stage; later approval gates still apply.\n' : ''}
Only mention-prefixed reply comments trigger the next run. Emoji reactions, checkbox ticks and bare replies do not advance the workflow.
`;
}

/**
 * Synchronous git command execution (used for the commit path which must be
 * synchronous to avoid race conditions with process exit).
 */
function execGitSync(command: string, cwd: string): string {
  return execSync(command, {
    cwd,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
    timeout: 30_000,
  }).trim();
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
