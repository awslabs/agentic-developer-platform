import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { continuationPrompt, publicationPrompt, publishedPullRequest, developerOutcomeSchema, developerPrompt, interpretDeveloperOutcome, parseDeveloperOutcome, resumeNote, runDeveloperTurns, validateTask,
  type DeveloperOutcome, type DeveloperTask } from "./developer.js";
import { ModelExecutionBudget } from "./model-budget.js";
import { readTaskBoardFile, writeTaskBoardFile, type Task } from "./task-board.js";

const task: DeveloperTask = { repository: "aws-e/adp", issue: 42, workspace: "/tmp/work", model: "openai.gpt-6-sol" };
const usage = { input_tokens: 10, output_tokens: 5, cached_input_tokens: 0, cache_write_input_tokens: 0, reasoning_output_tokens: 0 };
const t = (id: string, kind: Task["kind"], status: Task["status"], covers: string[] = [], note = ""): Task =>
  ({ id, kind, status, covers, criterion: id.split("-")[0]!, title: `${kind} ${id}`, files: [], note });
const outcome = (value: Partial<DeveloperOutcome> & { outcome: DeveloperOutcome["outcome"] }) => JSON.stringify({
  summary: "Worked", tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "done", ["AC1-c1"])], remainingWork: [], pullRequestUrl: "", ...value });

test("developer rejects invalid task identifiers before cloning or model execution", () => {
  for (const change of [{ repository: "--help" }, { repository: "https://github.com/aws-e/adp" }, { issue: 0 }, { issue: NaN }, { issue: 1.5 }, { model: "" }, { maxTurns: 0 }]) {
    assert.throws(() => validateTask({ ...task, ...change }));
  }
  assert.doesNotThrow(() => validateTask(task));
});

test("developer receives issue amendments, the task-board contract and must execute through PR publication", () => {
  const prompt = developerPrompt(task, { title: "Repair parser", comments: [{ body: "Handle unicode too" }] }, "agent/issue-42", "Developer rules");
  assert.match(prompt, /Handle unicode too/);
  assert.match(prompt, /agent\/issue-42/);
  assert.match(prompt, /actually publish the PR/);
  assert.match(prompt, /Never merge/);
  assert.match(prompt, /task board of code, test and infra tasks/);
  assert.match(prompt, /never stop early or narrow the story at a checkpoint/);
  assert.match(prompt, /commits with the task id/);
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
  assert.doesNotMatch(prompt, /task board/);
});

test("incomplete continuations and finishing turns preserve draft checkpoints", () => {
  const pending = parseDeveloperOutcome(outcome({ outcome: "checkpoint", remainingWork: ["AC1-t1"],
    tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])] }));
  const initial = developerPrompt(task, {}, "agent/issue-42", "");
  assert.match(initial, /progress to the issue without requiring a PR/);
  assert.doesNotMatch(initial, /Open the PR at the first checkpoint/);
  assert.match(continuationPrompt(pending), /Keep any existing draft as a draft/);
  assert.match(publicationPrompt(pending), /do not create a PR or mark it ready while tasks remain/);
  assert.doesNotMatch(publicationPrompt(pending), /make sure a ready/);
  assert.match(publicationPrompt(parseDeveloperOutcome(outcome({ outcome: "complete" }))), /make sure a ready \(non-draft\) pull request/);
});

