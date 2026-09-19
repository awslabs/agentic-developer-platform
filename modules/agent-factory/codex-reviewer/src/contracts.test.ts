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

test("rejects a non-standard dedicated reviewer envelope", () => {
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
  assert.throws(() => parseEnvelope(JSON.stringify(envelope)), /unsupported/);
});

test("accepts the standard worker envelope selected by persona name", () => {
  const envelope = {
    version: "1.0",
    channel: "github",
    message_id: "m-2",
    arrived_at: "2026-09-18T00:00:00Z",
    tenant_id: "tenant",
    persona: "agent-codex-reviewer",
    source_ref: {
      installation_id: 10,
      repo: "aws-e/adp",
      // The standard envelope keeps its existing PR-event semantics: issue is
      // the PR number. The adapter derives the driving story from head.ref.
      issue: 5460,
      pr: 5460,
      sha: "a".repeat(40),
    },
    payload: {
      pull_request: {
        number: 5460,
        head: { ref: "agent/issue-5054", sha: "a".repeat(40) },
        base: { ref: "main" },
        html_url: "https://github.com/aws-e/adp/pull/5460",
      },
    },
  };
  const parsed = parseEnvelope(JSON.stringify(envelope));
  assert.equal(parsed.kind, "codex_pr_review");
  if (parsed.kind !== "codex_pr_review") throw new Error("expected PR envelope");
  assert.equal(parsed.repository, "aws-e/adp");
  assert.equal(parsed.installation_id, 10);
  assert.equal(parsed.pull_request.number, 5460);
});

test("accepts the standard issue mention envelope", () => {
  const parsed = parseEnvelope(
    JSON.stringify({
      version: "1.0",
      channel: "github",
      message_id: "m-3",
      arrived_at: "2026-09-19T00:00:00Z",
      tenant_id: "tenant",
      persona: "agent-codex-reviewer",
      source_ref: {
        installation_id: 10,
        repo: "aws-e/adp",
        issue: 5499,
        pr: null,
        sha: null,
      },
      payload: {
        issue: { number: 5499, title: "Review this proposal" },
        comment: { body: "@agent-codex-reviewer review this issue" },
      },
    }),
  );
  assert.equal(parsed.kind, "codex_issue_review");
  if (parsed.kind !== "codex_issue_review") throw new Error("expected issue envelope");
  assert.equal(parsed.issue.number, 5499);
  assert.equal(parsed.issue.triggering_comment, "@agent-codex-reviewer review this issue");
});

test("rejects an issue envelope whose normalized and payload issue differ", () => {
  assert.throws(
    () => parseEnvelope(
      JSON.stringify({
        version: "1.0",
        message_id: "m-4",
        arrived_at: "2026-09-19T00:00:00Z",
        tenant_id: "tenant",
        persona: "agent-codex-reviewer",
        source_ref: { installation_id: 10, repo: "aws-e/adp", issue: 5499 },
        payload: {
          issue: { number: 5500 },
          comment: { body: "@agent-codex-reviewer review this issue" },
        },
      }),
    ),
    /does not match/,
  );
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
