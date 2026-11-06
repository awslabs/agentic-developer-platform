import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { run } from "./process.js";
import { prepareStoryBranch } from "./branch-reconciliation.js";

for (const branch of ["agent/issue-42", "agent/codex-issue-42"]) {
  test(`standalone restart preserves ${branch} and brings in new base`, async t => {
    const root = await mkdtemp(join(tmpdir(), "adp-branch-reconcile-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const remote = join(root, "remote"), local = join(root, "local");
    await mkdir(remote);
    const git = async (cwd: string, ...args: string[]) => (await run("git", args, { cwd })).stdout.trim();
    const identity = async (cwd: string) => {
      await git(cwd, "config", "user.name", "Test");
      await git(cwd, "config", "user.email", "test@example.invalid");
    };
    await git(remote, "init", "-b", "main");
    await identity(remote);
    await writeFile(join(remote, "base.txt"), "original\n");
    await git(remote, "add", "."); await git(remote, "commit", "-m", "base");
    await git(remote, "switch", "-c", branch);
    await writeFile(join(remote, "story.txt"), "keep story\n");
    await mkdir(join(remote, ".adp/tasks"), { recursive: true });
    await writeFile(join(remote, ".adp/tasks/42.json"), '{"tasks":[]}');
    await git(remote, "add", "."); await git(remote, "commit", "-m", "story");
    const original = await git(remote, "rev-parse", "HEAD");
    await git(remote, "switch", "main");
    await writeFile(join(remote, "deploy-infra.py"), "# current deployment\n");
    await git(remote, "add", "."); await git(remote, "commit", "-m", "base advances");
    await git(root, "clone", remote, local); await identity(local);
    const result = await prepareStoryBranch(local, 42);
    assert.equal(result.branch, branch); assert.equal(result.status, "merged");
    assert.equal(await readFile(join(local, "story.txt"), "utf8"), "keep story\n");
    assert.equal(await readFile(join(local, "deploy-infra.py"), "utf8"), "# current deployment\n");
    assert.equal(await readFile(join(local, ".adp/tasks/42.json"), "utf8"), '{"tasks":[]}');
    await git(local, "merge-base", "--is-ancestor", original, "HEAD");
    await git(local, "merge-base", "--is-ancestor", "origin/main", "HEAD");
    assert.equal(await git(remote, "rev-parse", branch), original);
    const merged = await git(local, "rev-parse", "HEAD");
    assert.equal((await prepareStoryBranch(local, 42)).status, "up_to_date");
    assert.equal(await git(local, "rev-parse", "HEAD"), merged);

    // A later base edit conflicts with saved local work. Both sides must remain
    // available to the model; neither a reset nor an automatic winner is allowed.
    await writeFile(join(local, "base.txt"), "story edit\n");
    await git(local, "add", "."); await git(local, "commit", "-m", "story edit");
    const beforeConflict = await git(local, "rev-parse", "HEAD");
    await writeFile(join(remote, "base.txt"), "base edit\n");
    await git(remote, "add", "."); await git(remote, "commit", "-m", "base edit");
    const conflict = await prepareStoryBranch(local, 42);
    assert.equal(conflict.status, "conflict"); assert.deepEqual(conflict.conflicts, ["base.txt"]);
    assert.equal(await git(local, "rev-parse", "HEAD"), beforeConflict);
    assert.equal(await git(local, "show", ":2:base.txt"), "story edit");
    assert.equal(await git(local, "show", ":3:base.txt"), "base edit");
  });
}

test("standalone first start creates the canonical branch and refuses ambiguous legacy work", async t => {
  const root = await mkdtemp(join(tmpdir(), "adp-branch-first-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const remote = join(root, "remote"), local = join(root, "local");
  await mkdir(remote);
  const git = async (cwd: string, ...args: string[]) => (await run("git", args, { cwd })).stdout.trim();
  await git(remote, "init", "-b", "trunk");
  await git(remote, "config", "user.name", "Test");
  await git(remote, "config", "user.email", "test@example.invalid");
  await git(remote, "commit", "--allow-empty", "-m", "base");
  await git(root, "clone", remote, local);
  assert.deepEqual(await prepareStoryBranch(local, 42), { branch: "agent/issue-42", status: "up_to_date", conflicts: [] });
  assert.equal(await git(local, "rev-parse", "HEAD"), await git(remote, "rev-parse", "trunk"));
  await assert.rejects(prepareStoryBranch(local, 42, "agent/issue-42"), /differ from its base/);

  await git(remote, "branch", "agent/issue-42");
  await git(remote, "switch", "-c", "agent/codex-issue-42");
  await git(remote, "commit", "--allow-empty", "-m", "legacy work");
  await git(remote, "switch", "trunk");
  const fresh = join(root, "ambiguous");
  await git(root, "clone", remote, fresh);
  await assert.rejects(prepareStoryBranch(fresh, 42), /neither is abandoned/);
  assert.equal(await git(fresh, "branch", "--show-current"), "trunk");
});
