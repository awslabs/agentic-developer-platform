/** Shared code/test task board for the developer and reviewer loops.
 *
 * One story is delivered as explicit tasks. `code` tasks change behavior, `test`
 * tasks prove a named `code` task, `infra` tasks (harness, CI, tooling) exist only
 * to unblock a named task. The board is what the live issue comment, the PR body
 * and the next process see, so "what is being worked on" is never re-derived from
 * a 10k-line diff. The runtime renders it; the model only reports it.
 */

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

export type TaskKind = "code" | "test" | "infra";
export type TaskStatus = "open" | "done" | "blocked";

export interface Task {
  id: string;
  kind: TaskKind;
  /** Acceptance criterion or requirement this task serves (e.g. DATA02). */
  criterion: string;
  title: string;
  status: TaskStatus;
  /** For `test`/`infra`: ids of the tasks this one proves or unblocks. */
  covers: string[];
  /** Primary files, for the live board and kind checks; may be empty early. */
  files: string[];
  /** Blocker or short evidence note. */
  note: string;
  /** Short SHAs the controller attributed to this task; never model-supplied. */
  commits?: string[];
}

export const TASK_KINDS: readonly TaskKind[] = ["code", "test", "infra"];
export const TASK_STATUSES: readonly TaskStatus[] = ["open", "done", "blocked"];
const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/** Strict schema fragment for Codex structured output (every key required). */
export const taskSchema = {
  type: "object", additionalProperties: false,
  properties: {
    id: { type: "string" },
    kind: { type: "string", enum: [...TASK_KINDS] },
    criterion: { type: "string" },
    title: { type: "string" },
    status: { type: "string", enum: [...TASK_STATUSES] },
    covers: { type: "array", items: { type: "string" } },
    files: { type: "array", items: { type: "string" } },
    note: { type: "string" },
  },
  required: ["id", "kind", "criterion", "title", "status", "covers", "files", "note"],
};

export const taskListSchema = { type: "array", items: taskSchema };

/** Validate a reported board. Dangling references and duplicate ids are model
 * errors, not something the controller should repair silently. */
export function validateTasks(raw: unknown): Task[] {
  if (!Array.isArray(raw)) throw new Error("Task board must be an array of tasks");
  const tasks = raw.map((item, index) => {
    if (!item || typeof item !== "object") throw new Error(`Task ${index} is not an object`);
    const task = item as Record<string, unknown>;
    if (typeof task.id !== "string" || !TASK_ID.test(task.id)) throw new Error(`Task ${index} has an invalid id`);
    if (!TASK_KINDS.includes(task.kind as TaskKind)) throw new Error(`Task ${task.id} has an invalid kind`);
    if (!TASK_STATUSES.includes(task.status as TaskStatus)) throw new Error(`Task ${task.id} has an invalid status`);
    if (typeof task.title !== "string" || !task.title.trim()) throw new Error(`Task ${task.id} needs a title`);
    const strings = (value: unknown, name: string) => {
      if (value === undefined) return [];
      if (!Array.isArray(value) || value.some(entry => typeof entry !== "string")) throw new Error(`Task ${task.id} ${name} must be strings`);
      return value as string[];
    };
    const commits = strings(task.commits, "commits").filter(sha => /^[0-9a-f]{7,40}$/.test(sha));
    return {
      id: task.id, kind: task.kind as TaskKind, status: task.status as TaskStatus, title: task.title.trim(),
      criterion: typeof task.criterion === "string" ? task.criterion.trim() : "",
      covers: strings(task.covers, "covers"), files: strings(task.files, "files"),
      note: typeof task.note === "string" ? task.note.trim() : "",
      ...(commits.length ? { commits } : {}),
    } satisfies Task;
  });
  const ids = new Set<string>();
  for (const task of tasks) {
    if (ids.has(task.id)) throw new Error(`Duplicate task id ${task.id}`);
    ids.add(task.id);
  }
  for (const task of tasks) {
    for (const ref of task.covers) if (!ids.has(ref)) throw new Error(`Task ${task.id} covers unknown task ${ref}`);
    if (task.kind === "test" && !task.covers.some(ref => tasks.find(t => t.id === ref)?.kind === "code")) {
      throw new Error(`Test task ${task.id} must cover at least one code task`);
    }
    if (task.kind === "infra" && task.covers.length === 0) throw new Error(`Infra task ${task.id} must name the task it unblocks`);
    if (task.status === "blocked" && !task.note) throw new Error(`Blocked task ${task.id} needs a note`);
  }
  return tasks;
}

