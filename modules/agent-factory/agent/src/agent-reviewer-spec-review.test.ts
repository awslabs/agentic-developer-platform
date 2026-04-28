/**
 * Regression tests for the structured spec-vs-diff review contract
 * in the @agent-reviewer prompt.
 *
 * These tests verify that the reviewer agent's prompt includes the
 * spec-vs-diff review step (Step 3.4) with all required sections,
 * and that the existing security review step (Step 3.5) is preserved.
 *
 * Background: Issue #197 — the reviewer agent had no explicit instruction
 * to verify PRs against the driving issue's acceptance criteria. Reviews
 * would approve PRs that passed tests but missed parts of the issue spec
 * (e.g. committing files under agent_learning/* despite AGENTS.md forbidding it).
 */

import * as fs from 'fs';
import * as path from 'path';

// Read the source file to verify prompt content structurally.
const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

// Also verify the workflow file wires PR_NUMBER into the env block.
const WORKFLOW_PATH = path.resolve(
  __dirname,
  '../../../../.github/workflows/agent-reviewer.yml',
);
const workflow = fs.readFileSync(WORKFLOW_PATH, 'utf-8');

describe('agent-reviewer spec-vs-diff review contract', () => {
  describe('agent-worker.ts — reviewer prompt block', () => {
    it('contains the Step 3.4 spec-vs-diff review section', () => {
      expect(source).toContain(
        'Step 3.4: Spec-vs-diff Review (MANDATORY for @agent-reviewer)',
      );
    });

    it('instructs the reviewer to fetch the PR diff', () => {
      expect(source).toContain('gh pr diff');
      expect(source).toContain('gh pr view');
    });

    it('instructs the reviewer to extract acceptance criteria', () => {
      expect(source).toContain('Extract the acceptance criteria from the issue');
    });

    it('instructs the reviewer to verify each criterion against the diff', () => {
      expect(source).toContain('Verify each criterion against the diff');
    });

    it('instructs the reviewer to check for invariant violations', () => {
      expect(source).toContain('Check for invariant violations');
    });

    it('instructs the reviewer to check for must-not-commit files', () => {
      expect(source).toContain('Check for committed files that should not exist');
      expect(source).toContain('AGENTS.md');
    });

    it('requires confidence-ranked findings (HIGH / MEDIUM / LOW)', () => {
      expect(source).toContain('Categorize every finding by confidence');
      expect(source).toContain('HIGH');
      expect(source).toContain('MEDIUM');
      expect(source).toContain('LOW');
    });

    it('requires writing the review summary to data/code-review/', () => {
      expect(source).toContain('data/code-review/review-');
    });

    it('requires posting the review summary as a PR comment', () => {
      expect(source).toContain('gh pr comment');
      expect(source).toContain('--body-file data/code-review/review-');
    });

    it('includes the Acceptance criteria checklist template', () => {
      expect(source).toContain('Acceptance criteria checklist');
    });

    it('includes the Findings (by confidence) template', () => {
      expect(source).toContain('Findings (by confidence)');
    });

    it('requires an explicit APPROVE / REQUEST CHANGES / BLOCK recommendation', () => {
      expect(source).toContain('APPROVE / REQUEST CHANGES / BLOCK');
    });

    it('tells the reviewer to stop if PR_NUMBER is missing', () => {
      expect(source).toContain(
        'cannot find PR_NUMBER in the environment',
      );
    });

    it('preserves the existing Step 3.5 security review', () => {
      expect(source).toContain(
        'Step 3.5: Security Review (MANDATORY for @agent-reviewer)',
      );
    });

    it('places Step 3.4 before Step 3.5 in the source', () => {
      const step34Idx = source.indexOf('Step 3.4: Spec-vs-diff Review');
      const step35Idx = source.indexOf('Step 3.5: Security Review');
      expect(step34Idx).toBeGreaterThan(-1);
      expect(step35Idx).toBeGreaterThan(-1);
      expect(step34Idx).toBeLessThan(step35Idx);
    });
  });

  describe('agent-reviewer.yml — PR_NUMBER wiring', () => {
    it('exports PR_NUMBER to GITHUB_ENV', () => {
      expect(workflow).toContain('echo "PR_NUMBER=$PR_NUMBER" >> $GITHUB_ENV');
    });

    it('passes PR_NUMBER in the Run @agent-reviewer env block', () => {
      // The env block should contain PR_NUMBER referencing the env context
      expect(workflow).toContain('PR_NUMBER: ${{ env.PR_NUMBER }}');
    });
  });
});
