import assert from "node:assert/strict";
import test from "node:test";
import {
  childEnvironment,
  gitEnvironment,
  mergeEnabled,
  selectedModel,
  repositoryUrl,
  WORKER_SANDBOX_MODE,
} from "./reviewer.js";


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

test("persona selection is used before the reviewer deployment default", () => {
  assert.equal(selectedModel({ ADP_MODEL_RESOLVED: "chosen", CODEX_REVIEWER_MODEL: "deployment" }), "chosen");
  assert.equal(selectedModel({ CODEX_REVIEWER_MODEL: "deployment" }), "deployment");
  assert.equal(selectedModel({}), "openai.gpt-5.6-sol");
});
