import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { chmod, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import test from "node:test";
import { parseEnvelope, type CodexEngineReviewEnvelope } from "./contracts.js";
import { BATCH_BUDGET_SHARE, engineReport, engineReviewBody, milestonesPerPublish, parseEngineVerdict, parseRepairMilestone, runEngineReview, observePublishedRepair, type EngineVerdict } from "./engine-review.js";
import { readTaskBoardFile, writeTaskBoardFile, type Task } from "./task-board.js";
import { runStandaloneReview } from "./standalone-review.js";
import { deliverEngineReview } from "./engine-delivery.js";

const exec = promisify(execFile);
const approved: EngineVerdict = { verdict: "approve", summary: "Story and tests verified", findings: [],
  validationGaps: [], stages: { functional: "completed", security: "completed" }, stageDetails: "Tests passed" };
const blocked: EngineVerdict = { ...approved, verdict: "request_changes", findings: [{
  id: "F1", title: "Story behavior missing", details: "The implementation rejects valid input", file: "code.txt", line: 1,
  impact: "high", confidence: "high", blocking: true, fixClass: "author_required", recommendedFix: "Implement the story behavior",
}] };

/** Pin the legacy publish-every-milestone cadence for tests that count per-milestone commits. */
function perMilestone(t: test.TestContext) {
  const previous = process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH;
  process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH = "1";
  t.after(() => { if (previous === undefined) delete process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH; else process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH = previous; });
}

async function fixture(t: test.TestContext, trackedLearning = false) {
  const directory = await mkdtemp(join(tmpdir(), "codex-engine-test-"));
  t.after(() => rm(directory, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }));
  const workspace = join(directory, "work");
  const remote = join(directory, "remote.git");
  await mkdir(workspace);
  const git = async (...args: string[]) => (await exec("git", args, { cwd: workspace })).stdout.trim();
  await git("init", "--initial-branch=story");
  // Keep automatic Git maintenance inside the awaited Git process so it cannot
  // race fixture teardown while rewriting objects/pack.
  await git("config", "gc.autoDetach", "false");
  await git("config", "maintenance.autoDetach", "false");
  await git("config", "user.name", "Reviewer Test");
  await git("config", "user.email", "reviewer@example.test");
  await writeFile(join(workspace, "code.txt"), "old behavior\n");
  await git("add", "code.txt");
  if (trackedLearning) {
    await mkdir(join(workspace, "agent_learning"));
    await writeFile(join(workspace, "agent_learning", "old.md"), "previously tracked notes\n");
    await writeFile(join(workspace, ".gitignore"), "agent_learning/\n");
    await git("add", "-f", "--", "agent_learning/old.md", ".gitignore");
  }
  await git("commit", "-m", "story implementation");
  const sha = await git("rev-parse", "HEAD");
  await git("init", "--bare", remote);
  await git("--git-dir", remote, "config", "gc.autoDetach", "false");
  await git("--git-dir", remote, "config", "maintenance.autoDetach", "false");
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
    getBranch: async () => ({ commit: { sha: pr.base.sha } }),
    getPullRequest: async () => ({ ...pr, head: { ...pr.head, sha: await git("--git-dir", remote, "rev-parse", "story") } }) };
  return { directory, workspace, remote, git, sha, pr, envelope, runtime, github };
}

test("engine envelope selects its bound PR without a webhook payload or agent branch", () => {
  const raw = { version: "1.0", persona: "agent-codex-reviewer", message_id: "run", arrived_at: "now", tenant_id: "tenant",
    source_ref: { repo: "org/repo", issue: 42, installation_id: 1 }, intent: { trigger: "engine_review_cycle" },
    review_cycle_input: { action: "review", repo: "org/repo", pr_number: 7, head_sha: "a".repeat(40), findings: [],
      operation_key: "operation", accepted_scope: "revision identity", allow_story_repairs: true } };
  const parsed = parseEnvelope(JSON.stringify(raw));
  assert.equal(parsed.kind, "codex_engine_review");
  if (parsed.kind === "codex_engine_review") assert.equal(parsed.cycle.accepted_scope, "revision identity");
  assert.throws(() => parseEnvelope(JSON.stringify({ ...raw, intent: {} })), /invalid engine/);
  assert.throws(() => parseEnvelope(JSON.stringify({ ...raw, review_cycle_input: { ...raw.review_cycle_input, repo: "other/repo" } })), /invalid engine/);
});

test("engine review repairs semantic story issues, re-reviews and pushes the verified child", async t => {
  const state = await fixture(t);
  state.envelope.cycle.accepted_scope = "Code merge precedes separate qualification #99";
  const prompts: string[] = [];
  let reviews = 0;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async prompt => {
      assert.match(prompt, /Code merge precedes separate qualification #99/);
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
  assert.equal(reviews, 2);
  assert.ok(prompts.some(prompt => prompt.includes("no additional scope approval")));
  assert.ok(prompts.some(prompt => prompt.includes("full repaired working tree")));
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
  assert.equal(repairs, 1);
  assert.equal(reviews, 2);
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

for (const newWhitespace of [false, true]) {
  test(`base merge preserves upstream whitespace while ${newWhitespace ? "rejecting" : "publishing"} the repair delta`, async t => {
    const state = await fixture(t);
    await state.git("checkout", "-b", "main");
    await writeFile(join(state.workspace, "base-evidence.md"), "upstream evidence \n");
    await state.git("add", "base-evidence.md");
    await state.git("commit", "-m", "upstream fixture with existing whitespace");
    state.pr.base.sha = await state.git("rev-parse", "HEAD");
    await state.git("checkout", "story");
    state.pr.mergeable = false;
    state.envelope.cycle.action = "repair";
    const run = () => runEngineReview(state.envelope, state.runtime, {
      github: state.github,
      review: async () => approved,
      fix: async () => {
        await writeFile(join(state.workspace, "code.txt"), "repaired behavior\n");
        if (newWhitespace) await writeFile(join(state.workspace, "new-repair.txt"), "new whitespace \n");
      },
    });
    if (newWhitespace) {
      await assert.rejects(run(), /diff --cached --check/);
      assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), state.sha);
    } else {
      const result = await run();
      assert.equal(result.report.verdict, "approve");
      assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
      assert.equal(await state.git("rev-parse", "HEAD^2"), state.pr.base.sha);
      assert.equal(await readFile(join(state.workspace, "base-evidence.md"), "utf8"), "upstream evidence \n");
    }
  });
}

test("an inspected repair stages a tracked deletion under a now ignored directory", async t => {
  const state = await fixture(t, true);
  let reviews = 0;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => {
      assert.ok(++reviews <= 2, "an unchanged commit tree needs no repeated model inspection");
      return reviews === 1 ? blocked : approved;
    },
    fix: async () => {
      await state.git("rm", "--", "agent_learning/old.md");
      await mkdir(join(state.workspace, "agent_learning"), { recursive: true });
      await writeFile(join(state.workspace, "agent_learning", "new.md"), "ignored new notes\n");
      await writeFile(join(state.workspace, "code.txt"), "fixed\n");
    } });
  assert.equal(result.report.verdict, "approve");
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
  assert.equal(await state.git("show", "HEAD:code.txt"), "fixed");
  await assert.rejects(state.git("show", "HEAD:agent_learning/old.md"));
  await assert.rejects(state.git("show", "HEAD:agent_learning/new.md"));
  assert.equal(await state.git("diff", "HEAD"), "");
});