/** Lenient reading of a model-reported board. Nothing here fails a run: every
 * defect is repaired to the nearest honest shape and reported as a warning the
 * controller feeds back on the next turn. Strict `validateTasks` is reserved for
 * data the controller wrote itself (the branch file). */
export function sanitizeTasks(raw: unknown): { tasks: Task[]; warnings: string[] } {
  const warnings: string[] = [];
  if (!Array.isArray(raw)) return { tasks: [], warnings: ["task board missing or not a list; keep returning the full board"] };
  const seen = new Set<string>();
  const tasks: Task[] = [];
  raw.forEach((item, index) => {
    if (!item || typeof item !== "object") { warnings.push(`task ${index} ignored: not an object`); return; }
    const t = item as Record<string, unknown>;
    let id = typeof t.id === "string" ? t.id.trim().replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+|[^A-Za-z0-9]+$/g, "").slice(0, 64) : "";
    if (!id) { id = `task-${index + 1}`; warnings.push(`task ${index} had no usable id; named ${id}`); }
    else if (id !== t.id) warnings.push(`task id "${String(t.id)}" normalised to ${id}`);
    while (seen.has(id)) { id = `${id}-dup`; warnings.push(`duplicate task id; renamed to ${id}`); }
    seen.add(id);
    const kind = TASK_KINDS.includes(t.kind as TaskKind) ? t.kind as TaskKind : (warnings.push(`task ${id}: unknown kind "${String(t.kind)}" treated as code`), "code" as TaskKind);
    const status = TASK_STATUSES.includes(t.status as TaskStatus) ? t.status as TaskStatus : (warnings.push(`task ${id}: unknown status "${String(t.status)}" treated as open`), "open" as TaskStatus);
    const strings = (value: unknown) => Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && v.trim() !== "").map(v => v.trim()) : [];
    const title = typeof t.title === "string" && t.title.trim() ? t.title.trim() : (warnings.push(`task ${id}: no title`), "(untitled)");
    let note = typeof t.note === "string" ? t.note.trim() : "";
    if (status === "blocked" && !note) { note = "(no reason given)"; warnings.push(`task ${id}: blocked without a reason`); }
    tasks.push({ id, kind, status, title, criterion: typeof t.criterion === "string" ? t.criterion.trim() : "",
      covers: strings(t.covers), files: strings(t.files), note });
  });
  const ids = new Set(tasks.map(task => task.id));
  for (const task of tasks) {
    const dangling = task.covers.filter(ref => !ids.has(ref));
    if (dangling.length) { warnings.push(`task ${task.id} covers unknown tasks ${dangling.join(", ")}; dropped`); task.covers = task.covers.filter(ref => ids.has(ref)); }
    if (task.kind === "test" && !task.covers.some(ref => tasks.find(x => x.id === ref)?.kind === "code")) warnings.push(`test task ${task.id} does not name the code task it proves`);
    if (task.kind === "infra" && !task.covers.length) warnings.push(`infra task ${task.id} does not name the task it unblocks`);
  }
  return { tasks, warnings };
}

/** A code task is only done when a done test covers it. Tests without code and
 * code without tests are both partial, never complete. */
export function uncoveredCode(tasks: Task[]): Task[] {
  return tasks.filter(task => task.kind === "code" && task.status === "done"
    && !tasks.some(test => test.kind === "test" && test.status === "done" && test.covers.includes(task.id)));
}

export function boardComplete(tasks: Task[]): boolean {
  return tasks.length > 0 && tasks.every(task => task.status === "done") && uncoveredCode(tasks).length === 0;
}

/** Next task to work: board order, code before the tests that prove it, and an
 * infra task only when the task it unblocks is itself next. */
