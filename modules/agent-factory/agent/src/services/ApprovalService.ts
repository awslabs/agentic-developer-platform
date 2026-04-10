import { GitHubClient } from '../components/GitHubClient';
import { Logger } from '../components/Logger';
import { Config, ApprovalResult } from '../types';

export class ApprovalService {
  private githubClient: GitHubClient;
  private logger: Logger;
  private config: Config;

  constructor(githubClient: GitHubClient, logger: Logger, config: Config) {
    this.githubClient = githubClient;
    this.logger = logger;
    this.config = config;
  }

  async pollForApproval(issueNumber: number, since: Date): Promise<ApprovalResult> {
    this.logger.info('Starting approval polling', { component: 'ApprovalService', issueNumber });

    while (true) {
      await this.sleep(this.config.pollingInterval);

      let comments;
      try {
        comments = await this.githubClient.getComments(issueNumber, since);
      } catch (err) {
        this.logger.warn('Failed to fetch comments, retrying...', { 
          component: 'ApprovalService', 
          error: (err as Error).message 
        });
        await this.sleep(this.config.pollingInterval);
        continue;
      }
      
      for (const comment of comments) {
        const body = comment.body.trim();
        const bodyLower = body.toLowerCase();
        
        if (bodyLower.includes('/approve')) {
          this.logger.info('Approval received', { component: 'ApprovalService' });
          return { approved: true, rejected: false, comment: body };
        }
        
        if (bodyLower.includes('/reject')) {
          // Extract feedback - everything after /reject
          const feedback = this.extractRejectionFeedback(body);
          this.logger.info('Rejection received with feedback', { 
            component: 'ApprovalService',
            feedbackLength: feedback.length 
          });
          
          return { 
            approved: false, 
            rejected: true, 
            feedback: feedback,
            comment: body 
          };
        }
      }
    }
  }

  /**
   * Extract feedback from rejection comment.
   * Removes the /reject command and returns the rest as feedback.
   */
  private extractRejectionFeedback(comment: string): string {
    // Remove /reject (case insensitive) and trim
    const feedback = comment.replace(/\/reject/gi, '').trim();
    return feedback;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}
