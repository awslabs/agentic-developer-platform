/**
 * Agent Management API Service
 *
 * Issue #119: Unified Cognito JWT Auth
 * - Agents authenticate via client_credentials flow
 * - Each agent is a Cognito App Client with client_id and client_secret
 */

import { apiClient } from './api';

// Types
export interface Agent {
  client_id: string;
  name: string;
  org_id: string;
  team_id?: string;
  department_id?: string;
  description?: string;
  scopes: string[];
  created_at: string;
  updated_at?: string;
  status: 'active' | 'disabled';
}

export interface AgentCredentials {
  client_id: string;
  client_secret: string;
  token_endpoint: string;
  scopes: string[];
  example_curl: string;
}

export interface CreateAgentRequest {
  name: string;
  org_id: string;
  team_id?: string;
  department_id?: string;
  description?: string;
  scopes?: string[];
}

export interface UpdateAgentRequest {
  name?: string;
  team_id?: string;
  department_id?: string;
  description?: string;
  status?: 'active' | 'disabled';
}

export interface AgentListResponse {
  items: Agent[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

// API Functions

/**
 * Create a new agent (Cognito App Client)
 */
export async function createAgent(request: CreateAgentRequest): Promise<Agent> {
  return apiClient.post<Agent>('/admin/agents', request);
}

/**
 * List agents for an organization
 */
export async function listAgents(
  orgId?: string,
  page: number = 1,
  pageSize: number = 50
): Promise<AgentListResponse> {
  const params = new URLSearchParams();
  if (orgId) params.append('org_id', orgId);
  params.append('page', page.toString());
  params.append('page_size', pageSize.toString());

  return apiClient.get<AgentListResponse>(`/admin/agents?${params.toString()}`);
}

/**
 * Get agent details by client ID
 */
export async function getAgent(clientId: string): Promise<Agent> {
  return apiClient.get<Agent>(`/admin/agents/${clientId}`);
}

/**
 * Get agent credentials (client_id and client_secret)
 * WARNING: This is typically a one-time operation. Store the secret securely.
 */
export async function getAgentCredentials(clientId: string): Promise<AgentCredentials> {
  return apiClient.get<AgentCredentials>(`/admin/agents/${clientId}/credentials`);
}

/**
 * Update agent metadata
 */
export async function updateAgent(
  clientId: string,
  request: UpdateAgentRequest
): Promise<Agent> {
  return apiClient.put<Agent>(`/admin/agents/${clientId}`, request);
}

/**
 * Delete an agent
 */
export async function deleteAgent(clientId: string): Promise<void> {
  await apiClient.delete(`/admin/agents/${clientId}`);
}
