/**
 * @agent-pt-superpower - Superpowers Workflow Agent
 *
 * A prototype agent that uses the obra/superpowers workflow skills.
 * This agent supports interactive brainstorming with the user via GitHub comments.
 *
 * Flow:
 * 1. Load Superpowers skills from .claude/skills/
 * 2. Brainstorming phase - post questions, wait for user response
 * 3. Planning phase - create detailed implementation plan
 * 4. Implementation phase - execute with TDD approach
 * 5. Create PR for review
 */

import { resilientQuery } from './utils/resilientQuery';
import { buildSuperpowerBrainstormingPrompt, buildSuperpowerImplementationPrompt } from './utils/superpower-prompts';
import { CommentAuthorityCache } from './utils/comment-authority';
import { renderSuperpowerContext, resolveSuperpowerTriggerComment, selectSuperpowerGuidance, type SelectedGuidance } from './utils/superpower-guidance';
import { buildSummaryPrompt } from './utils/superpower-summary-prompt';
import { CloudWatchLogsClient, PutLogEventsCommand, CreateLogStreamCommand } from '@aws-sdk/client-cloudwatch-logs';
import * as fs from 'fs';
import * as path from 'path';
import { execSync } from 'child_process';
import { randomUUID } from 'crypto';
import { bindPostedApproval, fetchApprovalComments, fetchApprovalPlan, postApprovalPlan, waitForPostedApproval } from './services/posted-approval';
import { refreshGitHubToken, saveToS3Fallback } from './utils/ghPost';
import { resolveAgentLogGroup } from './lib/logGroup';

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
const SUPERPOWERS_SKILLS_PATH = process.env.SUPERPOWERS_SKILLS_PATH || path.join(CWD, '.claude', 'skills');

// Project folder for generated code
function generateProjectFolderName(issueTitle: string, issueNumber: number): string {
  // Sanitize title: lowercase, replace spaces/special chars with hyphens, limit length
  const sanitized = issueTitle
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .substring(0, 30);
  return `projects/${sanitized}-${issueNumber}`;
}

// Timeout for user response during brainstorming (15 minutes)
const USER_RESPONSE_TIMEOUT_MS = 15 * 60 * 1000;
const POLL_INTERVAL_MS = 30_000; // 30 seconds

// ============================================================================
// CloudWatch Logging
// ============================================================================

const LOG_GROUP = resolveAgentLogGroup();
const LOG_STREAM = `agent-superpower-issue-${ISSUE_NUMBER}-${Date.now()}`;
const cwClient = new CloudWatchLogsClient({ region: AWS_REGION });
let cwBuffer: { timestamp: number; message: string }[] = [];
let cwInitialized = false;

async function initCloudWatch(): Promise<void> {
  try {
    await cwClient.send(new CreateLogStreamCommand({
      logGroupName: LOG_GROUP,
      logStreamName: LOG_STREAM,
    }));
    cwInitialized = true;
    log('INFO', 'CloudWatch logging initialized for @agent-pt-superpower');
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
    agentType: 'superpower',
    ...context,
    timestamp: new Date().toISOString(),
  };
  const line = JSON.stringify(entry);

  const emoji = level === 'ERROR' ? '❌' : level === 'WARN' ? '⚠️' : '→';
  console.log(`${emoji} [superpower] ${message}`);

  if (cwInitialized) {
    cwBuffer.push({ timestamp: Date.now(), message: line });
  }
}

async function flushCloudWatch(): Promise<void> {
  if (!cwInitialized || cwBuffer.length === 0) return;

  try {
    await cwClient.send(new PutLogEventsCommand({
      logGroupName: LOG_GROUP,
      logStreamName: LOG_STREAM,
      logEvents: cwBuffer.map((e) => ({ timestamp: e.timestamp, message: e.message })),
    }));
    cwBuffer = [];
  } catch (err) {
    console.warn('CloudWatch flush failed:', (err as Error).message);
  }
}

// ============================================================================
// GitHub Helpers
// ============================================================================

