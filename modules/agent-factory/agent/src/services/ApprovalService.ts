import { GitHubClient } from '../components/GitHubClient';
import { Logger } from '../components/Logger';
import { Config, ApprovalResult } from '../types';
import { waitForPostedApproval, type PostedApproval } from './posted-approval';

export class ApprovalService {
  constructor(
    private githubClient: GitHubClient,
    private logger: Logger,
    private config: Config,
  ) {}

  async pollForApproval(issueNumber: number, request: PostedApproval): Promise<ApprovalResult> {
    const repo = this.githubClient.getRepository();
    if (!request || request.issueNumber !== issueNumber ||
        request.owner !== repo.owner || request.repo !== repo.repo) {
      return { outcome: 'unavailable', feedback: 'Approval request scope could not be verified.' };
    }
    const target = (process.env.TARGET_REPO || `${repo.owner}/${repo.repo}`).split('/');
    if (target.length !== 2 || !target[0] || !target[1]) {
      return { outcome: 'unavailable', feedback: 'Approval effect repository could not be verified.' };
    }
    const maxPolls = this.readPositiveInt(process.env.APPROVAL_MAX_POLLS, 60);
    const timeoutMs = this.readPositiveInt(process.env.APPROVAL_TIMEOUT_MS, 30 * 60_000);
    request.expiresAt = Math.min(request.expiresAt, Date.now() + timeoutMs);
    this.logger.info('Starting bound approval polling', {
      component: 'ApprovalService', issueNumber, requestId: request.requestId, runId: request.runId,
    });
    const result = await waitForPostedApproval(request,
      () => this.githubClient.getApprovalComments(issueNumber),
      () => this.githubClient.getApprovalPlan(issueNumber, request.planCommentId),
      { ...repo, runId: request.runId, tokenProvider: () => this.githubClient.getApprovalToken(),
        effectRepo: { owner: target[0], repo: target[1] } },
      this.config.pollingInterval, maxPolls, Math.max(1, this.config.maxRetries));
    return result;
  }

  private readPositiveInt(raw: string | undefined, fallback: number): number {
    const parsed = parseInt(raw ?? '', 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  }
}
