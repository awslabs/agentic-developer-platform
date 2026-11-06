import { apiClient } from './api';

export interface OAuthProviderStatus {
  provider: string;
  service: string;
  status: 'setup_required' | 'deployment_incomplete' | 'registered' | 'unavailable';
  connection_status?: 'not_connected' | 'consent_paused' | 'reconnect_required' | 'verified' | 'unavailable';
  login?: string;
  tool_setup_required?: boolean;
}

export interface OAuthAppStatus extends OAuthProviderStatus {
  developer_url: string;
  scopes: string[];
  callback_url?: string;
  manifest?: Record<string, unknown>;
  automation_state?: 'creating' | 'create_rejected' | 'credentials_saved' | 'ready' | 'unavailable';
  callback_configured?: boolean;
}

export function getOAuthProviders(): Promise<{ providers: OAuthProviderStatus[] }> {
  return apiClient.get('/auth/connections/oauth/providers');
}

export function getOAuthApp(provider: string): Promise<OAuthAppStatus> {
  return apiClient.get(`/admin/connections/oauth/${encodeURIComponent(provider)}/app`);
}

export function registerOAuthApp(provider: string, clientId: string, clientSecret: string): Promise<OAuthAppStatus> {
  return apiClient.post(`/admin/connections/oauth/${encodeURIComponent(provider)}/app`, { client_id: clientId, client_secret: clientSecret });
}

export function automateSlackApp(configurationToken: string): Promise<OAuthAppStatus> {
  return apiClient.post('/admin/connections/oauth/slack/app/automate', { configuration_token: configurationToken });
}

export interface OAuthStartResponse {
  authorization_url: string;
  state: string;
  session_uri: string;
}

// repositoryAccess (#8555, GitHub only): also ask GitHub for `repo` and `workflow`, so the
// owner's Dev Box (`gh`, `git`) and `adp github token` can act as the owner. Off by default.
export function startOAuthConnection(provider: string, chatWrite = false, reconnect = false, repositoryAccess = false): Promise<OAuthStartResponse> {
  const url = `/auth/connections/oauth/${encodeURIComponent(provider)}/start`;
  return chatWrite || reconnect || repositoryAccess
    ? apiClient.post<OAuthStartResponse>(url, {
      ...(chatWrite ? { chat_write: true } : {}),
      ...(reconnect ? { reconnect: true } : {}),
      ...(repositoryAccess ? { repository_access: true } : {}),
    })
    : apiClient.post<OAuthStartResponse>(url);
}

export interface OwnerGithubToken {
  token: string;
  login: string;
  expires_at: number;
}

/** The signed-in owner's own GitHub token with repository access (#8555); never stored by the browser. */
export function issueOwnerGithubToken(): Promise<OwnerGithubToken> {
  return apiClient.post<OwnerGithubToken>('/auth/connections/oauth/github/token');
}

export function completeOAuthConnection(state: string, sessionUri: string): Promise<void> {
  return apiClient.post<void>('/auth/connections/oauth/return', { state, session_uri: sessionUri });
}

export function disconnectOAuthConnection(provider: string): Promise<void> {
  return apiClient.delete<void>(`/auth/connections/oauth/${encodeURIComponent(provider)}`);
}

export interface AtlassianConnectionStatus {
  status: 'ready' | 'consent_required' | 'setup_required' | 'policy_blocked' | 'unavailable';
  owner?: string | null;
  action?: string | null;
  connection?: {
    id: string;
    revision: string;
    selected_site_id: string | null;
    sites: string[];
  };
  capabilities?: { jira_writes?: boolean };
  pending_observation?: { attempt_id: string };
}

const atlassianPath = '/auth/connections/atlassian-mcp';

export function getAtlassianConnectionStatus(): Promise<AtlassianConnectionStatus> {
  return apiClient.get(`${atlassianPath}/status`);
}

export function prepareAtlassianConnection(): Promise<{ status: string; owner?: string; action?: string }> {
  return apiClient.post('/admin/connections/atlassian-mcp/prepare');
}

export function startAtlassianConnection(capabilitySet: 'reads' | 'jira-writes' = 'reads', reconnectRevision?: string): Promise<OAuthStartResponse> {
  return apiClient.post(`${atlassianPath}/start`, {
    capability_set: capabilitySet,
    ...(reconnectRevision ? { reconnect_revision: reconnectRevision } : {}),
  });
}

export function completeAtlassianConnection(state: string, sessionUri?: string, sessionSha256?: string): Promise<void> {
  return sessionUri
    ? apiClient.post(`${atlassianPath}/complete`, { state, session_uri: sessionUri })
    : apiClient.post(`${atlassianPath}/complete-return`, { state, session_sha256: sessionSha256 });
}

export function resumeAtlassianConnection(attemptId: string): Promise<void> {
  return apiClient.post(`${atlassianPath}/resume`, { attempt_id: attemptId });
}

export function selectAtlassianSite(connectionRevision: string, siteId: string): Promise<void> {
  return apiClient.post(`${atlassianPath}/select-site`, { connection_revision: connectionRevision, site_id: siteId });
}

export function disconnectAtlassianConnection(connectionRevision: string): Promise<{ revocation_pending: boolean }> {
  return apiClient.delete(atlassianPath, { connection_revision: connectionRevision });
}
