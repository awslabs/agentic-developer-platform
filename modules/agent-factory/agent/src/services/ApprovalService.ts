import { GitHubClient } from '../components/GitHubClient';
import { Logger } from '../components/Logger';
import { Config, ApprovalResult } from '../types';

/** Fallback wait budget when APPROVAL_TIMEOUT_MS is absent or unparseable: 30 minutes. */
const DEFAULT_TIMEOUT_MS = 30 * 60 * 1000;
/** Fallback iteration cap when APPROVAL_MAX_POLLS is absent or unparseable. */
const DEFAULT_MAX_POLLS = 60;
/** Repo permission levels that may approve. Anything else (triage/read/none) may not. */
const APPROVER_PERMISSIONS = new Set(['admin', 'maintain', 'write']);

/**
 * ApprovalService — bounded, fail-closed human approval on a GitHub issue/PR.
 *
 * Issue #4181. This previously polled `while (true)` with no deadline, no
 * iteration cap and no authorization check, swallowing fetch errors and
 * approving on a bare `/approve` substring from any commenter. Three properties
 * now hold instead:
 *
 *   1. Bounded — the wait is capped by BOTH a deadline and an iteration count.
 *      Expiry denies ('rejected'); it never approves and never waits forever.
 *   2. Fail-closed on error — persistent fetch failure returns 'unavailable'
 *      rather than looping, so a dead API cannot masquerade as a silent human.
 *   3. Authorized and named — the approver must be a non-bot user with write
 *      access or better, must not be the agent itself, and must name the
 *      specific request, so a stale `/approve` cannot authorize a later action.
 *
 * Mirrors the already-correct bounded pattern in `skill-agent.ts`.
 */
export class ApprovalService {
  private githubClient: GitHubClient;
  private logger: Logger;
  private config: Config;

  constructor(githubClient: GitHubClient, logger: Logger, config: Config) {
    this.githubClient = githubClient;
    this.logger = logger;
    this.config = config;
  }

  /**
   * Wait for an authorized human to approve or reject a specific request.
   *
   * @param issueNumber Issue or PR to poll for comments.
   * @param since Ignore comments created before this instant.
   * @param requestId Token identifying THIS request. An approve/reject intent
   *   must name it (`/approve <requestId>`) to count — a bare or stale
   *   `/approve` is ignored.
   * @returns Never a permissive value unless an authorized approver said so.
   */
  async pollForApproval(issueNumber: number, since: Date, requestId: string): Promise<ApprovalResult> {
    const timeoutMs = this.readPositiveInt(process.env.APPROVAL_TIMEOUT_MS, DEFAULT_TIMEOUT_MS);
    const maxPolls = this.readPositiveInt(process.env.APPROVAL_MAX_POLLS, DEFAULT_MAX_POLLS);
    const deadline = Date.now() + timeoutMs;
    // A transport failure must not look like a silent human: give up after this
    // many consecutive failures and report 'unavailable'.
    const maxConsecutiveErrors = Math.max(1, this.config.maxRetries);

    this.logger.info('Starting approval polling', {
      component: 'ApprovalService',
      issueNumber,
      requestId,
      timeoutMs,
      maxPolls,
    });

    // Repo permission is stable for the life of a wait; cache per call so a
    // chatty issue does not multiply API calls.
    const permissionCache = new Map<string, boolean>();
    let consecutiveErrors = 0;

    for (let attempt = 1; attempt <= maxPolls; attempt++) {
      await this.sleep(this.config.pollingInterval);

      if (Date.now() >= deadline) {
        return this.denyOnExpiry(issueNumber, requestId, 'deadline', attempt);
      }

      let comments;
      try {
        comments = await this.githubClient.getComments(issueNumber, since);
        consecutiveErrors = 0;
      } catch (err) {
        consecutiveErrors++;
        this.logger.warn('Failed to fetch comments while awaiting approval', {
          component: 'ApprovalService',
          issueNumber,
          requestId,
          attempt,
          consecutiveErrors,
          error: (err as Error).message,
        });
        if (consecutiveErrors >= maxConsecutiveErrors) {
          this.logger.error('Approval unavailable — could not reach GitHub', undefined, {
            component: 'ApprovalService',
            issueNumber,
            requestId,
            consecutiveErrors,
          });
          return {
            outcome: 'unavailable',
            feedback: `Could not reach GitHub to ask for approval after ${consecutiveErrors} attempts.`,
          };
        }
        continue;
      }

      for (const comment of comments) {
        const body = (comment.body || '').trim();
        const author = comment.author || '';

        const intent = this.parseIntent(body, requestId);
        if (!intent) continue;

        // Comments predating the request cannot answer it.
        if (comment.created_at && new Date(comment.created_at) < since) continue;

        // Bots — including this agent — cannot approve their own work.
        if (this.isMachineAuthor(author)) {
          this.logger.warn('Ignoring approval intent from a non-human author', {
            component: 'ApprovalService',
            issueNumber,
            requestId,
            author,
          });
          continue;
        }

        if (!(await this.isAuthorizedApprover(author, permissionCache))) {
          this.logger.warn('Ignoring approval intent from an unauthorized author', {
            component: 'ApprovalService',
            issueNumber,
            requestId,
            author,
          });
          continue;
        }

        if (intent.kind === 'approve') {
          this.logger.info('Approval granted', {
            component: 'ApprovalService',
            issueNumber,
            requestId,
            approver: author,
          });
          return { outcome: 'allowed-once', approver: author, comment: body };
        }

        this.logger.info('Approval rejected by approver', {
          component: 'ApprovalService',
          issueNumber,
          requestId,
          approver: author,
          feedbackLength: intent.feedback.length,
        });
        return {
          outcome: 'rejected',
          approver: author,
          feedback: intent.feedback,
          comment: body,
        };
      }
    }

    return this.denyOnExpiry(issueNumber, requestId, 'max-polls', maxPolls);
  }

