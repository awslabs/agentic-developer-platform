import { IssueContext, AgentState, Plan, ApprovalResult, ChecklistItem } from '../types';
import { Logger } from './Logger';
import { GitHubClient } from './GitHubClient';
import { StateManager } from './StateManager';
import { ProgressTracker } from './ProgressTracker';
import { PlanningAgent } from './PlanningAgent';
import { MCPOnboardPlanningAgent } from './MCPOnboardPlanningAgent';
import { CodeGenerationAgent } from './CodeGenerationAgent';
import { WorkspaceManager } from './WorkspaceManager';
import { ApprovalService } from '../services/ApprovalService';
import { ErrorRecoveryService } from '../services/ErrorRecoveryService';

const MAX_PLAN_REVISIONS = 5;

export class AgentOrchestrator {
  private logger: Logger;
  private githubClient: GitHubClient;
  private stateManager: StateManager;
  private progressTracker: ProgressTracker;
  private planningAgent: PlanningAgent;
  private mcpOnboardAgent: MCPOnboardPlanningAgent;
  private codeGenAgent: CodeGenerationAgent;
  private workspaceManager: WorkspaceManager;
  private approvalService: ApprovalService;
  private errorRecovery: ErrorRecoveryService;
  private state: AgentState | null = null;

  constructor(
    logger: Logger,
    githubClient: GitHubClient,
    stateManager: StateManager,
    workspaceManager: WorkspaceManager,
    approvalService: ApprovalService,
    errorRecovery: ErrorRecoveryService,
    issueNumber: number
  ) {
    this.logger = logger;
    this.githubClient = githubClient;
    this.stateManager = stateManager;
    this.workspaceManager = workspaceManager;
    this.approvalService = approvalService;
    this.errorRecovery = errorRecovery;
    this.progressTracker = new ProgressTracker(githubClient, logger, issueNumber);
    this.planningAgent = new PlanningAgent(logger);
    this.mcpOnboardAgent = new MCPOnboardPlanningAgent(logger);
    this.codeGenAgent = new CodeGenerationAgent(logger, this.progressTracker);
  }

  async run(issueContext: IssueContext): Promise<void> {
    console.log('\n' + '='.repeat(60));
    console.log(`🚀 AGENT STARTED - Issue #${issueContext.issueNumber}`);
    console.log(`   ${issueContext.issueTitle}`);
    console.log('='.repeat(60) + '\n');

    const workDir = await this.workspaceManager.createWorkspace(String(issueContext.issueNumber));
    const repoDir = this.workspaceManager.getRepoPath(workDir);
    console.log(`📂 Workspace: ${workDir}`);

    this.state = {
      issueContext,
      phase: 'planning',
      plan: null,
      checklistCommentId: null,
      planCommentId: null,
      workDir,
      startTime: new Date().toISOString(),
      errorCount: 0,
    };

    try {
      await this.handlePlanning(repoDir);
      
      // Approval loop with revision support
      let revisionCount = 0;
      let approved = false;
      
      while (!approved && revisionCount < MAX_PLAN_REVISIONS) {
        console.log('\n⏳ Waiting for /approve or /reject command on GitHub issue...');
        const approval = await this.waitForApproval();
        
        if (approval.approved) {
          approved = true;
          console.log('\n✅ Approval received! Starting code generation...\n');
        } else if (approval.rejected && approval.feedback) {
          revisionCount++;
          console.log(`\n🔄 Plan rejected (revision ${revisionCount}/${MAX_PLAN_REVISIONS})`);
          console.log(`   Feedback: ${approval.feedback.substring(0, 100)}...`);
          
          // Notify user we're revising
          await this.githubClient.createComment(
            issueContext.issueNumber,
            `🤖 Understood! Revising plan based on your feedback (revision ${revisionCount}/${MAX_PLAN_REVISIONS})...\n\n> ${approval.feedback.substring(0, 200)}${approval.feedback.length > 200 ? '...' : ''}`
          );
          
          // Regenerate plan with feedback
          await this.handlePlanRevision(repoDir, approval.feedback);
        }
      }
      
      if (!approved) {
        console.log(`\n❌ Max revisions (${MAX_PLAN_REVISIONS}) reached without approval.`);
        await this.githubClient.createComment(
          issueContext.issueNumber,
          `## ⚠️ Max Revisions Reached\n\nThe plan has been revised ${MAX_PLAN_REVISIONS} times without approval. Please create a new issue with clearer requirements.`
        );
        return;
      }
      
      await this.handleCodeGeneration(repoDir);
      
      console.log('\n📤 Creating pull request...');
      await this.createPullRequest(repoDir);
      console.log('✅ Pull request created!\n');

      this.state.phase = 'complete';
      await this.stateManager.saveState(this.state);
      
      console.log('\n' + '='.repeat(60));
      console.log(`🎉 AGENT COMPLETED - Issue #${issueContext.issueNumber}`);
      console.log('='.repeat(60) + '\n');
    } catch (err) {
      console.log(`\n❌ ERROR: ${(err as Error).message}\n`);
      await this.handleError(err as Error);
    } finally {
      await this.cleanup();
    }
  }