function execCommand(cmd: string): string {
  try {
    return execSync(cmd, {  // nosemgrep: detect-child-process
      cwd: CWD,
      encoding: 'utf-8',
      env: {
        ...process.env,
        GH_TOKEN: GH_APP_TOKEN || GITHUB_TOKEN,
        GITHUB_TOKEN: GH_APP_TOKEN || GITHUB_TOKEN,
      },
      maxBuffer: 10 * 1024 * 1024,
    }).trim();
  } catch (err) {
    const error = err as { stderr?: string; message: string };
    throw new Error(error.stderr || error.message);
  }
}

interface Issue {
  number: number;
  title: string;
  body: string;
  labels: string[];
  comments: Comment[];
  previousContext: string | null;
  sessionStart: number;
  triggerCommentId?: string;
  runId: string;
  authorityCache: CommentAuthorityCache;
}

async function getIssue(sessionStart: number, triggerCommentId?: string): Promise<Issue> {
  const result = execCommand(`gh issue view ${ISSUE_NUMBER} --repo ${REPO_OWNER}/${REPO_NAME} --json number,title,body,labels,comments`);
  const data = JSON.parse(result);

  // Parse comments
  const comments: Comment[] = (data.comments || []).map((c: { author: { login: string }; body: string; createdAt: string }) => ({
    author: c.author?.login || 'unknown',
    body: c.body || '',
    createdAt: c.createdAt || '',
  }));

  // Extract previous implementation context (agent summaries, approved designs)
  const previousContext = extractPreviousContext(comments);

  log('INFO', `Loaded issue with ${comments.length} comments`);
  if (previousContext) log('INFO', `Found previous context (${previousContext.length} chars)`);

  return {
    number: data.number,
    title: data.title,
    body: data.body || '',
    labels: (data.labels || []).map((l: { name: string }) => l.name),
    comments,
    previousContext,
    sessionStart,
    triggerCommentId,
    runId: process.env.ADP_MESSAGE_ID || randomUUID(),
    authorityCache: new CommentAuthorityCache(),
  };
}

function getTriggerCommentId(): string | undefined {
  if (!process.env.GITHUB_EVENT_PATH) return undefined;
  try {
    const event = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8')) as unknown;
    return resolveSuperpowerTriggerComment(event, REPO_OWNER, REPO_NAME, Number(ISSUE_NUMBER));
  } catch {
    return undefined;
  }
}

async function getCurrentGuidance(issue: Issue): Promise<SelectedGuidance | null> {
  try {
    await refreshGitHubToken();
    const token = process.env.GH_TOKEN || process.env.GITHUB_TOKEN || GH_APP_TOKEN || GITHUB_TOKEN;
    const effect = (process.env.TARGET_REPO || `${REPO_OWNER}/${REPO_NAME}`).split('/');
    if (!token || effect.length !== 2 || !effect[0] || !effect[1]) return null;
    const comments = await fetchApprovalComments(REPO_OWNER, REPO_NAME, issue.number, token);
    return selectSuperpowerGuidance(comments, {
      owner: REPO_OWNER, repo: REPO_NAME, runId: issue.runId, cache: issue.authorityCache,
      effectRepo: { owner: effect[0], repo: effect[1] },
      tokenProvider: async () => {
        await refreshGitHubToken();
        return process.env.GH_TOKEN || process.env.GITHUB_TOKEN || GH_APP_TOKEN || GITHUB_TOKEN;
      },
    }, issue.sessionStart, issue.triggerCommentId);
  } catch {
    log('WARN', 'Guidance could not be verified');
    return null;
  }
}

/**
 * Extract previous implementation context from agent comments.
 * Looks for agent summaries with "What Was Built", approved designs, etc.
 */
