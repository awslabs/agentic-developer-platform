/**
 * Minimal GitLab API client for agent-worker operations.
 *
 * Supports: post issue comment, create branch, create merge request, get file.
 * Uses native fetch — no external HTTP library required.
 *
 * Issue #3325: Agent GitLabClient — Minimal Operations
 */

import {
  GitLabClientConfig,
  CreateMergeRequestOptions,
  MergeRequestResult,
} from './gitlab_types';
import { validateBaseUrl } from '../lib/url-guard';

export class GitLabClient {
  private readonly baseUrl: string;
  private readonly accessToken: string;

  constructor(config: GitLabClientConfig) {
    // SSRF guard: validateBaseUrl returns the normalized origin, breaking
    // semgrep's taint path from config.baseUrl → fetch() (#3582, #3713).
    // allowHttp: GitLab base URL is a configured internal host (e.g. http://gitlab.dev.adp.internal).
    this.baseUrl = validateBaseUrl(config.baseUrl, { allowHttp: true }).replace(/\/$/, '');
    this.accessToken = config.accessToken;
  }

  /**
   * Post a comment (note) on a GitLab issue.
   * POST /api/v4/projects/:id/issues/:iid/notes
   */
  async postIssueComment(projectId: number, issueIid: number, body: string): Promise<void> {
    const url = `${this.baseUrl}/api/v4/projects/${projectId}/issues/${issueIid}/notes`;
    const resp = await fetch(url, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({ body }),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(
        `GitLab POST /projects/${projectId}/issues/${issueIid}/notes failed (${resp.status}): ${text}`,
      );
    }
  }

  /**
   * Create a new branch from a ref.
   * POST /api/v4/projects/:id/repository/branches
   */
  async createBranch(projectId: number, branchName: string, ref: string): Promise<void> {
    const url = `${this.baseUrl}/api/v4/projects/${projectId}/repository/branches`;
    const resp = await fetch(url, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({ branch: branchName, ref }),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(
        `GitLab POST /projects/${projectId}/repository/branches failed (${resp.status}): ${text}`,
      );
    }
  }

  /**
   * Create a merge request.
   * POST /api/v4/projects/:id/merge_requests
   */
  async createMergeRequest(
    projectId: number,
    options: CreateMergeRequestOptions,
  ): Promise<MergeRequestResult> {
    const url = `${this.baseUrl}/api/v4/projects/${projectId}/merge_requests`;
    const resp = await fetch(url, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({
        source_branch: options.sourceBranch,
        target_branch: options.targetBranch,
        title: options.title,
        description: options.description,
      }),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(
        `GitLab POST /projects/${projectId}/merge_requests failed (${resp.status}): ${text}`,
      );
    }
    const data = (await resp.json()) as { iid: number; web_url: string };
    return { iid: data.iid, web_url: data.web_url };
  }

  /**
   * Get file content from a repository (base64 decoded).
   * GET /api/v4/projects/:id/repository/files/:path?ref=:ref
   */
  async getFile(projectId: number, filePath: string, ref: string): Promise<string> {
    const encodedPath = encodeURIComponent(filePath);
    const url = `${this.baseUrl}/api/v4/projects/${projectId}/repository/files/${encodedPath}?ref=${encodeURIComponent(ref)}`;
    const resp = await fetch(url, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(
        `GitLab GET /projects/${projectId}/repository/files/${filePath} failed (${resp.status}): ${text}`,
      );
    }
    const data = (await resp.json()) as { content: string };
    return Buffer.from(data.content, 'base64').toString('utf-8');
  }

  private headers(): Record<string, string> {
    return {
      'PRIVATE-TOKEN': this.accessToken,
      'Content-Type': 'application/json',
    };
  }
}
