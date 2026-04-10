/**
 * Generic Skill-Driven Agent
 * 
 * Uses the SDK query API with Skills enabled for BOTH planning and execution.
 * Flow: Plan (with skills) → Post plan → Wait for approval → Execute (with skills) → PR
 * 
 * Claude decides which skills to use based on the task.
 */
/// <reference types="node" />
import { resilientQuery } from './utils/resilientQuery';
import { CloudWatchLogsClient, PutLogEventsCommand, CreateLogStreamCommand } from '@aws-sdk/client-cloudwatch-logs';
import { refreshGitHubToken, saveToS3Fallback } from './utils/ghPost';

const REPO_OWNER = process.env.REPO_OWNER || '';
const REPO_NAME = process.env.REPO_NAME || '';
const ISSUE_NUMBER = process.env.ISSUE_NUMBER || '';
const GITHUB_TOKEN = process.env.GITHUB_TOKEN || '';
const CWD = process.env.WORK_DIR || process.cwd();
const MODEL = process.env.ANTHROPIC_MODEL || 'claude-sonnet-4-5-20250929';
const GITHUB_API = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}`;

// ── CloudWatch Logger ──────────────────────────────────────────────
const LOG_GROUP = '/github-ccsdk-agent/logs';
const LOG_STREAM = `skill-agent-issue-${ISSUE_NUMBER}-${Date.now()}`;
const cwClient = new CloudWatchLogsClient({ region: process.env.AWS_REGION || 'us-east-1' });
let cwBuffer: { timestamp: number; message: string }[] = [];
let cwInitialized = false;

async function initCloudWatch(): Promise<void> {
  try {
    await cwClient.send(new CreateLogStreamCommand({
      logGroupName: LOG_GROUP,
      logStreamName: LOG_STREAM,
    }));
    cwInitialized = true;
  } catch (err: unknown) {
    if ((err as { name?: string }).name === 'ResourceAlreadyExistsException') {
      cwInitialized = true;
    } else {
      // Log group may not exist — try to create it, otherwise just skip CW logging
      console.warn('CloudWatch init failed (logs will only go to stdout):', (err as Error).message);
    }
  }
}

function cwLog(level: string, message: string, context?: Record<string, unknown>): void {
  const entry = {
    level,
    message,
    issueNumber: ISSUE_NUMBER,
    ...context,
    timestamp: new Date().toISOString(),
  };
  const line = JSON.stringify(entry);
  console.log(line);
  if (cwInitialized) {
    cwBuffer.push({ timestamp: Date.now(), message: line });
  }
}

async function flushCloudWatch(): Promise<void> {
  if (!cwInitialized || cwBuffer.length === 0) return;
  const events = cwBuffer.splice(0, cwBuffer.length);
  try {
    // Note: sequenceToken was deprecated by AWS in late 2023 and is no longer needed
    await cwClient.send(new PutLogEventsCommand({
      logGroupName: LOG_GROUP,
      logStreamName: LOG_STREAM,
      logEvents: events,
    }));
  } catch (err) {
    console.warn('CloudWatch flush failed:', (err as Error).message);
  }
}

// Flush every 5 seconds
// Note: This timer is cleaned up in main() and the error handler.
// If this module is imported elsewhere without running main(), call cleanupTimers().
const cwFlushTimer = setInterval(flushCloudWatch, 5000);

/** Cleanup function for external callers who import this module without running main() */
export function cleanupTimers(): void {
  clearInterval(cwFlushTimer);
}

async function postComment(body: string): Promise<void> {
  await fetch(`${GITHUB_API}/issues/${ISSUE_NUMBER}/comments`, {
    method: 'POST',
    headers: {
      'Authorization': `token ${GITHUB_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ body }),
  });
}

async function getLatestComments(count: number = 5): Promise<Array<{ body: string; created_at: string; author: string }>> {
  const resp = await fetch(`${GITHUB_API}/issues/${ISSUE_NUMBER}/comments`, {
    headers: { 'Authorization': `token ${GITHUB_TOKEN}` },
  });
  const comments = await resp.json() as Array<{ body: string; created_at: string; user?: { login: string } }>;
  return comments.slice(-count).map(c => ({
    body: c.body,
    created_at: c.created_at,
    author: c.user?.login || '',
  }));
}

