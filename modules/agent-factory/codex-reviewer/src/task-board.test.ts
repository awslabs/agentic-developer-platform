import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { acceptanceIds, attributeCommits, boardComplete, breakdownWarnings, carryCommits, isBoardCommit, kindMismatch, newlyDone, nextTask, parseTaskBoard,
  readTaskBoardFile, renderTaskBoard, sanitizeTasks, sizeSignal, taskBoardPath, uncoveredCode, upsertTaskBoardSection, validateTasks, writeTaskBoardFile,
  type Task } from "./task-board.js";

const task = (id: string, kind: Task["kind"], status: Task["status"] = "open", covers: string[] = [], extra: Partial<Task> = {}): Task =>
  ({ id, kind, status, covers, criterion: id.split("-")[0]!, title: `${kind} ${id}`, files: [], note: "", ...extra });

test("task board validation rejects duplicates, dangling coverage, uncovered tests and silent blockers", () => {
  assert.throws(() => validateTasks([task("A-c1", "code"), task("A-c1", "code")]), /Duplicate/);
  assert.throws(() => validateTasks([task("A-t1", "test", "open", ["A-c9"])]), /unknown task/);
  assert.throws(() => validateTasks([task("A-c1", "code"), task("A-t1", "test")]), /must cover at least one code task/);
  assert.throws(() => validateTasks([task("A-c1", "code"), task("A-i1", "infra")]), /must name the task it unblocks/);
  assert.throws(() => validateTasks([task("A-c1", "code", "blocked")]), /needs a note/);
  assert.throws(() => validateTasks([{ ...task("bad id", "code") }]), /invalid id/);
  assert.throws(() => validateTasks("nope"), /array/);
  const valid = validateTasks([task("A-c1", "code"), task("A-t1", "test", "open", ["A-c1"]), task("A-i1", "infra", "open", ["A-t1"]),
    task("B-c1", "code", "blocked", [], { note: "needs deployed target" })]);
  assert.equal(valid.length, 4);
});

test("a code task counts as done only when a done test covers it", () => {
  const tasks = [task("A-c1", "code", "done"), task("A-t1", "test", "open", ["A-c1"])];
  assert.deepEqual(uncoveredCode(tasks).map(t => t.id), ["A-c1"]);
  assert.equal(boardComplete(tasks), false);
  tasks[1]!.status = "done";
  assert.equal(boardComplete(tasks), true);
  assert.equal(boardComplete([]), false);
});

test("next task follows board order, code before its tests, infra only when it unblocks the next task", () => {
  const tasks = [task("A-c1", "code", "done"), task("A-t1", "test", "done", ["A-c1"]),
    task("B-i1", "infra", "open", ["B-t1"]), task("B-c1", "code"), task("B-t1", "test", "open", ["B-c1"])];
  assert.equal(nextTask(tasks)?.id, "B-c1", "infra for a later test does not jump the queue");
  tasks[3]!.status = "done";
  assert.equal(nextTask(tasks)?.id, "B-i1", "infra that unblocks the next task runs first");
  assert.equal(nextTask(tasks.map(t => ({ ...t, status: "done" as const }))), undefined);
});

