/** Engine review/fix controller. Publication and terminal receipts belong to Python;
 * checks, merge and subsequent scheduling belong to the engine. */
import { Codex } from "@openai/codex-sdk";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { setTimeout as pause } from "node:timers/promises";
import { parseVerdict, requiresChanges, reviewOutputSchema,
  type CodexEngineReviewEnvelope, type ReviewVerdict } from "./contracts.js";
import { GitHubClient, formatReviewComment } from "./github.js";
import { childEnvironment, gitEnvironment, repositoryUrl, selectedModel,
  WORKER_SANDBOX_MODE, type ReviewRuntime } from "./reviewer.js";
import { run } from "./process.js";
import { runResumableTurn } from "./turn.js";
import { ModelExecutionBudget } from "./model-budget.js";
import { observeReviewerChecks, type ReviewerChecks } from "./reviewer-checks.js";

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

export interface EngineReviewServices {
  github: Pick<GitHubClient, "getPullRequest" | "getIssue" | "getBranch">;
  review(prompt: string): Promise<EngineVerdict>;
  fix(prompt: string): Promise<void>;
  checks?(head: string): Promise<ReviewerChecks>;
  wait?(milliseconds: number): Promise<unknown>;
}

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

function services(runtime: ReviewRuntime & { repository: string }): EngineReviewServices {
  const codex = new Codex({ baseUrl: runtime.proxyBaseUrl,
    apiKey: "sigv4-proxy-placeholder", env: childEnvironment() });
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
  return {
    github: new GitHubClient(runtime.repository ?? "", runtime.getGitHubToken ?? (async () => {
      // The shared worker rotates this file; long reviews must not retain an expired token.
      try { return (await readFile(process.env.ADP_TOKEN_FILE ?? "/tmp/.adp-gh-token", "utf8")).trim() || runtime.githubToken; }
      catch { return runtime.githubToken; }
    })),
    checks: observeReviewerChecks,
    review: async prompt => budget.run(async signal => parseEngineVerdict((await runResumableTurn(inspection, prompt,
      { outputSchema: engineReviewSchema, signal })).finalResponse)),
    fix: async prompt => { await budget.run(signal => runResumableTurn(repair, prompt, { signal })); },
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
  const controller = supplied ?? services({ ...runtime, repository: envelope.repository });
  const localEnv = childEnvironment();
  const git = async (args: string[]) => (await run("git", args,
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
  const availableBase = await run("git", ["cat-file", "-e", `${baseSha}^{commit}`],
    { cwd: runtime.workspace, env: localEnv, allowFailure: true });
  if (availableBase.exitCode !== 0) {
    const token = runtime.getGitHubToken ? await runtime.getGitHubToken() : runtime.githubToken;
    await run("git", ["fetch", "--no-tags", repositoryUrl(envelope.repository), baseSha],
      { cwd: runtime.workspace, env: gitEnvironment(token) });
  }
  const branch = await git(["symbolic-ref", "--short", "HEAD"]);
  const config = await readFile(join(runtime.workspace, ".git", "config"), "utf8");
  const trackedDiff = () => git(["diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"]);
  if (await trackedDiff()) throw new Error("Engine review checkout has pre-existing tracked changes");
  const untracked = async () => (await git(["ls-files", "--others", "--exclude-standard", "-z"]))
    .split("\0").filter(Boolean);
  const baseline = new Set(await untracked());
  const issue = await controller.github.getIssue(envelope.issue_number);
  const persona = await readFile(new URL("../prompts/reviewer.md", import.meta.url), "utf8");
  const story = JSON.stringify({ issue: { number: envelope.issue_number, title: issue.title, body: issue.body },
    pullRequest: { title: initialPr.title, body: initialPr.body }, priorFindings: cycle.findings });
  const context = `${persona}\n\nThis is an engine assignment. The story and acceptance criteria define the work; no additional scope approval is required. The engine owns merge and scheduling. Do not publish, merge, dispatch another agent, or write review reports into the repository.\n\n<story-data>${story.replaceAll("<", "\\u003c").replaceAll(">", "\\u003e")}</story-data>`;
  const verifyGit = async (head: string) => {
    if (await git(["rev-parse", "HEAD"]) !== head
        || await git(["symbolic-ref", "--short", "HEAD"]) !== branch
        || await readFile(join(runtime.workspace, ".git", "config"), "utf8") !== config) {
      throw new Error("Codex changed protected Git state");
    }
  };
  const inspect = async (head: string, workingTree = false) => {
    const before = await trackedDiff();
    const beforeUntracked = new Set(await untracked());
    const pendingFiles = [...beforeUntracked].filter(file => !baseline.has(file));
    const fingerprint = async () => Promise.all(pendingFiles.map(async file =>
      createHash("sha256").update(await readFile(join(runtime.workspace, file))).digest("hex")));
    const beforeFiles = await fingerprint();
    const verdict = await controller.review(`${context}\n\nReview ${workingTree ? "the full repaired working tree" : `exact commit ${head}`} against base commit ${baseSha}. Inspect correctness and security and run relevant tests. Explicitly report whether both stages completed and any validation gaps. Return the structured verdict. Do not modify source files, stage, commit or run GitHub commands.`);
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
  const conflict = initialPr.mergeable === false || initialPr.mergeable_state === "dirty"
    || assignedBaseRepair || (cycle.action === "repair" && initialPr.mergeable !== true);
  const assignedRepair = cycle.action === "repair" || conflict;
  // Repair assignments already carry findings. Reviewing the unchanged head
  // first can approve it and silently skip the actual repair (notably conflicts).
  const original = assignedRepair && cycle.allow_story_repairs && !merged ? null : await inspect(expected);
  let verdict = original;
  let head = expected;
  let mergeBase: string | null = null;
  if ((!verdict || !complete(verdict)) && cycle.allow_story_repairs && !merged) {
    if (initialPr.head.repo?.full_name?.toLowerCase() !== envelope.repository.toLowerCase()) {
      throw new Error("Engine story repairs require the bound repository branch");
    }
    if (conflict) {
      // The controller prepares the exact base merge. The model only resolves
      // files; HEAD/branch/config and the publication lease remain fenced.
      const token = runtime.getGitHubToken ? await runtime.getGitHubToken() : runtime.githubToken;
      await run("git", ["fetch", "--no-tags", repositoryUrl(envelope.repository), baseSha],
        { cwd: runtime.workspace, env: gitEnvironment(token) });
      const merge = await run("git", ["-c", "core.hooksPath=/dev/null", "merge", "--no-commit", "--no-ff", baseSha],
        { cwd: runtime.workspace, env: localEnv, allowFailure: true });
      if (merge.exitCode && !await git(["ls-files", "--unmerged"])) throw new Error("Could not prepare the assigned base merge");
      const pendingMerge = await run("git", ["rev-parse", "--verify", "MERGE_HEAD"],
        { cwd: runtime.workspace, env: localEnv, allowFailure: true });
      if (pendingMerge.exitCode === 0) mergeBase = baseSha;
    }
    // Publish a completed inspection after one repair pass. A second speculative
    // pass used to consume the remaining deadline and lose the first pass too.
    {
      await controller.fix(`${context}\n\nFix the issues required by this story and its acceptance criteria, including the assigned findings and validation gaps below. ${conflict ? `The controller prepared a merge of base ${baseSha}; resolve every conflict while preserving the story and current base behavior.` : ""} You own the repair; do not hand it to a developer or ask for another scope approval. Make reasonable implementation decisions from the story and existing code. Add or update focused tests and run them. Report a concrete blocker only if the story cannot determine a required decision or an external dependency is unavailable. Do not commit, push, merge, alter Git configuration, call GitHub or write review reports.\n\n<findings-data>${JSON.stringify(verdict ?? cycle.findings).replaceAll("<", "\\u003c").replaceAll(">", "\\u003e")}</findings-data>`);
      await verifyGit(expected);
      if (conflict) {
        if (mergeBase && await git(["rev-parse", "MERGE_HEAD"]) !== mergeBase) throw new Error("Codex changed the protected merge base");
        await git(["diff", "--check"]);
        await git(["add", "--update", "--", "."]);
        if (await git(["ls-files", "--unmerged"])) throw new Error("Base merge still has unresolved files");
      }
      verdict = await inspect(expected, true);
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
      await git(["diff", "--cached", "--check"]);
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
      let token: string;
      if (runtime.getGitHubToken) token = await runtime.getGitHubToken();
      else {
        token = runtime.githubToken;
        try { token = (await readFile(process.env.ADP_TOKEN_FILE ?? "/tmp/.adp-gh-token", "utf8")).trim() || token; } catch { /* embedded entrypoint always supplies renewal */ }
      }
      await run("git", ["push", `--force-with-lease=refs/heads/${initialPr.head.ref}:${expected}`,
        repositoryUrl(envelope.repository), `HEAD:refs/heads/${initialPr.head.ref}`],
      { cwd: runtime.workspace, env: gitEnvironment(token) });
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
  } else {
    const current = await controller.github.getPullRequest(cycle.pr_number);
    if (current.head.sha !== head) throw new Error("PR head changed before review delivery");
  }
  if (!verdict) throw new Error("Engine review produced no inspection");
  return { status: "engine_reviewed", sha: head, merged,
    repair_base_sha: head !== expected ? expected : null,
    report: engineReport(verdict),
    body: engineReviewBody(verdict, head),
  };
}

/** Keep the same controller and both SDK threads alive until checks settle.
 * Python publishes final evidence/terminal receipts only after this returns;
 * the engine then performs its existing fenced merge and story completion. */
export async function runEngineReview(
  envelope: CodexEngineReviewEnvelope, runtime: ReviewRuntime, supplied?: EngineReviewServices,
) {
  const controller = supplied ?? services({ ...runtime, repository: envelope.repository });
  let result = await runEngineReviewPass(envelope, runtime, controller);
  if (!envelope.cycle.reviewer_owned_delivery || result.merged) return result;
  if (!controller.checks) throw new Error("Reviewer-owned delivery requires canonical check observations");
  const root = envelope.cycle.head_sha;
  const finish = () => ({ ...result, repair_base_sha: result.sha === root ? null : root });
  const wait = controller.wait ?? pause;
  let observationFailures = 0;
  while (true) {
    if (Object.values(result.report.stages).some(stage => stage !== "completed")) return finish();
    let checks: ReviewerChecks;
    try {
      checks = await controller.checks(result.sha);
      observationFailures = 0;
    } catch (error) {
      if (!(error instanceof Error) || !("retryable" in error) || error.retryable !== true || ++observationFailures >= 3) throw error;
      await wait(60000);
      continue;
    }
    if (checks.head_sha !== result.sha) throw new Error("PR head changed while waiting for checks");
    if (!checks.open && !checks.merged) throw new Error("PR closed while waiting for checks");
    if (checks.merged) return finish();
    if (checks.state === "pending" && !checks.base_repair_required) {
      // No model call and no terminal receipt while applicable CI is running.
      await wait(60000);
      continue;
    }
    if (checks.state === "passed" && !checks.base_repair_required && result.report.verdict === "approve") return finish();
    const previous = result;
    const needsRepair = checks.state === "failed" || checks.base_repair_required;
    const findings = [...result.report.findings, {
      source: checks.base_repair_required ? "merge-controller" : "required-checks",
      summary: checks.base_repair_required ? "Repair merge conflict or out-of-date base" : "Canonical CI observation for this exact head",
      evidence: checks,
    }];
    if (envelope.cycle.allow_story_repairs) {
      result = await runEngineReviewPass({ ...envelope, cycle: { ...envelope.cycle,
        head_sha: result.sha, action: needsRepair ? "repair" : "review", findings,
      } }, runtime, controller);
    }
    if (result.sha === previous.sha) {
      // A genuine external/no-progress blocker must remain visible. Do not spend
      // another model turn or accidentally approve code with failing checks.
      if (needsRepair) {
        result = { ...result, report: { ...result.report, verdict: "request-changes",
          findings: [...result.report.findings, { finding_id: "delivery-blocked", stage: "functional",
            severity: "blocking", disposition: "open", summary: JSON.stringify(findings.at(-1)) }] },
          body: result.body.replace(/— APPROVE/g, "— REQUEST CHANGES") + "\nDelivery remains blocked: " + JSON.stringify(findings.at(-1)) };
      }
      return finish();
    }
    // The pushed child is inspected already. Observe its checks, and only repair
    // new failures/findings; never dispatch another developer or reviewer here.
  }
}
