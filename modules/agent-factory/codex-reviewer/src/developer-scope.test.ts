import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { developerScopeHash, parseDeveloperNodeScope, restoreDeveloperBoard, type DeveloperNodeScope } from "./developer-scope.js";
import { developerPrompt, interpretDeveloperOutcome, type DeveloperTask } from "./developer.js";
import { taskBoardPath, writeTaskBoardFile, type Task } from "./task-board.js";

const scope: DeveloperNodeScope = {
  schema_version: 1, flow_id: "flow-one", node_id: "node-one", graph_address: "flow/epic/foundation/baseline",
  plan_version: 2, repository: "example/service", issue_number: 1, title: "Reconcile contracts",
  criteria: ["baseline/D1: Freeze protocol contracts.", "baseline/C1: Verify the agreed baseline."],
  evidence_expectations: ["verified-merge"], delivery_mode: "code_only",
};
const expected = { repository: scope.repository, issue: scope.issue_number, flowId: scope.flow_id, nodeAddress: scope.graph_address };
const task = (id: string, status: Task["status"] = "open"): Task => ({
  id, kind: "code", criterion: "baseline/C1", title: "Check the baseline", status, covers: [], files: [], note: "",
});

test("accepted dispatch context is bound to its flow, node address, repository and issue", () => {
  assert.deepEqual(parseDeveloperNodeScope(JSON.stringify(scope), expected), scope);
  assert.equal(parseDeveloperNodeScope("", expected), undefined);
  for (const change of [
    { flow_id: "other-flow" }, { graph_address: "other/node" }, { repository: "other/repo" },
    { issue_number: 2 }, { schema_version: 9 }, { plan_version: 0 }, { criteria: [null] }, { criteria: "wider issue" },
  ]) assert.throws(() => parseDeveloperNodeScope(JSON.stringify({ ...scope, ...change }), expected));
  assert.throws(() => parseDeveloperNodeScope("null", expected));
});

test("a shared epic provides context while accepted node criteria govern the developer prompt", () => {
  const input: DeveloperTask = { repository: scope.repository, issue: 1, workspace: "/tmp/fixture", model: "fixture", nodeScope: scope };
  const prompt = developerPrompt(input, { body: "Whole epic: authentication, runtime, release." }, "agent/issue-1", "");
  assert.match(prompt, /Whole epic: authentication/);
  assert.match(prompt, /implement the accepted delivery node/);
  assert.match(prompt, /baseline\/D1: Freeze protocol contracts/);
  assert.match(prompt, /do not expand this node into the whole issue or epic/);
  assert.match(prompt, /Refs #1 without closing the shared issue/);
  assert.doesNotMatch(prompt, /implement every acceptance criterion/);
});

test("a whole-epic board is archived byte-for-byte without becoming current work or completion", async () => {
  const directory = await mkdtemp(join(tmpdir(), "developer-node-board-"));
  try {
    await writeTaskBoardFile(directory, 1, [task("unrelated-open"), task("prior-done", "done")], new Date(), {
      flowId: scope.flow_id, nodeAddress: scope.graph_address,
    });
    const original = await readFile(join(directory, taskBoardPath(1)), "utf8");
    const restored = await restoreDeveloperBoard(directory, 1, scope);
    assert.equal(restored.tasks, null);
    assert.ok(restored.archivedPath);
    assert.equal(await readFile(join(directory, restored.archivedPath), "utf8"), original);
    assert.equal(await readFile(join(directory, taskBoardPath(1)), "utf8"), original);
    assert.equal((await restoreDeveloperBoard(directory, 1, scope)).archivedPath, restored.archivedPath);
    const reported = interpretDeveloperOutcome(JSON.stringify({
      outcome: "complete", summary: "Current node verified", tasks: [task("baseline-check", "done"),
        { ...task("baseline-test", "done"), kind: "test", covers: ["baseline-check"] }], remainingWork: [], pullRequestUrl: "",
    }), restored.tasks);
    assert.equal(reported.outcome.outcome, "complete");
    assert.deepEqual(reported.outcome.tasks.map(t => t.id), ["baseline-check", "baseline-test"]);
    await writeTaskBoardFile(directory, 1, reported.outcome.tasks, new Date(), {
      flowId: scope.flow_id, nodeAddress: scope.graph_address, scopeHash: developerScopeHash(scope), runId: "new-run",
    });
    assert.deepEqual((await restoreDeveloperBoard(directory, 1, scope)).tasks?.map(t => t.id), ["baseline-check", "baseline-test"]);
    const otherNode = { ...scope, node_id: "node-two", graph_address: "flow/epic/foundation/auth" };
    assert.equal((await restoreDeveloperBoard(directory, 1, otherNode)).tasks, null);
    assert.equal(await readFile(join(directory, restored.archivedPath), "utf8"), original);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("same-scope retries retain work; changed requirements get a new board; legacy reads still work", async () => {
  const directory = await mkdtemp(join(tmpdir(), "developer-scope-resume-"));
  try {
    await writeTaskBoardFile(directory, 1, [task("baseline-check", "done")], new Date(), {
      flowId: scope.flow_id, nodeAddress: scope.graph_address, scopeHash: developerScopeHash(scope), runId: "original-run",
    });
    assert.equal((await restoreDeveloperBoard(directory, 1, { ...scope, plan_version: 3 })).tasks?.[0]?.status, "done");
    assert.equal((await restoreDeveloperBoard(directory, 1)).tasks?.[0]?.status, "done");
    const changed = { ...scope, criteria: [...scope.criteria, "baseline/C2: Check a new invariant."] };
    assert.equal((await restoreDeveloperBoard(directory, 1, changed)).tasks, null);
    await writeTaskBoardFile(directory, 1, [task("baseline-check", "open")], new Date(), {
      flowId: scope.flow_id, nodeAddress: scope.graph_address, scopeHash: developerScopeHash(changed), runId: "new-run",
    });
    const current = (await restoreDeveloperBoard(directory, 1, changed)).tasks![0]!;
    assert.equal(current.status, "open");
    assert.equal(current.finished_at, null);
    assert.equal(current.run_id, null, "old-scope attribution must not carry into new work");
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("an unscoped issue worker cannot leave a stale accepted-node marker on its wider board", async () => {
  const directory = await mkdtemp(join(tmpdir(), "developer-scope-writer-"));
  try {
    const tasks = [task("baseline-check", "done")];
    await writeTaskBoardFile(directory, 1, tasks, new Date(), {
      flowId: scope.flow_id, nodeAddress: scope.graph_address, scopeHash: developerScopeHash(scope),
    });
    await writeTaskBoardFile(directory, 1, tasks, new Date(), {
      flowId: scope.flow_id, nodeAddress: scope.graph_address, runId: "reviewer-run",
    });
    assert.ok((await restoreDeveloperBoard(directory, 1, scope)).tasks, "a reviewer on the same node retains its scope");
    await writeTaskBoardFile(directory, 1, [...tasks, task("whole-issue-work")], new Date(), { runId: "webhook-run" });
    assert.equal((await restoreDeveloperBoard(directory, 1, scope)).tasks, null);
    assert.equal((await restoreDeveloperBoard(directory, 1)).tasks?.length, 2, "the wider board remains available as history");
  } finally { await rm(directory, { recursive: true, force: true }); }
});