test("published drafts satisfy checkpoints but never completed delivery", async () => {
  const directory = await mkdtemp(join(tmpdir(), "developer-publication-"));
  const oldPath = process.env.PATH;
  try {
    await mkdir(join(directory, "bin"));
    const gh = join(directory, "bin", "gh");
    process.env.PATH = join(directory, "bin") + ":" + oldPath;
    const setPullRequests = async (rows: unknown[]) => writeFile(gh,
      `#!/bin/sh\ncat <<'FIXTURE'\n${JSON.stringify(rows)}\nFIXTURE\n`, { mode: 0o700 });
    const pr = { url: "https://github.com/fixture/repo/pull/1", headRefOid: "head", changedFiles: 1, isDraft: true };
    await setPullRequests([pr]);
    assert.equal((await publishedPullRequest(directory, "fixture/repo", "agent/issue-42", "head", false))?.url, pr.url);
    assert.equal(await publishedPullRequest(directory, "fixture/repo", "agent/issue-42", "head", true), undefined);
    assert.equal(await publishedPullRequest(directory, "fixture/repo", "agent/issue-42", "stale", false), undefined);
    await setPullRequests([]);
    assert.equal(await publishedPullRequest(directory, "fixture/repo", "agent/issue-42", "head", false), undefined);
    await setPullRequests([{ ...pr, isDraft: false }]);
    assert.equal((await publishedPullRequest(directory, "fixture/repo", "agent/issue-42", "head", true))?.url, pr.url);
    await setPullRequests([{ ...pr, isDraft: false, changedFiles: 0 }]);
    assert.equal(await publishedPullRequest(directory, "fixture/repo", "agent/issue-42", "head", true), undefined);
  } finally {
    if (oldPath === undefined) delete process.env.PATH; else process.env.PATH = oldPath;
    await rm(directory, { recursive: true, force: true });
  }
});

test("developer outcome schema is strict; interpretation corrects dishonest outcomes instead of failing the run", () => {
  assert.equal(developerOutcomeSchema.additionalProperties, false);
  assert.deepEqual(developerOutcomeSchema.required, ["outcome", "summary", "tasks", "remainingWork", "pullRequestUrl"]);
  // Prose is the only "malformed" case; it falls back to the last known board as a checkpoint.
  const prose = interpretDeveloperOutcome("Tests passed; PR opened.", [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])]);
  assert.equal(prose.malformed, true);
  assert.equal(prose.outcome.outcome, "checkpoint");
  assert.deepEqual(prose.outcome.remainingWork, ["AC1-t1"]);
  assert.equal(prose.outcome.summary, "Tests passed; PR opened.");
  // A dishonest complete is downgraded, never accepted and never fatal.
  const open = interpretDeveloperOutcome(outcome({ outcome: "complete", tasks: [t("AC1-c1", "code", "open")] }), null);
  assert.equal(open.malformed, false);
  assert.equal(open.outcome.outcome, "checkpoint");
  assert.match(open.warnings.join("; "), /open tasks \[AC1-c1\]/);
  assert.deepEqual(open.outcome.remainingWork, ["AC1-c1"]);
  const uncovered = interpretDeveloperOutcome(outcome({ outcome: "complete", tasks: [t("AC1-c1", "code", "done")] }), null);
  assert.equal(uncovered.outcome.outcome, "checkpoint");
  assert.match(uncovered.warnings.join("; "), /uncovered code \[AC1-c1\]/);
  assert.deepEqual(uncovered.outcome.remainingWork, ["AC1-c1: covering test"]);
  const stray = interpretDeveloperOutcome(outcome({ outcome: "complete", remainingWork: ["more"] }), null);
  assert.equal(stray.outcome.outcome, "complete");
  assert.deepEqual(stray.outcome.remainingWork, []);
  assert.match(stray.warnings.join("; "), /cannot list remaining work/);
  const noList = interpretDeveloperOutcome(outcome({ outcome: "checkpoint", remainingWork: [], tasks: [t("AC1-c1", "code", "open")] }), null);
  assert.deepEqual(noList.outcome.remainingWork, ["AC1-c1"]);
  const unknown = interpretDeveloperOutcome(outcome({ outcome: "finished" as never }), null);
  assert.equal(unknown.outcome.outcome, "checkpoint");
  const blockedNoTask = interpretDeveloperOutcome(outcome({ outcome: "blocked" }), null);
  assert.equal(blockedNoTask.outcome.outcome, "blocked", "a reported blocker is honoured, not argued with");
  assert.match(blockedNoTask.warnings.join("; "), /no task is marked blocked/);
  const noBoard = interpretDeveloperOutcome(outcome({ outcome: "checkpoint", remainingWork: ["x"], tasks: [] }), [t("AC1-c1", "code", "open")]);
  assert.deepEqual(noBoard.outcome.tasks.map(x => x.id), ["AC1-c1"], "a missing board keeps the previous one");
  // The strict parser still exists for callers that want the exact defect.
  assert.throws(() => parseDeveloperOutcome("prose"), /structured outcome JSON/);
  assert.throws(() => parseDeveloperOutcome(outcome({ outcome: "complete", tasks: [t("AC1-c1", "code", "open")] })), /open tasks \[AC1-c1\]/);
  const parsed = parseDeveloperOutcome(outcome({ outcome: "checkpoint", remainingWork: ["AC1-t1: cover parser"],
    tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])], pullRequestUrl: " https://github.com/aws-e/adp/pull/1 " }));
  assert.equal(parsed.pullRequestUrl, "https://github.com/aws-e/adp/pull/1");
});

