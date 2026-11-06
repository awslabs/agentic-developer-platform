import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import test from 'node:test';
import { parseEnvelope, type CodexPullRequestReviewEnvelope } from './contracts.js';
import type { EngineVerdict } from './engine-review.js';
import { runReadOnlyReview } from './read-only-review.js';
import { runStandaloneReview } from './standalone-review.js';

const exec = promisify(execFile);
const verdict: EngineVerdict = { verdict: 'approve', summary: 'Source and offline regression verified', findings: [],
  validationGaps: ['Live qualification remains separate'], stages: { functional: 'completed', security: 'completed' }, stageDetails: 'Offline tests passed' };

async function fixture(t: test.TestContext) {
  const workspace = await mkdtemp(join(tmpdir(), 'adp-source-review-'));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const git = async (...args: string[]) => (await exec('git', args, { cwd: workspace })).stdout.trim();
  await git('init', '-b', 'codex/repair');
  await git('config', 'user.name', 'Review test');
  await git('config', 'user.email', 'review@example.invalid');
  await writeFile(join(workspace, 'code.txt'), 'before\n');
  await git('add', '.'); await git('commit', '-m', 'base');
  const base = await git('rev-parse', 'HEAD');
  await writeFile(join(workspace, 'code.txt'), 'after\n');
  await git('commit', '-am', 'repair');
  const head = await git('rev-parse', 'HEAD');
  await git('checkout', '--detach', head);
  const pr = { number: 8172, state: 'closed', merged: true, merge_commit_sha: 'c'.repeat(40),
    html_url: 'https://github.com/aws-e/adp/pull/8172', title: 'Repair', body: 'Fix Browser CI fixture',
    draft: false, mergeable: null, mergeable_state: 'unknown',
    head: { ref: 'codex/repair', sha: head, repo: { full_name: 'aws-e/adp' } },
    base: { ref: 'main', sha: base, repo: { full_name: 'aws-e/adp' } } };
  const raw = { version: '1.0', channel: 'github', persona: 'agent-codex-reviewer', message_id: 'retained-run',
    arrived_at: '2026-10-18T11:43:15Z', tenant_id: 'aws-e', intent: { trigger: 'mentioned' },
    correlation: { is_human_rooted: true, root_human_id: 'owner' },
    source_ref: { installation_id: 1, repo: 'aws-e/adp', issue: 8172, pr: 8172, sha: head },
    payload: { review_only: true, pull_request: pr, issue: { number: 8172 }, sender: { type: 'User', id: 123 },
      comment: { user: { type: 'User', id: 123 }, body: 'Review-only: do not reopen, push or merge. Test the fixture correction.' } } };
  const envelope = parseEnvelope(JSON.stringify(raw)) as CodexPullRequestReviewEnvelope;
  assert.equal(envelope.kind, 'codex_pr_review');
  const runtime = { workspace, githubToken: 'test', proxyBaseUrl: 'http://localhost/openai/v1' };
  return { workspace, git, pr, raw, envelope, runtime, head };
}

test('merged human repair review publishes the actual exact-head verdict without fix or merge', async t => {
  const s = await fixture(t);
  let posts = 0;
  const result = await runReadOnlyReview(s.envelope, s.runtime, {
    github: { getPullRequest: async () => s.pr },
    review: async prompt => {
      assert.match(prompt, /bounded read-only PR source review/);
      assert.match(prompt, /Do not modify tracked files/);
      assert.match(prompt, /Test the fixture correction/);
      return verdict;
    },
    publish: async (event, body, head) => {
      posts++; assert.equal(event, 'APPROVE'); assert.equal(head, s.head);
      assert.ok(body.startsWith('## agent-codex-reviewer — APPROVE\n\n**Reviewed head:**'));
      assert.match(body, /Live qualification remains separate/);
      return { verdict_recorded: true, state: 'APPROVED', commit_id: head, review_id: 1 };
    },
  });
  assert.equal(result.status, 'read_only_reviewed'); assert.equal(posts, 1);
  assert.equal(await s.git('rev-parse', 'HEAD'), s.head);
  assert.equal(await s.git('status', '--porcelain'), '');
});

