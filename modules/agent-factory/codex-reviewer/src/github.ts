import type { ReviewVerdict } from "./contracts.js";

interface PullRequestResponse {
  number: number;
  state: string;
  html_url: string;
  title: string;
  body: string | null;
  draft: boolean;
  mergeable: boolean | null;
  mergeable_state: string;
  head: { ref: string; sha: string };
  base: { ref: string; sha: string };
}

export function formatFixesPushedComment(
  verdict: ReviewVerdict,
  sha: string,
  engine: string,
): string {
  return formatReviewComment(verdict, sha, engine).replace(
    "## agent-codex-reviewer — APPROVE",
    "## agent-codex-reviewer — FIXES PUSHED AND APPROVED",
  );
}

export function formatIssueReviewComment(
  verdict: ReviewVerdict,
  issueNumber: number,
  engine: string,
): string {
  const blockers = verdict.findings.filter((finding) => finding.blocking);
  const lines = [
    `## agent-codex-reviewer — ${verdict.verdict === "approve" ? "ISSUE READY" : "ISSUE CHANGES REQUESTED"}`,
    "",
    `**Reviewed issue:** #${issueNumber}`,
    `**Blockers:** ${blockers.length}`,
    `**Engine:** ${engine}`,
    "",
    verdict.summary,
  ];
  for (const finding of verdict.findings) {
    const location = finding.file
      ? `${finding.file}${finding.line ? `:${finding.line}` : ""}`
      : "Issue-wide";
    lines.push(
      "",
      `### ${finding.id}: ${finding.title}`,
      "",
      `**Impact:** ${finding.impact} · **Confidence:** ${finding.confidence} · **Approval:** ${finding.blocking ? "blocker" : "non-blocking"} · **Owner:** ${finding.fixClass}`,
      "",
      finding.details,
      "",
      `**Location:** \`${location}\``,
      "",
      `**Recommended fix:** ${finding.recommendedFix}`,
    );
  }
  if (verdict.validationGaps.length > 0) {
    lines.push("", "### Validation gaps", "", ...verdict.validationGaps.map((gap) => `- ${gap}`));
  }
  return `${lines.join("\n")}\n`;
}

interface IssueResponse {
  number: number;
  title: string;
  body: string | null;
}

interface IssueCommentResponse {
  body: string | null;
}

interface CheckRunsResponse {
  check_runs: Array<{ name: string; status: string; conclusion: string | null }>;
}

interface CombinedStatusResponse {
  state: string;
  statuses: Array<{ context: string; state: string }>;
}

export interface ChecksState {
  ready: boolean;
  failing: string[];
  pending: string[];
  total: number;
}

// This live-fleet diagnostic is intentionally not a required merge context:
// it measures shared dev-worker capacity and remains red when a healthy route
// is merely queued past its SLA. Keep this in sync with the policy documented
// in .github/workflows/gitlab-integration-tests.yml.
const NON_BLOCKING_CHECKS = new Set(["GitLab Live Fleet"]);

class GitHubRequestError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** The only origin this client talks to. */
const GITHUB_API_ORIGIN = "https://api.github.com";

/**
 * Every request path must be rooted at `/`.
 *
 * `request` builds its URL as `${GITHUB_API_ORIGIN}${path}`, which only stays
 * on api.github.com while `path` starts with a slash. Without that leading
 * slash the origin escapes: `@evil.com/x` resolves to `https://evil.com`, and
 * `.evil.com/x` to `https://api.github.com.evil.com` — and since every request
 * carries a GitHub installation token, that would be a credential-bearing SSRF.
 *
 * All current callers pass a `/`-rooted template, so this rejects nothing that
 * is sent today; it exists so that a future caller cannot reintroduce the
 * escape by omitting the slash.
 *
 * Expressed with string comparisons rather than a regex: the check is exact,
 * and it keeps a path-shaped value away from any regex engine.
 */
function isRootedPath(path: string): boolean {
  // A protocol-relative `//host` path is also rejected: it keeps the scheme but
  // replaces the host, so `//evil.com/x` would resolve to https://evil.com.
  return path.startsWith("/") && !path.startsWith("//");
}

