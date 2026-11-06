import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { completeAtlassianConnection, completeOAuthConnection, disconnectAtlassianConnection, disconnectOAuthConnection, getAtlassianConnectionStatus, prepareAtlassianConnection, selectAtlassianSite, startAtlassianConnection, startOAuthConnection } from '@/services/oauthConnections';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() } }));

describe('OAuth connection routes', () => {
  beforeEach(() => vi.clearAllMocks());

  it('starts consent using the user OAuth route, not the GitHub App install route', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ authorization_url: 'https://example.com', state: 'state', session_uri: 'opaque' });
    await startOAuthConnection('atlassian');
    expect(apiClient.post).toHaveBeenCalledWith('/auth/connections/oauth/atlassian/start');
  });

  it('requests optional Slack write only after an explicit selection', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ authorization_url: 'https://example.com', state: 'state', session_uri: 'opaque' });
    await startOAuthConnection('slack');
    expect(apiClient.post).toHaveBeenCalledWith('/auth/connections/oauth/slack/start');
    await startOAuthConnection('slack', true);
    expect(apiClient.post).toHaveBeenLastCalledWith('/auth/connections/oauth/slack/start', { chat_write: true });
  });

  it('completes consent with the retained state and opaque session', async () => {
    await completeOAuthConnection('state', 'opaque');
    expect(apiClient.post).toHaveBeenCalledWith('/auth/connections/oauth/return', { state: 'state', session_uri: 'opaque' });
  });

  it('disconnects through the pointer-specific route', async () => {
    await disconnectOAuthConnection('slack');
    expect(apiClient.delete).toHaveBeenCalledWith('/auth/connections/oauth/slack');
  });

  it('reads persisted Atlassian readiness and connection independently from legacy Jira', async () => {
    const status = { status: 'ready', connection: { id: 'binding', revision: 'rev-1', selected_site_id: null, sites: ['site-1'] } };
    vi.mocked(apiClient.get).mockResolvedValue(status);
    expect(await getAtlassianConnectionStatus()).toEqual(status);
    expect(apiClient.get).toHaveBeenCalledWith('/auth/connections/atlassian-mcp/status');
  });

  it('uses revision-guarded Atlassian consent, site selection and disconnect', async () => {
    await startAtlassianConnection();
    expect(apiClient.post).toHaveBeenCalledWith('/auth/connections/atlassian-mcp/start', { capability_set: 'reads' });
    await startAtlassianConnection('jira-writes', 'rev-1');
    expect(apiClient.post).toHaveBeenLastCalledWith('/auth/connections/atlassian-mcp/start', { capability_set: 'jira-writes', reconnect_revision: 'rev-1' });
    await completeAtlassianConnection('nonce', 'opaque');
    expect(apiClient.post).toHaveBeenLastCalledWith('/auth/connections/atlassian-mcp/complete', { state: 'nonce', session_uri: 'opaque' });
    await selectAtlassianSite('rev-1', 'site-2');
    expect(apiClient.post).toHaveBeenLastCalledWith('/auth/connections/atlassian-mcp/select-site', { connection_revision: 'rev-1', site_id: 'site-2' });
    await disconnectAtlassianConnection('rev-2');
    expect(apiClient.delete).toHaveBeenCalledWith('/auth/connections/atlassian-mcp', { connection_revision: 'rev-2' });
  });

  it('prepares deployment setup through the admin-only route without app secrets', async () => {
    await prepareAtlassianConnection();
    expect(apiClient.post).toHaveBeenCalledExactlyOnceWith('/admin/connections/atlassian-mcp/prepare');
  });

  it('completes a runner-started browser return without exposing the Identity session URI', async () => {
    await completeAtlassianConnection('owner-bound-state', undefined, 'a'.repeat(64));
    expect(apiClient.post).toHaveBeenCalledExactlyOnceWith('/auth/connections/atlassian-mcp/complete-return', { state: 'owner-bound-state', session_sha256: 'a'.repeat(64) });
  });
});
