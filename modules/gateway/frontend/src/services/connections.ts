/**
 * Connections API service — GitHub App install + management.
 *
 * Issue #465: Gateway admin Connections page.
 */

import { apiClient } from './api';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface InstallStartResponse {
  install_url: string;
  state_token: string;
  expires_at: string;
}

export interface GitHubConnectionItem {
  provider: string;
  installation_id: number;
  account_login: string;
  account_type: string;
  repository_selection: string;
  repository_count: number;
  repositories: string[];
  installed_at: string | null;
  configure_url: string;
}

export interface ConnectionsListResponse {
  connections: GitHubConnectionItem[];
}

export interface DeleteConnectionResponse {
  deleted: boolean;
  installation_id: number;
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

/**
 * Start the GitHub App install flow.
 * Returns the install_url the user should be redirected to.
 */
export async function startGitHubInstall(): Promise<InstallStartResponse> {
  return apiClient.post<InstallStartResponse>(
    '/admin/connections/github/install-start',
    {},
  );
}

/**
 * List all GitHub installations connected to the current tenant.
 */
export async function listConnections(): Promise<ConnectionsListResponse> {
  return apiClient.get<ConnectionsListResponse>('/admin/connections');
}

/**
 * Disconnect (delete) a GitHub App installation from the current tenant.
 * Requires admin role.
 */
export async function deleteGitHubConnection(
  installationId: number,
): Promise<DeleteConnectionResponse> {
  return apiClient.delete<DeleteConnectionResponse>(
    `/admin/connections/github/${installationId}`,
  );
}
