import { reviewEvents, reviewSignal, reviewOperation, type ReviewObserver } from "./review-observer.js";
import { loadSharedInstructions } from "./shared-instructions.js";
import { Codex } from "@openai/codex-sdk";
import { readFile } from "node:fs/promises";
import {
  parseVerdict,
  reviewOutputSchema,
  type CodexIssueReviewEnvelope,
  type CodexPullRequestReviewEnvelope,
  type CodexReviewEnvelope,
  type ReviewVerdict,
} from "./contracts.js";
import {
  formatIssueReviewComment,
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
  observer?: ReviewObserver;
  /** Default/developer installation token prepared by the shared worker entrypoint. */
  githubToken: string;
  /** Renew through the shared worker before API calls and authenticated git. */
  getGitHubToken?: (force?: boolean) => Promise<string>;
  /** Existing gateway-only loopback proxy, ending in /openai/v1. */
  proxyBaseUrl: string;
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
    try { return await reviewOperation(runtime.observer, () => run("git", args, { cwd: runtime.workspace, env: gitEnvironment(token), signal: runtime.observer?.control?.signal })); }
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

export function selectedModel(env: NodeJS.ProcessEnv = process.env): string {
  return env.ADP_MODEL_RESOLVED ?? env.CODEX_REVIEWER_MODEL ?? "openai.gpt-5.6-sol";
}

async function codexVerdict(
  codex: Codex,
  workspace: string,
  prompt: string,
  verifyInstructions: () => void,
  observer?: ReviewObserver,
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
    signal: reviewSignal(AbortSignal.timeout(
      Number(process.env.CODEX_REVIEWER_TURN_TIMEOUT_MS ?? 45 * 60 * 1000),
    ), observer),
  }, undefined, verifyInstructions, reviewEvents(observer, true));
  return parseVerdict(turn.finalResponse);
}

export function mergeEnabled(
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  return (env.CODEX_REVIEWER_MERGE_ENABLED ?? "true") === "true";
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
    readFile(new URL("../prompts/reviewer.md", import.meta.url), "utf8").then(text => loadSharedInstructions("reviewer", text)),
  ]);
  const codex = new Codex({
    baseUrl: runtime.proxyBaseUrl,
    apiKey: "sigv4-proxy-placeholder",
    config: { developer_instructions: persona.text },
    env: { ...childEnvironment(), ...(runtime.observer?.control ? { ADP_CODEX_CONTROL_SOCKET: runtime.observer.control.socket } : {}) },
  });
  const verdict = await codexVerdict(
    codex,
    runtime.workspace,
    issueReviewPrompt("", issue, envelope.issue.triggering_comment),
    persona.verify,
    runtime.observer,
  );
  await reviewOperation(runtime.observer, () => github.commentOnce(
    envelope.issue.number,
    `<!-- agent-codex-reviewer:${envelope.message_id} -->`,
    formatIssueReviewComment(verdict, envelope.issue.number, `Codex SDK ${SDK_VERSION}`),
  ));
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
  const { runStandaloneReview } = await import("./standalone-review.js");
  return runStandaloneReview(envelope, runtime);
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
