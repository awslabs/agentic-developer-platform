import assert from "node:assert/strict";
import test from "node:test";
import { ProcessError, run } from "./process.js";

test("failed commands preserve stdout diagnostics and stderr", async () => {
  await assert.rejects(run(process.execPath, ["-e", "console.log('file.ts:3: trailing whitespace'); console.error('check failed'); process.exit(2)"]),
    error => error instanceof ProcessError && error.result.exitCode === 2
      && error.message.includes("file.ts:3: trailing whitespace") && error.message.includes("check failed"));
});

test("signal termination cannot look like a successful empty result", async () => {
  await assert.rejects(run(process.execPath, ["-e", "process.kill(process.pid, 'SIGTERM')"]), /signal SIGTERM/);
});

test("reviewer CLI publishes a structured failure on nonzero exit", async () => {
  const result = await run(process.execPath, [new URL("./index.js", import.meta.url).pathname], { allowFailure: true });
  assert.equal(result.exitCode, 1);
  const failure = JSON.parse(result.stdout.trim().split("\n").at(-1)!);
  assert.equal(failure.status, "review_failed");
  assert.match(failure.error.message, /shared worker entrypoint/);
});

test("long argument lists do not crowd out the diagnostic cause", () => {
  const error = new ProcessError("git", ["add", ...Array(200).fill("some/long/path.ts")],
    { stdout: "ignored path: story.ts", stderr: "", exitCode: 1 }, null);
  assert.ok(error.message.slice(0, 1024).includes("ignored path: story.ts"));
});
