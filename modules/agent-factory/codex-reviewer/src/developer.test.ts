import test from "node:test";
import assert from "node:assert/strict";
import { validateTask, developerPrompt, type DeveloperTask } from "./developer.js";
const task: DeveloperTask = { repository: "aws-e/adp", issue: 42, workspace: "/tmp/work", model: "openai.gpt-6-sol" };
test("developer rejects invalid task identifiers before cloning or model execution", () => {
  for (const change of [{ repository: "--help" }, { repository: "https://github.com/aws-e/adp" }, { issue: 0 }, { issue: NaN }, { issue: 1.5 }, { model: "" }]) {
    assert.throws(() => validateTask({ ...task, ...change }));
  }
  assert.doesNotThrow(() => validateTask(task));
});
test("developer receives issue amendments and must execute through PR publication", () => {
  const prompt = developerPrompt(task, { title: "Repair parser", comments: [{ body: "Handle unicode too" }] }, "agent/issue-42", "Developer rules");
  assert.match(prompt, /Handle unicode too/);
  assert.match(prompt, /agent\/issue-42/);
  assert.match(prompt, /actually publish the PR/);
  assert.match(prompt, /Never merge/);
});

test("architect retains its role and publishes design artifacts using native workspace tools", () => {
  const prompt = developerPrompt({ ...task, persona: "architect" }, { title: "Complete deployment inventory", comments: [{ body: "Include optional modules" }] }, "agent/issue-42", "Architect rules");
  assert.match(prompt, /native Codex SDK architect/);
  assert.match(prompt, /native shell, file, git, gh and network tools/);
  assert.match(prompt, /Include optional modules/);
  assert.match(prompt, /every|each to a component/);
  assert.match(prompt, /Markdown design document/);
  assert.match(prompt, /Actually publish the design PR/);
  assert.match(prompt, /Do not implement the proposed system or deploy/);
  assert.match(prompt, /Never merge the PR or force push/);
  assert.doesNotMatch(prompt, /Use your native shell and file tools to implement the issue/);
});
