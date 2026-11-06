import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ToastProvider } from '@/contexts/ToastContext';
import ProductConnectionSetup from '@/pages/products/ProductConnectionSetup';
import * as jira from '@/services/jiraContext';
import * as github from '@/services/connections';
import * as oauth from '@/services/oauthConnections';
import * as credentials from '@/services/credentials';

const { admin, devlake } = vi.hoisted(() => ({ admin: { value: true }, devlake: { enabled: false } }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'user', orgId: 'org' }, hasRole: () => admin.value }) }));
vi.mock('@/hooks/useFeatures', () => ({ useRevalidatingFeaturesQuery: () => ({ data: { assistant_ui: false, devlake_connectors: devlake.enabled } }) }));
vi.mock('@/services/jiraContext', () => ({
  jiraConnections: vi.fn(), jiraSources: vi.fn(), jiraResources: vi.fn(), selectJiraSource: vi.fn(),
  listJiraSystemConnections: vi.fn(), saveJiraSystemConnection: vi.fn(), updateJiraConnectionAccess: vi.fn(),
}));
vi.mock('@/services/connections', () => ({ listConnections: vi.fn(), getGitHubAppStatus: vi.fn(), startGitHubInstall: vi.fn(), startGitHubAppRegistration: vi.fn() }));
vi.mock('@/services/oauthConnections', () => ({ getOAuthProviders: vi.fn(), getAtlassianConnectionStatus: vi.fn(), getOAuthApp: vi.fn(), automateSlackApp: vi.fn(), startOAuthConnection: vi.fn() }));
vi.mock('@/services/credentials', async original => ({ ...await original<typeof import('@/services/credentials')>(), listCredentials: vi.fn(), startAwsConnect: vi.fn(), verifyAwsConnect: vi.fn(), registerProfileCredential: vi.fn() }));

function Location() { const location = useLocation(); return <output data-testid="location">{location.pathname}{location.search}</output>; }
function setup() {
  const onClose = vi.fn(); const onSaved = vi.fn();
  render(<MemoryRouter initialEntries={['/products/product/projects/project']}><ToastProvider>
    <ProductConnectionSetup productId="product" projectId="project" onClose={onClose} onSaved={onSaved} /><Location />
  </ToastProvider></MemoryRouter>);
  return { onClose, onSaved };
}
beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear(); admin.value = true; devlake.enabled = false;
  vi.mocked(jira.listJiraSystemConnections).mockResolvedValue({ connections: [] });
  vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [] });
  vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [] });
  vi.mocked(github.listConnections).mockResolvedValue({ connections: [] });
  vi.mocked(github.getGitHubAppStatus).mockResolvedValue({ registered: true, app_slug: 'test-app', app_id: '1', owner_type: 'Organization', created_at: null });
  vi.mocked(credentials.listCredentials).mockResolvedValue([]);
  vi.mocked(oauth.getOAuthProviders).mockResolvedValue({ providers: [{ provider: 'slack', service: 'slack', status: 'registered' }] });
});

