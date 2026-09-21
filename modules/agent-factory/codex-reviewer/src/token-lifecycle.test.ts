import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import test from "node:test";
import { withGitHubTokenRenewal } from "./token-lifecycle.js";

test("embedded reviews initialize renewal, use current tokens and stop refreshing on exit", async t => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  let calls = 0;
  let publishedToken = "";
  const manager = { getRuntimeGitHubToken: async () => {
    publishedToken = `renewed-${++calls}`;
    return publishedToken;
  } };
  const result = await withGitHubTokenRenewal(async (getToken, initial) => {
    assert.equal(initial, "renewed-1");
    t.mock.timers.tick(300_000);
    await setImmediate();
    assert.equal(publishedToken, "renewed-2");
    assert.equal(await getToken(), "renewed-3");
    return "review delivered";
  }, async () => manager);
  assert.equal(result, "review delivered");
  t.mock.timers.tick(600_000);
  await setImmediate();
  assert.equal(calls, 3);
});

test("review failure clears renewal and preserves the original failure", async t => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  let calls = 0;
  await assert.rejects(withGitHubTokenRenewal(async () => { throw new Error("inspection failed"); },
    async () => ({ getRuntimeGitHubToken: async () => { calls++; return "token"; } })), /inspection failed/);
  t.mock.timers.tick(600_000);
  await setImmediate();
  assert.equal(calls, 1);
});

test("initialization refuses withheld credentials instead of falling back to startup env", async () => {
  let ran = false;
  await assert.rejects(withGitHubTokenRenewal(async () => { ran = true; },
    async () => ({ getRuntimeGitHubToken: async () => { throw new Error("Mediated run: no GitHub token is available"); } })), /Mediated run/);
  assert.equal(ran, false);
});

test("PAT credentials are delegated unchanged to the shared manager", async () => {
  await withGitHubTokenRenewal(async (getToken, initial) => {
    assert.equal(initial, "configured-pat");
    assert.equal(await getToken(), initial);
  }, async () => ({ getRuntimeGitHubToken: async () => "configured-pat" }));
});

test("transient proactive renewal failure is sanitized and retried before delivery", async t => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  let calls = 0;
  let warnings = 0;
  await withGitHubTokenRenewal(async (getToken) => {
    t.mock.timers.tick(300_000);
    await setImmediate();
    assert.equal(warnings, 1);
    assert.equal(await getToken(), "fresh-token");
  }, async () => ({ getRuntimeGitHubToken: async () => {
    if (++calls === 2) throw new Error("broker response containing sensitive data");
    return calls === 1 ? "bootstrap-token" : "fresh-token";
  } }), 300_000, (...args) => { assert.equal(args.length, 0); warnings++; });
});
