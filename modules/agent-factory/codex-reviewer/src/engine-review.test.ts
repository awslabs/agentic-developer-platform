import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import test from "node:test";
import { parseEnvelope, type CodexEngineReviewEnvelope } from "./contracts.js";
import { engineReport, engineReviewBody, parseEngineVerdict, runEngineReview, observePublishedRepair, type EngineVerdict } from "./engine-review.js";

const exec = promisify(execFile);
const approved: EngineVerdict = { verdict: "approve", summary: "Story and tests verified", findings: [],
  validationGaps: [], stages: { functional: "completed", security: "completed" }, stageDetails: "Tests passed" };
const blocked: EngineVerdict = { ...approved, verdict: "request_changes", findings: [{
  id: "F1", title: "Story behavior missing", details: "The implementation rejects valid input", file: "code.txt", line: 1,
  impact: "high", confidence: "high", blocking: true, fixClass: "author_required", recommendedFix: "Implement the story behavior",
}] };

async function fixture(t: test.TestContext) {
  const directory = await mkdtemp(join(tmpdir(), "codex-engine-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const workspace = join(directory, "work");
  const remote = join(directory, "remote.git");
  await mkdir(workspace);
  const git = async (...args: string[]) => (await exec("git", args, { cwd: workspace })).stdout.trim();
  await git("init", "--initial-branch=story");
  await git("config", "user.name", "Reviewer Test");
  await git("config", "user.email", "reviewer@example.test");
  await writeFile(join(workspace, "code.txt"), "old behavior\n");
  await git("add", "code.txt");
  await git("commit", "-m", "story implementation");
  const sha = await git("rev-parse", "HEAD");
  await git("init", "--bare", remote);
  await git("push", remote, "HEAD:refs/heads/story");
  await git("config", `url.${remote}.insteadOf`, "https://x-access-token@github.com/org/repo.git");
  const pr = { number: 7, state: "open", title: "Story", body: "Implement the story", html_url: "https://github.com/org/repo/pull/7",
    draft: false, mergeable: true, mergeable_state: "clean", head: { ref: "story", sha, repo: { full_name: "org/repo" } },
    base: { ref: "main", sha } };
  const envelope: CodexEngineReviewEnvelope = { kind: "codex_engine_review", version: "1.0", message_id: "run", arrived_at: "now",
    tenant_id: "tenant", installation_id: 1, repository: "org/repo", issue_number: 42,
    cycle: { action: "review", repo: "org/repo", pr_number: 7, head_sha: sha, findings: [], allow_story_repairs: true } };
  const runtime = { workspace, githubToken: "test-token", proxyBaseUrl: "http://localhost/openai/v1" };
  const github = { getIssue: async () => ({ number: 42, title: "Story", body: "Valid input succeeds", html_url: "https://github.com/org/repo/issues/42" }),
    getPullRequest: async () => ({ ...pr, head: { ...pr.head, sha: await git("--git-dir", remote, "rev-parse", "story") } }) };
  return { directory, workspace, remote, git, sha, pr, envelope, runtime, github };
}

test("engine envelope selects its bound PR without a webhook payload or agent branch", () => {
  const raw = { version: "1.0", persona: "agent-codex-reviewer", message_id: "run", arrived_at: "now", tenant_id: "tenant",
    source_ref: { repo: "org/repo", issue: 42, installation_id: 1 }, intent: { trigger: "engine_review_cycle" },
    review_cycle_input: { action: "review", repo: "org/repo", pr_number: 7, head_sha: "a".repeat(40), findings: [],
      operation_key: "operation", accepted_scope: "revision identity", allow_story_repairs: true } };
  assert.equal(parseEnvelope(JSON.stringify(raw)).kind, "codex_engine_review");
  assert.throws(() => parseEnvelope(JSON.stringify({ ...raw, intent: {} })), /invalid engine/);
  assert.throws(() => parseEnvelope(JSON.stringify({ ...raw, review_cycle_input: { ...raw.review_cycle_input, repo: "other/repo" } })), /invalid engine/);
});

test("engine review repairs semantic story issues, re-reviews and pushes the verified child", async t => {
  const state = await fixture(t);
  const prompts: string[] = [];
  let reviews = 0;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async prompt => {
      prompts.push(prompt);
      reviews++;
      if (reviews === 1) {
        await writeFile(join(state.workspace, "review-note.md"), "local review report");
        return blocked;
      }
      return approved;
    },
    fix: async prompt => {
      prompts.push(prompt);
      await writeFile(join(state.workspace, "code.txt"), "story behavior fixed\n");
      await mkdir(join(state.workspace, "infra"));
      await writeFile(join(state.workspace, "infra", "story-test.txt"), "new coverage\n");
    } });
  assert.notEqual(result.sha, state.sha);
  assert.equal(result.repair_base_sha, state.sha);
  assert.equal(await state.git("rev-parse", "HEAD^"), state.sha);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
  assert.equal(result.report.verdict, "approve");
  assert.equal(reviews, 3);
  assert.ok(prompts.some(prompt => prompt.includes("no additional scope approval")));
  assert.ok(prompts.some(prompt => prompt.includes(`exact commit ${result.sha}`)));
  assert.match(await state.git("show", "HEAD:infra/story-test.txt"), /coverage/);
  await assert.rejects(state.git("show", "HEAD:review-note.md"));
});

