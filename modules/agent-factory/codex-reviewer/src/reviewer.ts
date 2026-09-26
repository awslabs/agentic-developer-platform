import { Codex } from "@openai/codex-sdk";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import {
  parseVerdict,
  requiresChanges,
  reviewOutputSchema,
  type CodexIssueReviewEnvelope,
  type CodexPullRequestReviewEnvelope,
  type CodexReviewEnvelope,
  type ReviewFinding,
  type ReviewVerdict,
} from "./contracts.js";
import {
  formatFixesPushedComment,
  formatIssueReviewComment,
  formatReviewComment,
  GitHubClient,
} from "./github.js";
import { ProcessError, run } from "./process.js";
import { runResumableTurn } from "./turn.js";

const SDK_VERSION = "0.155.1";
/**
 * The shared worker pod is the execution sandbox. Codex's Linux sandbox uses
 * bubblewrap/user namespaces, which are intentionally unavailable in that pod.
 */
export const WORKER_SANDBOX_MODE = "danger-full-access" as const;

export type ReviewRunResult =
  | { status: "issue_reviewed"; issue: number; blockers: number }
  | { status: "stale"; expected: string; actual: string }
  | { status: "changes_requested"; blockers: number }
  | { status: "approved"; sha: string }
  | { status: "merged"; sha: string; mergeSha: string };

export interface ReviewRuntime {
  /** Existing checkout prepared by the shared worker entrypoint. */
  workspace: string;
  /** Default/developer installation token prepared by the shared worker entrypoint. */
  githubToken: string;
  /** Renew through the shared worker before API calls and authenticated git. */
  getGitHubToken?: (force?: boolean) => Promise<string>;
  /** Existing gateway-only loopback proxy, ending in /openai/v1. */
  proxyBaseUrl: string;
}

function reviewerPrompt(
  persona: string,
  issue: { title: string; body: string | null },
  pr: { title: string; body: string | null },
  expectedSha: string,
): string {
  return `${persona}\n\nReview the currently checked-out pull request at exactly ${expectedSha}.\n\nDriving issue:\n# ${issue.title}\n${issue.body ?? "(no body)"}\n\nPull request:\n# ${pr.title}\n${pr.body ?? "(no body)"}\n\nInspect the repository, the merge-base diff, and relevant tests. Run read-only checks as useful. Return only the requested structured verdict. Do not modify files.`;
}

function fixPrompt(findings: ReviewFinding[]): string {
  return `Apply only the following reviewer-approved mechanical fixes to the working tree. Do not commit, push, merge, call GitHub, or alter unrelated files. Run focused tests for changed behavior. If a requested repair requires choosing product semantics or changing architecture, leave it untouched and explain that in the final response.\n\n${JSON.stringify(findings, null, 2)}`;
}

function issueReviewPrompt(
  persona: string,
  issue: { title: string; body: string | null },
  request: string,
): string {
  return `${persona}\n\nReview this GitHub issue:\n# ${issue.title}\n${issue.body ?? "(no body)"}\n\nThe human requested:\n${request}\n\nInspect the current repository where useful. Assess whether the issue is clear, feasible, consistent with the codebase, and testable. Identify missing acceptance criteria, security or operational risks, dependency gaps, and ambiguous product decisions. Return only the requested structured verdict. Do not modify files.`;
}

/** Retry only an explicit authentication rejection, never a lost push reply. */
export async function authenticatedGit(runtime: ReviewRuntime, args: string[]) {
  for (let attempt = 0; ; attempt++) {
    const token = runtime.getGitHubToken ? await runtime.getGitHubToken(attempt > 0) : runtime.githubToken;
    try { return await run("git", args, { cwd: runtime.workspace, env: gitEnvironment(token) }); }
    catch (error) {
      const rejected = error instanceof ProcessError && error.result.exitCode === 128
        && /Authentication failed|Invalid username or token|Bad credentials/i.test(error.result.stderr);
      if (attempt >= 1 || !runtime.getGitHubToken || !rejected) throw error;
    }
  }
}

export function gitEnvironment(token: string): NodeJS.ProcessEnv {
  return {
    ...childEnvironment(),
    GITHUB_TOKEN: token,
    GH_TOKEN: token,
    GIT_ASKPASS: process.env.GIT_ASKPASS ?? "/usr/local/bin/git-askpass-helper",
  };
}

export function childEnvironment(): Record<string, string> {
  const env = Object.fromEntries(
    ["PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SSL_CERT_FILE"].flatMap(
      (name) => (process.env[name] ? [[name, process.env[name] as string]] : []),
    ),
  );
  return {
    ...env,
    ADP_GATEWAY_PLACEHOLDER_KEY:
      process.env.ADP_GATEWAY_PLACEHOLDER_KEY ??
      "unused-sidecar-restrips-and-resigns",
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: "/dev/null",
    GIT_TERMINAL_PROMPT: "0",
  };
}

