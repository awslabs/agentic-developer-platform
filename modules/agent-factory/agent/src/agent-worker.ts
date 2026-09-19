import { workerAwsCredentials, workerAwsRegion, workerAwsEnvironment } from './lib/runIdentity';
/**
 * Generic Agent Worker
 *
 * A rule-driven agent worker that loads rules from .adp-rules/ based on AGENT_TYPE
 * and executes tasks using Claude Agent SDK.
 *
 * Supported AGENT_TYPE values:
 * - product: Requirements, user stories, acceptance criteria
 * - architect: Design, architecture, units generation
 * - developer: Code implementation, tests
 * - reviewer: Code review, integration testing
 * - operations: Infrastructure, deployment, monitoring
 */

import { loadHumanCommunication } from './human-communication';
import { assistantText } from './reporting-text';
import { resilientQuery } from './utils/resilientQuery';
import { wrapUntrusted } from './utils/trust-boundary';
import { resolveInstallationId as sharedResolveInstallationId } from './utils/installation';
import { TmpSpillStore } from './utils/spill';
import { createWorkerToolHooks, developerCheckpointGuidance } from './developer-checkpoints';
import { initTokenManager, canInitTokenManager, getToken, getTokenStatus, writeTokenFile, forceRefresh, adoptBootstrapToken, getRuntimeGitHubToken } from './token-refresh';
import { AuthWatchdog } from './lib/authWatchdog';
import { isBrokerEnabled } from './lib/githubTokenBroker';
import { resolveFallbackBucket, buildFallbackKey } from './utils/s3Fallback';
import { CloudWatchLogsClient, PutLogEventsCommand, CreateLogStreamCommand } from '@aws-sdk/client-cloudwatch-logs';
import { resolveAgentLogGroup } from './lib/logGroup';
import * as fs from 'fs';
import * as path from 'path';

// Memory module - persistent context across agent runs
import {
  configureMemory,
  ensureAdpBranch,
  readComponentContext,
  readAgentContext,
  writeComponentRecord,
  writeAgentRecord,
  detectComponent,
  buildComponentRecord,
  buildAgentRecord,
  formatContextForPrompt,
} from './memory';

// Live status comment — edit-in-place progress on GitHub issues
import { LiveStatusComment, createWorkerStages } from './github-comments';

// Module-scope reference so the SDK loop in runAgent() can append per-turn
// activity (tool calls) without needing the comment passed through every layer.
let activeLiveComment: LiveStatusComment | null = null;

/**
 * Issue #3961: this run's live-control runtime, or null when it has none.
 *
 * Published module-scope for the same reason as the live comment above: it is
 * built in main() alongside the control listener it serves and consumed in
 * runAgent()'s query options and heartbeat, with several layers in between that
 * have no business knowing about live control.
 *
 * `null` is the ordinary case — no control listener, no barrier — and every read
 * site treats it as "this run cannot pause". That is a correct answer rather than
 * a degraded one, and it is what keeps a run without an operator watching on
 * byte-identical code paths to the ones it took before this story.
 */
let activeControlRuntime: {
  adapter: ClaudeControlAdapter;
  gate: PauseGate;
} | null = null;

// Correlation propagation — Phase 2-d (EPIC #779)
import { prependCorrelationMarker } from './lib/correlationMarker';
import { writePointer } from './lib/correlationStore';
import { postProvenance } from './lib/provenanceClient';

// Check Run Streamer — per-turn live streaming to GitHub Check Run output
import { CheckRunStreamer, computeCodexCostUsd } from './components/checkRunStreamer';

// Codex Event Watcher — stream Codex delegation sub-steps to the live page
// while a codex-bridge delegation is in flight (issue #2884, EPIC #2702).
import { CodexEventWatcher } from './components/codexEventWatcher';
// Issue #3960: live-control foundations. Both modules are transport/SDK-isolated
// so the control surface is unit-testable without starting a run.
import { ControlListener } from './control-listener';
import { revalidateQueuedCommand } from './control-revalidation';
import { parseVerificationKeys } from './control-envelope';
import { ControlStateStore, type ControlAction } from './control-state';
// Issue #3962: the harness-neutral control contract and its first adapter. The
// worker composes them; it does not reach past the interface into the SDK.
import { listenerActionsFor } from './control-runtime';
import {
  ClaudeControlAdapter,
  ClaudeBackgroundWorkObserver,
} from './harnesses/claude-control';
import { PauseGate } from './pause-gate';
// Issue #3961: the outcome→journal mapping and the gate/store mirror live in their
// own module so they can be unit-tested; importing this file from a test pulls the
// SDK's ESM entry point into Jest and the suite cannot parse.
import { applyControlCommand, bindGateTransitionsToStore } from './control-command-apply';

// Knowledge Layer MCP — Issue #1592: register Door as agent MCP tools (feature-flagged)
import {
  KNOWLEDGE_LAYER_ENABLED,
  KNOWLEDGE_LAYER_TOOLS,
  KNOWLEDGE_LAYER_SERVER_NAME,
  KNOWLEDGE_LAYER_PROMPT,
  getKnowledgeLayerMcpConfig,
} from './knowledge-layer-config';

// Mediated GitHub operations — Issue #5223: this run has no GitHub token, so the
// agent must be told the helper that replaces `git push`/`gh pr create`.
import { MEDIATED_GITHUB_ENABLED, MEDIATED_GITHUB_PROMPT, isMediatedRun } from './mediated-github-config';

// Beads module - distributed state management for agents
import {
  configureBeads,
  isBeadsEnabled,
  isBeadsAvailable,
  isBeadsInitialized,
  syncPull,
  syncPush,
  startWork as beadsStartWork,
  completeWork as beadsCompleteWork,
  reportFailure as beadsReportFailure,
  setLogger as setBeadsLogger,
} from './beads';

// Experience-save post-task hook — Issue #1294
// Extracts learnings from agent output and persists to personal-context store.
import { saveExperienceLearnings } from './experience-save-hook';
import { buildPersonalContextIdentity, getPersonalContextHeaders } from './complex-task-chat/personal-context-headers';

// AIDLC Gate Enforcer — deterministic enforcement of commit + gate comment protocol
// (Issue #3231, EPIC #3158 hardening wave). Only invoked when AIDLC_ENABLED.
import { enforceAidlcGate } from './aidlc-gate-enforcer';
import { isTruncatedStreamResult } from './stream-truncation';

// AIDLC Presence — synthetic HUMAN_TURN on gate resume (Issue #3232, EPIC #3158).
// Writes a synthetic audit event proving a real human answered the gate, satisfying
// mint-presence.ts's anti-fabrication check in headless mode.
import { mintSyntheticPresence, extractGateAnswerComment, findPendingGateStage as findPresenceGateStage } from './aidlc-presence';

// ============================================================================
// Configuration
// ============================================================================

const REPO_OWNER = process.env.REPO_OWNER || '';
const REPO_NAME = process.env.REPO_NAME || '';
const ISSUE_NUMBER = process.env.ISSUE_NUMBER || '';
const GITHUB_TOKEN = process.env.GITHUB_TOKEN || '';
const GH_APP_TOKEN = process.env.GH_APP_TOKEN || '';
const CWD = process.env.WORK_DIR || process.cwd();
const MODEL = process.env.ANTHROPIC_MODEL || 'global.anthropic.claude-opus-5';
const AGENT_TYPE = process.env.AGENT_TYPE || 'developer';
const AWS_REGION = workerAwsRegion();

// Beads configuration - distributed state management (shared with PM)
const BEADS_ENABLED = process.env.BEADS_ENABLED !== 'false';
// Issue #4184: no `adp-agent-state` default — that bucket is in a foreign AWS
// account and no IAM statement here permits it. Empty degrades to a clean no-op
// (beads syncPull/syncPush guard on it); the real value arrives via
// BEADS_S3_BUCKET, set on the pod template in webhook-ingress scaledjob.tf.
const BEADS_S3_BUCKET = process.env.BEADS_S3_BUCKET || '';
const BEADS_S3_REGION = process.env.BEADS_S3_REGION || AWS_REGION;
const BEADS_S3_PATH = process.env.BEADS_S3_PATH || `beads/${REPO_NAME}`;

// AIDLC detection — enable Task tool only when workspace carries an AIDLC install
// (Issue #3167, EPIC #3158 Decision 2). The `aidlc/` directory is the canonical
// install marker (contains `spaces/default/memory/`).
const AIDLC_ENABLED = fs.existsSync(path.join(CWD, 'aidlc'));

// GitHub App installation tokens live ~60 min. Issue #4369: these two values are
// load-bearing TOGETHER — the interval must be short enough that a tick reliably
// lands inside the threshold window before expiry. A 30-min interval with a
// 15-min threshold has no tick in the window at all, which is how >1h runs ended
// up in an unrecoverable 401 loop. Named here so the pairing is visible and the
// cadence is assertable from a test.
const TOKEN_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const TOKEN_REFRESH_THRESHOLD_MS = 20 * 60 * 1000;

/**
 * Process exit code meaning "this run failed in a way a retry can fix".
 *
 * entrypoint.py acks (deletes) the SQS message on every other terminal exit, so
 * a plain non-zero exit would destroy the task rather than retry it. This code is
 * the opt-out: entrypoint.py leaves the message untouched, its visibility timeout
 * lapses, and SQS redelivers into a pod with a fresh token (bounded by
 * maxReceiveCount → DLQ). Keep in sync with AGENT_EXIT_RETRYABLE there.
 */
const EXIT_RETRYABLE = 75;

// ============================================================================
// CloudWatch Logging
// ============================================================================

const LOG_GROUP = resolveAgentLogGroup();
const LOG_STREAM = `agent-${AGENT_TYPE}-issue-${ISSUE_NUMBER}-${Date.now()}`;
const cwClient = new CloudWatchLogsClient({ region: AWS_REGION, credentials: workerAwsCredentials() });
let cwBuffer: { timestamp: number; message: string }[] = [];
let cwInitialized = false;

async function initCloudWatch(): Promise<void> {
  try {
    await cwClient.send(new CreateLogStreamCommand({
      logGroupName: LOG_GROUP,
      logStreamName: LOG_STREAM,
    }));
    cwInitialized = true;
    log('INFO', `CloudWatch logging initialized for @agent-${AGENT_TYPE}`);
  } catch (err: unknown) {
    if ((err as { name?: string }).name !== 'ResourceAlreadyExistsException') {
      console.warn('CloudWatch init failed:', (err as Error).message);
    } else {
      cwInitialized = true;
    }
  }
}

function log(level: string, message: string, context?: Record<string, unknown>): void {
  const entry = {
    level,
    message,
    issueNumber: ISSUE_NUMBER,
    agentType: AGENT_TYPE,
    ...context,
    timestamp: new Date().toISOString(),
  };
  const line = JSON.stringify(entry);

  const emoji = level === 'ERROR' ? '❌' : level === 'WARN' ? '⚠️' : '→';
  console.log(`${emoji} [${AGENT_TYPE}] ${message}`);

  if (cwInitialized) {
    cwBuffer.push({ timestamp: Date.now(), message: line });
  }
}

async function flushCloudWatch(): Promise<void> {
  if (!cwInitialized || cwBuffer.length === 0) return;
  const events = cwBuffer.splice(0, cwBuffer.length);
  try {
    await cwClient.send(new PutLogEventsCommand({
      logGroupName: LOG_GROUP,
      logStreamName: LOG_STREAM,
      logEvents: events,
    }));
  } catch (err) {
    console.warn('CloudWatch flush failed:', (err as Error).message);
  }
}

const cwFlushTimer = setInterval(flushCloudWatch, 5000);

// ============================================================================
// Detailed Message Logging
// ============================================================================

// Track skill usage for summary
const skillsDiscovered: Set<string> = new Set();
const skillCommandsExecuted: string[] = [];

// Known skill command patterns
const SKILL_COMMAND_PATTERNS: Record<string, RegExp[]> = {
  skypilot: [/^sky\s+(launch|exec|status|stop|down|logs|queue|serve)/i],
  kubernetes: [/^kubectl\s+/i, /^helm\s+/i],
  terraform: [/^terraform\s+(init|plan|apply|destroy)/i],
  docker: [/^docker\s+(build|push|run|compose)/i],
  playwright: [/playwright\s+test/i, /npx\s+playwright/i],
};

function detectSkillFromCommand(cmd: string): string | null {
  for (const [skill, patterns] of Object.entries(SKILL_COMMAND_PATTERNS)) {
    for (const pattern of patterns) {
      if (pattern.test(cmd)) {
        return skill;
      }
    }
  }
  return null;
}

function detectSkillFromPath(filePath: string): string | null {
  const match = filePath.match(/\.claude\/skills\/([^/]+)/);
  return match ? match[1] : null;
}

