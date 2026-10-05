import test from "node:test";
import assert from "node:assert/strict";
import { RepositoryAdapter, type Provider, type ProviderOperation, type ProviderBroker } from "./providers.js";

const HEAD = "a".repeat(40), MOVED = "b".repeat(40), MERGE = "c".repeat(40);
function fixture(provider: Provider, head = HEAD) {
  return provider === "github"
    ? { number: 7, state: "open", title: "Feature", body: "Requirements", head: { sha: head, ref: "agent/task-1" }, base: { ref: "main" } }
    : { iid: 7, state: "opened", title: "Feature", description: "Requirements", sha: head, source_branch: "agent/task-1", target_branch: "main" };
}
for (const provider of ["github", "gitlab"] as const) {
  test(`${provider}: common read/create/update contract preserves provider binding`, async () => {
    const calls: ProviderOperation[] = [];
    const broker: ProviderBroker = { async execute(operation) { calls.push(operation); return fixture(provider); } };
    const repository = provider === "github" ? "owner/repo" : "group/subgroup/repo";
    const adapter = new RepositoryAdapter(provider, repository, broker);
    const change = await adapter.readChange(7);
    assert.deepEqual(change, { number: 7, state: "open", head: HEAD, branch: "agent/task-1", base: "main", title: "Feature", description: "Requirements" });
    await adapter.createChange({ branch: "agent/task-1", base: "main", title: "Feature", description: "Requirements", operationKey: "run:1:create" });
    await adapter.updateDescription(7, "Requirements", "run:1:update");
    assert.deepEqual(calls.map(c => c.capability), ["repository.read", "change.create", "change.update"]);
    assert.deepEqual(calls.map(c => c.method), ["GET", "POST", provider === "github" ? "PATCH" : "PUT"]);
    assert.equal(calls[1]?.operationKey, "run:1:create");
    assert.ok(calls.every(c => c.provider === provider && c.repository === repository));
    if (provider === "gitlab") assert.match(calls[0]!.path, /group%2Fsubgroup%2Frepo/);
  });
  test(`${provider}: moved-head and nonterminal merge cannot be reported as merged`, async () => {
    let current = HEAD;
    let merged = false;
    const adapter = new RepositoryAdapter(provider, "owner/repo", { async execute(operation) {
      assert.equal(operation.capability, "change.merge");
      assert.equal(operation.method, "PUT");
      assert.equal(operation.operationKey, "run:merge");
      if (operation.body?.sha !== current) throw new Error("409 head mismatch");
      if (provider === "gitlab") assert.equal(operation.body.auto_merge, false);
      merged = true;
      return provider === "github" ? { merged: true, sha: MERGE } : { state: "merged", merge_commit_sha: MERGE };
    } });
    current = MOVED;
    await assert.rejects(adapter.merge(7, HEAD, "run:merge"), /409/);
    assert.equal(merged, false);
    assert.deepEqual(await adapter.merge(7, MOVED, "run:merge"), { commit: MERGE });
    const pending = new RepositoryAdapter(provider, "owner/repo", { async execute() {
      return provider === "github" ? { merged: false, sha: MERGE } : { state: "opened", merge_commit_sha: null };
    } });
    await assert.rejects(pending.merge(7, HEAD, "run:pending"));
  });
  test(`${provider}: invalid mutation binding never reaches the broker`, async () => {
    let calls = 0;
    const adapter = new RepositoryAdapter(provider, "owner/repo", { async execute() { calls++; return fixture(provider); } });
    await assert.rejects(adapter.merge(7, "main", "run:merge"));
    await assert.rejects(adapter.merge(7, HEAD, ""));
    await assert.rejects(adapter.createChange({ branch: "../main", base: "main", title: "x", description: "", operationKey: "run:create" }));
    await assert.rejects(adapter.createChange({ branch: "main", base: "main", title: "x", description: "", operationKey: "run:create" }));
    assert.equal(calls, 0);
  });
}

test("GitLab formal review is not falsely implemented as a comment or unapproval", async () => {
  let calls = 0;
  const adapter = new RepositoryAdapter("gitlab", "group/repo", { async execute() { calls++; } });
  assert.ok(!adapter.capabilities.includes("review.submit"));
  await assert.rejects(adapter.submitReview(7, HEAD, "request_changes", "Fix requirement", "run:review"), /not implemented/);
  assert.equal(calls, 0);
});

test("GitHub review names the exact reviewed commit", async () => {
  const adapter = new RepositoryAdapter("github", "owner/repo", { async execute(operation) {
    assert.equal(operation.path, "/repos/owner/repo/pulls/7/reviews");
    assert.deepEqual(operation.body, { commit_id: HEAD, event: "REQUEST_CHANGES", body: "Missing requirement" });
    return { id: 1 };
  } });
  await adapter.submitReview(7, HEAD, "request_changes", "Missing requirement", "run:review");
});

test("repository paths cannot redirect an operation outside the bound API resource", () => {
  const broker = { async execute() {} };
  for (const repository of ["../repo", "owner/..", "https://other/repo", "owner/repo?token=x", "owner//repo"]) {
    assert.throws(() => new RepositoryAdapter("github", repository, broker));
    assert.throws(() => new RepositoryAdapter("gitlab", repository, broker));
  }
});