test('standalone wrapper preserves injected read-only review and publication services', async t => {
  const s = await fixture(t);
  let reviews = 0; let publications = 0;
  const forbidden = async () => assert.fail('Source review must not fix, comment or merge');
  const result = await runStandaloneReview(s.envelope, s.runtime, {
    github: { getPullRequest: async () => s.pr, getBranch: forbidden, getIssue: forbidden,
      checks: forbidden, commentOnce: forbidden, merge: forbidden },
    review: async () => { reviews++; return verdict; }, fix: forbidden, wait: forbidden,
    now: () => 0, deliveryTimeoutMs: 1,
    publish: async (_event, _body, head) => {
      publications++;
      return { verdict_recorded: true, state: 'APPROVED', commit_id: head, review_id: 10 };
    },
  });
  assert.equal(result.status, 'read_only_reviewed');
  assert.equal(reviews, 1); assert.equal(publications, 1);
});

for (const change of ['head', 'repository', 'merge']) test(`changed ${change} prevents publication after inspection`, async t => {
  const s = await fixture(t);
  let reads = 0;
  const result = await runReadOnlyReview(s.envelope, s.runtime, {
    github: { getPullRequest: async () => {
      if (++reads === 1) return s.pr;
      return { ...s.pr, merge_commit_sha: change === 'merge' ? 'd'.repeat(40) : s.pr.merge_commit_sha,
        head: { ...s.pr.head, sha: change === 'head' ? 'e'.repeat(40) : s.head,
          repo: { full_name: change === 'repository' ? 'foreign/repo' : 'aws-e/adp' } } };
    } },
    review: async () => verdict,
    publish: async () => assert.fail('Changed identity must not publish'),
  });
  assert.equal(result.status, 'stale');
});

test('read-only inspection cannot modify the source or claim approval from a fallback comment', async t => {
  const s = await fixture(t);
  await assert.rejects(runReadOnlyReview(s.envelope, s.runtime, {
    github: { getPullRequest: async () => s.pr },
    review: async () => { await writeFile(join(s.workspace, 'code.txt'), 'changed\n'); return verdict; },
    publish: async () => assert.fail('Mutation must not publish'),
  }), /changed protected/);
  await s.git('restore', 'code.txt');
  await assert.rejects(runReadOnlyReview(s.envelope, s.runtime, {
    github: { getPullRequest: async () => s.pr }, review: async () => verdict,
    publish: async () => ({ verdict_recorded: false, state: 'COMMENTED', commit_id: s.head, review_id: 1 }),
  }), /publication is incomplete/);
});

test('lost publication response is not retried or reported as approval', async t => {
  const s = await fixture(t); let posts = 0;
  await assert.rejects(runReadOnlyReview(s.envelope, s.runtime, {
    github: { getPullRequest: async () => s.pr }, review: async () => verdict,
    publish: async () => { posts++; throw new Error('response lost'); },
  }), /response lost/);
  assert.equal(posts, 1);
});

test('review-only parser refuses forged human context and preserves the ordinary branch guard', async t => {
  const s = await fixture(t);
  for (const mutate of [
    (x: typeof s.raw) => { x.payload.sender.type = 'Bot'; },
    (x: typeof s.raw) => { x.payload.comment.user.id = 999; },
    (x: typeof s.raw) => { x.correlation.is_human_rooted = false; },
    (x: typeof s.raw) => { x.intent.trigger = 'pr_opened'; },
    (x: typeof s.raw) => { x.source_ref.sha = 'b'.repeat(40); },
    (x: typeof s.raw) => { x.payload.pull_request.base.repo.full_name = 'foreign/repo'; },
  ]) {
    const raw = structuredClone(s.raw); mutate(raw);
    assert.throws(() => parseEnvelope(JSON.stringify(raw)), /invalid human review-only/);
  }
  const raw = structuredClone(s.raw); raw.payload.review_only = false;
  assert.throws(() => parseEnvelope(JSON.stringify(raw)), /not an agent issue branch/);
});