test("merged PR retained checkout can be reviewed without recreating a branch or merging", async t => {
  const state = await fixture(t);
  const result = await runEngineReview(state.envelope, state.runtime, { github: { ...state.github,
    getPullRequest: async () => ({ ...state.pr, state: "closed", merged: true }) },
    review: async () => approved, fix: async () => assert.fail("merged PR must not be repaired") });
  assert.equal(result.sha, state.sha);
  assert.equal(result.report.verdict, "approve");
});

test("a commit whose tree changes after inspection is never pushed", async t => {
  const state = await fixture(t);
  const realGit = (await exec("which", ["git"])).stdout.trim();
  const bin = join(state.directory, "bin");
  await mkdir(bin);
  const wrapper = join(bin, "git");
  const quotedGit = "'" + realGit.replaceAll("'", "'\\''") + "'";
  await writeFile(wrapper, `#!/bin/sh
${quotedGit} "$@" || exit $?
if [ "$1" = "-c" ] && [ "$3" = "commit" ]; then
  printf 'unreviewed change\\n' > code.txt
  ${quotedGit} add -- code.txt
  ${quotedGit} -c core.hooksPath=/dev/null commit --amend --no-edit >/dev/null
fi
`);
  await chmod(wrapper, 0o755);
  const before = process.env.PATH;
  process.env.PATH = `${bin}:${before}`;
  t.after(() => { process.env.PATH = before; });
  let reviews = 0;
  await assert.rejects(runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => ++reviews === 1 ? blocked : approved,
    fix: async () => { await writeFile(join(state.workspace, "code.txt"), "reviewed repair\n"); },
  }), /differs from the inspected tree/);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), state.sha);
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

test("an explicit repair consumes assigned findings before its only verification review", async t => {
  const state = await fixture(t);
  state.envelope.cycle.action = "repair";
  state.envelope.cycle.findings = [{ summary: "Fix valid input handling" }];
  const steps: string[] = [];
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github,
    fix: async prompt => {
      steps.push("repair");
      assert.match(prompt, /Fix valid input handling/);
      await writeFile(join(state.workspace, "code.txt"), "fixed behavior\n");
    },
    review: async () => { steps.push("review"); return approved; },
  });
  assert.deepEqual(steps, ["repair", "review"]);
  assert.notEqual(result.sha, state.sha);
});

for (const mergeable of [false, null, true]) test(`an assigned base repair integrates the base when GitHub mergeable is ${mergeable}`, async t => {
  const state = await fixture(t);
  await state.git("checkout", "-b", "main");
  await writeFile(join(state.workspace, "code.txt"), "base behavior\n");
  await state.git("commit", "-am", "main evolved");
  const base = await state.git("rev-parse", "HEAD");
  await state.git("push", state.remote, "main");
  await state.git("checkout", "story");
  await writeFile(join(state.workspace, "code.txt"), "story behavior\n");
  await state.git("commit", "-am", "story evolved");
  const expected = await state.git("rev-parse", "HEAD");
  await state.git("push", state.remote, "story");
  state.envelope.cycle.head_sha = expected;
  state.envelope.cycle.action = "repair";
  // GitHub's PR snapshot remains at the old base although main has advanced.
  state.envelope.cycle.findings = [{ source: "merge-controller", summary: "Resolve the current merge conflict or update the out-of-date base within accepted scope." }];
  const steps: string[] = [];
  const result = await runEngineReview(state.envelope, state.runtime, { github: { ...state.github,
    getBranch: async () => ({ commit: { sha: base } }),
    getPullRequest: async () => ({ ...await state.github.getPullRequest(), mergeable, mergeable_state: mergeable === false ? "dirty" : "unknown" }) },
    fix: async prompt => {
      steps.push("repair");
      assert.match(prompt, /resolve every conflict/);
      assert.match(await readFile(join(state.workspace, "code.txt"), "utf8"), /<<<<<<</);
      await writeFile(join(state.workspace, "code.txt"), "base and story behavior\n");
    },
    review: async () => {
      steps.push("review");
      assert.equal(await state.git("ls-files", "--unmerged"), "");
      assert.equal(await readFile(join(state.workspace, "code.txt"), "utf8"), "base and story behavior\n");
      return approved;
    },
  });
  assert.deepEqual(steps, ["repair", "review"]);
  assert.equal(await state.git("rev-parse", "HEAD^"), expected);
  assert.equal(await state.git("rev-parse", "HEAD^2"), base);
  assert.equal(await state.git("merge-base", "HEAD", base), base);
  assert.equal(result.repair_base_sha, expected);
  assert.equal(result.report.verdict, "approve");
});

function checkObservation(head: string, state: "passed" | "pending" | "failed" = "passed", base = head) {
  return { head_sha: head, base_sha: base, state, open: true, merged: false, base_repair_required: false,
    reasons: state === "passed" ? [] : [`required_check_${state}`], checks: [], failures: state === "failed" ? [{ name: "unit", details: "Expected 2, received 1" }] : [] };
}

for (const edit of [false, true]) test(`CI handoff waits and revalidates the published head (${edit ? "repair" : "validation only"})`, async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0, waits = 0, reviews = 0, observations = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async prompt => {
      reviews++;
      if (reviews <= 2) return { ...approved, validationGaps: ["Browser CI must validate the published repair"] };
      const published = await state.git("--git-dir", state.remote, "rev-parse", "story");
      assert.match(prompt, new RegExp(`exact commit ${published}`));
      assert.match(prompt, /controller-ci-evidence/);
      assert.match(prompt, /onboarding-browser/);
      assert.match(prompt, /success/);
      assert.equal(waits, 2);
      return approved;
    },
    fix: async () => {
      assert.equal(++repairs, 1, "CI waiting must not spend repair turns");
      if (edit) await writeFile(join(state.workspace, "code.txt"), "reviewed repair awaiting browser CI\n");
      return { outcome: "awaiting_ci", summary: "Publish and validate browser CI", remainingWork: ["Browser evidence"] };
    },
    checks: async head => {
      assert.equal(head, await state.git("--git-dir", state.remote, "rev-parse", "story"));
      observations++;
      return { ...checkObservation(head, observations === 2 ? "pending" : "passed", state.sha),
        checks: observations === 1 ? [] : [{ name: "onboarding-browser", status: observations < 3 ? "in_progress" : "completed",
          conclusion: observations < 3 ? null : "success" }] };
    },
    wait: async () => { waits++; assert.equal(reviews, 2, "polling must not invoke the model"); },
    deliver: async result => {
      assert.equal(reviews, 3, "CI evidence must be inspected before approval");
      assert.equal(result.report.verdict, "approve");
      assert.equal(result.sha, await state.git("--git-dir", state.remote, "rev-parse", "story"));
      return { state: "merged" };
    },
  });
  assert.equal(result.merged, true);
  assert.equal(result.sha === state.sha, !edit);
});

