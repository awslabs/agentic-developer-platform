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
  const resp = await apiClient.post<InstallStartResponse>(
    '/api/admin/connections/github/install-start',
    {},
  );
  return resp.data;
}

/**
 * List all GitHub installations connected to the current tenant.
 */
export async function listConnections(): Promise<ConnectionsListResponse> {
  const resp = await apiClient.get<ConnectionsListResponse>('/api/admin/connections');
  return resp.data;
}

/**
 * Disconnect (delete) a GitHub App installation from the current tenant.
 * Requires admin role.
 */
export async function deleteGitHubConnection(
  installationId: number,
): Promise<DeleteConnectionResponse> {
  const resp = await apiClient.delete<DeleteConnectionResponse>(
    `/api/admin/connections/github/${installationId}`,
  );
  return resp.data;
}
