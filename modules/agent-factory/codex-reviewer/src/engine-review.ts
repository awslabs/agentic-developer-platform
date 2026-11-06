import { DEVELOPMENT_TIMEOUT_MS } from "./timeouts.js";
import { parseReviewClosure, reconcileReviewedTasks, reviewClosureReport, reviewClosureSchema, type ReviewClosure } from './closure-report.js';
import { reviewEvents, reviewSignal, reviewOperation } from "./review-observer.js";
/** One reviewer owns inspection, repairs, CI and deterministic merge delivery. */
import { loadSharedInstructions } from "./shared-instructions.js";
import { Codex } from "@openai/codex-sdk";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { setTimeout as pause } from "node:timers/promises";
import { parseVerdict, requiresChanges, reviewOutputSchema,
  type CodexEngineReviewEnvelope, type ReviewVerdict } from "./contracts.js";
import { GitHubClient, GitHubRequestError, formatReviewComment } from "./github.js";
import { authenticatedGit, childEnvironment, repositoryUrl, selectedModel,
  WORKER_SANDBOX_MODE, type ReviewRuntime } from "./reviewer.js";
import { ProcessError, run } from "./process.js";
import { runResumableTurn } from "./turn.js";
import { ModelExecutionBudget } from "./model-budget.js";
import { observeReviewerChecks, type ReviewerChecks } from "./reviewer-checks.js";
import { deliverEngineReview, type ReviewerMerge } from "./engine-delivery.js";
import { attributeCommits, carryCommits, checkpointIdentity, newlyDone, parseTaskBoard, preserveTaskHistory,
  readTaskBoardFile, renderTaskBoard, sanitizeTasks, taskBoardPath, taskListSchema, upsertTaskBoardSection,
  writeTaskBoardFile, type Task } from "./task-board.js";

export interface EngineVerdict extends ReviewVerdict {
  stages: { functional: "completed" | "failed"; security: "completed" | "failed" };
  stageDetails: string;
  closureReport?: ReviewClosure;
}

export const engineReviewSchema = {
  ...reviewOutputSchema,
  properties: {
    ...reviewOutputSchema.properties,
    summary: { type: "string", description: "Self-contained human GitHub review outcome: explain the resulting behavior and how it works, meaningful repairs, reproducible validation results, limitations and remaining work. Distinguish review approval from merge, deployment and live acceptance; the controller reports delivery separately." },
    stages: {
      type: "object", additionalProperties: false,
      properties: {
        functional: { type: "string", enum: ["completed", "failed"] },
        security: { type: "string", enum: ["completed", "failed"] },
      },
      required: ["functional", "security"],
    },
    stageDetails: { type: "string" },
    closureReport: reviewClosureSchema,
  },
  required: [...reviewOutputSchema.required, "stages", "stageDetails", "closureReport"],
};

export function parseEngineVerdict(raw: string): EngineVerdict {
  const verdict = parseVerdict(raw) as EngineVerdict;
  if (!verdict.stages || typeof verdict.stageDetails !== "string"
      || !["functional", "security"].every(name =>
        ["completed", "failed"].includes(verdict.stages[name as keyof EngineVerdict["stages"]]))) {
    throw new Error("Codex did not report functional and security review completion");
  }
  verdict.closureReport = parseReviewClosure(verdict.closureReport);
  return verdict;
}

function inspected(verdict: EngineVerdict): boolean {
  return Object.values(verdict.stages).every(value => value === "completed");
}

function complete(verdict: EngineVerdict): boolean {
  return !requiresChanges(verdict) && inspected(verdict);
}

/** Keep optional observations in the report, out of the repair assignment.
 * Unknown legacy/controller findings remain actionable rather than being waived. */
function repairFindings(findings: unknown[]): unknown[] {
  return findings.filter(finding => !(finding !== null && typeof finding === "object"
    && (("blocking" in finding && finding.blocking === false)
      || ("severity" in finding && finding.severity === "minor")
      || ("finding_id" in finding && /^validation-gap-\d+$/.test(String(finding.finding_id))))));
}

export interface RepairMilestone {
  outcome: 'checkpoint' | 'complete' | 'awaiting_ci' | 'blocked';
  summary: string;
  remainingWork: string[];
  /** Whole code/test task board after this milestone; empty when the model has none. */
  tasks?: Task[];
}

export const repairMilestoneSchema = {
  type: 'object', additionalProperties: false,
  properties: {
    outcome: { type: 'string', enum: ['checkpoint', 'complete', 'awaiting_ci', 'blocked'] },
    summary: { type: 'string', description: 'Human progress update: explain what changed, how it works, what was verified and the next useful action. For complete or blocked, give a self-contained outcome with limitations and remaining work; task IDs or a verdict alone are insufficient.' },
    remainingWork: { type: 'array', items: { type: 'string' } },
    tasks: taskListSchema,
  },
  required: ['outcome', 'summary', 'remainingWork', 'tasks'],
};

/** Lenient: a repair turn that did real work is never thrown away over the
 * shape of its final message. Malformed reporting is normalized and surfaced
 * in `warnings`; independent inspection decides approval. Only
 * non-JSON is rejected, and the caller treats that as "no milestone reported". */