function logMessage(message: { type: string; message: { content: Array<Record<string, unknown>> } }, turnCount: number): string {
  let text = '';
  const toolsUsed: string[] = [];
  console.log(`\n${'─'.repeat(60)}`);
  console.log(`[${AGENT_TYPE}] Turn ${turnCount}`);
  console.log('─'.repeat(60));

  for (const block of message.message.content) {
    if ('name' in block) {
      const toolName = block.name as string;
      const input = ('input' in block ? block.input : {}) as Record<string, unknown>;
      toolsUsed.push(toolName);

      if (toolName === 'Write') {
        console.log(`📝 Write: ${input.file_path}`);
      } else if (toolName === 'Edit') {
        console.log(`✏️  Edit: ${input.file_path}`);
      } else if (toolName === 'Read') {
        const filePath = input.file_path as string || '';
        const skill = detectSkillFromPath(filePath);
        if (skill) {
          skillsDiscovered.add(skill);
          console.log(`🎯 SKILL DISCOVERY [${skill}]: Reading ${filePath}`);
        } else {
          console.log(`📖 Read: ${filePath}`);
        }
      } else if (toolName === 'Bash') {
        const cmd = (input.command as string || '');
        const cmdPreview = cmd.substring(0, 200);
        const skill = detectSkillFromCommand(cmd);
        if (skill) {
          skillCommandsExecuted.push(`${skill}: ${cmd.substring(0, 100)}`);
          console.log(`\n${'═'.repeat(60)}`);
          console.log(`🎯 SKILL EXECUTION [${skill}]`);
          console.log(`${'═'.repeat(60)}`);
          console.log(`💻 Command: ${cmdPreview}${cmd.length > 200 ? '...' : ''}`);
          console.log(`${'═'.repeat(60)}\n`);
        } else {
          console.log(`💻 Bash: ${cmdPreview}${cmd.length >= 200 ? '...' : ''}`);
        }
      } else if (toolName === 'WebSearch') {
        console.log(`🔍 WebSearch: ${input.query}`);
      } else if (toolName === 'WebFetch') {
        console.log(`🌐 WebFetch: ${input.url}`);
      } else if (toolName === 'Glob') {
        const pattern = input.pattern as string || '';
        const skill = detectSkillFromPath(pattern);
        if (skill || pattern.includes('.claude/skills')) {
          console.log(`🎯 SKILL SEARCH: ${pattern}`);
        } else {
          console.log(`📂 Glob: ${pattern}`);
        }
      } else if (toolName === 'Grep') {
        console.log(`🔎 Grep: ${input.pattern}`);
      } else if (toolName === 'Skill') {
        const skillName = input.skill as string || input.name as string || 'unknown';
        skillsDiscovered.add(skillName);
        console.log(`\n${'═'.repeat(60)}`);
        console.log(`🎯 SKILL INVOKED: ${skillName}`);
        console.log(`${'═'.repeat(60)}\n`);
      } else {
        console.log(`🔧 ${toolName}`);
      }
    }

    if ('text' in block && typeof block.text === 'string' && (block.text as string).trim()) {
      const fullText = block.text as string;
      text += fullText;
      const preview = fullText.substring(0, 500);
      console.log(`💭 ${preview}${fullText.length > 500 ? '...' : ''}`);
    }
  }

  log('INFO', `Turn ${turnCount} completed`, {
    turn: turnCount,
    tools: toolsUsed,
    textLength: text.length,
    skillsDiscovered: Array.from(skillsDiscovered),
    skillCommandsExecuted: skillCommandsExecuted.length,
  });
  return text;
}

function logSkillSummary(): void {
  console.log(`\n${'═'.repeat(60)}`);
  console.log('🎯 SKILL USAGE SUMMARY');
  console.log('═'.repeat(60));

  if (skillsDiscovered.size > 0) {
    console.log(`\nSkills Discovered: ${Array.from(skillsDiscovered).join(', ')}`);
  } else {
    console.log('\nSkills Discovered: None');
  }

  if (skillCommandsExecuted.length > 0) {
    console.log(`\nSkill Commands Executed (${skillCommandsExecuted.length}):`);
    for (const cmd of skillCommandsExecuted) {
      console.log(`  • ${cmd}`);
    }
  } else {
    console.log('\nSkill Commands Executed: None');
    if (AGENT_TYPE === 'operations') {
      console.log('⚠️  WARNING: Operations agent did not execute any skill commands!');
    }
  }

  console.log('═'.repeat(60) + '\n');

  log('INFO', 'Skill usage summary', {
    skillsDiscovered: Array.from(skillsDiscovered),
    skillCommandsExecuted,
    commandCount: skillCommandsExecuted.length,
  });
}

// ============================================================================
// GitHub Helpers
// ============================================================================

async function execCommand(command: string, useAppToken: boolean = false): Promise<string> {
  const { execSync } = await import('child_process');
  // Use process.env tokens (updated by token refresh) instead of stale startup constants
  const token = useAppToken && process.env.GH_APP_TOKEN ? process.env.GH_APP_TOKEN : (process.env.GITHUB_TOKEN || GITHUB_TOKEN);

  try {
    return execSync(command, {
      cwd: CWD,
      encoding: 'utf-8',
      env: { ...process.env, GH_TOKEN: token, GITHUB_TOKEN: token },
      maxBuffer: 10 * 1024 * 1024,
    }).trim();
  } catch (error) {
    const err = error as { stderr?: string; message?: string };
    log('ERROR', `Command failed: ${command.substring(0, 100)}...`, { error: err.stderr || err.message });
    throw error;
  }
}


/**
 * Refresh the GitHub App installation token and update environment variables.
 * Called before any gh CLI operation to ensure fresh credentials.
 */
/**
 * Resolve the GitHub App installation id for this run's target org.
 *
 * The ladder itself lives in `utils/installation.ts` so that `utils/ghPost.ts`
 * shares it rather than keeping its own (previously `installations[0]`) copy —
 * see issue #4071. This wrapper only binds the worker's `log()`.
 */
async function resolveInstallationId(jwtToken: string): Promise<string | null> {
  return sharedResolveInstallationId(jwtToken, { log });
}

async function refreshAppToken(): Promise<void> {
  const appId = process.env.GH_APP_ID;
  const privateKey = process.env.GH_APP_PRIVATE_KEY;

  if (process.env.ADP_TOKEN_MODE === 'pat') return;
  // #5223: mediated runs hold no token, so there is nothing to refresh. Returning
  // rather than throwing keeps the auth watchdog's recovery attempt a no-op: a 401
  // in a mediated run means a call that should have gone through the gateway, and
  // re-minting is neither possible nor the fix.
  if (isMediatedRun()) return;
  if (isBrokerEnabled()) {
    await getRuntimeGitHubToken();
    return;
  }

  if (!appId || !privateKey) return; // Not using app auth

  try {
    const jwt = await import('jsonwebtoken');
    const now = Math.floor(Date.now() / 1000);
    const jwtToken = jwt.default.sign(
      { iat: now - 60, exp: now + 600, iss: appId },
      privateKey,
      { algorithm: 'RS256' }
    );

    // Resolve the installation for THIS run's target org. Never blindly use
    // installations[0]: the GitHub App may be installed on many orgs/users
    // (one per onboarded tenant), and the API returns them newest-first, so
    // installations[0] is an arbitrary install — almost never the target. A
    // token minted for the wrong installation makes every comment/check-run
    // PATCH return 404 (the resource is invisible outside its installation).
    const installationId = await resolveInstallationId(jwtToken);
    if (!installationId) return;

    const tokenResp = await fetch(
      `https://api.github.com/app/installations/${installationId}/access_tokens`,
      {
        method: 'POST',
        headers: { Authorization: `Bearer ${jwtToken}`, Accept: 'application/vnd.github+json' },
      }
    );
    const tokenData = await tokenResp.json() as { token: string };
    if (tokenData.token) {
      process.env.GH_TOKEN = tokenData.token;
      process.env.GITHUB_TOKEN = tokenData.token;
      process.env.GH_APP_TOKEN = tokenData.token;
      // Issue #4369: keep the token file in step. The old comment here claimed
      // "GIT_ASKPASS reads $GITHUB_TOKEN at each git network call — updating the
      // env var is sufficient", which stopped being true at #1469:
      // git-askpass-helper and gh-wrapper both read the FILE first and only fall
      // back to the env var when it is absent. So env-only refresh left every
      // subprocess git/gh authenticating with the stale file — a refresh that
      // logged success while the run kept 401ing. The broker branch above always
      // did this; this local-mint branch was the divergence.
      writeTokenFile(tokenData.token);
      log('INFO', 'Refreshed GitHub App token for gh CLI');
    }
  } catch (err) {
    log('WARN', `Token refresh failed: ${(err as Error).message}`);
  }
}

async function gh(args: string, useAppToken: boolean = true): Promise<string> {
  return execCommand(`gh ${args}`, useAppToken);
}

interface Issue {
  number: number;
  title: string;
  body: string;
  labels: string[];
}

async function getIssue(): Promise<Issue> {
  log('INFO', 'Fetching issue details...');
  const json = await gh(`issue view ${ISSUE_NUMBER} --json number,title,body,labels`);
  const data = JSON.parse(json);
  return {
    number: data.number,
    title: data.title,
    body: data.body || '',
    labels: (data.labels || []).map((l: { name: string }) => l.name),
  };
}

interface IssueComment {
  author: string;
  body: string;
  createdAt: string;
}

async function getIssueComments(limit: number = 20): Promise<IssueComment[]> {
  log('INFO', `Fetching up to ${limit} issue comments...`);
  try {
    const json = await gh(`issue view ${ISSUE_NUMBER} --json comments --jq '.comments[-${limit}:]'`);
    const comments = JSON.parse(json || '[]') as Array<{ author: { login: string }; body: string; createdAt: string }>;
    return comments.map(c => ({
      author: c.author?.login || 'unknown',
      body: c.body || '',
      createdAt: c.createdAt || '',
    }));
  } catch (err) {
    log('WARN', `Failed to fetch comments: ${(err as Error).message}`);
    return [];
  }
}

// Find the main/parent issue from the issue body (looks for "Parent: #NNN" or "Main Issue: #NNN")
function findMainIssue(issueBody: string): number | null {
  const patterns = [
    /Parent:\s*#(\d+)/i,
    /Main Issue:\s*#(\d+)/i,
    /Reports to:\s*#(\d+)/i,
    /Part of:\s*#(\d+)/i,
  ];
  for (const pattern of patterns) {
    const match = issueBody.match(pattern);
    if (match) return parseInt(match[1]);
  }
  return null;
}

async function postToMainIssue(mainIssueNumber: number | null, body: string): Promise<void> {
  const targetIssue = mainIssueNumber || parseInt(ISSUE_NUMBER);
  log('INFO', `Posting update to issue #${targetIssue}...`);
  // Phase 2-d: prepend correlation marker before posting
  const markedBody = prependCorrelationMarker(body);
  const tmpFile = `/tmp/comment-${Date.now()}.md`;
  fs.writeFileSync(tmpFile, markedBody);
  try {
    // Refresh token before posting
    await refreshAppToken();
    await gh(`issue comment ${targetIssue} --body-file "${tmpFile}"`);
    // Phase 2-d: On success — write pointer + provenance (fail-soft)
    writeOutboundCorrelation(`issue:${targetIssue}`, 'comment_post');
  } catch (err) {
    log('WARN', `GitHub post failed, saving to S3 fallback: ${(err as Error).message}`);
    // Issue #4184: resolve the bucket from config, never from a hardcoded
    // default. Unset → one ERROR + skip, not a doomed PutObject.
    const bucket = resolveFallbackBucket(msg => log('ERROR', msg));
    if (bucket) {
      try {
        const { S3Client, PutObjectCommand } = await import('@aws-sdk/client-s3');
        const s3 = new S3Client({ region: workerAwsRegion(), credentials: workerAwsCredentials() });
        const key = buildFallbackKey(targetIssue, 'comment');
        await s3.send(new PutObjectCommand({
          Bucket: bucket,
          Key: key,
          Body: markedBody,
          ContentType: 'text/markdown',
        }));
        log('INFO', `Comment saved to s3://${bucket}/${key}`);
        console.log(`📦 GitHub API failed — comment saved to S3: ${key}`);
      } catch (s3Err) {
        log('ERROR', `Both GitHub and S3 fallback failed: ${(s3Err as Error).message}`);
      }
    }
  } finally {
    try { fs.unlinkSync(tmpFile); } catch {}
  }
}

/**
 * Write DDB pointer + provenance after a successful outbound GitHub action.
 * Fail-soft: logs warnings but never throws. (Phase 2-d)
 */
function writeOutboundCorrelation(channelSuffix: string, actionKind: string): void {
  const correlationId = process.env.ADP_CORRELATION_ID || '';
  const rootHumanId = process.env.ADP_ROOT_HUMAN_ID || '';
  const isHumanRooted = process.env.ADP_IS_HUMAN_ROOTED === 'true';

  if (!correlationId || !rootHumanId) return;

  const repo = `${REPO_OWNER}/${REPO_NAME}`;
  const channelKey = `github:${repo}:${channelSuffix}`;

  // Fire-and-forget — don't await, don't block the agent
  writePointer(channelKey, correlationId, rootHumanId, isHumanRooted).catch(err => {
    log('WARN', `Correlation pointer write failed (non-fatal): ${(err as Error).message}`);
  });

  // Issue #4029: org_id is required and non-null, and comes from the run's
  // server-resolved tenant. Skip rather than post a null the gateway must reject.
  const tenantId = process.env.ADP_TENANT_ID || '';
  if (!tenantId) {
    log('WARN', 'No ADP_TENANT_ID in env — skipping provenance post');
    return;
  }

  postProvenance({
    actorUserId: process.env.ADP_USER_ID || '',
    triggeredBy: null,
    rootHumanId,
    isHumanRooted,
    actionKind,
    // source_event must be an object — the column is JSONB. Key vocabulary
    // matches the other producers so JSONB consumers need no per-producer branch.
    sourceEvent: {
      source: 'worker:agent-runtime',
      event_type: actionKind,
      repo,
      channel_suffix: channelSuffix,
    },
    correlationId,
    orgId: tenantId,
  }).catch(err => {
    log('WARN', `Provenance post failed (non-fatal): ${(err as Error).message}`);
  });
}

async function postComment(body: string): Promise<void> {
  log('INFO', 'Posting comment to issue...');
  const tmpFile = `/tmp/comment-${Date.now()}.md`;
  fs.writeFileSync(tmpFile, body);
  try {
    await refreshAppToken();
    await gh(`issue comment ${ISSUE_NUMBER} --body-file "${tmpFile}"`);
  } catch (err) {
    log('WARN', `GitHub post failed, saving to S3 fallback: ${(err as Error).message}`);
    // Issue #4184: see postToMainIssue — bucket from config, skip when unset.
    const bucket = resolveFallbackBucket(msg => log('ERROR', msg));
    if (bucket) {
      try {
        const { S3Client, PutObjectCommand } = await import('@aws-sdk/client-s3');
        const s3 = new S3Client({ region: workerAwsRegion(), credentials: workerAwsCredentials() });
        const key = buildFallbackKey(ISSUE_NUMBER, 'comment');
        await s3.send(new PutObjectCommand({
          Bucket: bucket,
          Key: key,
          Body: body,
          ContentType: 'text/markdown',
        }));
        log('INFO', `Comment saved to s3://${bucket}/${key}`);
        console.log(`📦 GitHub API failed — comment saved to S3: ${key}`);
      } catch (s3Err) {
        log('ERROR', `Both GitHub and S3 fallback failed: ${(s3Err as Error).message}`);
      }
    }
  } finally {
    try { fs.unlinkSync(tmpFile); } catch {}
  }
}

// ============================================================================
// Beads Prime - AI-optimized workflow context
// ============================================================================

