import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { authenticatedGit } from "./reviewer.js";

for (const diagnostic of ["Authentication failed", "connection reset"]) {
  test(`git retries only explicit authentication rejection: ${diagnostic}`, async t => {
    const directory = await mkdtemp(join(tmpdir(), "git-auth-test-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    await writeFile(join(directory, "git"), `#!/bin/sh\nif [ "$GH_TOKEN" = "fresh" ]; then exit 0; fi\necho '${diagnostic}' >&2\nexit 128\n`, { mode: 0o755 });
    const oldPath = process.env.PATH;
    t.after(() => { process.env.PATH = oldPath; });
    process.env.PATH = `${directory}:${oldPath}`;
    const forced: boolean[] = [];
    const operation = authenticatedGit({ workspace: directory, githubToken: "old", proxyBaseUrl: "unused",
      getGitHubToken: async force => { forced.push(force === true); return force ? "fresh" : "old"; },
    }, ["push"]);
    if (diagnostic === "Authentication failed") {
      await operation;
      assert.deepEqual(forced, [false, true]);
    } else {
      await assert.rejects(operation, /connection reset/);
      assert.deepEqual(forced, [false]);
    }
  });
}