test("rendered board is a task checklist the issue reader already understands, and round-trips through the PR body", () => {
  const tasks = [task("DATA02-c1", "code", "done", [], { files: ["a.py"] }), task("DATA02-t1", "test", "open", ["DATA02-c1"]),
    task("DATA03-c1", "code", "blocked", [], { note: "needs deployed target" })];
  const board = renderTaskBoard(tasks, { working: "DATA02-t1", since: "14:05 UTC" });
  assert.match(board, /^### Task checklist/);
  assert.match(renderTaskBoard(tasks, { heading: "" }), /^code 1\/2/, "publishers omit the heading the live comment and UI add themselves");
  assert.match(board, /code 1\/2 · test 0\/1 · infra 0\/0 · ▶ now working: DATA02-t1 \(test, DATA02\): test DATA02-t1 \(since 14:05 UTC\)/);
  assert.match(board, /\*\*DATA02\*\*\n- ☑ `code` DATA02-c1/);
  assert.match(board, /- ▶ `test` DATA02-t1 — test DATA02-t1 \(covers DATA02-c1\)/, "the in-progress row carries the indicator itself");
  assert.doesNotMatch(renderTaskBoard(tasks), /▶/);
  assert.match(board, /- ⛔ `code` DATA03-c1 .* — needs deployed target/);
  assert.match(board, /Code without a passing test: DATA02-c1/);
  const body = upsertTaskBoardSection("Model prose\n", tasks, { status: "Checkpoint 2" });
  assert.match(body, /^Model prose\n\n<!-- adp-task-board:start -->/);
  assert.match(body, /_Checkpoint 2_/);
  assert.doesNotMatch(body, /adp-task-board-data/, "the PR body is human-readable only; the branch file is the record");
  const updated = upsertTaskBoardSection(body, tasks.slice(0, 1), {});
  assert.equal((updated.match(/adp-task-board:start/g) ?? []).length, 1, "section is replaced, not appended");
  assert.match(updated, /^Model prose\n\n/);
  // Legacy PR-body data from before the branch file is still readable, once.
  const legacy = `<!-- adp-task-board-data:${Buffer.from(JSON.stringify(tasks)).toString("base64")} -->`;
  assert.deepEqual(parseTaskBoard(legacy), tasks);
  assert.equal(parseTaskBoard("no board here"), null);
  assert.equal(parseTaskBoard("<!-- adp-task-board-data:bm90anNvbg== -->"), null, "corrupt data is ignored, never trusted");
});

test("kind mismatch warns when completed tasks and touched files disagree", () => {
  const code = [task("A-c1", "code", "done")], tests = [task("A-t1", "test", "done", ["A-c1"])];
  assert.match(kindMismatch(code, ["tests/test_a.py", "src/a.test.ts"]) ?? "", /code tasks but only test files/);
  assert.match(kindMismatch(tests, ["src/a.py"]) ?? "", /test tasks but no test files/);
  assert.equal(kindMismatch(code, ["src/a.py", "tests/test_a.py"]), null);
  assert.equal(kindMismatch([], ["src/a.py"]), null);
  assert.equal(kindMismatch(tests, ["modules/gateway/tests/agentauth/test_x.py"]), null);
});

test("newly done tasks are those that changed to done since the previous board", () => {
  const before = [task("A-c1", "code", "done"), task("A-t1", "test", "open", ["A-c1"])];
  const after = [task("A-c1", "code", "done"), task("A-t1", "test", "done", ["A-c1"])];
  assert.deepEqual(newlyDone(before, after).map(t => t.id), ["A-t1"]);
  assert.deepEqual(newlyDone(null, after).map(t => t.id), ["A-c1", "A-t1"]);
});

test("commits are attributed to the task their subject names, else to the tasks finished this turn, and shown on the row", () => {
  const tasks = [task("A-c1", "code", "done"), task("A-t1", "test", "done", ["A-c1"]), task("B-c1", "code", "open"), task("A-c10", "code", "open")];
  const completed = tasks.slice(0, 2);
  const { tasks: mapped, unattributed } = attributeCommits(tasks, [
    { sha: "abc1234def", subject: "feat(#42 A-c1): add parser" },
    { sha: "bbb2222", subject: "test(#42 A-t1): cover parser" },
    { sha: "ccc3333", subject: "chore: tidy imports" },            // no id -> all tasks finished this turn
    { sha: "ddd4444", subject: "feat(#42 A-c10): unrelated id" },   // A-c1 must not match inside A-c10
  ], completed);
  assert.deepEqual(mapped.find(t => t.id === "A-c1")?.commits, ["abc1234", "ccc3333"]);
  assert.deepEqual(mapped.find(t => t.id === "A-t1")?.commits, ["bbb2222", "ccc3333"]);
  assert.deepEqual(mapped.find(t => t.id === "A-c10")?.commits, ["ddd4444"]);
  assert.equal(mapped.find(t => t.id === "B-c1")?.commits, undefined);
  assert.deepEqual(unattributed, []);
  const orphan = attributeCommits(tasks, [{ sha: "eee5555", subject: "wip" }], []);
  assert.deepEqual(orphan.unattributed.map(c => c.sha), ["eee5555"]);
  assert.match(renderTaskBoard(mapped), /- ☑ `code` A-c1 — code A-c1 · `abc1234` `ccc3333`/);
  const carried = carryCommits(mapped, tasks.map(t => ({ ...t, commits: undefined })));
  assert.deepEqual(carried.find(t => t.id === "A-c1")?.commits, ["abc1234", "ccc3333"], "the model never resends commits; the controller carries them");
  assert.ok(isBoardCommit("chore(#42): task board after turn 3") && !isBoardCommit("feat(#42 A-c1): task board rendering"));
  assert.equal(validateTasks([{ ...task("X-c1", "code"), commits: ["not a sha", "0123abc"] }])[0]?.commits?.join(), "0123abc", "only SHAs survive validation");
});

test("the branch board file round-trips, rejects another issue's board, and reports corruption instead of trusting it", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "task-board-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  assert.deepEqual(await readTaskBoardFile(workspace, 42), { tasks: null }, "absent file is simply absent");
  const tasks = [task("A-c1", "code", "done", [], { commits: ["abc1234"] }), task("A-t1", "test", "open", ["A-c1"])];
  const path = await writeTaskBoardFile(workspace, 42, tasks, new Date("2026-10-05T06:00:00Z"));
  assert.equal(path, taskBoardPath(42));
  assert.equal(path, ".adp/tasks/42.json");
  const raw = await readFile(join(workspace, path), "utf8");
  assert.match(raw, /"version": 1,\n  "issue": 42,\n  "updated_at": "2026-10-05T06:00:00.000Z"/);
  assert.match(raw, /\n$/);
  assert.deepEqual((await readTaskBoardFile(workspace, 42)).tasks, tasks);
  assert.deepEqual(await readTaskBoardFile(workspace, 43), { tasks: null }, "another issue's path is simply absent");
  await writeFile(join(workspace, taskBoardPath(43)), JSON.stringify({ version: 1, issue: 42, updated_at: "x", tasks }), "utf8");
  assert.match((await readTaskBoardFile(workspace, 43)).error ?? "", /another issue/, "a board written for a different issue is never adopted");
  await writeFile(join(workspace, path), "{ not json", "utf8");
  const broken = await readTaskBoardFile(workspace, 42);
  assert.equal(broken.tasks, null);
  assert.ok(broken.error);
  await writeFile(join(workspace, path), JSON.stringify({ version: 1, issue: 42, updated_at: "x", tasks: [{ ...task("A-t1", "test") }] }), "utf8");
  assert.match((await readTaskBoardFile(workspace, 42)).error ?? "", /must cover at least one code task/);
});