test("reviewed repairs publish progress while remaining findings block approval", async t => {
  const state = await fixture(t);
  let repairs = 0;
  let reviews = 0;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => { reviews++; return { ...approved, validationGaps: ["Remote image scan must run on the published PR commit"] }; },
    fix: async () => { repairs++; await writeFile(join(state.workspace, "code.txt"), "fixed behavior awaiting CI\n"); } });
  assert.equal(repairs, 2);
  assert.equal(reviews, 4);
  assert.notEqual(result.sha, state.sha);
  assert.equal(result.repair_base_sha, state.sha);
  assert.equal(result.report.verdict, "request-changes");
  assert.equal(result.report.findings[0]?.severity, "blocking");
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
  assert.ok(result.body.includes(result.sha));
});

test("failed functional or security inspection never publishes a repaired tree", async t => {
  const state = await fixture(t);
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => ({ ...blocked, stages: { functional: "failed", security: "completed" } }),
    fix: async () => { await writeFile(join(state.workspace, "code.txt"), "uninspected\n"); } });
  assert.equal(result.sha, state.sha);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), state.sha);
});

test("failed final commit inspection stops publication even after a completed working-tree review", async t => {
  const state = await fixture(t);
  let reviews = 0;
  await assert.rejects(runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => ++reviews === 1 ? blocked : reviews === 2 ? approved
      : { ...approved, stages: { functional: "completed", security: "failed" } },
    fix: async () => { await writeFile(join(state.workspace, "code.txt"), "fixed\n"); } }), /inspection did not complete/);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), state.sha);
});

test("merged PR retained checkout can be reviewed without recreating a branch or merging", async t => {
  const state = await fixture(t);
  const result = await runEngineReview(state.envelope, state.runtime, { github: { ...state.github,
    getPullRequest: async () => ({ ...state.pr, state: "closed", merged: true }) },
    review: async () => approved, fix: async () => assert.fail("merged PR must not be repaired") });
  assert.equal(result.sha, state.sha);
  assert.equal(result.report.verdict, "approve");
});

test("concurrent head movement stops a repair before publication", async t => {
  const state = await fixture(t);
  let reads = 0;
  let reviews = 0;
  await assert.rejects(runEngineReview(state.envelope, state.runtime, { github: { ...state.github,
    getPullRequest: async () => ({ ...state.pr, head: { ...state.pr.head, sha: ++reads === 1 ? state.sha : "b".repeat(40) } }) },
    review: async () => ++reviews === 1 ? blocked : approved,
    fix: async () => { await writeFile(join(state.workspace, "code.txt"), "fixed\n"); } }), /PR changed during/);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), state.sha);
});

test("model Git-state changes fail closed", async t => {
  const state = await fixture(t);
  await assert.rejects(runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => blocked, fix: async () => { await state.git("config", "core.hooksPath", "/tmp/model-hooks"); } }), /protected Git/);
});

test("review-only authorization never grants story repairs", async t => {
  const state = await fixture(t);
  state.envelope.cycle.allow_story_repairs = false;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => blocked, fix: async () => assert.fail("repair not assigned") });
  assert.equal(result.report.verdict, "request-changes");
});

test("missing stages and validation gaps cannot become approval", () => {
  assert.throws(() => parseEngineVerdict(JSON.stringify({ ...approved, stages: undefined })), /completion/);
  const report = engineReport({ ...approved, validationGaps: ["required behavior test failed"] });
  assert.equal(report.verdict, "request-changes");
  assert.equal(report.findings[0]?.severity, "blocking");
  assert.match(engineReviewBody({ ...approved, validationGaps: ["test failed"] }, "a".repeat(40)), /— REQUEST CHANGES/);
  assert.match(engineReviewBody({ ...approved, stages: { functional: "failed", security: "completed" } }, "a".repeat(40)), /— INCOMPLETE/);
});


test("published repair waits for the exact PR head without rerunning review", async t => {
  const state = await fixture(t);
  const head = "b".repeat(40);
  let reads = 0;
  const waits: number[] = [];
  await observePublishedRepair({ ...state.github,
    getPullRequest: async () => ({ ...state.pr, head: { ...state.pr.head, sha: ++reads < 3 ? state.sha : head } }),
  }, 7, state.sha, head, "story", async ms => { waits.push(ms); });
  assert.equal(reads, 3);
  assert.deepEqual(waits, [1000, 2000]);
});

test("publication observation rejects a concurrent revision immediately", async t => {
  const state = await fixture(t);
  let reads = 0;
  await assert.rejects(observePublishedRepair({ ...state.github,
    getPullRequest: async () => { reads++; return { ...state.pr, head: { ...state.pr.head, sha: "c".repeat(40) } }; },
  }, 7, state.sha, "b".repeat(40), "story", async () => assert.fail("must not retry a different revision")), /head changed/);
  assert.equal(reads, 1);
});

test("an indefinitely stale PR projection cannot deliver a review", async t => {
  const state = await fixture(t);
  let reads = 0;
  await assert.rejects(observePublishedRepair({ ...state.github,
    getPullRequest: async () => { reads++; return state.pr; },
  }, 7, state.sha, "b".repeat(40), "story", async () => {}), /not yet visible/);
  assert.equal(reads, 6);
});