async function getBeadsPrimeContext(cwd: string): Promise<string> {
  try {
    const { execSync } = await import('child_process');
    const output = execSync('bd prime', {
      cwd,
      encoding: 'utf-8',
      timeout: 10000,
      env: workerAwsEnvironment(),
    }).trim();

    if (output) {
      // Sanitize output to remove Anthropic reserved keywords that can't be in system prompts
      const reservedPatterns = [
        /x-anthropic-[\w-]+/gi,  // Any x-anthropic-* header names
        /anthropic-[\w-]+-header/gi,  // *-header patterns
      ];

      let sanitized = output;
      for (const pattern of reservedPatterns) {
        sanitized = sanitized.replace(pattern, '[REDACTED]');
      }

      if (sanitized !== output) {
        log('WARN', 'Sanitized reserved keywords from bd prime output');
      }

      log('INFO', `Loaded bd prime context (${sanitized.length} chars)`);
      return sanitized;
    }
  } catch (err) {
    // bd prime exits silently if not in a beads project - this is expected
    const error = err as { status?: number; message?: string };
    if (error.status !== 0) {
      log('DEBUG', `bd prime not available: ${error.message || 'no output'}`);
    }
  }
  return '';
}

// ============================================================================
// Rule Loading
// ============================================================================

function loadRules(): string {
  const rulesDir = path.join(CWD, '.adp-rules');
  const rules: string[] = [];

  // Load persona definition (identity + mindset) — loaded FIRST so agent gets identity before tasks
  // Check target repo first (repo-specific wins), fall back to adp defaults
  const repoPersona = path.join(CWD, '.github-agent', 'personas', `${AGENT_TYPE}.md`);
  const adpPersona = path.join(rulesDir, 'personas', `${AGENT_TYPE}.md`);

  if (fs.existsSync(repoPersona)) {
    rules.push(`## Your Persona\n${fs.readFileSync(repoPersona, 'utf-8')}`);
  } else if (fs.existsSync(adpPersona)) {
    rules.push(`## Your Persona\n${fs.readFileSync(adpPersona, 'utf-8')}`);
  }

  // Load core workflow
  const coreWorkflow = path.join(rulesDir, 'core-workflow.md');
  if (fs.existsSync(coreWorkflow)) {
    rules.push(`## Core Workflow\n${fs.readFileSync(coreWorkflow, 'utf-8')}`);
  }

  // Load agent-specific routing rules
  const agentRouting = path.join(rulesDir, 'agents', 'agent-routing.md');
  if (fs.existsSync(agentRouting)) {
    rules.push(`## Agent Routing\n${fs.readFileSync(agentRouting, 'utf-8')}`);
  }

  // Load research guide
  const researchGuide = path.join(rulesDir, 'research', 'research-guide.md');
  if (fs.existsSync(researchGuide)) {
    rules.push(`## Research Guide\n${fs.readFileSync(researchGuide, 'utf-8')}`);
  }

  // Load Beads usage guide (shared task management)
  const beadsUsage = path.join(rulesDir, 'tools', 'beads-usage.md');
  if (fs.existsSync(beadsUsage)) {
    rules.push(`## Task Management (Beads)\n${fs.readFileSync(beadsUsage, 'utf-8')}`);
  }

  // Load phase-specific rules based on agent type
  const phaseMap: Record<string, string[]> = {
    product: ['phases/inception/requirements-analysis.md', 'phases/inception/user-stories.md'],
    architect: ['phases/inception/application-design.md', 'phases/inception/units-generation.md', 'phases/construction/functional-design.md'],
    developer: ['phases/construction/code-generation.md'],
    reviewer: ['phases/construction/pr-review.md', 'phases/construction/build-and-test.md'],
    operations: ['phases/operations/deployment.md'],
  };

  const phasePaths = phaseMap[AGENT_TYPE] || [];
  for (const phasePath of phasePaths) {
    const fullPath = path.join(rulesDir, phasePath);
    if (fs.existsSync(fullPath)) {
      rules.push(`## ${path.basename(phasePath, '.md')}\n${fs.readFileSync(fullPath, 'utf-8')}`);
    }
  }

  // Load memory rules
  const memoryRules = path.join(rulesDir, 'memory.md');
  if (fs.existsSync(memoryRules)) {
    rules.push(`## Agent Memory\n${fs.readFileSync(memoryRules, 'utf-8')}`);
  }

  rules.push(loadHumanCommunication([path.join(rulesDir, 'personas')]));

  return rules.join('\n\n---\n\n');
}

// ============================================================================
// Result metadata bridge (/tmp/adp-result-metadata.json)
// ============================================================================

/**
 * Cross-process channel to entrypoint.py. Python owns the DynamoDB write
 * because it holds both halves of the row key (`event_id` = message_id AND
 * `arrived_at`); Node only ever has `ADP_MESSAGE_ID`. Rather than export
 * `arrived_at` into Node and add a second DDB writer, Node writes facts here
 * and Python does the single write (issue #4186 Phase 1).
 */
const RESULT_METADATA_PATH = '/tmp/adp-result-metadata.json';

/**
 * Recognise a gateway spend-cap denial in a thrown SDK error (issue #4187).
 *
 * The gateway denies with HTTP 402 and a body naming the scope that ran out
 * (`run`, `chain`, or a hierarchy entity). 402 is chosen precisely because
 * nothing retries it, so by the time the error reaches here the run is over and
 * the only question is how it gets recorded.
 *
 * Matching on the message text is unpleasant but it is the only channel
 * available: the SDK surfaces upstream HTTP errors as an `Error` whose message
 * embeds the status and body, with no structured status field to read. Both the
 * status and the error code must appear, so an unrelated error that merely
 * contains "402" is not misclassified.
 *
 * Returns null when this is not a budget stop, i.e. the normal path.
 */
function detectBudgetStop(err: Error): { stopReason: string } | null {
  const message = err?.message || '';
  const lower = message.toLowerCase();
  if (!lower.includes('402') || !lower.includes('budget_exceeded')) {
    return null;
  }

  // The scope discriminator, when the gateway included one. A bare
  // `budget_exceeded` is a hierarchy cap (org/team/user), which predates #4187.
  //
  // A static enum, not a sentence — same contract as `skip_reason` (#4020), so
  // the wording lives in the frontend and can change without redeploying the
  // agent image (see frontend/src/utils/stopReason.ts).
  // `root_user` (#4300) is the initiating human's own cumulative envelope. It
  // must be distinguishable from the hierarchy default: telling an operator to
  // raise an org budget when the real limit was one person's cap sends them to
  // change the wrong knob.
  // `person` (#4630) is the person's OWN platform-wide ceiling, spanning every org
  // their agents run in. It must be distinguishable from `root_user` — which is
  // one org's cap on that person — because the remedies differ and only one of
  // them involves an administrator: nobody but the person can raise a person cap.
  const scope = /"scope"\s*:\s*"(run|chain|root_user|person)"/.exec(message)?.[1];
  const stopReason = scope === 'run'
    ? 'run_cap_exceeded'
    : scope === 'chain'
      ? 'chain_cap_exceeded'
      : scope === 'root_user'
        ? 'root_user_cap_exceeded'
        : scope === 'person'
          ? 'person_cap_exceeded'
          : 'hierarchy_cap_exceeded';
  return { stopReason };
}

/**
 * Merge fields into the result-metadata file, preserving anything already
 * there.
 *
 * Merge rather than overwrite because there are now two writers at different
 * times: the session id lands mid-stream (on first capture) and the
 * cost/turns fields land at the `result` message. A truncating write from
 * either would erase the other — and the cost/turns pair is load-bearing for
 * the zero-token infrastructure-failure discriminator in entrypoint.py
 * (issue #2883), so losing it would resurrect that bug.
 *
 * Best-effort by design: mirrors the /tmp/adp-check-run-final.md pattern and
 * never throws.
 */
function writeResultMetadata(fields: Record<string, unknown>): void {
  try {
    let existing: Record<string, unknown> = {};
    try {
      const raw = fs.readFileSync(RESULT_METADATA_PATH, 'utf8');
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        existing = parsed as Record<string, unknown>;
      }
    } catch {
      // Absent or unparseable — start from an empty object. A corrupt file is
      // not worth failing over; the fields we are about to write are the ones
      // that matter.
    }
    fs.writeFileSync(
      RESULT_METADATA_PATH,
      JSON.stringify({ ...existing, ...fields }),
      'utf8',
    );
  } catch (err) {
    log('WARN', `Failed to write result metadata (non-fatal): ${err}`);
  }
}

// ============================================================================
// Agent Execution
// ============================================================================

// NOTE: Project board status updates are handled by the GitHub Actions workflow
// using the update-board-status action, which uses an efficient single GraphQL query.
// The workflow sets "In Progress" before the agent runs.
// "Done" status is set automatically by GitHub project automation when the issue is closed.
// This avoids redundant API calls that can cause rate limiting.