test("CI handoff repairs failures and waits for the replacement commit", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0, observations = 0, waits = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async prompt => prompt.includes("<controller-ci-evidence>") ? approved
      : { ...approved, validationGaps: ["Await final CI"] },
    fix: async prompt => {
      if (repairs) assert.match(prompt, /Expected 2, received 1/);
      await writeFile(join(state.workspace, "code.txt"), `repair ${++repairs}\n`);
      return { outcome: "awaiting_ci", summary: "Await CI", remainingWork: ["CI"] };
    },
    checks: async head => {
      observations++;
      return { ...checkObservation(head, observations === 1 || observations === 3 ? "pending"
        : observations === 2 ? "failed" : "passed", state.sha), checks: [{ name: "unit", conclusion: "success" }] };
    },
    wait: async () => { waits++; },
    deliver: async result => {
      assert.equal(repairs, 2);
      assert.equal(result.report.verdict, "approve");
      return { state: "merged" };
    },
  });
  assert.equal(result.merged, true);
  assert.equal(waits, 2);
  assert.equal(result.repair_base_sha, state.sha);
  assert.equal(await state.git("rev-parse", "HEAD~2"), state.sha);
});

test("green CI does not clear unrelated review findings", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0, reviews = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async () => { reviews++; return blocked; },
    fix: async () => ++repairs === 1
      ? { outcome: "awaiting_ci", summary: "Await CI", remainingWork: ["CI"] }
      : { outcome: "blocked", summary: "External contract decision missing", remainingWork: ["Owner decision"] },
    checks: async head => ({ ...checkObservation(head, "passed", state.sha), checks: [{ name: "unit", conclusion: "success" }] }),
    deliver: async () => assert.fail("green CI cannot waive a review finding"),
  });
  assert.equal(result.merged, false);
  assert.ok(reviews >= 3, "published evidence must be reviewed without automatically approving");
  assert.match("delivery_blocked" in result ? result.delivery_blocked : "", /External contract decision/);
});

test("CI handoff honors the delivery deadline without extra model turns", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let now = 0, reviews = 0, repairs = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github, now: () => now, deliveryTimeoutMs: 120000,
    review: async () => { reviews++; return { ...approved, validationGaps: ["Required CI"] }; },
    fix: async () => { repairs++; return { outcome: "awaiting_ci", summary: "Await CI", remainingWork: ["Required CI"] }; },
    checks: async head => checkObservation(head, "pending", state.sha),
    wait: async ms => { now += ms; },
    deliver: async () => assert.fail("pending CI must never merge"),
  });
  assert.equal(repairs, 1);
  assert.equal(reviews, 2);
  assert.equal(now, 120000);
  assert.match("delivery_blocked" in result ? result.delivery_blocked : "", /deadline exceeded/);
});

test("CI handoff refuses a changed PR head", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async () => ({ ...approved, validationGaps: ["Required CI"] }),
    fix: async () => ({ outcome: "awaiting_ci", summary: "Await CI", remainingWork: ["Required CI"] }),
    checks: async () => checkObservation("f".repeat(40), "passed", state.sha),
    deliver: async () => assert.fail("another head must never merge"),
  });
  assert.match("delivery_blocked" in result ? result.delivery_blocked : "", /PR head changed/);
});

test("PR mention carries named CI evidence through publication, waiting and merge", async t => {
  const state = await fixture(t);
  let reviews = 0, polls = 0, waits = 0;
  const comments: string[] = [];
  const result = await runStandaloneReview({ ...state.envelope, kind: "codex_pr_review",
    pull_request: { number: 7, issue_number: 42, head_ref: "story", base_ref: "main",
      expected_head_sha: state.sha, html_url: state.pr.html_url } }, state.runtime, {
    github: { ...state.github,
      checks: async () => {
        polls++;
        return { ready: polls > 1, total: 1, failing: [], pending: polls === 1 ? ["browser"] : [],
          observations: [{ name: "browser", status: polls === 1 ? "in_progress" : "completed",
            conclusion: polls === 1 ? null : "success", details_url: "https://github.com/org/repo/actions/runs/123" }] };
      },
      commentOnce: async (_number, _marker, body) => { comments.push(body); return true; },
      merge: async (_number, head) => {
        assert.equal(head, await state.git("--git-dir", state.remote, "rev-parse", "story"));
        assert.equal(reviews, 3);
        return "b".repeat(40);
      },
    },
    review: async prompt => {
      if (++reviews <= 2) return { ...approved, validationGaps: ["Browser evidence for published head"] };
      assert.match(prompt, /controller-ci-evidence/);
      assert.match(prompt, /"name":"browser"/);
      assert.match(prompt, /actions\/runs\/123/);
      assert.match(prompt, /"conclusion":"success"/);
      return approved;
    },
    fix: async () => {
      await writeFile(join(state.workspace, "code.txt"), "fixed behavior awaiting browser\n");
      return { outcome: "awaiting_ci", summary: "Need browser evidence", remainingWork: ["browser"] };
    },
    wait: async () => { waits++; },
  });
  assert.equal(result.status, "merged");
  assert.equal(waits, 1);
  assert.equal(comments.length, 1);
  assert.doesNotMatch(comments[0]!, /REQUEST CHANGES|unpublished/);
});

test("reviewer-owned project without CI finishes without waiting or extra review", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let reviews = 0;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github, deliver: async () => ({ state: "merged" }),
    review: async () => { reviews++; return approved; }, fix: async () => assert.fail("no repair"),
    checks: async head => checkObservation(head, "passed", state.sha), wait: async () => assert.fail("no CI must not wait") });
  assert.equal(result.report.verdict, "approve");
  assert.equal(reviews, 1);
});