describe('Product connection setup', () => {
  it('gates connector setup and stores a read-only SonarQube connection in the existing vault', async () => {
    devlake.enabled = true;
    const { onSaved } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'SonarQube' }));
    const dialog = screen.getByRole('dialog', { name: 'Connect SonarQube' });
    vi.mocked(credentials.registerProfileCredential).mockResolvedValue({ id: 'credential' } as never);
    fireEvent.change(within(dialog).getByLabelText('Connection label'), { target: { value: 'My quality projects' } });
    fireEvent.change(within(dialog).getByLabelText('Provider HTTPS endpoint'), { target: { value: 'https://sonar.example.test' } });
    fireEvent.change(within(dialog).getByLabelText('Read-only token'), { target: { value: 'fixture-only' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Register credential' }));
    await waitFor(() => expect(credentials.registerProfileCredential).toHaveBeenCalledOnce());
    const [operationId, request] = vi.mocked(credentials.registerProfileCredential).mock.calls[0];
    expect(operationId).toMatch(/^[a-f0-9-]{36}$/);
    expect(request).toMatchObject({ service: 'sonarqube', credential_type: 'bearer', scope_hint: 'user' });
    expect(JSON.parse(request.value)).toEqual({ url: 'https://sonar.example.test', token: 'fixture-only' });
    expect(await within(dialog).findByText(/Credential registered/)).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalledOnce();
  });

  it('does not offer connector credential setup when the master flag is off', () => {
    setup();
    expect(screen.queryByRole('button', { name: 'SonarQube' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Slack', exact: true })).toBeInTheDocument();
  });

  it.each(['api_key', 'bearer'] as const)('registers Linear %s credentials without changing their authentication type', async (credentialType) => {
    devlake.enabled = true;
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Linear', exact: true }));
    const dialog = screen.getByRole('dialog', { name: 'Connect Linear' });
    vi.mocked(credentials.registerProfileCredential).mockResolvedValue({ id: 'credential' } as never);
    expect(within(dialog).getByLabelText('Linear credential type')).toHaveValue('api_key');
    fireEvent.change(within(dialog).getByLabelText('Linear credential type'), { target: { value: credentialType } });
    fireEvent.change(within(dialog).getByLabelText('Connection label'), { target: { value: 'Team access' } });
    fireEvent.change(within(dialog).getByLabelText('Provider HTTPS endpoint'), { target: { value: 'https://api.linear.app/graphql' } });
    fireEvent.change(within(dialog).getByLabelText('Read-only token'), { target: { value: 'fixture-only' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Register credential' }));
    await waitFor(() => expect(credentials.registerProfileCredential).toHaveBeenCalledOnce());
    const [, request] = vi.mocked(credentials.registerProfileCredential).mock.calls[0];
    expect(request).toMatchObject({ service: 'linear', credential_type: credentialType, scope_hint: 'user' });
    expect(JSON.parse(request.value)).toEqual({ url: 'https://api.linear.app/graphql', token: 'fixture-only' });
    expect(await within(dialog).findByText(/Credential registered/)).toBeInTheDocument();
  });

  it('opens the Jira credential form directly and completes source selection inside the dialog', async () => {
    setup(); fireEvent.click(screen.getByRole('button', { name: 'Jira', exact: true }));
    const dialog = screen.getByRole('dialog', { name: 'Connect Jira' });
    expect(within(dialog).getByLabelText('Service account token')).toHaveAttribute('type', 'password');
    expect(within(dialog).queryByText('Set up shared Jira')).not.toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Verify and save' })).toBeEnabled());
    fireEvent.change(within(dialog).getByLabelText('Jira site ID'), { target: { value: 'test-site' } });
    fireEvent.change(within(dialog).getByLabelText('Service account token'), { target: { value: 'test-key' } });
    fireEvent.change(within(dialog).getByLabelText('Token expiry date (UTC)'), { target: { value: '2099-01-01' } });
    vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [{ id: 'shared', label: 'Team Jira', site_id: 'site' }] });
    vi.mocked(jira.jiraResources).mockResolvedValue({ data: { values: [{ id: '100', name: 'Project', key: 'APP' }], isLast: true } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Verify and save' }));
    fireEvent.change(await within(dialog).findByLabelText('Shared connection'), { target: { value: 'shared' } });
    await within(dialog).findByRole('option', { name: 'Project (APP)' });
    fireEvent.change(within(dialog).getByLabelText('Jira project'), { target: { value: '100' } });
    await waitFor(() => expect(within(dialog).getByLabelText(/Share this source/)).toBeEnabled());
    vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [{ id: 'source', revision: 1, available: true, connection_id: 'shared', site_id: 'site', label: 'Project', jira_project_id: '100', jira_project_key: 'APP', board_id: null }] });
    fireEvent.click(within(dialog).getByLabelText(/Share this source/));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Connect Jira source' }));
    expect(await within(dialog).findByRole('button', { name: 'Done — return to Product' })).toBeInTheDocument();
    expect(jira.saveJiraSystemConnection).toHaveBeenCalledWith(expect.objectContaining({ product_ids: ['product'] }), undefined);
    expect(jira.selectJiraSource).toHaveBeenCalledWith('product', 'project', 'shared', expect.objectContaining({ key: 'APP' }), undefined);
    expect(screen.getByTestId('location')).toHaveTextContent('/products/product/projects/project');
  });

  it('keeps AWS verification and retry in the Product and does not claim success for a denied role', async () => {
    const { onClose } = setup();
    vi.spyOn(window, 'open').mockImplementation(() => null);
    vi.mocked(credentials.startAwsConnect).mockResolvedValue({ credential_id: 'aws', launch_url: 'https://example.org/stack' });
    vi.mocked(credentials.verifyAwsConnect).mockResolvedValueOnce({ status: 'failed', reason: 'trust_verification_failed' }).mockResolvedValue({ status: 'verified' });
    fireEvent.click(screen.getByRole('button', { name: 'AWS account' }));
    fireEvent.change(screen.getByLabelText(/Nickname/), { target: { value: 'team-dev' } });
    fireEvent.change(screen.getByLabelText(/AWS Account ID/), { target: { value: '123456789012' } });
    fireEvent.click(screen.getByRole('button', { name: /Launch CloudFormation/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Verify & Save/ }));
    expect(await screen.findByText(/AWS connection verification failed/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Done — return to Product' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry Verify' }));
    expect(await screen.findByRole('button', { name: 'Done — return to Product' })).toBeInTheDocument();
    expect(credentials.verifyAwsConnect).toHaveBeenLastCalledWith({ credential_id: 'aws' });
    expect(screen.getByTestId('location')).toHaveTextContent('/products/product/projects/project');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('opens Slack administrator setup inline, then offers user consent without leaving the Product', async () => {
    vi.mocked(oauth.getOAuthProviders).mockResolvedValue({ providers: [{ provider: 'slack', service: 'slack', status: 'setup_required' }] });
    vi.mocked(oauth.getOAuthApp).mockResolvedValue({ provider: 'slack', service: 'slack', status: 'setup_required', developer_url: 'https://api.slack.com/apps', scopes: [] });
    vi.mocked(oauth.automateSlackApp).mockResolvedValue({ provider: 'slack', service: 'slack', status: 'registered', developer_url: 'https://api.slack.com/apps', scopes: [], automation_state: 'ready' });
    setup(); fireEvent.click(screen.getByRole('button', { name: 'Slack', exact: true }));
    fireEvent.change(await screen.findByLabelText('App configuration access token'), { target: { value: 'setup-test-key' } });
    vi.mocked(oauth.getOAuthProviders).mockResolvedValue({ providers: [{ provider: 'slack', service: 'slack', status: 'registered' }] });
    fireEvent.click(screen.getByRole('button', { name: 'Create and register Slack app' }));
    expect(await screen.findByRole('button', { name: 'Connect Slack' })).toBeEnabled();
    expect(oauth.automateSlackApp).toHaveBeenCalledWith('setup-test-key');
    expect(screen.queryByLabelText('App configuration access token')).not.toBeInTheDocument();
    expect(oauth.getAtlassianConnectionStatus).not.toHaveBeenCalled();
    expect(github.listConnections).not.toHaveBeenCalled();
    expect(screen.getByTestId('location')).toHaveTextContent('/products/product/projects/project');
  });

  it('uses the existing GitHub App install action and remembers the Product for its callback', async () => {
    vi.mocked(github.startGitHubInstall).mockResolvedValue({ install_url: 'https://github.com/apps/test-app/installations/new', state_token: 'test-state', expires_at: '2099-01-01T00:00:00Z' });
    setup(); fireEvent.click(screen.getByRole('button', { name: 'GitHub', exact: true }));
    fireEvent.click(await screen.findByRole('button', { name: 'Install on GitHub' }));
    await waitFor(() => expect(sessionStorage.getItem('adp_product_connection_return')).not.toBeNull());
    expect(github.startGitHubInstall).toHaveBeenCalledOnce();
    expect(github.startGitHubAppRegistration).not.toHaveBeenCalled();
    expect(oauth.getOAuthProviders).not.toHaveBeenCalled();
    expect(JSON.parse(sessionStorage.getItem('adp_product_connection_return')!)).toMatchObject({ productId: 'product', projectId: 'project', userId: 'user', orgId: 'org', service: 'github' });
  });

  it('explains the administrator requirement without exposing Jira credentials to members', async () => {
    admin.value = false;
    setup(); fireEvent.click(screen.getByRole('button', { name: 'Jira', exact: true }));
    expect(await screen.findByText(/Shared credentials require a platform administrator/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Service account token')).not.toBeInTheDocument();
    expect(jira.listJiraSystemConnections).not.toHaveBeenCalled();
  });

  it('clears a Jira token when changing service and cannot mark another service ready from an old AWS verification', async () => {
    let finish!: (value: credentials.ConnectVerifyResponse) => void;
    vi.spyOn(window, 'open').mockImplementation(() => null);
    vi.mocked(credentials.startAwsConnect).mockResolvedValue({ credential_id: 'aws', launch_url: 'https://example.org/stack' });
    vi.mocked(credentials.verifyAwsConnect).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    setup(); fireEvent.click(screen.getByRole('button', { name: 'Jira', exact: true }));
    fireEvent.change(screen.getByLabelText('Service account token'), { target: { value: 'discard-me' } });
    fireEvent.click(screen.getByRole('button', { name: 'Choose another service' }));
    fireEvent.click(screen.getByRole('button', { name: 'Jira', exact: true }));
    expect(screen.getByLabelText('Service account token')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Choose another service' }));
    fireEvent.click(screen.getByRole('button', { name: 'AWS account' }));
    fireEvent.change(screen.getByLabelText(/Nickname/), { target: { value: 'dev' } });
    fireEvent.change(screen.getByLabelText(/AWS Account ID/), { target: { value: '123456789012' } });
    fireEvent.click(screen.getByRole('button', { name: /Launch CloudFormation/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Verify & Save/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Choose another service' }));
    fireEvent.click(screen.getByRole('button', { name: 'Slack', exact: true }));
    await act(async () => finish({ status: 'verified' }));
    expect(screen.queryByRole('button', { name: 'Done — return to Product' })).not.toBeInTheDocument();
  });
});
