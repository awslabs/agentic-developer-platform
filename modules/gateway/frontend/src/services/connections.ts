/**
 * Connections API service — GitHub App install + management.
 *
 * Issue #465: Gateway admin Connections page.
 * Issue #2596: GitHub App registration + lifecycle (platform_admin gated).
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
  /** Deep-link to GitHub's installation repository management page. Issue #2983. */
  manage_url: string;
  /** Issue #3073: Whether the caller can manage (disconnect) this connection. */
  can_manage: boolean;
  /** Issue #3018: Tenant ID that owns this connection (multi-tenant mode). */
  tenant_id?: string | null;
  /** Issue #3018: Display name of the owning tenant (multi-tenant mode). */
  tenant_name?: string | null;
  /** Issue #3018: Whether this connection belongs to the caller's active tenant. */
  is_active_tenant?: boolean | null;
}

export interface ConnectionsListResponse {
  connections: GitHubConnectionItem[];
}

export interface DeleteConnectionResponse {
  deleted: boolean;
  installation_id: number;
}

// ---------------------------------------------------------------------------
// GitHub App registration types (Issue #2596)
// ---------------------------------------------------------------------------

export interface RegisterAppStartRequest {
  owner_type: 'user' | 'org';
  org?: string;
}

export interface RegisterAppStartResponse {
  status: 'ready' | 'already_registered';
  manifest: Record<string, unknown> | null;
  post_url: string | null;
  state: string | null;
  app_slug: string | null;
  app_id: string | null;
}

export interface AppStatusResponse {
  registered: boolean;
  /** Whether "Sign in with GitHub" is wired (broker OAuth secret populated). Issue #2708. */
  login_enabled?: boolean;
  app_slug: string | null;
  app_id: string | null;
  owner_type: string | null;
  created_at: string | null;
}

export interface RotateKeyResponse {
  rotated: boolean;
  app_id: string | null;
  message: string;
}

export interface DisconnectAppResponse {
  disconnected: boolean;
  app_id: string | null;
  message: string;
  affected_installations: number;
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

// ---------------------------------------------------------------------------
// Switch tenant (Issue #3071: one-click workspace switching)
// ---------------------------------------------------------------------------

export interface SwitchTenantResponse {
  active_tenant_id: string;
}

/**
 * Switch the caller's active tenant (workspace).
 * After switching, refetch connections to reflect the new active state.
 */
export async function switchTenant(tenantId: string): Promise<SwitchTenantResponse> {
  return apiClient.post<SwitchTenantResponse>(
    '/admin/connections/switch-tenant',
    { tenant_id: tenantId },
  );
}

// ---------------------------------------------------------------------------
// GitHub App registration + lifecycle (Issue #2596, platform_admin only)
// ---------------------------------------------------------------------------

/**
 * Get the current GitHub App registration status.
 * Platform admin only.
 */
export async function getGitHubAppStatus(): Promise<AppStatusResponse> {
  return apiClient.get<AppStatusResponse>('/admin/connections/github/app/status');
}

/**
 * Start the GitHub App registration (manifest flow).
 * Returns manifest + post_url to POST to GitHub in a new tab.
 * Platform admin only.
 */
export async function startGitHubAppRegistration(
  request: RegisterAppStartRequest,
): Promise<RegisterAppStartResponse> {
  return apiClient.post<RegisterAppStartResponse>(
    '/admin/connections/github/app/register-start',
    request,
  );
}

/**
 * Rotate the private key for the registered GitHub App.
 * Platform admin only.
 */
export async function rotateGitHubAppKey(): Promise<RotateKeyResponse> {
  return apiClient.post<RotateKeyResponse>(
    '/admin/connections/github/app/rotate-key',
    {},
  );
}

/**
 * Disconnect (unregister) the GitHub App entirely.
 * Platform admin only.
 */
export async function disconnectGitHubApp(): Promise<DisconnectAppResponse> {
  return apiClient.post<DisconnectAppResponse>(
    '/admin/connections/github/app/disconnect',
    {},
  );
}

// ---------------------------------------------------------------------------
// Manual App registration (Issue #3360)
// ---------------------------------------------------------------------------

export interface RegisterManualRequest {
  app_id: string;
  private_key: string;
  webhook_secret?: string;
  client_id?: string;
  client_secret?: string;
}

export interface RegisterManualResponse {
  registered: boolean;
  app_id: string;
  app_slug: string;
  app_name: string;
  login_enabled: boolean;
  warnings: string[];
}

/**
 * Register a GitHub App by manually providing App ID + private key.
 * Validates credentials against GitHub, stores them, returns config warnings.
 * Platform admin only.
 *
 * Issue #3360.
 */
export async function registerManualGitHubApp(
  request: RegisterManualRequest,
): Promise<RegisterManualResponse> {
  return apiClient.post<RegisterManualResponse>(
    '/admin/connections/github/app/register-manual',
    request,
  );
}
