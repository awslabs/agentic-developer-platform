/**
 * AIDLC Presence — Synthetic HUMAN_TURN event on gate resume.
 *
 * When an AIDLC-enabled headless pod resumes after a gate approval, no native
 * HUMAN_TURN event is recorded (there's no interactive human session). This
 * module writes a synthetic HUMAN_TURN audit event to the intent's audit shard,
 * derived from the triggering comment's metadata.
 *
 * This satisfies mint-presence.ts's anti-fabrication check while maintaining
 * the security invariant: a real human (or authorized agent) DID act — the
 * gate-answering comment IS the proof.
 *
 * Issue #3232, EPIC #3158 — hardening wave (Decision 3).
 */

import * as fs from 'fs';
import * as path from 'path';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PresenceDeps {
  cwd: string;
  log: (level: string, msg: string, meta?: Record<string, unknown>) => void;
}

export interface TriggerComment {
  author: string;
  createdAt: string;
  url: string;
}

export interface PresenceResult {
  written: boolean;
  reason: string;
  stage?: string;
}

// ---------------------------------------------------------------------------
// Known-good AIDLC versions
// ---------------------------------------------------------------------------

/**
 * Versions of AIDLC whose audit format and mint-presence.ts contract we have
 * validated. If the workspace pins a version NOT in this list, we skip the
 * write with a loud warning — better to fall through to the prompt-driven path
 * than to write a malformed event that bricks the state machine.
 */
export const KNOWN_AIDLC_VERSIONS: string[] = [
  'v2.2.0',
  'v2.2.1',
  'v2.2.2',
  'v2.2.3',
  'v2.3.0',
  'v2.3.1',
];

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Mint a synthetic HUMAN_TURN audit event on gate resume.
 *
 * Safe to call on every run — returns early with no-ops when:
 * - AIDLC is not detected (no `aidlc/` directory)
 * - No pending gate stage
 * - Pinned version is not in the known-good list
 * - No triggering comment metadata available
 */
export function mintSyntheticPresence(
  deps: PresenceDeps,
  triggerComment: TriggerComment | null,
): PresenceResult {
  const { cwd, log } = deps;

  // Guard 1: AIDLC directory must exist
  const aidlcDir = path.join(cwd, 'aidlc');
  if (!fs.existsSync(aidlcDir)) {
    return { written: false, reason: 'aidlc_not_detected' };
  }

  // Guard 2: Version gate — refuse unknown versions
  const versionResult = checkVersion(aidlcDir, log);
  if (!versionResult.ok) {
    return { written: false, reason: versionResult.reason };
  }

  // Guard 3: Must have a pending gate stage (indicates this is a resume)
  const pendingStage = findPendingGateStage(cwd);
  if (!pendingStage) {
    log('INFO', '[aidlc-presence] No pending gate stage — not a gate resume, skipping');
    return { written: false, reason: 'no_pending_gate' };
  }

  // Guard 4: Must have trigger comment metadata
  if (!triggerComment || !triggerComment.author) {
    log('WARN', '[aidlc-presence] No trigger comment metadata — cannot write HUMAN_TURN');
    return { written: false, reason: 'no_trigger_comment', stage: pendingStage };
  }

  // Write the synthetic HUMAN_TURN event to the audit shard
  const written = writeHumanTurnEvent(cwd, pendingStage, triggerComment, log);
  if (written) {
    log('INFO', '[aidlc-presence] Synthetic HUMAN_TURN written to audit shard', {
      author: triggerComment.author,
      stage: pendingStage,
    });
    return { written: true, reason: 'success', stage: pendingStage };
  }

  return { written: false, reason: 'write_failed', stage: pendingStage };
}

// ---------------------------------------------------------------------------
// Version Gate
// ---------------------------------------------------------------------------

interface VersionCheck {
  ok: boolean;
  reason: string;
  version?: string;
}

/**
 * Read aidlc/.aidlc-version and check against known-good list.
 */
