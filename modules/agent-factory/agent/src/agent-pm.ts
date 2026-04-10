/**
 * @agent-pm - AIDLC Workflow Orchestrator
 *
 * Flow:
 * 1. User tags issue with agent-pm label
 * 2. PM posts acknowledgment immediately
 * 3. PM assesses complexity and recommends AIDLC steps
 * 4. PM creates aidlc-docs/ structure with recommendations + questions
 * 5. PM creates project board (if needed) and reports status
 * 6. PM polls for user response (looks for commits with "AIDLC" in message)
 * 7. PM continues workflow until complete or 30-minute timeout
 */

import { resilientQuery } from './utils/resilientQuery';
import { CloudWatchLogsClient, PutLogEventsCommand, CreateLogStreamCommand } from '@aws-sdk/client-cloudwatch-logs';
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

// Reassessment module - modular and isolatable
import {
  configureReassessment,
  isReassessmentEnabled,
  gatherReassessmentContext,
  buildReassessPrompt,
  shouldReassess,
  fetchIssueComments,
  formatReassessmentComment,
  parseReassessmentResponse,
  buildReassessExecutionPrompt,
  formatCommandAcknowledgment,
  buildWorkflowProgress,
  ReassessmentContext,
  UserReassessmentChoice,
  AIDLCArtifacts,
} from './reassessment';

// Monitoring module - proactive agent tracking
import {
  configureMonitoring,
  isMonitoringEnabled,
  loadMonitoringState,
  saveMonitoringState,
  createInitialState,
  startOrResumeMonitoring,
  formatStatusUpdate,
  createSnapshot,
  handleCompletionWithAI,
  executeUserInstruction,
  analyzeStatusWithAI,
  handleQueryPM,
  MonitoringState,
  MonitoringCallbacks,
  MonitoringEvent,
  UserCommand,
  TrackedAgent,
  CompletionContext,
} from './monitoring';

// Beads module - distributed state management for agents
import {
  configureBeads,
  isBeadsEnabled,
  isBeadsAvailable,
  isBeadsInitialized,
  setupBeadsForADP,
  syncPull,
  syncPush,
  getReadyTasks,
  createProjectFromIssue,
  getProjectStatus,
  startWork,
  completeWork,
  setLogger as setBeadsLogger,
  BeadsTask,
  BeadsEpic,
} from './beads';

// Token refresh module - handles GitHub App token expiration (tokens expire after 1 hour)
import { refreshGitHubToken, saveToS3Fallback } from './utils/ghPost';
import {
  initTokenManager,
  getToken,
  needsRefresh,
  getTokenStatus,
  setToken,
  forceRefresh,
} from './token-refresh';

// ============================================================================
// Configuration
// ============================================================================

const REPO_OWNER = process.env.REPO_OWNER || '';
const REPO_NAME = process.env.REPO_NAME || '';
const ISSUE_NUMBER = process.env.ISSUE_NUMBER || '';
const GITHUB_TOKEN = process.env.GITHUB_TOKEN || '';
const GH_APP_TOKEN = process.env.GH_APP_TOKEN || '';
const CWD = process.env.WORK_DIR || process.cwd();
const MODEL = process.env.ANTHROPIC_MODEL || 'us.anthropic.claude-sonnet-4-20250514-v1:0';
const AWS_REGION = process.env.AWS_REGION || 'us-east-1';

// How long to wait for user response (in ms) - 15 minutes
// After timeout, agent proceeds with its own recommendations
const USER_RESPONSE_TIMEOUT_MS = 15 * 60 * 1000;
// How often to check for file changes (in ms) - 30 seconds
const POLL_INTERVAL_MS = 30 * 1000;

// Reassessment configuration - set to false to disable reassessment module
const REASSESSMENT_ENABLED = process.env.REASSESSMENT_ENABLED !== 'false';
configureReassessment({
  enabled: REASSESSMENT_ENABLED,
  maxCommentsToFetch: 50,
  maxChildIssuesToFetch: 100,
});

// Monitoring configuration - set to false to disable proactive monitoring
const MONITORING_ENABLED = process.env.MONITORING_ENABLED !== 'false';
const MONITORING_SESSION_TIMEOUT_MS = parseInt(process.env.MONITORING_SESSION_TIMEOUT_MS || '') || (2 * 60 * 60 * 1000); // 2 hours default
configureMonitoring({
  enabled: MONITORING_ENABLED,
  pollIntervalMs: 30 * 1000,           // 30 seconds
  sessionTimeoutMs: MONITORING_SESSION_TIMEOUT_MS,
  statusUpdateIntervalMs: 5 * 60 * 1000, // 5 minutes
  maxConsecutiveErrors: 5,
});

// Beads configuration - distributed state management
const BEADS_ENABLED = process.env.BEADS_ENABLED !== 'false';
const BEADS_S3_BUCKET = process.env.BEADS_S3_BUCKET || 'adp-agent-state';
const BEADS_S3_REGION = process.env.BEADS_S3_REGION || 'us-west-2';
const BEADS_S3_PATH = process.env.BEADS_S3_PATH || `beads/${REPO_NAME}`;
configureBeads({
  enabled: BEADS_ENABLED,
  s3Bucket: BEADS_S3_BUCKET,
  s3Region: BEADS_S3_REGION,
  s3Path: BEADS_S3_PATH,
  syncOnStart: true,
  syncOnComplete: true,
  fallbackToGitHub: true,
});
setBeadsLogger(log);

// Token refresh configuration - GitHub App tokens expire after 1 hour
const TOKEN_REFRESH_ENABLED = process.env.TOKEN_REFRESH_ENABLED === 'true';
const GH_APP_ID = process.env.GH_APP_ID || '';
const GH_APP_PRIVATE_KEY = process.env.GH_APP_PRIVATE_KEY || '';

if (TOKEN_REFRESH_ENABLED && GH_APP_ID && GH_APP_PRIVATE_KEY) {
  initTokenManager({
    appId: GH_APP_ID,
    privateKey: GH_APP_PRIVATE_KEY,
    owner: REPO_OWNER,
    repo: REPO_NAME,
    workDir: CWD,
    refreshThresholdMs: 15 * 60 * 1000, // Refresh 15 min before expiry
  });
  // Set the initial token (from workflow)
  if (GH_APP_TOKEN) {
    setToken(GH_APP_TOKEN, 60 * 60 * 1000); // Assume 1 hour expiry
  }
  console.log('[TokenRefresh] Initialized - tokens will auto-refresh before expiry');
} else if (TOKEN_REFRESH_ENABLED) {
  console.warn('[TokenRefresh] Enabled but missing GH_APP_ID or GH_APP_PRIVATE_KEY');
}

// Module-level variable for bd prime context (set in main after Beads init)
let beadsPrimeContext = '';

// Module-level variable for agent memory context (set in main after memory init)
let agentMemoryContext = '';

// ============================================================================
// Error Pattern Detection - Fail fast on repeated errors
// ============================================================================

interface ErrorPattern {
  pattern: string;
  count: number;
  firstSeen: Date;
  lastSeen: Date;
}

const errorPatterns: Map<string, ErrorPattern> = new Map();
const workflowStartTime = new Date();

// Store actual error messages for AI analysis
const recentErrors: Array<{ message: string; timestamp: Date; context?: string }> = [];

function categorizeError(errorMessage: string): string {
  const lowerError = errorMessage.toLowerCase();
  const patterns = ['401', 'bad credentials', 'rate limit', 'econnrefused', 'etimedout', 'fetch failed', 'socket hang up'];
  for (const pattern of patterns) {
    if (lowerError.includes(pattern)) {
      return pattern;
    }
  }
  return 'unknown';
}

function trackError(errorMessage: string, context?: string): void {
  const category = categorizeError(errorMessage);
  const existing = errorPatterns.get(category);
  const now = new Date();

  // Store full error for AI analysis
  recentErrors.push({ message: errorMessage, timestamp: now, context });
  // Keep only last 20 errors
  if (recentErrors.length > 20) {
    recentErrors.shift();
  }

  if (existing) {
    existing.count++;
    existing.lastSeen = now;
  } else {
    errorPatterns.set(category, {
      pattern: category,
      count: 1,
      firstSeen: now,
      lastSeen: now,
    });
  }
}

interface RecoveryPlan {
  canRecover: boolean;
  action: 'refresh_token' | 'wait_and_retry' | 'retry_with_backoff' | 'alternative_approach' | 'give_up';
  waitTimeMs?: number;
  alternativeCommand?: string;
  explanation: string;
}

async function analyzeAndPlanRecovery(): Promise<RecoveryPlan> {
  const workflowDuration = Math.floor((Date.now() - workflowStartTime.getTime()) / 60000);

  const errorSummary = Array.from(errorPatterns.entries())
    .map(([cat, p]) => `- "${cat}": ${p.count} times`)
    .join('\n');

  const recentErrorMessages = recentErrors
    .slice(-5)
    .map((e, i) => `${i + 1}. ${e.message.substring(0, 300)}`)
    .join('\n');

  const prompt = `You are an autonomous agent that needs to recover from errors and continue working.

## Context
- **Task**: Managing GitHub issue #${ISSUE_NUMBER} in ${REPO_OWNER}/${REPO_NAME}
- **Workflow Duration**: ${workflowDuration} minutes
- **Token Refresh Available**: ${TOKEN_REFRESH_ENABLED ? 'Yes' : 'No'}

## Error Summary
${errorSummary}

## Recent Errors
${recentErrorMessages}

## Your Task
1. **Diagnose** the root cause - use tools to verify:
   - \`gh auth status\` to check token validity
   - \`gh api rate_limit\` to check rate limits
   - Check network by testing \`gh issue view ${ISSUE_NUMBER}\`

2. **Take action** to recover:
   - If token expired: The system will auto-refresh, just report the issue
   - If rate limited: Wait the appropriate time
   - If network issue: Test connectivity and report
   - If command-specific: Try an alternative approach

3. **Report** your findings and decision in this JSON format:

\`\`\`json
{
  "canRecover": true,
  "action": "refresh_token|wait_and_retry|retry_with_backoff|alternative_approach|give_up",
  "waitTimeMs": 0,
  "explanation": "What you found and why this action"
}
\`\`\`

Rules:
- VERIFY before deciding - don't just guess from error messages
- If workflow > 55 min and seeing 401 errors → likely token expiry
- If rate limited → check actual reset time with gh api rate_limit
- Always prefer recovery over giving up
- Use tools to understand the actual state before deciding`;

  try {
    let response = '';
    for await (const event of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: MODEL,
          cwd: CWD,
          maxTurns: 10,
          allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch'],
          permissionMode: 'bypassPermissions',
        },
      },
      maxRetries: 2,
      baseDelayMs: 2000,
      log: (msg) => console.log(`[AI Recovery] ${msg}`),
    })) {
      if (event.type === 'assistant' && event.message?.content) {
        for (const block of event.message.content) {
          if (block.type === 'text') {
            response += block.text;
          }
        }
      }
    }

    // Parse JSON from response
    const jsonMatch = response.match(/```json\s*([\s\S]*?)\s*```/);
    if (jsonMatch) {
      const plan = JSON.parse(jsonMatch[1]) as RecoveryPlan;
      return plan;
    }

    // Try to parse as plain JSON
    const plainJson = response.match(/\{[\s\S]*"canRecover"[\s\S]*\}/);
    if (plainJson) {
      return JSON.parse(plainJson[0]) as RecoveryPlan;
    }

    throw new Error('Could not parse recovery plan');
  } catch (err) {
    // Fallback: make a simple decision based on error patterns
    console.log(`[AI Recovery] Fallback decision due to: ${(err as Error).message}`);

    const has401 = errorPatterns.has('401') || errorPatterns.has('bad credentials');
    const hasRateLimit = errorPatterns.has('rate limit');
    const workflowLong = workflowDuration > 55;

    if (has401 && workflowLong && TOKEN_REFRESH_ENABLED) {
      return {
        canRecover: true,
        action: 'refresh_token',
        explanation: 'Token likely expired, attempting refresh',
      };
    }

    if (hasRateLimit) {
      return {
        canRecover: true,
        action: 'wait_and_retry',
        waitTimeMs: 60000,
        explanation: 'Rate limited, waiting 60 seconds',
      };
    }

    const totalErrors = Array.from(errorPatterns.values()).reduce((sum, p) => sum + p.count, 0);
    if (totalErrors < 5) {
      return {
        canRecover: true,
        action: 'retry_with_backoff',
        explanation: 'Transient error, retrying with backoff',
      };
    }

    return {
      canRecover: false,
      action: 'give_up',
      explanation: 'Too many errors, cannot recover automatically',
    };
  }
}

