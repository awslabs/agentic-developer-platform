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
    "## agent-codex-reviewer — FIXES PUSHED; FRESH REVIEW REQUIRED",
  );
}

interface IssueResponse {
  number: number;
  title: string;
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

export class GitHubClient {
  constructor(
    private readonly repository: string,
    private readonly tokenProvider: () => Promise<string>,
  ) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const token = await this.tokenProvider();
    const response = await fetch(`https://api.github.com${path}`, {
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
    if (!response.ok) {
      throw new Error(
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

  async checks(sha: string): Promise<ChecksState> {
    const [checks, statuses] = await Promise.all([
      this.request<CheckRunsResponse>(
        `/repos/${this.repository}/commits/${sha}/check-runs?per_page=100`,
      ),
      this.request<CombinedStatusResponse>(
        `/repos/${this.repository}/commits/${sha}/status?per_page=100`,
      ),
    ]);
    const failing: string[] = [];
    const pending: string[] = [];
    for (const check of checks.check_runs) {
      if (check.status !== "completed") pending.push(check.name);
      else if (!["success", "neutral", "skipped"].includes(check.conclusion ?? "")) {
        failing.push(check.name);
      }
    }
    for (const status of statuses.statuses) {
      if (status.state === "pending") pending.push(status.context);
      else if (status.state !== "success") failing.push(status.context);
    }
    const total = checks.check_runs.length + statuses.statuses.length;
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
