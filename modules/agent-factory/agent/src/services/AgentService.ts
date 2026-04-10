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
    const isRetry = await this.isRetryTriggered(githubClient, issueContext.issueNumber);
    
    this.concurrencyGuard = new ConcurrencyGuard(this.logger);
    const lockAcquired = await this.concurrencyGuard.acquireLock(String(issueContext.issueNumber), isRetry);
    
    if (!lockAcquired) {
      this.logger.warn('Another agent is running, exiting', { component: 'AgentService' });
      return;
    }

    try {
      // Check for /retry comment with additional guidance
      const retryGuidance = await this.getRetryGuidance(githubClient, issueContext.issueNumber);
      if (retryGuidance) {
        issueContext.retryGuidance = retryGuidance;
        this.logger.info('Found retry guidance', { component: 'AgentService', guidance: retryGuidance.substring(0, 100) });
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

  private async getRetryGuidance(githubClient: GitHubClient, issueNumber: number): Promise<string | undefined> {
    try {
      const comments = await githubClient.getComments(issueNumber);
      // Find the most recent /retry comment
      const retryComments = comments
        .filter(c => c.body.includes('/retry'))
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
      
      if (retryComments.length > 0) {
        const latestRetry = retryComments[0].body;
        // Extract guidance after /retry command
        const match = latestRetry.match(/\/retry\s*([\s\S]*)/);
        if (match && match[1].trim()) {
          return match[1].trim();
        }
      }
    } catch (err) {
      this.logger?.warn('Could not fetch retry guidance', { component: 'AgentService', error: (err as Error).message });
    }
    return undefined;
  }

  private async isRetryTriggered(githubClient: GitHubClient, issueNumber: number): Promise<boolean> {
    try {
      const comments = await githubClient.getComments(issueNumber);
      // Check if there's a recent /retry comment (within last 5 minutes)
      const fiveMinutesAgo = Date.now() - 5 * 60 * 1000;
      const recentRetry = comments.some(c => 
        c.body.includes('/retry') && 
        new Date(c.created_at).getTime() > fiveMinutesAgo
      );
      return recentRetry;
    } catch (err) {
      this.logger?.warn('Could not check for retry', { component: 'AgentService', error: (err as Error).message });
    }
    return false;
  }
}
