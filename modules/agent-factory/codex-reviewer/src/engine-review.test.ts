import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { chmod, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import test from "node:test";
import { parseEnvelope, type CodexEngineReviewEnvelope } from "./contracts.js";
import { engineReport, engineReviewBody, parseEngineVerdict, runEngineReview, observePublishedRepair, type EngineVerdict } from "./engine-review.js";
import { deliverEngineReview } from "./engine-delivery.js";

const exec = promisify(execFile);
const approved: EngineVerdict = { verdict: "approve", summary: "Story and tests verified", findings: [],
  validationGaps: [], stages: { functional: "completed", security: "completed" }, stageDetails: "Tests passed" };
const blocked: EngineVerdict = { ...approved, verdict: "request_changes", findings: [{
  id: "F1", title: "Story behavior missing", details: "The implementation rejects valid input", file: "code.txt", line: 1,
  impact: "high", confidence: "high", blocking: true, fixClass: "author_required", recommendedFix: "Implement the story behavior",
}] };

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