test("reviewer remains alive through pending CI and fixes its failure using retained services", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0;
  let observations = 0;
  let waits = 0;
  let complete = false;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github, deliver: async () => ({ state: "merged" }),
    review: async () => approved,
    fix: async prompt => { assert.match(prompt, /Expected 2, received 1/); repairs++; await writeFile(join(state.workspace, "code.txt"), "fixed CI behavior\n"); },
    checks: async head => { assert.equal(complete, false); return checkObservation(head, ++observations === 1 ? "pending" : observations === 2 ? "failed" : "passed", state.sha); },
    wait: async () => { waits++; assert.equal(repairs, 0); },
  });
  complete = true;
  assert.equal(waits, 1);
  assert.equal(repairs, 1);
  assert.equal(observations, 3);
  assert.equal(result.report.verdict, "approve");
  assert.equal(result.repair_base_sha, state.sha);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
});

test("successive CI repairs retain original assignment lineage and one controller", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github, deliver: async () => ({ state: "merged" }),
    review: async () => approved,
    fix: async () => { await writeFile(join(state.workspace, "code.txt"), `repair ${++repairs}\n`); },
    checks: async head => checkObservation(head, repairs < 2 ? "failed" : "passed", state.sha),
  });
  assert.equal(repairs, 2);
  assert.equal(result.repair_base_sha, state.sha);
  assert.equal(await state.git("rev-parse", "HEAD~2"), state.sha);
  assert.equal(result.report.verdict, "approve");
});

for (const ci of ["passed", "pending"] as const) {
  test(`remaining findings go directly to repair with ${ci} CI`, async t => {
    const state = await fixture(t);
    state.envelope.cycle.reviewer_owned_delivery = true;
    const steps: string[] = [];
    let repairs = 0;
    const result = await runEngineReview(state.envelope, state.runtime, {
      github: state.github,
      review: async () => {
        steps.push("review");
        return repairs === 2 ? approved : blocked;
      },
      fix: async prompt => {
        steps.push("repair");
        assert.match(prompt, /Story behavior missing/);
        await writeFile(join(state.workspace, "code.txt"), `repair ${++repairs}\n`);
        return { outcome: "complete", summary: "Implemented repair", remainingWork: [] };
      },
      checks: async head => checkObservation(head, repairs === 2 ? "passed" : ci, state.sha),
      wait: async () => assert.fail("repair known findings before waiting for CI"),
      deliver: async result => {
        assert.equal(result.report.verdict, "approve");
        assert.equal(repairs, 2);
        return { state: "merged" };
      },
    });
    assert.deepEqual(steps, ["review", "repair", "review", "repair", "review"]);
    assert.equal(result.merged, true);
    assert.equal(result.repair_base_sha, state.sha);
  });
}

test("validation-only repair proceeds to merge without an empty commit", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async () => repairs === 2 ? approved : {
      ...approved, validationGaps: ["Run the focused compatibility test"],
    },
    fix: async () => { repairs++; },
    checks: async head => checkObservation(head, "passed", state.sha),
    deliver: async result => {
      assert.equal(result.report.verdict, "approve");
      return { state: "merged" };
    },
  });
  assert.equal(repairs, 2);
  assert.equal(result.sha, state.sha);
  assert.equal(result.repair_base_sha, null);
  assert.equal(result.merged, true);
});

test("unresolved review findings with no repair progress stop without merge", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async () => blocked,
    fix: async () => { repairs++; },
    checks: async head => checkObservation(head, "passed", state.sha),
    deliver: async () => assert.fail("unresolved findings must not merge"),
    wait: async () => assert.fail("no progress must not poll"),
  });
  assert.equal(repairs, 2);
  assert.equal(result.report.verdict, "request-changes");
  assert.equal(result.merged, false);
});

test("changing commits cannot bypass the automatic repair retry limit", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async () => approved,
    fix: async () => {
      await writeFile(join(state.workspace, "code.txt"), `attempt ${++repairs}\n`);
    },
    checks: async head => checkObservation(head, "failed", state.sha),
    deliver: async () => assert.fail("failing CI must not merge"),
  });
  assert.equal(repairs, 3);
  assert.equal(result.merged, false);
  assert.match("delivery_blocked" in result ? result.delivery_blocked : "", /retry limit reached \(3\)/);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
});

test("published implementation checkpoints do not exhaust repair retries", async t => {
  perMilestone(t);
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0, reviews = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async () => ++reviews === 1 ? blocked : approved,
    fix: async () => {
      repairs++;
      await writeFile(join(state.workspace, "code.txt"), `milestone ${repairs}\n`);
      return repairs < 6
        ? { outcome: "checkpoint", summary: "Implemented next milestone", remainingWork: ["Finish story"] }
        : { outcome: "complete", summary: "Story implemented", remainingWork: [] };
    },
    checks: async head => {
      assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), head);
      return checkObservation(head, repairs < 6 ? "pending" : "passed", state.sha);
    },
    wait: async () => assert.fail("continue unfinished implementation while CI is pending"),
    deliver: async result => {
      assert.equal(repairs, 6);
      assert.equal(result.report.verdict, "approve");
      assert.deepEqual(result.checkpoint_remaining, []);
      return { state: "merged" };
    },
  });
  assert.equal(repairs, 6);
  assert.equal(result.merged, true);
  assert.equal(await state.git("rev-parse", "HEAD~6"), state.sha);
});

test("no-progress repair never approves failed CI or loops model calls", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github, deliver: async () => ({ state: "merged" }),
    review: async () => approved, fix: async () => { repairs++; },
    checks: async head => checkObservation(head, "failed", state.sha),
  });
  assert.equal(repairs, 1);
  assert.equal(result.report.verdict, "request-changes");
  assert.match(result.body, /Expected 2, received 1/);
});

test("head movement and nonretryable policy refusal stop retained delivery", async t => {
  for (const refusal of [false, true]) {
    const state = await fixture(t);
    state.envelope.cycle.reviewer_owned_delivery = true;
    const result = await runEngineReview(state.envelope, state.runtime, { github: state.github, deliver: async () => ({ state: "merged" }),
      review: async () => approved, fix: async () => assert.fail("must not repair"),
      checks: async () => { if (refusal) throw Object.assign(new Error("policy expired"), { retryable: false }); return checkObservation("e".repeat(40)); },
      wait: async () => assert.fail("must not retry"),
    });
    assert.ok("delivery_blocked" in result);
  }
});

