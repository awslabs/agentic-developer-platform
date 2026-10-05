import test from "node:test";
import assert from "node:assert/strict";
import { continuationPrompt, developerOutcomeSchema, developerPrompt, interpretDeveloperOutcome, parseDeveloperOutcome, resumeNote, runDeveloperTurns, validateTask,
  type DeveloperOutcome, type DeveloperTask } from "./developer.js";
import { ModelExecutionBudget } from "./model-budget.js";
import type { Task } from "./task-board.js";

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
