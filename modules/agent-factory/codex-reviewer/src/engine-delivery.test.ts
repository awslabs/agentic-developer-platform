import assert from "node:assert/strict";
import test from "node:test";
import { deliverEngineReview } from "./engine-delivery.js";
import type { EngineReviewResult } from "./engine-review.js";
import type { CodexEngineReviewEnvelope } from "./contracts.js";
import type { ReviewerChecks } from "./reviewer-checks.js";
import { GitHubRequestError } from "./github.js";

function fixture() {
  const head = "a".repeat(40), base = "b".repeat(40);
  const events: string[] = [];
  let merged = false, queued = false;
  const pr = { number: 7, head: { sha: head, ref: "story" }, base: { sha: base, ref: "main" },
    state: "open", title: "Story", body: "Story", html_url: "url", draft: false, mergeable: true, mergeable_state: "clean" };
  const github = {
    getPullRequest: async () => ({ ...pr, merged }),
    getBranch: async () => ({ commit: { sha: base } }),
    merge: async (number: number, sha: string, method?: string) => {
      assert.equal(number, 7); assert.equal(sha, head); assert.equal(method, "rebase");
      events.push("merge"); merged = true; return "c".repeat(40);
    },
    queueEntry: async () => queued ? "queue-id" : null,
    enqueue: async () => { events.push("enqueue"); queued = true; },
  };
  const result: EngineReviewResult = { status: "engine_reviewed", sha: head, merged: false, reviewed_base_sha: base,
    repair_base_sha: null, body: "Reviewed", report: { verdict: "approve", findings: [],
      stages: { functional: "completed", security: "completed" }, stage_details: { functional: "done", security: "done" } } };
  const envelope: CodexEngineReviewEnvelope = { kind: "codex_engine_review", version: "1.0", message_id: "review-run", arrived_at: "now",
    tenant_id: "tenant", installation_id: 1, repository: "org/repo", issue_number: 42,
    cycle: { repo: "org/repo", pr_number: 7, head_sha: head, action: "review", findings: [], allow_story_repairs: true, reviewer_owned_delivery: true } };
  const observation: ReviewerChecks = { head_sha: head, base_sha: base, state: "passed", open: true, merged: false,
    base_repair_required: false, checks: [], failures: [], reasons: [], merge_state: "eligible", merge_reasons: [], merge_method: "rebase", pr_node_id: "PR_7" };
  const publish = async () => { events.push("publish"); };
  const checks = async (sha: string, forMerge: boolean) => {
    assert.equal(sha, head); assert.equal(forMerge, true); events.push("authorize"); return observation;
  };
  return { github, result, envelope, observation, publish, checks, events, pr, merged: () => { merged = true; },
    deliver: () => deliverEngineReview(github, result, envelope, publish, checks) };
}

test("publishes evidence, checks current permission, merges expected head and verifies delivery", async () => {
  const f = fixture();
  assert.equal((await f.deliver()).state, "merged");
  assert.deepEqual(f.events, ["publish", "authorize", "merge"]);
  assert.equal((await f.deliver()).state, "merged");
  assert.equal(f.events.filter(x => x === "merge").length, 1);
});

test("a merge with a lost response is observed without repeating review or merge", async () => {
  const f = fixture();
  f.github.merge = async () => { f.events.push("merge"); f.merged(); throw new Error("response lost"); };
  assert.equal((await f.deliver()).state, "merged");
  assert.equal((await f.deliver()).state, "merged");
  assert.deepEqual(f.events, ["publish", "authorize", "merge"]);
});

test("an unknown merge outcome remains retryable and never claims completion", async () => {
  const f = fixture();
  f.github.merge = async () => { throw new Error("connection lost"); };
  await assert.rejects(f.deliver(), error => error instanceof Error && "retryable" in error && error.retryable === true);
});

for (const changed of ["head", "base", "checks", "permission", "publication"] as const) {
  test(`${changed} changing before merge prevents mutation`, async () => {
    const f = fixture();
    if (changed === "head") f.pr.head.sha = "e".repeat(40);
    if (changed === "base") f.observation.base_sha = "e".repeat(40);
    if (changed === "checks") f.observation.state = "failed";
    if (changed === "permission") f.observation.merge_state = "blocked";
    if (changed === "publication") {
      await assert.rejects(deliverEngineReview(f.github, f.result, f.envelope, async () => { throw new Error("upload failed"); }, f.checks));
    } else {
      assert.equal((await f.deliver()).state, changed === "base" || changed === "checks" ? "repair" : "blocked");
    }
    assert.ok(!f.events.includes("merge"));
  });
}

test("merge queue admission waits for actual merge and is not repeated", async () => {
  const f = fixture(); f.observation.merge_method = "queue";
  assert.equal((await f.deliver()).state, "pending");
  f.observation.base_sha = "d".repeat(40); // The queue tests the current integration itself.
  assert.equal((await f.deliver()).state, "pending");
  assert.equal(f.events.filter(x => x === "enqueue").length, 1);
  f.merged();
  assert.equal((await f.deliver()).state, "merged");
});

test("GitHub permission refusal is an explicit blocker", async () => {
  const f = fixture(); f.github.merge = async () => { throw new GitHubRequestError(403, "denied"); };
  assert.deepEqual(await f.deliver(), { state: "blocked", reason: "GitHub refused merge (403)" });
});

test("a recovery draft becomes ready only after recorded review and checks", async () => {
  const f = fixture();
  f.pr.draft = true;
  f.envelope.cycle.recovery = { source: "checkpoint", prior_run_id: "old-run", checkpoint_sha: f.result.sha };
  f.observation.merge_state = "blocked";
  f.observation.merge_reasons = ["draft"];
  const github = { ...f.github, markReady: async (node: string) => {
    assert.equal(node, "PR_7"); f.events.push("ready"); f.pr.draft = false;
    f.observation.merge_state = "eligible"; f.observation.merge_reasons = [];
  } };
  assert.equal((await deliverEngineReview(github, f.result, f.envelope, f.publish, f.checks)).state, "merged");
  assert.deepEqual(f.events, ["publish", "authorize", "ready", "authorize", "merge"]);
});

for (const condition of ["permission", "checks", "verdict", "incomplete_review"] as const) {
  test(`recovery draft remains draft when ${condition} is blocked`, async () => {
    const f = fixture(); f.pr.draft = true;
    f.envelope.cycle.recovery = { source: "checkpoint", prior_run_id: "old-run", checkpoint_sha: f.result.sha };
    f.observation.merge_reasons = condition === "permission" ? ["draft", "action_not_permitted"] : ["draft"];
    if (condition === "incomplete_review") f.result.report.stages.security = "failed";
    if (condition === "checks") f.observation.state = "failed";
    if (condition === "verdict") f.result.report.verdict = "request_changes";
    const github = { ...f.github, markReady: async () => { throw new Error("must not mark ready"); } };
    assert.equal((await deliverEngineReview(github, f.result, f.envelope, f.publish, f.checks)).state, "blocked");
    assert.ok(f.pr.draft);
  });
}