export function checkVersion(
  aidlcDir: string,
  log: PresenceDeps['log'],
): VersionCheck {
  const versionFile = path.join(aidlcDir, '.aidlc-version');

  if (!fs.existsSync(versionFile)) {
    log('WARN', '[aidlc-presence] No .aidlc-version file found — skipping HUMAN_TURN write');
    return { ok: false, reason: 'no_version_file' };
  }

  const version = fs.readFileSync(versionFile, 'utf-8').trim();
  if (!version) {
    log('WARN', '[aidlc-presence] .aidlc-version file is empty — skipping HUMAN_TURN write');
    return { ok: false, reason: 'empty_version' };
  }

  if (!KNOWN_AIDLC_VERSIONS.includes(version)) {
    log('WARN', `[aidlc-presence] Unknown AIDLC version "${version}" — refusing to write HUMAN_TURN. ` +
      `Known versions: ${KNOWN_AIDLC_VERSIONS.join(', ')}. ` +
      'Prompt-driven flow still works; update KNOWN_AIDLC_VERSIONS after validating the new version.');
    return { ok: false, reason: 'unknown_version', version };
  }

  return { ok: true, reason: 'version_ok', version };
}

// ---------------------------------------------------------------------------
// Pending Gate Detection
// ---------------------------------------------------------------------------

/**
 * Find a pending gate stage by parsing aidlc state files.
 * Mirrors the logic in aidlc-gate-enforcer.ts.
 */
export function findPendingGateStage(cwd: string): string | null {
  const candidates = [
    ...findStateFiles(cwd, 'aidlc/spaces'),
    ...findStateFiles(cwd, 'aidlc-docs'),
  ];

  for (const filePath of candidates) {
    if (!fs.existsSync(filePath)) continue;

    const content = fs.readFileSync(filePath, 'utf-8');
    const waitingMatch = content.match(/\*\*Waiting For\*\*:\s*Human input/i);
    if (!waitingMatch) continue;

    const stageMatch = content.match(/\*\*Stage\*\*:\s*(.+)/);
    if (stageMatch) {
      return normalizeStageId(stageMatch[1].trim());
    }
  }

  return null;
}

/**
 * Normalize a stage name to kebab-case.
 */
function normalizeStageId(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * Find aidlc-state.md files under a given base directory.
 */
function findStateFiles(cwd: string, subdir: string): string[] {
  const baseDir = path.join(cwd, subdir);
  if (!fs.existsSync(baseDir)) return [];

  const results: string[] = [];
  walkForFile(baseDir, 'aidlc-state.md', results);
  return results;
}

function walkForFile(dir: string, targetName: string, results: string[]): void {
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walkForFile(fullPath, targetName, results);
    } else if (entry.name === targetName) {
      results.push(fullPath);
    }
  }
}

// ---------------------------------------------------------------------------
// Audit Shard Write
// ---------------------------------------------------------------------------

/**
 * Write the synthetic HUMAN_TURN event to the audit shard.
 *
 * The audit shard is the `audit.md` file in the same directory as the
 * aidlc-state.md that holds the pending gate. If not found, writes to
 * `aidlc-docs/audit.md` (legacy path) or creates it.
 */
function writeHumanTurnEvent(
  cwd: string,
  stage: string,
  comment: TriggerComment,
  log: PresenceDeps['log'],
): boolean {
  const auditPath = findAuditShardPath(cwd);
  const eventContent = buildHumanTurnEvent(stage, comment);

  try {
    // Ensure directory exists
    const dir = path.dirname(auditPath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }

    // Append to audit shard (or create if missing)
    if (fs.existsSync(auditPath)) {
      const existing = fs.readFileSync(auditPath, 'utf-8');
      fs.writeFileSync(auditPath, existing + '\n---\n\n' + eventContent, 'utf-8');
    } else {
      fs.writeFileSync(auditPath, '# AIDLC Audit Log\n\n---\n\n' + eventContent, 'utf-8');
    }

    return true;
  } catch (err) {
    log('WARN', `[aidlc-presence] Failed to write audit event: ${(err as Error).message}`);
    return false;
  }
}