export function nextTask(tasks: Task[]): Task | undefined {
  const open = tasks.filter(task => task.status === "open");
  const candidate = open.find(task => task.kind !== "infra");
  if (!candidate) return open[0];
  const blocker = open.find(task => task.kind === "infra" && task.covers.includes(candidate.id));
  return blocker ?? candidate;
}

export function describeTask(task: Task | undefined): string {
  return task ? `${task.id} (${task.kind}${task.criterion ? `, ${task.criterion}` : ""}): ${task.title}` : "none";
}

const BOARD_START = "<!-- adp-task-board:start -->";
const BOARD_END = "<!-- adp-task-board:end -->";
const BOARD_DATA = /<!-- adp-task-board-data:([A-Za-z0-9+/=]+) -->/;

/** Markdown board. Starts with `### Task checklist` so the existing issue
 * checklist reader (`GitHubClient.taskChecklists`) picks it up unchanged. */
export function renderTaskBoard(tasks: Task[], options: { working?: string; heading?: string; since?: string } = {}): string {
  // `working` is the id of the task in progress; its row gets ▶ and the summary
  // line names it, so a reader never has to match ids by eye.
  const working = tasks.find(task => task.id === options.working);
  const count = (kind: TaskKind) => {
    const all = tasks.filter(task => task.kind === kind);
    return `${kind} ${all.filter(task => task.status === "done").length}/${all.length}`;
  };
  const mark = (task: Task) => task.status === "done" ? "☑" : task.status === "blocked" ? "⛔" : task === working ? "▶" : "☐";
  const groups = new Map<string, Task[]>();
  for (const task of tasks) {
    const key = task.criterion || "general";
    groups.set(key, [...(groups.get(key) ?? []), task]);
  }
  // The live comment and UI add their own "Task checklist" heading (#6961), so
  // publishers pass heading "" and only standalone renderings carry one.
  const heading = options.heading ?? "### Task checklist";
  const lines = [...(heading ? [heading, ""] : []),
    `${count("code")} · ${count("test")} · ${count("infra")}${working ? ` · ▶ now working: ${describeTask(working)}${options.since ? ` (since ${options.since})` : ""}` : options.working ? ` · now working: ${options.working}` : ""}`, ""];
  for (const [criterion, group] of groups) {
    lines.push(`**${criterion}**`);
    for (const task of group) {
      const covers = task.covers.length ? ` (covers ${task.covers.join(", ")})` : "";
      const note = task.note ? ` — ${task.note}` : "";
      const commits = task.commits?.length ? ` · ${task.commits.map(sha => `\`${sha.slice(0, 7)}\``).join(" ")}` : "";
      lines.push(`- ${mark(task)} \`${task.kind}\` ${task.id} — ${task.title}${covers}${note}${commits}`);
    }
    lines.push("");
  }
  const uncovered = uncoveredCode(tasks);
  if (uncovered.length) lines.push(`Code without a passing test: ${uncovered.map(task => task.id).join(", ")}`, "");
  return lines.join("\n").trimEnd();
}

/** PR body section the controller owns; the model's prose stays untouched.
 * Human-readable only — the machine-readable board lives in the branch file. */
export function upsertTaskBoardSection(body: string | null | undefined, tasks: Task[], options: { working?: string; status?: string; since?: string } = {}): string {
  const section = [BOARD_START, renderTaskBoard(tasks, { heading: "### Task board", working: options.working, since: options.since }),
    options.status ? `\n_${options.status}_` : "", BOARD_END].filter(Boolean).join("\n");
  const existing = body ?? "";
  const start = existing.indexOf(BOARD_START), end = existing.indexOf(BOARD_END);
  if (start >= 0 && end > start) return `${existing.slice(0, start)}${section}${existing.slice(end + BOARD_END.length)}`;
  return `${existing.trimEnd()}\n\n${section}\n`;
}

/** Legacy fallback: boards that PRs created before the branch file carried as a
 * PR-body data comment. New runs write the file and never read this again. */
export function parseTaskBoard(body: string | null | undefined): Task[] | null {
  const match = body?.match(BOARD_DATA);
  if (!match) return null;
  try { return validateTasks(JSON.parse(Buffer.from(match[1]!, "base64").toString("utf8"))); }
  catch { return null; }
}

