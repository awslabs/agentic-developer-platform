import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { acceptanceIds, attributeCommits, boardComplete, breakdownWarnings, carryCommits, checkpointIdentity, isBoardCommit, kindMismatch, newlyDone, nextTask, parseTaskBoard,
  readTaskBoardFile, renderTaskBoard, safeCheckpointNote, sanitizeTasks, sizeSignal, taskBoardPath, uncoveredCode, upsertTaskBoardSection, validateTasks, writeTaskBoardFile,
  planSteps, preserveTaskHistory, type Task } from "./task-board.js";

const task = (id: string, kind: Task["kind"], status: Task["status"] = "open", covers: string[] = [], extra: Partial<Task> = {}): Task =>
  ({ id, kind, status, covers, criterion: id.split("-")[0]!, title: `${kind} ${id}`, files: [], note: "", ...extra });

test("saved hierarchy survives process restart and resumes the active child", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-plan-"));
  try {
    const planStep = { id: "recover-chat", title: "Keep the conversation when the connection drops" };
    const tasks = [task("A-c1", "code", "done", [], { planStep, commits: ["abcdef1"] }),
      task("A-t1", "test", "in_progress", ["A-c1"], { planStep }), task("B-c1", "code")];
    await writeTaskBoardFile(workspace, 42, tasks);
    const file = JSON.parse(await readFile(join(workspace, taskBoardPath(42)), "utf8"));
    assert.deepEqual(file.plan, [{ ...planStep, status: "in_progress", taskIds: ["A-c1", "A-t1"], completed: 1, total: 2 }]);
    const restored = (await readTaskBoardFile(workspace, 42)).tasks!;
    assert.deepEqual(restored.map(({ started_at, finished_at, run_id, attempt, checkpoint_note, ...item }) => item), tasks);
    assert.equal(nextTask(restored)?.id, "A-t1");
    assert.equal(planSteps(restored)[0]?.status, "in_progress");
    assert.match(renderTaskBoard(restored), /\*\*▶ Plan `recover-chat` — Keep the conversation/);
    restored[1]!.status = "blocked"; restored[1]!.note = "needs live target";
    assert.equal(planSteps(restored)[0]?.status, "blocked");
    restored[1]!.status = "done";
    assert.equal(planSteps(restored)[0]?.status, "done");
    assert.equal(planSteps([restored[0]!])[0]?.status, "in_progress", "uncovered code cannot finish its parent");
  } finally { await rm(workspace, { recursive: true, force: true }); }
});

