/**
 * Credentials API service — AWS account connect flow + credential management.
 *
 * Issue #562: Self-serve AWS account connect UI.
 */

import { apiClient } from './api';

export function registerProfileCredential(operationId: string, data: {
  service: string; label: string; credential_type: 'basic_auth' | 'bearer' | 'oauth_token' | 'api_key';
  value: string; scope_hint: 'user'; scopes?: { oauth_scopes: string[] };
}) {
  return apiClient.put<CredentialItem>(`/auth/credentials/${encodeURIComponent(operationId)}`, data);
}

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
  provider?: string | null;
  scope: string;
  scopes: Record<string, string> | null;
  expires_at: string | null;
  last_used_at: string | null;
  strict: boolean;
  agent_consent: boolean;
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

export async function setCredentialAgentConsent(id: string, agentConsent: boolean): Promise<CredentialItem> {
  return apiClient.put<CredentialItem>(`/auth/credentials/${encodeURIComponent(id)}/agent-consent`, {
    agent_consent: agentConsent,
  });
}

export type ConsentMode = 'default' | 'explicit';
export type ConsentPosture = 'granted' | 'explicit';

export interface ToolsConsent {
  allowed_tools: string[];
  consented_tools: string[];
  revision: number;
  /** #8387: absent on older gateways, which only know the explicit opt-in contract. */
  consent_mode?: ConsentMode;
  opted_out_tools?: string[];
  tenant_consent_default?: ConsentPosture;
  entitled_tools?: string[];
}

export async function getToolsConsent(): Promise<ToolsConsent> {
  return apiClient.get<ToolsConsent>('/auth/tools/consent');
}

export async function setToolsConsent(consentedTools: string[], expectedRevision: number): Promise<ToolsConsent> {
  return apiClient.put<ToolsConsent>('/auth/tools/consent', { consented_tools: consentedTools, expected_revision: expectedRevision });
}

/** #8387: keep (or return to) the organization default and opt out of the named tools only. */
export async function setToolsConsentDefault(optedOutTools: string[], expectedRevision: number): Promise<ToolsConsent> {
  return apiClient.put<ToolsConsent>('/auth/tools/consent', {
    consent_mode: 'default', opted_out_tools: optedOutTools, expected_revision: expectedRevision,
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
