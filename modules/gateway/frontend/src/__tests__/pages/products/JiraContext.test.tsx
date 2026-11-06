import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import JiraContextSources from '@/pages/products/JiraContextSources';
import JiraSystemConnection from '@/pages/settings/JiraSystemConnection';
import * as jira from '@/services/jiraContext';
import { listProducts } from '@/services/products';
import { AdminRole } from '@/types';

const { hasRole } = vi.hoisted(() => ({ hasRole: vi.fn() }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ hasRole }) }));

vi.mock('@/services/jiraContext', () => ({
  jiraConnections: vi.fn(), jiraResources: vi.fn(), jiraSources: vi.fn(), readJiraSource: vi.fn(),
  selectJiraSource: vi.fn(), unlinkJiraSource: vi.fn(), listJiraSystemConnections: vi.fn(),
  saveJiraSystemConnection: vi.fn(), disconnectJiraSystemConnection: vi.fn(), verifyJiraSystemConnection: vi.fn(),
  startJiraForgePairing: vi.fn(), updateJiraConnectionAccess: vi.fn(),
}));
vi.mock('@/services/products', () => ({ listProducts: vi.fn() }));
const source: jira.JiraSource = { id: 'source', revision: 1, connection_id: 'shared', site_id: 'site', label: 'App', jira_project_id: '100', jira_project_key: 'APP', board_id: 5 };
beforeEach(() => {
  vi.resetAllMocks();
  hasRole.mockReturnValue(false);
  vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [{ id: 'shared', label: 'Team Jira', site_id: 'site' }] });
  vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [] });
  vi.mocked(jira.jiraResources).mockImplementation(async (_p, _j, _c, kind) => ({ data: { values: kind === 'projects' ? [{ id: '100', key: 'APP', name: 'App' }] : [{ id: 5, name: 'App board' }], isLast: true } }));
  vi.mocked(jira.listJiraSystemConnections).mockResolvedValue({ connections: [] });
  vi.mocked(listProducts).mockResolvedValue({ items: [{ id: 'product', name: 'Example Product', project_ids: ['project'], revision: 1 }], next_cursor: null });
});

