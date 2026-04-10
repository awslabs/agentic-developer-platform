import { query } from '@anthropic-ai/claude-agent-sdk';
import { IssueContext, CodeResult } from '../types';
import { Logger } from './Logger';
import { GitHubClient } from './GitHubClient';
import { ApprovalService } from '../services/ApprovalService';

/**
 * FixOrchestrator handles /fixPR requests on pull requests.
 * 
 * Does NOT use PlanningAgent. Instead:
 * 1. Reads fix instructions
 * 2. Uses Claude directly to analyze and propose changes
 * 3. Posts proposal, waits for /approve
 * 4. Uses Claude directly to apply changes
 * 5. Commits and pushes
 */
export class FixOrchestrator {
  private logger: Logger;
  private githubClient: GitHubClient;
  private approvalService: ApprovalService;

  constructor(
    logger: Logger,
    githubClient: GitHubClient,
    _stateManager: unknown,
    _workspaceManager: unknown,
    approvalService: ApprovalService,
    private issueNumber: number
  ) {
    this.logger = logger;
    this.githubClient = githubClient;
    this.approvalService = approvalService;
  }

  async run(issueContext: IssueContext): Promise<void> {
    const fixInstructions = (process.env.FIX_INSTRUCTIONS || '').replace(/\/fixPR\s*/i, '').trim();
    const prNumber = issueContext.issueNumber;
    const prBranch = process.env.PR_BRANCH || '';
    const repoDir = process.cwd();

    console.log('\n' + '='.repeat(60));
    console.log(`🔧 FIX PR MODE - PR #${prNumber}`);
    console.log(`   Branch: ${prBranch}`);
    console.log(`   Instructions: ${fixInstructions.substring(0, 200)}`);
    console.log('='.repeat(60) + '\n');

    try {
      // Step 1: Analyze and propose
      await this.githubClient.createComment(prNumber,
        `## 🔧 Processing /fixPR\n\nAnalyzing what needs to change...`
      );

      console.log('\n📋 Analyzing codebase and generating proposal...\n');
      const proposal = await this.generateProposal(fixInstructions, repoDir);

      // Step 2: Post proposal and wait for approval
      await this.githubClient.createComment(prNumber,
        `## 📋 Proposed Fix\n\n${proposal}\n\n---\n**To approve:** Comment \`/approve\`\n**To reject:** Comment \`/reject <feedback>\``
      );

      console.log('\n⏳ Waiting for /approve or /reject...\n');
      const approval = await this.approvalService.pollForApproval(prNumber, new Date());

      if (!approval.approved) {
        await this.githubClient.createComment(prNumber,
          `## ❌ Fix Rejected\n\n${approval.feedback || 'No feedback'}\n\nUse \`/fixPR\` again with updated instructions.`
        );
        return;
      }

      // Step 3: Apply the fix (with retries)
      console.log('\n✅ Approved! Applying fix...\n');
      await this.githubClient.createComment(prNumber, `## 🔨 Applying fix...`);

      const MAX_FIX_ATTEMPTS = 3;
      let fixAttempt = 0;
      let lastFixError: string | undefined;
      let result: { filesChanged: number; summary: string } = { filesChanged: 0, summary: '' };

      while (fixAttempt < MAX_FIX_ATTEMPTS) {
        fixAttempt++;
        console.log(`\n🔄 Fix attempt ${fixAttempt}/${MAX_FIX_ATTEMPTS}`);

        const retryContext = lastFixError
          ? `\n\n## ⚠️ PREVIOUS ATTEMPT FAILED\n${lastFixError}\nTry a DIFFERENT approach. Do NOT repeat the same commands that failed.`
          : '';

        try {
          result = await this.applyFix(fixInstructions + retryContext, repoDir, prNumber);
          // If we got here without throwing, check if files were actually changed
          if (result.filesChanged > 0) break;
          lastFixError = 'No files were modified. The fix may not have been applied correctly.';
        } catch (applyErr) {
          lastFixError = (applyErr as Error).message;
          this.logger.warn(`Fix attempt ${fixAttempt} failed`, { component: 'FixOrchestrator', error: lastFixError });
        }

        if (fixAttempt < MAX_FIX_ATTEMPTS) {
          await this.githubClient.createComment(prNumber,
            `## 🔄 Retrying fix (attempt ${fixAttempt + 1}/${MAX_FIX_ATTEMPTS})\n\nPrevious attempt issue:\n\`\`\`\n${(lastFixError || '').substring(0, 500)}\n\`\`\`\n\nTrying a different approach...`
          );
          await new Promise(resolve => setTimeout(resolve, 3000));
        }
      }

      // Step 4: Commit and push
      if (result.filesChanged > 0) {
        console.log('\n📤 Committing and pushing...\n');
        try {
          await this.githubClient.commitAndPush(
            `fix: apply /fixPR feedback on PR #${prNumber}`,
            prBranch,
            repoDir
          );
        } catch (commitErr) {
          // The Claude agent may have already committed and pushed via Bash.
          // If so, the changes are already on the remote — this is not an error.
          const msg = (commitErr as Error).message;
          if (msg.includes('No files to commit') || msg.includes('nothing to commit')) {
            console.log('ℹ️  Agent already committed and pushed changes directly. Skipping orchestrator commit.');
            this.logger.info('Agent already committed changes via Bash', { component: 'FixOrchestrator' });
          } else {
            throw commitErr;
          }
        }

        await this.githubClient.createComment(prNumber,
          `## ✅ Fix Applied\n\n${result.summary}\n\nPlease review the updated changes.`
        );
      } else {
        await this.githubClient.createComment(prNumber,
          `## ⚠️ No changes were made\n\n${result.summary}`
        );
      }

      console.log('\n🎉 FIX COMPLETE\n');

    } catch (err) {
      console.error(`\n❌ ERROR: ${(err as Error).message}\n`);
      await this.githubClient.createComment(prNumber,
        `## ❌ Fix Error\n\n\`\`\`\n${(err as Error).message.substring(0, 2000)}\n\`\`\`\n\n---\n**To retry:** Comment \`/fixPR\` again with updated instructions.\n**Tip:** If the error is environment-related (Docker, permissions), mention alternatives in your instructions.`
      );
    }
  }

