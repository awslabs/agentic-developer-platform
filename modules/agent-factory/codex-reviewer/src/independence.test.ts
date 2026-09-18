import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";

test("runtime has no Claude SDK or shared agent-worker dependency", async () => {
  const packageJson = await readFile(new URL("../package.json", import.meta.url), "utf8");
  const manifest = JSON.parse(packageJson) as {
    dependencies: Record<string, string>;
  };
  assert.equal("@anthropic-ai/claude-agent-sdk" in manifest.dependencies, false);
  const sourceDirectory = new URL("../src/", import.meta.url);
  const sourceFiles = (await readdir(sourceDirectory)).filter(
    (name) => name.endsWith(".ts") && !name.endsWith(".test.ts"),
  );
  const sources = await Promise.all(
    sourceFiles.map((name) => readFile(new URL(name, sourceDirectory), "utf8")),
  );
  for (const source of sources) {
    assert.doesNotMatch(
      source,
      /claude-agent-sdk|agent-worker|spawn_persona|agent-submit|reviewer-finali[sz]er/i,
    );
  }
});