async function waitForApproval(): Promise<{ approved: boolean; feedback: string }> {
  console.log('\n⏳ Waiting for /approve or /reject on issue...');
  const waitStartTime = new Date().toISOString();
  const botAuthors = ['github-actions[bot]', 'gateway-dev-agent', 'BedrockGateway Agent', 'MCP Onboard Agent'];

  for (let i = 0; i < 60; i++) {
    const comments = await getLatestComments(10);
    for (const comment of comments) {
      // Skip comments posted before we started waiting
      if (comment.created_at < waitStartTime) continue;
      // Skip comments from the bot itself
      if (botAuthors.some(bot => comment.author.toLowerCase().includes(bot.toLowerCase()))) continue;

      const lower = comment.body.toLowerCase().trim();
      if (lower.includes('/approve') || lower === 'approved') {
        console.log(`✅ Approval received from ${comment.author}!`);
        return { approved: true, feedback: '' };
      }
      if (lower.includes('/reject')) {
        const feedback = comment.body.replace(/\/reject\s*/i, '').trim();
        console.log(`❌ Rejected by ${comment.author}: ${feedback.substring(0, 100)}`);
        return { approved: false, feedback };
      }
    }
    console.log(`   Poll ${i + 1}/60 — waiting...`);
    await new Promise(r => setTimeout(r, 30000));
  }
  console.log('⏰ Approval timeout (30 minutes)');
  return { approved: false, feedback: 'Timeout waiting for approval' };
}

function logMessage(message: { type: string; message: { content: Array<Record<string, unknown>> } }, turnCount: number): string {
  let text = '';
  const toolsUsed: string[] = [];
  console.log(`\n--- Turn ${turnCount} ---`);
  for (const block of message.message.content) {
    if ('name' in block) {
      const toolName = block.name as string;
      const input = ('input' in block ? block.input : {}) as Record<string, unknown>;
      toolsUsed.push(toolName);
      if (toolName === 'Skill') console.log(`🎯 Skill: ${input.skill || 'invoked'}`);
      else if (toolName === 'Write') console.log(`📝 Write: ${input.file_path}`);
      else if (toolName === 'Edit') console.log(`✏️  Edit: ${input.file_path}`);
      else if (toolName === 'Read') console.log(`📖 Read: ${input.file_path}`);
      else if (toolName === 'Bash') {
        const cmd = (input.command as string || '').substring(0, 150);
        console.log(`💻 Bash: ${cmd}${cmd.length >= 150 ? '...' : ''}`);
      }
      else if (toolName === 'WebSearch') console.log(`🔍 WebSearch: ${input.query}`);
      else if (toolName === 'WebFetch') console.log(`🌐 WebFetch: ${input.url}`);
      else if (toolName === 'Glob') console.log(`📂 Glob: ${input.pattern}`);
      else if (toolName === 'Grep') console.log(`🔎 Grep: ${input.pattern}`);
      else console.log(`🔧 ${toolName}`);
    }
    if ('text' in block && typeof block.text === 'string' && (block.text as string).trim()) {
      const t = (block.text as string).substring(0, 300);
      text += block.text as string;
      console.log(`💭 ${t}${(block.text as string).length > 300 ? '...' : ''}`);
    }
  }
  cwLog('INFO', `Turn ${turnCount}`, { turn: turnCount, tools: toolsUsed, textLength: text.length });
  return text;
}