test("one reviewer fixes story and CI, publishes its evidence, merges, and reports confirmed delivery", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0, observations = 0, merges = 0, merged = false, published = "";
  const github = { ...state.github,
    getPullRequest: async () => ({ ...await state.github.getPullRequest(), merged }),
    merge: async (number: number, head: string) => {
      assert.equal(number, 7); assert.equal(head, published);
      assert.equal(head, await state.git("--git-dir", state.remote, "rev-parse", "story"));
      merged = true; merges++; return "c".repeat(40);
    },
    queueEntry: async () => null, enqueue: async () => assert.fail("not a queue repository"),
  };
  const checks = async (head: string, forMerge = false) => {
    if (!forMerge) observations++;
    return { ...checkObservation(head, forMerge || repairs === 2 ? "passed" : observations === 1 ? "pending" : "failed", state.sha),
      merge_state: "eligible" as const, merge_method: "squash" as const, merge_reasons: [], pr_node_id: "PR_7" };
  };
  const result = await runEngineReview(state.envelope, state.runtime, {
    github, checks, wait: async () => {}, review: async () => repairs ? approved : blocked,
    fix: async () => { await writeFile(join(state.workspace, "code.txt"), `fixed ${++repairs}\n`); },
    deliver: (result, envelope) => deliverEngineReview(github, result, envelope, async data => {
      assert.equal(data.report.verdict, "approve"); published = data.sha;
    }, checks),
  });
  assert.equal(result.merged, true);
  assert.equal(repairs, 2);
  assert.equal(merges, 1);
  assert.equal(result.repair_base_sha, state.sha);
});

test("stalled checkpoint review uses the latest story and preserves the saved commit", async t => {
  const f = await fixture(t);
  f.envelope.cycle.recovery = { source: "checkpoint", prior_run_id: "previous-worker", checkpoint_sha: f.sha };
  const prompts: string[] = [];
  const github = { ...f.github, getIssue: async () => ({ number: 42, title: "Updated story",
    body: "Owner clarification: preserve accepted inputs", html_url: "url", comments: 201 }),
    taskChecklists: async (number: number, total?: number) => {
      assert.equal(number, 42); assert.equal(total, 201);
      return ["☑ Implement history; ☐ Verify integration"];
    } };
  const result = await runEngineReview(f.envelope, f.runtime, { github,
    review: async prompt => { prompts.push(prompt); return approved; },
    fix: async () => { throw new Error("Already valid work does not need rewriting"); } });
  assert.equal(result.sha, f.sha);
  assert.ok(prompts[0]?.includes("stalled-story recovery"));
  assert.ok(prompts[0]?.includes("Owner clarification: preserve accepted inputs"));
  assert.ok(prompts[0]?.includes("do not restart from main"));
  assert.ok(prompts[0]?.includes("☑ Implement history; ☐ Verify integration"));
  assert.equal(await f.git("rev-parse", "HEAD"), f.sha);
});

test("Git validation diagnostics return to the repair thread before final inspection", async t => {
  const state = await fixture(t);
  let fixes = 0;
  let reviews = 0;
  const result = await runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => {
      reviews++;
      if (reviews === 1) return blocked;
      assert.equal(await readFile(join(state.workspace, "code.txt"), "utf8"), "fixed behavior\n");
      return approved;
    },
    fix: async prompt => {
      fixes++;
      if (fixes === 2) assert.match(prompt, /code.txt:1: trailing whitespace/);
      await writeFile(join(state.workspace, "code.txt"), fixes === 1 ? "fixed behavior  \n" : "fixed behavior\n");
    } });
  assert.equal(fixes, 2);
  assert.equal(reviews, 2);
  assert.equal(result.report.verdict, "approve");
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
});

test("persistent Git validation failure is bounded and never publishes", async t => {
  const state = await fixture(t);
  let fixes = 0;
  let reviews = 0;
  await assert.rejects(runEngineReview(state.envelope, state.runtime, { github: state.github,
    review: async () => { reviews++; return blocked; },
    fix: async () => { fixes++; await writeFile(join(state.workspace, "code.txt"), "invalid whitespace  \n"); },
  }), /code.txt:1: trailing whitespace/);
  assert.equal(fixes, 2);
  assert.equal(reviews, 1);
  assert.equal(await state.git("rev-parse", "HEAD"), state.sha);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), state.sha);
});

for (const deliveryTimeoutMs of [undefined, 90_000])
for (const queued of [false, true]) test(`delivery deadline (${deliveryTimeoutMs ?? "default"}) retains inspection when ${queued ? "queue" : "CI"} never finishes`, async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let clock = 0;
  let inspections = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github, review: async () => { inspections++; return approved; },
    fix: async () => { assert.fail("No repair while waiting"); },
    checks: async head => checkObservation(head, queued ? "passed" : "pending", state.sha),
    deliver: async () => ({ state: "pending", queued }),
    now: () => clock, deliveryTimeoutMs,
    wait: async ms => { clock += ms; },
  });
  assert.equal(clock, deliveryTimeoutMs ?? 21_600_000);
  assert.equal(inspections, 1);
  assert.equal(result.sha, state.sha);
  assert.equal(result.merged, false);
  assert.equal(result.report.verdict, "approve");
  assert.match("delivery_blocked" in result ? result.delivery_blocked : "", /deadline exceeded/);
});


test("repair time neither consumes nor resets the CI delivery allowance", async t => {
  perMilestone(t);
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let clock = 0, repairs = 0, reviews = 0, observations = 0;
  const waits: number[] = [];
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async () => ++reviews === 1 ? blocked : approved,
    fix: async () => {
      repairs++;
      clock += 180_000; // Each useful repair takes longer than the CI allowance.
      await writeFile(join(state.workspace, "code.txt"), `milestone ${repairs}\n`);
      return repairs === 1
        ? { outcome: "checkpoint", summary: "First milestone", remainingWork: ["Finish story"] }
        : { outcome: "complete", summary: "Repair complete", remainingWork: [] };
    },
    checks: async head => checkObservation(head, ++observations === 3 ? "failed" : "pending", state.sha),
    deliver: async () => assert.fail("Pending CI must not merge"),
    now: () => clock, deliveryTimeoutMs: 90_000,
    wait: async ms => { waits.push(ms); clock += ms; },
  });
  assert.equal(repairs, 3);
  assert.deepEqual(waits, [60_000, 30_000], "CI waiting before a repair remains charged afterward");
  assert.equal(clock, 3 * 180_000 + 90_000);
  assert.equal(result.merged, false);
  assert.match("delivery_blocked" in result ? result.delivery_blocked : "", /deadline exceeded/);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
});