export function repositoryUrl(repository: string): string {
  return `https://x-access-token@github.com/${repository}.git`;
}

async function remoteHead(
  runtime: ReviewRuntime,
  repository: string,
  branch: string,
): Promise<string> {
  const result = await authenticatedGit(runtime,
    ["ls-remote", repositoryUrl(repository), `refs/heads/${branch}`]);
  return result.stdout.trim().split(/\s+/)[0] ?? "";
}

export function selectedModel(env: NodeJS.ProcessEnv = process.env): string {
  return env.ADP_MODEL_RESOLVED ?? env.CODEX_REVIEWER_MODEL ?? "openai.gpt-5.6-sol";
}

async function codexVerdict(
  codex: Codex,
  workspace: string,
  prompt: string,
): Promise<ReviewVerdict> {
  const thread = codex.startThread({
    workingDirectory: workspace,
    model: selectedModel(),
    modelReasoningEffort: "high",
    sandboxMode: WORKER_SANDBOX_MODE,
    approvalPolicy: "never",
    networkAccessEnabled: false,
    webSearchMode: "disabled",
    threadSource: "adp-agent-codex-reviewer",
  });
  const turn = await runResumableTurn(thread, prompt, {
    outputSchema: reviewOutputSchema,
    signal: AbortSignal.timeout(
      Number(process.env.CODEX_REVIEWER_TURN_TIMEOUT_MS ?? 45 * 60 * 1000),
    ),
  });
  return parseVerdict(turn.finalResponse);
}

async function applyMechanicalFixes(
  codex: Codex,
  workspace: string,
  findings: ReviewFinding[],
): Promise<void> {
  const thread = codex.startThread({
    workingDirectory: workspace,
    model: selectedModel(),
    modelReasoningEffort: "high",
    sandboxMode: WORKER_SANDBOX_MODE,
    approvalPolicy: "never",
    networkAccessEnabled: false,
    webSearchMode: "disabled",
    threadSource: "adp-agent-codex-reviewer-fix",
  });
  await runResumableTurn(thread, fixPrompt(findings), {
    signal: AbortSignal.timeout(
      Number(process.env.CODEX_REVIEWER_TURN_TIMEOUT_MS ?? 45 * 60 * 1000),
    ),
  });
}

export async function validateAutofix(
  workspace: string,
  expectedSha: string,
  expectedBranch: string,
  trustedGitConfig: string,
): Promise<string[]> {
  const env = childEnvironment();
  const head = (await run("git", ["rev-parse", "HEAD"], { cwd: workspace, env })).stdout.trim();
  const branch = (
    await run("git", ["symbolic-ref", "--short", "HEAD"], { cwd: workspace, env })
  ).stdout.trim();
  if (head !== expectedSha || branch !== expectedBranch) {
    throw new Error(`Codex autofix altered Git state: expected ${expectedBranch}@${expectedSha}`);
  }
  const currentGitConfig = await readFile(join(workspace, ".git", "config"), "utf8");
  if (currentGitConfig !== trustedGitConfig) {
    throw new Error("Codex autofix altered protected Git configuration");
  }
  const diffArgs = ["diff", "--no-ext-diff", "--no-textconv", "HEAD"];
  const names = await run("git", [...diffArgs, "--name-only"], { cwd: workspace, env });
  const files = names.stdout.split("\n").filter(Boolean);
  if (files.length === 0) throw new Error("Codex reported mechanical fixes but changed no files");
  if (files.length > 20) throw new Error(`Codex autofix touched ${files.length} files; maximum is 20`);
  const numstat = await run("git", [...diffArgs, "--numstat"], { cwd: workspace, env });
  let changedLines = 0;
  for (const line of numstat.stdout.split("\n")) {
    const [added, removed] = line.split("\t");
    changedLines += Number(added) || 0;
    changedLines += Number(removed) || 0;
  }
  if (changedLines > 800) throw new Error(`Codex autofix changed ${changedLines} lines; maximum is 800`);
  await run("git", [...diffArgs, "--check"], { cwd: workspace, env });
  return files;
}

async function waitForChecks(
  github: GitHubClient,
  prNumber: number,
  expectedSha: string,
): Promise<void> {
  const timeoutMs = Number(process.env.CODEX_REVIEWER_CHECK_TIMEOUT_MS ?? 20 * 60 * 1000);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const pr = await github.getPullRequest(prNumber);
    if (pr.head.sha !== expectedSha) throw new Error(`PR head changed while waiting for checks: ${pr.head.sha}`);
    const checks = await github.checks(expectedSha);
    if (checks.failing.length > 0) throw new Error(`required checks failed: ${checks.failing.join(", ")}`);
    if (checks.ready) return;
    await new Promise((resolve) => setTimeout(resolve, 30_000));
  }
  throw new Error("timed out waiting for required checks");
}