async function runQuery(prompt: string, maxTurns: number = 200): Promise<string> {
  let fullResponse = '';
  let turnCount = 0;
  let lastActivityTime = Date.now();

  // Heartbeat: log a "still alive" message if no SDK messages arrive for 60s
  const heartbeat = setInterval(() => {
    const silentSec = Math.round((Date.now() - lastActivityTime) / 1000);
    if (silentSec >= 60) {
      const msg = `💓 Heartbeat — no SDK messages for ${silentSec}s (turn ${turnCount})`;
      console.log(msg);
      cwLog('INFO', msg, { phase: 'heartbeat', silentSeconds: silentSec, turn: turnCount });
    }
  }, 30_000);

  try {
    for await (const message of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: MODEL,
          cwd: CWD,
          allowedTools: ['Skill', 'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep', 'WebSearch', 'WebFetch'],
          settingSources: ['project'],
          permissionMode: 'bypassPermissions',
          persistSession: false,
          maxTurns,
        }
      },
      maxRetries: 5,
      baseDelayMs: 10_000,
      maxDelayMs: 120_000,
      log: (msg) => cwLog('WARN', msg, { phase: 'query-retry' }),
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
          const res = message as { type: string; subtype: string; total_cost_usd?: number; num_turns?: number; duration_ms?: number; errors?: string[]; usage?: Record<string, unknown> };
          if (res.subtype === 'success') {
            const msg = `✅ Query completed — ${res.num_turns} turns, $${res.total_cost_usd?.toFixed(4) || '?'}, ${((res.duration_ms || 0) / 1000).toFixed(1)}s`;
            console.log(msg);
            cwLog('INFO', msg, { phase: 'result', subtype: res.subtype, cost: res.total_cost_usd, turns: res.num_turns, durationMs: res.duration_ms, usage: res.usage });
          } else {
            const errors = (res as { errors?: string[] }).errors || [];
            const msg = `⚠️  Query ended: ${res.subtype} — ${errors.join('; ').substring(0, 500) || 'no details'}`;
            console.log(msg);
            cwLog('ERROR', msg, { phase: 'result', subtype: res.subtype, errors, cost: res.total_cost_usd, turns: res.num_turns });
          }
          break;
        }

        case 'tool_progress': {
          const tp = message as { tool_name: string; tool_use_id: string; elapsed_time_seconds: number };
          const msg = `⏳ Tool running: ${tp.tool_name} (${tp.elapsed_time_seconds}s elapsed)`;
          console.log(msg);
          // Only log to CW every 30s to avoid noise
          if (tp.elapsed_time_seconds % 30 < 5) {
            cwLog('INFO', msg, { phase: 'tool-progress', tool: tp.tool_name, elapsedSec: tp.elapsed_time_seconds });
          }
          break;
        }

        case 'system': {
          const sys = message as { type: string; subtype: string; model?: string; tools?: string[]; mcp_servers?: Array<{ name: string; status: string }>; status?: string | null };
          if (sys.subtype === 'init') {
            const msg = `🔧 Session init — model: ${sys.model}, tools: [${(sys.tools || []).join(', ')}], MCP: [${(sys.mcp_servers || []).map(s => `${s.name}:${s.status}`).join(', ')}]`;
            console.log(msg);
            cwLog('INFO', msg, { phase: 'system-init', model: sys.model, tools: sys.tools, mcpServers: sys.mcp_servers });
          } else if (sys.subtype === 'status') {
            const msg = `📊 Status: ${sys.status || 'idle'}`;
            console.log(msg);
            cwLog('INFO', msg, { phase: 'system-status', status: sys.status });
          } else if (sys.subtype === 'hook_response') {
            const hook = message as { hook_name: string; hook_event: string; stdout: string; stderr: string; exit_code?: number };
            const msg = `🪝 Hook: ${hook.hook_name} (${hook.hook_event}) exit=${hook.exit_code ?? '?'}`;
            console.log(msg);
            if (hook.stderr) console.log(`   stderr: ${hook.stderr.substring(0, 200)}`);
            cwLog('INFO', msg, { phase: 'hook', hookName: hook.hook_name, event: hook.hook_event, exitCode: hook.exit_code });
          } else {
            console.log(`📋 System: ${sys.subtype}`);
          }
          break;
        }

        case 'user': {
          // Tool result being fed back to the model — log tool name if available
          const usr = message as { type: string; tool_use_result?: unknown; parent_tool_use_id?: string | null; isSynthetic?: boolean };
          if (usr.parent_tool_use_id) {
            console.log(`📨 Tool result returned (tool_use_id: ${usr.parent_tool_use_id})`);
          }
          break;
        }

        default: {
          // stream_event, auth_status, etc. — just note we're alive
          console.log(`📡 SDK message: ${message.type}`);
          break;
        }
      }
    }
  } finally {
    clearInterval(heartbeat);
  }

  console.log(`\n   (${turnCount} turns)`);
  return fullResponse;
}