async function runAgent(issue: Issue, mainIssueNumber: number | null, beadsPrimeContext: string = '', commentsContext: string = '', memoryCtx: string = ''): Promise<string> {
  const rules = loadRules();
  log('INFO', `Loaded ${rules.length} characters of rules`);

  const agentDescriptions: Record<string, string> = {
    product: 'Product Owner - responsible for requirements, user stories, acceptance criteria, and personas',
    architect: 'System Architect - responsible for design, architecture decisions, and units generation',
    developer: 'Developer - responsible for code implementation, unit tests, and PRs',
    reviewer: 'Code Reviewer - responsible for code review, integration testing, and quality validation',
    operations: 'DevOps/SRE and delivery coordinator - responsible for authorized infrastructure work and orchestration through acceptance',
  };

  const mainIssueInfo = mainIssueNumber
    ? `\n\n**IMPORTANT**: This task is part of a larger initiative. Post your progress updates to the MAIN issue #${mainIssueNumber}.`
    : '';

  // ── Prompt assembly: most-stable-first ordering (issue #4183) ──────────────
  // Sections are emitted in descending order of stability, so that the
  // invariant head of the prompt is an actual PREFIX:
  //
  //   1. role line                       — varies by AGENT_TYPE, not by run
  //   2. Rules and Guidelines (`rules`)  — the largest block in the prompt;
  //                                        byte-identical for a given persona
  //   3. Available Skills / Knowledge Layer — static text, env-gated
  //   ─────── stable/variable boundary: the `## Your Task` heading ───────
  //   4. issue title / body / memory / comments — new on every single run
  //   5. Instructions                    — interpolates ISSUE_NUMBER, so it
  //                                        belongs below the boundary
  //
  // Previously `rules` was emitted AFTER the issue body, which put the largest
  // invariant segment behind the highest-entropy one: nothing downstream of the
  // issue body could ever be a reusable prefix.
  //
  // NOTE for the cache-marker work (#4180): `## Your Task` is the boundary a
  // cache breakpoint should attach to. Everything above it is run-invariant;
  // everything below it changes per run. Ordering alone does not produce reuse
  // — the provider must also be told where the boundary is, which is that
  // issue's job and not this one's. Do not introduce run-specific
  // interpolations above `## Your Task` without moving that breakpoint too.
  const prompt = `You are @agent-${AGENT_TYPE}, the ${agentDescriptions[AGENT_TYPE] || 'agent'}.

## Rules and Guidelines

${rules}

---

## Available Skills

You have access to skills in \`.claude/skills/\`. Each skill has a \`SKILL.md\` with instructions.

**To use skills:**
1. Check if \`.claude/skills/\` exists in the repo
2. List available skills with \`ls .claude/skills/\`
3. Read the relevant \`SKILL.md\` files for instructions
4. Follow the skill's instructions to complete the task

**Common skills that may be available:**
- \`skypilot\`: Deploy workloads on cloud GPUs (AWS, Lambda, Nebius)
- \`webapp-testing\`: Playwright-based web application testing
- \`mcp-builder\`: Build custom MCP servers

**IMPORTANT:** If a skill exists that's relevant to your task, USE IT. Read its SKILL.md and follow the instructions.
${KNOWLEDGE_LAYER_ENABLED ? `
---

${KNOWLEDGE_LAYER_PROMPT}` : ''}${MEDIATED_GITHUB_ENABLED ? `
---

${MEDIATED_GITHUB_PROMPT}` : ''}

---

## Your Task
Process this GitHub issue and complete the assigned work.${mainIssueInfo}

### Issue #${issue.number}: ${issue.title}

${wrapUntrusted(issue.body)}
${memoryCtx ? `
---

${memoryCtx}
` : ''}${commentsContext ? `
---

## Existing Discussion / Comments

The following comments have been posted on this issue. Read them carefully - they may contain important context or research from previous agents or users.

${wrapUntrusted(commentsContext)}
` : ''}
---

## Instructions

**IMPORTANT: You MUST follow this structured approach:**

### Step 1: Analyze and Plan
- Read the issue carefully to understand what's being asked
- Identify whether the task is a bounded assessment, repository review, implementation or deployment
- Research only what the requested conclusion needs; use supplied evidence for a bounded scenario
- Create a clear plan for the assigned role and task

### Step 2: Post Your Plan
For a small read-only assessment answerable from the supplied material, skip a
separate plan comment and return the answer as your final response. Do not launch
a repository scan or create specification artifacts just to fill a role template.
If the task is hypothetical, assess its stated premises; do not replace them with
today's implementation. A missing review target calls for a brief blocked outcome.

For implementation, substantial investigation or a workflow that requires a plan,
post a self-contained plan before substantive work using
\`gh issue comment ${ISSUE_NUMBER} --body-file <plan-file>\`.
Explain the requested change, main steps and verification in enough detail for
the reader to understand the work without opening another document.
Match the plan to your role; do not announce implementation for an assessment.
For an implementation plan, lead with two clearly labelled parts:

- **My understanding of the task:** explain the requested change in simple
  language and good detail. Start with how it should work when complete, then
  explain the relevant current behavior and what must keep working. Focus on
  the change itself; no required "who needs this" section. Do not open with
  file paths or internal mechanisms unless they are the requested change.
- **How I plan to implement it:** explain the proposed approach in logical order,
  leading each step with what it accomplishes, why it is needed and how it
  connects to the other steps. Explain how you will check the result. For
  example, "Keep each environment's login separate so signing in to one cannot
  overwrite another" explains a step before naming storage files or locks.

Write both in plain, self-contained language for someone who has not read the
issue, earlier comments, design documents or code. A self-contained plan does
not need to reproduce the technical design. Explain each requirement once;
keep supporting file inventories, schemas, locks, ports, helper names and
branch/checkpoint details after the readable explanation or in the design.
Explain unavoidable technical terms by their purpose. Keep consequential
decisions and verification in the explanation itself; links cannot replace it.
Scale detail to the task without a fixed word limit or repeated design prose.
Before posting, check that the reader can describe the change, main steps and
verification without the technical notes. Describe the proposed behavior and
approach, not private deliberation.

Qualify unresolved facts accurately: an environment's address or test access
being unverified does not mean the environment does not exist. State what
needs checking, the affected step and work that can continue. Put commands
for different terminals in separate labelled code blocks, not side by side
in one shell block; identify placeholders and prerequisites.

Existing approval gates and required AIDLC plan artifacts still apply; the small
assessment exception does not bypass them or authorize execution.
Apply phase templates when the task is part of that workflow, not merely because
the persona has those templates available.
${developerCheckpointGuidance(AGENT_TYPE)}

### Step 3: Execute Your Plan
- Follow your plan step by step
- Create/modify files as needed
- Follow phase rules relevant to your agent type
- Document your work in appropriate locations

## Branch naming (MANDATORY)

When creating a git branch for your work, it MUST be named exactly:

    agent/issue-${ISSUE_NUMBER}

Use this command to create and switch to it:

    git checkout -b agent/issue-${ISSUE_NUMBER} 2>/dev/null || git checkout agent/issue-${ISSUE_NUMBER}

**This is not a style preference — it's a contract.** The reviewer-trigger workflow
(\`.github/workflows/pr-review-trigger.yml\`) only fires when the PR's \`head_ref\`
matches \`agent/issue-*\`. A branch with any other name will:
- Be pushed and open a PR successfully, BUT
- NOT trigger the reviewer agent (silent skip)
- NOT be found by downstream \`gh pr list --head agent/issue-${ISSUE_NUMBER}\` queries

If you need to push multiple branches for a single issue (rare), still prefix
with \`agent/issue-${ISSUE_NUMBER}-\` followed by a short suffix
(e.g. \`agent/issue-${ISSUE_NUMBER}-followup\`). The prefix match is what the trigger needs.

## Coding Guidelines (MANDATORY for all code changes)

Before editing or creating any code file, read and internalize \`docs/agent-coding-guidelines.md\`. Four principles:

1. **Think before coding** — state assumptions, surface tradeoffs, ask when unclear
2. **Simplicity first** — minimum code that solves the stated problem; no speculative features
3. **Surgical changes** — every changed line must trace directly to the user's request
4. **Goal-driven execution** — transform tasks into verifiable goals; state plans with per-step verification

**Hard rule**: if a file or line in your diff doesn't trace to an acceptance criterion in the issue, delete it before opening the PR.

Full guidelines at \`docs/agent-coding-guidelines.md\`.

## Pre-submit checks (MANDATORY before requesting review)

Do not create draft PRs, even if older task text requests one. Complete the agreed implementation, integration, tests and documentation, then run the linters and tests for the module(s) you touched before opening a ready PR or requesting review. Incomplete branch checkpoints may be pushed with check status disclosed; share commit links and continue working. Reuse any existing PR, marking an existing draft ready only after the same completion checks. Required CI still gates merge.

### Module → check commands

| Module you touched | Commands to run (in that order) |
|---|---|
| \`modules/gateway/\` (Python) | \`cd modules/gateway && ruff check src/ tests/ && ruff format --check src/ tests/ && python3 -m pytest tests/ -q\` |
| \`modules/agent-factory/agent/\` (TypeScript) | \`cd modules/agent-factory/agent && npx tsc --noEmit && npx jest\` |
| \`modules/agent-factory/gateway/lambdas/\` (Python) | \`cd modules/agent-factory && python3 -m pytest tests/lambda/ -q\` |
| \`modules/agent-context/\` (Python) | \`cd modules/agent-context && ruff check . && python3 -m pytest\` |
| Terraform (\`*/infra/\`, \`platform/infra/\`) | \`cd <module>/infra && terraform fmt -check && terraform validate\` |

### Rules

- **Run ALL commands for EVERY module you touched.** If your diff spans two modules, run two sets of checks.
- **If any command fails**, fix the underlying issue before requesting review. You may push an incomplete checkpoint with the failure disclosed. Do NOT suppress warnings with \`# noqa\` or \`eslint-disable\` unless the rule genuinely doesn't apply — and note why in a comment.
- **If a check fails on code you didn't touch** (pre-existing debt), note it in the PR description as "pre-existing on main: <file>:<line> <rule>" and move on. Don't clean up unrelated debt in the same PR (surgical changes principle from \`docs/agent-coding-guidelines.md\`).
- **Auto-fix tools are fine**: \`ruff check --fix\`, \`ruff format\`, \`eslint --fix\`. Treat their output as code you wrote — review the diff before committing.

### Post-commit sanity

After committing, before pushing, run \`git diff HEAD~1 --stat\` and confirm the files you expected to change are the only ones that changed. If the linter reformatted a file you didn't mean to touch, that's a surgical-changes violation — revert it.

Failing to run these checks is a process bug. PRs that land with lint/test failures traceable to the PR's own changes will be reverted.

${AGENT_TYPE === 'reviewer' ? `### Step 3.4: Spec-vs-diff Review (MANDATORY for @agent-reviewer)

Verify the assigned PR independently against accepted scope and current code;
do not trust its description as proof. The reviewer owns review, in-scope repair,
verification and the final report on the SAME PR. Reviewer-specific instructions
below take precedence over generic instructions to create a new branch/PR.

**If you cannot find PR_NUMBER in the environment**, do not proceed with an unscoped
PR review. Return a brief setup blocker with the missing target and next action.
Do not select an unrelated PR or manufacture review/security evidence.

1. **Identify the PR and the driving issue:**
   \`\`\`bash
   gh pr view \$PR_NUMBER --json number,title,body,state,isDraft,headRefName,headRefOid,files
   \`\`\`
   Stop if the assigned PR is a draft or is not open; do not mark it ready for its
   author. Verify the repository and bound issue, record headRefOid, and fetch
   that exact branch/diff. Never review a substitute checkout.
   \`\`\`bash
   gh pr diff \$PR_NUMBER > /tmp/pr-diff.patch
   \`\`\`

2. **Extract the acceptance criteria from the issue:**
   Re-read the issue body (already shown above), accepted story/design and
   applicable AGENTS.md. Separate code-merge
   requirements from explicitly deferred deployment/live criteria; retain their
   later gates without requiring live execution in this code review.

3. **Verify each criterion against the diff and current implementation:**
   Include unchanged code and test evidence. A missing changed line is not proof
   of missing behavior. Missing evidence is unverified until investigated, not an
   automatic HIGH-confidence defect. Report the concrete failure or unmet
   applicable requirement and practical consequence for every blocker.

4. **Check for invariant violations:**
   Verify the actual scope, behavior and evidence for each claimed violation.
   Preserve accepted architecture and isolation; do not invent requirements.

5. **Check for committed files that should not exist:**
   Respect AGENTS.md prohibitions such as \`agent_learning/*.md\`, secrets or
   generated infrastructure artifacts. Remove prohibited additions when safe and
   authorized; do not just suggest a fix you can make on the assigned PR.

6. **Label each finding independently:**
   - Impact severity: high / medium / low, with the practical consequence
   - Confidence: high / medium / low, with evidence or uncertainty
   - Approval impact: blocker / discussion needed / optional follow-up
   Applicable acceptance, security and prohibited-file requirements remain
   blockers. Style preferences and unrelated inherited debt are optional.

7. Collect provisional findings and continue to security review and repair.
   Do not publish a final REQUEST CHANGES or dispatch a developer for findings
   that you can fix within this task's scope, authority and remaining budget.

### Step 3.5: Security Review (MANDATORY for @agent-reviewer)

Run \`/security-review\` before approving. Investigate its findings against the
actual changed behavior, reachability and threat model; scanner output alone is
not proof. Check secrets, auth/authz, inputs, dependencies and configuration.
Fold confirmed in-scope defects into the repair batch below. Preserve explicit
permissions for changes to live credentials, resources, security policy or data;
review authority does not grant those operations. Keep functional and security
evidence distinct and do not claim either was run when it was not.

### Step 3.6: Reviewer-owned repair, verification and final verdict

1. **Verify branch ownership before edits.** Use current claim/run evidence to
   establish one writer. An active developer/reviewer/supervisor or unavailable
   ownership is a concrete hold, not permission to race. Re-read the remote head;
   concurrent changes require reconciliation. Never reset or force-push.
2. **Fix confirmed in-scope defects on the existing PR branch.** Missing behavior,
   logic/error-handling bugs, configuration, failing tests and inaccurate required
   handoff evidence are reviewer work when the solution is clear. Keep repairs
   surgical and batch related findings. Work size alone does not require sending
   the story back. Reproduce meaningful failures and add useful regressions.
   A read-only delegated review returns findings for the owning reviewer to fix;
   respect an explicitly read-only parent task and unavailable write authority.
3. **Verify and publish the repairs.** Inspect the changed diff, run affected
   tests/integrations and pinned lint tools, stage only intended files, commit and
   push through the authorized path to the SAME PR. Confirm the remote SHA. Do
   not claim unpublished local changes are fixed in the PR. Do not commit review
   logs or create artifact-only PRs. Preserve claim, action, lineage and budget;
   do not manually trigger another developer/reviewer solely because you fixed
   something. Cooperate with any review already scheduled by the engine.
4. **Validate the final revision.** Recheck repaired behavior and affected security
   surfaces, and observe all required checks. Reuse identified evidence for
   unchanged areas; broaden verification for changed risk, failure or unresolved
   concerns instead of repeating an unchanged full review. A new head invalidates
   earlier approval. You are the repair author; satisfy any independently required
   approval without pretending your own verdict is independent approval.
   Billing/runner/credential failures are external check blocks, not code rework.
   Never waive required CI or treat skipped/unrun tests as passes.
5. **Hand off only concrete blockers.** Complete independent authorized repairs
   first. An unresolved product/security/architecture decision, expanded scope,
   missing authority/input, active writer, external failure or exhausted limit
   needs its exact reason, remaining findings, owner and next action. Do not
   return REQUEST CHANGES for defects already fixed or merely optional cleanup.
6. **Write the final review summary to a file** at
   \`data/code-review/review-$(date +%Y%m%d)-pr-\$PR_NUMBER.md\`:
   - Start with verdict (APPROVE / REQUEST CHANGES / BLOCK), verified final revision,
     fixes/commits, remaining blocker count and practical consequence.
   - State validation gaps and outstanding required checks, and the next owner/action.
   - Record each finding as fixed (author/commit/evidence), unresolved blocker
     (reason/owner) or optional follow-up. Include functional/security results and
     required engine attribution. Interim updates must state actual phase/owner:
     reviewing, reviewer fixing, verifying, or waiting for a named input/check.
   - Follow with the full acceptance-criteria checklist (satisfied/missing,
     evidence per criterion) and detailed findings.
7. **Publish the final result for the verified remote head** through the available
   structured review/artifact channel and assigned PR:
   \`\`\`bash
   gh pr comment \$PR_NUMBER --body-file data/code-review/review-$(date +%Y%m%d)-pr-\$PR_NUMBER.md
   \`\`\`
   Publish any required formal GitHub review through the authorized review path;
   if the current identity cannot do so, name that pending approval explicitly.
   A comment or successful worker exit is not a substitute for required approval.
   Leave merging to the configured owner unless explicitly authorized to merge.

**DO NOT approve a PR if**:
- Any merge-blocking finding is unresolved
- Any acceptance criterion due at this stage is unsatisfied
- A prohibited-file violation or required check/independent approval is unresolved
- Functional/security evidence describes a different head

Do not merge, deploy, approve live gates or change credentials as an incidental
part of review. See pr-review.md and the reviewer persona for this same contract.
` : ''}${AGENT_TYPE === 'operations' ? `### Step 3.5: Execution (MANDATORY for @agent-operations)
**For authorized deployment work, execute and verify the requested infrastructure changes.**

The following execution steps apply only to an authorized deployment task. For
an assessment of a supplied record, assess that record and label its provenance;
do not run deployment commands or treat absent deployment authorization as a
blocker. Keep conclusions within the supplied evidence.

When working on deployment tasks:

1. **Check for relevant skills first:**
   \`\`\`bash
   ls .claude/skills/
   \`\`\`
   If a skill like \`skypilot\` exists, READ its SKILL.md and USE the commands it describes.

2. **EXECUTE the actual deployment commands:**
   - For SkyPilot: Run \`sky launch\`, \`sky exec\`, \`sky status\`, etc.
   - For Kubernetes: Run \`kubectl apply\`, \`kubectl get\`, etc.
   - For Terraform: Run \`terraform plan\`, \`terraform apply\`, etc.
   - For Docker: Run \`docker build\`, \`docker push\`, etc.

3. **Verify the deployment worked:**
   - Check service status (\`sky status\`, \`kubectl get pods\`, etc.)
   - Test endpoints if applicable (curl, health checks)
   - Capture and report the endpoint URL/IP

4. **If you CANNOT execute the deployment**, you MUST clearly state why:
   - Missing approval? State: "Deployment blocked: awaiting human approval for [X]"
   - Missing credentials? State: "Deployment blocked: missing [credential/permission]"
   - Cluster not ready? State: "Deployment blocked: [resource] not available"
   - Other blocker? State the specific reason

**DO NOT just create YAML files, PRs, or documentation without attempting actual deployment.**
**Deployment is complete only when the requested deployment and checks are verified.**
If deployment is blocked, report that action as blocked and continue any independent
authorized work. For orchestration, retain the assigned review, repair, merge,
deployment and evaluation ownership until acceptance, an acknowledged continuation,
or an evidenced block/stop as defined in the operations persona. A clear blocker
report or a child dispatch does not complete the delivery assignment.
` : ''}### Step 4: Report Results
- Return the outcome once in your final response, following Completion Summary Format
- Name the meaningful result, remaining blockers and next owner/action
- Include file changes only when they help the user assess the requested work