function fakeRuns(responses: string[]) {
  const prompts: string[] = [];
  const runTurn = async (prompt: string, _signal?: AbortSignal) => {
    prompts.push(prompt);
    const finalResponse = responses.shift();
    if (finalResponse === undefined) throw new Error("no more scripted turns");
    return { items: [], finalResponse, usage };
  };
  return { prompts, runTurn };
}

test("developer continues the same thread through checkpoints until the board is complete", async () => {
  const checkpoint = outcome({ outcome: "checkpoint", summary: "Parser repaired", remainingWork: ["AC1-t1: cover parser"],
    tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])] });
  const complete = outcome({ outcome: "complete", summary: "Story done" });
  const { prompts, runTurn } = fakeRuns([checkpoint, complete]);
  const seen: string[] = [];
  const notes: string[] = [];
  const result = await runDeveloperTurns("start", runTurn, { budget: new ModelExecutionBudget(3_600_000), maxTurns: 5,
    onOutcome: async (o, turn) => { seen.push(`${turn}:${o.outcome}`); },
    beforeContinue: async () => { notes.push("asked"); return "Uncommitted changes remain: M src/a.py"; } });
  assert.deepEqual(seen, ["1:checkpoint", "2:complete"]);
  assert.deepEqual(notes, ["asked"]);
  assert.equal(result.turns, 2);
  assert.equal(result.exhausted, false);
  assert.equal(result.outcome.outcome, "complete");
  assert.equal(prompts[0], "start");
  assert.match(prompts[1]!, /previous turn was a checkpoint, not completion/);
  assert.match(prompts[1]!, /Next task: AC1-t1 \(test, AC1\): test AC1-t1/);
  assert.match(prompts[1]!, /### Task checklist/);
  assert.match(prompts[1]!, /Uncommitted changes remain: M src\/a.py/);
  assert.match(prompts[1]!, /Do not re-plan or re-read/);
});

test("development continues beyond 24 checkpoints when no turn limit is configured", async () => {
  const checkpoint = outcome({ outcome: "checkpoint", remainingWork: ["AC1-t1"],
    tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])] });
  // A malformed response after the former cutoff still gets its correction turn.
  const runs = fakeRuns([...Array<string>(25).fill(checkpoint), "Return format needs correction", outcome({ outcome: "complete" })]);
  const seen: number[] = [];
  const result = await runDeveloperTurns("start", runs.runTurn, {
    budget: new ModelExecutionBudget(3_600_000),
    onOutcome: async (_outcome, turn) => { seen.push(turn); },
  });
  assert.equal(result.turns, 27);
  assert.equal(result.exhausted, false);
  assert.equal(result.outcome.outcome, "complete");
  assert.ok(seen.includes(25));
  assert.match(runs.prompts[26]!, /not the structured outcome/);
});

