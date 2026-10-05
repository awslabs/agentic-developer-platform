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
import { GitHubClient, formatReviewComment } from "./github.js";
import { authenticatedGit, childEnvironment, repositoryUrl, selectedModel,
  WORKER_SANDBOX_MODE, type ReviewRuntime } from "./reviewer.js";
import { ProcessError, run } from "./process.js";
import { runResumableTurn } from "./turn.js";
import { ModelExecutionBudget } from "./model-budget.js";
import { observeReviewerChecks, type ReviewerChecks } from "./reviewer-checks.js";
import { deliverEngineReview, type ReviewerMerge } from "./engine-delivery.js";

export interface EngineVerdict extends ReviewVerdict {
  stages: { functional: "completed" | "failed"; security: "completed" | "failed" };
  stageDetails: string;
}

export const engineReviewSchema = {
  ...reviewOutputSchema,
  properties: {
    ...reviewOutputSchema.properties,
    stages: {
      type: "object", additionalProperties: false,
      properties: {
        functional: { type: "string", enum: ["completed", "failed"] },
        security: { type: "string", enum: ["completed", "failed"] },
      },
      required: ["functional", "security"],
    },
    stageDetails: { type: "string" },
  },
  required: [...reviewOutputSchema.required, "stages", "stageDetails"],
};

export function parseEngineVerdict(raw: string): EngineVerdict {
  const verdict = parseVerdict(raw) as EngineVerdict;
  if (!verdict.stages || typeof verdict.stageDetails !== "string"
      || !["functional", "security"].every(name =>
        ["completed", "failed"].includes(verdict.stages[name as keyof EngineVerdict["stages"]]))) {
    throw new Error("Codex did not report functional and security review completion");
  }
  return verdict;
}

function inspected(verdict: EngineVerdict): boolean {
  return Object.values(verdict.stages).every(value => value === "completed");
}

function complete(verdict: EngineVerdict): boolean {
  return !requiresChanges(verdict) && verdict.validationGaps.length === 0 && inspected(verdict);
}

export interface RepairMilestone {
  outcome: 'checkpoint' | 'complete' | 'blocked';
  summary: string;
  remainingWork: string[];
}

export const repairMilestoneSchema = {
  type: 'object', additionalProperties: false,
  properties: {
    outcome: { type: 'string', enum: ['checkpoint', 'complete', 'blocked'] },
    summary: { type: 'string' },
    remainingWork: { type: 'array', items: { type: 'string' } },
  },
  required: ['outcome', 'summary', 'remainingWork'],
};

export function parseRepairMilestone(raw: string): RepairMilestone {
  const result = JSON.parse(raw) as RepairMilestone;
  if (!result || !['checkpoint', 'complete', 'blocked'].includes(result.outcome)
      || typeof result.summary !== 'string' || !result.summary.trim()
      || !Array.isArray(result.remainingWork) || result.remainingWork.some(item => typeof item !== 'string' || !item.trim())
      || (result.outcome === 'checkpoint' && result.remainingWork.length === 0)
      || (result.outcome === 'complete' && result.remainingWork.length !== 0)) {
    throw new Error('Invalid repair milestone result');
  }
  return result;
}

export interface EngineReviewServices {
  github: Pick<GitHubClient, "getPullRequest" | "getIssue" | "getBranch"> & Partial<Pick<GitHubClient, "taskChecklists">>;
  review(prompt: string): Promise<EngineVerdict>;
  fix(prompt: string): Promise<RepairMilestone | void>;
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
  const budget = new ModelExecutionBudget(Number(process.env.CODEX_REVIEWER_TURN_TIMEOUT_MS ?? 45 * 60 * 1000));
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
        instructions.verify, reviewEvents(runtime.observer, true))).finalResponse));
    },
    fix: async prompt => {
      runtime.observer?.explanation('Planning the next repair milestone, then checking and publishing its checkpoint.');
      return budget.run(async signal => parseRepairMilestone((await runResumableTurn(repair, prompt,
        { outputSchema: repairMilestoneSchema, signal: reviewSignal(signal, runtime.observer) }, undefined,
        instructions.verify, reviewEvents(runtime.observer, true))).finalResponse));
    },
  };
}