  private async generateProposal(instructions: string, repoDir: string): Promise<string> {
    let proposal = '';

    for await (const message of query({
      prompt: `You are analyzing a PR branch to propose a targeted fix.

## Fix Instructions:
${instructions}

## Your task:
1. Read the relevant files that need to change
2. Output a SHORT proposal listing:
   - Which files will be modified
   - What specific change will be made to each file
   - What commands will be run (install, test, etc.)

Do NOT make any changes yet. Only READ files and output your proposal.
Do NOT reimplement the entire feature. Only propose changes related to the fix instructions.`,
      options: {
        model: process.env.ANTHROPIC_MODEL || 'claude-sonnet-4-5-20250929',
        allowedTools: ['Read', 'Glob', 'Grep'],
        permissionMode: 'bypassPermissions',
        maxTurns: 20,
      }
    })) {
      if (message.type === 'assistant') {
        for (const block of message.message.content) {
          if ('text' in block && block.text) {
            proposal += block.text;
          }
          if ('name' in block) {
            console.log(`📖 ${(block as any).name}: ${JSON.stringify((block as any).input || {}).substring(0, 100)}`);
          }
        }
      }
    }

    return proposal || 'Could not generate proposal.';
  }

  private async applyFix(instructions: string, repoDir: string, prNumber: number): Promise<{ filesChanged: number; summary: string }> {
    let summary = '';
    let turnCount = 0;
    const filesModified: string[] = [];

    for await (const message of query({
      prompt: `You are applying a targeted fix to an existing PR.

## Fix Instructions:
${instructions}

## Rules:
- ONLY modify files related to the fix instructions
- Do NOT rewrite or regenerate files that already work
- After changes, run the relevant tests and show output
- Read .github-agent/CLAUDE.md and coding-standards.md for project rules
- If a command fails, DIAGNOSE and FIX the problem — do not just report the error
- Docker not available? Use SQLite, buildah, or direct installs instead
- Missing package? Install it with apt-get, pip, or npm
- Try at least 3 different approaches before giving up on any step
- Do NOT run git commit or git push — the orchestrator handles that after you finish

Apply the fix now.`,
      options: {
        model: process.env.ANTHROPIC_MODEL || 'claude-sonnet-4-5-20250929',
        allowedTools: ['Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep'],
        permissionMode: 'bypassPermissions',
        maxTurns: 100,
      }
    })) {
      if (message.type === 'assistant') {
        turnCount++;
        for (const block of message.message.content) {
          if ('name' in block) {
            const toolName = (block as any).name;
            const input = (block as any).input || {};
            if (toolName === 'Write' || toolName === 'Edit') {
              filesModified.push(input.file_path || '');
              console.log(`✏️  ${toolName}: ${input.file_path}`);
            } else if (toolName === 'Bash') {
              console.log(`💻 Bash: ${(input.command || '').substring(0, 100)}`);
            } else if (toolName === 'Read') {
              console.log(`📖 Read: ${input.file_path}`);
            } else {
              console.log(`🔧 ${toolName}`);
            }
          }
          if ('text' in block && block.text) {
            summary += block.text;
            const text = block.text.substring(0, 200);
            console.log(`💭 ${text}${block.text.length > 200 ? '...' : ''}`);
          }
        }
      }

      if (message.type === 'result') {
        console.log(`\n✅ Done. Turns: ${turnCount}, Cost: $${(message as any).total_cost_usd?.toFixed(4) || 'N/A'}`);
      }
    }

    const unique = [...new Set(filesModified)];
    return {
      filesChanged: unique.length,
      summary: `### Files modified (${unique.length}):\n\`\`\`\n${unique.join('\n')}\n\`\`\`\n\n${summary.substring(0, 2000)}`
    };
  }
}