test("sanitizing a model board repairs every defect to an honest shape and reports it, never throws", () => {
  const { tasks, warnings } = sanitizeTasks([
    { id: "DATA02 c1!", kind: "code", status: "done", title: "ACL write" },
    { id: "DATA02-c1", kind: "feature", status: "in_progress", title: "", covers: ["nope"] },
    { id: "", kind: "test", status: "open", title: "covers nothing" },
    { id: "DATA02-i1", kind: "infra", status: "blocked", title: "harness", covers: [] },
    "garbage",
  ]);
  assert.deepEqual(tasks.map(t => [t.id, t.kind, t.status, t.title]), [
    ["DATA02-c1", "code", "done", "ACL write"],
    ["DATA02-c1-dup", "code", "open", "(untitled)"],
    ["task-3", "test", "open", "covers nothing"],
    ["DATA02-i1", "infra", "blocked", "harness"],
  ]);
  assert.deepEqual(tasks[1]!.covers, [], "dangling coverage is dropped");
  assert.equal(tasks[3]!.note, "(no reason given)");
  for (const expected of [/normalised to DATA02-c1/, /duplicate task id/, /unknown kind "feature"/, /unknown status "in_progress"/, /no title/,
    /named task-3/, /covers unknown tasks nope/, /test task task-3 does not name the code task/, /infra task DATA02-i1 does not name/, /blocked without a reason/, /task 4 ignored/]) {
    assert.match(warnings.join("\n"), expected);
  }
  assert.deepEqual(sanitizeTasks("nope").tasks, []);
});

test("acceptance ids come from the issue's validation table and bold criterion markers", () => {
  const body = [
    "## Impact analysis", "| Failure | Impact | Acceptance ID |", "|---|---|---|", "| bad token | denied | AC-02 |",
    "## Validation", "| ID | Setup and action | Expected | Evidence | Phase |", "|---|---|---|---|---|",
    "| AC-01 | happy path | works | unit | before review |", "| AC-02 | malformed | 400 | unit | before review |",
    "- **DATA01:** A1 reads own history", "**DATA02** — A2 cannot read A1", "**Impact:** high · **Owner:** author_required",
  ].join("\n");
  assert.deepEqual(acceptanceIds(body), ["AC-01", "AC-02", "DATA01", "DATA02"]);
  assert.deepEqual(acceptanceIds(null), []);
  assert.deepEqual(acceptanceIds("no criteria here, just **bold** text and | tables | without | ids |"), []);
});

test("breakdown warnings name missing criteria, uncovered code and orphan tasks, and the size signal flags oversized stories", () => {
  const ids = ["AC-01", "AC-02", "AC-03"];
  const tasks = [task("AC-01-c1", "code", "done"), task("AC-01-t1", "test", "open", ["AC-01-c1"]),
    task("AC-02-c1", "code", "open"), task("X-c1", "code", "open", [], { criterion: "" }), task("AC-01-i1", "infra", "open", ["AC-01-t1"])];
  const warnings = breakdownWarnings(tasks, ids);
  assert.match(warnings.join("\n"), /AC-02 has no test task/);
  assert.match(warnings.join("\n"), /AC-03 has no tasks \(needs at least AC-03-c1 code and AC-03-t1 test\)/);
  assert.match(warnings.join("\n"), /tasks X-c1 serve no acceptance criterion/);
  assert.doesNotMatch(warnings.join("\n"), /AC-01 has no/);
  assert.deepEqual(breakdownWarnings([task("A-c1", "code", "open"), task("A-t1", "test", "open", ["A-c1"])], []), [], "no ids in the issue means no criterion warnings");
  assert.equal(sizeSignal(tasks), null);
  const big = Array.from({ length: 13 }, (_, i) => task(`AC-0${i}-c1`, "code", "open"));
  assert.match(sizeSignal(big) ?? "", /13 code tasks/);
  const live = ["AC-01", "AC-02", "AC-03"].map(id => task(`${id}-c1`, "code", "blocked", [], { note: "deployed-target: owner #6937", criterion: id }));
  assert.match(sizeSignal(live) ?? "", /3 criteria need a deployed target/);
});