test("partial outcomes retain saved children and canonical parent identities", () => {
  const planStep = { id: "login", title: "Keep each environment's login separate" };
  const saved = [task("A-c1", "code", "done", [], { planStep, commits: ["abcdef1"] }),
    task("A-t1", "test", "in_progress", ["A-c1"], { planStep })];
  const reported = sanitizeTasks([task("A-t1", "test", "done", ["A-c1"], { planStep: { ...planStep, title: "Changed" } }),
    task("A-c2", "code", "open", [], { planStep: { ...planStep, title: "Changed" } })], saved);
  const merged = preserveTaskHistory(saved, reported.tasks);
  assert.deepEqual(merged.tasks.map(t => t.id), ["A-c1", "A-t1", "A-c2"]);
  assert.deepEqual(merged.tasks[0], saved[0]);
  assert.deepEqual(merged.tasks[1]?.covers, ["A-c1"]);
  assert.ok(merged.tasks.every(t => t.planStep?.title === planStep.title));
  assert.doesNotThrow(() => validateTasks(merged.tasks));
  assert.equal(boardComplete(merged.tasks), false);
  assert.match(merged.warnings.join(" "), /omitted/);
  assert.throws(() => validateTasks([task("A-c1", "code", "open", [], { planStep: { id: "bad id", title: "x" } })]), /Invalid implementation/);
});

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
  assert.match(raw, /"version": 2,\n  "issue": 42,\n  "updated_at": "2026-10-05T06:00:00.000Z"/);
  assert.match(raw, /\n$/);
  assert.deepEqual((await readTaskBoardFile(workspace, 42)).tasks?.map(({ started_at, finished_at, run_id, attempt, checkpoint_note, ...item }) => item), tasks);
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
    { id: "DATA02-c1", kind: "feature", status: "working", title: "", covers: ["nope"] },
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
  for (const expected of [/normalised to DATA02-c1/, /duplicate task id/, /unknown kind "feature"/, /unknown status "working"/, /no title/,
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

test("GitHub PR progress leads with readable outcomes and collapses the detailed board", () => {
  const planStep = { id: "recover", title: "Keep conversations after disconnects" };
  const tasks = [task("A-c1", "code", "done", [], { planStep }), task("A-t1", "test", "in_progress", ["A-c1"], { planStep })];
  const body = upsertTaskBoardSection("Conversations resume from the last saved event.", tasks);
  assert.match(body, /\*\*Keep conversations after disconnects\*\* — In progress \(1\/2 detailed tasks\)/);
  assert.ok(body.indexOf('Conversations resume') < body.indexOf('### Implementation progress'));
  assert.ok(body.indexOf('<details>') < body.indexOf('- ☑ `code` A-c1'));
  assert.equal(upsertTaskBoardSection(body, tasks), body);
});


test("version-2 fixture checkpoints retain task attempts, original start, completion, pattern and lineage", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-v2-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const first = [task("A-c1", "code", "in_progress", [], { note: "tried parser" }), task("A-t1", "test", "open", ["A-c1"])];
  const start = new Date("2026-10-09T12:00:00Z");
  await writeTaskBoardFile(workspace, 42, first, start, { runId: "run-one", attempt: 2, nodeAddress: "wave/story", flowId: "flow-one" });
  const initial = JSON.parse(await readFile(join(workspace, taskBoardPath(42)), "utf8"));
  assert.equal(initial.version, 2);
  assert.equal(initial.node_address, "wave/story");
  assert.equal(initial.flow_id, "flow-one");
  assert.deepEqual([initial.tasks[0].started_at, initial.tasks[0].finished_at, initial.tasks[0].run_id, initial.tasks[0].attempt, initial.tasks[0].checkpoint_note],
    [start.toISOString(), null, "run-one", 2, "tried parser"]);
  assert.deepEqual([initial.tasks[1].started_at, initial.tasks[1].finished_at, initial.tasks[1].attempt], [null, null, 0]);
  const previous = (await readTaskBoardFile(workspace, 42)).tasks!;
  const finished = first.map((item, index) => index === 0 ? { ...item, status: "done" as const, note: "parser unblocked", pattern_id: "pattern-1" } : item);
  const end = new Date("2026-10-09T12:05:00Z");
  await writeTaskBoardFile(workspace, 42, finished, end, { runId: "run-one", attempt: 2, previous });
  const saved = JSON.parse(await readFile(join(workspace, taskBoardPath(42)), "utf8"));
  assert.deepEqual([saved.tasks[0].started_at, saved.tasks[0].finished_at, saved.tasks[0].attempt, saved.tasks[0].checkpoint_note, saved.tasks[0].pattern_id],
    [start.toISOString(), end.toISOString(), 2, "parser unblocked", "pattern-1"]);
  assert.equal(saved.node_address, "wave/story", "reviewer writes without dispatch preserve header");
  assert.equal(saved.flow_id, "flow-one");
  assert.equal((await readTaskBoardFile(workspace, 42)).tasks?.[0]?.pattern_id, "pattern-1");
  await writeTaskBoardFile(workspace, 42, (await readTaskBoardFile(workspace, 42)).tasks!, new Date("2026-10-09T12:06:00Z"));
  assert.equal((await readTaskBoardFile(workspace, 42)).tasks?.[0]?.attempt, 2, "repeat writes do not create attempts");
});

test("a legacy version-1 fixture reads with defaults and remains byte-for-byte unchanged", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-v1-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const old = await readFile(new URL("../../../../.adp/tasks/6932.json", import.meta.url), "utf8");
  const path = join(workspace, taskBoardPath(6932));
  await mkdir(join(workspace, ".adp", "tasks"), { recursive: true });
  await writeFile(path, old, "utf8");
  const restored = await readTaskBoardFile(workspace, 6932);
  assert.equal(restored.error, undefined);
  assert.ok(restored.tasks?.length);
  assert.deepEqual([restored.tasks![0]!.started_at, restored.tasks![0]!.finished_at, restored.tasks![0]!.run_id,
    restored.tasks![0]!.attempt, restored.tasks![0]!.checkpoint_note], [null, null, null, 0, ""]);
  assert.equal(await readFile(path, "utf8"), old);
});