  private async handlePlanning(repoDir: string): Promise<void> {
    this.logger.info('Starting planning phase', { component: 'AgentOrchestrator' });
    
    await this.githubClient.cloneRepo(repoDir);
    
    const items: ChecklistItem[] = [
      { label: 'Analyze issue and generate plan', completed: false },
      { label: 'Wait for approval', completed: false },
      { label: 'Implement changes', completed: false },
      { label: 'Create pull request', completed: false },
    ];
    this.state!.checklistCommentId = await this.progressTracker.createChecklist(items);

    // Route to MCP onboarding agent if issue title indicates MCP onboarding
    const isMCPOnboard = this.state!.issueContext.issueTitle.toLowerCase().includes('onboard mcp server');
    
    let plan;
    let planComment;
    if (isMCPOnboard) {
      this.logger.info('Detected MCP onboarding issue — using MCPOnboardPlanningAgent with skills', { component: 'AgentOrchestrator' });
      console.log('🔌 MCP onboarding detected — using skill-enabled planning agent');
      plan = await this.mcpOnboardAgent.generatePlan(this.state!.issueContext, repoDir);
      planComment = this.mcpOnboardAgent.formatPlanComment(plan);
    } else {
      plan = await this.planningAgent.generatePlan(this.state!.issueContext, repoDir);
      planComment = this.planningAgent.formatPlanComment(plan);
    }
    
    this.state!.plan = plan;

    await this.progressTracker.updateChecklistItem(0, true);

    this.state!.planCommentId = await this.githubClient.createComment(
      this.state!.issueContext.issueNumber,
      planComment
    );

    this.state!.phase = 'awaiting_approval';
    await this.stateManager.saveState(this.state!);
  }

  private async handlePlanRevision(repoDir: string, feedback: string): Promise<void> {
    this.logger.info('Revising plan based on feedback', { component: 'AgentOrchestrator' });
    
    const previousPlan = this.state!.plan!;
    const revisedPlan = await this.planningAgent.generatePlan(
      this.state!.issueContext, 
      repoDir, 
      feedback, 
      previousPlan
    );
    this.state!.plan = revisedPlan;

    // Post revised plan
    const planComment = `## 🔄 Revised Implementation Plan\n\n` + this.planningAgent.formatPlanComment(revisedPlan);
    this.state!.planCommentId = await this.githubClient.createComment(
      this.state!.issueContext.issueNumber,
      planComment
    );

    await this.stateManager.saveState(this.state!);
  }

  private async waitForApproval(): Promise<ApprovalResult> {
    this.logger.info('Waiting for approval', { component: 'AgentOrchestrator' });
    const result = await this.approvalService.pollForApproval(
      this.state!.issueContext.issueNumber,
      new Date()
    );
    if (result.approved) {
      await this.progressTracker.updateChecklistItem(1, true);
    }
    return result;
  }

