/**
 * TypeScript types for GitLab API responses and client configuration.
 *
 * Issue #3325: Agent GitLabClient — Minimal Operations
 */

/** Configuration for GitLabClient constructor. */
export interface GitLabClientConfig {
  /** GitLab origin; must match the operator-configured GITLAB_URL environment variable */
  baseUrl: string;
  /** Group Access Token for authentication */
  accessToken: string;
}

/** Options for creating a merge request. */
export interface CreateMergeRequestOptions {
  sourceBranch: string;
  targetBranch: string;
  title: string;
  description?: string;
}

/** Response from merge request creation. */
export interface MergeRequestResult {
  iid: number;
  web_url: string;
}

/** GitLab API error response body shape. */
export interface GitLabApiError {
  message?: string | string[] | Record<string, string[]>;
  error?: string;
}
