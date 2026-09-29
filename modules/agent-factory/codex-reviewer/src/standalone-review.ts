/** PR mentions use the same review/fix/test loop as scheduled reviews. */
import type { CodexPullRequestReviewEnvelope, CodexEngineReviewEnvelope } from './contracts.js';
import { createReviewServices, runEngineReview, type EngineReviewServices } from './engine-review.js';
import { GitHubClient } from './github.js';
import { mergeEnabled, type ReviewRuntime, type ReviewRunResult } from './reviewer.js';

export type StandaloneReviewServices = Pick<EngineReviewServices, 'review' | 'fix' | 'wait' | 'now' | 'deliveryTimeoutMs'> & {
  github: Pick<GitHubClient, 'getPullRequest' | 'getBranch' | 'getIssue' | 'checks' | 'commentOnce' | 'merge'>;
};

export async function runStandaloneReview(
  envelope: CodexPullRequestReviewEnvelope, runtime: ReviewRuntime, supplied?: StandaloneReviewServices,
): Promise<ReviewRunResult> {
  const github = supplied?.github ?? new GitHubClient(envelope.repository,
    runtime.getGitHubToken ?? (async () => runtime.githubToken));
  const expected = envelope.pull_request.expected_head_sha;
  const number = envelope.pull_request.number;
  const initial = await github.getPullRequest(number);
  if (initial.state !== 'open' || initial.head.sha !== expected || initial.head.ref !== envelope.pull_request.head_ref
      || initial.base.ref !== envelope.pull_request.base_ref) return { status: 'stale', expected, actual: initial.head.sha };
  const controller = supplied ?? createReviewServices({ ...runtime, repository: envelope.repository });
  const assignment: CodexEngineReviewEnvelope = { ...envelope, kind: 'codex_engine_review',
    issue_number: envelope.pull_request.issue_number,
    cycle: { action: 'review', repo: envelope.repository, pr_number: number, head_sha: expected,
      findings: [], allow_story_repairs: (process.env.CODEX_REVIEWER_APPLY_FIXES ?? 'true') === 'true',
      reviewer_owned_delivery: true },
  };
  let mergeSha: string | undefined;
  let approved = false;
  const result = await runEngineReview(assignment, runtime, { ...controller, github,
    async checks(head) {
      const pr = await github.getPullRequest(number);
      const base = await github.getBranch(pr.base.ref);
      const checks = await github.checks(head);
      return { head_sha: pr.head.sha, base_sha: base.commit.sha,
        state: checks.failing.length ? 'failed' : checks.ready || checks.total === 0 ? 'passed' : 'pending',
        open: pr.state === 'open', merged: pr.merged === true,
        base_repair_required: pr.mergeable === false || pr.mergeable_state === 'dirty',
        reasons: checks.pending, checks: [], failures: checks.failing };
    },
    async deliver(review) {
      const pr = await github.getPullRequest(number);
      if (pr.head.sha !== review.sha) return { state: 'blocked', reason: 'PR head changed before merge' };
      if (pr.merged) {
        if (!pr.merge_commit_sha) throw new Error('Merged PR receipt unavailable');
        mergeSha = pr.merge_commit_sha; return { state: 'merged' };
      }
      if (pr.state !== 'open') return { state: 'blocked', reason: 'PR closed without merge' };
      if (pr.draft) return { state: 'blocked', reason: 'PR is still a draft' };
      const base = await github.getBranch(pr.base.ref);
      if (base.commit.sha !== review.reviewed_base_sha) return { state: 'repair', reason: 'PR base changed' };
      await github.commentOnce(number, `<!-- codex-review:${envelope.message_id}:${review.sha} -->`, review.body);
      if (!mergeEnabled()) { approved = true; return { state: 'blocked', reason: 'Merge explicitly disabled' }; }
      mergeSha = await github.merge(number, review.sha);
      return { state: 'merged' };
    },
  });
  if (result.merged) {
    if (!mergeSha) {
      const pr = await github.getPullRequest(number);
      if (!pr.merged || pr.head.sha !== result.sha || !pr.merge_commit_sha) throw new Error('Merged PR receipt unavailable');
      mergeSha = pr.merge_commit_sha;
    }
    return { status: 'merged', sha: result.sha, mergeSha };
  }
  if (approved) return { status: 'approved', sha: result.sha };
  const reason = 'delivery_blocked' in result ? `\n\n${result.delivery_blocked}` : '';
  await github.commentOnce(number, `<!-- codex-review-blocked:${envelope.message_id}:${result.sha} -->`, result.body + reason);
  return { status: 'changes_requested', blockers: result.report.findings.filter(f => f.severity === 'blocking').length };
}
