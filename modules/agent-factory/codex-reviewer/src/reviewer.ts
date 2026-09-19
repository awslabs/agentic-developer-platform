import { Codex } from "@openai/codex-sdk";
import { readFile, rm, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  parseVerdict,
  requiresChanges,
  reviewOutputSchema,
  type CodexReviewEnvelope,
  type ReviewFinding,
  type ReviewVerdict,
} from "./contracts.js";
import {
  formatFixesPushedComment,
  formatReviewComment,
  GitHubClient,
} from "./github.js";
import { run } from "./process.js";
import { startGatewayProxy } from "./proxy.js";
import { TokenBroker } from "./token-broker.js";

const SDK_VERSION = "0.155.1";
const FORBIDDEN_AUTOFIX_PATHS = [
  /^\.github\/workflows\//,
  /(^|\/)infra\//,
  /(^|\/)migrations?\//,
  /(^|\/)alembic\//,
  /^agent_learning\//,
];

export type ReviewRunResult =
  | { status: "stale"; expected: string; actual: string }
  | { status: "changes_requested"; blockers: number }
  | { status: "fixes_pushed"; sha: string }
  | { status: "approved"; sha: string }
  | { status: "merged"; sha: string; mergeSha: string };

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

function gitEnvironment(token: string): NodeJS.ProcessEnv {
  return {
    ...childEnvironment(),
    GIT_CONFIG_COUNT: "1",
    GIT_CONFIG_KEY_0: "http.https://github.com/.extraheader",
    GIT_CONFIG_VALUE_0: `AUTHORIZATION: bearer ${token}`,
  };
}

function childEnvironment(): Record<string, string> {
  const env = Object.fromEntries(
    ["PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SSL_CERT_FILE"].flatMap(
      (name) => (process.env[name] ? [[name, process.env[name] as string]] : []),
    ),
  );
  return {
    ...env,
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: "/dev/null",
    GIT_TERMINAL_PROMPT: "0",
  };
}

function repositoryUrl(repository: string): string {
  return `https://github.com/${repository}.git`;
}

async function remoteHead(
  workspace: string,
  repository: string,
  branch: string,
  env: NodeJS.ProcessEnv,
): Promise<string> {
  const result = await run(
    "git",
    ["ls-remote", repositoryUrl(repository), `refs/heads/${branch}`],
    { cwd: workspace, env },
  );
  return result.stdout.trim().split(/\s+/)[0] ?? "";
}

async function checkout(
  workspace: string,
  repository: string,
  baseRef: string,
  headRef: string,
  env: NodeJS.ProcessEnv,
): Promise<void> {
  await run("git", ["init", "--initial-branch=review"], { cwd: workspace, env });
  await run("git", ["remote", "add", "origin", repositoryUrl(repository)], { cwd: workspace, env });
  await run(
    "git",
    [
      "fetch",
      "--no-tags",
      "origin",
      `+refs/heads/${baseRef}:refs/remotes/origin/${baseRef}`,
      `+refs/heads/${headRef}:refs/remotes/origin/${headRef}`,
    ],
    { cwd: workspace, env },
  );
  await run("git", ["checkout", "-B", headRef, `refs/remotes/origin/${headRef}`], { cwd: workspace, env });
  await run("git", ["config", "user.name", "agent-codex-reviewer"], { cwd: workspace, env });
  await run("git", ["config", "user.email", "agent-codex-reviewer@users.noreply.github.com"], { cwd: workspace, env });
}