export function parseRepairMilestone(raw: string, previous: Task[] | null = null): RepairMilestone & { warnings: string[] } {
  let data: unknown;
  try { data = JSON.parse(raw); } catch { throw new Error('Invalid repair milestone result: not JSON'); }
  const warnings: string[] = [];
  const d = (data && typeof data === 'object' && !Array.isArray(data) ? data : {}) as Record<string, unknown>;
  if (d !== data) warnings.push('milestone result was not an object');
  const outcome: RepairMilestone['outcome'] = ['checkpoint', 'complete', 'awaiting_ci', 'blocked'].includes(d.outcome as string)
    ? d.outcome as RepairMilestone['outcome'] : (warnings.push(`outcome "${String(d.outcome)}" treated as checkpoint`), 'checkpoint');
  const summary = typeof d.summary === 'string' && d.summary.trim() ? d.summary.trim() : (warnings.push('no summary given'), '(no summary)');
  const remainingWork = Array.isArray(d.remainingWork) ? (d.remainingWork as unknown[]).filter((v): v is string => typeof v === 'string' && v.trim() !== '').map(v => v.trim()) : [];
  const sanitized = sanitizeTasks(d.tasks ?? [], previous ?? []);
  warnings.push(...sanitized.warnings);
  const retained = preserveTaskHistory(previous, sanitized.tasks);
  warnings.push(...retained.warnings);
  const tasks = retained.tasks;
  // The board and remaining work are reporting metadata, not approval gates.
  // The independent inspection and current-head CI decide whether repairs remain.
  return { outcome, summary, remainingWork, tasks, warnings };
}

export interface EngineReviewServices {
  github: Pick<GitHubClient, "getPullRequest" | "getIssue" | "getBranch"> & Partial<Pick<GitHubClient, "taskChecklists" | "updatePullRequestBody">>;
  review(prompt: string): Promise<EngineVerdict>;
  fix(prompt: string, previous?: Task[] | null): Promise<RepairMilestone | void>;
  checks?(head: string): Promise<ReviewerChecks>;
  deliver?(result: EngineReviewResult, envelope: CodexEngineReviewEnvelope): Promise<ReviewerMerge>;
  wait?(milliseconds: number): Promise<unknown>;
  now?(): number;
  deliveryTimeoutMs?: number;
}

export type EngineReviewResult = Awaited<ReturnType<typeof runEngineReviewPass>>;

export async function observePublishedRepair(
  github: EngineReviewServices["github"], number: number, before: string, after: string,
  branch: string, wait: (milliseconds: number) => Promise<unknown> = pause,
) {
  // A successful push can precede the pull-request projection update. Retry
  // only that exact old head; another revision or branch is a real conflict.
  for (const delay of [0, 1000, 2000, 4000, 8000, 16000]) {
    if (delay) await wait(delay);
    const current = await github.getPullRequest(number);
    if (current.state !== "open" || current.head.ref !== branch) {
      throw new Error("PR changed before review delivery");
    }
    if (current.head.sha === after) return;
    if (current.head.sha !== before) throw new Error("PR head changed before review delivery");
  }
  throw new Error("Published repair is not yet visible in the PR projection");
}

export function createReviewServices(runtime: ReviewRuntime & { repository: string }): EngineReviewServices {
  const instructions = loadSharedInstructions("reviewer", readFileSync(new URL("../prompts/reviewer.md", import.meta.url), "utf8"));
  const codex = new Codex({ baseUrl: runtime.proxyBaseUrl,
    apiKey: "sigv4-proxy-placeholder", config: { developer_instructions: instructions.text }, env: { ...childEnvironment(), ...(runtime.observer?.control ? { ADP_CODEX_CONTROL_SOCKET: runtime.observer.control.socket } : {}) } });
  // Retained turns share one execution allowance; CI polling does not consume it.
  const budget = new ModelExecutionBudget(Number(process.env.CODEX_REVIEWER_TURN_TIMEOUT_MS ?? DEVELOPMENT_TIMEOUT_MS));
  const thread = () => codex.startThread({ workingDirectory: runtime.workspace,
    model: selectedModel(), modelReasoningEffort: "high", sandboxMode: WORKER_SANDBOX_MODE,
    approvalPolicy: "never", networkAccessEnabled: false, webSearchMode: "disabled",
    threadSource: "adp-agent-codex-reviewer-engine" });
  // Retain inspection context across verification, but keep repair and review
  // conversations separate. A retry resumes its own thread and working tree.
  const inspection = thread();
  const repair = thread();
  const github = new GitHubClient(runtime.repository ?? "", runtime.getGitHubToken ?? (async () => {
      // The shared worker rotates this file; long reviews must not retain an expired token.
      try { return (await readFile(process.env.ADP_TOKEN_FILE ?? "/tmp/.adp-gh-token", "utf8")).trim() || runtime.githubToken; }
      catch { return runtime.githubToken; }
    }));
  return {
    github,
    checks: observeReviewerChecks,
    deliver: (result, envelope) => deliverEngineReview(github, result, envelope),
    review: async prompt => {
      runtime.observer?.explanation('Reviewing correctness and security, and running the relevant tests.');
      return budget.run(async signal => parseEngineVerdict((await runResumableTurn(inspection, prompt,
        { outputSchema: engineReviewSchema, signal: reviewSignal(signal, runtime.observer) }, undefined,
        instructions.verify, reviewEvents(runtime.observer, true, 'inspection'))).finalResponse));
    },
    fix: async (prompt, previous) => {
      runtime.observer?.explanation('Fixing the assigned current-PR defects and running relevant tests.');
      return budget.run(async signal => parseRepairMilestone((await runResumableTurn(repair, prompt,
        { outputSchema: repairMilestoneSchema, signal: reviewSignal(signal, runtime.observer) }, undefined,
        instructions.verify, reviewEvents(runtime.observer, true, 'inspection'))).finalResponse, previous));
    },
  };
}

