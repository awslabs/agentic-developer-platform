/**
 * Credentials API service — AWS account connect flow + credential management.
 *
 * Issue #562: Self-serve AWS account connect UI.
 */

import { apiClient } from './api';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ConnectStartRequest {
  nickname: string;
  account_id: string;
  role_name?: string;
}

export interface ConnectStartResponse {
  credential_id: string;
  launch_url: string;
}

export interface ConnectVerifyRequest {
  credential_id: string;
}

export interface ConnectVerifyResponse {
  status: 'verified' | 'failed';
  reason?: string;
}

export interface CredentialItem {
  id: string;
  service: string;
  label: string;
  credential_type: string;
  scope: string;
  scopes: Record<string, string> | null;
  expires_at: string | null;
  last_used_at: string | null;
  strict: boolean;
  created_at: string;
  updated_at: string | null;
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

/**
 * Start the AWS account connect flow.
 * Returns a credential_id and a CloudFormation Quick-Create launch URL.
 */
export async function startAwsConnect(
  data: ConnectStartRequest,
): Promise<ConnectStartResponse> {
  return apiClient.post<ConnectStartResponse>(
    '/auth/credentials/aws/connect',
    data,
  );
}

/**
 * Verify that the CloudFormation stack was created and the role is assumable.
 */
export async function verifyAwsConnect(
  data: ConnectVerifyRequest,
): Promise<ConnectVerifyResponse> {
  return apiClient.post<ConnectVerifyResponse>(
    '/auth/credentials/aws/verify',
    data,
  );
}

/**
 * List all credentials for the current user.
 */
export async function listCredentials(): Promise<CredentialItem[]> {
  return apiClient.get<CredentialItem[]>('/auth/credentials');
}

/**
 * Delete a credential by ID.
 */
export async function deleteCredential(id: string): Promise<void> {
  return apiClient.delete<void>(`/auth/credentials/${id}`);
}

// ---------------------------------------------------------------------------
// GitHub PAT registration
// ---------------------------------------------------------------------------

export interface RegisterGitHubPatRequest {
  pat: string;
  expires_at?: string; // ISO date string (YYYY-MM-DD)
}

/**
 * Register a GitHub Personal Access Token in the vault.
 *
 * Uses fixed conventions per design note §2:
 *   service = "github"
 *   credential_type = "bearer"
 *   label = "github-pat"
 *   strict = true
 *   scope_hint = "user"
 */
export async function registerGitHubPat(
  data: RegisterGitHubPatRequest,
): Promise<CredentialItem> {
  return apiClient.post<CredentialItem>('/auth/credentials', {
    service: 'github',
    credential_type: 'bearer',
    label: 'github-pat',
    value: data.pat,
    strict: true,
    scope_hint: 'user',
    ...(data.expires_at ? { expires_at: data.expires_at } : {}),
  });
}

/**
 * Extract a user-friendly error message from the API error shape.
 *
 * The gateway returns errors as { detail: { error: string, message: string } }
 * or { detail: string } (FastAPI default for some cases). This helper handles
 * both shapes and provides a fallback.
 */
export function extractCredentialError(err: unknown): string {
  // Shape 1: { detail: { error, message } } — vault route errors
  const detail = (err as { detail?: { error?: string; message?: string } | string })?.detail;
  if (detail && typeof detail === 'object') {
    return detail.message || detail.error || 'An unexpected error occurred';
  }
  // Shape 2: { detail: string } — FastAPI generic
  if (typeof detail === 'string') {
    return detail;
  }
  // Shape 3: { message: string } — network/fetch errors
  const message = (err as { message?: string })?.message;
  if (message) {
    return message;
  }
  return 'An unexpected error occurred';
}