function extractPreviousContext(comments: Comment[]): string | null {
  const contextMarkers = [
    '### 🔧 What Was Built',
    '### What Was Built',
    '## Implementation Summary',
    '## 📁 Project Structure',
    '### Approved Design',
    '## Plan:',
    'PLAN_START',
  ];

  // Find agent comments with context markers
  const contextComments = comments.filter(c => {
    if (!c.author.includes('[bot]') && c.author !== 'github-actions' && c.author !== 'adp-agent-ops') return false;
    return contextMarkers.some(marker => c.body.includes(marker));
  });

  if (contextComments.length === 0) return null;

  // Return the most recent context (likely the most complete)
  const latest = contextComments[contextComments.length - 1];
  return latest.body;
}

async function postComment(body: string): Promise<void> {
  try {
    await refreshGitHubToken();
    const escapedBody = body.replace(/'/g, "'\\''");
    execCommand(`gh issue comment ${ISSUE_NUMBER} --repo ${REPO_OWNER}/${REPO_NAME} --body '${escapedBody}'`);
    log('INFO', `Posted comment (${body.length} chars)`);
  } catch (err) {
    log('WARN', `GitHub post failed, saving to S3: ${(err as Error).message}`);
    await saveToS3Fallback(ISSUE_NUMBER, 'comment', body);
  }
}

interface Comment {
  author: string;
  body: string;
  createdAt: string;
}

async function fetchIssueComments(): Promise<Comment[]> {
  const result = execCommand(`gh issue view ${ISSUE_NUMBER} --repo ${REPO_OWNER}/${REPO_NAME} --json comments --jq '.comments | .[-50:]'`);
  const comments = JSON.parse(result || '[]');
  return comments.map((c: { author: { login: string }; body: string; createdAt: string }) => ({
    author: c.author?.login || 'unknown',
    body: c.body || '',
    createdAt: c.createdAt || '',
  }));
}

// ============================================================================
// Superpowers Skills Loading
// ============================================================================

function loadSuperpowersSkills(): string {
  const skills: string[] = [];

  if (!fs.existsSync(SUPERPOWERS_SKILLS_PATH)) {
    log('WARN', `Superpowers skills path not found: ${SUPERPOWERS_SKILLS_PATH}`);
    return '';
  }

  log('INFO', `Loading Superpowers skills from ${SUPERPOWERS_SKILLS_PATH}`);

  const skillDirs = fs.readdirSync(SUPERPOWERS_SKILLS_PATH, { withFileTypes: true })
    .filter(dirent => dirent.isDirectory())
    .map(dirent => dirent.name);

  for (const skillDir of skillDirs) {
    const skillFile = path.join(SUPERPOWERS_SKILLS_PATH, skillDir, 'SKILL.md');
    if (fs.existsSync(skillFile)) {
      const skillContent = fs.readFileSync(skillFile, 'utf-8');
      skills.push(`## Superpowers Skill: ${skillDir}\n${skillContent}`);
    }
  }

  log('INFO', `Loaded ${skillDirs.length} Superpowers skills`);
  return skills.join('\n\n---\n\n');
}

// ============================================================================
// Host-owned plan approval
// ============================================================================

interface PollResult {
  shouldContinue: boolean;
  reason: 'approved' | 'rejected' | 'timeout' | 'error';
  userResponse?: string;
}

async function pollForUserResponse(phase: string, planBody: string): Promise<PollResult> {
  if (!planBody.trim()) return { shouldContinue: false, reason: 'error' };
  const issueNumber = Number(ISSUE_NUMBER);
  const requestId = randomUUID();
  const runId = process.env.ADP_INVOCATION_ID || randomUUID();
  const body = `${planBody}\n\n---\n**To approve:** Comment \`/approve ${requestId}\`\n**To reject:** Comment \`/reject ${requestId} [feedback]\``;
  const tokenProvider = async () => {
    await refreshGitHubToken();
    return process.env.GH_TOKEN || process.env.GITHUB_TOKEN || GH_APP_TOKEN || GITHUB_TOKEN;
  };
  try {
    const token = await tokenProvider();
    const receipt = await postApprovalPlan(REPO_OWNER, REPO_NAME, issueNumber, body, token);
    const request = bindPostedApproval(requestId, runId, REPO_OWNER, REPO_NAME,
      issueNumber, body, receipt, USER_RESPONSE_TIMEOUT_MS);
    if (!request) return { shouldContinue: false, reason: 'error' };
    const target = (process.env.TARGET_REPO || `${REPO_OWNER}/${REPO_NAME}`).split('/');
    if (target.length !== 2 || !target[0] || !target[1]) return { shouldContinue: false, reason: 'error' };
    const context = { owner: REPO_OWNER, repo: REPO_NAME, runId, tokenProvider,
      effectRepo: { owner: target[0], repo: target[1] } };
    log('INFO', `Waiting for current ${phase} request ${requestId}`);
    const result = await waitForPostedApproval(request,
      async () => fetchApprovalComments(REPO_OWNER, REPO_NAME, issueNumber, await tokenProvider()),
      async () => fetchApprovalPlan(REPO_OWNER, REPO_NAME, issueNumber, request.planCommentId, await tokenProvider()),
      context, POLL_INTERVAL_MS, Math.ceil(USER_RESPONSE_TIMEOUT_MS / POLL_INTERVAL_MS), 3);
    return { shouldContinue: result.outcome === 'allowed-once',
      reason: result.outcome === 'allowed-once' ? 'approved'
        : result.outcome === 'unavailable' ? 'error'
          : result.approver ? 'rejected' : 'timeout',
      userResponse: 'comment' in result ? result.comment : undefined };
  } catch {
    log('WARN', 'Approval plan or comments unavailable');
    return { shouldContinue: false, reason: 'error' };
  }
}

// ============================================================================
// Detailed Message Logging (same as agent-worker.ts)
// ============================================================================

function logMessage(message: { type: string; message: { content: Array<Record<string, unknown>> } }, turnCount: number): string {
  let text = '';
  const toolsUsed: string[] = [];
  console.log(`\n${'─'.repeat(60)}`);
  console.log(`[superpower] Turn ${turnCount}`);
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
      } else if (toolName === 'Skill') {
        console.log(`⚡ Skill: ${input.skill_name || input.name || 'unknown'}`);
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
// Phase Execution
// ============================================================================

async function runBrainstormingPhase(issue: Issue, skills: string): Promise<{ approved: boolean; design: string }> {
  log('INFO', 'Starting brainstorming phase');

  const guidance = await getCurrentGuidance(issue);
  const contextSection = renderSuperpowerContext(guidance, issue.previousContext);

  const prompt = buildSuperpowerBrainstormingPrompt(issue, skills, contextSection);

  let designOutput = '';
  let turnCount = 0;

  console.log('\n' + '═'.repeat(60));
  console.log('🧠 BRAINSTORMING PHASE');
  console.log('═'.repeat(60));

  for await (const message of resilientQuery({
    queryParams: {
      prompt,
      options: {
        model: MODEL,
        cwd: CWD,
        allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch'],
        permissionMode: 'bypassPermissions',
        maxTurns: 20,
      }
    },
    maxRetries: 3,
    baseDelayMs: 5000,
    maxDelayMs: 60000,
    log: (msg) => log('WARN', msg),
  })) {
    switch (message.type) {
      case 'assistant': {
        turnCount++;
        designOutput += logMessage(
          message as unknown as { type: string; message: { content: Array<Record<string, unknown>> } },
          turnCount
        );
        break;
      }

      case 'result': {
        const res = message as { subtype?: string; total_cost_usd?: number; num_turns?: number; duration_ms?: number };
        if (res.subtype === 'success') {
          const msg = `✅ Brainstorming completed — ${res.num_turns} turns, $${res.total_cost_usd?.toFixed(4) || '?'}, ${((res.duration_ms || 0) / 1000).toFixed(1)}s`;
          console.log(msg);
          log('INFO', msg, { phase: 'brainstorming', cost: res.total_cost_usd, turns: res.num_turns });
        } else {
          const msg = `⚠️  Brainstorming ended: ${res.subtype}`;
          console.log(msg);
          log('WARN', msg, { phase: 'brainstorming', subtype: res.subtype });
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
          log('INFO', 'Brainstorming session initialized', { model: sys.model, tools: sys.tools });
        }
        break;
      }
    }
  }

  // Wait for user response
  const pollResult = await pollForUserResponse('brainstorming', designOutput);

  if (!pollResult.shouldContinue) return { approved: false, design: '' };

  return { approved: true, design: designOutput };
}

async function runImplementationPhase(issue: Issue, skills: string, design: string, projectFolder: string): Promise<string> {
  log('INFO', 'Starting implementation phase');
  log('INFO', `Project folder: ${projectFolder}`);

  // Ensure project folder exists
  const projectPath = path.join(CWD, projectFolder);
  if (!fs.existsSync(projectPath)) {
    fs.mkdirSync(projectPath, { recursive: true });
    log('INFO', `Created project folder: ${projectPath}`);
  }

  const guidance = await getCurrentGuidance(issue);
  const contextSection = renderSuperpowerContext(guidance, issue.previousContext);

  const prompt = buildSuperpowerImplementationPrompt(issue, skills, design, projectFolder, contextSection);

  let implementationOutput = '';
  let turnCount = 0;
  let lastActivityTime = Date.now();

  console.log('\n' + '═'.repeat(60));
  console.log('🔨 IMPLEMENTATION PHASE');
  console.log(`📁 Project folder: ${projectFolder}`);
  console.log('═'.repeat(60));

  // Heartbeat to show activity during long operations
  const heartbeat = setInterval(() => {
    const idleSeconds = Math.round((Date.now() - lastActivityTime) / 1000);
    if (idleSeconds > 30) {
      console.log(`💓 Still working... (${idleSeconds}s since last activity, turn ${turnCount})`);
    }
  }, 30_000);

  try {
    for await (const message of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: MODEL,
          cwd: CWD,
          allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill'],
          settingSources: ['project'],
          permissionMode: 'bypassPermissions',
          maxTurns: 50,
        }
      },
      maxRetries: 5,
      baseDelayMs: 10000,
      maxDelayMs: 120000,
      log: (msg) => log('WARN', msg),
    })) {
      lastActivityTime = Date.now();

      switch (message.type) {
        case 'assistant': {
          turnCount++;
          implementationOutput += logMessage(
            message as unknown as { type: string; message: { content: Array<Record<string, unknown>> } },
            turnCount
          );
          break;
        }

        case 'result': {
          const res = message as { subtype?: string; total_cost_usd?: number; num_turns?: number; duration_ms?: number };
          if (res.subtype === 'success') {
            const msg = `✅ Implementation completed — ${res.num_turns} turns, $${res.total_cost_usd?.toFixed(4) || '?'}, ${((res.duration_ms || 0) / 1000).toFixed(1)}s`;
            console.log(msg);
            log('INFO', msg, { phase: 'implementation', cost: res.total_cost_usd, turns: res.num_turns });
          } else {
            const msg = `⚠️  Implementation ended: ${res.subtype}`;
            console.log(msg);
            log('WARN', msg, { phase: 'implementation', subtype: res.subtype });
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
            log('INFO', 'Implementation session initialized', { model: sys.model, tools: sys.tools });
          }
          break;
        }
      }
    }
  } finally {
    clearInterval(heartbeat);
  }

  log('INFO', 'Implementation phase complete', { turns: turnCount });
  return implementationOutput;
}