async function main(): Promise<void> {
  if (!ISSUE_NUMBER || !REPO_OWNER || !REPO_NAME || !GITHUB_TOKEN) {
    console.error('Missing required env vars');
    process.exit(1);
  }

  await initCloudWatch();
  cwLog('INFO', 'Skill agent starting', { repo: `${REPO_OWNER}/${REPO_NAME}`, model: MODEL });

  // Fetch issue
  const issueResp = await fetch(`${GITHUB_API}/issues/${ISSUE_NUMBER}`, {
    headers: { 'Authorization': `token ${GITHUB_TOKEN}` },
  });
  const issue = await issueResp.json() as { title: string; body: string };
  console.log(`\n🤖 Skill Agent — Issue #${ISSUE_NUMBER}: ${issue.title}\n`);

  // Fetch existing comments to include in context
  const existingComments = await getLatestComments(20);
  const commentsContext = existingComments.length > 0
    ? existingComments.map((c, i) => `### Comment ${i + 1} (by ${c.author}):\n${c.body}`).join('\n\n---\n\n')
    : '';
  console.log(`📝 Found ${existingComments.length} existing comments to include in context`);

  // ========== PHASE 1: PLANNING (with skills) ==========
  console.log('📋 Phase 1: Planning...');
  cwLog('INFO', 'Phase 1: Planning started', { phase: 'planning' });
  await postComment('## 🤖 Agent Progress\n\n- [ ] Generate plan\n- [ ] Wait for approval\n- [ ] Execute plan\n- [ ] Create PR\n\n_Starting..._');

  const planPrompt = `You are an AI agent working on a GitHub issue. You have access to skills in .claude/skills/ that you can use.

## Issue #${ISSUE_NUMBER}: ${issue.title}

${issue.body}
${commentsContext ? `
## Existing Discussion / Comments

The following comments have been posted on this issue. Read them carefully - they may contain important context, decisions, research, or approvals from previous agents or users.

${commentsContext}

---
` : ''}
## Available Skills
Check .claude/skills/ for available skills. Each skill has a SKILL.md with instructions.
Key skills that may be relevant:
- webapp-testing: Playwright-based web application testing
- onboard-mcp-server: MCP server onboarding and deployment
- mcp-builder: Build custom MCP servers
- skill-creator: Create new skills
- frontend-design: Frontend design patterns

## Your Task: Create a Plan
1. Read the issue carefully
2. Check if any skills in .claude/skills/ are relevant — read their SKILL.md files
3. Research the codebase (read relevant source files)
4. Create a detailed plan with numbered steps

## Environment
- Working directory: ${CWD}
- GitHub Token available as $GITHUB_TOKEN and $GH_TOKEN
- Docker and kubectl available
- All env vars from the workflow are available

## Output Format
Respond with your plan in this format:

PLAN_START
## Plan: [Title]

**Skills to use**: [list skills you'll invoke, or "none"]

### Steps
1. [Step 1 description]
2. [Step 2 description]
...

### Files to create/modify
- [file paths]
PLAN_END

IMPORTANT: Actually read the skill files and codebase during planning. Use Read, Glob, Grep tools. Don't guess.`;

  const planResponse = await runQuery(planPrompt, 100);

  // Extract plan from response
  const planMatch = planResponse.match(/PLAN_START([\s\S]*?)PLAN_END/);
  const planText = planMatch ? planMatch[1].trim() : planResponse.substring(0, 2000);

  // Post plan for approval
  const planComment = `## 🤖 Implementation Plan\n\n${planText}\n\n---\n**To approve:** Comment \`/approve\`\n**To reject:** Comment \`/reject <feedback>\``;
  await postComment(planComment);
  console.log('\n📋 Plan posted. Waiting for approval...');

  // ========== PHASE 2: APPROVAL ==========
  const approval = await waitForApproval();
  if (!approval.approved) {
    if (approval.feedback === 'Timeout waiting for approval') {
      await postComment('## ⏰ Approval Timeout\n\nNo response received within 30 minutes. Re-trigger by adding the label again.');
    } else {
      await postComment(`## ❌ Plan Rejected\n\nFeedback: ${approval.feedback}\n\nPlease create a new issue with updated requirements.`);
    }
    cwLog('INFO', 'Agent exiting — plan not approved', { reason: approval.feedback });
    await flushCloudWatch();
    clearInterval(cwFlushTimer);
    process.exit(0);
  }

  // ========== PHASE 3: EXECUTION (with skills) ==========
  // Allow the previous Claude Code subprocess to fully exit before starting a new one
  console.log('⏳ Waiting for previous session to clean up...');
  await new Promise(r => setTimeout(r, 5000));
  console.log('\n🚀 Phase 3: Executing plan...');
  cwLog('INFO', 'Phase 3: Execution started', { phase: 'execution' });
  await postComment('## 🤖 Agent Progress\n\n- [x] Generate plan\n- [x] Approved\n- [ ] Execute plan\n- [ ] Create PR\n\n_Executing..._');

  const execPrompt = `You are an AI agent executing an approved plan. You have access to skills in .claude/skills/.

## Issue #${ISSUE_NUMBER}: ${issue.title}

${issue.body}
${commentsContext ? `
## Discussion Context

Previous comments on this issue (may contain research, decisions, or approvals):

${commentsContext}

---
` : ''}
## Approved Plan
${planText}

## Instructions
1. Execute the plan step by step
2. Use skills when the plan says to (invoke them with /skill-name or read their SKILL.md and follow instructions)
3. Write all output files to the repo
4. After completing all steps, create a git branch, commit, push, and create a PR

## Git Operations
- Branch name: agent/issue-${ISSUE_NUMBER}
- Commit message: descriptive of what was done
- Push: git push origin agent/issue-${ISSUE_NUMBER}
- Create PR: gh pr create --title "[Agent] ${issue.title}" --head agent/issue-${ISSUE_NUMBER} --base main --body "Resolves #${ISSUE_NUMBER}"

## Environment
- Working directory: ${CWD}
- GitHub Token: $GITHUB_TOKEN and $GH_TOKEN
- Docker and kubectl available
- Credentials in env vars (WEBAPP_TEST_USERNAME, WEBAPP_TEST_PASSWORD if set)

Execute the plan now. Do all the work, then create the PR at the end.`;

  await runQuery(execPrompt, 500);

  // ========== PHASE 4: DONE ==========
  await postComment('## 🤖 Agent Progress\n\n- [x] Generate plan\n- [x] Approved\n- [x] Execute plan\n- [x] Create PR\n\n_Complete!_');
  cwLog('INFO', 'Skill agent completed successfully', { phase: 'complete' });
  await flushCloudWatch();
  clearInterval(cwFlushTimer);
  console.log('\n✅ Skill Agent completed\n');

  // Exit explicitly to avoid hanging on unclosed handles (AWS SDK, etc)
  console.log('Skill agent cleanup complete, exiting');
  process.exit(0);
}