/**
 * Find the correct audit shard path.
 * Prefers the same directory as the pending-gate state file.
 */
function findAuditShardPath(cwd: string): string {
  // Check spaces layout first (new)
  const spacesDir = path.join(cwd, 'aidlc', 'spaces');
  if (fs.existsSync(spacesDir)) {
    const auditFiles: string[] = [];
    walkForFile(spacesDir, 'audit.md', auditFiles);
    if (auditFiles.length > 0) return auditFiles[0];

    // No existing audit.md in spaces — find the space that has aidlc-state.md
    const stateFiles: string[] = [];
    walkForFile(spacesDir, 'aidlc-state.md', stateFiles);
    if (stateFiles.length > 0) {
      return path.join(path.dirname(stateFiles[0]), 'audit.md');
    }

    // Fallback to default space
    return path.join(spacesDir, 'default', 'audit.md');
  }

  // Legacy layout
  return path.join(cwd, 'aidlc-docs', 'audit.md');
}

/**
 * Build the markdown content for a HUMAN_TURN audit event.
 * Shape matches the fixture in __fixtures__/human-turn-event.md.
 */
export function buildHumanTurnEvent(stage: string, comment: TriggerComment): string {
  const timestamp = new Date().toISOString();
  return `### HUMAN_TURN — Gate Approval
**Timestamp**: ${timestamp}
**Author**: @${comment.author}
**Type**: HUMAN_TURN
**Source**: synthetic (ADP gate resume)
**Evidence**:
- Comment URL: ${comment.url}
- Comment created: ${comment.createdAt}
- Gate stage: ${stage}
**Action**: Gate decision recorded — human presence verified for headless AIDLC run
`;
}

// ---------------------------------------------------------------------------
// Comment Extraction Helper
// ---------------------------------------------------------------------------

/**
 * Extract the gate-answering comment from a list of issue comments.
 *
 * The gate answer is the most recent comment after the gate marker comment
 * (identified by `<!-- aidlc-gate:<stage> -->`) that is NOT from a bot.
 */
export function extractGateAnswerComment(
  comments: Array<{ author: string; body: string; createdAt: string }>,
  stage: string,
  repoOwner: string,
  repoName: string,
  issueNumber: string,
): TriggerComment | null {
  // Find the gate marker comment index
  const marker = `<!-- aidlc-gate:${stage} -->`;
  let markerIdx = -1;

  for (let i = comments.length - 1; i >= 0; i--) {
    if (comments[i].body.includes(marker)) {
      markerIdx = i;
      break;
    }
  }

  if (markerIdx === -1) {
    // No gate marker found — use the most recent non-bot comment as fallback
    for (let i = comments.length - 1; i >= 0; i--) {
      const c = comments[i];
      if (!isBotComment(c.author)) {
        return {
          author: c.author,
          createdAt: c.createdAt,
          url: buildCommentUrl(repoOwner, repoName, issueNumber, i),
        };
      }
    }
    return null;
  }

  // Find the first non-bot comment AFTER the gate marker
  for (let i = markerIdx + 1; i < comments.length; i++) {
    const c = comments[i];
    if (!isBotComment(c.author)) {
      return {
        author: c.author,
        createdAt: c.createdAt,
        url: buildCommentUrl(repoOwner, repoName, issueNumber, i),
      };
    }
  }

  return null;
}

function isBotComment(author: string): boolean {
  if (!author) return true;
  if (author.endsWith('[bot]')) return true;
  if (author.startsWith('aws-e-adp-agent-')) return true;
  return false;
}

function buildCommentUrl(
  owner: string,
  repo: string,
  issue: string,
  _commentIndex: number,
): string {
  // We don't have the actual comment ID, so construct a best-effort URL
  return `https://github.com/${owner}/${repo}/issues/${issue}`;
}