async function executeRecovery(plan: RecoveryPlan): Promise<boolean> {
  console.log(`🔧 Recovery: ${plan.action} - ${plan.explanation}`);

  switch (plan.action) {
    case 'refresh_token': {
      if (!TOKEN_REFRESH_ENABLED) {
        console.log('   ❌ Token refresh not available');
        return false;
      }
      try {
        const newToken = await getToken();
        process.env.GH_TOKEN = newToken;
        process.env.GITHUB_TOKEN = newToken;
        console.log('   ✅ Token refreshed successfully');
        // Reset auth error counts
        resetErrorPattern('401');
        resetErrorPattern('bad credentials');
        return true;
      } catch (err) {
        console.log(`   ❌ Token refresh failed: ${(err as Error).message}`);
        return false;
      }
    }

    case 'wait_and_retry': {
      const waitTime = plan.waitTimeMs || 60000;
      console.log(`   ⏳ Waiting ${waitTime / 1000} seconds...`);
      await new Promise(resolve => setTimeout(resolve, waitTime));
      // Reset rate limit errors
      resetErrorPattern('rate limit');
      console.log('   ✅ Wait complete, resuming');
      return true;
    }

    case 'retry_with_backoff': {
      // Just signal that we should retry - the caller handles backoff
      console.log('   🔄 Will retry with backoff');
      return true;
    }

    case 'alternative_approach': {
      if (plan.alternativeCommand) {
        console.log(`   🔀 Trying alternative: ${plan.alternativeCommand}`);
        // The caller would need to handle this
      }
      return true;
    }

    case 'give_up':
    default:
      console.log('   💀 Cannot recover');
      return false;
  }
}

async function analyzeErrorsWithAI(): Promise<string> {
  const workflowDuration = Math.floor((Date.now() - workflowStartTime.getTime()) / 60000);

  const errorSummary = Array.from(errorPatterns.entries())
    .map(([cat, p]) => `- "${cat}": ${p.count} times (first: ${p.firstSeen.toISOString()}, last: ${p.lastSeen.toISOString()})`)
    .join('\n');

  const recentErrorMessages = recentErrors
    .slice(-10)
    .map((e, i) => `${i + 1}. [${e.timestamp.toISOString()}] ${e.message.substring(0, 500)}${e.context ? ` (context: ${e.context})` : ''}`)
    .join('\n');

  // This is now only called when we've given up on recovery
  return `## 🚨 Unrecoverable Error - All Recovery Attempts Failed

**Workflow Duration**: ${workflowDuration} minutes
**Recovery Attempts**: Multiple automatic recovery attempts were made but failed.

### Error Pattern Summary
${errorSummary}

### Recent Error Messages
${recentErrorMessages}

### What Was Tried
The agent attempted automatic recovery including:
- Token refresh (if available)
- Wait and retry (for rate limits)
- Retry with backoff (for transient errors)

All recovery strategies were exhausted.

### Next Steps
1. Check the workflow logs for detailed error information
2. Verify GitHub App credentials are configured correctly
3. Check GitHub API status for any ongoing incidents
4. Re-trigger by adding the \`agent-pm\` label after resolving the issue`;
}

// Track recovery attempts to avoid infinite loops
let recoveryAttempts = 0;
const MAX_RECOVERY_ATTEMPTS = 3;

async function checkForRepeatedErrors(threshold: number = 3): Promise<{ shouldFail: boolean; diagnosis: string } | null> {
  for (const [_category, pattern] of errorPatterns) {
    if (pattern.count >= threshold) {
      const workflowDuration = Math.floor((Date.now() - workflowStartTime.getTime()) / 60000);

      // Don't give up immediately - try to recover first!
      if (recoveryAttempts < MAX_RECOVERY_ATTEMPTS) {
        console.log(`\n${'='.repeat(60)}`);
        console.log(`🔍 Repeated errors detected (${pattern.pattern}: ${pattern.count} times)`);
        console.log(`🤖 Analyzing and planning recovery (attempt ${recoveryAttempts + 1}/${MAX_RECOVERY_ATTEMPTS})...`);
        console.log(`${'='.repeat(60)}\n`);

        const plan = await analyzeAndPlanRecovery();

        if (plan.canRecover) {
          recoveryAttempts++;
          const success = await executeRecovery(plan);

          if (success) {
            // Reset the specific error pattern that we recovered from
            resetErrorPattern(pattern.pattern);
            console.log(`✅ Recovery successful! Continuing with task...\n`);
            return null; // Don't fail, continue
          }
        }
      }

      // If we get here, recovery failed or we've exhausted attempts
      console.log(`\n❌ Recovery failed after ${recoveryAttempts} attempts. Generating final report...`);

      const aiDiagnosis = await analyzeErrorsWithAI();

      const message = `## 🚨 Error Recovery Failed

**Error Pattern**: ${pattern.pattern} occurred ${pattern.count} times
**Workflow Duration**: ${workflowDuration} minutes
**Recovery Attempts**: ${recoveryAttempts}

---

${aiDiagnosis}

---

### Recovery History
The agent attempted ${recoveryAttempts} automatic recovery(ies) before giving up.

**To retry**: Add the \`agent-pm\` label again after addressing the underlying issue.`;

      return { shouldFail: true, diagnosis: message };
    }
  }
  return null;
}

// Reset recovery attempts when starting fresh
function resetRecoveryState(): void {
  recoveryAttempts = 0;
  errorPatterns.clear();
  recentErrors.length = 0;
}

function resetErrorPattern(category: string): void {
  errorPatterns.delete(category);
}

function getErrorSummary(): string {
  if (errorPatterns.size === 0) return 'No errors tracked';

  const lines = ['Error patterns detected:'];
  for (const [category, pattern] of errorPatterns) {
    lines.push(`  - ${category}: ${pattern.count} occurrences`);
  }
  return lines.join('\n');
}

// ============================================================================
// Logging
// ============================================================================

const LOG_GROUP = '/github-ccsdk-agent/logs';
const LOG_STREAM = `agent-pm-issue-${ISSUE_NUMBER}-${Date.now()}`;
const cwClient = new CloudWatchLogsClient({ region: AWS_REGION });
let cwBuffer: { timestamp: number; message: string }[] = [];
let cwInitialized = false;

async function initCloudWatch(): Promise<void> {
  try {
    await cwClient.send(new CreateLogStreamCommand({ logGroupName: LOG_GROUP, logStreamName: LOG_STREAM }));
    cwInitialized = true;
  } catch (err: unknown) {
    if ((err as { name?: string }).name !== 'ResourceAlreadyExistsException') {
      console.warn('CloudWatch init failed:', (err as Error).message);
    } else {
      cwInitialized = true;
    }
  }
}

function log(level: string, message: string, context?: Record<string, unknown>): void {
  const entry = { level, message, issueNumber: ISSUE_NUMBER, ...context, timestamp: new Date().toISOString() };
  const emoji = level === 'ERROR' ? '❌' : level === 'WARN' ? '⚠️' : '→';
  console.log(`${emoji} ${message}`);
  if (cwInitialized) cwBuffer.push({ timestamp: Date.now(), message: JSON.stringify(entry) });
}

async function flushCloudWatch(): Promise<void> {
  if (!cwInitialized || cwBuffer.length === 0) return;
  const events = cwBuffer.splice(0, cwBuffer.length);
  try {
    await cwClient.send(new PutLogEventsCommand({ logGroupName: LOG_GROUP, logStreamName: LOG_STREAM, logEvents: events }));
  } catch { /* ignore */ }
}

const cwFlushTimer = setInterval(flushCloudWatch, 5000);

// ============================================================================
// Detailed Message Logging
// ============================================================================

function logMessage(message: { type: string; message: { content: Array<Record<string, unknown>> } }, turnCount: number): string {
  let text = '';
  const toolsUsed: string[] = [];
  console.log(`\n${'─'.repeat(60)}`);
  console.log(`Turn ${turnCount}`);
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
        console.log(`📖 Read: ${input.file_path}`);
      } else if (toolName === 'Bash') {
        const cmd = (input.command as string || '').substring(0, 200);
        console.log(`💻 Bash: ${cmd}${cmd.length >= 200 ? '...' : ''}`);
      } else if (toolName === 'WebSearch') {
        console.log(`🔍 WebSearch: ${input.query}`);
      } else if (toolName === 'WebFetch') {
        console.log(`🌐 WebFetch: ${input.url}`);
      } else if (toolName === 'Glob') {
        console.log(`📂 Glob: ${input.pattern}`);
      } else if (toolName === 'Grep') {
        console.log(`🔎 Grep: ${input.pattern}`);
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

  log('INFO', `Turn ${turnCount} completed`, { turn: turnCount, tools: toolsUsed, textLength: text.length });
  return text;
}

// ============================================================================
// GitHub Helpers
// ============================================================================

function getLocalToken(useAppToken: boolean): string {
  return useAppToken && GH_APP_TOKEN ? GH_APP_TOKEN : GITHUB_TOKEN;
}

async function getFreshToken(useAppToken: boolean): Promise<string> {
  // If token refresh is enabled and we're using app token, get a fresh one
  if (TOKEN_REFRESH_ENABLED && useAppToken && needsRefresh()) {
    try {
      const freshToken = await getToken();
      log('INFO', 'Token refreshed successfully');
      return freshToken;
    } catch (err) {
      log('WARN', `Token refresh failed, using existing token: ${(err as Error).message}`);
    }
  }
  return getLocalToken(useAppToken);
}