export function mergeEnabled(
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  return (env.CODEX_REVIEWER_MERGE_ENABLED ?? "true") === "true";
}

async function publishVerdict(
  github: GitHubClient,
  prNumber: number,
  verdict: ReviewVerdict,
  sha: string,
): Promise<void> {
  const body = formatReviewComment(verdict, sha, `Codex SDK ${SDK_VERSION}`);
  await github.comment(prNumber, body);
}

async function runIssueReview(
  envelope: CodexIssueReviewEnvelope,
  runtime: ReviewRuntime,
): Promise<ReviewRunResult> {
  if (!runtime.workspace || !runtime.githubToken || !runtime.proxyBaseUrl) {
    throw new Error("Codex review requires the shared worker workspace, GitHub token, and gateway proxy");
  }
  const github = new GitHubClient(envelope.repository, runtime.getGitHubToken ?? (async () => runtime.githubToken));
  const [issue, persona] = await Promise.all([
    github.getIssue(envelope.issue.number),
    readFile(new URL("../prompts/reviewer.md", import.meta.url), "utf8"),
  ]);
  const codex = new Codex({
    baseUrl: runtime.proxyBaseUrl,
    apiKey: "sigv4-proxy-placeholder",
    env: childEnvironment(),
  });
  const verdict = await codexVerdict(
    codex,
    runtime.workspace,
    issueReviewPrompt(persona, issue, envelope.issue.triggering_comment),
  );
  await github.commentOnce(
    envelope.issue.number,
    `<!-- agent-codex-reviewer:${envelope.message_id} -->`,
    formatIssueReviewComment(verdict, envelope.issue.number, `Codex SDK ${SDK_VERSION}`),
  );
  return {
    status: "issue_reviewed",
    issue: envelope.issue.number,
    blockers: verdict.findings.filter((finding) => finding.blocking).length,
  };
}