## Available Tools

You have access to:
- **Bash**: Execute shell commands (gh CLI, git, bd, etc.)
- **Read/Write/Edit**: File operations
- **Glob/Grep**: Search codebase
- **WebSearch/WebFetch**: Research external sources

### Beads Task Management (bd)

You can use \`bd\` commands to manage tasks and dependencies:
- \`bd ready --json\` - List tasks ready to work on
- \`bd show <task-id>\` - View task details and dependencies
- \`bd create "Title" -p 1 --json\` - Create a discovered subtask
- \`bd dep add <task> <blocker> --type discovered-from\` - Link discovered work
- \`bd list --json\` - List all tasks

Use Beads when you:
- Discover new work that should be tracked
- Find your task is blocked by something
- Need to check what else is ready to work on

${beadsPrimeContext ? `### Beads Workflow Context (from bd prime)

${beadsPrimeContext}` : ''}

## Completion Summary Format

Your FINAL message is the human outcome report. Follow the shared writing rules
and your persona's format. Start with the capability/problem and its actual
state, including any blocker; then evidence, limitations and next owner/action.
Use a few connected paragraphs or brief sections when helpful. Distinguish a
prepared design, PR opened, code merged, deployment and verified acceptance.
A run ending does not establish any of those states. Do not use a generic
"Task Complete" heading when work or required checks remain.

The runtime publishes your final response as the issue's outcome. For an issue
assessment, do not first use \`gh issue comment\` to publish the assessment and
then return a recap: both would appear on the same issue. Return the full answer
only here. Required gate comments, formal PR reviews and audit records still
belong in their designated places; link to those with a brief status and next
action rather than repeating their findings in the final response.
Keep technical inventories, commands and long test matrices below the summary
or in linked evidence. For implementation work, include the shared policy's
walkthrough of the mechanism, decisions and reproducible verification in the
final response. Critical caveats must remain visible.

${AGENT_TYPE === 'operations' ? `For operations, name the target environment and state separately whether
 deployment ran, the service is running, and the endpoint was checked. Report
 passed, failed, skipped and not-run checks; explain missing deployment or
 acceptance checks and the next action. Include relevant cleanup/cost exposure.
` : ''}
**Write handoff learnings to file**: Before returning your final report, save detailed learnings to \`agent_learning/{date}-issue-{number}-learnings.md\`. This file is read by future agents — make it HIGH QUALITY:
- What worked and what didn't (specific commands, configurations, error messages)
- Key technical decisions and why they were made
- Gotchas, workarounds, and things that took multiple attempts
- Exact versions, endpoints, resource names that future agents will need
- NEVER include secrets, API keys, tokens, passwords, or private keys in learnings

Writing the required learnings record is a file change, even when no source code
changed. Do not end with blanket claims such as "no files changed" or "no actions
taken". Omit routine scope footers and bookkeeping unless requested or consequential;
when needed, describe the verified scope precisely. Keep missing checks visible.
Before returning, remove unnecessary assumptions and follow-up questions that
would not change the recommendation or the user's requested next step. Keep
qualifications beside their claims and required approvals explicit; a
recommendation is not the owner's decision. Keep each message as short as its
purpose allows. Ending an explanation does not end execution: continue unfinished
authorized work, including required waits and follow-through, until the assigned
acceptance is verified, an authorized continuation is acknowledged, or an evidenced
block, human gate, cancellation or execution limit requires stopping. A written
next action is not an accepted handoff. A standalone assessment ends when its
requested answer is complete.

Now, complete the assigned task.`;

  // ── Check Run Streamer ────────────────────────────────────────────────────
  // Instantiate only when CHECK_RUN_ID is present (pod environment with #417
  // entrypoint baseline). Absent in ARC-runner flows → complete no-op.
  let checkRunStreamer: CheckRunStreamer | null = null;
  // Sink for streamer PATCH errors; assigned once the auth watchdog exists
  // inside the query loop below (#4430). Until then, errors are just logged.
  let checkRunPatchErrorSink: ((msg: string) => void) | null = null;
  const checkRunIdEnv = process.env.CHECK_RUN_ID;
  if (checkRunIdEnv) {
    const crId = parseInt(checkRunIdEnv, 10);
    // One-shot PRESENCE check only — don't start a streamer with no token at
    // all. The value itself must never be captured for PATCHes: a run outlives
    // its ~60-min installation token, so the streamer resolves the token at
    // patch time via tokenProvider (#4430).
    const crToken = process.env.GITHUB_TOKEN || '';
    const crRepo = `${REPO_OWNER}/${REPO_NAME}`;
    if (!isNaN(crId) && crToken && crRepo !== '/') {
      checkRunStreamer = new CheckRunStreamer({
        checkRunId: crId,
        repo: crRepo,
        // Same precedence ladder as ghPost.ts — read fresh on every PATCH so
        // the token manager's re-mints actually reach the streamer.
        tokenProvider: () =>
          process.env.GH_APP_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN || '',
        persona: AGENT_TYPE,
        issueNumber: parseInt(ISSUE_NUMBER) || 0,
        model: MODEL,
        log: (msg) => log('WARN', msg),
        onPatchError: (msg) => checkRunPatchErrorSink?.(msg),
      });
      log('INFO', `CheckRunStreamer active for check run ${crId}`);
    }
  }
  // ─────────────────────────────────────────────────────────────────────────

  // ── Codex Event Watcher ───────────────────────────────────────────────────
  // Tail the stable Codex events file (written by run-codex.sh, #2884) and
  // stream compact per-step summaries to BOTH live sinks while a codex-bridge
  // delegation runs. Inert by default (no file → zero PATCHes/noise); never
  // throws into the worker loop; owned by the worker lifecycle (start now,
  // dispose in finally). Runs in every worker — only Codex-delegating runs
  // produce an events file for it to see.
  const codexEventWatcher = new CodexEventWatcher({
    eventsFile: process.env.CODEX_EVENTS_FILE,
    checkRunStreamer,
    liveComment: activeLiveComment,
    log: (msg) => log('WARN', msg),
  });
  codexEventWatcher.start();
  // ─────────────────────────────────────────────────────────────────────────

  log('INFO', 'Starting agent execution...');
  if (AIDLC_ENABLED) {
    log('INFO', 'AIDLC install detected — Task tool enabled');
  }
  console.log('\n' + '═'.repeat(60));
  console.log(`Starting @agent-${AGENT_TYPE} Query`);
  console.log('═'.repeat(60) + '\n');

  try {
    let turnCount = 0;
    let fullResponse = '';
    let lastTurnText = '';
    let lastActivityTime = Date.now();
    let queryCompleted = false;          // tracks whether a 'result' message was received
    let queryCompletedTime: number | null = null; // timestamp when query completed

    // Max time (ms) to wait for the stream to close after query completes.
    // If the SDK iterator doesn't terminate within this window, the heartbeat
    // will force-exit the process.  10 minutes is generous — in practice the
    // stream should close within seconds.
    const POST_COMPLETION_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes

    // Issue #4369: watch the stream for the stale-token signature. The failing
    // pushes happen inside the SDK subprocess, so this is the only place the
    // worker can see them.
    const authWatchdog = new AuthWatchdog();

    /**
     * Apply the watchdog's verdict for one chunk of stream output.
     *
     * Refreshing rewrites the token file that the subprocess's git/gh read at
     * command time, so a re-mint here actually reaches the failing caller. If
     * 401s survive that, exiting with EXIT_RETRYABLE hands the task back to SQS
     * for redelivery into a fresh pod — the automatic form of the manual
     * `kubectl delete job` recovery this bug required.
     */
    const applyAuthWatchdog = async (text: string): Promise<void> => {
      const action = authWatchdog.observe(text);

      if (action === 'force_refresh') {
        log('WARN', 'Repeated GitHub 401s in the agent stream — forcing token refresh', {
          phase: 'auth-watchdog',
        });
        try {
          await forceRefresh();
          log('INFO', 'Token force-refreshed after 401 cluster', { phase: 'auth-watchdog' });
        } catch (err) {
          log('ERROR', `Forced token refresh failed: ${(err as Error).message}`, {
            phase: 'auth-watchdog',
          });
        }
        return;
      }

      if (action === 'abort') {
        log('ERROR', 'GitHub 401s persist after a forced token refresh — aborting for retry', {
          phase: 'auth-watchdog',
        });
        // Record the cause before exiting: without it this looks like a generic
        // crash and the operator re-runs it blind.
        writeResultMetadata({ auth_failure: true, stop_reason: 'github_auth_401' });
        await flushCloudWatch();
        process.exit(EXIT_RETRYABLE);
      }
    };

    // Route check-run PATCH failures into the watchdog (#4430): a PATCH failing
    // every cycle against a dead token is the highest-signal 401 in the run,
    // and previously it was swallowed into a WARN log the watchdog never saw.
    checkRunPatchErrorSink = (msg) => {
      void applyAuthWatchdog(msg).catch(() => {
        // fail-soft: a watchdog error must never break the streamer
      });
    };

    // Heartbeat: log a "still alive" message if no SDK messages arrive for 60s.
    // Also acts as a safety net: if the query already completed but the stream
    // hasn't closed, force-exit after POST_COMPLETION_TIMEOUT_MS.
    const heartbeat = setInterval(() => {
      const silentSec = Math.round((Date.now() - lastActivityTime) / 1000);
      // Issue #3961: a paused run is silent *on purpose*. Every read below is of
      // the live gate rather than a captured boolean, because a pause can begin
      // and end between two ticks of this interval.
      const gate = activeControlRuntime?.gate;
      const paused = gate?.isPauseActive() === true;

      // Safety net: force exit if stream hangs after query completion.
      //
      // Skipped while paused. This watchdog exists to catch a stream that never
      // closed, and it cannot distinguish that from a run whose last tool is
      // parked at the admission barrier — so left unguarded it would kill a
      // healthy paused run within POST_COMPLETION_TIMEOUT_MS, i.e. an operator
      // pausing to look at something would come back to a dead pod. The pause has
      // its own bound (the gate's expiry timer, clamped to the pod deadline), so
      // skipping here defers to a bound rather than removing one. Note the
      // condition is only about *starting* the exit: once the pause is released,
      // the elapsed comparison uses the original completion time, so a stream
      // that really is hung is still caught on the next tick.
      if (queryCompleted && queryCompletedTime && !paused) {
        const elapsed = Date.now() - queryCompletedTime;
        if (elapsed >= POST_COMPLETION_TIMEOUT_MS) {
          const msg = `⚠️  Force exit — stream did not close ${Math.round(elapsed / 1000)}s after query completed`;
          console.log(msg);
          log('WARN', msg, { phase: 'post-completion-timeout', elapsedMs: elapsed });
          process.exit(0);
        }
      }

      // Visibility is preserved through a pause, not suppressed: the heartbeat
      // keeps logging, and says *why* it is quiet. An operator watching the log
      // of a paused run must be able to tell "paused, holding N tools" apart from
      // "stalled", and a silent log is the one thing that makes those identical.
      if (silentSec >= 60) {
        const msg = paused
          ? `💓 Heartbeat — paused by operator, no SDK messages for ${silentSec}s (turn ${turnCount})`
          : `💓 Heartbeat — no SDK messages for ${silentSec}s (turn ${turnCount})`;
        console.log(msg);
        log('INFO', msg, {
          phase: 'heartbeat',
          silentSeconds: silentSec,
          turn: turnCount,
          ...(paused
            ? {
                controlPhase: gate?.currentPhase(),
                heldTools: gate?.heldCount(),
                activeTools: gate?.activeToolCount(),
              }
            : {}),
        });
      }
    }, 30_000);

    try {
      // Issue #3962 left the adapter's three transport hooks (`attemptInputFactory`,
      // `onAttemptHandle`, `cancellation`) proven-but-unused here, because passing
      // them switches the prompt from a string to a streaming iterable — a
      // different SDK code path — and S3 had no verb that needed it.
      //
      // Issue #3961 is the story that needs it, so they go in now, behind
      // `activeControlRuntime`. Two of the three are load-bearing for pause:
      // `onAttemptHandle` is what publishes a live attempt, and without an attempt
      // `requestPause` correctly reports `unavailable` however good the barrier is;
      // and the input channel it opens is how an *expired* pause delivers its
      // neutral annotation back into the same session.
      //
      // The gate is exactly as #3962 described: the switch happens for runs that
      // asked for control, and nothing changes for the ordinary runs — including
      // the other 17 callers of this wrapper — because a run with no started
      // listener passes `undefined` for all three and takes the string-prompt path
      // byte-for-byte.
      const control = activeControlRuntime;
      // Labeled loop so we can break out of the `for await` from inside the
      // switch statement.  Without the label, `break` only exits the switch.
      queryLoop:                          // eslint-disable-line no-labels
      for await (const message of resilientQuery({
        queryParams: {
          prompt,
          options: {
            model: MODEL,
            cwd: CWD,
            allowedTools: [
              'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill',
              ...(KNOWLEDGE_LAYER_ENABLED ? KNOWLEDGE_LAYER_TOOLS : []),
              ...(AIDLC_ENABLED ? ['Task'] : []),
            ],
            ...(KNOWLEDGE_LAYER_ENABLED ? {
              mcpServers: { [KNOWLEDGE_LAYER_SERVER_NAME]: getKnowledgeLayerMcpConfig() },
            } : {}),
            settingSources: ['project'],
            permissionMode: 'bypassPermissions',
            // Issue #2079: persist the session to disk so resilientQuery can
            // TRULY resume it (via options.resume) after a transient stream
            // stall, instead of restarting the task from scratch. The SDK can
            // only resume sessions it persisted. CLAUDE_CONFIG_DIR points at
            // ephemeral container storage, so this leaves no durable footprint
            // beyond the pod's lifetime.
            persistSession: true,
            maxTurns: 10000,
            // Issue #4179: spill oversized tool output to the run's workspace
            // and hand the model a `Read`-able locator instead of the full
            // blob. With maxTurns: 10000, one verbose command's output would
            // otherwise be re-sent on every remaining turn and force an early
            // (lossy) compaction. The hook fails open — a storage error leaves
            // the original output in place.
            // Issue #3961: the pause barrier joins the same composed hook set.
            // `pauseHooks` is undefined for a run with no control listener, in
            // which case the composed object is byte-identical to what it was
            // before this story — an ordinary run gains no PreToolUse hook and
            // therefore no new failure mode on the path every agent takes.
            hooks: createWorkerToolHooks({
              agentType: AGENT_TYPE,
              store: buildWorkerSpillStore(),
              log: (msg) => log('INFO', msg),
            }),
          }
        },
        maxRetries: 5,
        baseDelayMs: 10_000,
        maxDelayMs: 120_000,
        idleTimeoutMs: 600_000, // 10 min — detect silent upstream stalls (issue #1223)
        // Per-attempt transport hooks and output hold (#3961). These are
        // undefined without a started control listener.
        attemptInputFactory: control?.adapter.attemptInputFactory((pauseHooks) => ({
          hooks: createWorkerToolHooks({
            agentType: AGENT_TYPE,
            store: buildWorkerSpillStore(),
            log: (msg) => log('INFO', msg),
            pauseHooks,
          }),
        })),
        beforeOutput: control ? () => control.gate.waitForOutput() : undefined,
        onAttemptHandle: control?.adapter.onAttemptHandle(),
        cancellation: control?.adapter.cancellationSource(),
        // Issue #3961: a paused stream is quiet on purpose. Without this the idle
        // guard would retry the attempt a pause is deliberately holding, and a
        // retry replaces the live attempt — destroying the same-execution resume
        // that is the entire point of pausing rather than stopping.
        idleSuspended: control ? () => control.gate.isPauseActive() : undefined,
        // Issue #2079: On retry, resilientQuery resumes the persisted session
        // (full conversation history reloaded), so this nudge is just a short
        // continuation instruction — the agent already remembers what it read,
        // decided, and posted. If no session_id was captured before the stall,
        // resilientQuery falls back to prepending this to the original prompt.
        resumeContext: (attemptNumber, priorMessagesYielded) => [
          `You are RESUMING this task after a transient stream interruption`,
          `(retry attempt ${attemptNumber}; ${priorMessagesYielded} messages produced before the stall).`,
          `Continue from where you left off — do NOT repeat completed work`,
          `(re-reading files, re-running analysis, or re-posting an Implementation`,
          `Plan you already posted). Proceed with the next unfinished step.`,
        ].join('\n'),
        // Issue #4186 (Phase 1): record the SDK session id as soon as it
        // exists, so it outlives this process. Written to the metadata bridge
        // (not DynamoDB) because Python holds the row key — see
        // writeResultMetadata. Observability only: nothing reads this to
        // resume yet (that is Phase 3), so a Phase-1 deploy cannot change the
        // outcome of any run.
        onSessionId: (sessionId) => {
          log('INFO', `SDK session id captured: ${sessionId}`, { phase: 'session-id', sessionId });
          writeResultMetadata({ session_id: sessionId });
        },
        log: (msg) => log('WARN', msg),
      })) {
        lastActivityTime = Date.now();

        switch (message.type) {
          case 'assistant': {
            turnCount++;
            const assistantMsg = message as unknown as { type: string; message: { content: Array<Record<string, unknown>> } };
            const turnText = logMessage(assistantMsg, turnCount);
            fullResponse += turnText;
            lastTurnText = turnText;
            // Stream turn to Check Run (no-op when streamer is null)
            if (checkRunStreamer) {
              const codexUsage = codexEventWatcher.getTotalUsage();
              checkRunStreamer.onTurn({
                turn: turnCount,
                content: assistantMsg.message.content as Array<{ name?: string; input?: Record<string, unknown>; text?: string }>,
                codexCostUsd: computeCodexCostUsd(codexUsage.inputTokens, codexUsage.outputTokens),
              });
            }
            // Publish intentional explanations as well as technical activity.
            if (activeLiveComment) {
              activeLiveComment.setExplanation(assistantText(assistantMsg.message.content));
              for (const block of assistantMsg.message.content) {
                if (block.type === 'tool_use' && typeof block.name === 'string') {
                  const inputPreview = JSON.stringify(block.input ?? {}).slice(0, 80);
                  activeLiveComment.appendActivity(`turn ${turnCount}  ${block.name}  ${inputPreview}`);
                }
              }
            }
            await applyAuthWatchdog(turnText);
            break;
          }

          // Issue #4369: tool results are where a failed `git push` / `gh pr
          // create` actually surfaces — the assistant's own prose may never
          // mention the 401. Not previously handled at all, which is precisely
          // why the auth failure was invisible to the worker.
          case 'user': {
            const userMsg = message as unknown as {
              message?: { content?: unknown };
            };
            const content = userMsg.message?.content;
            if (typeof content === 'string') {
              await applyAuthWatchdog(content);
            } else if (Array.isArray(content)) {
              for (const block of content) {
                const b = block as { type?: string; content?: unknown };
                if (b.type !== 'tool_result') continue;
                // tool_result content is either a bare string or an array of
                // {type:'text', text}. Flatten both to one string.
                const raw = b.content;
                const text = typeof raw === 'string'
                  ? raw
                  : Array.isArray(raw)
                    ? raw
                        .map((part) => (part as { text?: string })?.text ?? '')
                        .join('\n')
                    : '';
                await applyAuthWatchdog(text);
              }
            }
            break;
          }

          case 'result': {
            const res = message as { subtype?: string; total_cost_usd?: number; num_turns?: number; duration_ms?: number };
            if (res.subtype === 'success') {
              const msg = `✅ Query completed — ${res.num_turns} turns, $${res.total_cost_usd?.toFixed(4) || '?'}, ${((res.duration_ms || 0) / 1000).toFixed(1)}s`;
              console.log(msg);
              log('INFO', msg, { phase: 'result', subtype: res.subtype, cost: res.total_cost_usd, turns: res.num_turns });
            } else {
              const msg = `⚠️  Query ended: ${res.subtype}`;
              console.log(msg);
              log('WARN', msg, { phase: 'result', subtype: res.subtype });
            }
            // Persist result metadata so entrypoint.py can distinguish a genuine
            // "no changes needed" verdict (>0 tokens burned) from an infra
            // failure where the model call never succeeded ($0.0000 / 1 turn —
            // e.g. Bedrock AccessDenied, sigv4 403, throttling). The SDK returns
            // gracefully in that case, so without this signal the entrypoint
            // would report a fake success (issue #2883). Best-effort: mirrors
            // the /tmp/adp-check-run-final.md pattern; never throws.
            //
            // Issue #4186: merged rather than overwritten so the session id
            // written mid-stream survives this write.
            writeResultMetadata({
              subtype: res.subtype ?? null,
              total_cost_usd: res.total_cost_usd ?? null,
              num_turns: res.num_turns ?? null,
            });
            // Flush final transcript to Check Run before breaking the loop
            if (checkRunStreamer) {
              const codexUsage = codexEventWatcher.getTotalUsage();
              checkRunStreamer.onResult({
                costUsd: res.total_cost_usd,
                codexCostUsd: computeCodexCostUsd(codexUsage.inputTokens, codexUsage.outputTokens),
                turns: res.num_turns,
                durationMs: res.duration_ms,
              });
            }

            // The 'result' message signals the query is done.  Break out of
            // the for-await loop so the finally block runs, clearing the
            // heartbeat and starting the force-exit timer.  Without this,
            // the loop waits forever for the next message that never comes
            // (see issue #319).
            queryCompleted = true;
            queryCompletedTime = Date.now();
            log('INFO', 'Breaking out of message loop after result message');
            break queryLoop;              // eslint-disable-line no-labels
          }

          case 'tool_progress': {
            const tp = message as { tool_name: string; elapsed_time_seconds: number };
            console.log(`⏳ Tool running: ${tp.tool_name} (${tp.elapsed_time_seconds}s elapsed)`);
            // Keep mid-turn polling alive for long-running tools
            if (checkRunStreamer) {
              checkRunStreamer.onToolProgress(tp.tool_name);
            }
            break;
          }

          case 'system': {
            const sys = message as { subtype: string; model?: string; tools?: string[] };
            if (sys.subtype === 'init') {
              console.log(`🔧 Session init — model: ${sys.model}, tools: [${(sys.tools || []).join(', ')}]`);
              log('INFO', 'Session initialized', { model: sys.model, tools: sys.tools });
            }
            break;
          }
        }
      }
    } finally {
      clearInterval(heartbeat);
      // Stop the Codex event watcher before the streamer so no late poll can
      // forward into a destroyed streamer (issue #2884).
      codexEventWatcher.dispose();
      // Clean up the Check Run streamer timers
      if (checkRunStreamer) {
        checkRunStreamer.destroy();
      }
      // Safety net: if the process doesn't exit within 30s after query completion,
      // force exit. This handles cases where session.close() doesn't kill all
      // child processes (e.g., kubectl port-forward, background bash).
      const forceExitTimer = setTimeout(() => {
        console.log('⚠️  Force exit — process did not terminate within 30s after query completion');
        process.exit(0);
      }, 30_000);
      forceExitTimer.unref(); // Don't keep the event loop alive just for this timer
    }

    log('INFO', 'Agent execution complete', { turns: turnCount });

    // Log skill usage summary
    logSkillSummary();

    return lastTurnText || fullResponse.slice(-3000) || 'Task completed but no response returned.';
  } catch (error) {
    const err = error as Error;
    log('ERROR', 'Agent execution failed', { error: err.message });
    // Issue #4187: a budget stop is a distinct outcome, not a generic failure.
    // The gateway already returns a non-retryable 402 with a `scope`
    // discriminator, and resilientQuery correctly refuses to retry it — but the
    // signal died here, at the process boundary, so the run was recorded as
    // "failed" with a stack trace and an operator could not tell an
    // out-of-budget stop from a crash. Persisting it lets entrypoint.py record
    // `budget_stopped` + a stop reason instead.
    const budgetStop = detectBudgetStop(err);
    if (budgetStop) {
      log('WARN', 'Run stopped by a spend cap', budgetStop);
      writeResultMetadata({ budget_stopped: true, stop_reason: budgetStop.stopReason });
    }
    throw error;
  }
}

