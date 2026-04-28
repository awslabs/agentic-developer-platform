/**
 * Regression tests for GitHub sub-issue linkage in agent-pm prompts.
 *
 * These tests verify that the PM agent's prompt blocks enforce mandatory
 * sub-issue linkage via the `addSubIssue` GraphQL mutation, and include
 * a verification step that checks `subIssues.totalCount`.
 *
 * Background: Issue #195 — the PM agent was creating child issues with
 * `gh issue create` but skipping the `addSubIssue` mutation, leaving
 * children orphaned (subIssues.totalCount = 0 on the parent).
 */

import * as fs from 'fs';
import * as path from 'path';

// Read the source file to verify prompt content structurally.
// The prompt-building functions are not exported, so we verify the
// source text directly — this catches regressions in prompt ordering.
const SOURCE_PATH = path.join(__dirname, 'agent-pm.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

describe('agent-pm sub-issue linkage prompts', () => {
  describe('buildStartPrompt — child issue creation block', () => {
    it('contains the MANDATORY sub-issue linkage section', () => {
      expect(source).toContain('MANDATORY: Creating Child Issues');
    });

    it('states that a child issue does NOT exist until addSubIssue is called', () => {
      // Source uses escaped backticks in template literals
      expect(source).toContain(
        'A child issue does NOT exist until \\`addSubIssue\\` has been called for it.'
      );
    });

    it('warns against relying on "Parent: #N" text', () => {
      expect(source).toContain(
        'Do NOT rely on "Parent: #N" text in the issue body.'
      );
    });

    it('shows addSubIssue mutation BEFORE any standalone gh issue create template', () => {
      // The addSubIssue mutation must appear in the same block as (or immediately after)
      // the gh issue create command — not in a separate later section.
      const mandatorySection = source.indexOf('MANDATORY: Creating Child Issues');
      const addSubIssueInSection = source.indexOf('addSubIssue(input:', mandatorySection);

      expect(mandatorySection).toBeGreaterThan(-1);
      expect(addSubIssueInSection).toBeGreaterThan(-1);

      // The addSubIssue call should appear within 800 chars of "STEP 1 — Create the child issue"
      const step1 = source.indexOf('STEP 1', mandatorySection);
      expect(step1).toBeGreaterThan(-1);
      expect(addSubIssueInSection - step1).toBeLessThan(800);
    });

    it('includes STEP 2 with addSubIssue immediately after STEP 1', () => {
      const step1Idx = source.indexOf('# STEP 1 — Create the child issue');
      const step2Idx = source.indexOf('# STEP 2 — Link it as a native sub-issue');
      expect(step1Idx).toBeGreaterThan(-1);
      expect(step2Idx).toBeGreaterThan(-1);
      // STEP 2 must come after STEP 1
      expect(step2Idx).toBeGreaterThan(step1Idx);
      // And within a reasonable distance (same code block)
      expect(step2Idx - step1Idx).toBeLessThan(500);
    });
  });

  describe('mandatory verification step (STEP 3)', () => {
    it('contains the subIssues.totalCount verification query', () => {
      expect(source).toContain('subIssues { totalCount nodes { number title }');
    });

    it('contains the EXPECTED vs ACTUAL count comparison', () => {
      expect(source).toContain('EXPECTED=<number of children you created>');
      expect(source).toContain(
        'if [ "$ACTUAL" -ne "$EXPECTED" ]'
      );
    });

    it('includes MISMATCH error message with re-run instructions', () => {
      expect(source).toContain('MISMATCH: expected $EXPECTED sub-issues, got $ACTUAL');
      expect(source).toContain('re-run addSubIssue for missing children');
    });

    it('forbids reporting success before verification passes', () => {
      expect(source).toContain(
        'NEVER report "sub-issues created" until STEP 3 confirms the count matches.'
      );
    });

    it('verification step appears after the creation template, not at the end of the file', () => {
      const mandatorySection = source.indexOf('MANDATORY: Creating Child Issues');
      const verifyStep = source.indexOf('STEP 3 — Verify ALL sub-issues are linked');
      const executeWorkflow = source.indexOf('Execute the workflow now.');

      expect(verifyStep).toBeGreaterThan(mandatorySection);
      // Verification must appear before the "Execute the workflow now" closing
      expect(verifyStep).toBeLessThan(executeWorkflow);
    });
  });

  describe('decomposition summary must include verified count', () => {
    it('requires verified sub-issue count in the summary comment', () => {
      // Source uses escaped backticks in template literals
      expect(source).toContain(
        '**Sub-issue verification**: N/N child issues linked (verified via \\`subIssues.totalCount\\`)'
      );
    });
  });

  describe('buildContinuePrompt — reinforces verification on output', () => {
    it('reminds to verify sub-issue linkage when generating outputs', () => {
      // The continue prompt's "Generate outputs" section should mention verification
      // Note: source file uses escaped backticks (\`) inside template literals
      expect(source).toContain(
        'You MUST verify sub-issue linkage via \\`subIssues.totalCount\\` GraphQL query BEFORE posting the summary'
      );
    });

    it('requires including verified count in the comment', () => {
      expect(source).toContain(
        'Include "Sub-issue verification: N/N child issues linked" in your comment'
      );
    });
  });

  describe('quick/simple task flow also enforces sub-issue linkage', () => {
    it('requires addSubIssue when creating child issues in simple task flow', () => {
      // The simple task "create a child issue" path must also mention addSubIssue
      const simpleTaskSection = source.indexOf('Create a child issue assigned to the right agent');
      expect(simpleTaskSection).toBeGreaterThan(-1);

      // Within the next 500 chars, it should reference addSubIssue
      const nearbyText = source.substring(simpleTaskSection, simpleTaskSection + 500);
      expect(nearbyText).toContain('addSubIssue');
    });

    it('requires verification with subIssues.totalCount in simple task flow', () => {
      const simpleTaskSection = source.indexOf('Create a child issue assigned to the right agent');
      const nearbyText = source.substring(simpleTaskSection, simpleTaskSection + 500);
      expect(nearbyText).toContain('subIssues.totalCount');
    });
  });

  describe('old patterns are removed', () => {
    it('does not contain the old weak "IMPORTANT" warning as standalone', () => {
      // The old version had this exact text at the end of the section.
      // It should be replaced by the stronger MANDATORY framing at the top.
      expect(source).not.toContain(
        '**IMPORTANT**: Do NOT rely only on "Parent: #N" text in issue body. Always use the GraphQL API to establish proper sub-issue relationships.'
      );
    });

    it('shows gh issue create followed by addSubIssue in the same code block', () => {
      // In the MANDATORY section's code block, gh issue create should be followed by addSubIssue.
      // Find the bash code block that contains STEP 1 and STEP 2.
      const mandatoryStart = source.indexOf('MANDATORY: Creating Child Issues');
      const mandatoryEnd = source.indexOf('#### Hierarchy Structure', mandatoryStart);
      const mandatorySection = source.substring(mandatoryStart, mandatoryEnd);

      // Find STEP 1 (create) and STEP 2 (link) within the section
      const step1Idx = mandatorySection.indexOf('STEP 1');
      const step2Idx = mandatorySection.indexOf('STEP 2');
      const createInStep = mandatorySection.indexOf('gh issue create', step1Idx);
      const addSubInStep = mandatorySection.indexOf('addSubIssue(input:', step2Idx);

      expect(step1Idx).toBeGreaterThan(-1);
      expect(step2Idx).toBeGreaterThan(step1Idx);
      expect(createInStep).toBeGreaterThan(-1);
      expect(addSubInStep).toBeGreaterThan(createInStep);
    });
  });
});