describe('Shared Jira', () => {
  it('browses the clicked Jira connection and links a project with explicit sharing', async () => {
    vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [
      { id: 'shared', label: 'Team Jira', site_id: 'site' },
      { id: 'other', label: 'Other Jira', site_id: 'other-site' },
    ] });
    vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [{ ...source, id: 'other-source', connection_id: 'other', label: 'Other source' }] });
    render(<JiraContextSources productId="product" projectId="project" onSaved={vi.fn()} list />);
    fireEvent.click(await screen.findByRole('button', { name: 'Browse Team Jira' }));
    expect(await screen.findByRole('option', { name: 'App (APP)' })).toBeInTheDocument();
    expect(jira.jiraResources).toHaveBeenCalledWith('product', 'project', 'shared', 'projects', '', 0);
    expect(screen.queryByLabelText('Shared connection')).not.toBeInTheDocument();
    expect(screen.queryByText(/Other source/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Jira project'), { target: { value: '100' } });
    await screen.findByRole('option', { name: 'App board' });
    expect(screen.getByRole('button', { name: 'Connect Jira source' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Share this source/));
    vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [source] });
    fireEvent.click(screen.getByRole('button', { name: 'Connect Jira source' }));
    expect(await screen.findByRole('button', { name: 'Read backlog' })).toBeEnabled();
    expect(jira.selectJiraSource).toHaveBeenCalledWith('product', 'project', 'shared', expect.objectContaining({ id: '100' }), undefined);
    expect(screen.getByRole('button', { name: 'Browse Team Jira' })).toHaveTextContent('1 linked');
    fireEvent.click(screen.getByRole('button', { name: 'Browse Other Jira' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Browse Other Jira' })).toHaveAttribute('aria-expanded', 'true'));
    expect(screen.queryByRole('button', { name: 'Read backlog' })).not.toBeInTheDocument();
  });

  it('keeps an unavailable linked source removable from its connection row', async () => {
    vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [] });
    const unavailable = { ...source, available: false };
    vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [unavailable] });
    render(<JiraContextSources productId="product" projectId="project" onSaved={vi.fn()} list />);
    fireEvent.click(await screen.findByRole('button', { name: 'Browse Unavailable Jira connection' }));
    expect(screen.getByRole('button', { name: 'Read issues' })).toBeDisabled();
    expect(jira.jiraResources).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Remove source' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Browse Unavailable Jira connection' })).not.toBeInTheDocument());
    expect(jira.unlinkJiraSource).toHaveBeenCalledWith('product', 'project', unavailable);
  });

  it('enables an existing shared identity inside setup without a new token or removing other grants', async () => {
    const connection: jira.JiraSystemConnection = { id: 'shared', revision: 'r7', label: 'Existing Jira', site_id: 'site', account_id: null,
      product_ids: ['another-product'], status: 'ready', expires_at: '2099-01-01T00:00:00Z', verified_at: null, capabilities: [] };
    vi.mocked(jira.listJiraSystemConnections).mockResolvedValue({ connections: [connection] });
    render(<JiraSystemConnection productId="product" initialSetup />);
    fireEvent.click(await screen.findByText('Use an existing shared Jira connection'));
    fireEvent.click(screen.getByRole('button', { name: 'Use Existing Jira for this Product' }));
    await waitFor(() => expect(jira.updateJiraConnectionAccess).toHaveBeenCalledWith('shared', { label: 'Existing Jira', product_ids: ['another-product', 'product'], expected_revision: 'r7' }));
    expect(jira.saveJiraSystemConnection).not.toHaveBeenCalled();
    expect(await screen.findByText(/Shared Jira access enabled/)).toBeInTheDocument();
  });
  it('sets up shared access for the current Product and immediately offers its Jira projects', async () => {
    hasRole.mockImplementation(role => role === AdminRole.PLATFORM_ADMIN);
    vi.mocked(jira.jiraConnections).mockResolvedValueOnce({ connections: [] });
    render(<JiraContextSources productId="product" projectId="project" onSaved={vi.fn()} />);
    fireEvent.click(await screen.findByText('Set up shared Jira'));
    expect(listProducts).not.toHaveBeenCalled();
    expect(screen.queryByText('Products allowed to use this connection')).not.toBeInTheDocument();
    expect(screen.getByText(/available to this Product only/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Jira site ID'), { target: { value: '12345678-1234-1234-1234-123456789abc' } });
    fireEvent.change(screen.getByLabelText('Service account token'), { target: { value: 'test-service-key' } });
    fireEvent.change(screen.getByLabelText('Token expiry date (UTC)'), { target: { value: '2099-01-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    expect(await screen.findByText('Shared Jira connection verified. Choose a Jira project below.')).toBeInTheDocument();
    expect(jira.saveJiraSystemConnection).toHaveBeenCalledWith({ label: 'Shared Jira', site_id: '12345678-1234-1234-1234-123456789abc', api_key: 'test-service-key', expires_at: '2099-01-01T00:00:00.000Z', product_ids: ['product'] }, undefined);
    expect(screen.queryByLabelText('Service account token')).not.toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText('Shared connection'), { target: { value: 'shared' } });
    expect(await screen.findByRole('option', { name: 'App (APP)' })).toBeInTheDocument();
    expect(jira.jiraResources).toHaveBeenCalledWith('product', 'project', 'shared', 'projects', '', 0);
  });

  it.each([false, true])('keeps credential administration out of the member view (connected: %s)', async connected => {
    if (!connected) vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [] });
    render(<JiraContextSources productId="product" projectId="project" onSaved={vi.fn()} />);
    if (connected) expect(await screen.findByLabelText('Shared connection')).toBeInTheDocument();
    else expect(await screen.findByText(/Ask a platform admin to set up shared Jira here/)).toBeInTheDocument();
    expect(jira.listJiraSystemConnections).not.toHaveBeenCalled();
    expect(screen.queryByText('Set up shared Jira')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Service account token')).not.toBeInTheDocument();
  });

  it('shows only this Product’s connections and preserves all grants when replacing a shared token', async () => {
    const connection: jira.JiraSystemConnection = { id: 'shared', revision: 'r7', label: 'Team Jira', site_id: 'site', account_id: null,
      product_ids: ['product', 'another-product'], status: 'ready', expires_at: '2099-01-01T00:00:00Z', verified_at: null, capabilities: [] };
    vi.mocked(jira.listJiraSystemConnections).mockResolvedValue({ connections: [connection, { ...connection, id: 'unrelated', label: 'Unrelated Jira', product_ids: ['other'] }] });
    render(<JiraSystemConnection productId="product" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Replace token' }));
    expect(screen.queryByText(/Unrelated Jira/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Revoke shared access' })).not.toBeInTheDocument();
    expect(screen.getByText(/also affects the other Products/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Service account token'), { target: { value: 'replacement-test-key' } });
    fireEvent.change(screen.getByLabelText('Token expiry date (UTC)'), { target: { value: '2099-02-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    await waitFor(() => expect(jira.saveJiraSystemConnection).toHaveBeenCalledWith(expect.objectContaining({ product_ids: ['product', 'another-product'], expected_revision: 'r7' }), 'shared'));
  });

  it('refreshes Product source choices when an app pairing becomes ready', async () => {
    const connection: jira.JiraSystemConnection = { id: 'app', revision: 'r1', label: 'Shared Jira', site_id: '', account_id: null,
      product_ids: ['product'], status: 'unverified', expires_at: '2099-01-01T00:00:00Z', verified_at: null, capabilities: [], connection_method: 'forge_app' };
    vi.mocked(jira.listJiraSystemConnections).mockResolvedValue({ connections: [], forge_available: true });
    vi.mocked(jira.startJiraForgePairing).mockResolvedValue({ connection, pairing_code: 'test-pairing-code', expires_at: connection.expires_at, install_url: 'https://example.org/install' });
    const onChanged = vi.fn();
    render(<JiraSystemConnection productId="product" onChanged={onChanged} />);
    fireEvent.click(await screen.findByText('Set up shared Jira'));
    fireEvent.click(screen.getByRole('button', { name: 'Create connection code' }));
    await screen.findByLabelText('Connection code');
    expect(jira.startJiraForgePairing).toHaveBeenCalledWith({ label: 'Shared Jira', product_ids: ['product'] });
    onChanged.mockClear();
    vi.mocked(jira.listJiraSystemConnections).mockResolvedValue({ connections: [{ ...connection, status: 'ready' }], forge_available: true });
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce(), { timeout: 6000 });
    expect(screen.getByText('ADP app connected. Choose a Jira project below.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Connection code')).not.toBeInTheDocument();
  }, 8000);

  it('offers both admin methods only when Forge is configured and creates a pairing without a user token', async () => {
    vi.mocked(jira.listJiraSystemConnections).mockResolvedValue({ connections: [], forge_available: true });
    const connection: jira.JiraSystemConnection = { id: 'app', revision: 'r1', label: 'Shared Jira', site_id: '', account_id: null,
      product_ids: ['product'], status: 'unverified', expires_at: '2099-01-01T00:00:00Z', verified_at: null, capabilities: [], connection_method: 'forge_app' };
    vi.mocked(jira.startJiraForgePairing).mockResolvedValue({ connection, pairing_code: 'a'.repeat(43), expires_at: connection.expires_at,
      install_url: 'https://developer.atlassian.com/console/install/example' });
    render(<JiraSystemConnection />);
    fireEvent.click(await screen.findByText('Set up shared Jira'));
    expect(screen.getByRole('radio', { name: 'Install ADP app' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Connect service account' })).not.toBeChecked();
    expect(screen.queryByLabelText('Service account token')).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Example Product'));
    fireEvent.click(screen.getByRole('button', { name: 'Create connection code' }));
    expect(await screen.findByLabelText('Connection code')).toHaveValue('a'.repeat(43));
    expect(jira.startJiraForgePairing).toHaveBeenCalledWith({ label: 'Shared Jira', product_ids: ['product'] });
    expect(jira.saveJiraSystemConnection).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: /Install or open/ })).toHaveAttribute('href', 'https://developer.atlassian.com/console/install/example');
  });

  it('explains pending app setup and prevents an unavailable installation from starting', async () => {
    render(<JiraSystemConnection />);
    fireEvent.click(await screen.findByText('Set up shared Jira'));
    expect(screen.getByRole('radio', { name: 'Install ADP app' })).toBeDisabled();
    expect(screen.getByText(/App setup needed/)).toBeInTheDocument();
    expect(screen.getByLabelText('Service account token')).toBeInTheDocument();
    expect(jira.startJiraForgePairing).not.toHaveBeenCalled();
  });

  it('defaults the Product setup form to the configured app without requiring a user token', async () => {
    vi.mocked(jira.listJiraSystemConnections).mockResolvedValue({ connections: [], forge_available: true });
    render(<JiraSystemConnection productId="product" initialSetup />);
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Install ADP app' })).toBeChecked());
    expect(screen.queryByLabelText('Service account token')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create connection code' })).toBeEnabled();
    fireEvent.click(screen.getByRole('radio', { name: 'Connect service account' }));
    expect(screen.getByLabelText('Service account token')).toBeInTheDocument();
    expect(jira.startJiraForgePairing).not.toHaveBeenCalled();
  });

  it('does not change identity method or discard a token when app status arrives after typing', async () => {
    let resolve!: (value: Awaited<ReturnType<typeof jira.listJiraSystemConnections>>) => void;
    vi.mocked(jira.listJiraSystemConnections).mockReturnValue(new Promise(done => { resolve = done; }));
    render(<JiraSystemConnection productId="product" initialSetup />);
    fireEvent.change(screen.getByLabelText('Service account token'), { target: { value: 'draft-test-token' } });
    await act(async () => resolve({ connections: [], forge_available: true }));
    expect(screen.getByRole('radio', { name: 'Connect service account' })).toBeChecked();
    expect(screen.getByLabelText('Service account token')).toHaveValue('draft-test-token');
    fireEvent.click(screen.getByRole('radio', { name: 'Install ADP app' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Connect service account' }));
    expect(screen.getByLabelText('Service account token')).toHaveValue('');
  });

  it('requires explicit Product sharing and reads a selected backlog through the saved source', async () => {
    render(<MemoryRouter><JiraContextSources productId="product" projectId="project" onSaved={vi.fn()} /></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText('Shared connection'), { target: { value: 'shared' } });
    await screen.findByRole('option', { name: 'App (APP)' });
    fireEvent.change(screen.getByLabelText('Jira project'), { target: { value: '100' } });
    await screen.findByRole('option', { name: 'App board' });
    fireEvent.change(screen.getByLabelText('Board'), { target: { value: '5' } });
    expect(screen.getByRole('button', { name: 'Connect Jira source' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Share this source/));
    vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [source] });
    fireEvent.click(screen.getByRole('button', { name: 'Connect Jira source' }));
    const read = await screen.findByRole('button', { name: 'Read backlog' });
    expect(jira.selectJiraSource).toHaveBeenCalledWith('product', 'project', 'shared', expect.objectContaining({ id: '100' }), 5);
    vi.mocked(jira.readJiraSource).mockResolvedValue({ source, view: 'backlog', data: { issues: [{ id: '200', key: 'APP-1', fields: { summary: 'Example issue', description: '<script>unsafe()</script>' } }] }, retrieved_at_ms: 1, next_cursor: null, coverage: 'provider_response' });
    fireEvent.click(read);
    expect(await screen.findByText(/APP-1: Example issue/)).toBeInTheDocument();
    expect(screen.getByText('<script>unsafe()</script>')).toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
    expect(jira.readJiraSource).toHaveBeenCalledWith('product', 'project', source, 'backlog', undefined, undefined);
  });

  it('drops a previous Project read after navigation', async () => {
    let finish!: (result: jira.JiraContextResult) => void;
    vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [source] });
    vi.mocked(jira.readJiraSource).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const page = render(<MemoryRouter><JiraContextSources key="a" productId="product" projectId="a" onSaved={vi.fn()} /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Read issues' }));
    page.rerender(<MemoryRouter><JiraContextSources key="b" productId="product" projectId="b" onSaved={vi.fn()} /></MemoryRouter>);
    await act(async () => finish({ source, view: 'issues', data: { issues: [{ id: '1', key: 'APP-1', fields: { summary: 'Old Project secret' } }] }, retrieved_at_ms: 1, next_cursor: null, coverage: 'provider_response' }));
    expect(screen.queryByText(/Old Project secret/)).not.toBeInTheDocument();
  });

  it('offers a Kanban backlog without claiming the board supports sprints', async () => {
    vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [{ ...source, board_type: 'kanban' }] });
    render(<MemoryRouter><JiraContextSources productId="product" projectId="project" onSaved={vi.fn()} /></MemoryRouter>);
    expect(await screen.findByRole('button', { name: 'Read backlog' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Read sprints' })).not.toBeInTheDocument();
  });

  it('clears the service key from the form on a failed verification', async () => {
    vi.mocked(jira.saveJiraSystemConnection).mockRejectedValue(new Error('provider unavailable'));
    render(<JiraSystemConnection />);
    fireEvent.click(await screen.findByText('Set up shared Jira'));
    fireEvent.change(screen.getByLabelText('Jira site ID'), { target: { value: '12345678-1234-1234-1234-123456789abc' } });
    fireEvent.change(screen.getByLabelText('Service account token'), { target: { value: 'secret-sentinel' } });
    fireEvent.change(screen.getByLabelText('Token expiry date (UTC)'), { target: { value: '2026-11-15' } });
    fireEvent.click(screen.getByLabelText('Example Product'));
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    await waitFor(() => expect(jira.saveJiraSystemConnection).toHaveBeenCalled());
    expect(screen.queryByLabelText('Service account token')).not.toBeInTheDocument();
    expect(localStorage.getItem('secret-sentinel')).toBeNull();
    expect(await screen.findByText(/Could not verify/)).toBeInTheDocument();
  });
});