async function runPullRequestReview(
  envelope: CodexPullRequestReviewEnvelope,
  runtime: ReviewRuntime,
): Promise<ReviewRunResult> {
  if (!runtime.workspace || !runtime.githubToken || !runtime.proxyBaseUrl) {
    throw new Error("Codex review requires the shared worker workspace, GitHub token, and gateway proxy");
  }
  const tokenProvider = runtime.getGitHubToken ?? (async () => runtime.githubToken);
  const github = new GitHubClient(envelope.repository, tokenProvider);
  const expected = envelope.pull_request.expected_head_sha;
  const initialPr = await github.getPullRequest(envelope.pull_request.number);
  if (initialPr.state !== "open") return { status: "stale", expected, actual: initialPr.head.sha };
  if (
    initialPr.head.ref !== envelope.pull_request.head_ref ||
    initialPr.base.ref !== envelope.pull_request.base_ref ||
    initialPr.head.sha !== expected
  ) {
    return { status: "stale", expected, actual: initialPr.head.sha };
  }

  const workspace = runtime.workspace;
  {
    const checkedOutSha = (
      await run("git", ["rev-parse", "HEAD"], { cwd: workspace })
    ).stdout.trim();
    if (checkedOutSha !== expected) {
      return { status: "stale", expected, actual: checkedOutSha };
    }
    if (
      (await remoteHead(
        runtime,
        envelope.repository,
        envelope.pull_request.head_ref,
      )) !== expected
    ) {
      return { status: "stale", expected, actual: initialPr.head.sha };
    }
    const trustedGitConfig = await readFile(join(workspace, ".git", "config"), "utf8");
    const [issue, persona] = await Promise.all([
      github.getIssue(envelope.pull_request.issue_number),
      readFile(new URL("../prompts/reviewer.md", import.meta.url), "utf8"),
    ]);
    const codex = new Codex({
      baseUrl: runtime.proxyBaseUrl,
      apiKey: "sigv4-proxy-placeholder",
      env: childEnvironment(),
    });
    let verdict = await codexVerdict(
      codex,
      workspace,
      reviewerPrompt(persona, issue, initialPr, expected),
    );

    const current = await remoteHead(
      runtime,
      envelope.repository,
      envelope.pull_request.head_ref,
    );
    if (current !== expected) return { status: "stale", expected, actual: current };

    const blockers = verdict.findings.filter((finding) => finding.blocking);
    const mechanical = blockers.filter((finding) => finding.fixClass === "mechanical");
    const applyFixes = (process.env.CODEX_REVIEWER_APPLY_FIXES ?? "true") === "true";
    if (blockers.length > 0 && mechanical.length > 0 && applyFixes) {
      await applyMechanicalFixes(codex, workspace, mechanical);
      const files = await validateAutofix(
        workspace,
        expected,
        envelope.pull_request.head_ref,
        trustedGitConfig,
      );
      verdict = await codexVerdict(
        codex,
        workspace,
        `${persona}\n\nRe-review the complete working-tree diff against origin/${envelope.pull_request.base_ref}. The controller applied attempted mechanical repairs for ${mechanical.map((finding) => finding.id).join(", ")}. Return a fresh structured verdict. Do not modify files.`,
      );
      if (requiresChanges(verdict)) {
        const body = `${verdict.summary}\n\nMechanical repairs were attempted locally but were not pushed because the fresh review still requested changes.`;
        verdict = { ...verdict, summary: body };
        await publishVerdict(
          github,
          envelope.pull_request.number,
          verdict,
          expected,
        );
        return { status: "changes_requested", blockers: verdict.findings.filter((finding) => finding.blocking).length };
      }
      const beforePush = await remoteHead(
        runtime,
        envelope.repository,
        envelope.pull_request.head_ref,
      );
      if (beforePush !== expected) return { status: "stale", expected, actual: beforePush };
      const localGitEnv = childEnvironment();
      await run("git", ["add", "--", ...files], { cwd: workspace, env: localGitEnv });
      await run(
        "git",
        [
          "-c",
          "core.hooksPath=/dev/null",
          "commit",
          "-m",
          `fix(review): apply Codex review repairs for #${envelope.pull_request.number}`,
        ],
        { cwd: workspace, env: localGitEnv },
      );
      const newSha = (
        await run("git", ["rev-parse", "HEAD"], { cwd: workspace, env: localGitEnv })
      ).stdout.trim();
      const parentSha = (
        await run("git", ["rev-parse", "HEAD^"], { cwd: workspace, env: localGitEnv })
      ).stdout.trim();
      if (parentSha !== expected) {
        throw new Error(`Codex autofix commit does not descend directly from ${expected}`);
      }
      await authenticatedGit(runtime,
        [
          "push",
          `--force-with-lease=refs/heads/${envelope.pull_request.head_ref}:${expected}`,
          repositoryUrl(envelope.repository),
          `HEAD:refs/heads/${envelope.pull_request.head_ref}`,
        ],
      );
      const pushedHead = await remoteHead(
        runtime,
        envelope.repository,
        envelope.pull_request.head_ref,
      );
      if (pushedHead !== newSha) {
        return { status: "stale", expected: newSha, actual: pushedHead };
      }
      await github.comment(
        envelope.pull_request.number,
        `${formatFixesPushedComment(verdict, newSha, `Codex SDK ${SDK_VERSION}`)}\nMechanical fixes were pushed directly to \`${envelope.pull_request.head_ref}\` after the repaired tree passed a fresh Codex review.`,
      );
      await waitForChecks(github, envelope.pull_request.number, newSha);
      const repairedPr = await github.getPullRequest(envelope.pull_request.number);
      if (repairedPr.head.sha !== newSha) {
        return { status: "stale", expected: newSha, actual: repairedPr.head.sha };
      }
      if (repairedPr.draft) {
        throw new Error("approved PR is still a draft; refusing to merge");
      }
      if (!mergeEnabled()) {
        return { status: "approved", sha: newSha };
      }
      const mergeSha = await github.merge(envelope.pull_request.number, newSha);
      return { status: "merged", sha: newSha, mergeSha };
    }

    if (requiresChanges(verdict)) {
      await publishVerdict(
        github,
        envelope.pull_request.number,
        verdict,
        expected,
      );
      return { status: "changes_requested", blockers: blockers.length };
    }

    await waitForChecks(github, envelope.pull_request.number, expected);
    const beforeMerge = await github.getPullRequest(envelope.pull_request.number);
    if (beforeMerge.head.sha !== expected) return { status: "stale", expected, actual: beforeMerge.head.sha };
    if (beforeMerge.draft) {
      throw new Error("approved PR is still a draft; refusing to merge");
    }
    await publishVerdict(
      github,
      envelope.pull_request.number,
      verdict,
      expected,
    );
    if (!mergeEnabled()) {
      return { status: "approved", sha: expected };
    }
    const mergeSha = await github.merge(envelope.pull_request.number, expected);
    return { status: "merged", sha: expected, mergeSha };
  }
}

export async function runReview(
  envelope: Exclude<CodexReviewEnvelope, { kind: "codex_engine_review" }>,
  runtime: ReviewRuntime,
): Promise<ReviewRunResult> {
  if (envelope.kind === "codex_issue_review") {
    return runIssueReview(envelope, runtime);
  }
  return runPullRequestReview(envelope, runtime);
}