for (const failure of ["semantic", "ci", "validation"] as const) {
  test(`PR mention owns ${failure} fixes, tests and merge without a handoff`, async t => {
    const state = await fixture(t);
    let repairs = 0, reviews = 0, merges = 0;
    const comments: string[] = [];
    const result = await runStandaloneReview({ ...state.envelope, kind: "codex_pr_review",
      pull_request: { number: 7, issue_number: 42, head_ref: "story", base_ref: "main",
        expected_head_sha: state.sha, html_url: state.pr.html_url } }, state.runtime, {
      github: { ...state.github,
        checks: async () => ({ ready: repairs > 0, total: failure === "semantic" ? 0 : 1,
          failing: failure === "ci" && repairs === 0 ? ["tests: valid input rejected"] : [], pending: [] }),
        commentOnce: async (_number, _marker, body) => { comments.push(body); return true; },
        merge: async (_number, head) => {
          assert.equal(head, await state.git("--git-dir", state.remote, "rev-parse", "story"));
          assert.equal(repairs, 1);
          assert.equal(reviews, 2);
          merges++;
          return "b".repeat(40);
        },
      },
      review: async () => {
        reviews++;
        if (repairs) return approved;
        if (failure === "semantic") return blocked;
        if (failure === "validation") return { ...approved, validationGaps: ["Run the focused input test"] };
        return approved;
      },
      fix: async prompt => {
        assert.match(prompt, /do not hand it to a developer or ask for another scope approval/);
        repairs++;
        if (failure !== "validation") await writeFile(join(state.workspace, "code.txt"), "valid input succeeds\n");
      },
    });
    assert.equal(result.status, "merged");
    assert.equal(merges, 1);
    assert.equal(comments.length, 1);
    assert.equal(await state.git("rev-parse", "HEAD") === state.sha, failure === "validation");
  });
}

test("PR mention reports a genuine no-progress blocker without merging", async t => {
  const state = await fixture(t);
  let repairs = 0;
  const comments: string[] = [];
  const result = await runStandaloneReview({ ...state.envelope, kind: "codex_pr_review",
    pull_request: { number: 7, issue_number: 42, head_ref: "story", base_ref: "main",
      expected_head_sha: state.sha, html_url: state.pr.html_url } }, state.runtime, {
    github: { ...state.github,
      checks: async () => ({ ready: true, total: 0, failing: [], pending: [] }),
      commentOnce: async (_number, _marker, body) => { comments.push(body); return true; },
      merge: async () => { assert.fail("Unresolved review must not merge"); },
    },
    review: async () => blocked,
    fix: async () => { repairs++; },
  });
  assert.equal(result.status, "changes_requested");
  assert.equal(comments.length, 1);
  assert.ok(repairs <= 2);
});


test("PR mention preserves an explicit review-only setting", async t => {
  const state = await fixture(t);
  const previous = process.env.CODEX_REVIEWER_MERGE_ENABLED;
  process.env.CODEX_REVIEWER_MERGE_ENABLED = "false";
  try {
    const result = await runStandaloneReview({ ...state.envelope, kind: "codex_pr_review",
      pull_request: { number: 7, issue_number: 42, head_ref: "story", base_ref: "main",
        expected_head_sha: state.sha, html_url: state.pr.html_url } }, state.runtime, {
      github: { ...state.github,
        checks: async () => ({ ready: false, total: 0, failing: [], pending: [] }),
        commentOnce: async () => true,
        merge: async () => { assert.fail("Merge was explicitly disabled"); },
      },
      review: async () => approved,
      fix: async () => { assert.fail("No repair required"); },
    });
    assert.equal(result.status, "approved");
  } finally {
    if (previous === undefined) delete process.env.CODEX_REVIEWER_MERGE_ENABLED;
    else process.env.CODEX_REVIEWER_MERGE_ENABLED = previous;
  }
});


test("repair milestone results are coerced to an honest outcome with warnings; only non-JSON is rejected", () => {
  assert.throws(() => parseRepairMilestone("not json"), /not JSON/);
  const empty = parseRepairMilestone("{}");
  assert.equal(empty.outcome, "checkpoint");
  assert.equal(empty.summary, "(no summary)");
  assert.deepEqual(empty.remainingWork, ["(remaining work not stated)"]);
  assert.ok(empty.warnings.length >= 2);
  assert.equal(parseRepairMilestone("null").outcome, "checkpoint");
  const noRemaining = parseRepairMilestone(JSON.stringify({ outcome: "checkpoint", summary: "Done", remainingWork: [] }));
  assert.equal(noRemaining.outcome, "checkpoint");
  assert.match(noRemaining.warnings.join("; "), /listed no remaining work/);
  const contradictory = parseRepairMilestone(JSON.stringify({ outcome: "complete", summary: "Done", remainingWork: ["Still missing"] }));
  assert.equal(contradictory.outcome, "checkpoint", "complete with remaining work is a checkpoint, not a failure");
  const blank = parseRepairMilestone(JSON.stringify({ outcome: "blocked", summary: " ", remainingWork: [] }));
  assert.equal(blank.outcome, "blocked");
  assert.equal(blank.summary, "(no summary)");
  const clean = parseRepairMilestone(JSON.stringify({ outcome: "checkpoint", summary: "Parser repaired", remainingWork: ["Add coverage"] }));
  assert.equal(clean.outcome, "checkpoint");
  assert.deepEqual(clean.warnings, []);
  const waiting = parseRepairMilestone(JSON.stringify({ outcome: "awaiting_ci", summary: "Browser CI required",
    remainingWork: ["Browser evidence"], tasks: [] }));
  assert.equal(waiting.outcome, "awaiting_ci");
  assert.deepEqual(waiting.remainingWork, ["Browser evidence"]);
  assert.deepEqual(waiting.warnings, []);
});

for (const ownedDelivery of [false, true]) {
test(`repair publishes two milestones with ${ownedDelivery ? "reviewer" : "engine"} delivery ownership`, async t => {
  perMilestone(t);
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = ownedDelivery;
  let repairs = 0, reviews = 0, waits = 0, observations = 0, deliveries = 0;
  let first = "";
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async prompt => {
      reviews++;
      if (reviews === 1) return blocked;
      if (reviews === 2) assert.match(prompt, /incomplete checkpoint/);
      return approved; // A partial inspection alone must never approve completion.
    },
    fix: async prompt => {
      repairs++;
      assert.match(prompt, /Plan the required repairs as coherent milestones/);
      if (repairs === 2) {
        first = await state.git("--git-dir", state.remote, "rev-parse", "story");
        assert.notEqual(first, state.sha, "first checkpoint must already be published");
        assert.equal(waits, 0, "unfinished work must continue without waiting for checkpoint CI");
        assert.match(prompt, /Remaining repair milestone: Add input coverage/);
      }
      await writeFile(join(state.workspace, "code.txt"), `milestone ${repairs}\n`);
      return repairs === 1
        ? { outcome: "checkpoint", summary: "Repair parser", remainingWork: ["Add input coverage"] }
        : { outcome: "complete", summary: "Coverage complete", remainingWork: [] };
    },
    checks: async head => {
      assert.equal(ownedDelivery, true, "engine-owned delivery must not invoke reviewer delivery APIs");
      return checkObservation(head, ++observations < 3 ? "pending" : "passed", state.sha);
    },
    wait: async () => { waits++; assert.equal(repairs, 2); },
    deliver: async result => {
      deliveries++;
      assert.equal(observations, 3);
      assert.equal(repairs, 2);
      assert.equal(result.report.verdict, "approve");
      assert.deepEqual(result.checkpoint_remaining, []);
      return { state: "merged" };
    },
  });
  assert.equal(await state.git("rev-parse", "HEAD^"), first);
  assert.equal(await state.git("rev-parse", "HEAD~2"), state.sha);
  assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), result.sha);
  assert.equal(reviews, 3);
  assert.equal(waits, ownedDelivery ? 1 : 0);
  assert.equal(deliveries, ownedDelivery ? 1 : 0);
  assert.equal(result.merged, ownedDelivery);
  assert.equal(result.repair_base_sha, state.sha);
});
}