export function engineReport(verdict: EngineVerdict) {
  const findings = verdict.findings.map(finding => ({
    finding_id: finding.id, stage: "functional",
    severity: finding.blocking ? "blocking" : "minor", disposition: "open",
    summary: `${finding.title}: ${finding.details} (${finding.file}${finding.line ? `:${finding.line}` : ""}). Repair: ${finding.recommendedFix}`,
  }));
  const details = [verdict.stageDetails, ...(verdict.validationGaps.length
    ? ['Evidence notes (non-blocking):', ...verdict.validationGaps] : [])].join('\n');
  return {
    verdict: complete(verdict) ? "approve" : findings.some(f => f.severity === "blocking")
      ? "request-changes" : "incomplete",
    stages: verdict.stages,
    stage_details: { functional: details, security: details },
    findings,
  };
}

export function engineReviewBody(verdict: EngineVerdict, head: string): string {
  const outcome = engineReport(verdict).verdict;
  const headline = outcome === "approve" ? "APPROVE" : outcome === "request-changes" ? "REQUEST CHANGES" : "INCOMPLETE";
  return formatReviewComment(verdict, head, "Codex SDK 0.155.1 · engine review")
    .replace(/^## agent-codex-reviewer — .*$/m, `## agent-codex-reviewer — ${headline}`)
    + `\nFunctional review: ${verdict.stages.functional}. Security review: ${verdict.stages.security}.\n${verdict.stageDetails}\n`;
}

async function runEngineReviewPass(
  envelope: CodexEngineReviewEnvelope,
  runtime: ReviewRuntime,
  supplied?: EngineReviewServices,
  ciEvidence?: ReviewerChecks,
) {
  const { cycle } = envelope;
  if (!runtime.workspace || !runtime.githubToken || !runtime.proxyBaseUrl) {
    throw new Error("Engine Codex review requires the shared worker runtime");
  }
  const controller = supplied ?? createReviewServices({ ...runtime, repository: envelope.repository });
  const localEnv = childEnvironment();
  const localRun: typeof run = (command, args, options = {}) => reviewOperation(runtime.observer,
    () => run(command, args, { ...options, signal: runtime.observer?.control?.signal }));
  const git = async (args: string[]) => (await localRun("git", args,
    { cwd: runtime.workspace, env: localEnv })).stdout.trim();
  const expected = cycle.head_sha;
  if (ciEvidence && (ciEvidence.head_sha !== expected || ciEvidence.state !== "passed")) {
    throw new Error("CI revalidation requires passing evidence for the assigned head");
  }
  const initialPr = await controller.github.getPullRequest(cycle.pr_number);
  // PR base.sha is a saved PR snapshot, not the current target branch. Merging
  // it can say "already up to date" while the real target still conflicts.
  const baseSha = (await controller.github.getBranch(initialPr.base.ref)).commit.sha;
  if (!/^[a-f0-9]{40}$/.test(baseSha)) throw new Error("Invalid current base revision");
  const merged = initialPr.state === "closed" && initialPr.merged === true;
  if (initialPr.head.sha !== expected || (initialPr.state !== "open" && !merged)
      || (merged && cycle.action === "repair")) throw new Error("Engine PR head or state changed before review");
  if (await git(["rev-parse", "HEAD"]) !== expected) throw new Error("Engine checkout does not match assigned head");
  const availableBase = await localRun("git", ["cat-file", "-e", `${baseSha}^{commit}`],
    { cwd: runtime.workspace, env: localEnv, allowFailure: true });
  if (availableBase.exitCode !== 0) {
    await authenticatedGit(runtime, ["fetch", "--no-tags", repositoryUrl(envelope.repository), baseSha]);
  }
  const branch = await git(["symbolic-ref", "--short", "HEAD"]);
  const config = await readFile(join(runtime.workspace, ".git", "config"), "utf8");
  const trackedDiff = () => git(["diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"]);
  if (await trackedDiff()) throw new Error("Engine review checkout has pre-existing tracked changes");
  const untracked = async () => (await git(["ls-files", "--others", "--exclude-standard", "-z"]))
    .split("\0").filter(Boolean);
  const baseline = new Set(await untracked());
  const issue = await controller.github.getIssue(envelope.issue_number);
  let priorTaskChecklists: string[] = [];
  try { priorTaskChecklists = await controller.github.taskChecklists?.(envelope.issue_number, issue.comments) ?? []; }
  catch { runtime.observer?.activity('Prior task checklist unavailable; reconstruct progress from the saved branch and acceptance criteria.'); }
  const persona = await readFile(new URL("../prompts/reviewer.md", import.meta.url), "utf8");
  // The developer's board (if it published one) is the handoff: open tasks are
  // the repair plan, so the reviewer starts from it instead of re-deriving one.
  const boardFile = await readTaskBoardFile(runtime.workspace, envelope.issue_number);
  if (boardFile.error) runtime.observer?.activity(`Task board file ${taskBoardPath(envelope.issue_number)} is unreadable; rebuilding from the story: ${boardFile.error}`);
  const boardBefore = boardFile.tasks ?? parseTaskBoard(initialPr.body);
  const story = JSON.stringify({ issue: { number: envelope.issue_number, title: issue.title, body: issue.body },
    pullRequest: { title: initialPr.title, body: initialPr.body }, acceptedScope: cycle.accepted_scope,
    priorFindings: repairFindings(cycle.findings), priorTaskChecklists, taskBoard: boardBefore ?? undefined });
  const recoveryContext = cycle.recovery
    ? "This is stalled-story recovery. The prior worker has exited. Preserve its committed work; do not restart from main or treat a checkpoint/PR as completed implementation. Read the current issue including owner clarifications. Finish the current PR's code-stage requirements and relevant tests. Historical evidence and deferred deployment/live acceptance are report notes, not repair tasks or merge blockers. Report a concrete unresolved decision only if it prevents an in-scope repair."
    : "";
  const validationContext = ciEvidence
    ? `\n\nThe controller has published this exact commit and observed its CI. Verify retained defects and any behavior affected by the new base, preserving prior inspection of unchanged code. Earlier statements that the repair is unpublished or CI is pending are historical. Verify that the named checks actually cover the required validation; an empty list or unrelated/skipped check is not proof of a required browser or other test. Keep unresolved concrete defects blocking. Missing evidence remains a non-blocking note; do not turn it into a new acceptance gate. Do not repeat successful unchanged local suites.\n<controller-ci-evidence>${JSON.stringify(ciEvidence).replaceAll("<", "\\u003c").replaceAll(">", "\\u003e")}</controller-ci-evidence>` : "";
  const context = `${recoveryContext}\n\n${persona}\n\nThis is a review, fix, test and merge assignment. The current PR's accepted code scope defines the work; no additional scope approval is required. Story acceptance, historical proof, deployment and live qualification are outside this merge decision. Your controller publishes and merges after verified review and CI; the engine completes the story. Do not publish, merge, dispatch another agent, or write review reports into the repository.\n\n<story-data>${story.replaceAll("<", "\\u003c").replaceAll(">", "\\u003e")}</story-data>${validationContext}`;
  const verifyGit = async (head: string) => {
    if (await git(["rev-parse", "HEAD"]) !== head
        || await git(["symbolic-ref", "--short", "HEAD"]) !== branch
        || await readFile(join(runtime.workspace, ".git", "config"), "utf8") !== config) {
      throw new Error("Codex changed protected Git state");
    }
  };
  const inspect = async (head: string, workingTree = false, changedFiles: string[] = []) => {
    const before = await trackedDiff();
    const beforeUntracked = new Set(await untracked());
    const pendingFiles = [...beforeUntracked].filter(file => !baseline.has(file));
    const fingerprint = async () => Promise.all(pendingFiles.map(async file =>
      createHash("sha256").update(await readFile(join(runtime.workspace, file))).digest("hex")));
    const beforeFiles = await fingerprint();
    const scope = changedFiles.length ? ` Files changed by this repair batch: ${changedFiles.slice(0, 60).join(", ")}${changedFiles.length > 60 ? ` (+${changedFiles.length - 60} more)` : ""}.` : "";
    const verification = workingTree || ciEvidence
      ? "Verify the assigned blockers, repairs and affected behavior. Preserve completed inspection of unchanged code; do not reopen it for optional improvements. Further repair is only for an unresolved blocker, a concrete regression or a failed required test/check."
      : "Inspect correctness and security within the current PR's accepted scope and run relevant tests.";
    const verdict = await controller.review(`${context}\n\nReview ${workingTree ? "the repaired working tree" : `exact commit ${head}`} against base commit ${baseSha}.${scope} ${verification} Only concrete current-PR defects belong in blocking findings. Missing evidence, unfinished board entries and repair outcome labels are reporting context, never independent blockers. Record those limits in validationGaps or the closure report without claiming they passed. Explicitly report whether both inspections completed. Return the structured verdict. Do not modify source files, stage, commit or run GitHub commands.`);
    await verifyGit(head);
    if (await trackedDiff() !== before) throw new Error("Read-only Codex review modified tracked files");
    if (JSON.stringify(await fingerprint()) !== JSON.stringify(beforeFiles)) {
      throw new Error("Read-only Codex review modified new repair files");
    }
    // Test output and review notes created during inspection are never repair files.
    for (const file of await untracked()) if (!beforeUntracked.has(file)) baseline.add(file);
    return verdict;
  };
  // GitHub may return null while calculating mergeability. An assigned base
  // repair must not disappear because that projection is temporarily unknown.
  const assignedBaseRepair = cycle.findings.some(finding =>
    finding !== null && typeof finding === "object" && "source" in finding
    && finding.source === "merge-controller" && "summary" in finding
    && /merge conflict|out-of-date base/i.test(String(finding.summary)));
  // Divergence alone does not require a source change. Inspect the published
  // head against the current base; canonical checks below require integration
  // when branch policy is strict or a real conflict appears.
  const conflict = initialPr.mergeable === false || initialPr.mergeable_state === "dirty"
    || assignedBaseRepair || (cycle.action === "repair" && initialPr.mergeable !== true);
  const assignedRepair = conflict || (cycle.action === "repair" && repairFindings(cycle.findings).length > 0);
  // Repair assignments already carry findings. Reviewing the unchanged head
  // first can approve it and silently skip the actual repair (notably conflicts).
  const original = assignedRepair && cycle.allow_story_repairs && !merged && !ciEvidence ? null : await inspect(expected);
  let verdict = original;
  let head = expected;
  let mergeBase: string | null = null;
  let milestone: RepairMilestone | void = undefined;
  let board: Task[] | null = boardBefore;
  const publishBoard = (tasks: Task[]) => {
    const text = renderTaskBoard(tasks, { heading: "" });
    if (runtime.observer?.progress) runtime.observer.progress(text, { id: "adp-task-board", category: "plan", state: "running", plan_scope: "assignment" });
    else runtime.observer?.activity(text);
  };
  if ((!verdict || (inspected(verdict) && requiresChanges(verdict))) && cycle.allow_story_repairs && !merged && !ciEvidence) {
    if (initialPr.head.repo?.full_name?.toLowerCase() !== envelope.repository.toLowerCase()) {
      throw new Error("Engine story repairs require the bound repository branch");
    }
    if (conflict) {
      // The controller prepares the exact base merge. The model only resolves
      // files; HEAD/branch/config and the publication lease remain fenced.
      await authenticatedGit(runtime, ["fetch", "--no-tags", repositoryUrl(envelope.repository), baseSha]);
      const merge = await localRun("git", ["-c", "core.hooksPath=/dev/null", "merge", "--no-commit", "--no-ff", baseSha],
        { cwd: runtime.workspace, env: localEnv, allowFailure: true });
      if (merge.exitCode && !await git(["ls-files", "--unmerged"])) throw new Error("Could not prepare the assigned base merge");
      const pendingMerge = await localRun("git", ["rev-parse", "--verify", "MERGE_HEAD"],
        { cwd: runtime.workspace, env: localEnv, allowFailure: true });
      if (pendingMerge.exitCode === 0) mergeBase = baseSha;
    }
    // One repair batch, then independent verification. A checkpoint label or an
    // unfinished checklist cannot schedule another repair without a real finding.
    {
      const taskHint = board ? ` Existing task board (reporting context only; preserve unfinished entries honestly):\n${renderTaskBoard(board)}` : "";
      const findings = verdict ? verdict.findings.filter(finding => finding.blocking) : repairFindings(cycle.findings);
      const prompt = `${context}\n\nFix the assigned concrete defects in one bounded batch and run the relevant tests.${taskHint} Do not work the board's first open task or collect missing evidence merely because it is listed. Resolve the assigned defects using the current PR's accepted scope. ${conflict ? `The controller prepared a merge of base ${baseSha}; resolve every conflict while preserving the story and current base behavior.` : ""} You own the repair; do not hand it to a developer or ask for another scope approval. Return complete when the batch is finished, awaiting_ci when it needs publication and CI, or checkpoint/blocked with the specific unresolved defect. The next inspection verifies the fixes and affected behavior; your outcome, board and remainingWork do not determine approval. Do not commit, push, merge, alter Git configuration, call GitHub or write review reports.\n\n<findings-data>${JSON.stringify(findings).replaceAll("<", "\\u003c").replaceAll(">", "\\u003e")}</findings-data>`;
      milestone = await controller.fix(prompt, board);
      await verifyGit(expected);
      if (milestone) {
        for (const warning of ("warnings" in milestone && Array.isArray(milestone.warnings) ? milestone.warnings : [])) runtime.observer?.activity(`Repair report: ${warning}.`);
        if (milestone.tasks?.length) {
          board = preserveTaskHistory(board, carryCommits(board, milestone.tasks)).tasks;
          publishBoard(board);
        }
      }
      if (conflict) {
        if (mergeBase && await git(["rev-parse", "MERGE_HEAD"]) !== mergeBase) throw new Error("Codex changed the protected merge base");
        await git(["add", "--update", "--", "."]);
        if (await git(["ls-files", "--unmerged"])) throw new Error("Base merge still has unresolved files");
      }
      // Validate before paying for another inspection. Give the same repair
      // thread one chance to fix concrete Git diagnostics, within its existing
      // model allowance, then inspect the final tree before publication.
      for (let attempt = 0; ; attempt++) {
        await verifyGit(expected);
        if (mergeBase && await git(["rev-parse", "MERGE_HEAD"]) !== mergeBase) {
          throw new Error("Codex changed the protected merge base");
        }
        await git(["add", "--update", "--", "."]);
        const repairFiles = (await untracked()).filter(file => !baseline.has(file));
        if (repairFiles.length) await git(["add", "--", ...repairFiles]);
        const args = ["diff", "--cached", "--check", baseSha];
        const check = await localRun("git", args, { cwd: runtime.workspace, env: localEnv, allowFailure: true });
        if (check.exitCode === 0) break;
        if (attempt >= 1 || check.exitCode !== 2) throw new ProcessError("git", args, check, null);
        const diagnostics = JSON.stringify({ stdout: check.stdout.slice(0, 8192), stderr: check.stderr.slice(0, 8192) })
          .replaceAll("<", "\\u003c").replaceAll(">", "\\u003e");
        await controller.fix(`${context}\n\nThe repaired tree failed Git validation before publication. Fix only the reported whitespace errors or conflict markers while preserving story behavior. Do not commit, push, merge, change Git configuration, disable checks, or write review reports. The controller will stage, recheck and re-review your changes.\n\n<git-validation-data>${diagnostics}</git-validation-data>`);
      }
      const batchFiles = [...new Set([...(await git(["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD"]))
        .split("\0").filter(Boolean), ...(await untracked()).filter(file => !baseline.has(file))])];
      verdict = await inspect(expected, true, batchFiles);
      if (milestone) {
        verdict = { ...verdict, validationGaps: [...verdict.validationGaps,
          ...milestone.remainingWork.map(item => `Repair report (unverified): ${item}`)] };
      }
    }
    const changed = (await git(["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD"]))
      .split("\0").filter(Boolean);
    const newFiles = (await untracked()).filter(file => !baseline.has(file));
    const files = [...new Set([...changed, ...newFiles])];
    // A repaired PR must be published before remote CI can validate it. Review
    // completion permits publishing progress; only complete() permits approval.
    // Retain remaining findings against the exact commit sent to the provider.
    if (inspected(verdict) && (files.length || mergeBase)) {
      const current = await controller.github.getPullRequest(cycle.pr_number);
      if (current.state !== "open" || current.head.sha !== expected || current.head.ref !== initialPr.head.ref) {
        throw new Error("PR changed during Codex story repair");
      }
      // The branch board rides in the batch commit so the next process resumes
      // from it. This batch's own SHA is recorded at the next write (and in the
      // commit subject below); a file cannot name the commit that contains it.
      if (board) await git(["add", "--", await writeTaskBoardFile(runtime.workspace, envelope.issue_number, board, new Date(), {
        ...checkpointIdentity(process.env), runId: envelope.message_id, summary: milestone?.summary ?? verdict.summary,
      })]);
      // A deleted tracked file can now match .gitignore. Updating the index
      // handles that deletion without trying to add the ignored path anew.
      await git(["add", "--update", "--", "."]);
      if (newFiles.length) await git(["add", "--", ...newFiles]);
      // A prepared base merge can include existing whitespace in unrelated
      // upstream fixtures. Check the reviewed PR delta, not everything added
      // since the stale feature head; keep rejecting new repair whitespace.
      await git(["diff", "--cached", "--check", baseSha]);
      const reviewedTree = await git(["write-tree"]);
      if (mergeBase && await git(["rev-parse", "MERGE_HEAD"]) !== mergeBase) throw new Error("Protected merge base changed before commit");
      // Name the finished tasks so the history reads as a task list, not N
      // identical "address story" commits.
      const finished = board ? newlyDone(boardBefore, board).map(task => task.id) : [];
      await git(["-c", "core.hooksPath=/dev/null", "commit", "-m", finished.length
        ? `fix(review #${envelope.issue_number}): ${finished.join(", ")}`
        : `fix(review): address story #${envelope.issue_number}`]);
      head = await git(["rev-parse", "HEAD"]);
      if (board) board = attributeCommits(board, [{ sha: head, subject: await git(["log", "-1", "--format=%s"]) }],
        board.filter(task => finished.includes(task.id))).tasks;
      if (await git(["rev-parse", "HEAD^"]) !== expected) throw new Error("Repair does not descend directly from assigned head");
      if (mergeBase && await git(["rev-parse", "HEAD^2"]) !== mergeBase) throw new Error("Repair did not retain the assigned merge base");
      // The functional/security verdict already covers this exact repaired tree.
      // A commit adds identity, not code; verify that identity without spending
      // another full model review on unchanged content.
      if (await git(["rev-parse", "HEAD^{tree}"]) !== reviewedTree || await trackedDiff()) {
        throw new Error("Committed repair differs from the inspected tree; nothing pushed");
      }
      await authenticatedGit(runtime, ["push", `--force-with-lease=refs/heads/${initialPr.head.ref}:${expected}`,
        repositoryUrl(envelope.repository), `HEAD:refs/heads/${initialPr.head.ref}`]);
    } else {
      // An unpublished repaired tree is not evidence about the remote commit.
      if (files.length || mergeBase) {
        if (!original) throw new Error("Assigned repair inspection did not complete; nothing pushed");
        verdict = original;
      }
    }
  }
  if (head !== expected) {
    await observePublishedRepair(controller.github, cycle.pr_number, expected, head, initialPr.head.ref);
    runtime.observer?.explanation(`Published inspected repair: https://github.com/${envelope.repository}/commit/${head}. ${milestone?.summary ?? 'The repairs are now on the PR branch.'} Checking required CI and merge eligibility.`);
    if (board && controller.github.updatePullRequestBody) {
      // Keep the PR description's board current so the next process (or a
      // human) reads real state, not the developer's first draft.
      const status = verdict && complete(verdict)
        ? "Reviewer: code inspection passed; checking required CI and merge. Unfinished story tasks remain listed."
        : "Reviewer: concrete PR blockers or incomplete inspection remain; see review findings.";
      try {
        const current = await controller.github.getPullRequest(cycle.pr_number);
        await controller.github.updatePullRequestBody(cycle.pr_number, upsertTaskBoardSection(current.body, board,
          { status }));
      } catch (error) { runtime.observer?.activity(`PR task board not updated: ${error instanceof Error ? error.message : String(error)}`); }
    }
  } else {
    const current = await controller.github.getPullRequest(cycle.pr_number);
    if (current.head.sha !== head) throw new Error("PR head changed before review delivery");
  }
  if (!verdict) throw new Error("Engine review produced no inspection");
  // CI evidence remains bound to its observed base if main moved during this
  // inspection. The delivery loop must repair/revalidate that change next.
  return { status: "engine_reviewed", sha: head, merged, reviewed_base_sha: ciEvidence?.base_sha ?? baseSha,
    repair_base_sha: head !== expected ? expected : null,
    checkpoint_remaining: [] as string[], // Compatibility field; board metadata never schedules repairs.
    ...(milestone?.outcome === "awaiting_ci" ? { awaiting_ci: true } : {}),
    ...(milestone?.outcome === "blocked" && !complete(verdict) ? { repair_blocked: milestone.summary } : {}),
    ...(board ? { task_board: board } : {}),
    closure_report: parseReviewClosure(verdict.closureReport),
    summary: verdict.summary,
    report: engineReport(verdict),
    body: engineReviewBody(verdict, head),
  };
}

/** Retain both SDK threads through CI and merge. Polling makes no model calls. */
export async function runEngineReview(
  envelope: CodexEngineReviewEnvelope, runtime: ReviewRuntime, supplied?: EngineReviewServices,
) {
  const controller = supplied ?? createReviewServices({ ...runtime, repository: envelope.repository });
  const result = await runEngineReviewLoop(envelope, runtime, controller);
  // Reporting happens after the delivery decision. No metadata-only commit,
  // new CI cycle, or reporting exception can undo a verified merge.
  try {
    const board = result.merged && result.report.verdict === 'approve'
      ? reconcileReviewedTasks(result.task_board ?? [], result.closure_report, result.sha)
      : result.task_board ?? [];
    const report = reviewClosureReport({ summary: result.summary, tasks: board, closure: result.closure_report,
      merged: result.merged, sha: result.sha, blocker: 'delivery_blocked' in result ? result.delivery_blocked : undefined });
    if (result.merged && board.length) {
      const attempt = async (name: string, work: () => unknown | Promise<unknown>) => {
        try { await work(); } catch { report.reporting_notes.push(`${name} could not be updated. The verified merge is unaffected.`); }
      };
      await attempt('Saved task file', () => writeTaskBoardFile(runtime.workspace, envelope.issue_number, board, new Date(), {
        ...checkpointIdentity(process.env), runId: envelope.message_id, summary: result.summary,
      }));
      await attempt('Saved checklist', () => runtime.observer?.progress?.(renderTaskBoard(board, { heading: '' }),
        { id: 'adp-task-board', category: 'plan', state: 'completed', plan_scope: 'assignment' }));
      if (controller.github.updatePullRequestBody) await attempt('PR checklist', () => reviewOperation(runtime.observer, async () => {
        const pr = await controller.github.getPullRequest(envelope.cycle.pr_number);
        if (pr.head.sha !== result.sha) throw new Error('PR head changed');
        await controller.github.updatePullRequestBody!(envelope.cycle.pr_number,
          upsertTaskBoardSection(pr.body, board, { status: `PR merged; final review verified at ${result.sha}. Deferred work remains listed.` }));
      }));
    }
    runtime.observer?.closure?.(report);
  } catch {
    // Best effort even if a reporter itself is unavailable. Preserve the result.
    try { runtime.observer?.activity('Closure reporting could not be completed; the review and merge result is unchanged.'); } catch { /* reporting only */ }
  }
  return result;
}

async function runEngineReviewLoop(
  envelope: CodexEngineReviewEnvelope, runtime: ReviewRuntime, supplied?: EngineReviewServices,
) {
  const controller = supplied ?? createReviewServices({ ...runtime, repository: envelope.repository });
  let result = await runEngineReviewPass(envelope, runtime, controller);
  const root = envelope.cycle.head_sha;
  const finish = () => ({ ...result, repair_base_sha: result.sha === root ? null : root });
  if (!envelope.cycle.reviewer_owned_delivery) {
    // Legacy assignments leave CI/merge delivery with the engine. Continue only
    // for concrete unresolved findings, within the same bounded repair allowance.
    for (let retry = 0; retry < 3 && envelope.cycle.allow_story_repairs
        && result.report.verdict === "request-changes" && !result.merged && !result.repair_blocked
        && Object.values(result.report.stages).every(stage => stage === "completed"); retry++) {
      const previous = result;
      result = await runEngineReviewPass({ ...envelope, cycle: { ...envelope.cycle,
        head_sha: result.sha, action: "repair", findings: repairFindings(result.report.findings),
      } }, runtime, controller);
      if (result.sha === previous.sha) break;
    }
    return finish();
  }
  if (result.merged) return result;
  if (!controller.checks || !controller.deliver) throw new Error("Reviewer-owned delivery requires checks and deterministic merge delivery");
  const now = controller.now ?? Date.now;
  const timeout = controller.deliveryTimeoutMs ?? DEVELOPMENT_TIMEOUT_MS;
  if (!Number.isFinite(timeout) || timeout <= 0) throw new Error("Invalid delivery timeout");
  let deadline = now() + timeout;
  const wait = async (milliseconds: number) => {
    runtime.observer?.explanation('Waiting for CI or merge status; the reviewer will continue automatically.');
    const duration = Math.max(0, Math.min(milliseconds, deadline - now()));
    if (controller.wait) await controller.wait(duration);
    else await pause(duration, undefined, { signal: runtime.observer?.control?.signal });
  };
  const deliver = () => reviewOperation(runtime.observer, () => {
    runtime.observer?.explanation('Publishing the review and checking merge delivery.');
    return controller.deliver!(finish(), envelope);
  });
  let lastDeliveryError = "";
  // Keep the inspected head and threads through temporary outages. Every retry
  // reauthorizes at the gateway, and the existing delivery deadline still bounds waiting.
  const retryDelivery = async (error: unknown, operation: string): Promise<boolean> => {
    runtime.observer?.control?.signal.throwIfAborted();
    const code = error instanceof Error && "code" in error && typeof error.code === "string"
      && /^[a-z][a-z0-9_]{0,79}$/.test(error.code) ? error.code
      : error instanceof GitHubRequestError ? `github_http_${error.status}`
      : error instanceof ProcessError ? "reviewer_bridge_failed" : "delivery_operation_failed";
    lastDeliveryError = `${operation}: ${code}`;
    const temporary = (error instanceof Error && "retryable" in error && error.retryable === true)
      || (error instanceof GitHubRequestError && (error.status === 429 || error.status >= 500));
    if (!temporary) return false;
    runtime.observer?.explanation(`${lastDeliveryError}; retaining this review and waiting to retry.`);
    await wait(60000);
    return true;
  };
  let queued = false;
  while (true) {
    await reviewOperation(runtime.observer, async () => {});
    if (result.repair_blocked) return { ...finish(), delivery_blocked: result.repair_blocked };
    if (now() >= deadline) return { ...finish(), delivery_blocked: "Delivery deadline exceeded while waiting for CI or merge; inspected head retained for recovery" + (lastDeliveryError ? ` (${lastDeliveryError})` : "") };
    if (Object.values(result.report.stages).some(stage => stage !== "completed")) return finish();
    let checks: ReviewerChecks;
    try {
      checks = await controller.checks(result.sha);
    } catch (error) {
      if (await retryDelivery(error, "CI observation blocked")) continue;
      return { ...finish(), delivery_blocked: lastDeliveryError };
    }
    if (checks.head_sha !== result.sha) return { ...finish(), delivery_blocked: "PR head changed while waiting for checks" };
    if (!checks.open && !checks.merged) return { ...finish(), delivery_blocked: "PR closed while waiting for checks" };
    if (checks.merged) {
      try {
        const delivery = await deliver();
        if (delivery.state === "merged") return { ...finish(), merged: true };
        return { ...finish(), delivery_blocked: "Merged PR delivery could not be verified" };
      } catch (error) {
        if (await retryDelivery(error, "Merge verification blocked")) continue;
        return { ...finish(), delivery_blocked: lastDeliveryError };
      }
    }
    if (queued) {
      try {
        const delivery = await deliver();
        if (delivery.state === "merged") return { ...finish(), merged: true };
        if (delivery.state === "blocked") return { ...finish(), delivery_blocked: delivery.reason ?? "Merge queue blocked" };
        if (delivery.queued) { await wait(60000); continue; }
        queued = false; // Queue removed the entry; current CI/base may need repair.
      } catch (error) {
        if (await retryDelivery(error, "Merge queue observation blocked")) continue;
        return { ...finish(), delivery_blocked: lastDeliveryError };
      }
    }
    // Moving main alone does not require a source edit. The canonical observer
    // separately requires integration for conflicts or strict behind checks.
    // Reinspect a clean new base against the same published head once CI passes.
    const baseChanged = checks.base_sha !== result.reviewed_base_sha;
    // The canonical observer owns pending/missing required checks. Evidence
    // notes and repair labels cannot invent an additional CI discovery gate.
    if (checks.state === "pending" && !checks.base_repair_required && result.report.verdict === "approve") {
      // No model call and no terminal receipt while applicable CI is running.
      await wait(60000);
      continue;
    }
    if (checks.state === "passed" && !checks.base_repair_required && baseChanged) {
      // A changed base can change behavior. Revalidate that integration once,
      // retaining the inspection of unchanged code. CI success alone needs no turn.
      const validationStarted = now();
      const previous = result;
      result = await runEngineReviewPass({ ...envelope, cycle: { ...envelope.cycle,
        head_sha: result.sha, action: "review", findings: result.report.findings,
      } }, runtime, controller, checks);
      deadline += Math.max(0, now() - validationStarted);
      result = { ...result, repair_base_sha: previous.repair_base_sha };
      continue; // Reobserve head/base and CI after the model call, before merge.
    }
    if (checks.state === "passed" && !checks.base_repair_required && result.report.verdict === "approve") {
      let delivery: ReviewerMerge;
      try { delivery = await deliver(); }
      catch (error) {
        if (await retryDelivery(error, "Review publication or merge blocked")) continue;
        return { ...finish(), delivery_blocked: lastDeliveryError };
      }
      if (delivery.state === "merged") return { ...finish(), merged: true };
      if (delivery.state === "blocked") return { ...finish(), delivery_blocked: delivery.reason ?? "Merge blocked" };
      if (delivery.state === "pending") { queued = delivery.queued === true; await wait(60000); continue; }
      checks = { ...checks, base_repair_required: true, failures: [delivery.reason] };
    }
    const previous = result;
    // Remaining findings are a repair assignment, not another read-only review.
    // Required CI failures and conflicts join concrete defects in the repair batch.
    const needsRepair = result.report.verdict !== "approve" || checks.state === "failed"
      || checks.base_repair_required;
    const findings = [...repairFindings(result.report.findings), {
      source: checks.base_repair_required ? "merge-controller" : "required-checks",
      summary: checks.base_repair_required ? "Repair merge conflict or out-of-date base" : "Canonical CI observation for this exact head",
      evidence: checks,
    }];
    if (envelope.cycle.allow_story_repairs) {
      // Useful repairs share the existing model/time/spend allowance. Stop on
      // no progress below, rather than imposing a second three-turn cutoff.
      const repairStarted = now();
      result = await runEngineReviewPass({ ...envelope, cycle: { ...envelope.cycle,
        head_sha: result.sha, action: needsRepair ? "repair" : "review", findings,
      } }, runtime, controller);
      // Repair work has its own shared model deadline. Preserve time already
      // spent waiting for CI without charging implementation against it too.
      deadline += Math.max(0, now() - repairStarted);
    }
    if (result.sha === previous.sha) {
      // Running missing validation can resolve a finding without a code change.
      // Reobserve CI and merge eligibility once approval has actually improved.
      if (previous.report.verdict !== "approve" && result.report.verdict === "approve"
          && !result.repair_blocked && checks.state !== "failed" && !checks.base_repair_required) continue;
      // A genuine external/no-progress blocker must remain visible. Do not spend
      // another model turn or accidentally approve code with failing checks.
      if (checks.state === "failed" || checks.base_repair_required) {
        result = { ...result, report: { ...result.report, verdict: "request-changes",
          findings: [...result.report.findings, { finding_id: "delivery-blocked", stage: "functional",
            severity: "blocking", disposition: "open", summary: JSON.stringify(findings.at(-1)) }] },
          body: result.body.replace(/— APPROVE/g, "— REQUEST CHANGES") + "\nDelivery remains blocked: " + JSON.stringify(findings.at(-1)) };
      }
      return { ...finish(), delivery_blocked: result.repair_blocked
        ?? "Reviewer repair made no progress; unresolved findings or checks require intervention" };
    }
    // The pushed child is inspected already. Observe its checks, and only repair
    // new failures/findings; never dispatch another developer or reviewer here.
  }
}