  private async handleCodeGeneration(repoDir: string): Promise<void> {
    this.logger.info('Starting code generation', { component: 'AgentOrchestrator' });
    this.state!.phase = 'code_generation';
    await this.stateManager.saveState(this.state!);

    const issueNumber = this.state!.issueContext.issueNumber;
    const MAX_CODE_GEN_ATTEMPTS = 3;
    let attempts = 0;
    let lastError: string | undefined;
    let finalResult: { success: boolean; error?: string } = { success: false };

    while (attempts < MAX_CODE_GEN_ATTEMPTS) {
      attempts++;
      console.log(`\n🔄 Code generation attempt ${attempts}/${MAX_CODE_GEN_ATTEMPTS}`);

      // Build context with retry guidance if this is a retry
      const context = { ...this.state!.issueContext };
      if (lastError) {
        context.retryGuidance = [
          `PREVIOUS ATTEMPT ${attempts - 1} FAILED. Here is what went wrong:`,
          lastError,
          '',
          'You MUST try a DIFFERENT approach this time. Do NOT repeat the same commands that failed.',
          'Common alternatives:',
          '- Docker not available? Use SQLite, buildah, or direct installs instead.',
          '- Permission denied? Try sudo, a different path, or skip that step and continue.',
          '- Package missing? Install it with apt-get, pip, or npm.',
          '- Service unavailable? Mock it or use a local alternative.',
        ].join('\n');

        await this.githubClient.createComment(
          issueNumber,
          `## 🔄 Retrying (attempt ${attempts}/${MAX_CODE_GEN_ATTEMPTS})\n\nPrevious attempt failed:\n\`\`\`\n${lastError.substring(0, 500)}\n\`\`\`\n\nTrying a different approach...`
        );
      }

      const result = await this.codeGenAgent.executePlan(this.state!.plan!, repoDir, issueNumber, context);
      finalResult = result;

      if (result.success) {
        console.log(`\n✅ Code generation succeeded on attempt ${attempts}`);
        break;
      }

      lastError = result.error || 'Unknown error — agent ended without success';
      this.logger.warn(`Code generation attempt ${attempts} failed`, {
        component: 'AgentOrchestrator',
        attempt: attempts,
        error: lastError.substring(0, 200),
      });

      // Don't retry if we've exhausted attempts
      if (attempts >= MAX_CODE_GEN_ATTEMPTS) {
        break;
      }

      // Brief pause before retry
      await new Promise(resolve => setTimeout(resolve, 3000));
    }

    // If all attempts failed, post a detailed blocker comment
    if (!finalResult.success) {
      this.logger.warn('All code generation attempts failed', { component: 'AgentOrchestrator', attempts });
      const blockerComment = [
        `## 🚫 Execution Blocked (after ${attempts} attempts)`,
        '',
        'The agent tried multiple approaches but could not complete the task.',
        '',
        `**Last error:**`,
        '```',
        (lastError || 'Unknown error').substring(0, 2000),
        '```',
        '',
        '---',
        '**To retry:** Add the `bedrock-gateway-agent` label again.',
        '**To provide guidance:** Comment with specific instructions, then re-label.',
      ].join('\n');
      await this.githubClient.createComment(
        issueNumber,
        blockerComment.substring(0, 65000)
      );
    }

    await this.progressTracker.updateChecklistItem(2, true);
  }

  private async createPullRequest(repoDir: string): Promise<void> {
    this.logger.info('Creating pull request', { component: 'AgentOrchestrator' });
    
    const branchName = `agent/issue-${this.state!.issueContext.issueNumber}`;
    await this.githubClient.createBranch(branchName, repoDir);

    try {
      await this.githubClient.commitAndPush(
        `Implement changes for #${this.state!.issueContext.issueNumber}`,
        branchName,
        repoDir
      );
    } catch (commitErr) {
      const msg = (commitErr as Error).message;
      if (msg.includes('No files to commit') || msg.includes('nothing to commit')) {
        // Agent may have already committed and pushed via Bash tool — check if branch exists on remote
        this.logger.info('Agent already committed changes via Bash, checking remote branch', { component: 'AgentOrchestrator' });
      } else {
        throw commitErr;
      }
    }

    await this.githubClient.createPR(
      `[Agent] ${this.state!.issueContext.issueTitle}`,
      this.state!.plan!.summary,
      branchName,
      this.state!.issueContext.issueNumber
    );

    await this.progressTracker.updateChecklistItem(3, true);
  }

  private async handleError(error: Error): Promise<void> {
    this.state!.errorCount++;
    
    const shouldRetry = await this.errorRecovery.handleError(error, this.state!);
    if (shouldRetry) {
      this.logger.info('Error is retryable, will be handled by caller', { component: 'AgentOrchestrator' });
      return;
    }

    this.state!.phase = 'error';
    await this.githubClient.createComment(
      this.state!.issueContext.issueNumber,
      `## ❌ Agent Error (attempt ${this.state!.errorCount})\n\n\`\`\`\n${error.message.substring(0, 2000)}\n\`\`\`\n\n---\n**To retry:** Add the \`bedrock-gateway-agent\` label again.\n**To provide guidance:** Comment with instructions, then re-label.`
    );
  }

  private async cleanup(): Promise<void> {
    this.logger.info('Cleaning up', { component: 'AgentOrchestrator' });
    await this.workspaceManager.cleanup(this.state!.workDir);
  }
}