main()
  .catch(async (err) => {
    const message = (err as Error).message || String(err);
    const stack = (err as Error).stack || '';
    console.error('Fatal error:', err);

    cwLog('ERROR', 'Skill agent fatal error', {
      error: message,
      stack: stack.substring(0, 2000),
      phase: 'fatal'
    });
    await flushCloudWatch();
    clearInterval(cwFlushTimer);

    // Post a more helpful error comment that distinguishes transient vs permanent failures
    const isTransient = /fetch failed|rate.?limit|429|503|502|timeout|econnreset|overloaded/i.test(message);
    const errorComment = isTransient
      ? `## ⚠️ Agent Hit Transient API Error\n\n\`\`\`\n${message.substring(0, 1000)}\n\`\`\`\n\nThis looks like a rate limit or network issue. The agent retried multiple times but couldn't recover.\n\nCloudWatch logs: \`${LOG_GROUP}\` → \`${LOG_STREAM}\`\n\n**To retry:** Re-add the trigger label or comment \`/retry\`.`
      : `## ❌ Agent Error\n\n\`\`\`\n${message.substring(0, 1000)}\n\`\`\`\n\nCloudWatch logs: \`${LOG_GROUP}\` → \`${LOG_STREAM}\``;

    await postComment(errorComment);
    process.exit(1);
  });