// ============================================================================
// Main
// ============================================================================

async function main(): Promise<void> {
  console.log('');
  console.log('═'.repeat(60));
  console.log('  @agent-pt-superpower - Superpowers Workflow');
  console.log('═'.repeat(60));
  console.log('');

  await initCloudWatch();

  try {
    // Get issue details
    const issue = await getIssue(Date.now(), getTriggerCommentId());
    log('INFO', `Processing issue: ${issue.title}`);

    // Generate project folder name
    const projectFolder = generateProjectFolderName(issue.title, issue.number);
    log('INFO', `Project folder: ${projectFolder}`);

    // Load Superpowers skills
    const skills = loadSuperpowersSkills();
    if (!skills) {
      throw new Error('Failed to load Superpowers skills. Ensure .claude/skills/ exists.');
    }

    // Post start comment
    await postComment(`## 🦸 @agent-pt-superpower Started

**Status**: In Progress
**Started**: ${new Date().toISOString()}
**Mode**: Superpowers Workflow (obra/superpowers)
**Project Folder**: \`${projectFolder}/\`

**Phases:**
1. 🧠 Brainstorming - Understanding requirements, asking questions
2. 📋 Planning - Creating detailed implementation plan
3. 🔨 Implementation - TDD approach, task by task (all files in \`${projectFolder}/\`)
4. ✅ Verification - Testing and validation

Starting brainstorming phase...`);

    // Phase 1: Brainstorming
    const { approved, design } = await runBrainstormingPhase(issue, skills);

    if (!approved) {
      await postComment(`## 🦸 @agent-pt-superpower Stopped

User rejected the proposed design. No changes made.`);
      log('INFO', 'Workflow stopped by user');
      await flushCloudWatch();
      return;
    }

    // Phase 2 & 3: Planning and Implementation
    const result = await runImplementationPhase(issue, skills, design, projectFolder);

    // Gather list of files created
    const projectPath = path.join(CWD, projectFolder);
    const fileList: string[] = [];
    if (fs.existsSync(projectPath)) {
      const gatherFiles = (dir: string, prefix: string = ''): void => {
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        for (const entry of entries) {
          const fullPath = path.join(dir, entry.name);
          const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
          if (entry.isDirectory() && !entry.name.startsWith('.') && entry.name !== 'node_modules') {
            gatherFiles(fullPath, relativePath);
          } else if (entry.isFile()) {
            fileList.push(relativePath);
          }
        }
      };
      gatherFiles(projectPath);
    }

    // Generate AI-powered task-specific summary
    console.log('\n' + '═'.repeat(60));
    console.log('📝 GENERATING COMPLETION SUMMARY');
    console.log('═'.repeat(60));

    let summaryComment = '';
    try {
      for await (const message of resilientQuery({
        queryParams: {
          prompt: buildSummaryPrompt({
            issueTitle: issue.title,
            issueNumber: issue.number,
            issueBody: issue.body,
            design,
            result,
            fileList,
            projectFolder,
            commentIssueNumber: ISSUE_NUMBER,
          }),
          options: {
            model: MODEL,
            cwd: CWD,
            allowedTools: ['Bash'],
            permissionMode: 'bypassPermissions',
            maxTurns: 5,
          }
        },
        maxRetries: 2,
        baseDelayMs: 5000,
        maxDelayMs: 30000,
        log: (msg) => log('WARN', msg),
      })) {
        if (message.type === 'assistant') {
          const content = (message as any).message?.content;
          if (Array.isArray(content)) {
            for (const block of content) {
              if (block.type === 'text') {
                summaryComment += block.text;
              }
            }
          }
        }
      }
      log('INFO', 'AI-generated summary posted');
    } catch (err) {
      log('WARN', `Failed to generate AI summary: ${(err as Error).message}`);
      // Fallback to basic summary
      await postComment(`## 🦸 @agent-pt-superpower Complete

**Completed**: ${new Date().toISOString()}
**Project Folder**: \`${projectFolder}/\`
**Files Created**: ${fileList.length}

Implementation complete. Please review the PR for details.

**Files include:**
${fileList.slice(0, 20).map(f => `- \`${f}\``).join('\n')}
${fileList.length > 20 ? `\n... and ${fileList.length - 20} more` : ''}`);
    }

    log('INFO', 'Workflow complete');
    await flushCloudWatch();

  } catch (error) {
    const err = error as Error;
    log('ERROR', `Workflow failed: ${err.message}`);

    try {
      await postComment(`## 🦸 @agent-pt-superpower Failed

**Error**: ${err.message}

Check workflow logs for details.`);
    } catch {
      console.error('Failed to post error comment');
    }

    await flushCloudWatch();
    process.exit(1);
  }
}

main();