async function execCommand(command: string, useAppToken: boolean = true): Promise<string> {
  const { execSync } = await import('child_process');

  // Check for repeated errors before even trying
  const errorCheck = await checkForRepeatedErrors(5);
  if (errorCheck?.shouldFail) {
    throw new Error(`Failing fast due to repeated errors:\n${errorCheck.diagnosis}`);
  }

  // Get a fresh token if needed
  const token = await getFreshToken(useAppToken);

  try {
    const result = execSync(command, {
      cwd: CWD,
      encoding: 'utf-8',
      env: { ...process.env, GH_TOKEN: token, GITHUB_TOKEN: token },
      maxBuffer: 10 * 1024 * 1024,
    }).trim();

    // Success - reset error patterns for auth errors (transient issue resolved)
    resetErrorPattern('401');
    resetErrorPattern('Bad credentials');

    return result;
  } catch (error) {
    const err = error as { stderr?: string; message?: string };
    const errorMessage = err.stderr || err.message || 'Unknown error';

    // Track this error for pattern detection
    trackError(errorMessage);

    // Check if we've hit the threshold
    const repeatedErrorCheck = await checkForRepeatedErrors(3);
    if (repeatedErrorCheck?.shouldFail) {
      // Post the diagnosis to the issue before failing
      try {
        const { execSync: execSyncDirect } = await import('child_process');
        const diagnosisFile = `/tmp/error-diagnosis-${Date.now()}.md`;
        const fsModule = await import('fs');
        fsModule.writeFileSync(diagnosisFile, repeatedErrorCheck.diagnosis);
        execSyncDirect(`gh issue comment ${ISSUE_NUMBER} --body-file "${diagnosisFile}"`, {
          cwd: CWD,
          encoding: 'utf-8',
          env: { ...process.env, GH_TOKEN: GH_APP_TOKEN || GITHUB_TOKEN },
        });
      } catch {
        // If we can't even post the comment, just log it
        console.error(repeatedErrorCheck.diagnosis);
      }
      throw new Error(`Failing fast due to repeated errors:\n${repeatedErrorCheck.diagnosis}`);
    }

    // If we get a 401, try refreshing token and retrying once
    if (TOKEN_REFRESH_ENABLED && useAppToken &&
        (err.stderr?.includes('401') || err.stderr?.includes('Bad credentials'))) {
      log('WARN', 'Got 401, forcing token refresh and retry...');
      try {
        const freshToken = await forceRefresh();
        const result = execSync(command, {
          cwd: CWD,
          encoding: 'utf-8',
          env: { ...process.env, GH_TOKEN: freshToken, GITHUB_TOKEN: freshToken },
          maxBuffer: 10 * 1024 * 1024,
        }).trim();

        // Success after refresh - reset the error counter
        resetErrorPattern('401');
        resetErrorPattern('Bad credentials');
        return result;
      } catch (retryError) {
        trackError(errorMessage); // Track the retry failure too
        log('ERROR', `Retry after token refresh also failed`);
        throw retryError;
      }
    }

    log('ERROR', `Command failed: ${command.substring(0, 100)}`, { error: errorMessage });
    throw error;
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
  const json = await gh(`issue view ${ISSUE_NUMBER} --json number,title,body,labels`);
  const data = JSON.parse(json);
  return {
    number: data.number,
    title: data.title,
    body: data.body || '',
    labels: (data.labels || []).map((l: { name: string }) => l.name),
  };
}

async function postComment(body: string): Promise<void> {
  try {
    await refreshGitHubToken();
    const tmpFile = `/tmp/comment-${Date.now()}.md`;
    fs.writeFileSync(tmpFile, body);
    try {
      await gh(`issue comment ${ISSUE_NUMBER} --body-file "${tmpFile}"`);
    } finally {
      try { fs.unlinkSync(tmpFile); } catch {}
    }
  } catch (err) {
    log('WARN', `GitHub post failed, saving to S3: ${(err as Error).message}`);
    await saveToS3Fallback(ISSUE_NUMBER, 'comment', body);
  }
}

// ============================================================================
// Rule Loading
// ============================================================================

function loadRules(): string {
  const rulesDir = path.join(CWD, '.adp-rules');
  const rules: string[] = [];

  // Load PM persona definition (identity + mindset) — loaded FIRST
  // Check target repo first (repo-specific wins), fall back to adp defaults
  const repoPersona = path.join(CWD, '.github-agent', 'personas', 'pm.md');
  const adpPersona = path.join(rulesDir, 'personas', 'pm.md');

  if (fs.existsSync(repoPersona)) {
    rules.push(`## Your Persona\n${fs.readFileSync(repoPersona, 'utf-8')}`);
  } else if (fs.existsSync(adpPersona)) {
    rules.push(`## Your Persona\n${fs.readFileSync(adpPersona, 'utf-8')}`);
  }

  const filesToLoad = [
    'core-workflow.md',
    'agents/github-issue-hierarchy-guidelines.md',  // Epic/Story/Unit hierarchy with dependencies
    'phases/inception/requirements-analysis.md',
    'phases/inception/user-stories.md',
    'phases/inception/application-design.md',
    'phases/inception/units-generation.md',
    'project-management/board-management.md',
    'agents/agent-routing.md',
    'tools/beads-usage.md',  // Shared task management
    'memory.md',  // Agent memory system
  ];

  for (const file of filesToLoad) {
    const fullPath = path.join(rulesDir, file);
    if (fs.existsSync(fullPath)) {
      rules.push(`## ${path.basename(file, '.md')}\n${fs.readFileSync(fullPath, 'utf-8')}`);
    }
  }

  // Add bd prime context if available (AI-optimized workflow guidance)
  if (beadsPrimeContext) {
    rules.push(`## Beads Workflow Context (from bd prime)\n${beadsPrimeContext}`);
  }

  // Add agent memory context if available (persistent context from adp branch)
  if (agentMemoryContext) {
    rules.push(agentMemoryContext);
  }

  return rules.join('\n\n---\n\n');
}

function loadTemplates(): Record<string, string> {
  const templatesDir = path.join(CWD, '.adp-rules', 'templates');
  const templates: Record<string, string> = {};

  const templateFiles = [
    'aidlc-state-template.md',
    'audit-template.md',
    'inception/requirements-plan-template.md',
  ];

  for (const file of templateFiles) {
    const fullPath = path.join(templatesDir, file);
    if (fs.existsSync(fullPath)) {
      templates[path.basename(file, '.md')] = fs.readFileSync(fullPath, 'utf-8');
    }
  }

  return templates;
}

// ============================================================================
// State Management
// ============================================================================

interface AIDLCState {
  phase: string;
  stage: string;
  complexity: string;
  projectBoardUrl?: string;
  projectNumber?: number;
  currentPlanFile?: string;
  waitingForUser: boolean;
  lastCommitSha?: string;
  conversationContext: string;
}

function loadState(): AIDLCState | null {
  const statePath = path.join(CWD, 'aidlc-docs', 'aidlc-state.json');
  if (fs.existsSync(statePath)) {
    try {
      return JSON.parse(fs.readFileSync(statePath, 'utf-8'));
    } catch {
      return null;
    }
  }
  return null;
}

function saveState(state: AIDLCState): void {
  const aidlcDir = path.join(CWD, 'aidlc-docs');
  if (!fs.existsSync(aidlcDir)) {
    fs.mkdirSync(aidlcDir, { recursive: true });
  }
  fs.writeFileSync(path.join(aidlcDir, 'aidlc-state.json'), JSON.stringify(state, null, 2));
}

// ============================================================================
// AIDLC Branch Management
// ============================================================================

/**
 * Check if an AIDLC branch exists for this issue and checkout if found.
 * This ensures we can load state from previous workflow runs.
 *
 * Branch naming convention: aidlc/issue-{number}-{slug}
 */
async function checkoutAIDLCBranchIfExists(): Promise<{ found: boolean; branch: string | null }> {
  try {
    // Fetch all remote branches
    await execCommand('git fetch origin');

    // Look for AIDLC branch for this issue
    const branchPattern = `aidlc/issue-${ISSUE_NUMBER}-`;
    const remoteBranches = await execCommand('git branch -r');

    // Find matching branch
    const branches = remoteBranches.split('\n').map(b => b.trim());
    const aidlcBranch = branches.find(b => b.includes(branchPattern));

    if (aidlcBranch) {
      // Extract branch name (remove 'origin/' prefix)
      const branchName = aidlcBranch.replace('origin/', '');
      log('INFO', `Found existing AIDLC branch: ${branchName}`);

      // Check if we're already on this branch
      const currentBranch = await execCommand('git rev-parse --abbrev-ref HEAD');
      if (currentBranch === branchName) {
        log('INFO', 'Already on AIDLC branch');
        return { found: true, branch: branchName };
      }

      // Checkout the AIDLC branch
      try {
        // First try to checkout existing local branch
        await execCommand(`git checkout ${branchName}`);
      } catch {
        // If local branch doesn't exist, create from remote
        await execCommand(`git checkout -b ${branchName} origin/${branchName}`);
      }

      log('INFO', `Checked out AIDLC branch: ${branchName}`);
      return { found: true, branch: branchName };
    }

    log('INFO', `No existing AIDLC branch found for issue #${ISSUE_NUMBER}`);
    return { found: false, branch: null };
  } catch (err) {
    log('WARN', `Error checking for AIDLC branch: ${(err as Error).message}`);
    return { found: false, branch: null };
  }
}

// ============================================================================
// Polling for User Response
// ============================================================================

async function getCurrentBranch(): Promise<string> {
  try {
    return execCommand('git rev-parse --abbrev-ref HEAD');
  } catch {
    return 'main';
  }
}

async function getLatestCommit(): Promise<{ sha: string; message: string } | null> {
  try {
    // Fetch latest from remote
    await execCommand('git fetch origin');
    const branch = await getCurrentBranch();

    // Get latest commit info
    const sha = await execCommand(`git rev-parse origin/${branch}`);
    const message = await execCommand(`git log -1 --format=%s origin/${branch}`);

    return { sha, message };
  } catch (err) {
    log('WARN', `Could not get latest commit: ${(err as Error).message}`);
    return null;
  }
}

async function pullLatestChanges(): Promise<boolean> {
  try {
    const branch = await getCurrentBranch();
    const result = await execCommand(`git pull --rebase origin ${branch}`);
    log('INFO', `Pulled latest changes: ${result.substring(0, 100)}`);
    return true;
  } catch (err) {
    log('WARN', `Could not pull changes: ${(err as Error).message}`);
    return false;
  }
}

interface PollResult {
  shouldContinue: boolean;
  reason: 'commit_detected' | 'timeout' | 'error';
  commitMessage?: string;
}

async function pollForUserResponse(lastKnownSha: string): Promise<PollResult> {
  const startTime = Date.now();
  const maxPolls = Math.ceil(USER_RESPONSE_TIMEOUT_MS / POLL_INTERVAL_MS);

  log('INFO', `Starting to poll for user response (max ${maxPolls} polls over ${USER_RESPONSE_TIMEOUT_MS / 60000} minutes)`);
  console.log('\n' + '═'.repeat(60));
  console.log('⏳ Waiting for user to commit changes...');
  console.log('   Looking for commits containing "AIDLC" in message');
  console.log('═'.repeat(60) + '\n');

  for (let poll = 1; poll <= maxPolls; poll++) {
    const elapsedMinutes = Math.round((Date.now() - startTime) / 60000);
    const remainingMinutes = Math.round((USER_RESPONSE_TIMEOUT_MS - (Date.now() - startTime)) / 60000);

    // Check for repeated errors before continuing
    const errorCheck = await checkForRepeatedErrors(5);
    if (errorCheck?.shouldFail) {
      log('ERROR', 'Failing fast due to repeated errors during polling');
      // Try to post diagnosis
      try {
        await postComment(errorCheck.diagnosis);
      } catch {
        console.error(errorCheck.diagnosis);
      }
      return {
        shouldContinue: false,
        reason: 'error',
      };
    }

    console.log(`🔄 Poll ${poll}/${maxPolls} (${elapsedMinutes}m elapsed, ${remainingMinutes}m remaining)`);

    try {
      const latestCommit = await getLatestCommit();

      if (latestCommit && latestCommit.sha !== lastKnownSha) {
        // New commit detected - check if it's an AIDLC continue signal
        const message = latestCommit.message.toLowerCase();
        const isAidlcCommit = message.includes('aidlc') ||
                             message.includes('answer') ||
                             message.includes('continue') ||
                             message.includes('response');

        log('INFO', `New commit detected: ${latestCommit.sha.substring(0, 7)} - "${latestCommit.message}"`);
        console.log(`📥 New commit: ${latestCommit.message}`);

        if (isAidlcCommit) {
          console.log('✅ AIDLC continue signal detected!');
          await pullLatestChanges();
          return {
            shouldContinue: true,
            reason: 'commit_detected',
            commitMessage: latestCommit.message,
          };
        } else {
          // Update the known SHA even if not an AIDLC commit
          lastKnownSha = latestCommit.sha;
          console.log('   (Not an AIDLC commit, continuing to poll...)');
        }
      }

      // Successful poll - print error summary if there were previous errors
      if (errorPatterns.size > 0) {
        console.log(`   ℹ️ ${getErrorSummary()}`);
      }
    } catch (err) {
      const errorMessage = (err as Error).message;
      log('WARN', `Poll error: ${errorMessage}`);
      trackError(errorMessage);

      // Give immediate feedback if this looks serious
      const category = categorizeError(errorMessage);
      if (category !== 'unknown') {
        const pattern = errorPatterns.get(category);
        if (pattern && pattern.count >= 2) {
          console.log(`   ⚠️ Warning: "${category}" error occurred ${pattern.count} times`);
        }
      }
    }

    // Wait before next poll
    if (poll < maxPolls) {
      await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }

  // Timeout reached
  return {
    shouldContinue: false,
    reason: 'timeout',
  };
}

// ============================================================================
// Main AIDLC Orchestrator
// ============================================================================

async function runQuery(prompt: string, maxTurns: number = 100): Promise<string> {
  let fullResponse = '';
  let turnCount = 0;
  let lastActivityTime = Date.now();

  const heartbeat = setInterval(() => {
    const silentSec = Math.round((Date.now() - lastActivityTime) / 1000);
    if (silentSec >= 60) {
      const msg = `💓 Heartbeat — no SDK messages for ${silentSec}s (turn ${turnCount})`;
      console.log(msg);
      log('INFO', msg, { phase: 'heartbeat', silentSeconds: silentSec, turn: turnCount });
    }
  }, 30_000);

  try {
    for await (const message of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: MODEL,
          cwd: CWD,
          allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch'],
          settingSources: ['project'],
          permissionMode: 'bypassPermissions',
          persistSession: false,
          maxTurns,
        }
      },
      maxRetries: 5,
      baseDelayMs: 10_000,
      maxDelayMs: 120_000,
      log: (msg) => log('WARN', msg),
    })) {
      lastActivityTime = Date.now();

      switch (message.type) {
        case 'assistant': {
          turnCount++;
          fullResponse += logMessage(
            message as { type: string; message: { content: Array<Record<string, unknown>> } },
            turnCount
          );
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
          break;
        }

        case 'tool_progress': {
          const tp = message as { tool_name: string; elapsed_time_seconds: number };
          console.log(`⏳ Tool running: ${tp.tool_name} (${tp.elapsed_time_seconds}s elapsed)`);
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
  }

  return fullResponse;
}

function buildStartPrompt(issue: Issue, rules: string, templates: Record<string, string>): string {
  return `You are @agent-pm, the AIDLC Workflow Orchestrator.

## Your Role
You guide humans through the AI-Driven Development Life Cycle (AIDLC) while managing a GitHub Project board to coordinate work with other agents.

## The Issue
**Issue #${issue.number}: ${issue.title}**

${issue.body}

---

## AIDLC Rules and Guidelines

${rules}

---

## Available Templates

${Object.entries(templates).map(([name, content]) => `### ${name}\n\`\`\`\n${content.substring(0, 500)}...\n\`\`\``).join('\n\n')}

---

## Your Task - START NEW WORKFLOW

### Step 1: RESEARCH AND ANALYZE (use your tools!)
**Before asking ANY questions**, thoroughly investigate:

1. **Analyze the codebase** - Use Glob, Grep, Read to understand:
   - Project structure and existing patterns
   - Similar implementations that already exist
   - Technology stack and conventions used
   - Configuration files, dependencies, README

2. **Research externally** - Use WebSearch/WebFetch to find:
   - Best practices for this type of implementation
   - Documentation for relevant technologies
   - Common pitfalls and solutions
   - Reference implementations or tutorials

3. **Check existing work** - Use Bash (gh CLI) to find:
   - Related issues or PRs in the repo
   - Previous discussions about similar features
   - Any existing documentation

**IMPORTANT**: Spend at least 5-10 tool calls researching before proceeding. The more you understand, the fewer questions you need to ask.

### Step 2: ASSESS COMPLEXITY (based on your research)
Now that you understand the context, classify:
- **Simple** (single component, clear requirements, you know how to do it) → Minimal AIDLC, maybe no questions needed
- **Medium** (multiple components, some unknowns) → Standard AIDLC
- **Complex** (large scope, many unknowns even after research) → Full AIDLC with project board

### Step 3: GIVE YOUR RECOMMENDATIONS
Based on your research:
- What technology/approach do YOU recommend and why?
- What did you learn from analyzing the codebase?
- Which AIDLC phases are actually needed?
- Be opinionated with clear reasoning backed by evidence

### Step 4: CREATE AIDLC STRUCTURE
Create these files:
- \`aidlc-docs/aidlc-state.md\` - Current state for humans
- \`aidlc-docs/aidlc-state.json\` - Machine-readable state
- \`aidlc-docs/audit.md\` - Activity log with your research findings
- \`aidlc-docs/inception/plans/requirements-plan.md\` - Plan with ONLY questions you couldn't answer through research

**CRITICAL**: For every question you ask:
1. You MUST provide YOUR RECOMMENDATION as the default answer
2. Explain why you're recommending this approach
3. Only ask if the user wants to override your recommendation

Format questions like this:
\`\`\`
### Q1: [Question]
**My Recommendation**: [Your recommended answer based on research]
**Reasoning**: [Why you recommend this]
[Answer]: [Pre-fill with your recommendation - user can modify or accept]
\`\`\`

Only ask questions about things that:
- Cannot be found in the codebase
- Cannot be researched online
- Require business/domain knowledge
- Involve subjective preferences

**AUTO-PROCEED**: If the user doesn't respond within 15 minutes, the agent will automatically proceed with the recommended answers.

### Step 5: CREATE PROJECT BOARD (if medium+ complexity)
If complexity is medium or higher:
1. Create GitHub Project: \`gh project create --owner ${REPO_OWNER} --title "Issue #${issue.number} - ${issue.title.substring(0, 30)}"\`
2. Add custom fields for phase, item_type, assigned_agent, blocked_by
3. Add the main issue to the project
4. Report the project URL in your comment

### Step 6: SAVE STATE
Save state to aidlc-docs/aidlc-state.json:
\`\`\`json
{
  "phase": "inception",
  "stage": "requirements",
  "complexity": "medium|simple|complex",
  "projectBoardUrl": "URL or null",
  "projectNumber": number or null,
  "currentPlanFile": "aidlc-docs/inception/plans/requirements-plan.md",
  "waitingForUser": true or false,
  "conversationContext": "Summary of your research findings and recommendations"
}
\`\`\`

**Note**: Set \`waitingForUser: false\` if you have enough information to proceed without questions.

### Step 7: COMMIT AND PUSH
After creating files:
\`\`\`bash
git add aidlc-docs/
git commit -m "Initialize AIDLC workflow for issue #${issue.number}"
git push
\`\`\`

### Step 8: POST DETAILED COMMENT
Post a comment to the issue with:
1. **Research Findings** - What you learned from analyzing the codebase and external research
2. **Complexity Assessment** - What you determined and why
3. **Recommendations** - Your suggested approach with evidence from your research
4. **Project Board Status** - Created with URL, or not needed
5. **Questions with Recommendations** - Each question MUST include your recommended answer
6. **Auto-Proceed Notice** - Tell user they have 15 minutes to respond, otherwise you'll proceed with recommendations

Example notice:
\`\`\`
⏱️ **Auto-Proceed in 15 minutes**: I've provided my recommendations for each question above.
- To accept all recommendations: Do nothing, I'll proceed automatically
- To modify: Edit the plan file, fill [Answer]: tags, and commit with "AIDLC continue"
\`\`\`

**IMPORTANT**: If you have no questions (you found all answers through research), proceed directly to the next phase instead of waiting.

## Tools Available

- **Bash**: Execute gh CLI, bd (beads), and git commands
- **Write/Edit**: Create AIDLC documents
- **Read/Glob/Grep**: Analyze codebase
- **WebSearch/WebFetch**: Research

### Beads Task Management (bd)

Use \`bd\` for task orchestration (shared state across all agents):
- \`bd create "Title" -p 1 -t task --json\` - Create tasks
- \`bd ready --json\` - List tasks ready to work on (no blockers)
- \`bd dep add <child> <parent> --type blocks\` - Add dependencies
- \`bd list --json\` - List all tasks
- \`bd show <task-id>\` - View task details
- \`bd update <id> --status done\` - Mark task complete
- \`bd dolt push\` - Sync state to S3

### ⚠️ CRITICAL: Agent Triggering Rules

**NEVER add agent labels to blocked tasks!**

Before adding ANY \`agent-*\` label:
1. Check if the task has blockers (\`blocked_by\` field on project board OR Beads dependencies)
2. If blocked, DO NOT add the agent label - wait for blockers to complete
3. Only trigger agents for tasks with NO unresolved blockers

Use \`bd ready --json\` to get ONLY tasks that are ready to work on (no blockers).
The PM monitoring loop will automatically unblock and trigger agents when their blockers complete.

#### IMPORTANT: Beads Bootstrap/Sync on Startup

**ALWAYS check Beads state first** by running \`bd list --json\`.

**If Beads is empty but child issues exist:**
1. This means Beads needs to be bootstrapped from existing state
2. **Source of Truth** (in order):
   - GitHub Sub-Issues (native parent-child via GraphQL API) - defines project structure
   - Workflow runs (\`gh run list\`) - defines actual execution status
   - PRs linked to issues - defines completion state
3. **NOT Source of Truth**: GitHub Projects (human-facing view only)

**Bootstrap procedure when Beads is empty:**
\`\`\`bash
# 1. Find child issues using native sub-issues API (preferred)
gh api graphql -f query='query { repository(owner: "${REPO_OWNER}", name: "${REPO_NAME}") { issue(number: ${ISSUE_NUMBER}) { subIssues(first: 100) { nodes { number title state labels(first: 10) { nodes { name } } } } } } }' --jq '.data.repository.issue.subIssues.nodes'

# Fallback: search for "Parent: #N" in body (legacy)
gh issue list --search "parent:#${ISSUE_NUMBER} in:body" --json number,title,state,labels

# 2. Check workflow runs (source of truth for status)
gh run list --limit 20 --json databaseId,status,conclusion,headBranch

# 3. Create epic in Beads
bd create "#${ISSUE_NUMBER} - Epic Title" -t epic -p 1 --json

# 4. For each child issue, create a task
bd create "[Unit N] Title" -p 1 --json
# Then add dependency to epic:
bd dep add <task-id> <epic-id> --type parent-child

# 5. Set correct status based on workflow runs:
# - If workflow completed successfully → bd update <id> --status done
# - If workflow failed → bd update <id> --status failed
# - If workflow in progress → bd update <id> --status in_progress

# 6. Add blockers between units (from issue body "Blocked by: #N")
bd dep add <blocked-task> <blocker-task> --type blocks

# 7. Push to S3
bd dolt push
\`\`\`

**After bootstrap, update GitHub Projects** to match Beads state (Projects is for human review, not source of truth).

When creating NEW project structure, use Beads to create the epic and tasks with proper dependencies.

### GitHub Sub-Issues (REQUIRED for issue hierarchy)

**ALWAYS use GitHub's native sub-issues** to create parent-child relationships between issues.
This creates a proper hierarchy visible in GitHub UI.

#### Creating Issues as Sub-Issues

When creating child issues (epics, stories, units), ALWAYS add them as sub-issues:

\`\`\`bash
# 1. Create the issue first
gh issue create --repo ${REPO_OWNER}/${REPO_NAME} --title "US-1: Task Title" --body "Description here" --label "type: story"

# 2. Get the new issue number from the output URL, then add as sub-issue
PARENT_ID=$(gh api graphql -f query='query { repository(owner: "${REPO_OWNER}", name: "${REPO_NAME}") { issue(number: ${ISSUE_NUMBER}) { id } } }' --jq '.data.repository.issue.id')
CHILD_ID=$(gh api graphql -f query='query { repository(owner: "${REPO_OWNER}", name: "${REPO_NAME}") { issue(number: <NEW_ISSUE_NUMBER>) { id } } }' --jq '.data.repository.issue.id')
gh api graphql -f query="mutation { addSubIssue(input: { issueId: \\"$PARENT_ID\\", subIssueId: \\"$CHILD_ID\\" }) { subIssue { number } } }"
\`\`\`

#### Hierarchy Structure

For complex issues, create this hierarchy using sub-issues:
\`\`\`
#${ISSUE_NUMBER} (Original Issue - Parent)
└── Epic Issue (sub-issue of original)
    ├── US-1: Story (sub-issue of epic)
    ├── US-2: Story (sub-issue of epic)
    └── US-3: Story (sub-issue of epic)
\`\`\`

#### Querying Sub-Issues

\`\`\`bash
# Get sub-issues of current issue
gh api graphql -f query='query { repository(owner: "${REPO_OWNER}", name: "${REPO_NAME}") { issue(number: ${ISSUE_NUMBER}) { subIssues(first: 100) { nodes { number title state } } } } }' --jq '.data.repository.issue.subIssues.nodes'

# Get parent of an issue
gh api graphql -f query='query { repository(owner: "${REPO_OWNER}", name: "${REPO_NAME}") { issue(number: <ISSUE_NUM>) { parent { number title } } } }' --jq '.data.repository.issue.parent'
\`\`\`

**IMPORTANT**: Do NOT rely only on "Parent: #N" text in issue body. Always use the GraphQL API to establish proper sub-issue relationships.

Execute the workflow now.`;
}

function buildContinuePrompt(issue: Issue, state: AIDLCState, rules: string): string {
  return `You are @agent-pm, the AIDLC Workflow Orchestrator.

## CONTINUING WORKFLOW

The user has submitted their responses. Continue the AIDLC workflow.

### Current State
- **Phase**: ${state.phase}
- **Stage**: ${state.stage}
- **Complexity**: ${state.complexity}
- **Project Board**: ${state.projectBoardUrl || 'Not created'}
- **Current Plan File**: ${state.currentPlanFile || 'None'}

### Previous Context
${state.conversationContext}

### Issue #${issue.number}: ${issue.title}

${issue.body}

---

## Your Task

1. **Pull latest changes** and read the current plan file

2. **Check user responses** - look for filled [Answer]: tags

3. **RESEARCH to fill gaps** - Before asking follow-up questions:
   - Use Glob/Grep/Read to search the codebase for answers
   - Use WebSearch/WebFetch to research technical questions
   - Use gh CLI to check related issues, PRs, discussions
   - Try to answer ambiguous responses yourself through investigation

4. **Validate and proceed**:
   - If you can infer/research the answer → proceed without asking
   - If user gave clear direction → follow it
   - If genuinely ambiguous AND you can't research the answer → ask ONE focused follow-up

5. **Generate outputs**:
   - Generate output documents for current stage
   - Create next plan file (if more phases needed)
   - Update aidlc-state.json and aidlc-state.md
   - Commit and push changes
   - Post comment with progress and next steps

6. **Only ask questions when truly necessary**:
   - Questions should be about business decisions, not technical details you can research
   - Each question should be critical to proceeding
   - Offer your recommendation with each question

### IMPORTANT
- **Bias toward action**: If you have enough information, proceed rather than asking
- **Research first**: Spend tool calls investigating before asking follow-ups
- **Use pre-filled recommendations**: If [Answer]: tags already have values (from your previous recommendations), USE THEM unless the user explicitly changed them
- **Auto-proceed mode**: If this is a timeout continuation, proceed with all pre-filled recommendations
- Set \`waitingForUser: true\` ONLY if you genuinely need user input AND have no good recommendation
- Set \`waitingForUser: false\` if you can proceed (either with user answers or your recommendations)
- Always commit and push your changes

### Handling Pre-filled Answers
When you see \`[Answer]: <some value>\`:
- If the value looks like YOUR recommendation (technical, detailed) → User accepted, proceed with it
- If the value is different from typical recommendations → User modified, use their version
- If empty → Provide your recommendation and either ask or proceed based on confidence

## Rules
${rules}

Execute the continuation now.`;
}

// ============================================================================
// Reassessment Integration
// ============================================================================

interface ReassessmentResult {
  needsReassessment: boolean;
  context: ReassessmentContext | null;
}

async function checkForReassessment(
  issue: Issue,
  existingState: AIDLCState | null
): Promise<ReassessmentResult> {
  if (!isReassessmentEnabled()) {
    log('INFO', 'Reassessment module disabled');
    return { needsReassessment: false, context: null };
  }

  try {
    log('INFO', 'Checking if reassessment is needed...');

    // Quick check: fetch comments to see if there's agent activity
    const comments = await fetchIssueComments(ISSUE_NUMBER, execCommand);

    if (!shouldReassess(existingState, comments)) {
      log('INFO', 'No reassessment needed - normal flow');
      return { needsReassessment: false, context: null };
    }

    log('INFO', 'Reassessment needed - gathering full context...');

    // Get project number from existing state or search for it
    let projectNumber: number | null = existingState?.projectNumber || null;
    if (!projectNumber) {
      // Try to find project from issue labels or body
      try {
        const projectSearch = await execCommand(
          `gh project list --owner ${REPO_OWNER} --format json --limit 20`
        );
        const projects = JSON.parse(projectSearch || '{"projects":[]}');
        // Look for a project that might be related to this issue
        for (const proj of projects.projects || []) {
          if (proj.title && proj.title.includes(`#${ISSUE_NUMBER}`)) {
            projectNumber = proj.number;
            break;
          }
        }
      } catch {
        log('WARN', 'Could not search for project');
      }
    }

    // Save discovered project number back to existingState so monitoring can use it
    if (projectNumber && existingState && !existingState.projectNumber) {
      existingState.projectNumber = projectNumber;
      log('INFO', `Discovered and saved project number: ${projectNumber}`);
      saveState(existingState);
    }

    // Convert existingState to AIDLCStateInfo for reassessment context
    const currentBranch = await getCurrentBranch();

    // Try to extract PM recommendations from the plan file
    let pmRecommendations: string | null = null;
    if (existingState?.currentPlanFile) {
      const planFilePath = path.join(CWD, existingState.currentPlanFile);
      if (fs.existsSync(planFilePath)) {
        try {
          const planContent = fs.readFileSync(planFilePath, 'utf-8');
          // Extract the "PM Recommendations" section if it exists
          const recMatch = planContent.match(/## PM Recommendations\s*([\s\S]*?)(?=\n## |\n---|\n# |$)/i);
          if (recMatch) {
            pmRecommendations = recMatch[1].trim().substring(0, 2000); // Limit to 2000 chars
          }
        } catch (err) {
          log('WARN', `Could not read plan file: ${(err as Error).message}`);
        }
      }
    }

    // Build artifacts from child issues (we'll fetch them to categorize)
    let artifacts: AIDLCArtifacts | null = null;
    try {
      const childIssuesJson = await execCommand(
        `gh issue list --repo ${REPO_OWNER}/${REPO_NAME} --search "Parent: #${ISSUE_NUMBER} in:body" --json number,title,labels --limit 50`
      );
      const childIssues = JSON.parse(childIssuesJson || '[]');

      const epics: { number: number; title: string }[] = [];
      const stories: { number: number; title: string }[] = [];
      const units: { number: number; title: string }[] = [];

      for (const issue of childIssues) {
        const labels = (issue.labels || []).map((l: { name: string }) => l.name.toLowerCase());
        if (labels.includes('type: epic') || labels.includes('epic')) {
          epics.push({ number: issue.number, title: issue.title });
        } else if (labels.includes('type: story') || labels.includes('story')) {
          stories.push({ number: issue.number, title: issue.title });
        } else if (labels.includes('type: unit') || labels.includes('unit') || labels.includes('type: task') || labels.includes('task')) {
          units.push({ number: issue.number, title: issue.title });
        }
      }

      // Get project board info
      let projectBoard: { number: number; url: string } | null = null;
      if (projectNumber) {
        projectBoard = {
          number: projectNumber,
          url: `https://github.com/orgs/${REPO_OWNER}/projects/${projectNumber}`,
        };
      }

      // Get PRs (simplified - just check for any linked PRs)
      const prsCreated: { number: number; title: string }[] = [];
      const prsMerged: { number: number; title: string }[] = [];
      try {
        const prsJson = await execCommand(
          `gh pr list --repo ${REPO_OWNER}/${REPO_NAME} --search "${ISSUE_NUMBER}" --json number,title,state --limit 20`
        );
        const prs = JSON.parse(prsJson || '[]');
        for (const pr of prs) {
          if (pr.state === 'MERGED') {
            prsMerged.push({ number: pr.number, title: pr.title });
          } else if (pr.state === 'OPEN') {
            prsCreated.push({ number: pr.number, title: pr.title });
          }
        }
      } catch {
        // PR fetch failed, continue without
      }

      artifacts = { epics, stories, units, projectBoard, prsCreated, prsMerged };
    } catch (err) {
      log('WARN', `Could not build artifacts: ${(err as Error).message}`);
    }

    const aidlcStateInfo = existingState ? {
      waitingForUser: existingState.waitingForUser ?? false,
      phase: existingState.phase ?? 'unknown',
      stage: existingState.stage ?? 'unknown',
      currentPlanFile: existingState.currentPlanFile ?? null,
      branch: currentBranch !== 'main' ? currentBranch : null,
      pmRecommendations,
      artifacts,
    } : null;

    // Gather full reassessment context
    const context = await gatherReassessmentContext(
      ISSUE_NUMBER,
      REPO_OWNER,
      REPO_NAME,
      projectNumber,
      execCommand,
      aidlcStateInfo
    );

    if (context.hasExistingProgress) {
      log('INFO', `Reassessment context: ${context.childIssues.length} child issues, ${context.analysis.failedTasks.length} failed, ${context.analysis.pendingTasks.length} pending`);
      return { needsReassessment: true, context };
    }

    log('INFO', 'No significant existing progress found');
    return { needsReassessment: false, context: null };

  } catch (error) {
    log('ERROR', `Reassessment check failed: ${(error as Error).message}`);
    // On error, fall back to normal flow
    return { needsReassessment: false, context: null };
  }
}

async function pollForReassessmentChoice(): Promise<{ choice: UserReassessmentChoice | null; comment: string | null }> {
  const startTime = Date.now();
  const maxPolls = Math.ceil(USER_RESPONSE_TIMEOUT_MS / POLL_INTERVAL_MS);

  log('INFO', `Waiting for user's reassessment choice (max ${maxPolls} polls)`);
  console.log('\n' + '='.repeat(60));
  console.log('Waiting for user choice...');
  console.log('Commands: /approve, /action N, /retry #N, /skip, /custom');
  console.log('='.repeat(60) + '\n');

  // Track by timestamp instead of count (fetchIssueComments returns last 50, so count won't work)
  let lastSeenTimestamp = new Date().toISOString();
  try {
    const initialComments = await fetchIssueComments(ISSUE_NUMBER, execCommand);
    if (initialComments.length > 0) {
      // Get the timestamp of the most recent comment
      lastSeenTimestamp = initialComments[initialComments.length - 1].createdAt || lastSeenTimestamp;
    }
    log('INFO', `Initial lastSeenTimestamp: ${lastSeenTimestamp}`);
  } catch {
    log('WARN', 'Could not get initial comments');
  }

  for (let poll = 1; poll <= maxPolls; poll++) {
    const elapsedMinutes = Math.round((Date.now() - startTime) / 60000);
    const remainingMinutes = Math.round((USER_RESPONSE_TIMEOUT_MS - (Date.now() - startTime)) / 60000);

    // Check for repeated errors before continuing
    const errorCheck = await checkForRepeatedErrors(5);
    if (errorCheck?.shouldFail) {
      log('ERROR', 'Failing fast due to repeated errors during reassessment polling');
      try {
        await postComment(errorCheck.diagnosis);
      } catch {
        console.error(errorCheck.diagnosis);
      }
      return { choice: null, comment: null };
    }

    console.log(`Waiting for choice... ${elapsedMinutes}m elapsed, ${remainingMinutes}m remaining`);

    try {
      const comments = await fetchIssueComments(ISSUE_NUMBER, execCommand);

      // Find comments newer than lastSeenTimestamp
      const newComments = comments.filter(c => c.createdAt > lastSeenTimestamp);

      if (newComments.length > 0) {
        log('INFO', `Found ${newComments.length} new comment(s) since ${lastSeenTimestamp}`);

        for (const comment of newComments) {
          // Skip bot comments
          if (comment.author.includes('[bot]') || comment.author === 'github-actions') {
            continue;
          }

          log('INFO', `Checking comment from ${comment.author}: ${comment.body.substring(0, 50)}...`);

          // Try to parse as a reassessment command
          const choice = parseReassessmentResponse(comment.body);

          if (choice.action !== 'unknown') {
            log('INFO', `User choice detected: ${choice.action}`);
            console.log(`User choice: ${choice.action}`);
            return { choice, comment: comment.body };
          }
        }

        // Update timestamp to latest comment
        const latestComment = newComments[newComments.length - 1];
        lastSeenTimestamp = latestComment.createdAt || lastSeenTimestamp;
      }
    } catch (err) {
      const errorMessage = (err as Error).message;
      log('WARN', `Poll error: ${errorMessage}`);
      trackError(errorMessage);

      // Give immediate feedback if this looks serious
      const category = categorizeError(errorMessage);
      if (category !== 'unknown') {
        const pattern = errorPatterns.get(category);
        if (pattern && pattern.count >= 2) {
          console.log(`   ⚠️ Warning: "${category}" error occurred ${pattern.count} times`);
        }
      }
    }

    // Wait before next poll
    if (poll < maxPolls) {
      await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }

  // Timeout
  return { choice: null, comment: null };
}

async function runReassessmentWorkflow(
  issue: Issue,
  context: ReassessmentContext
): Promise<{ waitingForUser: boolean; lastCommitSha: string }> {
  const rules = loadRules();

  log('INFO', 'Running LLM-driven reassessment workflow');
  console.log('\n' + '='.repeat(60));
  console.log('RE-ASSESSMENT MODE - LLM-DRIVEN');
  console.log('='.repeat(60));
  console.log(`Found: ${context.childIssues.length} child issues`);
  console.log(`Completed: ${context.analysis.completedTasks.length} | Failed: ${context.analysis.failedTasks.length} | Blocked: ${context.analysis.blockedTasks.length} | Pending: ${context.analysis.pendingTasks.length}`);
  console.log('='.repeat(60) + '\n');

  // Get current commit SHA
  const currentCommit = await getLatestCommit();
  const startSha = currentCommit?.sha || '';

  // Build comprehensive prompt for LLM to analyze AND act
  const reassessmentPrompt = buildLLMReassessmentPrompt(issue, context, rules);

  // Run LLM - it will analyze, post summary, and take action
  await runQuery(reassessmentPrompt, 50);

  // Get commit SHA after changes
  const afterCommit = await getLatestCommit();
  const endSha = afterCommit?.sha || startSha;

  // Check state after execution
  const newState = loadState();
  const waitingForUser = newState?.waitingForUser ?? false;

  return { waitingForUser, lastCommitSha: endSha };
}

/**
 * Build a prompt for LLM-driven reassessment.
 * The LLM will analyze the situation, post a clear summary, and take action.
 */
function buildLLMReassessmentPrompt(
  issue: Issue,
  context: ReassessmentContext,
  rules: string
): string {
  const { analysis, childIssues, projectBoardItems } = context;

  // Format task status for LLM
  const formatTasks = (tasks: number[], items: typeof projectBoardItems) => {
    return tasks.map(num => {
      const item = items.find(i => i.issueNumber === num);
      return `  - #${num}: ${item?.title || 'Unknown'} (${item?.status || 'Unknown'})`;
    }).join('\n') || '  (none)';
  };

  // Format child issues
  const childIssuesList = childIssues.map(i =>
    `  - #${i.number}: ${i.title} [${i.state}] ${i.labels.join(', ')}`
  ).join('\n') || '  (none)';

  // Format board items with blockers
  const boardItemsList = projectBoardItems.map(i =>
    `  - #${i.issueNumber}: ${i.title} | Status: ${i.status || 'N/A'} | Blocked by: ${i.blockedBy || 'none'} | Agent: ${i.assignedAgent || 'none'}`
  ).join('\n') || '  (none)';

  return `You are @agent-pm, the AIDLC Workflow Orchestrator.

## SITUATION: Re-Assessment Required

I've been triggered on issue #${ISSUE_NUMBER} which has existing progress. I need to:
1. Analyze what's been done and what needs to happen next
2. Post a CLEAR, ACTIONABLE summary to the issue
3. Take appropriate action (trigger agents, unblock tasks, etc.)

## CURRENT STATE

**Issue**: #${ISSUE_NUMBER} - ${issue.title}
**Phase**: ${analysis.phase}

### Task Summary
- Completed: ${analysis.completedTasks.length}
- In Progress: ${analysis.inProgressTasks.length}
- Failed: ${analysis.failedTasks.length}
- Blocked: ${analysis.blockedTasks.length}
- Pending: ${analysis.pendingTasks.length}

### Completed Tasks
${formatTasks(analysis.completedTasks, projectBoardItems)}

### In Progress Tasks
${formatTasks(analysis.inProgressTasks, projectBoardItems)}

### Failed Tasks
${formatTasks(analysis.failedTasks, projectBoardItems)}

### Blocked Tasks
${formatTasks(analysis.blockedTasks, projectBoardItems)}

### Pending Tasks (ready to start)
${formatTasks(analysis.pendingTasks, projectBoardItems)}

### All Project Board Items
${boardItemsList}

### Child Issues
${childIssuesList}

${analysis.staleBlockers.length > 0 ? `
### ⚠️ Stale Blockers Detected
These tasks are blocked by issues that are already DONE - blockers need to be cleared:
${analysis.staleBlockers.map(sb => `  - #${sb.issueNumber} blocked by DONE issues: ${sb.doneBlockers.map(n => `#${n}`).join(', ')}`).join('\n')}
` : ''}

## YOUR TASK

1. **ANALYZE** the situation - what's the current state and what needs to happen?

2. **POST A SUMMARY** to issue #${ISSUE_NUMBER} using this format:
   \`\`\`
   ## 📊 PM Reassessment Summary

   ### Current Status
   [Brief 2-3 sentence summary of where things stand]

   ### What's Complete
   [List completed work with issue numbers]

   ### What's Next
   [Specific actions I'm taking now]

   ### Blockers/Issues (if any)
   [Any problems that need attention]
   \`\`\`

3. **TAKE ACTION** - Don't wait for user permission for obvious next steps:
   - If tasks have stale blockers (blocked by done issues), clear them
   - If tasks are ready (pending, no blockers), trigger the appropriate agent
   - If tasks failed, retry them or flag for human review
   - Update project board status as needed

## RULES FOR ACTION

### Assigning Agents (IMPORTANT - do BOTH steps)
When assigning an agent to a task:
1. **Set assigned_agent on project board**:
   \`\`\`bash
   # First get field IDs
   gh project field-list <project-num> --owner ${REPO_OWNER} --format json
   # Then set the field (use single-select-option-id for the agent)
   gh project item-edit --id "<item-id>" --project-id "<project-id>" --field-id "<assigned_agent-field-id>" --single-select-option-id "<agent-option-id>"
   \`\`\`
2. **Trigger the agent** by adding label: \`gh issue edit <num> --add-label "agent-<type>"\`

### Clearing Blockers
When a blocker completes, clear the blocked_by field:
\`gh project item-edit --id "<item-id>" --project-id "<project-id>" --field-id "<blocked_by-field-id>" --text ""\`

### Decision Making
- **Be proactive** - if the next step is obvious, just do it
- **Only ask user** if genuinely uncertain (e.g., conflicting requirements, need clarification)

## WORKFLOW RULES

${rules}

## GO

Analyze the situation, post a clear summary, and take action. Be decisive - don't wait for permission for obvious next steps.`;
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
      env: { ...process.env },
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
// Beads Integration
// ============================================================================

async function initializeBeadsIfNeeded(): Promise<boolean> {
  if (!isBeadsEnabled()) {
    log('INFO', 'Beads disabled via configuration');
    return false;
  }

  try {
    // Check if beads CLI is available
    if (!(await isBeadsAvailable())) {
      log('WARN', 'Beads CLI not available - falling back to GitHub Projects');
      return false;
    }

    // Initialize if needed
    if (!(await isBeadsInitialized(CWD))) {
      log('INFO', 'Initializing Beads for this project...');
      await setupBeadsForADP(CWD, {
        bucket: BEADS_S3_BUCKET,
        region: BEADS_S3_REGION,
        path: BEADS_S3_PATH,
      });
    } else {
      // Just sync
      await syncPull(CWD);
    }

    log('INFO', 'Beads initialized and synced');
    return true;
  } catch (error) {
    log('ERROR', `Beads initialization failed: ${(error as Error).message}`);
    return false;
  }
}

async function createBeadsProjectStructure(
  issue: Issue,
  tasks: Array<{ title: string; agent: string; priority?: number; blockedBy?: number[] }>
): Promise<BeadsEpic | null> {
  if (!isBeadsEnabled()) {
    return null;
  }

  try {
    const epic = await createProjectFromIssue(
      issue.number,
      issue.title,
      tasks,
      CWD
    );

    log('INFO', `Created Beads epic ${epic.id} with ${epic.tasks.length} tasks`);
    return epic;
  } catch (error) {
    log('ERROR', `Failed to create Beads project: ${(error as Error).message}`);
    return null;
  }
}

async function getBeadsReadyWork(): Promise<BeadsTask[]> {
  if (!isBeadsEnabled()) {
    return [];
  }

  try {
    const ready = await getReadyTasks(CWD);
    log('INFO', `Beads ready work: ${ready.length} tasks`);
    return ready;
  } catch (error) {
    log('WARN', `Failed to get Beads ready work: ${(error as Error).message}`);
    return [];
  }
}

async function syncBeadsState(): Promise<void> {
  if (!isBeadsEnabled()) {
    return;
  }

  try {
    await syncPush(CWD);
    log('INFO', 'Beads state synced to S3');
  } catch (error) {
    log('WARN', `Failed to sync Beads state: ${(error as Error).message}`);
  }
}

// ============================================================================
// Monitoring Integration
// ============================================================================

async function shouldEnterMonitoringMode(existingState: AIDLCState | null): Promise<boolean> {
  if (!isMonitoringEnabled()) {
    return false;
  }

  // Enter monitoring mode if:
  // 1. We have a project board (medium+ complexity)
  // 2. There are child issues assigned to agents
  // 3. User explicitly requested monitoring via /monitor command

  if (!existingState?.projectNumber) {
    return false;
  }

  // Check if there are tasks assigned to other agents
  try {
    const projectItems = await execCommand(
      `gh project item-list ${existingState.projectNumber} --owner ${REPO_OWNER} --format json --limit 100`
    );
    const items = JSON.parse(projectItems || '{"items":[]}');

    // Look for items assigned to non-PM agents
    for (const item of items.items || []) {
      const agent = item.assigned_agent || '';
      if (agent && agent !== '@agent-pm' && !agent.includes('pm')) {
        log('INFO', `Found agent assignment: ${agent} - entering monitoring mode`);
        return true;
      }
    }
  } catch (error) {
    log('WARN', `Could not check project items: ${(error as Error).message}`);
  }

  return false;
}

async function runMonitoringMode(
  issue: Issue,
  existingState: AIDLCState
): Promise<{ completed: boolean; reason: string }> {
  log('INFO', 'Entering proactive monitoring mode');

  console.log('\n' + '='.repeat(60));
  console.log('  PROACTIVE MONITORING MODE');
  console.log('  Tracking all triggered agents in real-time');
  console.log('  Goal-focused orchestration enabled');
  console.log('='.repeat(60) + '\n');

  // Build completion context for AI-driven orchestration
  let projectId = '';
  if (existingState.projectNumber) {
    try {
      const projectJson = await execCommand(
        `gh api graphql -f query='query($org: String!, $num: Int!) { organization(login: $org) { projectV2(number: $num) { id } } }' -f org="${REPO_OWNER}" -F num=${existingState.projectNumber} --jq '.data.organization.projectV2.id'`
      );
      projectId = projectJson.trim();
    } catch (e) {
      log('WARN', `Could not fetch project ID: ${(e as Error).message}`);
    }
  }

  const completionContext: CompletionContext = {
    repoOwner: REPO_OWNER,
    repoName: REPO_NAME,
    parentIssue: {
      number: issue.number,
      title: issue.title,
      body: issue.body,
    },
    projectNumber: existingState.projectNumber || 0,
    projectId,
  };

  // Post monitoring start message
  await postComment(`## PM Entering Monitoring Mode

I'm now actively monitoring all triggered agents for issue #${issue.number}.

**What I'll do:**
- Track workflow progress for each agent
- Detect completions, failures, and stuck agents
- Post status updates every 5 minutes
- Respond to your commands

**Available Commands:**
| Command | Description |
|---------|-------------|
| \`/queryPM <question>\` | Ask PM a question or change direction |
| \`/status\` | Get current monitoring status |
| \`/retry #N\` | Retry failed agent on issue #N |
| \`/instruct <msg>\` | Give PM a custom instruction |
| \`/pause\` | Pause monitoring |
| \`/resume\` | Resume monitoring |
| \`/stop\` | Stop monitoring |
| \`/extend\` | Extend session by 1 hour |

---
*Session timeout: 5 hours (use \`/extend\` to add time)*`);

  // Set up monitoring callbacks
  const callbacks: MonitoringCallbacks = {
    log,
    postComment,
    execCommand,
    onEvent: (event: MonitoringEvent) => {
      // Log events for debugging
      console.log(`[EVENT] ${event.type}: #${event.issueNumber} @agent-${event.agentType}`);
    },
    onCommand: async (cmd: UserCommand): Promise<boolean> => {
      // Handle /retry command
      if (cmd.command === '/retry' && cmd.args.length > 0) {
        const issueArg = cmd.args[0].replace('#', '');
        const issueNum = parseInt(issueArg, 10);

        if (isNaN(issueNum)) {
          await postComment(`Invalid issue number: ${cmd.args[0]}`);
          return true;
        }

        // Find the agent type for this issue from monitoring state
        const monState = loadMonitoringState(CWD);
        const agent = monState?.trackedAgents.find((a) => a.issueNumber === issueNum);

        if (!agent) {
          await postComment(`Issue #${issueNum} is not being tracked. Cannot retry.`);
          return true;
        }

        // Re-add the agent label to trigger workflow
        try {
          await execCommand(
            `gh issue edit ${issueNum} --add-label "agent-${agent.agentType}"`
          );
          await postComment(`Retrying @agent-${agent.agentType} on issue #${issueNum}...`);
        } catch (error) {
          await postComment(`Failed to retry: ${(error as Error).message}`);
        }

        return true;
      }

      return false; // Not handled
    },
    // AI-driven completion handler - orchestrates work toward the goal
    onAgentComplete: async (agent: TrackedAgent, state: MonitoringState): Promise<{ goalAchieved: boolean }> => {
      log('INFO', `Handling completion of @agent-${agent.agentType} on #${agent.issueNumber}`);

      if (!completionContext.projectNumber || !completionContext.projectId) {
        log('WARN', 'No project context available for completion handling');
        return { goalAchieved: false };
      }

      const result = await handleCompletionWithAI(
        agent,
        state,
        completionContext,
        { log, postComment, execCommand }
      );

      return { goalAchieved: result.goalAchieved };
    },
    // Handle user /instruct command
    onInstruct: async (instruction: string, state: MonitoringState): Promise<{ success: boolean }> => {
      log('INFO', `Received user instruction: ${instruction}`);

      if (!completionContext.projectNumber || !completionContext.projectId) {
        log('WARN', 'No project context available for instruction execution');
        await postComment('Cannot execute instruction: no project context available.');
        return { success: false };
      }

      const result = await executeUserInstruction(
        instruction,
        state,
        completionContext,
        { log, postComment, execCommand }
      );

      return { success: result.success };
    },
    // AI-driven periodic status analysis
    onAnalyze: async (state: MonitoringState, previousReport: string | null): Promise<{ actionTaken: boolean; newReport: string }> => {
      if (!completionContext.projectNumber || !completionContext.projectId) {
        log('WARN', 'No project context available for AI analysis');
        return { actionTaken: false, newReport: '' };
      }

      const result = await analyzeStatusWithAI(
        state,
        completionContext,
        previousReport,
        { log, postComment, execCommand }
      );

      return { actionTaken: result.actionTaken, newReport: result.newReport };
    },
    // Handle /queryPM command - human asks questions or changes direction
    onQueryPM: async (query: string, state: MonitoringState): Promise<{ response: string }> => {
      if (!completionContext.projectNumber || !completionContext.projectId) {
        return { response: 'Cannot process query: no project context available.' };
      }

      const result = await handleQueryPM(
        query,
        state,
        completionContext,
        { log, postComment, execCommand }
      );

      return result;
    },
  };

  // Run the monitoring loop
  const result = await startOrResumeMonitoring(
    parseInt(ISSUE_NUMBER, 10),
    existingState.projectNumber || null,
    REPO_OWNER,
    REPO_NAME,
    CWD,
    callbacks
  );

  log('INFO', `Monitoring completed: ${result.reason}`);

  return {
    completed: result.reason === 'all_complete',
    reason: result.reason,
  };
}

async function runAIDLCWorkflow(issue: Issue): Promise<{ waitingForUser: boolean; lastCommitSha: string }> {
  const rules = loadRules();
  const templates = loadTemplates();

  log('INFO', `Loaded ${rules.length} chars of rules, ${Object.keys(templates).length} templates`);

  // Get current commit SHA before we start
  const currentCommit = await getLatestCommit();
  const startSha = currentCommit?.sha || '';

  // Check if this is a continuation
  const existingState = loadState();
  const isResume = existingState !== null && existingState.waitingForUser;

  let prompt: string;

  if (isResume && existingState) {
    log('INFO', `Resuming workflow - Phase: ${existingState.phase}, Stage: ${existingState.stage}`);
    prompt = buildContinuePrompt(issue, existingState, rules);
  } else {
    log('INFO', 'Starting new AIDLC workflow');
    prompt = buildStartPrompt(issue, rules, templates);
  }

  console.log('\n' + '═'.repeat(60));
  console.log(isResume ? 'Continuing AIDLC Workflow' : 'Starting New AIDLC Workflow');
  console.log('═'.repeat(60) + '\n');

  await runQuery(prompt);

  // Check the state after the query to see if we're waiting for user
  const newState = loadState();
  const waitingForUser = newState?.waitingForUser ?? false;

  // Get the commit SHA after our changes
  const afterCommit = await getLatestCommit();
  const endSha = afterCommit?.sha || startSha;

  log('INFO', `Workflow step completed. Waiting for user: ${waitingForUser}`);

  return { waitingForUser, lastCommitSha: endSha };
}

// ============================================================================
// Adaptive Depth Analysis - Fast-path for simple tasks
// ============================================================================

type TaskDepth = 'quick' | 'simple' | 'standard' | 'complex';

interface DepthAssessment {
  depth: TaskDepth;
  reasoning: string;
  canExecuteDirectly: boolean;
  suggestedAgent?: string;
  estimatedEffort?: string;
}

/**
 * Perform quick assessment of task complexity to determine if we need full AIDLC
 * or can fast-track execution.
 */
async function assessTaskDepth(issue: Issue): Promise<DepthAssessment> {
  log('INFO', 'Performing adaptive depth analysis...');

  const prompt = `You are @agent-pm performing a QUICK assessment of a task to determine the appropriate workflow depth.

## Task
**Issue #${issue.number}: ${issue.title}**

${issue.body}

## Your Job
Quickly assess this task (use 3-5 tool calls max) to determine:
1. Is this a simple task that can be executed directly?
2. Or does it need full AIDLC planning?

## Quick Assessment Steps
1. **Scan the issue** - What type of work is this?
2. **Quick codebase check** - Use Glob/Grep to see if the change location is obvious
3. **Complexity signals** - Look for signs of simple vs complex work

## Task Type Indicators

**QUICK tasks** (execute immediately, no planning needed):
- Bug fix with clear error message and obvious fix location
- Typo or documentation fix
- Configuration change (env vars, settings)
- Version/dependency bump
- Single file change with clear instructions
- Revert a specific commit
- Enable/disable a feature flag

**SIMPLE tasks** (minimal planning, route to agent directly):
- Add a new endpoint following existing patterns
- Implement feature similar to existing code
- Deployment of already-built code
- Add tests for existing code
- Refactor with clear scope

**STANDARD tasks** (need some AIDLC, but skip some phases):
- New feature with clear requirements
- Multiple related changes
- Integration work

**COMPLEX tasks** (full AIDLC needed):
- Architectural changes
- New system/service
- Unclear requirements
- Multiple unknowns
- Cross-cutting concerns

## Response Format
After your quick assessment, respond with:

\`\`\`json
{
  "depth": "quick|simple|standard|complex",
  "reasoning": "Brief explanation of why this depth",
  "canExecuteDirectly": true/false,
  "suggestedAgent": "developer|operations|reviewer|architect|null",
  "estimatedEffort": "minutes|hours|days"
}
\`\`\`

Be decisive - bias toward simpler depths when the task is clear.`;

  try {
    let response = '';
    for await (const event of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: MODEL,
          cwd: CWD,
          maxTurns: 8, // Limited turns for quick assessment
          allowedTools: ['Bash', 'Read', 'Glob', 'Grep'],
          permissionMode: 'bypassPermissions',
        },
      },
      maxRetries: 2,
      baseDelayMs: 5000,
      log: (msg) => log('INFO', `[Depth Assessment] ${msg}`),
    })) {
      if (event.type === 'assistant' && event.message?.content) {
        for (const block of event.message.content) {
          if (block.type === 'text') {
            response += block.text;
          }
        }
      }
    }

    // Parse JSON from response
    const jsonMatch = response.match(/```json\s*([\s\S]*?)\s*```/);
    if (jsonMatch) {
      const assessment = JSON.parse(jsonMatch[1]) as DepthAssessment;
      log('INFO', `Depth assessment: ${assessment.depth} - ${assessment.reasoning}`);
      return assessment;
    }

    // Default to standard if parsing fails
    log('WARN', 'Could not parse depth assessment, defaulting to standard');
    return {
      depth: 'standard',
      reasoning: 'Could not parse assessment response',
      canExecuteDirectly: false,
    };
  } catch (error) {
    log('ERROR', `Depth assessment failed: ${(error as Error).message}`);
    return {
      depth: 'standard',
      reasoning: 'Assessment failed, using standard workflow',
      canExecuteDirectly: false,
    };
  }
}

/**
 * Execute a quick/simple task directly without full AIDLC ceremony
 */
async function executeQuickTask(issue: Issue, assessment: DepthAssessment): Promise<void> {
  log('INFO', `Executing quick task directly (depth: ${assessment.depth})`);

  const prompt = `You are @agent-pm executing a ${assessment.depth.toUpperCase()} task directly.

## Task
**Issue #${issue.number}: ${issue.title}**

${issue.body}

## Assessment
- **Depth**: ${assessment.depth}
- **Reasoning**: ${assessment.reasoning}
- **Suggested Agent**: ${assessment.suggestedAgent || 'you (PM)'}
- **Estimated Effort**: ${assessment.estimatedEffort || 'unknown'}

## Your Job
This is a ${assessment.depth} task - execute it directly without full AIDLC ceremony.

${assessment.depth === 'quick' ? `
### QUICK TASK EXECUTION
1. **Understand** - Read relevant files to understand the change needed
2. **Execute** - Make the change directly using Edit/Write tools
3. **Verify** - Run any relevant tests or checks
4. **Commit** - Commit with clear message referencing the issue
5. **Create PR** - Create a PR for review (unless trivial like typo fix)
6. **Report** - Post completion summary to the issue

Do NOT create AIDLC docs. Just do the work.
` : `
### SIMPLE TASK EXECUTION
1. **Quick Research** - Understand the codebase patterns (2-3 tool calls)
2. **Plan Briefly** - Identify files to change (no formal doc needed)
3. **Execute** - Make the changes
4. **Test** - Run relevant tests
5. **Create PR** - Create PR with good description
6. **Report** - Post completion summary to the issue

OR if you determine this needs a specialist agent:
- Create a child issue assigned to the right agent
- Add the appropriate label (agent-developer, agent-operations, etc.)
- Post a brief comment explaining what you've set up
`}

## Tools Available
- **Bash**: git, gh CLI, npm/yarn, any shell commands
- **Read/Write/Edit**: File operations
- **Glob/Grep**: Search codebase
- **WebSearch/WebFetch**: Research if needed

## Completion
When done, post a comment to issue #${issue.number} with:
\`\`\`
## ✅ Quick Task Complete

**What was done**: [Brief summary]
**Changes**: [Files changed or PR link]
**Verification**: [How it was tested]
\`\`\`

Then close the issue if the work is complete, or explain next steps if follow-up is needed.

Execute the task now.`;

  await postComment(`## ⚡ Quick Task Detected

**Assessment**: ${assessment.depth.toUpperCase()} task
**Reasoning**: ${assessment.reasoning}
**Estimated Effort**: ${assessment.estimatedEffort || 'minimal'}

Executing directly without full AIDLC workflow...`);

  await runQuery(prompt, 50);

  log('INFO', 'Quick task execution completed');
}

/**
 * Route a simple task to the appropriate agent
 */
async function routeToAgent(issue: Issue, assessment: DepthAssessment): Promise<void> {
  const agent = assessment.suggestedAgent || 'developer';
  log('INFO', `Routing simple task to @agent-${agent}`);

  await postComment(`## 🚀 Fast-Track Routing

**Assessment**: ${assessment.depth.toUpperCase()} task
**Reasoning**: ${assessment.reasoning}
**Routing to**: @agent-${agent}

This task is straightforward enough to skip full AIDLC planning.
Triggering @agent-${agent} directly...`);

  // Add the agent label to trigger the workflow
  await gh(`issue edit ${issue.number} --add-label "agent-${agent}"`);

  log('INFO', `Routed to @agent-${agent}`);
}

// ============================================================================
// Main
// ============================================================================

async function main(): Promise<void> {
  console.log('');
  console.log('═'.repeat(60));
  console.log('  @agent-pm - AIDLC Workflow Orchestrator');
  console.log('═'.repeat(60));
  console.log('');

  await initCloudWatch();

  // Initialize Beads for distributed state management
  const beadsAvailable = await initializeBeadsIfNeeded();
  if (beadsAvailable) {
    log('INFO', 'Beads state management active');
  } else {
    log('INFO', 'Using GitHub Projects for state management (Beads not available)');
  }

  // Get bd prime context for AI-optimized workflow guidance
  beadsPrimeContext = await getBeadsPrimeContext(CWD);

  // Initialize agent memory system
  configureMemory({
    cwd: CWD,
    agentType: 'pm',
    issueNumber: ISSUE_NUMBER,
    log,
  });

  let pmSucceeded = false;

  try {
    await ensureAdpBranch();
  } catch (err) {
    log('WARN', `Memory: failed to ensure adp branch: ${(err as Error).message}`);
  }

  try {
    const issue = await getIssue();
    log('INFO', `Processing issue #${issue.number}: ${issue.title}`);

    // Load agent memory context from adp branch (best-effort)
    try {
      const component = detectComponent(issue.labels || [], issue.body);
      const componentCtx = await readComponentContext(component);
      const agentCtx = await readAgentContext('pm');
      agentMemoryContext = formatContextForPrompt(componentCtx, agentCtx, component, 'pm');
      if (agentMemoryContext) {
        log('INFO', `Loaded memory context: ${componentCtx.length} component records, ${agentCtx.length} agent records`);
      }
    } catch (err) {
      log('WARN', `Memory: failed to load context: ${(err as Error).message}`);
    }

    // CRITICAL: Check if an AIDLC branch exists for this issue and checkout if found
    // This ensures we can load state from previous workflow runs
    const aidlcBranchResult = await checkoutAIDLCBranchIfExists();
    if (aidlcBranchResult.found) {
      log('INFO', `Working on AIDLC branch: ${aidlcBranchResult.branch}`);
    }

    // Check for existing state and reassessment needs
    const existingState = loadState();
    const isResume = existingState !== null && existingState.waitingForUser;

    // Check if user explicitly requested monitoring mode via comment
    const recentComments = await fetchIssueComments(ISSUE_NUMBER, execCommand);
    const hasMonitorCommand = recentComments.some(
      (c) => !c.author.includes('[bot]') && c.body.toLowerCase().includes('/monitor')
    );

    // Check if we should go directly to monitoring mode
    if (hasMonitorCommand && existingState?.projectNumber) {
      log('INFO', 'User requested monitoring mode via /monitor command');
      await postComment(`Acknowledged \`/monitor\` command. Entering monitoring mode...`);

      const monitoringResult = await runMonitoringMode(issue, existingState);
      log('INFO', `Monitoring completed: ${monitoringResult.reason}`);
      return; // Exit after monitoring
    }

    // Check if we need reassessment (existing progress but not a normal resume)
    const reassessmentResult = await checkForReassessment(issue, existingState);
    const isReassessment = reassessmentResult.needsReassessment;

    // =========================================================================
    // ADAPTIVE DEPTH ANALYSIS - Fast-path for simple tasks
    // =========================================================================
    // Only for NEW issues (not resumes, reassessments, or monitoring)
    if (!existingState && !isReassessment) {
      log('INFO', 'New issue detected - performing adaptive depth analysis');

      await postComment(`## 🔍 @agent-pm Analyzing Task

**Issue**: #${issue.number} - ${issue.title}

Performing quick assessment to determine the right workflow depth...`);

      const depthAssessment = await assessTaskDepth(issue);

      log('INFO', `Depth assessment result: ${depthAssessment.depth}`);

      // Handle quick and simple tasks with fast-path
      if (depthAssessment.depth === 'quick') {
        log('INFO', 'QUICK task - executing directly');
        await executeQuickTask(issue, depthAssessment);
        log('INFO', 'Quick task completed - exiting');
        return; // Exit after quick task
      }

      if (depthAssessment.depth === 'simple' && depthAssessment.canExecuteDirectly) {
        log('INFO', 'SIMPLE task - routing to agent or executing');
        if (depthAssessment.suggestedAgent && depthAssessment.suggestedAgent !== 'pm') {
          await routeToAgent(issue, depthAssessment);
          log('INFO', 'Simple task routed - exiting');
          return; // Exit after routing
        } else {
          // PM can handle it directly
          await executeQuickTask(issue, depthAssessment);
          log('INFO', 'Simple task completed - exiting');
          return;
        }
      }

      // For standard/complex tasks, continue with full AIDLC
      log('INFO', `${depthAssessment.depth.toUpperCase()} task - proceeding with AIDLC workflow`);
      await postComment(`## 📋 Full AIDLC Workflow Required

**Assessment**: ${depthAssessment.depth.toUpperCase()} task
**Reasoning**: ${depthAssessment.reasoning}

This task requires structured planning. Proceeding with AIDLC workflow...`);
    }
    // =========================================================================

    // Determine mode for acknowledgment
    let modeLabel = 'Starting';
    let modeDescription = `
I'm analyzing this request and will:
1. Assess the complexity
2. Provide my recommendations
3. Create the AIDLC documentation structure
4. Create a project board if needed
5. Wait for your responses (15 min, then auto-proceed with recommendations)

**Please wait** - I'll post another comment when I'm ready for your input.
`;

    if (isReassessment) {
      modeLabel = 'Re-assessing';
      const ctx = reassessmentResult.context!;
      modeDescription = `
I detected existing progress on this issue. Analyzing the situation...

**Quick Stats:**
- Completed: ${ctx.analysis.completedTasks.length} | In Progress: ${ctx.analysis.inProgressTasks.length} | Pending: ${ctx.analysis.pendingTasks.length}

I'll analyze what needs to happen next and take action.
`;
    } else if (isResume) {
      modeLabel = 'Continuing';
      modeDescription = `
I detected your changes and am continuing the workflow.
`;
    }

    await postComment(`## 🤖 @agent-pm ${modeLabel}

**Issue**: #${issue.number} - ${issue.title}
**Mode**: ${modeLabel}${isResume ? ` (Phase: ${existingState?.phase}, Stage: ${existingState?.stage})` : ''}
**Started**: ${new Date().toISOString()}

${modeDescription}

---
*Watch the [workflow logs](https://github.com/${REPO_OWNER}/${REPO_NAME}/actions) for detailed progress.*`);

    // Note: For reassessment, the LLM will post its own analysis in runReassessmentWorkflow
    // No need to post a template here

    // Run the workflow loop
    let continueLoop = true;
    let iteration = 0;
    const maxIterations = 10; // Safety limit
    let isFirstIteration = true;
    let justCompletedReassessment = false;

    while (continueLoop && iteration < maxIterations) {
      iteration++;
      log('INFO', `Workflow iteration ${iteration}`);

      // On first iteration, check if we should run reassessment
      let result: { waitingForUser: boolean; lastCommitSha: string };

      if (isFirstIteration && isReassessment && reassessmentResult.context) {
        // Run reassessment workflow
        log('INFO', 'Running reassessment workflow');
        result = await runReassessmentWorkflow(issue, reassessmentResult.context);
        isFirstIteration = false;
        justCompletedReassessment = true;
      } else {
        // Run normal AIDLC workflow step
        result = await runAIDLCWorkflow(issue);
        isFirstIteration = false;
        justCompletedReassessment = false;
      }

      if (result.waitingForUser) {
        // Poll for user response
        log('INFO', 'Workflow waiting for user input, starting polling...');

        const pollResult = await pollForUserResponse(result.lastCommitSha);

        if (pollResult.shouldContinue) {
          log('INFO', `User response detected: ${pollResult.commitMessage}`);
          // Continue the loop to process the user's response
          continueLoop = true;
        } else {
          // Timeout - proceed with recommendations instead of stopping
          log('INFO', 'Polling timeout reached - proceeding with recommendations');

          await postComment(`## ⏱️ @agent-pm Auto-Proceeding

No response received within 15 minutes. **Proceeding with my recommendations.**

All questions in the plan file had recommended answers - I'm now continuing the workflow using those recommendations.

If you want to make changes later:
1. Review the generated artifacts in \`aidlc-docs/\`
2. Create a new issue or comment with adjustments needed
3. Add the \`agent-pm\` label to trigger a reassessment

---
*Continuing with recommended answers...*`);

          // Continue the loop - next iteration will use the pre-filled recommendations
          log('INFO', 'Continuing workflow with pre-filled recommendations');
          continueLoop = true;
        }
      } else {
        // Workflow complete or not waiting for user
        log('INFO', 'Workflow step complete, not waiting for user');

        // After reassessment, check if there's ready work that needs agent assignment
        if (justCompletedReassessment) {
          const readyWork = await getBeadsReadyWork();
          if (readyWork.length > 0) {
            log('INFO', `Reassessment complete, ${readyWork.length} tasks ready - continuing to assign agents`);

            // Post a message about continuing to assign agents
            await postComment(`## Continuing to Assign Agents

Reassessment complete. Found **${readyWork.length} ready task(s)** in Beads:
${readyWork.map(t => `- \`${t.id}\`: ${t.title}`).join('\n')}

Now proceeding to assign agents to ready work...`);

            // Continue the loop - next iteration will run runAIDLCWorkflow
            continueLoop = true;
            continue;
          } else {
            log('INFO', 'Reassessment complete, no ready work found');
          }
        }

        // Check if we should enter monitoring mode
        const latestState = loadState();
        if (latestState && await shouldEnterMonitoringMode(latestState)) {
          log('INFO', 'Entering monitoring mode for multi-agent orchestration');

          const monitoringResult = await runMonitoringMode(issue, latestState);

          if (monitoringResult.completed) {
            log('INFO', 'All agents completed successfully');
            await postComment(`## All Agents Complete

All triggered agents have completed their work.

**Next Steps:**
- Review the changes made by each agent
- Check for any follow-up tasks
- Add \`agent-pm\` label if more orchestration needed`);
          } else {
            log('INFO', `Monitoring ended: ${monitoringResult.reason}`);
          }
        }

        continueLoop = false;
      }
    }

    if (iteration >= maxIterations) {
      log('WARN', 'Reached maximum workflow iterations');
    }

    log('INFO', 'Workflow completed successfully');
    pmSucceeded = true;

    // Sync Beads state to S3
    await syncBeadsState();

  } catch (error) {
    const err = error as Error;
    log('ERROR', `Fatal error: ${err.message}`);

    await postComment(`## ❌ @agent-pm Error

The workflow encountered an error. Please check the [workflow logs](https://github.com/${REPO_OWNER}/${REPO_NAME}/actions) for details.

**Error**: ${err.message}

**To retry**: Add the \`agent-pm\` label again.`);

    throw error;
  } finally {
    // Write agent memory context to adp branch (best-effort, never blocks)
    try {
      const issue = await getIssue().catch(() => null);
      if (issue) {
        const component = detectComponent(issue.labels || [], issue.body);
        const memStatus = pmSucceeded ? 'success' : 'failed';
        await writeComponentRecord(component, buildComponentRecord({
          issueNumber: ISSUE_NUMBER,
          issueTitle: issue.title,
          component,
          agentType: 'pm',
          status: memStatus,
          summary: `PM processed issue #${ISSUE_NUMBER}: ${issue.title}`,
        }));
        await writeAgentRecord('pm', buildAgentRecord({
          issueNumber: ISSUE_NUMBER,
          issueTitle: issue.title,
          agentType: 'pm',
          component,
          status: memStatus,
          oneLiner: `Orchestrated issue #${ISSUE_NUMBER}: ${issue.title}`,
        }));
      }
    } catch (memErr) {
      log('WARN', `Memory: failed to write context: ${(memErr as Error).message}`);
    }

    clearInterval(cwFlushTimer);
    await flushCloudWatch();
  }
}

main()
  .then(() => {
    console.log('Agent PM completed successfully');
    process.exit(0);
  })
  .catch((err) => {
    console.error('Fatal error:', err);
    process.exit(1);
  });