for (const outcome of ["checkpoint", "blocked"] as const) {
  test(`${outcome} repair cannot approve despite an approving inspection`, async t => {
    const state = await fixture(t);
    let reviews = 0;
    state.envelope.cycle.reviewer_owned_delivery = outcome === "blocked";
    const result = await runEngineReview(state.envelope, state.runtime, {
      github: state.github,
      checks: async () => assert.fail("external blocker must not poll CI or restart repair"),
      deliver: async () => assert.fail("incomplete work must not merge"),
      review: async () => ++reviews === 1 ? blocked : approved,
      fix: async () => {
        await writeFile(join(state.workspace, "code.txt"), "partial repair\n");
        return { outcome, summary: "Needs remaining work", remainingWork: ["Missing acceptance evidence"] };
      },
    });
    assert.notEqual(result.sha, state.sha);
    assert.equal(result.report.verdict, "request-changes");
    assert.match(result.body, /Missing acceptance evidence/);
  });
}

/** Commit a developer board file on the story branch and point the PR at it. */
async function seedBoardFile(state: Awaited<ReturnType<typeof fixture>>, tasks: Task[]): Promise<string> {
  await writeTaskBoardFile(state.workspace, 42, tasks);
  await state.git("add", ".adp/tasks/42.json");
  await state.git("commit", "-m", "chore(#42): task board after turn 2");
  const head = await state.git("rev-parse", "HEAD");
  await state.git("push", state.remote, "HEAD:refs/heads/story");
  state.envelope.cycle.head_sha = head;
  state.pr.head.sha = head;
  return head;
}

const boardTask = (id: string, kind: Task["kind"], status: Task["status"], covers: string[] = []): Task =>
  ({ id, kind, status, covers, criterion: id.split("-")[0]!, title: `${kind} ${id}`, files: [], note: "" });

for (const failedSink of ['none', 'file', 'pr', 'progress', 'closure'] as const) {
test(`merged delivery survives ${failedSink} reporting failure and preserves deferred work`, async t => {
  const state = await fixture(t);
  const tasks = [boardTask('AC1-c1', 'code', 'done'), boardTask('AC1-t1', 'test', 'open', ['AC1-c1']),
    { ...boardTask('AC1-live', 'test', 'blocked', ['AC1-c1']), note: 'Separate live qualification' }];
  const head = await seedBoardFile(state, tasks);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let merged = false;
  let boardText = '', report: import('./closure-report.js').ClosureReport | undefined;
  const result = await runEngineReview(state.envelope, { ...state.runtime, observer: {
    explanation() {}, activity() {}, session() {}, async finish() {}, async fail() {},
    progress(text) { assert.equal(merged, true); if (failedSink === 'progress') throw new Error('reporter down'); boardText = text; },
    closure(value) { assert.equal(merged, true); if (failedSink === 'closure') throw new Error('archive down'); report = value; },
  } }, {
    github: { ...state.github, updatePullRequestBody: async (_number, body) => {
      assert.equal(merged, true);
      if (failedSink === 'pr') throw new Error('GitHub reporting unavailable');
      assert.match(body, /PR merged/);
    } },
    review: async () => ({ ...approved, closureReport: { completed: ['The UI passed browser validation.'], remaining: ['Live demo remains.'],
      verifiedTasks: [{ id: 'AC1-t1', evidence: 'Browser check passed' }] } }),
    fix: async () => assert.fail('reporting must not require another repair'),
    checks: async sha => checkObservation(sha, 'passed', state.sha),
    deliver: async () => {
      merged = true;
      if (failedSink === 'file') {
        await rm(join(state.workspace, '.adp/tasks'), { recursive: true });
        await writeFile(join(state.workspace, '.adp/tasks'), 'not a directory');
      }
      return { state: 'merged' };
    },
  });
  assert.equal(result.merged, true);
  assert.equal(await state.git('rev-parse', 'HEAD'), head, 'no reporting commit or CI restart');
  const saved = await readTaskBoardFile(state.workspace, 42);
  if (failedSink !== 'file') assert.deepEqual(saved.tasks?.map(task => task.status), ['done', 'done', 'blocked']);
  if (failedSink !== 'progress') assert.match(boardText, /test 1\/2/);
  if (failedSink !== 'closure') {
    assert.equal(report?.delivery, 'Pull request merged.');
    assert.match(report!.remaining.join(' '), /Separate live qualification/);
    if (failedSink !== 'none') assert.match(report!.reporting_notes.join(' '), /could not be updated/);
  }
});
}

test("milestones per publish defaults to three and ignores invalid overrides", t => {
  const previous = process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH;
  t.after(() => { if (previous === undefined) delete process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH; else process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH = previous; });
  delete process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH;
  assert.equal(milestonesPerPublish(), 3);
  process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH = "0";
  assert.equal(milestonesPerPublish(), 3);
  process.env.CODEX_REVIEWER_MILESTONES_PER_PUBLISH = "5";
  assert.equal(milestonesPerPublish(), 5);
});

test("repair milestones carry a validated task board and reject a dishonest one", () => {
  const tasks = [boardTask("AC1-c1", "code", "done"), boardTask("AC1-t1", "test", "open", ["AC1-c1"])];
  const parsed = parseRepairMilestone(JSON.stringify({ outcome: "checkpoint", summary: "Parser repaired", remainingWork: ["AC1-t1"], tasks }));
  assert.deepEqual(parsed.tasks, tasks);
  assert.deepEqual(parseRepairMilestone(JSON.stringify({ outcome: "complete", summary: "Done", remainingWork: [], tasks: [] })).tasks, []);
  const loose = parseRepairMilestone(JSON.stringify({ outcome: "checkpoint", summary: "x", remainingWork: ["y"],
    tasks: [boardTask("AC1-t1", "test", "open")] }));
  assert.equal(loose.tasks?.length, 1, "a flawed board is kept and the flaw is reported, not fatal");
  assert.match(loose.warnings.join("; "), /does not name the code task it proves/);
});

