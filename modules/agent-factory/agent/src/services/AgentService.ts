import { ConfigLoader } from '../components/ConfigLoader';
import { Logger } from '../components/Logger';
import { TokenManager } from '../components/TokenManager';
import { GitHubClient } from '../components/GitHubClient';
import { StateManager } from '../components/StateManager';
import { WorkspaceManager } from '../components/WorkspaceManager';
import { InputHandler } from '../components/InputHandler';
import { ConcurrencyGuard } from '../components/ConcurrencyGuard';
import { AgentOrchestrator } from '../components/AgentOrchestrator';
import { ApprovalService } from './ApprovalService';
import { ErrorRecoveryService } from './ErrorRecoveryService';
import { selectLegacyRetry } from '../utils/legacy-retry';
import { resolveSuperpowerTriggerComment } from '../utils/superpower-guidance';
import * as fs from 'fs';
import { randomUUID } from 'crypto';

export class AgentService {
  private configLoader: ConfigLoader;
  private logger: Logger | null = null;
  private tokenManager: TokenManager | null = null;
  private concurrencyGuard: ConcurrencyGuard | null = null;

  constructor() {
    this.configLoader = new ConfigLoader();
  }

  async run(): Promise<void> {
    const config = await this.configLoader.load();
    
    // Initialize logger with temporary ID until we have issue context
    this.logger = new Logger(config, 'init');
    await this.logger.initialize();

    this.logger.info('Agent starting', { component: 'AgentService' });

    const inputHandler = new InputHandler(this.logger);
    const issueContext = await inputHandler.getIssueContext();

    // Reinitialize logger with actual issue ID
    await this.logger.close();
    this.logger = new Logger(config, String(issueContext.issueNumber));
    await this.logger.initialize();

    // Initialize token manager and GitHub client early to check for retry
    this.tokenManager = new TokenManager(this.configLoader, this.logger);
    await this.tokenManager.initialize();

    const githubClient = new GitHubClient(this.tokenManager, this.logger);
    await githubClient.initialize(issueContext.owner, issueContext.repo, issueContext.issueNumber);

    // Check if this is a retry - if so, we should override any existing lock
    const retry = await this.findAuthorizedRetry(githubClient, issueContext.owner, issueContext.repo, issueContext.issueNumber);
    const isRetry = retry !== null;
    
    this.concurrencyGuard = new ConcurrencyGuard(this.logger);
    const lockAcquired = await this.concurrencyGuard.acquireLock(String(issueContext.issueNumber), isRetry);
    
    if (!lockAcquired) {
      this.logger.warn('Another agent is running, exiting', { component: 'AgentService' });
      return;
    }

    try {
      // Check for /retry comment with additional guidance
      if (retry?.guidance) {
        issueContext.retryGuidance = retry.guidance;
        this.logger.info('Verified retry guidance', { component: 'AgentService' });
      }

      const workspaceManager = new WorkspaceManager(this.logger);
      const stateManager = new StateManager(this.logger, workspaceManager);
      const approvalService = new ApprovalService(githubClient, this.logger, config);
      const errorRecovery = new ErrorRecoveryService(this.logger, config);

      const orchestrator = new AgentOrchestrator(
        this.logger,
        githubClient,
        stateManager,
        workspaceManager,
        approvalService,
        errorRecovery,
        issueContext.issueNumber
      );

      await orchestrator.run(issueContext);
      this.logger.info('Agent completed successfully', { component: 'AgentService' });
    } finally {
      await this.cleanup();
    }
  }

  private async cleanup(): Promise<void> {
    this.tokenManager?.stopRefreshTimer();
    await this.concurrencyGuard?.releaseLock();
    await this.logger?.close();
  }

  private async findAuthorizedRetry(
    githubClient: GitHubClient, owner: string, repo: string, issueNumber: number,
  ): Promise<{ guidance?: string } | null> {
    try {
      if (!process.env.GITHUB_EVENT_PATH) return null;
      const event = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8')) as
        { sender?: { id?: number } };
      const triggerId = resolveSuperpowerTriggerComment(event, owner, repo, issueNumber);
      if (!triggerId || !event.sender?.id) return null;
      const target = (process.env.TARGET_REPO || `${owner}/${repo}`).split('/');
      if (target.length !== 2 || !target[0] || !target[1]) return null;
      return await selectLegacyRetry(await githubClient.getApprovalComments(issueNumber), {
        owner, repo, runId: process.env.ADP_MESSAGE_ID || randomUUID(),
        effectRepo: { owner: target[0], repo: target[1] },
        tokenProvider: () => githubClient.getApprovalToken(),
      }, triggerId, event.sender.id);
    } catch {
      this.logger?.warn('Retry authority could not be verified', { component: 'AgentService' });
      return null;
    }
  }
}