async function codexVerdict(
  codex: Codex,
  workspace: string,
  prompt: string,
): Promise<ReviewVerdict> {
  const thread = codex.startThread({
    workingDirectory: workspace,
    model: process.env.CODEX_REVIEWER_MODEL ?? "openai.gpt-5.6-sol",
    modelReasoningEffort: "high",
    sandboxMode: "read-only",
    approvalPolicy: "never",
    networkAccessEnabled: false,
    webSearchMode: "disabled",
    threadSource: "adp-agent-codex-reviewer",
  });
  const turn = await thread.run(prompt, {
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
    model: process.env.CODEX_REVIEWER_MODEL ?? "openai.gpt-5.6-sol",
    modelReasoningEffort: "high",
    sandboxMode: "workspace-write",
    approvalPolicy: "never",
    networkAccessEnabled: false,
    webSearchMode: "disabled",
    threadSource: "adp-agent-codex-reviewer-fix",
  });
  await thread.run(fixPrompt(findings), {
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
  const forbidden = files.find((file) => FORBIDDEN_AUTOFIX_PATHS.some((pattern) => pattern.test(file)));
  if (forbidden) throw new Error(`Codex autofix touched protected path ${forbidden}`);
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

async function publishVerdict(
  github: GitHubClient,
  prNumber: number,
  verdict: ReviewVerdict,
  sha: string,
): Promise<void> {
  const body = formatReviewComment(verdict, sha, `Codex SDK ${SDK_VERSION}`);
  await github.comment(prNumber, body);
}

export async function runReview(envelope: CodexReviewEnvelope): Promise<ReviewRunResult> {
  const gatewayEndpoint = process.env.ADP_GATEWAY_ENDPOINT;
  const proxyTarget = process.env.SIGV4_PROXY_TARGET;
  if (!gatewayEndpoint || !proxyTarget) throw new Error("ADP_GATEWAY_ENDPOINT and SIGV4_PROXY_TARGET are required");
  const region = process.env.AWS_REGION ?? "us-east-1";
  const broker = new TokenBroker(
    gatewayEndpoint,
    region,
    envelope.installation_id,
    envelope.repository,
    envelope.message_id,
  );
  const github = new GitHubClient(envelope.repository, () => broker.getToken());
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

  const token = await broker.getToken();
  const gitEnv = gitEnvironment(token);
  const workspace = await mkdtemp(join(tmpdir(), "agent-codex-reviewer-"));
  const proxy = await startGatewayProxy({
    target: proxyTarget,
    region,
    tenantId: envelope.tenant_id,
    invocationId: envelope.message_id,
  });
  try {
    await checkout(
      workspace,
      envelope.repository,
      envelope.pull_request.base_ref,
      envelope.pull_request.head_ref,
      gitEnv,
    );
    const checkedOutSha = (
      await run("git", ["rev-parse", "HEAD"], { cwd: workspace })
    ).stdout.trim();
    if (checkedOutSha !== expected) {
      return { status: "stale", expected, actual: checkedOutSha };
    }
    if (
      (await remoteHead(
        workspace,
        envelope.repository,
        envelope.pull_request.head_ref,
        gitEnv,
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
      baseUrl: proxy.baseUrl,
      apiKey: "sigv4-proxy-placeholder",
      env: childEnvironment(),
    });
    let verdict = await codexVerdict(
      codex,
      workspace,
      reviewerPrompt(persona, issue, initialPr, expected),
    );

    const current = await remoteHead(
      workspace,
      envelope.repository,
      envelope.pull_request.head_ref,
      gitEnv,
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
        workspace,
        envelope.repository,
        envelope.pull_request.head_ref,
        gitEnv,
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
      await run(
        "git",
        [
          "push",
          `--force-with-lease=refs/heads/${envelope.pull_request.head_ref}:${expected}`,
          repositoryUrl(envelope.repository),
          `HEAD:refs/heads/${envelope.pull_request.head_ref}`,
        ],
        { cwd: workspace, env: gitEnv },
      );
      await github.comment(
        envelope.pull_request.number,
        `${formatFixesPushedComment(verdict, newSha, `Codex SDK ${SDK_VERSION}`)}\nMechanical fixes were pushed directly to \`${envelope.pull_request.head_ref}\`. This run will not merge; the resulting synchronize event must receive a fresh current-head review.`,
      );
      return { status: "fixes_pushed", sha: newSha };
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
    if ((process.env.CODEX_REVIEWER_MERGE_ENABLED ?? "false") !== "true") {
      return { status: "approved", sha: expected };
    }
    const mergeSha = await github.merge(envelope.pull_request.number, expected);
    return { status: "merged", sha: expected, mergeSha };
  } finally {
    await proxy.close().catch(() => undefined);
    await rm(workspace, { recursive: true, force: true });
  }
}