test("execution allowance still stops development without an explicit turn limit", async () => {
  const checkpoint = outcome({ outcome: "checkpoint", remainingWork: ["AC1-t1"],
    tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])] });
  const runs = fakeRuns([checkpoint, outcome({ outcome: "complete" })]);
  let clock = 0;
  const budget = new ModelExecutionBudget(10_000, () => clock);
  const result = await runDeveloperTurns("start", async (prompt, signal) => {
    clock += 6_000;
    return runs.runTurn(prompt, signal);
  }, { budget, minTurnMs: 5_000 });
  assert.equal(result.turns, 1);
  assert.equal(result.exhausted, true);
  assert.equal(result.outcome.outcome, "checkpoint");
  assert.deepEqual(result.outcome.remainingWork, ["AC1-t1"]);
});

test("a malformed outcome gets one correction turn, then the loop continues from the last board instead of failing", async () => {
  const good = outcome({ outcome: "complete" });
  const dishonest = outcome({ outcome: "complete", tasks: [t("AC1-c1", "code", "open")] });
  let runs = fakeRuns(["I think I'm done.", good]);
  let result = await runDeveloperTurns("start", runs.runTurn, { budget: new ModelExecutionBudget(3_600_000), maxTurns: 5 });
  assert.equal(result.turns, 2);
  assert.match(runs.prompts[1]!, /not the structured outcome/);
  assert.match(runs.prompts[1]!, /Make no further changes in this turn/);
  // Two prose replies in a row: no failure; the run continues as a checkpoint on the previous board.
  const checkpoint = outcome({ outcome: "checkpoint", remainingWork: ["AC1-t1"], tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])] });
  runs = fakeRuns([checkpoint, "still prose", "more prose", good]);
  const warningsSeen: string[][] = [];
  result = await runDeveloperTurns("start", runs.runTurn, { budget: new ModelExecutionBudget(3_600_000), maxTurns: 6,
    onOutcome: async (_o, _turn, warnings) => { warningsSeen.push(warnings); } });
  assert.equal(result.outcome.outcome, "complete");
  assert.equal(result.turns, 4);
  assert.match(runs.prompts[3]!, /Controller notes on your last outcome.*not the structured outcome JSON/);
  assert.match(runs.prompts[3]!, /Next task: AC1-t1/, "continuation uses the last known board");
  assert.deepEqual(warningsSeen.map(w => w.length > 0), [false, true, false]);
  // A dishonest complete is downgraded and the model is told what is open; the run does not fail.
  runs = fakeRuns([dishonest, good]);
  result = await runDeveloperTurns("start", runs.runTurn, { budget: new ModelExecutionBudget(3_600_000), maxTurns: 5 });
  assert.equal(result.turns, 2);
  assert.equal(result.outcome.outcome, "complete");
  assert.match(runs.prompts[1]!, /complete was claimed with open tasks \[AC1-c1\]/);
});

test("blocked ends the run immediately and exhausted allowance stops continuation with the board intact", async () => {
  const blocked = outcome({ outcome: "blocked", summary: "Needs a deployed worker", remainingWork: ["AC2-c1"],
    tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "done", ["AC1-c1"]), t("AC2-c1", "code", "blocked", [], "needs deployed target")] });
  let runs = fakeRuns([blocked]);
  let result = await runDeveloperTurns("start", runs.runTurn, { budget: new ModelExecutionBudget(3_600_000), maxTurns: 5 });
  assert.equal(result.outcome.outcome, "blocked");
  assert.equal(result.turns, 1);
  const checkpoint = outcome({ outcome: "checkpoint", remainingWork: ["AC1-t1"], tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])] });
  runs = fakeRuns([checkpoint, checkpoint]);
  let clock = 0;
  const budget = new ModelExecutionBudget(10_000, () => clock);
  result = await runDeveloperTurns("start", async (prompt, signal) => { clock += 6_000; return runs.runTurn(prompt, signal); }, { budget, maxTurns: 5, minTurnMs: 5_000 });
  assert.equal(result.turns, 1, "no continuation starts with less than the minimum model time left");
  assert.equal(result.exhausted, true);
  assert.equal(result.outcome.outcome, "checkpoint");
  runs = fakeRuns([checkpoint, checkpoint, checkpoint]);
  result = await runDeveloperTurns("start", runs.runTurn, { budget: new ModelExecutionBudget(3_600_000), maxTurns: 2 });
  assert.equal(result.turns, 2);
  assert.equal(result.exhausted, true);
});