export function engineReport(verdict: EngineVerdict) {
  const findings = verdict.findings.map(finding => ({
    finding_id: finding.id, stage: "functional",
    severity: finding.blocking ? "blocking" : "minor", disposition: "open",
    summary: `${finding.title}: ${finding.details} (${finding.file}${finding.line ? `:${finding.line}` : ""}). Repair: ${finding.recommendedFix}`,
  }));
  verdict.validationGaps.forEach((gap, index) => findings.push({
    finding_id: `validation-gap-${index + 1}`, stage: "functional",
    severity: "blocking", disposition: "open", summary: gap,
  }));
  return {
    verdict: complete(verdict) ? "approve" : findings.some(f => f.severity === "blocking")
      ? "request-changes" : "incomplete",
    stages: verdict.stages,
    stage_details: { functional: verdict.stageDetails, security: verdict.stageDetails },
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
  const story = JSON.stringify({ issue: { number: envelope.issue_number, title: issue.title, body: issue.body },
    pullRequest: { title: initialPr.title, body: initialPr.body }, acceptedScope: cycle.accepted_scope,
    priorFindings: cycle.findings, priorTaskChecklists });
  const recoveryContext = cycle.recovery
    ? "This is stalled-story recovery. The prior worker has exited. Preserve its committed work; do not restart from main or treat a checkpoint/PR as completed implementation. Read the current issue including owner clarifications. Identify every unfinished acceptance criterion, repair within the assigned scope when authorized, and revalidate the final changes. An unresolved product/contract clarification or unavailable required evidence is a blocker, not permission to guess or report success."
    : "";
  const context = `${recoveryContext}\n\n${persona}\n\nThis is a review, fix, test and merge assignment. The story and acceptance criteria define the work; no additional scope approval is required. Your controller publishes and merges after verified review and CI; the engine completes the story. Do not publish, merge, dispatch another agent, or write review reports into the repository.\n\n<story-data>${story.replaceAll("<", "\\u003c").replaceAll(">", "\\u003e")}</story-data>`;
  const verifyGit = async (head: string) => {
    if (await git(["rev-parse", "HEAD"]) !== head
        || await git(["symbolic-ref", "--short", "HEAD"]) !== branch
        || await readFile(join(runtime.workspace, ".git", "config"), "utf8") !== config) {
      throw new Error("Codex changed protected Git state");
    }
  };
  const inspect = async (head: string, workingTree = false, checkpoint = false) => {
    const before = await trackedDiff();
    const beforeUntracked = new Set(await untracked());
    const pendingFiles = [...beforeUntracked].filter(file => !baseline.has(file));
    const fingerprint = async () => Promise.all(pendingFiles.map(async file =>
      createHash("sha256").update(await readFile(join(runtime.workspace, file))).digest("hex")));
    const beforeFiles = await fingerprint();
    const verdict = await controller.review(`${context}\n\nReview ${workingTree ? "the full repaired working tree" : `exact commit ${head}`} against base commit ${baseSha}. ${checkpoint ? "This is an incomplete checkpoint. Inspect the changed milestone for correctness and security with focused checks; record unfinished criteria as findings. Leave long final validation for the completed repair, and never approve unfinished work." : "Inspect correctness and security and run relevant tests."} Explicitly report whether both stages completed and any validation gaps. Return the structured verdict. Do not modify source files, stage, commit or run GitHub commands.`);
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
  const behind = envelope.cycle.reviewer_owned_delivery && (await localRun("git", ["merge-base", "--is-ancestor", baseSha, expected],
    { cwd: runtime.workspace, env: localEnv, allowFailure: true })).exitCode !== 0;
  const conflict = behind || initialPr.mergeable === false || initialPr.mergeable_state === "dirty"
    || assignedBaseRepair || (cycle.action === "repair" && initialPr.mergeable !== true);
  const assignedRepair = cycle.action === "repair" || conflict;
  // Repair assignments already carry findings. Reviewing the unchanged head
  // first can approve it and silently skip the actual repair (notably conflicts).
  const original = assignedRepair && cycle.allow_story_repairs && !merged ? null : await inspect(expected);
  let verdict = original;
  let head = expected;
  let mergeBase: string | null = null;
  let milestone: RepairMilestone | void = undefined;
  if ((!verdict || !complete(verdict)) && cycle.allow_story_repairs && !merged) {
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
    // Publish a completed inspection after one repair pass. A second speculative
    // pass used to consume the remaining deadline and lose the first pass too.
    {
      milestone = await controller.fix(`${context}\n\nPlan the required repairs as coherent milestones and explain the plan before editing. Fix the next useful milestone from the assigned findings and validation gaps below, within this story and its acceptance criteria. Return a checkpoint after that milestone, before long validation, and approximately every 15 minutes at a safe tool boundary while changes accumulate. Do not accumulate all remaining work into one turn. Report remaining implementation work in remainingWork with outcome checkpoint; your controller will inspect, commit, push and verify this milestone, then continue in this same task. Use outcome complete only when all repairs are implemented, or blocked for a concrete external blocker. A checkpoint is not approval or story completion. ${conflict ? `The controller prepared a merge of base ${baseSha}; resolve every conflict while preserving the story and current base behavior.` : ""} You own the repair; do not hand it to a developer or ask for another scope approval. Make reasonable implementation decisions from the story and existing code. Add or update focused tests and run them. Report a concrete blocker only if the story cannot determine a required decision or an external dependency is unavailable. Do not commit, push, merge, alter Git configuration, call GitHub or write review reports.\n\n<findings-data>${JSON.stringify(verdict ?? cycle.findings).replaceAll("<", "\\u003c").replaceAll(">", "\\u003e")}</findings-data>`);
      await verifyGit(expected);
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
      verdict = await inspect(expected, true, milestone?.outcome === 'checkpoint');
      if (milestone?.outcome === 'blocked') {
        verdict = { ...verdict, validationGaps: [...verdict.validationGaps, milestone.summary, ...milestone.remainingWork] };
      }
      if (milestone?.outcome === 'checkpoint') {
        verdict = { ...verdict, validationGaps: [...verdict.validationGaps,
          ...milestone.remainingWork.map(item => `Remaining repair milestone: ${item}`)] };
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
      await git(["-c", "core.hooksPath=/dev/null", "commit", "-m", `fix(review): address story #${envelope.issue_number}`]);
      head = await git(["rev-parse", "HEAD"]);
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
    runtime.observer?.explanation(`Published ${milestone?.outcome === 'checkpoint' ? 'repair checkpoint' : 'reviewed repair'}: https://github.com/${envelope.repository}/commit/${head}. ${milestone?.summary ?? 'The inspected repairs are now on the PR branch.'} ${milestone?.outcome === 'checkpoint' ? `Remaining work: ${milestone.remainingWork.join('; ')}. Continuing the same assignment.` : 'Checking final CI and merge eligibility.'} CI for this revision has not yet been verified.`);
  } else {
    const current = await controller.github.getPullRequest(cycle.pr_number);
    if (current.head.sha !== head) throw new Error("PR head changed before review delivery");
  }
  if (!verdict) throw new Error("Engine review produced no inspection");
  return { status: "engine_reviewed", sha: head, merged, reviewed_base_sha: baseSha,
    repair_base_sha: head !== expected ? expected : null,
    checkpoint_remaining: head !== expected && milestone?.outcome === 'checkpoint' ? milestone.remainingWork : [],
    ...(milestone?.outcome === "blocked" ? { repair_blocked: milestone.summary } : {}),
    report: engineReport(verdict),
    body: engineReviewBody(verdict, head),
  };
}

/** Retain both SDK threads through CI and merge. Polling makes no model calls. */
export async function runEngineReview(
  envelope: CodexEngineReviewEnvelope, runtime: ReviewRuntime, supplied?: EngineReviewServices,
) {
  const controller = supplied ?? createReviewServices({ ...runtime, repository: envelope.repository });
  let result = await runEngineReviewPass(envelope, runtime, controller);
  const root = envelope.cycle.head_sha;
  const finish = () => ({ ...result, repair_base_sha: result.sha === root ? null : root });
  if (!envelope.cycle.reviewer_owned_delivery) {
    // Retained legacy assignments leave CI/merge delivery with the engine. Publishing
    // a milestone must not end their repair task or consume another dispatch.
    // Reuse the same repair/inspection threads and shared model allowance.
    while (result.checkpoint_remaining.length && !result.merged && !result.repair_blocked) {
      const previous = result;
      result = await runEngineReviewPass({ ...envelope, cycle: { ...envelope.cycle,
        head_sha: result.sha, action: "repair", findings: result.report.findings,
      } }, runtime, controller);
      if (result.sha === previous.sha) break;
    }
    return finish();
  }
  if (result.merged) return result;
  if (!controller.checks || !controller.deliver) throw new Error("Reviewer-owned delivery requires checks and deterministic merge delivery");
  const now = controller.now ?? Date.now;
  const timeout = controller.deliveryTimeoutMs ?? 60 * 60 * 1000;
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
  let observationFailures = 0;
  let deliveryFailures = 0;
  let queued = false;
  let repairRetries = 0;
  while (true) {
    await reviewOperation(runtime.observer, async () => {});
    if (result.repair_blocked) return { ...finish(), delivery_blocked: result.repair_blocked };
    if (now() >= deadline) return { ...finish(), delivery_blocked: "Delivery deadline exceeded while waiting for CI or merge; inspected head retained for recovery" };
    if (Object.values(result.report.stages).some(stage => stage !== "completed")) return finish();
    let checks: ReviewerChecks;
    try {
      checks = await controller.checks(result.sha);
      observationFailures = 0;
    } catch (error) {
      runtime.observer?.control?.signal.throwIfAborted();
      if (!(error instanceof Error) || !("retryable" in error) || error.retryable !== true || ++observationFailures >= 3) {
        return { ...finish(), delivery_blocked: "CI observation unavailable or authorization withdrawn" };
      }
      await wait(60000);
      continue;
    }
    if (checks.head_sha !== result.sha) return { ...finish(), delivery_blocked: "PR head changed while waiting for checks" };
    if (!checks.open && !checks.merged) return { ...finish(), delivery_blocked: "PR closed while waiting for checks" };
    if (checks.merged) {
      const delivery = await deliver();
      if (delivery.state === "merged") return { ...finish(), merged: true };
      throw new Error("Merged PR delivery could not be verified");
    }
    if (queued) {
      try {
        const delivery = await deliver();
        if (delivery.state === "merged") return { ...finish(), merged: true };
        if (delivery.state === "blocked") return { ...finish(), delivery_blocked: delivery.reason ?? "Merge queue blocked" };
        if (delivery.queued) { await wait(60000); continue; }
        queued = false; // Queue removed the entry; current CI/base may need repair.
      } catch {
        runtime.observer?.control?.signal.throwIfAborted();
        return { ...finish(), delivery_blocked: "Merge queue observation unavailable" };
      }
    }
    if (checks.base_sha !== result.reviewed_base_sha) checks = { ...checks, base_repair_required: true };
    if (checks.state === "pending" && !checks.base_repair_required
        && result.checkpoint_remaining.length === 0 && result.report.verdict === "approve") {
      // No model call and no terminal receipt while applicable CI is running.
      await wait(60000);
      continue;
    }
    if (checks.state === "passed" && !checks.base_repair_required && result.report.verdict === "approve") {
      let delivery: ReviewerMerge;
      try { delivery = await deliver(); }
      catch (error) {
        runtime.observer?.control?.signal.throwIfAborted();
        if (!(error instanceof Error) || !("retryable" in error) || error.retryable !== true || ++deliveryFailures >= 3) {
          return { ...finish(), delivery_blocked: "Review publication or merge unavailable; inspect delivery evidence" };
        }
        await wait(60000);
        continue;
      }
      deliveryFailures = 0;
      if (delivery.state === "merged") return { ...finish(), merged: true };
      if (delivery.state === "blocked") return { ...finish(), delivery_blocked: delivery.reason ?? "Merge blocked" };
      if (delivery.state === "pending") { queued = delivery.queued === true; await wait(60000); continue; }
      checks = { ...checks, base_repair_required: true, failures: [delivery.reason] };
    }
    const previous = result;
    // Remaining findings are a repair assignment, not another read-only review.
    // Continue useful work while checkpoint CI is pending.
    const needsRepair = result.report.verdict !== "approve" || checks.state === "failed"
      || checks.base_repair_required || result.checkpoint_remaining.length > 0;
    const findings = [...result.report.findings, {
      source: checks.base_repair_required ? "merge-controller" : "required-checks",
      summary: checks.base_repair_required ? "Repair merge conflict or out-of-date base" : "Canonical CI observation for this exact head",
      evidence: checks,
    }];
    if (envelope.cycle.allow_story_repairs) {
      // Published milestones continue implementation within the same model/time
      // allowance; they are not retries of a supposedly completed repair.
      if (result.checkpoint_remaining.length === 0) {
        if (repairRetries >= 3) return { ...finish(),
          delivery_blocked: "Automatic repair retry limit reached (3); unresolved findings or CI require intervention" };
        repairRetries++;
      }
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