const TEST_PATH = /(^|\/)(tests?|__tests__|spec|fixtures)\/|\.(test|spec)\.[cm]?[jt]sx?$|(^|\/)test_[^/]+\.py$|_test\.(py|go|rs)$|(^|\/)conftest\.py$/;

/** Soft check that the files touched match the kinds of tasks just completed.
 * Returns a warning, never a failure: the board must stay honest, not rigid. */
export function kindMismatch(completed: Task[], changedFiles: string[]): string | null {
  if (!completed.length || !changedFiles.length) return null;
  const kinds = new Set(completed.map(task => task.kind));
  const testFiles = changedFiles.filter(file => TEST_PATH.test(file));
  if (kinds.size === 1 && kinds.has("code") && testFiles.length === changedFiles.length) {
    return `Tasks ${completed.map(t => t.id).join(", ")} are code tasks but only test files changed`;
  }
  if (kinds.size === 1 && kinds.has("test") && testFiles.length === 0) {
    return `Tasks ${completed.map(t => t.id).join(", ")} are test tasks but no test files changed`;
  }
  return null;
}

/** Ids whose status moved to done between two boards. */
export function newlyDone(before: Task[] | null, after: Task[]): Task[] {
  const previous = new Map((before ?? []).map(task => [task.id, task.status]));
  return after.filter(task => task.status === "done" && previous.get(task.id) !== "done");
}

export interface CommitRecord { sha: string; subject: string }

/** Map the commits of one turn onto tasks. A subject naming task ids wins;
 * otherwise the commit belongs to every task that became done this turn. The
 * controller computes this from Git, so the mapping is evidence, not a claim.
 * Returns the enriched board and the commits that could not be attributed. */
export function attributeCommits(tasks: Task[], commits: CommitRecord[], completed: Task[]): { tasks: Task[]; unattributed: CommitRecord[] } {
  const owners = new Map<string, Set<string>>();
  const unattributed: CommitRecord[] = [];
  for (const commit of commits) {
    const named = tasks.filter(task => new RegExp(`(^|[^A-Za-z0-9._-])${task.id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![A-Za-z0-9._-])`).test(commit.subject));
    const targets = named.length ? named : completed;
    if (!targets.length) { unattributed.push(commit); continue; }
    for (const task of targets) owners.set(task.id, new Set([...(owners.get(task.id) ?? []), commit.sha.slice(0, 7)]));
  }
  return {
    tasks: tasks.map(task => {
      const added = owners.get(task.id);
      if (!added) return task;
      return { ...task, commits: [...new Set([...(task.commits ?? []), ...added])] };
    }),
    unattributed,
  };
}

/** Carry controller-owned commit attributions from the previous board onto a
 * freshly reported one (the model never sends them). */
export function carryCommits(previous: Task[] | null, tasks: Task[]): Task[] {
  if (!previous) return tasks;
  const known = new Map(previous.map(task => [task.id, task.commits]));
  return tasks.map(task => known.get(task.id)?.length ? { ...task, commits: known.get(task.id) } : task);
}

/** The board's home: tracked in the story branch, written only by the controller,
 * read by whichever process comes online next (developer restart or reviewer). */
export function taskBoardPath(issue: number): string {
  return `.adp/tasks/${issue}.json`;
}

export interface TaskBoardFile { version: 1; issue: number; updated_at: string; tasks: Task[] }

/** Read and validate the branch board. Unreadable or invalid content is reported
 * as null, never trusted: the model has shell access to this file. */
export async function readTaskBoardFile(workspace: string, issue: number): Promise<{ tasks: Task[] | null; error?: string }> {
  let raw: string;
  try { raw = await readFile(join(workspace, taskBoardPath(issue)), "utf8"); }
  catch (error) { return (error as NodeJS.ErrnoException).code === "ENOENT" ? { tasks: null } : { tasks: null, error: String(error) }; }
  try {
    const data = JSON.parse(raw) as Partial<TaskBoardFile>;
    if (data.version !== 1 || data.issue !== issue) return { tasks: null, error: "task board file is for another issue or version" };
    return { tasks: validateTasks(data.tasks) };
  } catch (error) { return { tasks: null, error: (error as Error).message }; }
}

