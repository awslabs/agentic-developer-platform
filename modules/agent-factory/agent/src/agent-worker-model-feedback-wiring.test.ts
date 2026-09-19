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
    expect(source).toContain("import { deliverModelPolicyFeedback } from './model-policy-feedback'");
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
});