test("a missing version-1 field and concurrent checkpoint writes cannot corrupt the board", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-atomic-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const tasks = [task("A-c1", "code", "in_progress")];
  await Promise.all(Array.from({ length: 8 }, (_, index) => writeTaskBoardFile(workspace, 42, tasks,
    new Date(1760000000000 + index * 1000), { runId: "run-" + index })));
  assert.equal((await readTaskBoardFile(workspace, 42)).tasks?.[0]?.id, "A-c1");
  assert.equal((await readdir(join(workspace, ".adp", "tasks"))).length, 1, "atomic temp files are removed");
});

test("checkpoint notes mask credentials before truncation", () => {
  const text = "tried token=super-secret and AKIAABCDEFGHIJKLMNOP and Bearer header-secret " + "x".repeat(500);
  const result = safeCheckpointNote(text);
  assert.ok(result.length <= 400);
  assert.match(result, /tried token=\[REDACTED\]/);
  assert.doesNotMatch(result, /super-secret|AKIAABCDEFGHIJKLMNOP|header-secret/);
});

test("checkpoint notes redact quoted credentials and standard private-key blocks before persistence", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-note-secrets-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const fixtures = [
    '{"token":"fixture-json-secret"}',
    '{"api_key": "fixture-api-secret"}',
    "{'password': 'fixture single quoted password'}",
    'password="fixture password with spaces"',
    'token="fixture \\"quoted\\" secret"',
    'token="fixture unterminated secret',
    "-----BEGIN PRIVATE KEY-----\nfixture-key-material\n-----END PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----\nfixture-rsa-key\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-openssh-key\n-----END OPENSSH PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----\nfixture-incomplete-key",
  ];
  for (const fixture of fixtures) {
    const note = `checkpoint ${fixture}; ${"x".repeat(500)}`;
    const sanitized = safeCheckpointNote(note);
    assert.doesNotMatch(sanitized, /fixture/, "synthetic credential must be fully masked");
    assert.match(sanitized, /\[REDACTED\]/);
    assert.ok(sanitized.length <= 400);
    const tasks = [task("A-c1", "code", "done", [], { note })];
    await writeTaskBoardFile(workspace, 42, tasks);
    const raw = await readFile(join(workspace, taskBoardPath(42)), "utf8");
    assert.doesNotMatch(raw, /fixture/, "neither note field may persist the synthetic credential");
    const saved = JSON.parse(raw).tasks[0];
    assert.equal(saved.note, sanitized);
    assert.equal(saved.checkpoint_note, sanitized);
  }
  assert.equal(safeCheckpointNote('Parser accepted {"status":"done"}; tests passed'),
    'Parser accepted {"status":"done"}; tests passed');
});


