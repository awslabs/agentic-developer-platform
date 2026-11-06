/**
 * Connections page component tests.
 *
 * Issue #477: Test coverage for the Connections page (parent #465).
 * Uses React Testing Library with mocked services.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { StrictMode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import Connections from '@/pages/settings/Connections';
import { ToastProvider } from '@/contexts/ToastContext';
import { rememberProductConnection } from '@/services/productConnectionSetup';

vi.mock('@/hooks/useFeatures', () => ({ useRevalidatingFeaturesQuery: () => ({ data: { assistant_ui: false } }) }));

// Mock the connections service
vi.mock('@/services/connections', () => ({
  startGitHubInstall: vi.fn(),
  listConnections: vi.fn(),
  deleteGitHubConnection: vi.fn(),
  getGitHubAppStatus: vi.fn(),
  startGitHubAppRegistration: vi.fn(),
  rotateGitHubAppKey: vi.fn(),
  disconnectGitHubApp: vi.fn(),
  registerManualGitHubApp: vi.fn(),
}));

vi.mock('@/services/workspaces', () => ({ switchWorkspace: vi.fn() }));
vi.mock('@/components/aws/BedrockAccountSelector', () => ({ BedrockAccountSelector: () => null }));
vi.mock('@/services/credentials', async (importOriginal) => ({ ...await importOriginal<typeof import('@/services/credentials')>(), listCredentials: vi.fn(), deleteCredential: vi.fn(), setCredentialAgentConsent: vi.fn() }));
vi.mock('@/services/oauthConnections', () => ({ startOAuthConnection: vi.fn(), completeOAuthConnection: vi.fn(), disconnectOAuthConnection: vi.fn(), getOAuthProviders: vi.fn(), getOAuthApp: vi.fn(), registerOAuthApp: vi.fn(), automateSlackApp: vi.fn(), getAtlassianConnectionStatus: vi.fn(), prepareAtlassianConnection: vi.fn(), startAtlassianConnection: vi.fn(), completeAtlassianConnection: vi.fn(), resumeAtlassianConnection: vi.fn(), selectAtlassianSite: vi.fn(), disconnectAtlassianConnection: vi.fn() }));
import { switchWorkspace } from '@/services/workspaces';
import { deleteCredential, listCredentials, setCredentialAgentConsent } from '@/services/credentials';
import { startOAuthConnection, completeOAuthConnection, disconnectOAuthConnection, getOAuthProviders, getOAuthApp, registerOAuthApp, automateSlackApp } from '@/services/oauthConnections';
import { getAtlassianConnectionStatus, prepareAtlassianConnection, startAtlassianConnection, completeAtlassianConnection, resumeAtlassianConnection, selectAtlassianSite, disconnectAtlassianConnection } from '@/services/oauthConnections';

// Connections calls useAuth() to read the current user (free-tier banner gating)
// and hasRole for platform_admin check. Mock both.
// Default: non-admin. Tests that need admin override this via mockUseAuth.
const mockUseAuth = vi.fn(() => ({
  user: { orgId: 'org-test' },
  hasRole: () => false,
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

import {
  startGitHubInstall,
  listConnections,
  deleteGitHubConnection,
  getGitHubAppStatus,
  startGitHubAppRegistration,
  registerManualGitHubApp,
} from '@/services/connections';

const mockStartGitHubInstall = startGitHubInstall as ReturnType<typeof vi.fn>;
const mockListConnections = listConnections as ReturnType<typeof vi.fn>;
const mockDeleteGitHubConnection = deleteGitHubConnection as ReturnType<typeof vi.fn>;
const mockGetGitHubAppStatus = getGitHubAppStatus as ReturnType<typeof vi.fn>;
const mockRegisterManualGitHubApp = registerManualGitHubApp as ReturnType<typeof vi.fn>;

function renderConnections(initialEntries: string[] = ['/settings/connections']) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <ToastProvider>
        <Connections />
      </ToastProvider>
    </MemoryRouter>,
  );
}

function LocationProbe() {
  const { pathname } = useLocation();
  return <output data-testid="current-path">{pathname}</output>;
}

function ProductReturnProbe() {
  const location = useLocation();
  return <output data-testid="product-return">{location.pathname}{location.search}</output>;
}

describe('Connections Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({ user: { orgId: 'org-test' }, hasRole: () => false });
    vi.mocked(getOAuthProviders).mockResolvedValue({ providers: [
      { provider: 'github', service: 'github', status: 'registered' },
      { provider: 'slack', service: 'slack', status: 'registered' },
    ] });
    vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'ready' });
    vi.mocked(completeAtlassianConnection).mockResolvedValue(undefined);
    vi.mocked(disconnectAtlassianConnection).mockResolvedValue({ revocation_pending: false });
    mockListConnections.mockResolvedValue({ connections: [] });
    vi.mocked(listCredentials).mockResolvedValue([]);
    window.sessionStorage.clear();
    vi.mocked(completeOAuthConnection).mockResolvedValue(undefined);
    vi.mocked(disconnectOAuthConnection).mockResolvedValue(undefined);
    // Default: app is registered so Install button is visible (existing test expectations)
    mockGetGitHubAppStatus.mockResolvedValue({
      registered: true,
      app_slug: 'adp-agent',
      app_id: '123',
      owner_type: 'Organization',
      created_at: '2026-06-01T00:00:00Z',
    });
  });

  describe('Product setup callback return', () => {
    const actor = { id: 'user-test', orgId: 'org-test' };
    function renderReturn(query: string) {
      return render(<MemoryRouter initialEntries={[`/settings/connections?${query}`]}><ToastProvider><Connections /><ProductReturnProbe /></ToastProvider></MemoryRouter>);
    }
    it.each(['success=1', 'github_app=registered', 'error=installation_cancelled'])('returns GitHub setup to its Product after %s', async query => {
      mockUseAuth.mockReturnValue({ user: actor, hasRole: () => false });
      rememberProductConnection({ productId: 'product', projectId: 'project' }, 'github', actor);
      renderReturn(query);
      await waitFor(() => expect(screen.getByTestId('product-return')).toHaveTextContent('/products/product/projects/project?connection_setup=github'));
      expect(startGitHubInstall).not.toHaveBeenCalled();
      expect(startGitHubAppRegistration).not.toHaveBeenCalled();
    });
    it.each([true, false])('returns Slack setup to its Product after consent completion (success: %s)', async success => {
      mockUseAuth.mockReturnValue({ user: actor, hasRole: () => false });
      rememberProductConnection({ productId: 'product', projectId: 'project' }, 'slack', actor);
      sessionStorage.setItem('oauth_connection_pending', JSON.stringify({ state: 'state', session_uri: 'session', service: 'slack' }));
      if (success) vi.mocked(listCredentials).mockResolvedValue([{ id: 'slack', service: 'slack', provider: 'agentcore-identity', credential_type: 'oauth_token', scope: 'user', label: 'Slack', scopes: null, expires_at: null, last_used_at: null, strict: false, agent_consent: true, created_at: '2026-01-01T00:00:00Z', updated_at: null }]);
      else vi.mocked(completeOAuthConnection).mockRejectedValue(new Error('Consent denied'));
      renderReturn('oauth_consent=returned');
      await waitFor(() => expect(screen.getByTestId('product-return')).toHaveTextContent('/products/product/projects/project?connection_setup=slack'));
      expect(completeOAuthConnection).toHaveBeenCalledOnce();
      expect(startOAuthConnection).not.toHaveBeenCalled();
    });
  });

  describe('Atlassian readiness and roles', () => {
    it.each([
      ['setup_required', 'platform_admin', 'Ask a platform administrator to complete and verify the deployment setup.', 'Admin setup needed'],
      ['policy_blocked', 'atlassian_organization_admin', 'Ask the Atlassian organization administrator to allow this deployment client.', 'Policy approval needed'],
      ['unavailable', 'platform_admin', 'Ask a platform administrator to reconcile the deployment configuration.', 'Unavailable'],
    ] as const)('gives regular users the correct %s action without administrator controls', async (status, owner, action, label) => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status, owner, action });
      renderConnections();
      const card = await screen.findByLabelText('Atlassian — Jira & Confluence');
      await waitFor(() => expect(within(card).getByRole('status')).toHaveTextContent(label));
      expect(within(card).getByRole('alert')).toHaveTextContent(action);
      expect(within(card).queryByRole('button', { name: /Connect Atlassian|Prepare Atlassian/ })).not.toBeInTheDocument();
      expect(within(card).queryByLabelText(/client secret|client id|token/i)).not.toBeInTheDocument();
      expect(within(card).queryByText(/Entra ID|Forge|PAT/)).not.toBeInTheDocument();
      expect(getOAuthApp).not.toHaveBeenCalled();
      expect(prepareAtlassianConnection).not.toHaveBeenCalled();
    });

    it('lets a platform admin prepare setup without asking for client credentials', async () => {
      mockUseAuth.mockReturnValue({ user: { orgId: 'org-test' }, hasRole: () => true });
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValueOnce({ status: 'setup_required', owner: 'platform_admin', action: 'Ask a platform administrator to complete and verify the deployment setup.' })
        .mockResolvedValue({ status: 'ready' });
      vi.mocked(prepareAtlassianConnection).mockResolvedValue({ status: 'prepared' });
      renderConnections();
      const card = await screen.findByLabelText('Atlassian — Jira & Confluence');
      await userEvent.click(await within(card).findByRole('button', { name: 'Prepare Atlassian connection' }));
      expect(prepareAtlassianConnection).toHaveBeenCalledTimes(1);
      expect(await within(card).findByRole('button', { name: 'Connect Atlassian' })).toBeInTheDocument();
      expect(within(card).queryByLabelText(/client secret|client id/i)).not.toBeInTheDocument();
    });

    it.each([
      ['policy_blocked', 'atlassian_organization_admin'],
      ['unavailable', 'platform_admin'],
    ] as const)('does not retry admin preparation for %s', async (status, owner) => {
      mockUseAuth.mockReturnValue({ user: { orgId: 'org-test' }, hasRole: () => true });
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status, owner, action: 'Check the deployment policy.' });
      renderConnections();
      const card = await screen.findByLabelText('Atlassian — Jira & Confluence');
      await waitFor(() => expect(within(card).getByRole('status')).toHaveTextContent(status === 'policy_blocked' ? 'Policy approval needed' : 'Unavailable'));
      expect(within(card).queryByRole('button', { name: 'Prepare Atlassian connection' })).not.toBeInTheDocument();
      await userEvent.click(within(card).getByRole('button', { name: 'Refresh Atlassian status' }));
      expect(prepareAtlassianConnection).not.toHaveBeenCalled();
    });

    it('hides stale connection controls if status becomes unavailable during refresh', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValueOnce({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: 'site-1' } })
        .mockRejectedValueOnce(new Error('Adapter unavailable'));
      renderConnections();
      const card = await screen.findByLabelText('Atlassian — Jira & Confluence');
      await userEvent.click(await within(card).findByRole('button', { name: 'Refresh Atlassian status' }));
      await waitFor(() => expect(within(card).getByRole('status')).toHaveTextContent('Unavailable'));
      expect(within(card).queryByRole('button', { name: 'Reconnect Atlassian' })).not.toBeInTheDocument();
      expect(within(card).queryByRole('button', { name: 'Disconnect Atlassian' })).not.toBeInTheDocument();
    });
  });

  describe('legacy Jira migration', () => {
    const legacy = { id: 'old-jira', scope: 'user', service: 'jira', credential_type: 'oauth_token',
      provider: 'agentcore-identity', agent_consent: true, scopes: { oauth_scopes: ['read:jira-work'] } };
    const github = { ...legacy, id: 'github-owner', service: 'github', scopes: { oauth_scopes: ['read:user'] } };
    const slack = { ...legacy, id: 'slack-owner', service: 'slack', scopes: { oauth_scopes: ['users:read'] } };

    it.each(['error', 'returned'] as const)('retains legacy and other provider records after %s consent failure and reload', async (consent) => {
      const records = [legacy, github, slack];
      vi.mocked(listCredentials).mockImplementation(async () => records as never);
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'ready' });
      if (consent === 'returned') vi.mocked(completeAtlassianConnection).mockRejectedValue(new Error('Identity consent failed'));
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({ state: 'nonce', session_uri: 'opaque', service: 'atlassian-mcp' }));
      const { unmount } = renderConnections([`/settings/connections?oauth_consent=${consent}`]);
      expect(await screen.findByText(consent === 'error' ? /Consent was cancelled or expired/ : 'Identity consent failed')).toBeInTheDocument();
      expect(await screen.findByText(/Your existing Jira connection is unchanged/)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Disconnect old Jira connection' })).not.toBeInTheDocument();
      expect(disconnectOAuthConnection).not.toHaveBeenCalled();
      expect(disconnectAtlassianConnection).not.toHaveBeenCalled();
      expect(records.map((record) => record.id)).toEqual(['old-jira', 'github-owner', 'slack-owner']);
      expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
      unmount();
      renderConnections();
      expect(await screen.findByRole('button', { name: 'Connect Atlassian' })).toBeInTheDocument();
      expect(screen.getByText(/Your existing Jira connection is unchanged/)).toBeInTheDocument();
      expect(completeAtlassianConnection).toHaveBeenCalledTimes(consent === 'error' ? 0 : 1);
    });

    it('offers old Jira removal only after saved site selection, without changing GitHub or Slack', async () => {
      let records = [legacy, github, slack];
      let selectedSite: string | null = null;
      let connected = false;
      vi.mocked(listCredentials).mockImplementation(async () => records as never);
      vi.mocked(getAtlassianConnectionStatus).mockImplementation(async () => ({ status: 'ready' as const,
        connection: connected ? { id: 'new-atlassian', revision: 'rev-1', selected_site_id: selectedSite, sites: ['site-one', 'site-two'] } : undefined }));
      vi.mocked(completeAtlassianConnection).mockImplementation(async () => { connected = true; });
      vi.mocked(selectAtlassianSite).mockImplementation(async (_revision, site) => { selectedSite = site; });
      vi.mocked(disconnectOAuthConnection).mockImplementation(async (provider) => {
        expect(provider).toBe('atlassian');
        records = records.filter((record) => record.id !== 'old-jira');
      });
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({ state: 'nonce', session_uri: 'opaque', service: 'atlassian-mcp' }));
      renderConnections(['/settings/connections?oauth_consent=returned']);
      expect(await screen.findByText(/Atlassian consent completed/)).toBeInTheDocument();
      expect(screen.getByText(/Your existing Jira connection is unchanged/)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Disconnect old Jira connection' })).not.toBeInTheDocument();
      await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Atlassian site' }), 'site-two');
      expect(await screen.findByRole('button', { name: 'Disconnect old Jira connection' })).toBeInTheDocument();
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
      await userEvent.click(screen.getByRole('button', { name: 'Disconnect old Jira connection' }));
      await waitFor(() => expect(records.map((record) => record.id)).toEqual(['github-owner', 'slack-owner']));
      expect(getAtlassianConnectionStatus).toHaveBeenCalledTimes(4);
      expect(screen.getByRole('combobox', { name: 'Atlassian site' })).toHaveValue('site-two');
      expect(screen.queryByText(/Your existing Jira connection is unchanged/)).not.toBeInTheDocument();
      expect(disconnectAtlassianConnection).not.toHaveBeenCalled();
      confirm.mockRestore();
    });

    it('keeps old Jira when the replacement is unavailable just before removal', async () => {
      const records = [legacy, github, slack];
      vi.mocked(listCredentials).mockResolvedValue(records as never);
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValueOnce({ status: 'ready', connection: { id: 'new', revision: 'rev-1', selected_site_id: 'site-one', sites: ['site-one'] } })
        .mockResolvedValue({ status: 'unavailable', owner: 'platform_admin', action: 'Service unavailable.' });
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Disconnect old Jira connection' }));
      expect(await screen.findByText('Atlassian connection changed or is unavailable. Your old Jira connection was kept.')).toBeInTheDocument();
      expect(disconnectOAuthConnection).not.toHaveBeenCalled();
      expect(records.map((record) => record.id)).toEqual(['old-jira', 'github-owner', 'slack-owner']);
      confirm.mockRestore();
    });

    it('does not remove old Jira if the replacement revision changes or the user cancels', async () => {
      const records = [legacy, github, slack];
      vi.mocked(listCredentials).mockResolvedValue(records as never);
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValueOnce({ status: 'ready', connection: { id: 'new', revision: 'rev-1', selected_site_id: 'site-one', sites: ['site-one'] } })
        .mockResolvedValue({ status: 'ready', connection: { id: 'new', revision: 'rev-2', selected_site_id: 'site-one', sites: ['site-one'] } });
      const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true);
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Disconnect old Jira connection' }));
      expect(getAtlassianConnectionStatus).toHaveBeenCalledTimes(1);
      await userEvent.click(screen.getByRole('button', { name: 'Disconnect old Jira connection' }));
      expect(await screen.findByText('Atlassian connection changed or is unavailable. Your old Jira connection was kept.')).toBeInTheDocument();
      expect(disconnectOAuthConnection).not.toHaveBeenCalled();
      expect(records.map((record) => record.id)).toEqual(['old-jira', 'github-owner', 'slack-owner']);
      confirm.mockRestore();
    });
  });

  describe('Empty state rendering', () => {
    it('renders page header and GitHub tile', async () => {
      renderConnections();

      expect(screen.getByText('Connections')).toBeInTheDocument();
      expect(screen.getByText('GitHub')).toBeInTheDocument();
      await waitFor(() => {
        expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
      });
    });

    it('shows disconnected provider rows separately from the GitHub App tile', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'Slack' })).toBeInTheDocument();
        expect(screen.queryByText('Google')).not.toBeInTheDocument();
      });
      expect(screen.getAllByText('Not connected')).toHaveLength(3);
      expect(screen.queryByText('Coming soon')).not.toBeInTheDocument();
    });

    it('renders user-owned OAuth pointer scopes and last-used metadata without confusing PATs or App installs', async () => {
      vi.mocked(listCredentials).mockResolvedValue([
        {
          id: 'pointer', scope: 'user', service: 'jira', credential_type: 'oauth_token',
          label: 'AgentCore OAuth', provider: 'agentcore-identity', agent_consent: true, scopes: { oauth_scopes: ['read:jira-work'] },
          last_used_at: '2026-10-12T12:00:00Z',
        },
        { id: 'pat', scope: 'user', service: 'github', credential_type: 'bearer', label: 'PAT' },
        { id: 'fake', scope: 'user', service: 'github', credential_type: 'oauth_token', provider: 'custom', label: 'AgentCore OAuth' },
      ] as never);
      renderConnections();

      const accounts = await screen.findByRole('region', { name: 'Service connections' });
      expect(accounts).toHaveTextContent('Atlassian — Jira & ConfluenceNot connected');
      expect(accounts).toHaveTextContent('Your existing Jira connection is unchanged');
      expect(screen.queryByRole('checkbox', { name: /Allow agents to use Atlassian/ })).not.toBeInTheDocument();
      expect(accounts).toHaveTextContent('Your accountNot connected');
      expect(accounts).toHaveTextContent('SlackNot connected');
    });

    it('distinguishes a connected pointer with agent access disabled', async () => {
      let finishLoading!: () => void;
      const loading = new Promise<void>((resolve) => { finishLoading = resolve; });
      vi.mocked(listCredentials).mockImplementation(async () => {
        await loading;
        return [{
          id: 'pointer', scope: 'user', service: 'slack', credential_type: 'oauth_token',
          provider: 'agentcore-identity', agent_consent: false, scopes: { oauth_scopes: ['users:read'] },
        }] as never;
      });
      renderConnections();
      const accounts = await screen.findByRole('region', { name: 'Service connections' });
      expect(accounts).toHaveTextContent('Loading third-party connections…');
      finishLoading();
      await waitFor(() => expect(accounts).toHaveTextContent('SlackConnected'));
      expect(screen.getByRole('checkbox', { name: 'Allow agents to use Slack' })).not.toBeChecked();
      expect(screen.queryByRole('button', { name: 'Re-consent Slack test-channel posting' })).not.toBeInTheDocument();
    });

    it('shows setup required instead of a broken connect action', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'setup_required', owner: 'platform_admin', action: 'Ask a platform administrator to complete and verify the deployment setup.' });
      renderConnections();
      await screen.findByText('Ask a platform administrator to complete and verify the deployment setup.');
      expect(screen.queryByRole('button', { name: 'Connect Atlassian' })).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Client secret')).not.toBeInTheDocument();
      expect(startOAuthConnection).not.toHaveBeenCalled();
    });

    it('does not offer app creation when readiness is unknown', async () => {
      mockUseAuth.mockReturnValue({ user: { orgId: 'org-test' }, hasRole: () => true });
      vi.mocked(getOAuthProviders).mockRejectedValue(new Error('Unavailable'));
      renderConnections();
      await screen.findAllByText('Unavailable');
      expect(screen.queryByRole('button', { name: 'Set up Atlassian' })).not.toBeInTheDocument();
    });

    it('automates Slack setup without showing client credentials to the administrator', async () => {
      mockUseAuth.mockReturnValue({ user: { orgId: 'org-test' }, hasRole: () => true });
      const app = { provider: 'slack', service: 'slack', status: 'setup_required' as const,
        developer_url: 'https://api.slack.com/apps', scopes: ['users:read'] };
      vi.mocked(getOAuthProviders).mockResolvedValue({ providers: [app] });
      vi.mocked(getOAuthApp).mockResolvedValue(app);
      vi.mocked(automateSlackApp).mockResolvedValue({ ...app, status: 'registered', automation_state: 'ready',
        callback_configured: true, callback_url: 'https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/fixture' });
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Set up Slack' }));
      await userEvent.type(await screen.findByLabelText('App configuration access token'), 'private-configuration-token');
      await userEvent.click(screen.getByRole('button', { name: 'Create and register Slack app' }));
      expect(await screen.findByText('ADP saved this callback in Slack. You can now connect your account.')).toBeInTheDocument();
      expect(automateSlackApp).toHaveBeenCalledExactlyOnceWith('private-configuration-token');
      expect(registerOAuthApp).not.toHaveBeenCalled();
      expect(window.sessionStorage.getItem('private-configuration-token')).toBeNull();
      expect(screen.queryByLabelText('App configuration access token')).not.toBeInTheDocument();
    });

    it('requires explicit reconnect for an Atlassian account without deleting legacy Jira', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: null } });
      vi.mocked(getOAuthProviders).mockResolvedValue({ providers: [{ provider: 'atlassian', service: 'jira',
        status: 'registered', connection_status: 'reconnect_required', tool_setup_required: true }] });
      vi.mocked(listCredentials).mockResolvedValue([{ id: 'pointer', scope: 'user', service: 'jira', credential_type: 'oauth_token',
        provider: 'agentcore-identity', agent_consent: true, scopes: { oauth_scopes: ['read:jira-work'] } }] as never);
      vi.mocked(startAtlassianConnection).mockRejectedValue(new Error('Consent preview stopped'));
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Reconnect Atlassian' }));
      expect(startAtlassianConnection).toHaveBeenCalledExactlyOnceWith('reads', 'rev-1');
      expect(disconnectOAuthConnection).not.toHaveBeenCalled();
      expect(deleteCredential).not.toHaveBeenCalled();
    });

    it('does not claim tool access merely because an Atlassian site is selected', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: 'site-1' } });
      vi.mocked(getOAuthProviders).mockResolvedValue({ providers: [{ provider: 'atlassian', service: 'jira',
        status: 'registered', connection_status: 'verified', tool_setup_required: true }] });
      vi.mocked(listCredentials).mockResolvedValue([{ id: 'pointer', scope: 'user', service: 'jira', credential_type: 'oauth_token',
        provider: 'agentcore-identity', agent_consent: true, scopes: { oauth_scopes: ['read:jira-work', 'read:me'] } }] as never);
      renderConnections();
      expect(await screen.findByText('Site selected')).toBeInTheDocument();
      expect(screen.getByText(/Tool access depends on workspace permissions/)).toBeInTheDocument();
    });

    it('saves a site with the current revision and displays the refreshed server choice', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValueOnce({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1', 'site-2'], selected_site_id: null } })
        .mockResolvedValue({ status: 'ready', connection: { id: 'new', revision: 'rev-2', sites: ['site-1', 'site-2'], selected_site_id: 'site-2' } });
      renderConnections();
      await userEvent.selectOptions(await screen.findByRole('combobox', { name: 'Atlassian site' }), 'site-2');
      expect(selectAtlassianSite).toHaveBeenCalledExactlyOnceWith('rev-1', 'site-2');
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Atlassian site' })).toHaveValue('site-2'));
    });

    it('only offers write consent when status explicitly advertises it', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: 'site-1' } });
      const { unmount } = renderConnections();
      expect(await screen.findByText('Site selected')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Request Jira write access' })).not.toBeInTheDocument();
      unmount();
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'ready', capabilities: { jira_writes: true }, connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: 'site-1' } });
      vi.mocked(startAtlassianConnection).mockRejectedValue(new Error('Consent stopped'));
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Request Jira write access' }));
      expect(startAtlassianConnection).toHaveBeenCalledExactlyOnceWith('jira-writes', 'rev-1');
    });

    it('does not present the old Jira app registration inside the Atlassian card', async () => {
      mockUseAuth.mockReturnValue({ user: { orgId: 'org-test' }, hasRole: () => true });
      renderConnections();
      await screen.findByRole('heading', { name: 'Atlassian — Jira & Confluence' });
      expect(registerOAuthApp).not.toHaveBeenCalled();
      expect(screen.queryByLabelText('Client secret')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Set up Atlassian (Jira)' })).not.toBeInTheDocument();
    });

    it('does not present a failed pointer lookup as disconnected', async () => {
      vi.mocked(listCredentials).mockRejectedValue(new Error('Offline'));
      renderConnections();
      expect((await screen.findAllByRole('alert')).some((alert) => alert.textContent?.includes('Could not load third-party connections'))).toBe(true);
      expect(screen.queryByRole('button', { name: 'Connect Slack' })).not.toBeInTheDocument();
    });

    it('shows no installation cards when connections list is empty', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
      });
      expect(screen.queryByText('Disconnect')).not.toBeInTheDocument();
    });
  });

  describe('OAuth consent and account actions', () => {
    const pointer = {
      id: 'pointer', scope: 'user', service: 'slack', credential_type: 'oauth_token',
      label: 'AgentCore OAuth', provider: 'agentcore-identity', agent_consent: true, scopes: { oauth_scopes: ['users:read'] }, last_used_at: null,
    };

    it.each(['returned', 'error'])('handles a PAT migration return through Connections (%s) without deleting the PAT', async (consent) => {
      const pat = { id: 'legacy-pat', service: 'github', scope: 'user', label: 'github-pat', credential_type: 'bearer', agent_consent: false };
      vi.mocked(listCredentials).mockResolvedValue([pat] as never);
      vi.mocked(startOAuthConnection).mockResolvedValue({
        authorization_url: 'https://github.example.test/authorize', state: 'migration-state', session_uri: 'migration-session',
      });
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'migration-state', session_uri: 'migration-session', service: 'github',
      }));

      if (consent === 'returned') {
        vi.mocked(listCredentials).mockResolvedValue([pat, { ...pointer, service: 'github' }] as never);
      }
      renderConnections([`/settings/connections?oauth_consent=${consent}`]);
      if (consent === 'returned') {
        expect(await screen.findByText('Third-party account connected.')).toBeInTheDocument();
        expect(completeOAuthConnection).toHaveBeenCalledExactlyOnceWith('migration-state', 'migration-session');
        await userEvent.click(screen.getByText('Personal account access (optional)'));
        expect(await screen.findByRole('button', { name: 'Disconnect GitHub' })).toBeInTheDocument();
      } else {
        expect(await screen.findByText(/Consent was cancelled or expired/)).toBeInTheDocument();
        expect(completeOAuthConnection).not.toHaveBeenCalled();
        expect(screen.queryByText('Third-party account connected.')).not.toBeInTheDocument();
      }
      expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
      expect(deleteCredential).not.toHaveBeenCalled();
    });

    it('connects GitHub with repository access only when the owner ticks it (#8555)', async () => {
      const locationSpy = vi.spyOn(window, 'location', 'get');
      const location = { ...window.location, href: '' };
      locationSpy.mockReturnValue(location as Location);
      vi.mocked(startOAuthConnection).mockResolvedValue({
        authorization_url: 'https://github.example.test/authorize', state: 'nonce', session_uri: 'opaque',
      });
      renderConnections();
      await userEvent.click(await screen.findByText('Personal account access (optional)'));
      await userEvent.click(await screen.findByRole('checkbox', { name: 'Include repository access' }));
      await userEvent.click(screen.getByRole('button', { name: 'Connect GitHub' }));
      expect(startOAuthConnection).toHaveBeenCalledExactlyOnceWith('github', false, false, true);
      expect(location.href).toBe('https://github.example.test/authorize');
      locationSpy.mockRestore();
    });

    it('offers to grant repository access to a connected GitHub account and reports it once granted (#8555)', async () => {
      const connected = { ...pointer, service: 'github', scopes: { oauth_scopes: ['read:user'] } };
      vi.mocked(listCredentials).mockResolvedValue([connected] as never);
      vi.mocked(startOAuthConnection).mockResolvedValue({
        authorization_url: 'https://github.example.test/authorize', state: 'nonce', session_uri: 'opaque',
      });
      const locationSpy = vi.spyOn(window, 'location', 'get');
      locationSpy.mockReturnValue({ ...window.location, href: '' } as Location);
      const { unmount } = renderConnections();
      await userEvent.click(await screen.findByText('Personal account access (optional)'));
      await userEvent.click(await screen.findByText('Connection details'));
      await userEvent.click(await screen.findByRole('button', { name: 'Grant repository access' }));
      expect(startOAuthConnection).toHaveBeenCalledExactlyOnceWith('github', false, false, true);
      unmount();
      vi.mocked(listCredentials).mockResolvedValue([{ ...connected, scopes: { oauth_scopes: ['read:user', 'repo', 'workflow'] } }] as never);
      renderConnections();
      await userEvent.click(await screen.findByText('Personal account access (optional)'));
      await userEvent.click(await screen.findByText('Connection details'));
      expect(await screen.findByText(/Repository access granted/)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Grant repository access' })).not.toBeInTheDocument();
      locationSpy.mockRestore();
    });

    it('starts provider consent and keeps the opaque session in this browser tab', async () => {
      const locationSpy = vi.spyOn(window, 'location', 'get');
      const location = { ...window.location, href: '' };
      locationSpy.mockReturnValue(location as Location);
      vi.mocked(startAtlassianConnection).mockResolvedValue({
        authorization_url: 'https://atlassian.example.test/authorize', state: 'nonce', session_uri: 'opaque',
      });
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Connect Atlassian' }));
      expect(startAtlassianConnection).toHaveBeenCalledWith('reads', undefined);
      expect(location.href).toBe('https://atlassian.example.test/authorize');
      expect(JSON.parse(window.sessionStorage.getItem('oauth_connection_pending')!)).toEqual({
        state: 'nonce', session_uri: 'opaque', service: 'atlassian-mcp',
      });
      locationSpy.mockRestore();
    });

    it('offers Slack write re-consent separately without disconnecting the read connection', async () => {
      vi.mocked(listCredentials).mockResolvedValue([{
        id: 'slack-read', scope: 'user', service: 'slack', credential_type: 'oauth_token',
        label: 'AgentCore OAuth', provider: 'agentcore-identity', agent_consent: true,
        scopes: { oauth_scopes: ['users:read'] },
      }] as never);
      const locationSpy = vi.spyOn(window, 'location', 'get');
      const location = { ...window.location, href: '' };
      locationSpy.mockReturnValue(location as Location);
      vi.mocked(startOAuthConnection).mockResolvedValue({
        authorization_url: 'https://slack.com/authorize', state: 'write-state', session_uri: 'write-session',
      });
      renderConnections();
      expect(await screen.findByRole('button', { name: 'Disconnect Slack' })).toBeInTheDocument();
      expect(screen.getByText('Scopes: users:read')).toBeInTheDocument();
      const slackCard = screen.getByRole('heading', { name: 'Slack' }).parentElement!.parentElement!;
      await userEvent.click(within(slackCard).getByText('Connection details'));
      await userEvent.click(screen.getByRole('button', { name: 'Re-consent Slack test-channel posting' }));
      expect(startOAuthConnection).toHaveBeenCalledExactlyOnceWith('slack', true);
      expect(disconnectOAuthConnection).not.toHaveBeenCalled();
      expect(JSON.parse(window.sessionStorage.getItem('oauth_connection_pending')!)).toEqual({
        state: 'write-state', session_uri: 'write-session', service: 'slack',
      });
      expect(location.href).toBe('https://slack.com/authorize');
      locationSpy.mockRestore();
    });

    it('lets users change account consent from Connections and preserves saved state on failure', async () => {
      vi.mocked(listCredentials).mockResolvedValue([pointer] as never);
      vi.mocked(setCredentialAgentConsent).mockResolvedValueOnce({ ...pointer, agent_consent: false } as never)
        .mockRejectedValueOnce(new Error('Could not save consent'));
      renderConnections();
      const toggle = await screen.findByRole('checkbox', { name: 'Allow agents to use Slack' });
      expect(toggle).toBeChecked();
      await userEvent.click(toggle);
      await waitFor(() => expect(toggle).not.toBeChecked());
      expect(setCredentialAgentConsent).toHaveBeenCalledWith('pointer', false);
      await userEvent.click(toggle);
      await screen.findByText('Could not save consent');
      expect(toggle).not.toBeChecked();
    });

    it('offers first consent before provider verification without offering write access', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'consent_required', owner: 'user' });
      renderConnections();
      expect(await screen.findByRole('button', { name: 'Connect Atlassian' })).toBeEnabled();
      expect(screen.queryByRole('button', { name: 'Request Jira write access' })).not.toBeInTheDocument();
    });

    it('resumes saved consent without starting another sign-in', async () => {
      const attemptId = 'a'.repeat(64);
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValueOnce({ status: 'consent_required', pending_observation: { attempt_id: attemptId } })
        .mockResolvedValue({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: 'site-1' } });
      vi.mocked(resumeAtlassianConnection).mockResolvedValue();
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Finish connecting Atlassian' }));
      expect(resumeAtlassianConnection).toHaveBeenCalledExactlyOnceWith(attemptId);
      expect(startAtlassianConnection).not.toHaveBeenCalled();
      expect(await screen.findByText('Atlassian account verified.')).toBeInTheDocument();
    });

    it('retains the resume option when provider verification is temporarily unavailable', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'consent_required', pending_observation: { attempt_id: 'a'.repeat(64) } });
      vi.mocked(resumeAtlassianConnection).mockRejectedValue(new Error('Adapter unavailable'));
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Finish connecting Atlassian' }));
      expect(await screen.findByText('Adapter unavailable')).toBeInTheDocument();
      expect(startAtlassianConnection).not.toHaveBeenCalled();
      expect(screen.getByRole('button', { name: 'Finish connecting Atlassian' })).toBeEnabled();
    });

    it('completes a runner-started consent after the owner returns in another browser', async () => {
      const state = 'a'.repeat(43);
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'consent_required', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: null } });
      const sessionSha = 'b'.repeat(64);
      renderConnections([`/settings/connections?oauth_consent=returned&atlassian_state=${state}&atlassian_session_sha256=${sessionSha}`]);
      await waitFor(() => expect(completeAtlassianConnection).toHaveBeenCalledExactlyOnceWith(state, undefined, sessionSha));
      expect(await screen.findByText(/Atlassian consent completed/)).toBeInTheDocument();
      expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
    });

    it('finishes returned consent and reports connected only after the pointer is visible', async () => {
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'nonce', session_uri: 'opaque', service: 'atlassian-mcp',
      }));
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValueOnce({ status: 'ready' }).mockResolvedValue({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: null } });
      renderConnections(['/settings/connections?oauth_consent=returned']);
      await waitFor(() => expect(completeAtlassianConnection).toHaveBeenCalledWith('nonce', 'opaque'));
      expect(await screen.findByRole('button', { name: 'Disconnect Atlassian' })).toBeInTheDocument();
      expect(await screen.findByText(/Atlassian consent completed/)).toBeInTheDocument();
      expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
    });

    it('returns completed Dev Box consent to the box', async () => {
      const actor = { id: 'user-test', orgId: 'org-test' };
      mockUseAuth.mockReturnValue({ user: actor, hasRole: () => false });
      rememberProductConnection({ productId: 'product', projectId: 'project' }, 'github', actor);
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'nonce', session_uri: 'opaque', service: 'github', return_to: '/settings/dev-box',
      }));
      let finish!: () => void;
      vi.mocked(completeOAuthConnection).mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
      vi.mocked(listCredentials).mockResolvedValue([{
        ...pointer, service: 'github', scopes: { github_app_grant: { schema_version: 1, repository_access: true } },
      }] as never);
      render(<MemoryRouter initialEntries={['/settings/connections?oauth_consent=returned']}>
        <ToastProvider><Connections /><LocationProbe /></ToastProvider>
      </MemoryRouter>);
      await waitFor(() => expect(completeOAuthConnection).toHaveBeenCalledExactlyOnceWith('nonce', 'opaque'));
      expect(screen.getByTestId('current-path')).toHaveTextContent('/settings/connections');
      expect(listCredentials).not.toHaveBeenCalled();
      finish();
      await waitFor(() => expect(screen.getByTestId('current-path')).toHaveTextContent('/settings/dev-box'));
      expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
    });

    it('does not return to the box on a rejected App callback despite a stale pointer', async () => {
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'nonce', session_uri: 'opaque', service: 'github', return_to: '/settings/dev-box',
      }));
      vi.mocked(completeOAuthConnection).mockRejectedValue(new Error('Consent rejected'));
      vi.mocked(listCredentials).mockResolvedValue([{ ...pointer, service: 'github' }] as never);
      render(<MemoryRouter initialEntries={['/settings/connections?oauth_consent=returned']}>
        <ToastProvider><Connections /><LocationProbe /></ToastProvider>
      </MemoryRouter>);
      expect(await screen.findByText('Consent rejected')).toBeInTheDocument();
      expect(screen.getByTestId('current-path')).toHaveTextContent('/settings/connections');
      expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
    });

    it('does not follow an untrusted return destination', async () => {
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'nonce', session_uri: 'opaque', service: 'github', return_to: 'https://untrusted.example/elsewhere',
      }));
      vi.mocked(listCredentials).mockResolvedValue([{ ...pointer, service: 'github' }] as never);
      render(<MemoryRouter initialEntries={['/settings/connections?oauth_consent=returned']}>
        <ToastProvider><Connections /><LocationProbe /></ToastProvider>
      </MemoryRouter>);
      await waitFor(() => expect(completeOAuthConnection).toHaveBeenCalledExactlyOnceWith('nonce', 'opaque'));
      expect(screen.getByTestId('current-path')).toHaveTextContent('/settings/connections');
    });

    it('does not complete an unsolicited provider return', async () => {
      renderConnections(['/settings/connections?oauth_consent=returned']);
      expect(await screen.findByText(/Consent was cancelled or expired/)).toBeInTheDocument();
      expect(completeOAuthConnection).not.toHaveBeenCalled();
      expect(screen.queryByText('Third-party account connected.')).not.toBeInTheDocument();
    });

    it('completes a return only once when React replays mount effects', async () => {
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'nonce', session_uri: 'opaque', service: 'slack',
      }));
      vi.mocked(listCredentials).mockResolvedValue([pointer] as never);
      render(
        <StrictMode>
          <MemoryRouter initialEntries={['/settings/connections?oauth_consent=returned']}>
            <ToastProvider><Connections /></ToastProvider>
          </MemoryRouter>
        </StrictMode>,
      );
      expect(await screen.findByText('Third-party account connected.')).toBeInTheDocument();
      expect(completeOAuthConnection).toHaveBeenCalledTimes(1);
    });

    it('confirms disconnect and refreshes the pointer list', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValueOnce({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: 'site-1' } }).mockResolvedValue({ status: 'ready' });
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Disconnect Atlassian' }));
      expect(disconnectAtlassianConnection).toHaveBeenCalledWith('rev-1');
      expect(await screen.findByRole('button', { name: 'Connect Atlassian' })).toBeInTheDocument();
      confirm.mockRestore();
    });

    it('keeps a cancelled disconnect connected and does not call the API', async () => {
      vi.mocked(getAtlassianConnectionStatus).mockResolvedValue({ status: 'ready', connection: { id: 'new', revision: 'rev-1', sites: ['site-1'], selected_site_id: 'site-1' } });
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Disconnect Atlassian' }));
      expect(disconnectAtlassianConnection).not.toHaveBeenCalled();
      confirm.mockRestore();
    });

    it('does not report a cancelled or failed consent as connected', async () => {
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'nonce', session_uri: 'opaque', service: 'atlassian-mcp',
      }));
      renderConnections(['/settings/connections?oauth_consent=error']);
      expect(await screen.findByText(/Consent was cancelled or expired/)).toBeInTheDocument();
      expect(completeOAuthConnection).not.toHaveBeenCalled();
      expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
    });

    it('does not report success when completion fails or no pointer is returned', async () => {
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'nonce', session_uri: 'opaque', service: 'atlassian-mcp',
      }));
      vi.mocked(completeAtlassianConnection).mockRejectedValue(new Error('Consent rejected'));
      renderConnections(['/settings/connections?oauth_consent=returned']);
      expect(await screen.findByText('Consent rejected')).toBeInTheDocument();
      expect(screen.queryByText('Third-party account connected.')).not.toBeInTheDocument();
    });

    it('keeps a failed return disconnected when completion produces no pointer', async () => {
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: 'nonce', session_uri: 'opaque', service: 'slack',
      }));
      renderConnections(['/settings/connections?oauth_consent=returned']);
      expect(await screen.findByText(/Connection not found after consent/)).toBeInTheDocument();
      expect(await screen.findByRole('button', { name: 'Connect Slack' })).toBeInTheDocument();
      expect(screen.queryByText('Third-party account connected.')).not.toBeInTheDocument();
    });

    it('does not redirect when consent start fails', async () => {
      vi.mocked(startAtlassianConnection).mockRejectedValue(new Error('Provider unavailable'));
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Connect Atlassian' }));
      expect(await screen.findByText('Provider unavailable')).toBeInTheDocument();
      expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
    });
  });

  describe('Install flow', () => {
    it('calls startGitHubInstall and redirects to install_url on button click', async () => {
      const user = userEvent.setup();
      const mockUrl = 'https://github.com/apps/adp-agent/installations/new?state=abc';
      mockStartGitHubInstall.mockResolvedValue({
        install_url: mockUrl,
        state_token: 'abc',
        expires_at: '2026-05-05T12:00:00Z',
      });

      // Mock window.location.href setter
      const locationHrefSpy = vi.spyOn(window, 'location', 'get');
      const mockLocation = { ...window.location, href: '' };
      locationHrefSpy.mockReturnValue(mockLocation as Location);

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Install on GitHub'));

      await waitFor(() => {
        expect(mockStartGitHubInstall).toHaveBeenCalledTimes(1);
      });
      expect(mockLocation.href).toBe(mockUrl);

      locationHrefSpy.mockRestore();
    });

    it('shows error toast when startGitHubInstall fails', async () => {
      const user = userEvent.setup();
      mockStartGitHubInstall.mockRejectedValue(new Error('Network error'));

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Install on GitHub'));

      await waitFor(() => {
        expect(screen.getByText('Network error')).toBeInTheDocument();
      });
    });
  });

  describe('Success callback (query param ?success=1)', () => {
    it('shows success toast when ?success=1 is present', async () => {
      renderConnections(['/settings/connections?success=1']);

      await waitFor(() => {
        expect(
          screen.getByText('GitHub connected! You can now trigger agents from this org.'),
        ).toBeInTheDocument();
      });
    });

    it('refetches connections list on success', async () => {
      renderConnections(['/settings/connections?success=1']);

      await waitFor(() => {
        // Initial load + refetch triggered by success param
        expect(mockListConnections).toHaveBeenCalled();
      });
    });
  });

  it('preserves the GitHub App manifest registration action and callback state', async () => {
    mockUseAuth.mockReturnValue({ user: { orgId: 'org-test' }, hasRole: () => true });
    mockGetGitHubAppStatus.mockResolvedValue({ registered: false });
    vi.mocked(startGitHubAppRegistration).mockResolvedValue({
      post_url: 'https://github.com/settings/apps/new', manifest: { name: 'ADP' }, state: 'registration-state',
    } as never);
    const submitted: { action: string; manifest: FormDataEntryValue | null; state: FormDataEntryValue | null }[] = [];
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(function(this: HTMLFormElement) {
      const data = new FormData(this);
      submitted.push({ action: this.action, manifest: data.get('manifest'), state: data.get('state') });
    });
    renderConnections();
    await userEvent.click(await screen.findByRole('button', { name: 'Create on GitHub' }));
    expect(submitted).toEqual([{ action: 'https://github.com/settings/apps/new', manifest: '{"name":"ADP"}', state: 'registration-state' }]);
    submit.mockRestore();
  });

  describe('App registered callback (query param ?github_app=registered)', () => {
    it('shows success toast when login is wired', async () => {
      renderConnections(['/settings/connections?github_app=registered']);

      await waitFor(() => {
        expect(screen.getByText('GitHub App registered successfully!')).toBeInTheDocument();
      });
    });

    it('shows a warning toast when login_enabled=false (Issue #2708)', async () => {
      renderConnections(['/settings/connections?github_app=registered&login_enabled=false']);

      await waitFor(() => {
        expect(
          screen.getByText(/"Sign in with GitHub" is not wired yet/i),
        ).toBeInTheDocument();
      });
    });
  });

  describe('Error callback (query params ?error=...&message=...)', () => {
    it('shows error toast with message from query param', async () => {
      renderConnections(['/settings/connections?error=expired_nonce&message=Token+expired']);

      await waitFor(() => {
        expect(screen.getByText('Token expired')).toBeInTheDocument();
      });
    });

    it('shows fallback error message when message param is missing', async () => {
      renderConnections(['/settings/connections?error=unknown_error']);

      await waitFor(() => {
        expect(
          screen.getByText('GitHub connection failed (unknown_error). Please try again.'),
        ).toBeInTheDocument();
      });
    });
  });

  it('uses the full workspace transition for the installation callback switch-back', async () => {
    vi.mocked(switchWorkspace).mockResolvedValue();
    renderConnections(['/settings/connections?success=1&installed=SOPHOS-IT&switched_from=home']);
    await userEvent.click(await screen.findByRole('button', { name: 'Switch back' }));
    expect(switchWorkspace).toHaveBeenCalledWith('home');
  });

  describe('Connection card rendering', () => {
    const mockConnection = {
      provider: 'github',
      installation_id: 12345,
      account_login: 'my-org',
      account_type: 'Organization',
      repository_selection: 'selected',
      repository_count: 5,
      installed_at: '2026-05-01T10:00:00Z',
      configure_url: 'https://github.com/organizations/my-org/settings/installations/12345',
    };

    beforeEach(() => {
      mockListConnections.mockResolvedValue({ connections: [mockConnection] });
    });

    it('uses the full token transition from a GitHub workspace card', async () => {
      mockListConnections.mockResolvedValue({ connections: [{ ...mockConnection, tenant_id: 'work', tenant_name: 'SOPHOS-IT', is_active_tenant: false }] });
      vi.mocked(switchWorkspace).mockResolvedValue();
      renderConnections();
      await userEvent.click(await screen.findByRole('button', { name: 'Switch to this workspace' }));
      expect(switchWorkspace).toHaveBeenCalledWith('work');
      expect(mockListConnections).toHaveBeenCalledTimes(1);
    });

    it('renders org name and installation ID', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('my-org')).toBeInTheDocument();
        expect(screen.getByText(/Installation #12345/)).toBeInTheDocument();
      });
    });

    it('renders repository count', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText(/5 repos/)).toBeInTheDocument();
      });
    });

    it('shows "All repositories" when selection is all', async () => {
      mockListConnections.mockResolvedValue({
        connections: [{ ...mockConnection, repository_selection: 'all' }],
      });

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText(/All repositories/)).toBeInTheDocument();
      });
    });

    it('renders Manage repositories link pointing to correct URL', async () => {
      renderConnections();

      await waitFor(() => {
        const link = screen.getByText(/Manage repositories/);
        expect(link).toBeInTheDocument();
        expect(link.closest('a')).toHaveAttribute('href', mockConnection.configure_url);
        expect(link.closest('a')).toHaveAttribute('target', '_blank');
      });
    });
  });

  describe('Disconnect flow', () => {
    const mockConnection = {
      provider: 'github',
      installation_id: 12345,
      account_login: 'my-org',
      account_type: 'Organization',
      repository_selection: 'selected',
      repository_count: 3,
      installed_at: '2026-05-01T10:00:00Z',
      configure_url: 'https://github.com/organizations/my-org/settings/installations/12345',
      can_manage: true,
    };

    beforeEach(() => {
      mockListConnections.mockResolvedValue({ connections: [mockConnection] });
      mockDeleteGitHubConnection.mockResolvedValue({
        deleted: true,
        installation_id: 12345,
      });
    });

    it('retains pending cleanup and warning until an authorized retry completes', async () => {
      const user = userEvent.setup();
      const warning = 'Local access is revoked. Retry to finish the pending cleanup or provider uninstall.';
      mockDeleteGitHubConnection.mockResolvedValue({
        deleted: true, installation_id: 12345, local_revoked: true,
        provider_revoked: false, residual: ['provider_uninstall'], warning,
      });
      renderConnections();
      await user.click(await screen.findByRole('button', { name: 'Disconnect' }));
      mockListConnections.mockResolvedValue({ connections: [{ ...mockConnection, revocation_pending: true }] });
      await user.click(screen.getByRole('button', { name: 'Confirm?' }));
      expect(await screen.findByText(warning)).toBeInTheDocument();
      expect(await screen.findByText('Access revoked · cleanup pending')).toBeInTheDocument();
      expect(screen.queryByRole('link', { name: /Manage repositories/ })).not.toBeInTheDocument();
      expect(screen.queryByText('GitHub installation disconnected.')).not.toBeInTheDocument();

      mockListConnections.mockResolvedValue({ connections: [] });
      mockDeleteGitHubConnection.mockResolvedValue({ deleted: true, installation_id: 12345, provider_revoked: true, residual: [] });
      await user.click(screen.getByRole('button', { name: 'Retry cleanup' }));
      await waitFor(() => expect(mockDeleteGitHubConnection).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(screen.queryByText('Access revoked · cleanup pending')).not.toBeInTheDocument());
    });

    it('reports local-only completion honestly', async () => {
      const user = userEvent.setup();
      mockDeleteGitHubConnection.mockResolvedValue({ deleted: true, installation_id: 12345, local_revoked: true, provider_revoked: false });
      renderConnections();
      await user.click(await screen.findByRole('button', { name: 'Disconnect' }));
      mockListConnections.mockResolvedValue({ connections: [] });
      await user.click(screen.getByRole('button', { name: 'Confirm?' }));
      expect(await screen.findByText('Local access revoked. The installation remains at GitHub.')).toBeInTheDocument();
    });

    it('shows Disconnect button on card', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Disconnect')).toBeInTheDocument();
      });
    });

    it('first click shows Confirm, second click calls deleteGitHubConnection', async () => {
      const user = userEvent.setup();

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Disconnect')).toBeInTheDocument();
      });

      // First click — shows confirmation
      await user.click(screen.getByText('Disconnect'));
      expect(screen.getByText('Confirm?')).toBeInTheDocument();

      // After delete, the list should refresh with empty result
      mockListConnections.mockResolvedValue({ connections: [] });

      // Second click — performs delete
      await user.click(screen.getByText('Confirm?'));

      await waitFor(() => {
        expect(mockDeleteGitHubConnection).toHaveBeenCalledWith(12345);
      });
    });

    it('shows success toast after disconnect', async () => {
      const user = userEvent.setup();

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Disconnect')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Disconnect'));
      await user.click(screen.getByText('Confirm?'));

      await waitFor(() => {
        expect(screen.getByText('GitHub installation disconnected.')).toBeInTheDocument();
      });
    });
  });

  describe('Manual registration warnings (Issue #3360)', () => {
    it('renders page-level warnings after successful manual registration', async () => {
      // Override useAuth to return platform admin
      mockUseAuth.mockReturnValue({
        user: { orgId: 'org-test' },
        hasRole: () => true,
      });

      // Initially the app is NOT registered (so the form is shown)
      mockGetGitHubAppStatus.mockResolvedValue({
        registered: false,
        app_slug: null,
        app_id: null,
        owner_type: null,
        created_at: null,
      });

      // After successful registration, status shows registered
      const warningMessages = [
        'Webhook URL mismatch: App has "https://old.example.com", deployment expects "https://new.example.com".',
        'Missing permissions: contents: write.',
      ];

      mockRegisterManualGitHubApp.mockResolvedValue({
        registered: true,
        app_id: '123456',
        app_slug: 'my-app',
        app_name: 'My App',
        login_enabled: true,
        warnings: warningMessages,
      });

      const user = userEvent.setup();
      renderConnections();

      // Wait for the manual registration expand button to appear
      await waitFor(() => {
        expect(screen.getByTestId('expand-manual-register')).toBeInTheDocument();
      });

      // Expand the manual registration form
      await user.click(screen.getByTestId('expand-manual-register'));

      // Fill in required fields
      const appIdInput = screen.getByLabelText(/App ID/i);
      const privateKeyInput = screen.getByLabelText(/Private Key/i);
      await user.type(appIdInput, '123456');
      await user.type(privateKeyInput, '-----BEGIN RSA PRIVATE KEY-----');

      // After submit, loadAppStatus will be called — mock it to return registered
      mockGetGitHubAppStatus.mockResolvedValue({
        registered: true,
        app_slug: 'my-app',
        app_id: '123456',
        owner_type: 'Organization',
        created_at: '2026-07-09T00:00:00Z',
      });

      // Submit the form
      const submitButton = screen.getByRole('button', { name: /Connect App/i });
      await user.click(submitButton);

      // Warnings should render at page level (persist after form unmounts)
      await waitFor(() => {
        expect(screen.getByText('Configuration warnings')).toBeInTheDocument();
      });

      // Verify individual warning messages are rendered
      for (const warning of warningMessages) {
        expect(screen.getByText(warning)).toBeInTheDocument();
      }
    });
  });
});
