import type { ReviewVerdict } from "./contracts.js";

interface PullRequestResponse {
  number: number;
  state: string;
  merged?: boolean;
  merge_commit_sha?: string | null;
  html_url: string;
  title: string;
  body: string | null;
  draft: boolean;
  mergeable: boolean | null;
  mergeable_state: string;
  head: { ref: string; sha: string; repo?: { full_name: string } };
  base: { ref: string; sha: string; repo?: { full_name: string } };
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
  comments?: number;
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

export class GitHubRequestError extends Error {
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
    private readonly tokenProvider: (force?: boolean) => Promise<string>,
  ) {}

  private async request<T>(path: string, init: RequestInit = {}, refreshed = false): Promise<T> {
    if (!isRootedPath(path)) {
      throw new Error(`GitHub request path must start with "/": ${path}`);
    }
    const repositoryParts = this.repository.split("/");
    if (repositoryParts.length !== 2 || !repositoryParts.every(part =>
      /^[a-z0-9_.-]+$/i.test(part) && part !== "." && part !== "..")) {
      throw new Error("GitHub repository must be a single owner/name pair");
    }
    const token = await this.tokenProvider(refreshed);
    const options: RequestInit = {
      ...init,
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${token}`,
        "x-github-api-version": "2022-11-28",
        "content-type": "application/json",
        ...(init.headers ?? {}),
      },
      signal: init.signal ?? AbortSignal.timeout(30_000),
      redirect: "manual",
    };
    let url = `${GITHUB_API_ORIGIN}${path}`;
    let response: Response;
    for (let redirects = 0; ; redirects += 1) {
      response = await fetch(url, options);
      if (![301, 302, 303, 307, 308].includes(response.status)) break;
      const location = response.headers.get("location");
      await response.body?.cancel();
      if (!location || redirects >= 3) throw new Error("GitHub redirect unavailable or limit exceeded");
      const target = new URL(location, url);
      if (target.origin !== GITHUB_API_ORIGIN || target.username || target.password) {
        throw new Error("GitHub redirect must remain on api.github.com");
      }
      // Do not silently rewrite or replay mutations after method-changing redirects.
      // 307/308 explicitly preserve the original method/body for renamed resources.
      if (!["GET", "HEAD"].includes(options.method ?? "GET") && [301, 302, 303].includes(response.status)) {
        throw new Error("GitHub mutation requires a method-preserving redirect");
      }
      url = target.href;
    }
    // Retain a final-origin defense as well as validating every hop before transit.
    if (response.url && new URL(response.url).origin !== GITHUB_API_ORIGIN) {
      throw new Error(
        `GitHub ${init.method ?? "GET"} ${path} was redirected off api.github.com`,
      );
    }
    // A rejected credential did not authorize the request. Refresh once for
    // an explicit 401 only. Network errors and lost write replies are never
    // replayed here; they retain operation-specific reconciliation.
    if (response.status === 401 && !refreshed) {
      await response.body?.cancel();
      return this.request<T>(path, init, true);
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

  getBranch(ref: string): Promise<{ commit: { sha: string } }> {
    return this.request(`/repos/${this.repository}/branches/${encodeURIComponent(ref)}`);
  }

  getIssue(number: number): Promise<IssueResponse> {
    return this.request(`/repos/${this.repository}/issues/${number}`);
  }

  /** Controller-owned PR description update (task board section). */
  async updatePullRequestBody(number: number, body: string): Promise<void> {
    await this.request(`/repos/${this.repository}/pulls/${number}`, { method: "PATCH", body: JSON.stringify({ body }) });
  }

  /** Recent persisted plans are task context, never verified acceptance evidence. */
  async taskChecklists(number: number, total = 0): Promise<string[]> {
    if (!Number.isSafeInteger(total) || total <= 0) return [];
    const last = Math.ceil(total / 100);
    const comments: IssueCommentResponse[] = [];
    for (let page = Math.max(1, last - 1); page <= last; page++) {
      comments.push(...await this.request<IssueCommentResponse[]>(
        `/repos/${this.repository}/issues/${number}/comments?per_page=100&page=${page}`));
    }
    return comments.filter(comment => comment.body?.includes('### Task checklist')).slice(-3)
      .map(comment => comment.body!.slice(comment.body!.indexOf('### Task checklist')).split(/\n#{1,3} /)[0]!.slice(0, 8192));
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

  async markReady(nodeId: string): Promise<void> {
    const result = await this.request<{ errors?: unknown; data?: { markPullRequestReadyForReview?: { pullRequest?: { isDraft: boolean } } } }>("/graphql", {
      method: "POST", body: JSON.stringify({
        query: "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{isDraft}}}",
        variables: { id: nodeId },
      }),
    });
    if (result.errors || result.data?.markPullRequestReadyForReview?.pullRequest?.isDraft !== false) {
      throw new Error("Recovery PR readiness was not acknowledged");
    }
  }

  async merge(number: number, sha: string, method: "squash" | "merge" | "rebase" = "squash"): Promise<string> {
    const result = await this.request<{ merged: boolean; message: string; sha?: string }>(
      `/repos/${this.repository}/pulls/${number}/merge`,
      {
        method: "PUT",
        body: JSON.stringify({ sha, merge_method: method }),
      },
    );
    if (!result.merged) throw new Error(`GitHub refused merge: ${result.message}`);
    return result.sha ?? "";
  }

  async queueEntry(nodeId: string, sha: string): Promise<string | null> {
    const result = await this.request<{ errors?: unknown; data?: { node?: { headRefOid: string; mergeQueueEntry: { id: string } | null } } }>("/graphql", {
      method: "POST", body: JSON.stringify({
        query: "query($id:ID!) { node(id:$id) { ... on PullRequest { headRefOid mergeQueueEntry { id } } } }",
        variables: { id: nodeId },
      }),
    });
    if (result.errors || result.data?.node?.headRefOid !== sha) throw new Error("Merge queue observation unavailable or head changed");
    return result.data.node.mergeQueueEntry?.id ?? null;
  }

  async enqueue(nodeId: string, sha: string, operation: string): Promise<void> {
    const result = await this.request<{ errors?: unknown; data?: { enqueuePullRequest?: { mergeQueueEntry?: { id: string } } } }>("/graphql", {
      method: "POST", body: JSON.stringify({
        query: "mutation($input:EnqueuePullRequestInput!) { enqueuePullRequest(input:$input) { mergeQueueEntry { id } } }",
        variables: { input: { pullRequestId: nodeId, expectedHeadOid: sha, clientMutationId: operation } },
      }),
    });
    if (result.errors || !result.data?.enqueuePullRequest?.mergeQueueEntry?.id) throw new Error("Merge queue admission outcome unknown");
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