test("checkpoint persistence masks complete authorization values and qualified token keys", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-qualified-secrets-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const credential = Buffer.from("fixture-user:fixture-password").toString("base64");
  const token = "fixture-token-value";
  const fixtures = [
    { note: `Authorization: Basic ${credential}`, secret: credential },
    { note: `authorization = basic ${credential}`, secret: credential },
    { note: `Proxy-Authorization: Basic ${credential}`, secret: credential },
    { note: `{"Authorization":"Basic ${credential}"}`, secret: credential },
    { note: `Authorization: Digest username="fixture-user", response="${token}"`, secret: token },
    { note: `Authorization: Bearer ${token}`, secret: token },
    { note: `{"access_token":"${token}"}`, secret: token },
    { note: `{'refresh_token': '${token}'}`, secret: token },
    { note: `AWS_SESSION_TOKEN=${token}`, secret: token },
    { note: `export aws_session_token="${token}"`, secret: token },
    { note: `session-token: ${token}`, secret: token },
    { note: `id_token=${token}`, secret: token },
  ];
  for (const [index, fixture] of fixtures.entries()) {
    const note = `${fixture.note}\nretry status unchanged ${"x".repeat(500)}`;
    await writeTaskBoardFile(workspace, 42, [task("A-c1", "code", "in_progress", [], { note })]);
    const raw = await readFile(join(workspace, taskBoardPath(42)), "utf8");
    assert.ok(!raw.includes(fixture.secret), `persisted notes must mask ${fixture.note.split(/[:=]/)[0]}`);
    const saved = JSON.parse(raw).tasks[0];
    for (const value of [saved.note, saved.checkpoint_note]) {
      assert.match(value, /\[REDACTED\]/);
      assert.match(value, /retry status unchanged/);
      assert.ok(value.length <= 400);
    }
    await writeTaskBoardFile(workspace, 43, [task("A-c1", "code", "in_progress")], new Date(),
      { runId: `fixture-run-${index}`, summary: note });
    const summaryRaw = await readFile(join(workspace, taskBoardPath(43)), "utf8");
    assert.ok(!summaryRaw.includes(fixture.secret), "checkpoint summary must also be masked");
    assert.match(JSON.parse(summaryRaw).tasks[0].checkpoint_note, /\[REDACTED\]/);
  }
});

test("controller metadata maps the run envelope without inferring missing dispatch lineage", () => {
  assert.deepEqual(checkpointIdentity({ ADP_MESSAGE_ID: "run-one", ADP_RUN_ATTEMPT: "3",
    ADP_BOARD_NODE_ADDRESS: "wave/story", ADP_BOARD_FLOW_ID: "flow-one" }),
  { runId: "run-one", attempt: 3, nodeAddress: "wave/story", flowId: "flow-one" });
  assert.deepEqual(checkpointIdentity({ ADP_RUN_ATTEMPT: "invalid" }),
    { runId: undefined, attempt: undefined, nodeAddress: undefined, flowId: undefined });
});

test("restarted in-progress tasks increment the attempt and keep safe notes on disk", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-retry-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const tasks = [task("A-c1", "code", "in_progress")];
  await writeTaskBoardFile(workspace, 42, tasks, new Date("2026-10-09T12:00:00Z"), { runId: "run-one", attempt: 1 });
  const previous = (await readTaskBoardFile(workspace, 42)).tasks!;
  await writeTaskBoardFile(workspace, 42, tasks, new Date("2026-10-09T12:10:00Z"), { runId: "run-two", attempt: 2,
    previous, summary: "tried token=not-for-repo; retry blocked" });
  const raw = await readFile(join(workspace, taskBoardPath(42)), "utf8");
  const saved = JSON.parse(raw).tasks[0];
  assert.equal(saved.run_id, "run-two");
  assert.equal(saved.attempt, 2);
  assert.equal(saved.started_at, "2026-10-09T12:10:00.000Z");
  assert.match(saved.checkpoint_note, /token=\[REDACTED\]/);
  assert.doesNotMatch(raw, /not-for-repo/);
});

