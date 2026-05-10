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
