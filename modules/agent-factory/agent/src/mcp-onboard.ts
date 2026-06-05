/**
 * Simple MCP Server Onboarding Agent
 * Uses the SDK query API with Skills enabled.
 * No orchestrator, no planning agent — just follows the skill.
 */
/// <reference types="node" />
import { resilientQuery } from './utils/resilientQuery';
import { wrapUntrusted } from './utils/trust-boundary';

async function main(): Promise<void> {
  const issueNumber = process.env.ISSUE_NUMBER;
  const repoOwner = process.env.REPO_OWNER;
  const repoName = process.env.REPO_NAME;
  const githubToken = process.env.GITHUB_TOKEN;
  const cwd = process.env.WORK_DIR || process.cwd();

  if (!issueNumber || !repoOwner || !repoName || !githubToken) {
    console.error('Missing required env vars: ISSUE_NUMBER, REPO_OWNER, REPO_NAME, GITHUB_TOKEN');
    process.exit(1);
  }

  // Fetch issue content
  const issueResp = await fetch(
    `https://api.github.com/repos/${repoOwner}/${repoName}/issues/${issueNumber}`,
    { headers: { Authorization: `token ${githubToken}` } }
  );
  const issue = await issueResp.json() as { title: string; body: string };

  console.log(`\n🔌 MCP Onboard Agent — Issue #${issueNumber}: ${issue.title}\n`);

  const prompt = `You are onboarding an MCP server. Read and follow the /onboard-mcp-server skill at .claude/skills/onboard-mcp-server/SKILL.md exactly.

## Issue #${issueNumber}: ${issue.title}

${wrapUntrusted(issue.body)}

## Environment
- GitHub Token is available as $GITHUB_TOKEN env var
- Docker is available (docker info works)
- kubectl is available and configured for the EKS cluster
- Git is configured (user.email and user.name set)
- Working directory: ${cwd}
- Repo: ${repoOwner}/${repoName}

## Communication
Post comments on the issue using:
curl -s -X POST -H "Authorization: token $GITHUB_TOKEN" -H "Content-Type: application/json" \\
  "https://api.github.com/repos/${repoOwner}/${repoName}/issues/${issueNumber}/comments" \\
  -d '{"body": "YOUR_COMMENT_HERE"}'

Poll for approval using SHORT intervals (poll every 30 seconds, log each attempt):
for i in $(seq 1 60); do
  RESULT=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \\
    "https://api.github.com/repos/${repoOwner}/${repoName}/issues/${issueNumber}/comments" | \\
    python3 -c "import json,sys; comments=json.load(sys.stdin); print('APPROVED' if any('approved' in c['body'].lower() for c in comments[-5:]) else 'WAITING')")
  echo "Poll attempt $i/60: $RESULT"
  if [ "$RESULT" = "APPROVED" ]; then echo "APPROVED"; break; fi
  sleep 30
done

## Instructions
1. Follow ALL stages in the skill — research, config, test, post plan, wait for approval, deploy to EKS, create PR
2. Do NOT skip the kubectl deployment stage
3. Post the deployment plan as an issue comment and WAIT for approval before deploying
4. After approval, run kubectl apply to deploy, then create a PR with all files`;

  let turnCount = 0;

  for await (const message of resilientQuery({
    queryParams: {
      prompt,
      options: {
        model: process.env.ANTHROPIC_MODEL || 'claude-sonnet-4-5-20250929',
        cwd,
        allowedTools: ['Skill', 'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep', 'WebSearch', 'WebFetch'],
        settingSources: ['project'],
        permissionMode: 'bypassPermissions',
        maxTurns: 500,
      }
    },
    maxRetries: 5,
    baseDelayMs: 10_000,
    maxDelayMs: 120_000,
    log: console.log,
  })) {
    if (message.type === 'assistant') {
      turnCount++;
      console.log(`\n--- Turn ${turnCount} ---`);

      for (const block of message.message.content) {
        if ('name' in block) {
          const toolName = (block as { name: string }).name;
          const input = 'input' in block ? (block as { input: Record<string, unknown> }).input : {};

          if (toolName === 'Skill') {
            console.log(`🎯 Skill: ${input.skill_name || 'invoked'}`);
          } else if (toolName === 'Write') {
            console.log(`📝 Write: ${input.file_path}`);
          } else if (toolName === 'Edit') {
            console.log(`✏️  Edit: ${input.file_path}`);
          } else if (toolName === 'Read') {
            console.log(`📖 Read: ${input.file_path}`);
          } else if (toolName === 'Bash') {
            const cmd = (input.command as string || '').substring(0, 150);
            console.log(`💻 Bash: ${cmd}${cmd.length >= 150 ? '...' : ''}`);
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
        if ('text' in block && typeof block.text === 'string' && block.text.trim()) {
          const text = block.text.substring(0, 300);
          console.log(`💭 ${text}${block.text.length > 300 ? '...' : ''}`);
        }
      }
    }
  }

  console.log(`\n✅ MCP Onboard Agent completed (${turnCount} turns)\n`);
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