test("continuation prompt names the next task and asks for its covering test when it is code", () => {
  const prompt = continuationPrompt({ outcome: "checkpoint", summary: "s", remainingWork: ["AC1-c2"], pullRequestUrl: "",
    tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "done", ["AC1-c1"]), t("AC1-c2", "code", "open"), t("AC1-t2", "test", "open", ["AC1-c2"])] });
  assert.match(prompt, /Next task: AC1-c2 \(code, AC1\): code AC1-c2\. Take it to done together with the test task that covers it/);
});

test("blocked acceptance tasks end the checkpoint loop without claiming completion", async () => {
  const tasks = [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "done", ["AC1-c1"]),
    t("AC1-live", "test", "blocked", ["AC1-c1"], "deployed-target: release verification owner")];
  const remainingWork = ["AC1-live: release verification owner must run the deployed checks"];
  const runs = fakeRuns([
    outcome({ outcome: "complete", tasks, remainingWork }),
    outcome({ outcome: "blocked", tasks, remainingWork, pullRequestUrl: "https://github.com/example/product/pull/1" }),
  ]);
  const result = await runDeveloperTurns("start", runs.runTurn, {
    budget: new ModelExecutionBudget(3_600_000), maxTurns: 4,
  });
  assert.equal(result.turns, 2);
  assert.equal(result.exhausted, false);
  assert.equal(result.outcome.outcome, "blocked");
  assert.deepEqual(result.outcome.tasks.map(task => task.status), ["done", "done", "blocked"]);
  assert.deepEqual(result.outcome.remainingWork, remainingWork);
  assert.equal(result.outcome.pullRequestUrl, "https://github.com/example/product/pull/1");
  assert.match(runs.prompts[1]!, /No runnable tasks remain; tasks AC1-live are blocked/);
  assert.match(runs.prompts[1]!, /return blocked with the concrete dependency and next owner\/action/);
  assert.doesNotMatch(runs.prompts[1]!, /Every task is done and covered|Run the full relevant suites now/);
});

test("blocked tasks do not stop other runnable work", () => {
  const prompt = continuationPrompt({ outcome: "checkpoint", summary: "s", remainingWork: ["AC1-live", "AC2-c1"], pullRequestUrl: "",
    tasks: [t("AC1-live", "test", "blocked", ["AC1-c1"], "deployed-target: verification owner"),
      t("AC1-c1", "code", "done"), t("AC1-t1", "test", "done", ["AC1-c1"]), t("AC2-c1", "code", "open")] });
  assert.match(prompt, /Next task: AC2-c1/);
  assert.doesNotMatch(prompt, /No runnable tasks remain/);
});

test("a process that comes online with an existing board resumes it instead of re-planning", () => {
  const note = resumeNote([t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])]);
  assert.match(note, /already exists from a previous process/);
  assert.match(note, /Do not re-plan/);
  assert.match(note, /resume at the first open task \(AC1-t1 \(test, AC1\): test AC1-t1\)/);
  assert.match(note, /- ☑ `code` AC1-c1/);
});

test("breakdown checks join the controller notes on the next turn and never end the run", async () => {
  const checkpoint = outcome({ outcome: "checkpoint", remainingWork: ["AC1-t1"], tasks: [t("AC1-c1", "code", "done"), t("AC1-t1", "test", "open", ["AC1-c1"])] });
  const good = outcome({ outcome: "complete" });
  const runs = fakeRuns([checkpoint, good]);
  const result = await runDeveloperTurns("start", runs.runTurn, { budget: new ModelExecutionBudget(3_600_000), maxTurns: 5,
    checkBoard: () => ["acceptance criterion AC2 has no tasks"] });
  assert.equal(result.outcome.outcome, "complete");
  assert.match(runs.prompts[1]!, /Controller notes on your last outcome.*acceptance criterion AC2 has no tasks/);
});

