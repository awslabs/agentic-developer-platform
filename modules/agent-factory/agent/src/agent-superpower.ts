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
import { CloudWatchLogsClient, PutLogEventsCommand, CreateLogStreamCommand } from '@aws-sdk/client-cloudwatch-logs';
import * as fs from 'fs';
import * as path from 'path';
import { execSync } from 'child_process';
import { refreshGitHubToken, saveToS3Fallback } from './utils/ghPost';

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

const LOG_GROUP = '/github-ccsdk-agent/logs';
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
    return execSync(cmd, {
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
  guidance: string | null;
  previousContext: string | null;
}

async function getIssue(): Promise<Issue> {
  const result = execCommand(`gh issue view ${ISSUE_NUMBER} --repo ${REPO_OWNER}/${REPO_NAME} --json number,title,body,labels,comments`);
  const data = JSON.parse(result);

  // Parse comments
  const comments: Comment[] = (data.comments || []).map((c: { author: { login: string }; body: string; createdAt: string }) => ({
    author: c.author?.login || 'unknown',
    body: c.body || '',
    createdAt: c.createdAt || '',
  }));

  // Extract guidance from comments (look for /context, /guidance, /resume, /use commands)
  const guidance = extractGuidance(comments);

  // Extract previous implementation context (agent summaries, approved designs)
  const previousContext = extractPreviousContext(comments);

  log('INFO', `Loaded issue with ${comments.length} comments`);
  if (guidance) log('INFO', `Found guidance comment`);
  if (previousContext) log('INFO', `Found previous context (${previousContext.length} chars)`);

  return {
    number: data.number,
    title: data.title,
    body: data.body || '',
    labels: (data.labels || []).map((l: { name: string }) => l.name),
    comments,
    guidance,
    previousContext,
  };
}

/**
 * Extract user guidance from comments.
 * Looks for comments starting with /context, /guidance, /resume, /use, /instruction
 * Returns the most recent guidance comment.
 */
function extractGuidance(comments: Comment[]): string | null {
  const guidanceCommands = ['/context', '/guidance', '/resume', '/use', '/instruction', '/direction'];

  // Find guidance comments (non-bot, starting with guidance commands)
  const guidanceComments = comments.filter(c => {
    if (c.author.includes('[bot]') || c.author === 'github-actions') return false;
    const lower = c.body.toLowerCase().trim();
    return guidanceCommands.some(cmd => lower.startsWith(cmd));
  });

  if (guidanceComments.length === 0) return null;

  // Return the most recent guidance
  const latest = guidanceComments[guidanceComments.length - 1];
  // Remove the command prefix and return the content
  const body = latest.body.trim();
  const firstNewline = body.indexOf('\n');
  return firstNewline > 0 ? body.substring(firstNewline + 1).trim() : body.substring(body.indexOf(' ') + 1).trim();
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
// User Response Parsing
// ============================================================================

interface UserResponse {
  action: 'approve' | 'feedback' | 'reject' | 'unknown';
  content: string;
}

function parseUserResponse(comment: string): UserResponse {
  const lower = comment.toLowerCase().trim();

  // Check for approval commands
  if (lower.startsWith('/approve') || lower.startsWith('/lgtm') || lower.startsWith('/go')) {
    return { action: 'approve', content: comment };
  }

  // Check for rejection
  if (lower.startsWith('/reject') || lower.startsWith('/stop') || lower.startsWith('/cancel')) {
    return { action: 'reject', content: comment };
  }

  // Check for feedback (any substantive response)
  if (comment.length > 20 && !comment.startsWith('##')) {
    return { action: 'feedback', content: comment };
  }

  return { action: 'unknown', content: comment };
}

// ============================================================================
// Interactive Polling
// ============================================================================

interface PollResult {
  shouldContinue: boolean;
  reason: 'approved' | 'feedback' | 'rejected' | 'timeout' | 'error';
  userResponse?: string;
}

async function pollForUserResponse(phase: string): Promise<PollResult> {
  const startTime = Date.now();
  const maxPolls = Math.ceil(USER_RESPONSE_TIMEOUT_MS / POLL_INTERVAL_MS);

  log('INFO', `Waiting for user response in ${phase} phase (max ${USER_RESPONSE_TIMEOUT_MS / 60000} minutes)`);
  console.log('\n' + '═'.repeat(60));
  console.log(`⏳ Waiting for user response (${phase} phase)...`);
  console.log('   Commands: /approve, /reject, or provide feedback');
  console.log('═'.repeat(60) + '\n');

  // Track the timestamp of comments we've seen
  let lastSeenTimestamp = new Date().toISOString();
  try {
    const initialComments = await fetchIssueComments();

    // FIRST: Check existing comments for approval (handles re-runs where /approve was already posted)
    log('INFO', `Checking ${initialComments.length} existing comments for approval...`);
    for (const comment of initialComments) {
      // Skip bot comments
      if (comment.author.includes('[bot]') || comment.author === 'github-actions' || comment.author === 'adp-agent-ops') {
        continue;
      }

      const response = parseUserResponse(comment.body);
      if (response.action === 'approve') {
        log('INFO', `Found existing approval from ${comment.author}`);
        console.log('✅ Found existing approval! Proceeding...');
        return { shouldContinue: true, reason: 'approved', userResponse: comment.body };
      }
      if (response.action === 'reject') {
        log('INFO', `Found existing rejection from ${comment.author}`);
        console.log('❌ Found existing rejection.');
        return { shouldContinue: false, reason: 'rejected', userResponse: comment.body };
      }
    }

    // Set timestamp to last comment for polling new ones
    if (initialComments.length > 0) {
      lastSeenTimestamp = initialComments[initialComments.length - 1].createdAt || lastSeenTimestamp;
    }
  } catch {
    log('WARN', 'Could not get initial comments');
  }

  for (let poll = 1; poll <= maxPolls; poll++) {
    const elapsedMinutes = Math.round((Date.now() - startTime) / 60000);
    const remainingMinutes = Math.round((USER_RESPONSE_TIMEOUT_MS - (Date.now() - startTime)) / 60000);

    console.log(`🔄 Poll ${poll}/${maxPolls} (${elapsedMinutes}m elapsed, ${remainingMinutes}m remaining)`);

    try {
      const comments = await fetchIssueComments();

      // Find comments newer than lastSeenTimestamp
      const newComments = comments.filter(c => c.createdAt > lastSeenTimestamp);

      if (newComments.length > 0) {
        log('INFO', `Found ${newComments.length} new comment(s)`);

        for (const comment of newComments) {
          // Skip bot comments
          if (comment.author.includes('[bot]') || comment.author === 'github-actions') {
            continue;
          }

          log('INFO', `Checking comment from ${comment.author}: ${comment.body.substring(0, 50)}...`);

          const response = parseUserResponse(comment.body);

          if (response.action === 'approve') {
            console.log('✅ User approved! Proceeding...');
            return { shouldContinue: true, reason: 'approved', userResponse: comment.body };
          }

          if (response.action === 'reject') {
            console.log('❌ User rejected. Stopping.');
            return { shouldContinue: false, reason: 'rejected', userResponse: comment.body };
          }

          if (response.action === 'feedback') {
            console.log('💬 User provided feedback. Incorporating...');
            return { shouldContinue: true, reason: 'feedback', userResponse: comment.body };
          }
        }

        // Update timestamp to latest comment
        const latestComment = newComments[newComments.length - 1];
        lastSeenTimestamp = latestComment.createdAt || lastSeenTimestamp;
      }
    } catch (err) {
      log('WARN', `Poll error: ${(err as Error).message}`);
    }

    // Wait before next poll
    if (poll < maxPolls) {
      await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }

  // Timeout - auto-proceed with recommendations
  console.log('⏰ Timeout reached. Auto-proceeding with recommendations...');
  return { shouldContinue: true, reason: 'timeout' };
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

  // Build context sections
  const guidanceSection = issue.guidance ? `
---

## 🎯 User Guidance (from comments)

The user has provided specific guidance for this run:

${issue.guidance}

**IMPORTANT:** Follow this guidance carefully. It may ask you to skip certain steps, use previous context, or focus on specific aspects.
` : '';

  const previousContextSection = issue.previousContext ? `
---

## 📋 Previous Implementation Context

A previous run of this agent produced the following work (which may not have been committed due to workflow issues). You can use this as reference:

<previous_context>
${issue.previousContext.substring(0, 8000)}${issue.previousContext.length > 8000 ? '\n...(truncated)' : ''}
</previous_context>

**NOTE:** If the user's guidance asks you to resume or use this context, you should leverage this previous work instead of starting from scratch.
` : '';

  const prompt = `You are @agent-pt-superpower using the Superpowers workflow from obra/superpowers.

## Your Task
Process this GitHub issue using the brainstorming skill.

### Issue #${issue.number}: ${issue.title}

${issue.body}
${guidanceSection}
${previousContextSection}
---

## Superpowers Skills Available

${skills}

---

## Instructions for Brainstorming Phase

**Follow the brainstorming skill workflow:**

1. **Don't jump into code** - Step back and understand what the user really wants
2. **Ask clarifying questions** - Identify ambiguities, assumptions, edge cases
3. **Explore alternatives** - Consider different approaches
4. **Present design in chunks** - Break it down for easy review

${issue.guidance ? `**SPECIAL:** User has provided guidance - check if they want you to skip brainstorming or use previous context.` : ''}

**Your output should be a GitHub comment with:**
1. Your understanding of the problem
2. Key questions that need answers (if any)
3. Proposed approach/design (in digestible sections)
4. Request for approval or feedback

Post your brainstorming output as a comment using:
\`\`\`bash
gh issue comment ${ISSUE_NUMBER} --body "## 🧠 Brainstorming: [Title]

### Understanding
[Your interpretation of the problem]

### Questions (if any)
1. [Question 1]
2. [Question 2]

### Proposed Approach
[Design in sections]

---
**Please respond with:**
- \`/approve\` to proceed with implementation
- \`/reject\` to stop
- Or provide feedback to refine the design"
\`\`\`

After posting, report back with a summary.`;

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
          message as { type: string; message: { content: Array<Record<string, unknown>> } },
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
  const pollResult = await pollForUserResponse('brainstorming');

  if (pollResult.reason === 'rejected') {
    return { approved: false, design: '' };
  }

  if (pollResult.reason === 'feedback' && pollResult.userResponse) {
    // Incorporate feedback and iterate
    log('INFO', 'Incorporating user feedback...');
    // For now, treat feedback as approval to continue
    // In a more sophisticated version, we'd loop back to brainstorming
  }

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

  // Build context sections
  const guidanceSection = issue.guidance ? `
---

## 🎯 User Guidance

${issue.guidance}
` : '';

  const previousContextSection = issue.previousContext ? `
---

## 📋 Previous Implementation Reference

A previous run produced this work (use as reference if applicable):

<previous_implementation>
${issue.previousContext.substring(0, 6000)}${issue.previousContext.length > 6000 ? '\n...(truncated)' : ''}
</previous_implementation>
` : '';

  const prompt = `You are @agent-pt-superpower using the Superpowers workflow from obra/superpowers.

## Context
The brainstorming phase is complete and the design has been approved.

### Issue #${issue.number}: ${issue.title}

${issue.body}

### Approved Design Summary
${design.substring(0, 2000)}...
${guidanceSection}
${previousContextSection}
---

## Superpowers Skills Available

${skills}

---

## IMPORTANT: Project Folder Structure

**ALL generated files MUST be created inside this folder:**
\`\`\`
${projectFolder}/
\`\`\`

For example:
- Source code: \`${projectFolder}/src/\`
- Tests: \`${projectFolder}/tests/\` or \`${projectFolder}/src/__tests__/\`
- Config files: \`${projectFolder}/\`
- Documentation: \`${projectFolder}/docs/\` or \`${projectFolder}/README.md\`

**First, create the project folder if it doesn't exist:**
\`\`\`bash
mkdir -p ${projectFolder}
\`\`\`

---

## Instructions for Implementation Phase

**Follow these Superpowers skills in order:**

### 1. Writing Plans (writing-plans skill)
Create a detailed implementation plan:
- Break into 2-5 minute tasks
- Each task: exact file paths (inside ${projectFolder}/), code snippets, verification steps
- Post plan to issue for reference

### 2. Test-Driven Development (test-driven-development skill)
For each task:
- Write failing test FIRST (RED) in ${projectFolder}/tests/ or similar
- Implement minimal code to pass (GREEN)
- Refactor if needed
- Commit after each green test

### 3. Implementation
Execute the plan task by task:
- ALL files go in ${projectFolder}/
- Follow TDD strictly
- Commit after each task
- Self-review before moving on

### 4. Verification (verification-before-completion skill)
- Run all tests
- Verify the implementation actually works
- Fix any issues found

### 5. Report Results
Post a summary comment with:
- Tasks completed
- Tests passing
- Files changed (all should be in ${projectFolder}/)
- Any issues encountered

Now execute the implementation. Start by creating the plan, then implement with TDD.
Remember: ALL files must be created inside ${projectFolder}/`;

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
            message as { type: string; message: { content: Array<Record<string, unknown>> } },
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
    const issue = await getIssue();
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
          prompt: `You just completed implementing a task. Generate a detailed, SPECIFIC completion summary for the GitHub issue.

## Original Issue
**Title**: ${issue.title}
**Issue #**: ${issue.number}

**Description**:
${issue.body}

## What Was Done

### Brainstorming/Design Phase Output:
${design.substring(0, 3000)}

### Implementation Output:
${result.substring(0, 3000)}

### Files Created (${fileList.length} total):
${fileList.slice(0, 50).join('\n')}
${fileList.length > 50 ? `\n... and ${fileList.length - 50} more files` : ''}

## Your Task

Write a GitHub comment that provides a **SPECIFIC** summary of what was accomplished.

**IMPORTANT**: Do NOT use generic phrases like "Created implementation plan" or "Analyzed requirements".
Instead, be SPECIFIC about:
- What specific design decisions were made and WHY
- What specific components/features were built
- What specific technologies/tools are used
- What specific tests were written
- Any important configuration or setup details
- Specific next steps the user should take

Format the comment as:

## 🦸 @agent-pt-superpower Complete

**Completed**: [timestamp]
**Project Folder**: \`${projectFolder}/\`

---

### 📋 Summary

[2-3 sentences summarizing the SPECIFIC outcome - what was built, not generic process description]

---

### 🎯 Key Decisions Made

[Bullet points of SPECIFIC decisions from brainstorming, e.g., "Chose Hub-and-Spoke architecture because...", "Selected ArgoCD over Flux because..."]

---

### 🔧 What Was Built

[SPECIFIC description of components created, e.g., "Terraform modules for EKS cluster with...", "ArgoCD ApplicationSets for..."]

---

### 📁 Project Structure

[Brief description of the folder structure and what each major directory contains]

---

### ✅ Tests & Validation

[What specific tests were written and what they validate]

---

### 🚀 Next Steps

[SPECIFIC, actionable next steps for THIS task, not generic instructions. E.g., "1. Set AWS credentials: export AWS_PROFILE=...", "2. Initialize Terraform: cd terraform/environments/hub && terraform init"]

---

### ⚠️ Important Notes

[Any caveats, assumptions, or things the user should be aware of specific to this implementation]

Post this summary using:
\`\`\`bash
gh issue comment ${ISSUE_NUMBER} --body "<your summary>"
\`\`\``,
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