test("several repair tasks are finished before one inspection and one publication, named in the commit and PR board", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  const developerBoard = [boardTask("AC1-c1", "code", "done"), boardTask("AC1-t1", "test", "done", ["AC1-c1"]),
    boardTask("AC2-c1", "code", "open"), boardTask("AC2-t1", "test", "open", ["AC2-c1"])];
  state.pr.body = "Developer prose";
  const start = await seedBoardFile(state, developerBoard);
  let repairs = 0, reviews = 0;
  const bodies: string[] = [];
  const prompts: string[] = [];
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: { ...state.github, updatePullRequestBody: async (_number, body) => { bodies.push(body); } },
    review: async prompt => {
      reviews++;
      prompts.push(prompt);
      return reviews === 1 ? blocked : approved;
    },
    fix: async prompt => {
      repairs++;
      if (repairs === 1) {
        assert.match(prompt, /Current task board/);
        assert.match(prompt, /AC2-c1/, "the developer's board is the repair plan");
      } else {
        assert.match(prompt, /accepted locally and not yet published/);
        assert.doesNotMatch(prompt, /Plan the required repairs/);
      }
      assert.equal(await state.git("--git-dir", state.remote, "rev-parse", "story"), start, "nothing is pushed between batched tasks");
      await writeFile(join(state.workspace, "code.txt"), `task ${repairs}\n`);
      const board = developerBoard.map(task => ({ ...task, status: (task.id === "AC2-c1" || (task.id === "AC2-t1" && repairs >= 2)) ? "done" as const : task.status }));
      return repairs < 2
        ? { outcome: "checkpoint", summary: "Behavior added", remainingWork: ["AC2-t1: prove it"], tasks: board }
        : { outcome: "complete", summary: "Covered", remainingWork: [], tasks: board };
    },
    checks: async head => checkObservation(head, "passed", state.pr.base.sha),
    deliver: async result => { assert.equal(result.report.verdict, "approve"); return { state: "merged" }; },
    wait: async () => assert.fail("no CI wait expected"),
  });
  assert.equal(repairs, 2);
  assert.equal(reviews, 2, "one inspection before repair, one after the whole batch");
  assert.equal(await state.git("rev-parse", "HEAD^"), start, "the batch is a single published commit");
  assert.match(await state.git("log", "-1", "--format=%s"), /^fix\(review #42\): AC2-c1, AC2-t1$/);
  assert.match(prompts[1]!, /Files changed by this repair batch: code.txt/);
  assert.equal(bodies.length, 2, 'one batch update and one post-merge checklist update');
  assert.match(bodies[0]!, /^Developer prose/);
  assert.doesNotMatch(bodies[0]!, /adp-task-board-data/);
  const published = (await state.git("rev-parse", "HEAD")).slice(0, 7);
  assert.match(bodies[0]!, new RegExp(`AC2-c1 — code AC2-c1 · \`${published}\``), "the PR body names the commit that finished each task");
  assert.match(bodies[0]!, new RegExp(`AC2-t1 — test AC2-t1 \\(covers AC2-c1\\) · \`${published}\``));
  // The branch board rides in the batch commit; it cannot name that commit itself.
  const committed = JSON.parse(await state.git("show", "HEAD:.adp/tasks/42.json"));
  assert.deepEqual(committed.tasks.map((task: Task) => task.status), ["done", "done", "done", "done"]);
  assert.equal(committed.issue, 42);
  assert.equal(await state.git("show", `HEAD:.adp/tasks/42.json`) !== "", true);
  assert.match(bodies[0]!, /Reviewer: all tasks done/);
  assert.equal(result.merged, true);
});

test("a batch ends at the configured task count or when the model allowance is mostly spent", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  let repairs = 0, reviews = 0, pushes = 0, lastRemote = state.sha;
  let used = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    budgetUsedShare: () => used,
    review: async () => ++reviews === 1 ? blocked : approved,
    fix: async () => {
      repairs++;
      const remote = await state.git("--git-dir", state.remote, "rev-parse", "story");
      if (remote !== lastRemote) { pushes++; lastRemote = remote; }
      await writeFile(join(state.workspace, "code.txt"), `task ${repairs}\n`);
      // Tasks 1-3 fill the default batch; task 4 then trips the budget guard.
      if (repairs === 4) used = BATCH_BUDGET_SHARE;
      return repairs < 5
        ? { outcome: "checkpoint", summary: `task ${repairs}`, remainingWork: ["more"] }
        : { outcome: "complete", summary: "done", remainingWork: [] };
    },
    checks: async head => checkObservation(head, "passed", state.sha),
    deliver: async () => ({ state: "merged" }),
    wait: async () => assert.fail("unfinished work continues without CI waits"),
  });
  assert.equal(repairs, 5);
  // Publications: after task 3 (count), after task 4 (budget), after task 5 (complete).
  assert.equal(pushes + 1, 3);
  assert.equal(await state.git("rev-parse", "HEAD~3"), state.sha);
  assert.equal(result.merged, true);
});

test("the reviewer resumes from the branch board file when the developer left one, before any PR-body fallback", async t => {
  const state = await fixture(t);
  state.envelope.cycle.reviewer_owned_delivery = true;
  const developerBoard = [boardTask("AC1-c1", "code", "done"), boardTask("AC1-t1", "test", "done", ["AC1-c1"]), boardTask("AC2-c1", "code", "open")];
  // The developer committed the board with its work; the PR body has no data section.
  await seedBoardFile(state, developerBoard);
  let reviews = 0, repairs = 0;
  const result = await runEngineReview(state.envelope, state.runtime, {
    github: state.github,
    review: async () => ++reviews === 1 ? blocked : approved,
    fix: async prompt => {
      repairs++;
      assert.match(prompt, /Current task board/);
      assert.match(prompt, /☐ `code` AC2-c1/, "the board came from the branch file, not the PR body");
      await writeFile(join(state.workspace, "code.txt"), "AC2 behavior\n");
      return { outcome: "complete", summary: "AC2 done", remainingWork: [],
        tasks: developerBoard.map(task => ({ ...task, status: "done" as const })) };
    },
    checks: async head => checkObservation(head, "passed", state.pr.base.sha),
    deliver: async () => ({ state: "merged" }),
    wait: async () => assert.fail("no CI wait expected"),
  });
  assert.equal(repairs, 1);
  assert.equal(result.merged, true);
  const after = await readTaskBoardFile(state.workspace, 42);
  assert.deepEqual(after.tasks?.map(task => task.status), ["done", "done", "done"]);
  assert.match(await state.git("log", "-1", "--format=%s"), /^fix\(review #42\): AC2-c1$/);
});