test("resumed developer turns retain refreshed checkpoint lineage through completion", async context => {
  const workspace = await mkdtemp(join(tmpdir(), "adp-resumed-turns-"));
  context.after(() => rm(workspace, { recursive: true, force: true }));
  const tasks = [t("AC1-c1", "code", "in_progress"), t("AC1-t1", "test", "open", ["AC1-c1"])];
  await writeTaskBoardFile(workspace, 42, tasks, new Date("2026-10-09T12:00:00Z"), { runId: "run-one", attempt: 1 });
  const previousTasks = (await readTaskBoardFile(workspace, 42)).tasks!;
  const runs = fakeRuns([
    outcome({ outcome: "checkpoint", tasks, remainingWork: ["AC1-c1", "AC1-t1"] }),
    outcome({ outcome: "complete", tasks: tasks.map(entry => ({ ...entry, status: "done" })) }),
  ]);
  let checkpointTasks = previousTasks;
  const result = await runDeveloperTurns("resume", runs.runTurn, {
    budget: new ModelExecutionBudget(3_600_000), maxTurns: 2, previousTasks,
    onOutcome: async (reported, turn) => {
      if (turn === 2) {
        assert.equal(reported.tasks[0]!.run_id, "run-two", "the retained loop board must be refreshed before the next turn");
        assert.equal(reported.tasks[0]!.attempt, 2);
        assert.equal(reported.tasks[0]!.started_at, "2026-10-09T12:10:00.000Z");
      }
      await writeTaskBoardFile(workspace, 42, reported.tasks, new Date(`2026-10-09T12:${turn}0:00Z`),
        { runId: "run-two", attempt: 2, previous: checkpointTasks, summary: reported.summary });
      checkpointTasks = (await readTaskBoardFile(workspace, 42)).tasks!;
      reported.tasks = checkpointTasks;
    },
  });
  assert.equal(result.turns, 2);
  assert.equal(result.outcome.outcome, "complete");
  const completed = (await readTaskBoardFile(workspace, 42)).tasks![0]!;
  assert.equal(completed.run_id, "run-two");
  assert.equal(completed.attempt, 2);
  assert.equal(completed.started_at, "2026-10-09T12:10:00.000Z");
  assert.equal(completed.finished_at, "2026-10-09T12:20:00.000Z");
  assert.deepEqual(result.outcome.tasks, checkpointTasks);
});

test("completion cannot drop an unfinished saved child or change its parent", () => {
  const planStep = { id: "recover", title: "Keep conversations after disconnects" };
  const previous = [t("A-c1", "code", "done"), t("A-t1", "test", "done", ["A-c1"]),
    t("A-t2", "test", "in_progress", ["A-c1"])].map(task => ({ ...task, planStep }));
  const result = interpretDeveloperOutcome(outcome({ outcome: "complete", tasks: previous.slice(0, 2) }), previous);
  assert.equal(result.outcome.outcome, "checkpoint");
  assert.deepEqual(result.outcome.tasks[2], previous[2]);
  assert.ok(result.outcome.remainingWork.includes("A-t2"));
  assert.match(resumeNote(result.outcome.tasks), /A-t2/);
});


test("resumed blocked boards recheck current amendments before continuing implementation", () => {
  const prompt = resumeNote([t("AC1-c1", "code", "blocked", [], "Old proxy contract")]);
  assert.match(prompt, /current issue amendments and merged dependencies/);
  assert.match(prompt, /resume implementation when their blocker is resolved/);
  assert.match(prompt, /Keep its ids and completed work/);
  assert.match(prompt, /AC1-c1/);
});
