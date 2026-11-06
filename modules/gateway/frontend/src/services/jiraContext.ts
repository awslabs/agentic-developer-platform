import { apiClient } from './api';

export interface JiraSystemConnection {
  id: string; revision: string; label: string; site_id: string; account_id: string | null;
  product_ids: string[]; status: string; expires_at: string; verified_at: string | null;
  capabilities: string[];
  connection_method?: 'service_account' | 'forge_app'; installation_id?: string | null;
}
export interface JiraSystemInput {
  label: string; api_key: string; site_id: string; expires_at: string; product_ids: string[];
  expected_revision?: string;
}
const adminPath = '/auth/connections/atlassian-mcp/system';
export const listJiraSystemConnections = () => apiClient.get<{ connections: JiraSystemConnection[]; forge_available?: boolean }>(adminPath);
export const saveJiraSystemConnection = (body: JiraSystemInput, id?: string) =>
  apiClient.request<JiraSystemConnection>(id ? `${adminPath}/${encodeURIComponent(id)}` : adminPath, {
    method: id ? 'PUT' : 'POST', body,
  });
export const verifyJiraSystemConnection = (id: string) =>
  apiClient.post<JiraSystemConnection>(`${adminPath}/${encodeURIComponent(id)}/verify`, {});
export const disconnectJiraSystemConnection = (id: string) =>
  apiClient.delete<{ status: string; provider_cleanup_pending: boolean }>(`${adminPath}/${encodeURIComponent(id)}`);

export interface ForgePairing { connection: JiraSystemConnection; pairing_code: string; expires_at: string; install_url: string }
const forgePath = '/auth/connections/atlassian-mcp/forge';
export const startJiraForgePairing = (body: { label: string; product_ids: string[]; connection_id?: string; expected_revision?: string }) =>
  apiClient.post<ForgePairing>(`${forgePath}/pairings`, body);
export const updateJiraConnectionAccess = (id: string, body: { label: string; product_ids: string[]; expected_revision: string }) =>
  apiClient.request<JiraSystemConnection>(`${forgePath}/${encodeURIComponent(id)}/access`, { method: 'PUT', body });

export interface JiraConnection { id: string; label: string; site_id: string }
export interface JiraResource { id: string | number; key?: string; name: string; type?: string }
export interface JiraSource {
  id: string; revision: number; available?: boolean; connection_id: string; site_id: string; label: string;
  jira_project_id: string; jira_project_key: string; board_id: number | null; board_type?: string | null;
}
export interface JiraResourcePage { values: JiraResource[]; isLast?: boolean; total?: number; startAt?: number; maxResults?: number }
export type JiraView = 'issues' | 'backlog' | 'board' | 'sprints';
export interface JiraIssue { id: string; key: string; webUrl?: string; fields?: { summary?: string; description?: string; status?: { name?: string } } }
export interface JiraContextResult {
  data: { returnHint?: string; issues?: JiraIssue[]; values?: { id: number; name: string; state?: string; goal?: string }[]; name?: string; type?: string; goal?: string };
  source: JiraSource; view: JiraView; retrieved_at_ms: number; next_cursor: string | null; coverage: string;
}
const contextPath = (productId: string, projectId: string) => `/v1/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}/jira-context`;
export const jiraConnections = (p: string, j: string) => apiClient.get<{ connections: JiraConnection[] }>(`${contextPath(p, j)}/connections`);
export const jiraSources = (p: string, j: string) => apiClient.get<{ sources: JiraSource[] }>(`${contextPath(p, j)}/sources`);
export const jiraResources = (p: string, j: string, connection: string, kind: 'projects' | 'boards', query = '', startAt = 0) =>
  apiClient.get<{ data: JiraResourcePage }>(`${contextPath(p, j)}/connections/${encodeURIComponent(connection)}/resources?${new URLSearchParams({ kind, query, start_at: String(startAt) })}`);
export const selectJiraSource = (p: string, j: string, connection: string, project: JiraResource, boardId?: number) =>
  apiClient.post<JiraSource>(`${contextPath(p, j)}/sources`, {
    connection_id: connection, jira_project_id: String(project.id), jira_project_key: project.key,
    board_id: boardId ?? null, share_with_product: true,
  });
export const unlinkJiraSource = (p: string, j: string, source: JiraSource) =>
  apiClient.delete(`${contextPath(p, j)}/sources/${encodeURIComponent(source.id)}?revision=${source.revision}`);
export const readJiraSource = (p: string, j: string, source: JiraSource, view: JiraView, text?: string, cursor?: string) =>
  apiClient.post<JiraContextResult>(`${contextPath(p, j)}/read`, {
    product_id: p, project_id: j, source_id: source.id, view, limit: 20,
    ...(text ? { text } : {}), ...(cursor ? { cursor } : {}),
  });
