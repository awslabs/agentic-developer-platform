import { apiClient } from './api';

// Types for the onboarding flow
export type AccessStatus = 'new' | 'pending' | 'denied' | 'registered';

export interface AccessStatusResponse {
  status: AccessStatus;
  request_id?: string;
  decision_note?: string;
}

export interface AccessRequestPayload {
  proposed_tenant_id: string;
  motivation: string;
}

export type AccessRequestOutcome = 'approved' | 'pending';

export interface AccessRequestResponse {
  outcome: AccessRequestOutcome;
  request_id?: string;
  redirect?: string;
  hint?: string;
  error?: string;
}

export interface AccessRequestItem {
  id: string;
  target_login: string;
  proposed_tenant_id: string;
  motivation: string;
  avatar_url?: string;
  created_at: string;
}

export interface AccessRequestListResponse {
  items: AccessRequestItem[];
}

/**
 * Get the current user's onboarding access status.
 */
export async function getAccessStatus(): Promise<AccessStatusResponse> {
  return apiClient.get<AccessStatusResponse>('/access/status');
}

/**
 * Submit an access request (onboarding form).
 * Returns the raw Response to allow checking status codes (200/202/400/409/503).
 */
export async function submitAccessRequest(
  payload: AccessRequestPayload
): Promise<Response> {
  const token = (await import('./auth')).getAccessToken();
  const baseUrl = import.meta.env.VITE_API_URL || '/api';
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return fetch(`${baseUrl}/access/request`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  });
}

/**
 * Get pending access requests (admin only).
 */
export async function getAccessRequests(): Promise<AccessRequestItem[]> {
  const response = await apiClient.get<AccessRequestListResponse>('/admin/access-requests');
  return Array.isArray(response?.items) ? response.items : [];
}

/**
 * Approve an access request (admin only).
 */
export async function approveAccessRequest(requestId: string): Promise<void> {
  await apiClient.post(`/admin/access-requests/${requestId}/approve`);
}

/**
 * Deny an access request (admin only).
 */
export async function denyAccessRequest(
  requestId: string,
  decisionNote?: string
): Promise<void> {
  await apiClient.post(`/admin/access-requests/${requestId}/deny`, {
    decision_note: decisionNote,
  });
}
