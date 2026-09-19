import assert from "node:assert/strict";
import test from "node:test";
import {
  formatFixesPushedComment,
  formatReviewComment,
  GitHubClient,
} from "./github.js";

test("review comments identify the exact reviewed head and engine", () => {
  const body = formatReviewComment(
    { verdict: "approve", summary: "Verified.", findings: [], validationGaps: [] },
    "a".repeat(40),
    "Codex SDK 0.155.1",
  );
  assert.match(body, /Reviewed head/);
  assert.match(body, /Codex SDK 0\.155\.1/);
  assert.match(body, /Blockers:\*\* 0/);
});

test("a repair push identifies the repaired tree as reviewed and approved", () => {
  const body = formatFixesPushedComment(
    { verdict: "approve", summary: "Local repair review passed.", findings: [], validationGaps: [] },
    "b".repeat(40),
    "Codex SDK 0.155.1",
  );
  assert.match(body, /FIXES PUSHED AND APPROVED/);
  assert.doesNotMatch(body, /— APPROVE/);
});

test("a verdict is posted as a PR comment with the default GitHub identity", async () => {
  const originalFetch = globalThis.fetch;
  let requested = "";
  let payload = "";
  globalThis.fetch = async (input, init) => {
    requested = String(input);
    payload = String(init?.body ?? "");
    return new Response(JSON.stringify({ id: 1 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    await github.comment(5471, "Reviewed current head.");
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.match(requested, /\/repos\/aws-e\/adp\/issues\/5471\/comments$/);
  assert.deepEqual(JSON.parse(payload), { body: "Reviewed current head." });
});

test("shared live-fleet diagnostics do not block an otherwise ready merge", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    const body = url.endsWith("/check-runs?per_page=100")
      ? {
          check_runs: [
            { name: "Codex Adapter Unit Tests", status: "completed", conclusion: "success" },
            { name: "GitLab Live Fleet", status: "completed", conclusion: "failure" },
          ],
        }
      : { state: "success", statuses: [] };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    assert.deepEqual(await github.checks("a".repeat(40)), {
      ready: true,
      failing: [],
      pending: [],
      total: 1,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("unavailable legacy statuses do not hide accessible check runs", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/status?per_page=100")) {
      return new Response(JSON.stringify({ message: "Resource not accessible by integration" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(
      JSON.stringify({
        check_runs: [
          { name: "Codex Adapter Unit Tests", status: "completed", conclusion: "success" },
        ],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  try {
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    assert.deepEqual(await github.checks("a".repeat(40)), {
      ready: true,
      failing: [],
      pending: [],
      total: 1,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