test("persisted lineage takes precedence over stale tasks and previous snapshots", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-stale-lineage-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  await writeTaskBoardFile(workspace, 42, [task("A-c1", "code", "in_progress")], new Date("2026-10-09T12:00:00Z"),
    { runId: "run-one", attempt: 1, summary: "initial attempt" });
  const stale = (await readTaskBoardFile(workspace, 42)).tasks!;
  await writeTaskBoardFile(workspace, 42, stale, new Date("2026-10-09T12:10:00Z"),
    { runId: "run-two", attempt: 2, summary: "resumed attempt" });
  const completed = stale.map(entry => ({ ...entry, status: "done" as const }));
  await writeTaskBoardFile(workspace, 42, completed, new Date("2026-10-09T12:20:00Z"),
    { runId: "run-two", attempt: 2, previous: stale });
  const saved = (await readTaskBoardFile(workspace, 42)).tasks![0]!;
  assert.equal(saved.run_id, "run-two");
  assert.equal(saved.attempt, 2);
  assert.equal(saved.started_at, "2026-10-09T12:10:00.000Z");
  assert.equal(saved.finished_at, "2026-10-09T12:20:00.000Z");
  assert.equal(saved.checkpoint_note, "resumed attempt");
  await writeTaskBoardFile(workspace, 42, completed, new Date("2026-10-09T12:30:00Z"));
  assert.deepEqual((await readTaskBoardFile(workspace, 42)).tasks![0], saved, "a later rewrite must not restore stale lineage");
});

test("same-run harness attempts advance once and unchanged checkpoints retain their start", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-harness-retry-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const tasks = [task("A-c1", "code", "in_progress")];
  await writeTaskBoardFile(workspace, 42, tasks, new Date("2026-10-09T12:00:00Z"), { runId: "same-run", attempt: 1 });
  await writeTaskBoardFile(workspace, 42, tasks, new Date("2026-10-09T12:10:00Z"), { runId: "same-run", attempt: 2 });
  const retried = (await readTaskBoardFile(workspace, 42)).tasks![0]!;
  assert.equal(retried.run_id, "same-run");
  assert.equal(retried.attempt, 2);
  assert.equal(retried.started_at, "2026-10-09T12:10:00.000Z");
  assert.equal(retried.finished_at, null);
  await writeTaskBoardFile(workspace, 42, tasks, new Date("2026-10-09T12:20:00Z"), { runId: "same-run", attempt: 2 });
  assert.deepEqual((await readTaskBoardFile(workspace, 42)).tasks![0], retried);
  await writeTaskBoardFile(workspace, 42, tasks.map(entry => ({ ...entry, status: "done" })), new Date("2026-10-09T12:30:00Z"),
    { runId: "same-run", attempt: 2 });
  const completed = (await readTaskBoardFile(workspace, 42)).tasks![0]!;
  assert.equal(completed.attempt, 2);
  assert.equal(completed.started_at, retried.started_at);
  assert.equal(completed.finished_at, "2026-10-09T12:30:00.000Z");
  await writeTaskBoardFile(workspace, 42, [completed], new Date("2026-10-09T12:40:00Z"), { runId: "same-run", attempt: 3 });
  assert.deepEqual((await readTaskBoardFile(workspace, 42)).tasks![0], completed, "a later harness attempt must not relabel completed work");
});

test("new fixture boards populate every version-2 field for later live evaluation", async t => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-live-ready-"));
  t.after(() => rm(workspace, { recursive: true, force: true }));
  const schema = JSON.parse(await readFile(new URL("../schemas/task-board-v2.schema.json", import.meta.url), "utf8"));
  const tasks = [task("A-c1", "code", "done", [], { note: "verified" }), task("A-t1", "test", "done", ["A-c1"], { note: "passed" })];
  await writeTaskBoardFile(workspace, 42, tasks, new Date("2026-10-09T12:00:00Z"),
    { runId: "run-1", attempt: 1, nodeAddress: "wave/story", flowId: "flow-1" });
  const file = JSON.parse(await readFile(join(workspace, taskBoardPath(42)), "utf8"));
  for (const key of schema.required as string[]) assert.ok(key in file, `board needs ${key}`);
  assert.equal(file.version, schema.properties.version.const);
  for (const entry of file.tasks) {
    for (const key of schema.properties.tasks.items.required as string[]) assert.ok(key in entry, `task needs ${key}`);
    assert.ok(entry.started_at && entry.finished_at && entry.run_id && entry.attempt > 0 && entry.checkpoint_note);
  }
});