export class GitHubClient {
  constructor(
    private readonly repository: string,
    private readonly tokenProvider: () => Promise<string>,
  ) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!isRootedPath(path)) {
      throw new Error(`GitHub request path must start with "/": ${path}`);
    }
    const token = await this.tokenProvider();
    const response = await fetch(`${GITHUB_API_ORIGIN}${path}`, {
      ...init,
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${token}`,
        "x-github-api-version": "2022-11-28",
        "content-type": "application/json",
        ...(init.headers ?? {}),
      },
      signal: init.signal ?? AbortSignal.timeout(30_000),
    });
    // A cross-origin redirect cannot leak the token (Node strips Authorization
    // when the origin changes) but it can substitute the response body — and
    // these bodies drive the merge gate in `checks()`. Confirm the response
    // actually came from GitHub. Same-origin redirects, which GitHub issues for
    // renamed repositories, are unaffected.
    if (response.url && new URL(response.url).origin !== GITHUB_API_ORIGIN) {
      throw new Error(
        `GitHub ${init.method ?? "GET"} ${path} was redirected off api.github.com`,
      );
    }
    if (!response.ok) {
      throw new GitHubRequestError(
        response.status,
        `GitHub ${init.method ?? "GET"} ${path} returned ${response.status}: ${await response.text()}`,
      );
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  getPullRequest(number: number): Promise<PullRequestResponse> {
    return this.request(`/repos/${this.repository}/pulls/${number}`);
  }

  getIssue(number: number): Promise<IssueResponse> {
    return this.request(`/repos/${this.repository}/issues/${number}`);
  }

  async comment(number: number, body: string): Promise<void> {
    await this.request(`/repos/${this.repository}/issues/${number}/comments`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
  }

  async commentOnce(number: number, marker: string, body: string): Promise<boolean> {
    const comments = await this.request<IssueCommentResponse[]>(
      `/repos/${this.repository}/issues/${number}/comments?per_page=100&sort=created&direction=desc`,
    );
    if (comments.some((comment) => comment.body?.includes(marker))) return false;
    await this.comment(number, `${marker}\n${body}`);
    return true;
  }

  async checks(sha: string): Promise<ChecksState> {
    const [checks, statuses] = await Promise.all([
      this.request<CheckRunsResponse>(
        `/repos/${this.repository}/commits/${sha}/check-runs?per_page=100`,
      ),
      this.request<CombinedStatusResponse>(
        `/repos/${this.repository}/commits/${sha}/status?per_page=100`,
      ).catch((error: unknown) => {
        // The reused developer installation can read check runs but does not
        // necessarily have legacy commit-status permission. GitHub's merge API
        // remains the final authority for every branch-protection requirement.
        if (error instanceof GitHubRequestError && error.status === 403) {
          return { state: "pending", statuses: [] };
        }
        throw error;
      }),
    ]);
    const failing: string[] = [];
    const pending: string[] = [];
    const blockingChecks = checks.check_runs.filter(
      (check) => !NON_BLOCKING_CHECKS.has(check.name),
    );
    const blockingStatuses = statuses.statuses.filter(
      (status) => !NON_BLOCKING_CHECKS.has(status.context),
    );
    for (const check of blockingChecks) {
      if (check.status !== "completed") pending.push(check.name);
      else if (!["success", "neutral", "skipped"].includes(check.conclusion ?? "")) {
        failing.push(check.name);
      }
    }
    for (const status of blockingStatuses) {
      if (status.state === "pending") pending.push(status.context);
      else if (status.state !== "success") failing.push(status.context);
    }
    const total = blockingChecks.length + blockingStatuses.length;
    return { ready: total > 0 && failing.length === 0 && pending.length === 0, failing, pending, total };
  }

  async merge(number: number, sha: string): Promise<string> {
    const result = await this.request<{ merged: boolean; message: string; sha?: string }>(
      `/repos/${this.repository}/pulls/${number}/merge`,
      {
        method: "PUT",
        body: JSON.stringify({ sha, merge_method: "squash" }),
      },
    );
    if (!result.merged) throw new Error(`GitHub refused merge: ${result.message}`);
    return result.sha ?? "";
  }
}

export function formatReviewComment(
  verdict: ReviewVerdict,
  sha: string,
  engine: string,
): string {
  const blockers = verdict.findings.filter((finding) => finding.blocking);
  const lines = [
    `## agent-codex-reviewer — ${verdict.verdict === "approve" ? "APPROVE" : "REQUEST CHANGES"}`,
    "",
    `**Reviewed head:** \`${sha}\``,
    `**Blockers:** ${blockers.length}`,
    `**Engine:** ${engine}`,
    "",
    verdict.summary,
  ];
  for (const finding of verdict.findings) {
    const location = finding.file
      ? `${finding.file}${finding.line ? `:${finding.line}` : ""}`
      : "PR-wide";
    lines.push(
      "",
      `### ${finding.id}: ${finding.title}`,
      "",
      `**Impact:** ${finding.impact} · **Confidence:** ${finding.confidence} · **Approval:** ${finding.blocking ? "blocker" : "non-blocking"} · **Owner:** ${finding.fixClass}`,
      "",
      `${finding.details}`,
      "",
      `**Location:** \`${location}\``,
      "",
      `**Recommended fix:** ${finding.recommendedFix}`,
    );
  }
  if (verdict.validationGaps.length > 0) {
    lines.push("", "### Validation gaps", "", ...verdict.validationGaps.map((gap) => `- ${gap}`));
  }
  return `${lines.join("\n")}\n`;
}
