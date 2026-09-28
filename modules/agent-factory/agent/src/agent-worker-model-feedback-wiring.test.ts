import * as fs from 'fs';
import * as path from 'path';

/**
 * Guards the worker's model-policy feedback call site (#2293 / PMM-07).
 *
 * The behavioral coverage for building, deduplicating and posting the notice
 * lives in `model-policy-feedback.test.ts`, which drives the real
 * orchestration with the GitHub post mocked. What cannot be asserted there is
 * that `agent-worker.ts` actually routes through that orchestration and hands
 * it GitHub's authorship attestation: a worker that fetched comments without
 * `viewerDidAuthor`, or that re-implemented a marker-only dedup at the call
 * site, would leave those tests green while restoring the forgeable
 * suppression this slice fixes. These are deliberately source assertions for
 * that wiring only.
 */
const source = fs.readFileSync(path.join(__dirname, 'agent-worker.ts'), 'utf-8');

describe('agent-worker model-policy feedback wiring', () => {
  it('delegates the posting decision to the audited orchestration', () => {
    expect(source).toContain('deliverModelPolicyFeedback({');
    // Matched without assuming the import's formatting: it is a multi-line named
    // import now that the bounded adapter factory is imported alongside.
    expect(source).toMatch(/import \{[\s\S]*?deliverModelPolicyFeedback,?[\s\S]*?\} from '\.\/model-policy-feedback'/);
  });

  it('does not re-implement a marker-only dedup at the call site', () => {
    // The pre-fix call site branched on feedbackAlreadyPosted() directly, which
    // trusted any comment carrying the marker.
    expect(source).not.toContain('feedbackAlreadyPosted(');
    expect(source).not.toContain('modelPolicyFeedback.body');
  });

  it('carries GitHub-attested authorship through the fetched comments', () => {
    // Without requesting and propagating viewerDidAuthor, every comment looks
    // unattributed and dedup degrades to "post every time".
    expect(source).toMatch(/viewerDidAuthor\?: boolean/);
    expect(source).toContain("viewerDidAuthor: typeof c.viewerDidAuthor === 'boolean' ? c.viewerDidAuthor : undefined");
  });

  it('gives dedup its own complete lookup, not the 20-comment LLM context', () => {
    // The defect this pins: feeding getIssueComments(20) into dedup means a
    // genuine earlier notice followed by 21 unrelated comments scrolls out of
    // the window and the requester is warned again on every retry.
    expect(source).toContain('fetchCommentPage: issueCommentPageFetcher()');
    expect(source).not.toMatch(/deliverModelPolicyFeedback\(\{\s*\n\s*comments:/);
    // The dedicated lookup must request authorship itself and page the history.
    expect(source).toMatch(/query \{ repository\(/);
    expect(source).toContain('body viewerDidAuthor');
    expect(source).toContain('pageInfo { endCursor hasNextPage }');
  });

  it('routes the lookup through the bounded, validating adapter', () => {
    // The response parsing and the two timeouts must stay in
    // model-policy-feedback.ts, where they are covered without a child process.
    // A worker that went back to parsing the payload itself would reintroduce the
    // silent "malformed response becomes an empty terminal page" normalization.
    expect(source).toContain('createCommentPageFetcher({');
    expect(source).toContain('runGraphQL: runIssueCommentGraphQL');
    expect(source).toContain('buildQuery: buildIssueCommentPageQuery');
  });

  it('runs the GraphQL query as an argument vector with a timeout', () => {
    // Two defects pinned together. `JSON.stringify` is JSON encoding, not shell
    // escaping, so building a shell string left `$`, backticks and `;` live; and
    // the call had no timeout at all, so a wedged `gh` blocked the run forever.
    const adapter = source.slice(
      source.indexOf('async function runIssueCommentGraphQL'),
      source.indexOf('function issueCommentPageFetcher'),
    );
    expect(adapter).toContain('execFile');
    expect(adapter).toMatch(/\['api', 'graphql', '-f', `query=\$\{query\}`\]/);
    expect(adapter).toContain('timeout: timeoutMs');
    // Neither the shell-string form nor the untimed helpers may come back.
    expect(adapter).not.toContain('execSync');
    expect(adapter).not.toMatch(/\bgh\(`api graphql/);
    expect(source).not.toContain('api graphql -f query=${JSON.stringify(query)}');
  });
});