/** Stable, pretty-printed JSON so each turn's diff is only what changed. */
export async function writeTaskBoardFile(workspace: string, issue: number, tasks: Task[], now = new Date()): Promise<string> {
  const path = taskBoardPath(issue);
  await mkdir(join(workspace, ".adp", "tasks"), { recursive: true });
  const ordered = tasks.map(task => ({ id: task.id, kind: task.kind, criterion: task.criterion, title: task.title, status: task.status,
    covers: task.covers, files: task.files, note: task.note, ...(task.commits?.length ? { commits: task.commits } : {}) }));
  const file: TaskBoardFile = { version: 1, issue, updated_at: now.toISOString(), tasks: ordered };
  await writeFile(join(workspace, path), `${JSON.stringify(file, null, 2)}\n`, "utf8");
  return path;
}

/** Controller board commits are bookkeeping; they never count as task work. */
export function isBoardCommit(subject: string): boolean {
  return /^chore\(#\d+\): task board\b/.test(subject);
}

/** Acceptance IDs the issue defines: Validation-table rows (`| AC-01 |`), bold
 * criterion markers (`**DATA01:**`) or list markers (`- **AC-01**`). Only the
 * shapes the issue template mandates; anything else is left to the model. */
export function acceptanceIds(issueBody: string | null | undefined): string[] {
  const ids = new Set<string>();
  for (const match of (issueBody ?? "").matchAll(/^\|\s*([A-Z][A-Z0-9]{1,}-?\d{1,3}[a-z]?)\s*\|/gm)) ids.add(match[1]!);
  for (const match of (issueBody ?? "").matchAll(/\*\*([A-Z][A-Z0-9]{1,}-?\d{1,3}[a-z]?)\b[^*\n]{0,40}\*\*/g)) ids.add(match[1]!);
  return [...ids].filter(id => !/^(ID|PR|CI|AC)$/.test(id));
}

/** Soft conformance of a board to the breakdown rule. Warnings only — the
 * controller feeds them back on the next turn and never fails a run on them. */
export function breakdownWarnings(tasks: Task[], ids: string[]): string[] {
  const warnings: string[] = [];
  const serves = (task: Task, id: string) => task.criterion === id || task.id.startsWith(`${id}-`) || task.id === id;
  for (const id of ids) {
    const mine = tasks.filter(task => serves(task, id));
    if (!mine.length) { warnings.push(`acceptance criterion ${id} has no tasks (needs at least ${id}-c1 code and ${id}-t1 test)`); continue; }
    if (!mine.some(task => task.kind === "code")) warnings.push(`acceptance criterion ${id} has no code task`);
    if (!mine.some(task => task.kind === "test")) warnings.push(`acceptance criterion ${id} has no test task proving it`);
  }
  if (ids.length) {
    const orphans = tasks.filter(task => task.kind !== "infra" && !ids.some(id => serves(task, id)));
    if (orphans.length) warnings.push(`tasks ${orphans.map(task => task.id).join(", ")} serve no acceptance criterion from the issue`);
  }
  for (const task of tasks) {
    if (task.kind === "code" && task.status === "done" && !tasks.some(test => test.kind === "test" && test.covers.includes(task.id))) {
      warnings.push(`code task ${task.id} has no test task covering it`);
    }
  }
  return warnings;
}

/** Oversize signal for the issue owner: surfaced, never enforced. */
export function sizeSignal(tasks: Task[]): string | null {
  const code = tasks.filter(task => task.kind === "code").length;
  const deployed = new Set(tasks.filter(task => /^deployed-target/i.test(task.note)).map(task => task.criterion || task.id)).size;
  const reasons: string[] = [];
  if (code > 12) reasons.push(`${code} code tasks`);
  if (deployed > 2) reasons.push(`${deployed} criteria need a deployed target`);
  return reasons.length ? `Story size signal: ${reasons.join(" and ")} — consider splitting into child issues (one per criterion group) before spending more budget.` : null;
}
