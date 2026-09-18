import assert from "node:assert/strict";
import test from "node:test";
import {
  issueFromAgentBranch,
  parseEnvelope,
  parseVerdict,
  requiresChanges,
} from "./contracts.js";

test("extracts the story from an agent branch", () => {
  assert.equal(issueFromAgentBranch("agent/issue-5054"), 5054);
  assert.equal(issueFromAgentBranch("agent/issue-5054-followup"), 5054);
  assert.equal(issueFromAgentBranch("agent/issue-5460/other"), null);
});

test("rejects an envelope whose story and branch disagree", () => {
  const envelope = {
    version: "1.0",
    kind: "codex_pr_review",
    message_id: "m-1",
    arrived_at: "2026-09-18T00:00:00Z",
    tenant_id: "tenant",
    installation_id: 10,
    repository: "aws-e/adp",
    pull_request: {
      number: 5460,
      issue_number: 5460,
      head_ref: "agent/issue-5054",
      base_ref: "main",
      expected_head_sha: "a".repeat(40),
      html_url: "https://github.com/aws-e/adp/pull/5460",
    },
  };
  assert.throws(() => parseEnvelope(JSON.stringify(envelope)), /does not match/);
});

test("a blocking finding always forces request_changes", () => {
  const verdict = parseVerdict(
    JSON.stringify({
      verdict: "approve",
      summary: "looks fine",
      validationGaps: [],
      findings: [
        {
          id: "B1",
          title: "lost update",
          impact: "high",
          confidence: "high",
          blocking: true,
          fixClass: "author_required",
          details: "state can be lost",
          file: "src/state.ts",
          line: 10,
          recommendedFix: "serialize updates",
        },
      ],
    }),
  );
  assert.equal(verdict.verdict, "request_changes");
});

test("an explicit request_changes verdict blocks approval without blocking findings", () => {
  assert.equal(
    requiresChanges({
      verdict: "request_changes",
      summary: "The validation gap must be resolved before approval.",
      findings: [],
      validationGaps: ["Required integration test was not run"],
    }),
    true,
  );
});
