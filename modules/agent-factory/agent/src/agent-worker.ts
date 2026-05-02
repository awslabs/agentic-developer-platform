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

import { resilientQuery } from './utils/resilientQuery';
import { initTokenManager, getToken, getTokenStatus } from './token-refresh';
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

// Live status comment — edit-in-place progress on GitHub issues
import { LiveStatusComment, createWorkerStages } from './github-comments';

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
const AGENT_TYPE = process.env.AGENT_TYPE || 'developer';
const AWS_REGION = process.env.AWS_REGION || 'us-east-1';

// Beads configuration - distributed state management (shared with PM)
const BEADS_ENABLED = process.env.BEADS_ENABLED !== 'false';
const BEADS_S3_BUCKET = process.env.BEADS_S3_BUCKET || 'adp-agent-state';
const BEADS_S3_REGION = process.env.BEADS_S3_REGION || AWS_REGION;
const BEADS_S3_PATH = process.env.BEADS_S3_PATH || `beads/${REPO_NAME}`;

// ============================================================================
// CloudWatch Logging
// ============================================================================

const LOG_GROUP = '/github-ccsdk-agent/logs';
const LOG_STREAM = `agent-${AGENT_TYPE}-issue-${ISSUE_NUMBER}-${Date.now()}`;
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
async function refreshAppToken(): Promise<void> {
  const appId = process.env.GH_APP_ID;
  const privateKey = process.env.GH_APP_PRIVATE_KEY;
  if (!appId || !privateKey) return; // Not using app auth

  try {
    const jwt = await import('jsonwebtoken');
    const now = Math.floor(Date.now() / 1000);
    const jwtToken = jwt.default.sign(
      { iat: now - 60, exp: now + 600, iss: appId },
      privateKey,
      { algorithm: 'RS256' }
    );

    const resp = await fetch('https://api.github.com/app/installations', {
      headers: { Authorization: `Bearer ${jwtToken}`, Accept: 'application/vnd.github+json' },
    });
    const installations = await resp.json() as Array<{ id: number }>;
    if (!installations.length) return;

    const tokenResp = await fetch(
      `https://api.github.com/app/installations/${installations[0].id}/access_tokens`,
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
      // Also update git remote URL so git push uses the fresh token
      try {
        const { execSync } = await import('child_process');
        const repo = process.env.TARGET_REPO || `${process.env.REPO_OWNER}/${process.env.REPO_NAME}`;
        execSync(`git remote set-url origin "https://x-access-token:${tokenData.token}@github.com/${repo}.git"`, { stdio: 'pipe', cwd: process.env.WORK_DIR || process.cwd() });
      } catch { /* git remote update is best-effort */ }
      log('INFO', 'Refreshed GitHub App token for gh CLI + git remote');
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
  const tmpFile = `/tmp/comment-${Date.now()}.md`;
  fs.writeFileSync(tmpFile, body);
  try {
    // Refresh token before posting
    await refreshAppToken();
    await gh(`issue comment ${targetIssue} --body-file "${tmpFile}"`);
  } catch (err) {
    log('WARN', `GitHub post failed, saving to S3 fallback: ${(err as Error).message}`);
    try {
      const { S3Client, PutObjectCommand } = await import('@aws-sdk/client-s3');
      const s3 = new S3Client({ region: process.env.AWS_REGION || 'us-east-1' });
      const key = `agent-fallback/issue-${targetIssue}/${new Date().toISOString().replace(/[:.]/g, '-')}-comment.md`;
      await s3.send(new PutObjectCommand({
        Bucket: process.env.AGENT_FALLBACK_BUCKET || 'adp-agent-state',
        Key: key,
        Body: body,
        ContentType: 'text/markdown',
      }));
      log('INFO', `Comment saved to s3://${process.env.AGENT_FALLBACK_BUCKET || 'adp-agent-state'}/${key}`);
      console.log(`📦 GitHub API failed — comment saved to S3: ${key}`);
    } catch (s3Err) {
      log('ERROR', `Both GitHub and S3 fallback failed: ${(s3Err as Error).message}`);
    }
  } finally {
    try { fs.unlinkSync(tmpFile); } catch {}
  }
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
    try {
      const { S3Client, PutObjectCommand } = await import('@aws-sdk/client-s3');
      const s3 = new S3Client({ region: process.env.AWS_REGION || 'us-east-1' });
      const key = `agent-fallback/issue-${ISSUE_NUMBER}/${new Date().toISOString().replace(/[:.]/g, '-')}-comment.md`;
      await s3.send(new PutObjectCommand({
        Bucket: process.env.AGENT_FALLBACK_BUCKET || 'adp-agent-state',
        Key: key,
        Body: body,
        ContentType: 'text/markdown',
      }));
      log('INFO', `Comment saved to S3: ${key}`);
      console.log(`📦 GitHub API failed — comment saved to S3: ${key}`);
    } catch (s3Err) {
      log('ERROR', `Both GitHub and S3 fallback failed: ${(s3Err as Error).message}`);
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

  return rules.join('\n\n---\n\n');
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
    operations: 'DevOps/SRE - responsible for infrastructure, deployment, and monitoring',
  };

  const mainIssueInfo = mainIssueNumber
    ? `\n\n**IMPORTANT**: This task is part of a larger initiative. Post your progress updates to the MAIN issue #${mainIssueNumber}.`
    : '';

  const prompt = `You are @agent-${AGENT_TYPE}, the ${agentDescriptions[AGENT_TYPE] || 'agent'}.${mainIssueInfo}

## Your Task
Process this GitHub issue and complete the assigned work.

### Issue #${issue.number}: ${issue.title}

${issue.body}
${memoryCtx ? `
---

${memoryCtx}
` : ''}${commentsContext ? `
---

## Existing Discussion / Comments

The following comments have been posted on this issue. Read them carefully - they may contain important context, decisions, research, or approvals from previous agents or users.

${commentsContext}
` : ''}
---

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

---

## Instructions

**IMPORTANT: You MUST follow this structured approach:**

### Step 1: Analyze and Plan
- Read the issue carefully to understand what's being asked
- Research as needed (web search for external docs, grep/glob for codebase)
- Create a clear, numbered implementation plan

### Step 2: Post Your Plan
**Before doing any implementation work**, post your plan to the issue using:
\`\`\`bash
gh issue comment ${ISSUE_NUMBER} --body "## 📋 Implementation Plan

**Agent**: @agent-${AGENT_TYPE}
**Issue**: #${issue.number}

### Analysis
[Your analysis of what needs to be done]

### Implementation Steps
1. [Step 1 - be specific]
2. [Step 2 - be specific]
3. [Continue as needed...]

### Expected Deliverables
- [File/artifact 1]
- [File/artifact 2]

---
Starting implementation..."
\`\`\`

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

## Pre-submit checks (MANDATORY before creating a PR)

Before you push your branch and open the PR, run the linters and tests for the module(s) you touched. A PR that lands with red CI wastes the reviewer's time, trains everyone to ignore the signal, and ships bugs the linter would have caught.

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
- **If any command fails**, fix the underlying issue before pushing. Do NOT suppress warnings with \`# noqa\` or \`eslint-disable\` unless the rule genuinely doesn't apply — and note why in a comment.
- **If a check fails on code you didn't touch** (pre-existing debt), note it in the PR description as "pre-existing on main: <file>:<line> <rule>" and move on. Don't clean up unrelated debt in the same PR (surgical changes principle from \`docs/agent-coding-guidelines.md\`).
- **Auto-fix tools are fine**: \`ruff check --fix\`, \`ruff format\`, \`eslint --fix\`. Treat their output as code you wrote — review the diff before committing.

### Post-commit sanity

After committing, before pushing, run \`git diff HEAD~1 --stat\` and confirm the files you expected to change are the only ones that changed. If the linter reformatted a file you didn't mean to touch, that's a surgical-changes violation — revert it.

Failing to run these checks is a process bug. PRs that land with lint/test failures traceable to the PR's own changes will be reverted.

${AGENT_TYPE === 'reviewer' ? `### Step 3.4: Spec-vs-diff Review (MANDATORY for @agent-reviewer)

You are reviewing a PR. Treat this as an INDEPENDENT review — don't trust the PR description, verify against the code.

**If you cannot find PR_NUMBER in the environment**, stop and report the setup failure in an issue comment — don't proceed with an unscoped review.

1. **Identify the PR and the driving issue:**
   \`\`\`bash
   # PR_NUMBER is provided in your environment
   echo "Reviewing PR #\$PR_NUMBER against issue #\$ISSUE_NUMBER"
   gh pr view \$PR_NUMBER --json title,body,files,additions,deletions
   gh pr diff \$PR_NUMBER > /tmp/pr-diff.patch
   \`\`\`

2. **Extract the acceptance criteria from the issue:**
   Re-read the issue body (already shown above). List every acceptance criterion, invariant, and "must NOT" constraint as a checklist. If there's an "Acceptance Criteria" section, extract it verbatim. If not, synthesize from the Goal + Scope sections.

3. **Verify each criterion against the diff:**
   For each criterion in your checklist, find the concrete line(s) in the diff that satisfy it. If you can't find one, that's a HIGH-confidence merge blocker.

4. **Check for invariant violations:**
   Specifically watch for things the issue said NOT to do — "do not touch X", "do not change behavior of Y", "zero regression to Z". Grep the diff for those areas. Any violation is a HIGH-confidence merge blocker.

5. **Check for committed files that should not exist:**
   The repo's AGENTS.md at the root defines a set of "must not commit" rules (e.g. \`agent_learning/*.md\`, \`tfplan\` files, anything under \`.terraform/\`). Grep the diff's file list for violations — these are HIGH-confidence merge blockers and the agent should propose fixes.

6. **Categorize every finding by confidence:**
   - **HIGH**: certain merge blocker, verified against code
   - **MEDIUM**: likely issue, worth discussing before merge
   - **LOW**: nice-to-have, file as a follow-up

7. **Write the review summary to a file:**
   \`\`\`bash
   mkdir -p data/code-review
   cat > data/code-review/review-$(date +%Y%m%d)-pr-\$PR_NUMBER.md <<'SUMMARY'
   # Review of PR #$PR_NUMBER

   ## Driving issue
   - #$ISSUE_NUMBER: <issue title>

   ## Acceptance criteria checklist
   - [x|✗] <criterion 1> — <where in diff it's satisfied OR why it's not>
   - [x|✗] <criterion 2> — ...

   ## Findings (by confidence)

   ### HIGH — merge blockers
   - <file:line>: <concrete issue + exact line in diff>

   ### MEDIUM — discuss before merge
   - ...

   ### LOW — follow-up candidates
   - ...

   ## Recommendation
   APPROVE / REQUEST CHANGES / BLOCK
   SUMMARY
   \`\`\`

8. **Post the review summary to the PR:**
   \`\`\`bash
   gh pr comment \$PR_NUMBER --body-file data/code-review/review-$(date +%Y%m%d)-pr-\$PR_NUMBER.md
   \`\`\`

9. **Only after Step 8:** proceed to the security review step below.

**DO NOT approve a PR if**:
- Any HIGH finding is unresolved
- Any acceptance criterion from the issue is ✗
- Any file committed to the PR matches a "must not commit" rule in AGENTS.md

### Step 3.5: Security Review (MANDATORY for @agent-reviewer)
**You MUST run security review before approving ANY PR:**

1. **Run the /security-review command:**
   Use the built-in security review skill by invoking:
   \`/security-review\`

   This will automatically:
   - Scan for hardcoded secrets and credentials
   - Check for vulnerable dependencies
   - Identify OWASP Top 10 vulnerabilities
   - Flag insecure configurations

2. **Review and fix findings:**
   - Fix issues you can fix safely (see pr-review.md for guidance)
   - Document unfixable issues for human review

3. **Create review log file:**
   \`\`\`bash
   mkdir -p data/code-review
   # Create data/code-review/review-YYYYMMDD-pr-NNN.md with:
   # - Security findings from /security-review
   # - Fixes applied
   # - Issues escalated
   \`\`\`

4. **Post security summary to PR:**
   \`\`\`bash
   gh pr comment $PR_NUMBER --body "## 🔒 Security Review Complete
   [Summary of /security-review findings and actions taken]"
   \`\`\`

**DO NOT merge without completing /security-review.**
` : ''}${AGENT_TYPE === 'operations' ? `### Step 3.5: Execution (MANDATORY for @agent-operations)
**You are the DEPLOYMENT agent. Your job is to EXECUTE infrastructure changes, not just prepare them.**

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
**DO NOT consider your task complete until you have either deployed OR clearly stated why you could not.**
` : ''}### Step 4: Report Results
- Summarize what you accomplished
- List files created/modified
- Note any issues encountered
- Recommend next steps

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

**IMPORTANT**: When your work is complete, your FINAL message must be a well-structured summary that stakeholders can easily read and understand. Use this EXACT format:

\`\`\`
## ✅ Task Complete: [Brief title of what was accomplished]

### What Was Done
[2-4 bullet points describing the key accomplishments in business terms. Focus on OUTCOMES, not just actions. Example: "Deployed agent-mail service to EKS cluster" not "Ran kubectl apply"]

### Key Deliverables
| Deliverable | Status | Location/Details |
|-------------|--------|------------------|
| [e.g., Docker image] | ✅ Ready | [e.g., ECR: xxx.dkr.ecr...] |
| [e.g., K8s manifests] | ✅ Created | [e.g., k8s/agent-mail/] |
| [e.g., PR] | ✅ Opened | [e.g., #268] |

### Verification
[How can someone verify this work is complete? Include specific commands or URLs]

### Next Steps
[What should happen next? Who/what is unblocked by this work?]

### Issues Encountered (if any)
[Only include if there were significant issues. Briefly describe and how resolved]

${AGENT_TYPE === 'operations' ? `### Deployment Status (REQUIRED for @agent-operations)
| Action | Status | Details |
|--------|--------|---------|
| Deployment Executed? | ✅ Yes / ❌ No | [If No, explain WHY: awaiting approval, missing creds, etc.] |
| Service Running? | ✅ Yes / ❌ No / N/A | [Status check result] |
| Endpoint Accessible? | ✅ Yes / ❌ No / N/A | [URL/IP or reason not accessible] |

**If deployment was NOT executed, clearly explain why and what is needed to proceed.**
` : ''}### Learnings
[Document insights that would help future work on this codebase or similar tasks:
- Gotchas or non-obvious configurations discovered
- Useful patterns or approaches that worked well
- Things that didn't work and why
- Recommendations for improving the process
Keep each learning to 1-2 sentences. These help future agents and humans avoid repeating mistakes.]
\`\`\`

Your summary will be posted to the parent issue for stakeholders to review. Make it clear, concise, and actionable.

**Also write learnings to file**: After posting your summary, save detailed learnings to \`agent_learning/{date}-issue-{number}-learnings.md\`. This file is read by future agents — make it HIGH QUALITY:
- What worked and what didn't (specific commands, configurations, error messages)
- Key technical decisions and why they were made
- Gotchas, workarounds, and things that took multiple attempts
- Exact versions, endpoints, resource names that future agents will need
- NEVER include secrets, API keys, tokens, passwords, or private keys in learnings

Now, complete the assigned task.`;

  log('INFO', 'Starting agent execution...');
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

    // Heartbeat: log a "still alive" message if no SDK messages arrive for 60s.
    // Also acts as a safety net: if the query already completed but the stream
    // hasn't closed, force-exit after POST_COMPLETION_TIMEOUT_MS.
    const heartbeat = setInterval(() => {
      const silentSec = Math.round((Date.now() - lastActivityTime) / 1000);

      // Safety net: force exit if stream hangs after query completion
      if (queryCompleted && queryCompletedTime) {
        const elapsed = Date.now() - queryCompletedTime;
        if (elapsed >= POST_COMPLETION_TIMEOUT_MS) {
          const msg = `⚠️  Force exit — stream did not close ${Math.round(elapsed / 1000)}s after query completed`;
          console.log(msg);
          log('WARN', msg, { phase: 'post-completion-timeout', elapsedMs: elapsed });
          process.exit(0);
        }
      }

      if (silentSec >= 60) {
        const msg = `💓 Heartbeat — no SDK messages for ${silentSec}s (turn ${turnCount})`;
        console.log(msg);
        log('INFO', msg, { phase: 'heartbeat', silentSeconds: silentSec, turn: turnCount });
      }
    }, 30_000);

    try {
      // Labeled loop so we can break out of the `for await` from inside the
      // switch statement.  Without the label, `break` only exits the switch.
      queryLoop:                          // eslint-disable-line no-labels
      for await (const message of resilientQuery({
        queryParams: {
          prompt,
          options: {
            model: MODEL,
            cwd: CWD,
            allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill'],
            settingSources: ['project'],
            permissionMode: 'bypassPermissions',
            persistSession: false,
            maxTurns: 10000,
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
            const turnText = logMessage(
              message as { type: string; message: { content: Array<Record<string, unknown>> } },
              turnCount
            );
            fullResponse += turnText;
            lastTurnText = turnText;
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

    // Upload to S3
    const s3 = new S3Client({ region: process.env.AWS_REGION || 'us-east-1' });
    const bucket = process.env.AGENT_FALLBACK_BUCKET || 'adp-agent-state';
    const key = `agent-fallback/issue-${ISSUE_NUMBER}/${timestamp}-git-changes.tar.gz`;

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

  if (appId && appKey && repoOwner) {
    initTokenManager({
      appId,
      privateKey: appKey,
      owner: repoOwner,
      repo: REPO_NAME,
      workDir: CWD,
      refreshThresholdMs: 15 * 60 * 1000, // Refresh when 15 min remaining
    });

    // Proactively refresh token every 30 minutes
    const tokenRefreshInterval = setInterval(async () => {
      try {
        await getToken();
        const status = getTokenStatus();
        log('INFO', 'Token refreshed proactively', {
          expiresInMin: status ? Math.round(status.expiresIn / 60000) : 0,
        });
      } catch (err) {
        log('WARN', `Token refresh failed: ${(err as Error).message}`);
      }
    }, 30 * 60 * 1000); // Every 30 minutes

    // Clean up interval on exit
    process.on('exit', () => clearInterval(tokenRefreshInterval));
    // Also store reference for cleanup in finally block
    (global as any).__tokenRefreshInterval = tokenRefreshInterval;

    log('INFO', 'Token manager initialized with 30-minute refresh interval');
  } else {
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
    const liveComment = new LiveStatusComment(createWorkerStages(), {
      owner: REPO_OWNER,
      repo: REPO_NAME,
      issueNumber: parseInt(ISSUE_NUMBER),
      token,
      log,
    });

    try {
      await liveComment.post();
      log('INFO', `Live status comment posted: ${liveComment.getCommentId()}`);
    } catch (err) {
      log('WARN', `Could not post live status comment: ${(err as Error).message}`);
    }

    // Stage 0: Setup — mark complete (we're past setup at this point)
    liveComment.transition(0, 'complete', 'Environment ready');

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
    liveComment.transition(1, 'in_progress', 'Running agent');

    // Run the agent
    const result = await runAgent(issue, mainIssueNumber, beadsPrimeContext, commentsContext, memoryContext);
    agentResult = result || '';

    // Mark analyze through PR stages as complete (agent handles all internally)
    liveComment.transition(1, 'complete');
    liveComment.transition(2, 'complete');
    liveComment.transition(3, 'complete');
    liveComment.transition(4, 'complete');
    liveComment.transition(5, 'complete');

    // Complete task in Beads (if claimed)
    if (beadsAvailable && beadsTaskId) {
      try {
        await beadsCompleteWork(beadsTaskId, 'Completed successfully', CWD);
        log('INFO', `Beads task ${beadsTaskId} marked complete`);
      } catch (err) {
        log('WARN', `Could not complete Beads task: ${(err as Error).message}`);
      }
    }

    // NOTE: Don't update project board status to Done here.
    // Status will be set to Done automatically by GitHub project automation
    // when the PR is merged and the issue is closed.

    // Finalize live status comment with success summary
    const runDuration = Date.now() - (liveComment.getStages()[0]?.startedAt || Date.now());
    await liveComment.finalizeSuccess({
      durationMs: runDuration,
      details: result ? result.substring(0, 500) : undefined,
    }).catch(err => log('WARN', `Could not finalize live comment: ${(err as Error).message}`));

    // Post completion to main issue
    const summary = `## @agent-${AGENT_TYPE} Completed

**Task**: #${issue.number} - ${issue.title}
**Status**: Done
**Completed**: ${new Date().toISOString()}
${beadsTaskId ? `**Beads ID**: ${beadsTaskId}` : ''}

### Summary
${result}`;

    await postToMainIssue(mainIssueNumber, summary);

    log('INFO', 'Work completed successfully');
    agentSucceeded = true;

  } catch (error) {
    const err = error as Error;
    log('ERROR', `Agent failed: ${err.message}`);

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