  /**
   * Deny on expiry. This is the load-bearing safety property: an unanswered
   * request resolves to 'rejected', never to a permissive outcome.
   */
  private denyOnExpiry(
    issueNumber: number,
    requestId: string,
    reason: 'deadline' | 'max-polls',
    attempts: number
  ): ApprovalResult {
    this.logger.warn('Approval wait expired with no authorized answer — denying', {
      component: 'ApprovalService',
      issueNumber,
      requestId,
      reason,
      attempts,
    });
    return {
      outcome: 'rejected',
      feedback: `No authorized approval received before the wait expired (${reason}); denying by default.`,
    };
  }

  /**
   * Parse an approve/reject intent that NAMES this request.
   *
   * `/approve <requestId>` and `/reject <requestId> [feedback]` count. A bare
   * `/approve`, or one naming a different request, does not — that is what
   * stops a stale command on a busy issue from authorizing a later action.
   */
  private parseIntent(
    body: string,
    requestId: string
  ): { kind: 'approve' } | { kind: 'reject'; feedback: string } | null {
    const match = body.match(/\/(approve|reject)\s+(\S+)([\s\S]*)/i);
    if (!match) return null;

    const [, verb, named, rest] = match;
    if (named !== requestId) return null;

    return verb.toLowerCase() === 'approve'
      ? { kind: 'approve' }
      : { kind: 'reject', feedback: rest.trim() };
  }

  /** Bot or agent identity — never allowed to answer an approval request. */
  private isMachineAuthor(author: string): boolean {
    if (!author) return true;
    const lower = author.toLowerCase();
    if (lower.endsWith('[bot]')) return true;
    if (lower.startsWith('aws-e-adp-agent-')) return true;
    if (lower === 'github-actions') return true;
    // The agent's own login, when the runtime provides it.
    const self = (process.env.GITHUB_ACTOR || '').toLowerCase();
    if (self && lower === self) return true;
    return false;
  }

  /** Write access or better on the repo. Fails closed if the lookup errors. */
  private async isAuthorizedApprover(author: string, cache: Map<string, boolean>): Promise<boolean> {
    const cached = cache.get(author);
    if (cached !== undefined) return cached;

    let authorized = false;
    try {
      const permission = await this.githubClient.getUserPermission(author);
      authorized = APPROVER_PERMISSIONS.has((permission || '').toLowerCase());
    } catch (err) {
      this.logger.warn('Permission lookup failed — treating author as unauthorized', {
        component: 'ApprovalService',
        author,
        error: (err as Error).message,
      });
      authorized = false;
    }

    cache.set(author, authorized);
    return authorized;
  }

  /**
   * Parse a positive integer from env, resolving anything missing, unparseable,
   * zero or negative to the safe default. Critically, a bad value must never
   * resolve to "unbounded" — that is the bug this issue fixes.
   */
  private readPositiveInt(raw: string | undefined, fallback: number): number {
    const parsed = parseInt(raw ?? '', 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}