// ============================================================================
// Main
// ============================================================================

/**
 * Strip secrets, keys, tokens, and other sensitive data from text
 * before writing to agent memory (adp branch).
 */
function sanitizeMemory(text: string): string {
  const patterns = [
    /(?:AKIA|ASIA)[A-Z0-9]{16}/g,                          // AWS access key IDs
    /[A-Za-z0-9/+=]{40}/g,                                  // AWS secret keys (40-char base64)
    /ghp_[A-Za-z0-9]{36,}/g,                                // GitHub PATs
    /ghs_[A-Za-z0-9]{36,}/g,                                // GitHub App installation tokens
    /ghu_[A-Za-z0-9]{36,}/g,                                // GitHub user-to-server tokens
    /-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----/g,  // PEM keys
    /eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/g,  // JWTs
    /sk-[A-Za-z0-9]{32,}/g,                                 // OpenAI/Anthropic API keys
    /xox[bpras]-[A-Za-z0-9-]{10,}/g,                        // Slack tokens
    /(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['"][^'"]{8,}['"]/gi,  // key=value secrets
    /(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}/gi,  // unquoted secrets
  ];
  let sanitized = text;
  for (const pattern of patterns) {
    sanitized = sanitized.replace(pattern, '[REDACTED]');
  }
  return sanitized;
}

/**
 * Build the spill store for this run (Issue #4179).
 *
 * The workspace directory is the authoritative destination: `Read` is in the
 * allowlist above, so a path under CWD is a locator the model can always act
 * on. The S3 leg is strictly best-effort durability so the payload outlives the
 * pod — it is decoupled from the locator on purpose, because the worker's
 * bucket configuration is known-unreliable (#4184). Spilling must work with the
 * S3 leg entirely absent.
 */
function buildWorkerSpillStore(): TmpSpillStore {
  const bucket = process.env.AGENT_RUN_LOGS_BUCKET || '';

  const uploadToS3 = bucket
    ? async (key: string, body: string): Promise<void> => {
        const { S3Client, PutObjectCommand } = await import('@aws-sdk/client-s3');
        const s3 = new S3Client({ region: AWS_REGION, credentials: workerAwsCredentials() });
        // Same run-scoped prefix shape as the transcript upload in
        // agent-worker-image/entrypoint.py — keeps spills beside the run they
        // came from, and inherits that prefix's scoping rather than inventing
        // a new shared location.
        await s3.send(new PutObjectCommand({
          Bucket: bucket,
          Key: `${AGENT_TYPE}/${REPO_OWNER}/${REPO_NAME}/issue-${ISSUE_NUMBER}/spill/${key}`,
          Body: body,
          ContentType: 'text/plain',
        }));
      }
    : undefined;

  return new TmpSpillStore(CWD, {
    uploadToS3,
    log: (msg) => log('WARN', msg),
  });
}

/**
 * Upload uncommitted/unpushed git changes to S3 as fallback when git push fails.
 */
async function uploadGitChangesToS3(): Promise<void> {
  try {
    const { execSync } = await import('child_process');
    const { S3Client, PutObjectCommand } = await import('@aws-sdk/client-s3');
    const fs = await import('fs');
    const path = await import('path');

    // Check if there are any changes (committed but not pushed, or uncommitted)
    const status = execSync('git status --porcelain', { cwd: CWD, encoding: 'utf-8' }).trim();
    const unpushed = execSync('git log --oneline origin/main..HEAD 2>/dev/null || echo ""', { cwd: CWD, encoding: 'utf-8' }).trim();

    if (!status && !unpushed) {
      log('INFO', 'No git changes to backup to S3');
      return;
    }

    log('INFO', `Git changes detected — uploading to S3 fallback (${status ? 'uncommitted' : 'unpushed'})`);

    // Create a tar of changed files
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const tarFile = `/tmp/git-changes-${ISSUE_NUMBER}-${timestamp}.tar.gz`;

    // Get list of changed files
    const changedFiles = execSync(
      'git diff --name-only HEAD 2>/dev/null; git diff --name-only --cached 2>/dev/null; git diff --name-only origin/main..HEAD 2>/dev/null',
      { cwd: CWD, encoding: 'utf-8' }
    ).trim().split('\n').filter(f => f.length > 0);

    const uniqueFiles = [...new Set(changedFiles)].filter(f => {
      try { fs.statSync(path.join(CWD, f)); return true; } catch { return false; }
    });

    if (uniqueFiles.length === 0) {
      log('INFO', 'No changed files to backup');
      return;
    }

    // Tar the changed files
    execSync(`tar czf ${tarFile} ${uniqueFiles.join(' ')}`, { cwd: CWD, stdio: 'pipe' });

    // Upload to S3.
    // Issue #4184: this is the one fallback site that loses IRREPLACEABLE work —
    // it preserves uncommitted changes after `git push` already failed, i.e. the
    // entire output of a run that may have burned hours of model time. It was
    // silently AccessDenied on every occurrence. When the bucket is unconfigured,
    // say so on stdout too: a log line the operator never reads is not an alert.
    const bucket = resolveFallbackBucket(msg => log('ERROR', msg));
    if (!bucket) {
      console.error(
        `❌ Git push failed and AGENT_FALLBACK_BUCKET is unset — ` +
          `${uniqueFiles.length} changed files could NOT be preserved.`
      );
      try { fs.unlinkSync(tarFile); } catch {}
      return;
    }
    const s3 = new S3Client({ region: workerAwsRegion(), credentials: workerAwsCredentials() });
    const key = buildFallbackKey(ISSUE_NUMBER, 'git-changes', 'tar.gz');

    const fileContent = fs.readFileSync(tarFile);
    await s3.send(new PutObjectCommand({
      Bucket: bucket,
      Key: key,
      Body: fileContent,
      ContentType: 'application/gzip',
    }));

    const uri = `s3://${bucket}/${key}`;
    log('INFO', `Git changes backed up to ${uri} (${uniqueFiles.length} files)`);
    console.log(`📦 Git push failed — ${uniqueFiles.length} changed files saved to ${uri}`);

    // Also upload a manifest of what's in the tar
    const manifest = `# Git Changes Backup\nIssue: #${ISSUE_NUMBER}\nTimestamp: ${timestamp}\nFiles:\n${uniqueFiles.map(f => '- ' + f).join('\n')}\n`;
    await s3.send(new PutObjectCommand({
      Bucket: bucket,
      Key: key.replace('.tar.gz', '-manifest.md'),
      Body: manifest,
      ContentType: 'text/markdown',
    }));

    // Cleanup
    try { fs.unlinkSync(tarFile); } catch {}
  } catch (err) {
    log('WARN', `S3 git fallback failed: ${(err as Error).message}`);
  }
}

/**
 * The instant a pause must be over by — Issue #3961.
 *
 * The control credential's own expiry, not a separately-computed pod deadline.
 * The entrypoint derives that expiry from `ADP_POD_DEADLINE_SECONDS` — the very
 * variable rendered into `activeDeadlineSeconds` — so it already tracks the
 * wall-clock limit Kubernetes enforces, and reading it here means the two cannot
 * drift apart through a second calculation. It is also the tighter and more
 * honest bound: past that instant nobody can send a resume, so a pause held
 * beyond it could only ever end by expiry.
 *
 * `null` when unset or unparseable, which the gate reads as "unbounded" and
 * therefore allows the default 30-minute pause. That is the right failure
 * direction: the alternative — treating an absent deadline as zero remaining —
 * would refuse every pause on any run whose entrypoint did not export it.
 */
function controlDeadlineAt(): number | null {
  const raw = (process.env.ADP_CONTROL_TOKEN_EXPIRES_AT || '').trim();
  if (!raw) return null;
  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * Apply an accepted control command to the running agent — Issue #3961.
 *
 * The missing half of the control channel until now: S1 built the journal and
 * S3 built the adapter, but nothing drained one into the other, so an enabled
 * verb would have answered 202 and then done nothing at all.
 *
 * Exported and parameterised rather than closed over module state so the mapping
 * from outcome to journal status is testable without a running agent — that
 * mapping is the entire operator-visible contract of a pause, and it is the part
 * that must not be able to say `applied` for a pause that did not take effect.
 *
 * The three pause outcomes map to three different journal statuses on purpose:
 *
 * - `confirmed` → `applied` + phase `paused`. The barrier is closed and admitted
 *   work has drained, so "Paused" is a claim about the world.
 * - `requested` → the command stays **pending** and the phase becomes
 *   `pause_requested`. Not `applied`, because nothing has settled yet; not
 *   `rejected`, because admission *is* closed and the pause may still confirm.
 *   Leaving it pending is what lets the dashboard show "pausing…" honestly, and a
 *   later resume settles it as `cancelled`.
 * - `unavailable` → `rejected` with the gate's reason. The one thing that must
 *   never happen is this outcome rendering as a pause.
 */

async function main(): Promise<void> {
  console.log('');
  console.log('═'.repeat(60));
  console.log(`  @agent-${AGENT_TYPE} - Starting Work`);
  console.log('═'.repeat(60));
  console.log('');

  await initCloudWatch();

  // Initialize token refresh for long-running tasks (tokens expire after 1 hour)
  const appId = process.env.GH_APP_ID || '';
  const appKey = process.env.GH_APP_PRIVATE_KEY || process.env.GH_APP_KEY || '';
  const repoOwner = process.env.REPO_OWNER || '';
  // Issue #4272: in broker mode there is no private key in this process — the
  // gateway gatekeeper mints. The predicate MUST NOT require appKey then, or the
  // token manager never initialises, no refresh is ever scheduled, and the run
  // dies at the 1-hour mark with a 401 while git/gh degrade quietly.
  const brokerMode = isBrokerEnabled();

  // canInitTokenManager() rather than a hand-written predicate: this decision is
  // tested once in token-refresh.ts. A local copy here is what silently goes
  // false when the key stops being exported.
  if (canInitTokenManager()) {
    initTokenManager({
      appId,
      privateKey: brokerMode ? undefined : appKey,
      brokerMode,
      owner: repoOwner,
      repo: REPO_NAME,
      // Authoritative installation id for this run's target org (exported by
      // entrypoint.py). Prevents resolving the wrong installation when the App
      // is installed on many tenants.
      installationId: process.env.GH_APP_INSTALLATION_ID || undefined,
      workDir: CWD,
      // Issue #4369: 20 min for BOTH modes. A gatekeeper round-trip can fail and
      // need retrying, but a local mint can too, and the old 15-min local value
      // was half of the reason >1h runs died: see the interval note below.
      refreshThresholdMs: TOKEN_REFRESH_THRESHOLD_MS,
    });

    adoptBootstrapToken();

    // Issue #4369: tick every 5 min, not 30. `getToken()` is a no-op unless the
    // token is inside the refresh threshold, so a short interval costs nothing —
    // but a 30-min interval against a ~60-min token and a 15-min threshold never
    // refreshed AT ALL: the ticks landed at t≈30 (30 min left → skip) and t≈60
    // (already expiring), straddling the window entirely. Every call in the last
    // few minutes then 401ed for the rest of the run. 5 min / 20 min puts at
    // least three ticks inside the window, so a re-mint always lands early.
    const tokenRefreshInterval = setInterval(async () => {
      try {
        const before = getTokenStatus()?.refreshedAt?.getTime();
        await getToken();
        const status = getTokenStatus();
        // Only claim a refresh when a NEW token was actually minted. This used to
        // log unconditionally, so the logs asserted the refresh was healthy on
        // every tick while the token silently expired.
        if (status && status.refreshedAt.getTime() !== before) {
          log('INFO', 'Token refreshed proactively', {
            expiresInMin: Math.round(status.expiresIn / 60000),
          });
        }
      } catch (err) {
        log('WARN', `Token refresh failed: ${(err as Error).message}`);
      }
    }, TOKEN_REFRESH_INTERVAL_MS);

    // Clean up interval on exit
    process.on('exit', () => clearInterval(tokenRefreshInterval));
    // Also store reference for cleanup in finally block
    (global as any).__tokenRefreshInterval = tokenRefreshInterval;

    log('INFO', `Token manager initialized with ${TOKEN_REFRESH_INTERVAL_MS / 60000}-minute refresh interval`);

    // Write the initial token to file BEFORE the SDK query starts, so that
    // GIT_ASKPASS and the gh wrapper can read it from day one (issue #1469).
    try {
      const initialToken = await getToken();
      writeTokenFile(initialToken);
      log('INFO', 'Initial token written to token file for SDK subprocess');
    } catch (err) {
      if (brokerMode) throw err;
      log('WARN', `Initial token file write failed: ${(err as Error).message}`);
    }
  } else if (isMediatedRun()) {
    // #5223: having no token is this run's correct steady state, not a
    // misconfiguration. Must be checked BEFORE the brokerMode throw below —
    // mediated runs in the authority cohort have brokerMode true, so falling
    // through would abort every mediated run at startup.
    log('INFO', 'Mediated GitHub operations: no token to refresh; writes go through the gateway');
  } else if (process.env.ADP_TOKEN_MODE !== 'pat') {
    if (brokerMode) throw new Error('Brokered GitHub renewal configuration unavailable');
    log('WARN', 'GitHub App credentials not available — token refresh disabled. Token will expire after ~1 hour.');
  }

  // Initialize Beads if available (shared state with PM)
  let beadsTaskId: string | null = null;
  let beadsAvailable = false;

  if (BEADS_ENABLED) {
    configureBeads({
      enabled: true,
      s3Bucket: BEADS_S3_BUCKET,
      s3Region: BEADS_S3_REGION,
      s3Path: BEADS_S3_PATH,
      syncOnStart: true,
      syncOnComplete: true,
      fallbackToGitHub: true,
    });
    setBeadsLogger(log);

    const bdAvailable = await isBeadsAvailable();
    const bdInitialized = await isBeadsInitialized(CWD);
    log('INFO', `Beads check: available=${bdAvailable}, initialized=${bdInitialized}, cwd=${CWD}`);

    if (bdAvailable && bdInitialized) {
      beadsAvailable = true;
      log('INFO', 'Beads state management active');

      // Pull latest state
      try {
        await syncPull(CWD);
      } catch (err) {
        log('WARN', `Beads sync pull failed: ${(err as Error).message}`);
      }
    } else {
      log('INFO', 'Beads not available, using GitHub Projects only');
    }
  }

  // Get bd prime context for AI-optimized workflow guidance
  const beadsPrimeContext = await getBeadsPrimeContext(CWD);

  // Initialize agent memory system
  configureMemory({
    cwd: CWD,
    agentType: AGENT_TYPE,
    issueNumber: ISSUE_NUMBER,
    log,
  });

  let memoryContext = '';
  let detectedComponent = 'general';
  let agentSucceeded = false;
  let agentResult = '';

  // Issue #3960: the in-pod control listener. Declared outside the try so the
  // finally block can close the port on every exit path — including a thrown
  // error — rather than only on the success path.
  let controlListener: ControlListener | null = null;
  // Issue #3962: the harness adapter, constructed unconditionally and outside the
  // try for the same reason. Constructing it costs nothing and starts nothing —
  // it holds an attempt registry and no transport until `resilientQuery` attaches
  // one — so it is not gated on the listener having started. That independence is
  // deliberate: the adapter is the object the retry-safety rules live in, and
  // making it conditional on a control listener would tie the correctness of a
  // retry to whether an operator had enabled an intervention channel.
  //
  // Issue #3961: the adapter now carries the pause barrier. The gate is the
  // object the `PreToolUse` hook consults, so it must exist before the query
  // options are built — which is why it is constructed here and not inside the
  // query setup. Its deadline comes from the pod's own remaining budget, so a
  // pause can never outlive the run it is pausing.
  //
  // The observer is passed to BOTH the gate (as its background-work probe) and
  // the hooks (which feed it) so there is exactly one answer to "is anything
  // still running behind the tools that finished?". Two instances would let the
  // gate consult a probe nobody was updating, and an un-updated probe answers `0`
  // — a fabricated quiescence claim, which is the single failure this whole story
  // exists to prevent.
  const backgroundWork = new ClaudeBackgroundWorkObserver();
  const pauseGate = new PauseGate({
    deadlineAt: controlDeadlineAt,
    backgroundWorkProbe: () => backgroundWork.count(),
    log: (msg) => log('DEBUG', msg),
  });
  const controlAdapter = new ClaudeControlAdapter({
    log: (msg) => log('DEBUG', msg),
    pauseGate,
    backgroundWorkObserver: backgroundWork,
  });
  try {
    // Started here, after config resolution and before the SDK query, so a
    // state read is answerable for the whole life of the run. Everything the
    // listener needs was placed in this process's env by the entrypoint, which
    // only does so when the flag is on and registration succeeded — so an
    // unregistered listener cannot exist.
    const controlStore = new ControlStateStore({
      generation: Number.parseInt(process.env.ADP_CONTROL_GENERATION || '1', 10) || 1,
      // Issue #3962: derived from the adapter rather than declared here, so the
      // wire cannot be enabled without the transport behind it — there is only one
      // place left to say yes. Issue #3961 is what that buys: `pause`/`resume` are
      // now in the ADP set and this adapter carries a barrier, so the intersection
      // yields them and the listener answers 202 instead of 501. `steer`/`abort`
      // stay out on both sides.
      supportedActions: listenerActionsFor(controlAdapter),
      revalidate: revalidateQueuedCommand,
    });
    // Issue #3961: mirror every gate-initiated transition — the admitted-tool
    // count (replacing S1's permanent `null`, and only for a run that actually has
    // a gate, because `0` is a quiescence claim only the gate may make), plus the
    // confirm/unavailable/expiry edges that change admission with no command behind
    // them. Without the latter the store can report `paused` while tools run.
    bindGateTransitionsToStore({ gate: pauseGate, store: controlStore, log });
    const listener = new ControlListener({
      bindAddress: process.env.ADP_CONTROL_BIND_ADDRESS || '',
      port: Number.parseInt(process.env.ADP_CONTROL_PORT || '0', 10),
      token: process.env.ADP_CONTROL_TOKEN || '',
      tokenExpiresAt: process.env.ADP_CONTROL_TOKEN_EXPIRES_AT || '',
      credentialFile: process.env.ADP_CONTROL_CREDENTIAL_FILE,
      generation: Number.parseInt(process.env.ADP_CONTROL_GENERATION || '1', 10) || 1,
      store: controlStore,
      // Issue #3961: the seam that makes an accepted command actually happen.
      // Without it every 202 was a promise nothing kept.
      executor: (action, commandId) =>
        applyControlCommand({ action, commandId, adapter: controlAdapter, store: controlStore, log }),
      // Issue #5028: this run's own identity and the gateway's public verification
      // keys. Both are placed here by the entrypoint. An absent key map means
      // live-control commands are refused — the read paths still work, and no verb
      // is implemented yet, so that is the expected state today.
      runId: process.env.ADP_CONTROL_RUN_ID || '',
      envelopeKeys: parseVerificationKeys(process.env.ADP_CONTROL_ENVELOPE_KEYS),
      envelopeKeysFile: process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE,
      logger: (level, message, context) => log(level.toUpperCase(), message, context),
    });
    const outcome = await listener.start();
    if (outcome.started) {
      controlListener = listener;
      // Issue #3961: publishing the runtime here — and only here — is what
      // installs the admission barrier into the query options below. Gating it on
      // a *started* listener rather than on the gate merely existing keeps two
      // properties. A run nobody can send a command to gets the pre-#3961 hook set
      // exactly, so the barrier cannot introduce a `PreToolUse` failure mode on
      // the path every ordinary agent takes. And the capability claim stays
      // truthful in the only direction that matters: pause is advertised where the
      // mechanism is actually in place.
      activeControlRuntime = { adapter: controlAdapter, gate: pauseGate };
      log('INFO', `Control listener started on port ${outcome.port}`);
    } else if (outcome.reason !== 'disabled') {
      // A failure to start is logged at WARN and the run continues: control is an
      // add-on, and refusing to work without it would make an intervention
      // channel a new way for ordinary runs to die. 'disabled' is silent because
      // it is the normal state for every ordinary workload.
      log('WARN', `Control listener unavailable (${outcome.reason}): ${outcome.detail ?? ''}`);
    }
  } catch (err) {
    log('WARN', `Control listener setup failed (non-blocking): ${(err as Error).message}`);
  }

  try {
    await ensureAdpBranch();
  } catch (err) {
    log('WARN', `Memory: failed to ensure adp branch: ${(err as Error).message}`);
  }

  try {
    // Get issue details
    const issue = await getIssue();
    log('INFO', `Processing issue: ${issue.title}`);

    // Load agent memory context from adp branch
    try {
      detectedComponent = detectComponent(issue.labels, issue.body);
      const componentCtx = await readComponentContext(detectedComponent);
      const agentCtx = await readAgentContext(AGENT_TYPE);
      memoryContext = formatContextForPrompt(componentCtx, agentCtx, detectedComponent, AGENT_TYPE);
      if (memoryContext) {
        log('INFO', `Loaded memory context: ${componentCtx.length} component records, ${agentCtx.length} agent records`);
      }
    } catch (err) {
      log('WARN', `Memory: failed to load context: ${(err as Error).message}`);
    }

    // Fetch existing comments to include in context
    const existingComments = await getIssueComments(20);
    const commentsContext = existingComments.length > 0
      ? existingComments.map((c, i) => `### Comment ${i + 1} (by ${c.author} at ${c.createdAt}):\n${c.body}`).join('\n\n---\n\n')
      : '';
    log('INFO', `Found ${existingComments.length} existing comments to include in context`);

    // AIDLC Presence — synthetic HUMAN_TURN on gate resume (Issue #3232).
    // Must run BEFORE the SDK query starts so that mint-presence.ts sees the
    // event when /aidlc is invoked. Best-effort: never blocks startup.
    if (AIDLC_ENABLED && existingComments.length > 0) {
      try {
        const pendingStage = findPresenceGateStage(CWD);
        if (pendingStage) {
          const triggerComment = extractGateAnswerComment(
            existingComments,
            pendingStage,
            REPO_OWNER,
            REPO_NAME,
            ISSUE_NUMBER,
          );
          const presenceResult = mintSyntheticPresence(
            { cwd: CWD, log },
            triggerComment,
          );
          if (presenceResult.written) {
            log('INFO', 'AIDLC presence: synthetic HUMAN_TURN written', {
              stage: presenceResult.stage,
              author: triggerComment?.author,
            });
          }
        }
      } catch (presenceErr) {
        log('WARN', `AIDLC presence failed (non-blocking): ${(presenceErr as Error).message}`);
      }
    }

    // Find the main/parent issue
    const mainIssueNumber = findMainIssue(issue.body);
    if (mainIssueNumber) {
      log('INFO', `Found main issue: #${mainIssueNumber}`);
    }

    // Claim task in Beads (if available)
    if (beadsAvailable) {
      try {
        const workResult = await beadsStartWork(
          issue.number,
          `@agent-${AGENT_TYPE}`,
          CWD
        );
        if (workResult) {
          beadsTaskId = workResult.task.id;
          log('INFO', `Claimed Beads task: ${beadsTaskId}`);
        }
      } catch (err) {
        log('WARN', `Could not claim Beads task: ${(err as Error).message}`);
      }
    }

    // Initialize live status comment (edit-in-place progress)
    const token = process.env.GH_APP_TOKEN || process.env.GITHUB_TOKEN || GITHUB_TOKEN;
    activeLiveComment = new LiveStatusComment(createWorkerStages(AGENT_TYPE), {
      owner: REPO_OWNER,
      repo: REPO_NAME,
      issueNumber: parseInt(ISSUE_NUMBER),
      token,
      log,
    });

    try {
      await activeLiveComment.post();
      log('INFO', `Live status comment posted: ${activeLiveComment.getCommentId()}`);
    } catch (err) {
      log('WARN', `Could not post live status comment: ${(err as Error).message}`);
    }

    // Stage 0: Setup — mark complete (we're past setup at this point)
    activeLiveComment.transition(0, 'complete', 'Environment ready');

    // Post start notification to main issue
    await postToMainIssue(mainIssueNumber, `## @agent-${AGENT_TYPE} Started

**Task**: #${issue.number} - ${issue.title}
**Status**: In Progress
**Started**: ${new Date().toISOString()}
${beadsTaskId ? `**Beads ID**: ${beadsTaskId}` : ''}

Working on this task...`);

    // NOTE: Project board status is already set to "In Progress" by the workflow
    // using the update-board-status action before the agent runs.

    // Stage 1: Analyze — starting agent execution
    activeLiveComment.transition(1, 'in_progress', 'Running agent');

    // Run the agent
    const result = await runAgent(issue, mainIssueNumber, beadsPrimeContext, commentsContext, memoryContext);
    agentResult = result || '';

    // #4450: The SDK degrades a mid-stream connection drop into a *successful*
    // result whose final assistant text is a "Connection closed mid-response"
    // sentinel (subtype 'success', normal turns/cost — so the #2883 $0/1-turn
    // guard misses it). Trusting it posts a fake "Done / no changes needed" and
    // silently abandons the work. Route it through the catch block instead so
    // the run reports Failed and can be re-dispatched honestly.
    if (isTruncatedStreamResult(agentResult)) {
      throw new Error(
        'Model response stream was truncated (connection closed mid-response); ' +
        'treating run as failed rather than reporting a false completion',
      );
    }

    // The runtime observes execution ending, not implementation/test/PR outcomes.
    activeLiveComment.transition(1, 'complete');

    // Complete task in Beads (if claimed)
    if (beadsAvailable && beadsTaskId) {
      try {
        await beadsCompleteWork(beadsTaskId, 'Completed successfully', CWD);
        log('INFO', `Beads task ${beadsTaskId} marked complete`);
      } catch (err) {
        log('WARN', `Could not complete Beads task: ${(err as Error).message}`);
      }
    }

    // AIDLC Gate Enforcement (Issue #3231) — deterministic commit + gate comment.
    // Runs only on AIDLC-flagged workspaces. Best-effort: never blocks finalization.
    if (AIDLC_ENABLED) {
      try {
        const enforceResult = await enforceAidlcGate({
          cwd: CWD,
          issueNumber: ISSUE_NUMBER,
          repoOwner: REPO_OWNER,
          repoName: REPO_NAME,
          log,
          execCommand,
          postComment,
        });
        if (enforceResult.committed || enforceResult.gateCommentPosted) {
          log('INFO', 'AIDLC gate enforcement acted', {
            committed: enforceResult.committed,
            gateCommentPosted: enforceResult.gateCommentPosted,
            stage: enforceResult.stage,
          });
        }
      } catch (enforceErr) {
        log('WARN', `AIDLC gate enforcement failed (non-blocking): ${(enforceErr as Error).message}`);
      }
    }

    // NOTE: Don't update project board status to Done here.
    // Status will be set to Done automatically by GitHub project automation
    // when the PR is merged and the issue is closed.

    // Publish one full outcome without cutting away qualifications or blockers.
    const outcome = result || 'The run ended without an outcome report. Task completion has not been verified.';
    let outcomeUrl: string | undefined;
    try {
      await activeLiveComment.finalizeSuccess({ details: outcome });
      outcomeUrl = activeLiveComment.getCommentUrl() || undefined;
    } catch (err) {
      log('WARN', `Could not finalize live comment: ${(err as Error).message}`);
    }
    if (outcomeUrl) {
      writeResultMetadata({ outcome_comment_url: outcomeUrl });
      if (mainIssueNumber && mainIssueNumber !== issue.number) {
        await postToMainIssue(mainIssueNumber,
          `Agent run ended for **${issue.title}** (#${issue.number}). [Outcome, remaining work and next action](${outcomeUrl}).`);
      }
    } else {
      await postToMainIssue(mainIssueNumber, outcome);
    }

    log('INFO', 'Work completed successfully');
    agentSucceeded = true;

  } catch (error) {
    const err = error as Error;
    log('ERROR', `Agent failed: ${err.message}`);
    if (activeLiveComment) {
      await activeLiveComment.finalizeFailure({
        error: err.message,
        durationMs: activeLiveComment.getDurationMs(),
      }).catch(finalizeErr => log('WARN', `Could not finalize live comment: ${finalizeErr.message}`));
    }


    // Report failure to Beads (if task was claimed)
    if (beadsAvailable && beadsTaskId) {
      try {
        await beadsReportFailure(beadsTaskId, err.message, CWD);
        log('INFO', `Reported failure to Beads for task ${beadsTaskId}`);
      } catch {
        log('WARN', 'Could not report failure to Beads');
      }
    }

    // Get issue for main issue reference
    try {
      const issue = await getIssue();
      const mainIssueNumber = findMainIssue(issue.body);

      await postToMainIssue(mainIssueNumber, `## @agent-${AGENT_TYPE} Failed

**Task**: #${issue.number} - ${issue.title}
**Status**: Failed
**Error**: ${err.message}
${beadsTaskId ? `**Beads ID**: ${beadsTaskId}` : ''}

Please check the workflow logs for details.`);
    } catch {
      // Can't even post error, just log
      log('ERROR', 'Could not post error comment');
    }

    throw error;
  } finally {
    // Write agent memory context to adp branch (best-effort, never blocks)
    try {
      const issue = await getIssue().catch(() => null);
      if (issue) {
        const component = detectedComponent || detectComponent(issue.labels, issue.body);
        const memStatus = agentSucceeded ? 'success' : 'failed';
        await writeComponentRecord(component, buildComponentRecord({
          issueNumber: ISSUE_NUMBER,
          issueTitle: issue.title,
          component,
          agentType: AGENT_TYPE,
          status: memStatus,
          summary: sanitizeMemory(agentResult || `Processed issue #${ISSUE_NUMBER}: ${issue.title}`).slice(0, 3000),
        }));
        await writeAgentRecord(AGENT_TYPE, buildAgentRecord({
          issueNumber: ISSUE_NUMBER,
          issueTitle: issue.title,
          agentType: AGENT_TYPE,
          component,
          status: memStatus,
          oneLiner: sanitizeMemory(agentResult ? agentResult.slice(0, 500) : `Worked on issue #${ISSUE_NUMBER}: ${issue.title}`),
        }));
      }
    } catch (memErr) {
      log('WARN', `Memory: failed to write context: ${(memErr as Error).message}`);
    }

    // Issue #1294: Experience-save post-task hook — persist learnings to
    // personal-context store (non-blocking, best-effort). Only fires on
    // success and when PERSONAL_CONTEXT_SAVE_ENABLED=true.
    if (agentSucceeded) {
      try {
        const pcIdentity = buildPersonalContextIdentity({
          cognito_sub: process.env.ADP_OWNER_SUB,
          tenant_id: process.env.ADP_TENANT_ID,
        });
        const pcHeaders = getPersonalContextHeaders(pcIdentity);
        await saveExperienceLearnings({
          agentOutput: agentResult,
          persona: AGENT_TYPE,
          identityHeaders: pcHeaders,
          taskContext: { issue: ISSUE_NUMBER, repo: `${REPO_OWNER}/${REPO_NAME}` },
          log,
        });
      } catch (expErr) {
        log('WARN', `[experience-save] Hook failed (non-blocking): ${(expErr as Error).message}`);
      }
    }

    // Issue #3960: close the control port before the process exits. Placed with
    // the other teardown rather than after it because `process.exit` below is
    // unconditional — anything past that line never runs. Awaited so the socket
    // is actually closed rather than merely asked to close, and wrapped because a
    // teardown throw here would mask the run's real outcome.
    if (controlListener) {
      try {
        await controlListener.stop();
        log('INFO', 'Control listener stopped');
      } catch (err) {
        log('WARN', `Control listener stop failed: ${(err as Error).message}`);
      }
    }

    // Issue #3962: dispose the adapter after the listener, not before. The
    // listener is what can still answer a request, and a request answered from a
    // disposed runtime would read the post-teardown state as though it were the
    // run's — so the surface closes first and the runtime it describes second.
    // Idempotent, and safe when no attempt was ever attached.
    try {
      await controlAdapter.dispose();
    } catch (err) {
      log('WARN', `Control adapter dispose failed: ${(err as Error).message}`);
    }

    clearInterval(cwFlushTimer);
    if ((global as any).__tokenRefreshInterval) {
      clearInterval((global as any).__tokenRefreshInterval);
    }
    await flushCloudWatch();

    // Exit explicitly to avoid hanging on unclosed handles (AWS SDK, etc)
    // If git push failed during agent execution, backup changes to S3
    await uploadGitChangesToS3();

    console.log('Agent cleanup complete, exiting');
    process.exit(agentSucceeded ? 0 : 1);
  }
}

main().catch((err) => {
  console.error('Fatal error in main:', err);
  process.exit(1);
});
