/**
 * Settings → Dev Box page — Issues #8094 / #8109 / #8140 (AC-01).
 *
 * One primary action, "Open my Dev Box": the click synchronously opens the interstitial tab
 * (`/settings/dev-box/open`, tested in DevBoxOpen.test.tsx), which runs the flow and becomes
 * the dashboard; this page only watches. Nothing here ever answers `/ssh-access`: the SSH
 * block must work without it, and the assertions prove no credential-shaped text reaches the DOM.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import DevBoxPage from '@/pages/settings/DevBox';
import { apiClient } from '@/services/api';
import { resolveCrewDashboardDestination } from '@/services/crewDashboard';
import { enrollCrewHost } from '@/services/crewHostEnrollment';
import { DEV_BOX_OPEN_PATH, consumeDevBoxOpenIntent } from '@/services/devBoxOpen';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn(), request: vi.fn() } }));
vi.mock('@/services/crewDashboard', () => ({ resolveCrewDashboardDestination: vi.fn() }));
vi.mock('@/services/crewHostEnrollment', () => ({ enrollCrewHost: vi.fn() }));
vi.mock('@/hooks/useFeatures', () => ({ useFeatures: () => ({ crew_dashboard: true, crew_dashboard_origin: 'https://crew.example.test' }) }));
const hasRole = vi.fn(() => false);
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ hasRole }) }));
const crewRuntime = { hostAllowed: true };
vi.mock('@/hooks/useCrewRuntime', () => ({ useCrewRuntime: () => ({ loading: false, allowed: false, hostAllowed: crewRuntime.hostAllowed, choice: 'kirocrew-ec2', options: [], setChoice: () => undefined }) }));
vi.mock('@/components/chat/CrewHostEnrollment', () => ({ default: () => <div data-testid="crew-host-enrollment" /> }));

const DESTINATION = 'https://crew.example.test/?token=test';
const INTERSTITIAL = new RegExp(`^${DEV_BOX_OPEN_PATH}\\?intent=[0-9a-f-]{36}$`);
const openedHref = () => String(vi.mocked(window.open).mock.calls[0][0]);
const openedTab = () => ({ closed: false, focus: vi.fn() }) as unknown as Window;
const cost = { estimated_cost_usd: '1.25', reconciled_cost_usd: null, total_cost_usd: null, coverage: 'partial_unknown',
  retained_resources: [{ kind: 'workspace_disk', size_gb: 30 }] };
const running = { desktop_id: 'owned', name: 'assistant', template: 'approved', template_revision: 3, profile: 'crew', status: 'running',
  observed_state: 'running', desired_state: 'running', machine_generation: 2, crew_bootstrap: 'user-data',
  lease_expires_at: new Date(Date.now() + 2 * 3_600_000).toISOString(), infrastructure_cost: cost };
const stopped = { ...running, status: 'stopped', observed_state: 'stopped', desired_state: 'stopped' };
const ready = { health: 'ready', lease: { target_kind: 'ec2-instance', target_ref: 'owned', generation: 2 } };
const resources = { contract_version: 1, items: [{ resource_id: 'r1', resource_kind: 'workspace_disk', provider_resource_id: 'vol-1',
  machine_generation: 2, retention_state: 'retained', deletion_state: null, encrypted: true, size_gb: 30 }] };

type Api = { items: unknown[]; snapshots: unknown[]; operation: unknown; policy: unknown; target: () => unknown };
const api: Api = { items: [], snapshots: [], operation: null, policy: { contract_version: 1, admission_allowed: true, reason: null }, target: () => ready };
let backupUnavailable = false;
const backupRecord = (sequence: number, status = 'verified') => ({
  id: `private-${sequence}`, status, sequence, captured_at: new Date(Date.now() - 10 * 60_000).toISOString(),
  archive_size: sequence * 1_048_576, failure_class: status === 'failed' ? 'verification_failed' : null,
  is_latest_verified: sequence === 6,
});
let backupRows: ReturnType<typeof backupRecord>[] = [];
let selectedBackupId: string | null = null;
let githubCredential: Record<string, unknown> | null = null;
let githubProvider: Record<string, unknown> | null = null;
const notEnrolled = () => { throw { status: 404, detail: { error: 'crew_target_missing' } }; };
const fenced = () => { throw { status: 409, detail: { error: 'lease_fenced' } }; };

const renderPage = () => render(<DevBoxPage />);
const primary = () => screen.getByRole('button', { name: /my Dev Box$/ });
const keys = () => vi.mocked(apiClient.request).mock.calls.map(([, options]) => (options?.headers as Record<string, string>)['Idempotency-Key']);
const posted = () => vi.mocked(apiClient.request).mock.calls.map(([endpoint]) => endpoint.split('/').pop());

beforeEach(() => {
  vi.restoreAllMocks();
  hasRole.mockReturnValue(false);
  api.items = [];
  api.snapshots = [];
  backupUnavailable = false;
  backupRows = [];
  selectedBackupId = null;
  githubCredential = null;
  githubProvider = null;
  api.operation = { desktop_id: 'owned', operation_id: 'op', status: 'succeeded', progress: 'running' };
  api.policy = { contract_version: 1, admission_allowed: true, reason: null };
  api.target = () => ready;
  localStorage.clear();
  vi.spyOn(window, 'open').mockReturnValue(openedTab());
  vi.mocked(enrollCrewHost).mockReset().mockResolvedValue(undefined);
  vi.mocked(apiClient.post).mockReset();
  vi.mocked(apiClient.get).mockReset().mockImplementation(async endpoint => {
    if (endpoint === '/auth/credentials') return githubCredential ? [githubCredential] : [];
    if (endpoint === '/auth/connections/oauth/providers') return { providers: githubProvider ? [githubProvider] : [] };
    if (endpoint === '/v1/crew/checkpoints') {
      if (backupUnavailable) throw { status: 503 };
      return { checkpoints: backupRows };
    }
    if (endpoint === '/v1/crew/checkpoints/selection') return { checkpoint_id: selectedBackupId };
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items: api.items };
    if (endpoint === '/cloud-desktops/policy') return api.policy;
    if (endpoint === '/cloud-desktops/crew-template') return { contract_version: 1, template: 'approved' };
    if (endpoint === '/cloud-desktops/owned') return api.items[0] ?? running;
    if (endpoint === '/cloud-desktops/owned/resources') return resources;
    if (endpoint === '/cloud-desktops/owned/snapshots') return { contract_version: 1, items: api.snapshots };
    if (endpoint === '/cloud-desktops/owned/operations/op') return api.operation;
    if (endpoint === '/crew/target') return api.target();
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockReset().mockImplementation(async (endpoint, options) => {
    if (endpoint === '/cloud-desktops/ensure') {
      api.items = [running];
      return { contract_version: 1, desktop_id: 'owned', operation_id: 'op', status: 'pending', progress: 'requested' };
    }
    const action = endpoint.split('/').pop();
    if (action === 'stop') api.items = [stopped];
    if (action === 'start' || action === 'replace') api.items = [running];
    if (action === 'release') api.items = [];
    return { contract_version: 1, desktop_id: 'owned', operation_id: 'op', status: 'pending', progress: `${action}:${JSON.stringify(options?.body ?? null)}` };
  });
  vi.mocked(resolveCrewDashboardDestination).mockReset().mockResolvedValue(DESTINATION);
});

describe('GitHub on this box', () => {
  const credential = (scopes: Record<string, unknown>, consent = true) => ({
    id: 'owned-github', service: 'github', provider: 'agentcore-identity', scope: 'user', scopes, agent_consent: consent,
  });
  const appGrant = { github_app_grant: { schema_version: 1, repository_access: true } };
  const card = () => screen.getByRole('region', { name: 'GitHub on this box' });

  it('shows exactly one consent button when the owner has no connection', async () => {
    githubProvider = { provider: 'github', service: 'github', status: 'registered', connection_status: 'not_connected' };
    renderPage();
    await waitFor(() => expect(within(card()).getByRole('button', { name: 'Use GitHub as you on this box' })).toBeInTheDocument());
    expect(within(card()).getAllByRole('button')).toHaveLength(1);
  });

  it('starts repository consent and retains a return-to-box intent', async () => {
    githubProvider = { provider: 'github', service: 'github', status: 'registered', connection_status: 'not_connected' };
    vi.mocked(apiClient.post).mockResolvedValue({ authorization_url: 'https://github.example.test/authorize', state: 'nonce', session_uri: 'opaque' });
    const location = { ...window.location, href: '' };
    vi.spyOn(window, 'location', 'get').mockReturnValue(location as Location);
    renderPage();
    fireEvent.click(await within(card()).findByRole('button', { name: 'Use GitHub as you on this box' }));
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith('/auth/connections/oauth/github/start', { repository_access: true }));
    expect(JSON.parse(window.sessionStorage.getItem('oauth_connection_pending')!)).toEqual({
      state: 'nonce', session_uri: 'opaque', service: 'github', return_to: '/settings/dev-box',
    });
    expect(location.href).toBe('https://github.example.test/authorize');
  });

  it.each(['request rejected', 'invalid response'])('keeps paused App consent retryable after %s', async (failure) => {
    githubCredential = credential(appGrant, false);
    githubProvider = { provider: 'github', status: 'registered', connection_status: 'consent_paused' };
    window.sessionStorage.removeItem('oauth_connection_pending');
    if (failure === 'request rejected') vi.mocked(apiClient.post).mockRejectedValueOnce(new Error('private fixture'));
    else vi.mocked(apiClient.post).mockResolvedValueOnce({ authorization_url: '', state: 'nonce', session_uri: 'opaque' });
    vi.mocked(apiClient.post).mockResolvedValueOnce({ authorization_url: 'https://github.example.test/authorize', state: 'nonce', session_uri: 'opaque' });
    const location = { ...window.location, href: '' };
    vi.spyOn(window, 'location', 'get').mockReturnValue(location as Location);
    renderPage();
    const button = await within(card()).findByRole('button', { name: 'Reconnect GitHub for this box' });
    fireEvent.click(button);
    expect(await within(card()).findByRole('alert')).toHaveTextContent('Could not start GitHub consent. Try again.');
    expect(button).toBeEnabled();
    expect(window.sessionStorage.getItem('oauth_connection_pending')).toBeNull();
    expect(within(card()).queryByText(/private fixture/)).not.toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledTimes(2));
    expect(JSON.parse(window.sessionStorage.getItem('oauth_connection_pending')!)).toEqual({
      state: 'nonce', session_uri: 'opaque', service: 'github', return_to: '/settings/dev-box',
    });
    expect(within(card()).queryByRole('alert')).not.toBeInTheDocument();
    expect(location.href).toBe('https://github.example.test/authorize');
  });

  it('shows a connected login without a second consent button', async () => {
    githubCredential = credential(appGrant);
    githubProvider = { provider: 'github', status: 'registered', connection_status: 'verified', login: 'fixture-owner' };
    renderPage();
    await waitFor(() => expect(within(card()).getByText('Connected as @fixture-owner')).toBeInTheDocument());
    expect(within(card()).queryByRole('button')).not.toBeInTheDocument();
  });

  it('offers fresh repository consent for a paused App grant', async () => {
    githubCredential = credential(appGrant, false);
    githubProvider = { provider: 'github', status: 'registered', connection_status: 'consent_paused' };
    vi.mocked(apiClient.post).mockResolvedValue({ authorization_url: 'https://github.example.test/authorize', state: 'nonce', session_uri: 'opaque' });
    const location = { ...window.location, href: '' };
    vi.spyOn(window, 'location', 'get').mockReturnValue(location as Location);
    renderPage();
    await waitFor(() => expect(within(card()).getByText(/Paused\. Reconnect GitHub/)).toBeInTheDocument());
    const reconnect = within(card()).getByRole('button', { name: 'Reconnect GitHub for this box' });
    fireEvent.click(reconnect);
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith('/auth/connections/oauth/github/start', {
      reconnect: true, repository_access: true,
    }));
    expect(JSON.parse(window.sessionStorage.getItem('oauth_connection_pending')!)).toEqual({
      state: 'nonce', session_uri: 'opaque', service: 'github', return_to: '/settings/dev-box',
    });
    expect(location.href).toBe('https://github.example.test/authorize');
  });

  it('keeps legacy paused consent on its existing resume route', async () => {
    githubCredential = credential({ oauth_scopes: ['read:user'] }, false);
    githubProvider = { provider: 'github', status: 'registered', connection_status: 'consent_paused' };
    renderPage();
    await waitFor(() => expect(within(card()).getByText(/Paused\. Resume access/)).toBeInTheDocument());
    expect(within(card()).queryByRole('button')).not.toBeInTheDocument();
  });

  it('asks legacy-only owners to reconnect rather than declaring box access', async () => {
    githubCredential = credential({ oauth_scopes: ['read:user', 'repo', 'workflow'] });
    githubProvider = { provider: 'github', status: 'registered', connection_status: 'verified' };
    renderPage();
    await waitFor(() => expect(within(card()).getByText(/Reconnect to use GitHub on your box/)).toBeInTheDocument());
    expect(within(card()).getByRole('button', { name: 'Reconnect GitHub for this box' })).toBeInTheDocument();
  });

  it('offers one consent button for an App profile-only connection', async () => {
    githubCredential = credential({ github_app_grant: { schema_version: 1, repository_access: false } });
    githubProvider = { provider: 'github', status: 'registered', connection_status: 'verified' };
    renderPage();
    await waitFor(() => expect(within(card()).getByRole('button', { name: 'Use GitHub as you on this box' })).toBeInTheDocument());
    expect(within(card()).getAllByRole('button')).toHaveLength(1);
  });

  it('does not offer consent when provider status or credential loading fails', async () => {
    githubProvider = { provider: 'github', status: 'setup_required', connection_status: 'not_connected' };
    renderPage();
    await waitFor(() => expect(within(card()).getByText(/GitHub connection setup is unavailable/)).toBeInTheDocument());
    expect(within(card()).queryByRole('button')).not.toBeInTheDocument();
  });

  it('does not mistake a failed credential read for a missing grant', async () => {
    githubProvider = { provider: 'github', status: 'registered', connection_status: 'not_connected' };
    const read = vi.mocked(apiClient.get).getMockImplementation()!;
    vi.mocked(apiClient.get).mockImplementation(async endpoint => {
      if (endpoint === '/auth/credentials') throw new Error('credential read failed');
      return read(endpoint);
    });
    renderPage();
    await waitFor(() => expect(within(card()).getByText(/GitHub connection setup is unavailable/)).toBeInTheDocument());
    expect(within(card()).queryByRole('button')).not.toBeInTheDocument();
  });
});

it('shows workspace snapshots next to distinct Crew context backups on the owner page', async () => {
  api.items = [stopped];
  api.snapshots = [{ snapshot_id: 'snap-private', size_gb: 40, state: 'present',
    created_at: '2026-10-21T12:00:00Z', observed_at: '2026-10-22T13:00:00Z' }];
  renderPage();
  const panel = await screen.findByRole('region', { name: 'Workspace snapshots' });
  await waitFor(() => expect(within(panel).getByText('State: present')).toBeInTheDocument());
  expect(within(panel).getByText(/Created/).querySelector('time')).toHaveAttribute('dateTime', '2026-10-21T12:00:00Z');
  expect(panel).toHaveTextContent('40 GB');
  expect(screen.getByRole('region', { name: 'Context backups' })).toHaveTextContent('Crew home only');
  expect(panel.textContent).not.toContain('snap-private');
  expect(within(panel).queryByRole('button')).not.toBeInTheDocument();
});

describe('the one primary action', () => {
  it.each([
    ['no box', [], 'Provision and open my Dev Box'],
    ['stopped box', [stopped], 'Start and open my Dev Box'],
    ['running box', [running], 'Open my Dev Box'],
  ])('%s: the click opens the interstitial tab synchronously with a one-time intent and makes no call itself', async (_label, items, label) => {
    api.items = items;
    await act(async () => { renderPage(); });
    const button = await screen.findByRole('button', { name: label });
    const callsBefore = vi.mocked(apiClient.get).mock.calls.length;
    fireEvent.click(button);
    // Synchronous: the tab exists before any await in the handler, so no pop-up blocker is involved.
    expect(window.open).toHaveBeenCalledOnce();
    expect(openedHref()).toMatch(INTERSTITIAL);
    expect(vi.mocked(window.open).mock.calls[0][1]).toBe('_blank');
    expect(vi.mocked(window.open).mock.calls[0]).toHaveLength(2);
    expect(consumeDevBoxOpenIntent(new URL(openedHref(), 'https://adp.example.test').searchParams.get('intent'))).toBe(true);
    expect(apiClient.request).not.toHaveBeenCalled();
    expect(enrollCrewHost).not.toHaveBeenCalled();
    expect(resolveCrewDashboardDestination).not.toHaveBeenCalled();
    expect(vi.mocked(apiClient.get).mock.calls.length).toBe(callsBefore);
    expect(screen.getByTestId('connecting-in-tab')).toHaveTextContent('Connecting in a new tab');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Open in new tab' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
  });

  it('while the tab works this page watches the box and stops once Crew is ready', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      api.items = [running];
      api.target = notEnrolled;
      renderPage();
      const button = await screen.findByRole('button', { name: 'Open my Dev Box' });
      await waitFor(() => expect(screen.getByTestId('crew-target')).toHaveTextContent('crew_target_missing'));
      fireEvent.click(button);
      const reads = () => vi.mocked(apiClient.get).mock.calls.filter(([endpoint]) => endpoint === '/cloud-desktops').length;
      const before = reads();
      api.target = () => ready;
      // Tick 1: re-read → Crew is ready now. Tick 2: ready observed → the watch ends, no further read.
      await vi.advanceTimersByTimeAsync(5_100);
      await waitFor(() => expect(reads()).toBe(before + 1));
      await waitFor(() => expect(screen.getByTestId('crew-target')).toHaveTextContent('ready'));
      expect(screen.getByTestId('connecting-in-tab')).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(5_100);
      await waitFor(() => expect(screen.queryByTestId('connecting-in-tab')).not.toBeInTheDocument());
      await vi.advanceTimersByTimeAsync(10_500);
      expect(reads()).toBe(before + 1);
      expect(apiClient.request).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a second click while the tab is open focuses it instead of opening another', async () => {
    api.items = [running];
    const tab = openedTab();
    vi.mocked(window.open).mockReturnValue(tab);
    renderPage();
    const button = await screen.findByRole('button', { name: 'Open my Dev Box' });
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);
    expect(window.open).toHaveBeenCalledOnce();
    expect(tab.focus).toHaveBeenCalledTimes(2);
    expect(apiClient.request).not.toHaveBeenCalled();
  });

  it('running but not enrolled: the page says enrollment is automatic and never offers a separate enrollment click', async () => {
    api.items = [running];
    api.target = notEnrolled;
    renderPage();
    await screen.findByRole('button', { name: 'Open my Dev Box' });
    await waitFor(() => expect(screen.getByTestId('crew-target')).toHaveTextContent('crew_target_missing'));
    expect(screen.getByText('Crew is installed; opening the box enrolls this host automatically.', { selector: '#open-my-dev-box-reason' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Enroll/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open dashboard' })).not.toBeInTheDocument();
    expect(enrollCrewHost).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('running and enrolled: the page names the ready state next to the button', async () => {
    api.items = [running];
    renderPage();
    await screen.findByRole('button', { name: 'Open my Dev Box' });
    expect(screen.getByText('Crew is ready on this host.', { selector: '#open-my-dev-box-reason' })).toBeInTheDocument();
  });

  it('offers the interstitial as an "Open in new tab" link (same origin, no noopener) when the browser refuses even a click-time open', async () => {
    api.items = [running];
    vi.mocked(window.open).mockReturnValue(null);
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Open my Dev Box' }));
    const link = await screen.findByRole('link', { name: 'Open in new tab' });
    expect(link.getAttribute('href')).toMatch(INTERSTITIAL);
    expect(link.getAttribute('href')).toBe(openedHref());
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).not.toHaveAttribute('rel');
    expect(screen.getByText(/The browser blocked the new tab/)).toBeInTheDocument();
    expect(screen.queryByTestId('connecting-in-tab')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('policy denied', () => {
  it('renders the policy reason and nothing clickable', async () => {
    api.policy = { contract_version: 1, admission_allowed: false, reason: 'Dev Box policy is disabled or incomplete' };
    renderPage();
    expect(await screen.findByTestId('policy-denied')).toHaveTextContent('Dev Box policy is disabled or incomplete');
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(apiClient.request).not.toHaveBeenCalled();
  });
});

describe('secondary controls', () => {
  beforeEach(() => { api.items = [running]; });

  it('shows the SSH route next to the launch button, with alias and CLI steps, and never fetches or renders a credential', async () => {
    const view = renderPage();
    // Inline since 2026-10-26 (owner): no toggle; the instructions sit inside the "Open my Dev Box" section.
    const launch = await screen.findByRole('region', { name: 'Open my Dev Box' });
    expect(launch).toContainElement(screen.getByTestId('ssh-setup'));
    expect(screen.queryByRole('button', { name: 'Connect via SSH' })).not.toBeInTheDocument();
    expect(screen.getByTestId('ssh-alias')).toHaveTextContent('adp-devbox-assistant');
    expect(screen.getByTestId('ssh-setup')).toHaveTextContent('adp update adp devbox ssh-config assistant adp devbox ssh assistant');
    expect(screen.getByText(/AWS CLI v2 and the Session Manager plugin/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy SSH setup commands' })).toBeInTheDocument();
    const calls = [...vi.mocked(apiClient.get).mock.calls, ...vi.mocked(apiClient.request).mock.calls].map(([endpoint]) => endpoint);
    expect(calls.some(endpoint => endpoint.includes('ssh-access'))).toBe(false);
    expect(view.container.textContent).not.toMatch(/AKIA|ASIA[A-Z0-9]{12}|SessionToken|SecretAccessKey|aws_secret_access_key|AWS_SESSION_TOKEN|--profile/);
  });

  it('stops with an idempotency key; a stopped box then offers "Start and open" and Replace, each with fresh keys', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(apiClient.request).toHaveBeenCalledWith('/cloud-desktops/owned/stop', expect.objectContaining({
      method: 'POST', body: undefined, headers: { 'Idempotency-Key': expect.any(String) },
    })));
    await screen.findByRole('button', { name: 'Start and open my Dev Box' });
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
    await waitFor(() => expect(apiClient.request).toHaveBeenCalledWith('/cloud-desktops/owned/replace', expect.objectContaining({ method: 'POST' })));
    const used = keys();
    expect(used).toHaveLength(2);
    expect(new Set(used).size).toBe(2);
    for (const key of used) expect(key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('sends a selected owned snapshot only on Replace and leaves ordinary Replace unchanged', async () => {
    api.items = [stopped];
    api.snapshots = [
      { snapshot_id: 'snap-selected', size_gb: 40, state: 'present', created_at: '2026-10-21T12:00:00Z', observed_at: null },
      { snapshot_id: 'snap-pending', size_gb: 40, state: 'pending', created_at: null, observed_at: null },
    ];
    renderPage();
    const choice = await screen.findByRole('combobox', { name: 'Restore workspace during Replace' });
    expect(within(choice).getAllByRole('option')).toHaveLength(2);
    expect(screen.getByRole('region', { name: 'Workspace snapshots' }).textContent).not.toContain('snap-selected');
    fireEvent.change(choice, { target: { value: 'snap-selected' } });
    fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
    await waitFor(() => expect(apiClient.request).toHaveBeenCalledWith('/cloud-desktops/owned/replace', expect.objectContaining({
      method: 'POST', body: { snapshot_id: 'snap-selected' },
    })));
  });

  it('does not offer restore while snapshots are pending or unavailable', async () => {
    api.items = [stopped];
    api.snapshots = [{ snapshot_id: 'snap-pending', size_gb: 40, state: 'pending', created_at: null, observed_at: null }];
    renderPage();
    await screen.findByText('State: pending');
    expect(screen.queryByRole('combobox', { name: 'Restore workspace during Replace' })).not.toBeInTheDocument();
  });

  it('requires an explicit retain or delete choice before releasing', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Release' }));
    const dialog = screen.getByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Release' });
    expect(confirm).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('radio', { name: /Delete the workspace/ }));
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(apiClient.request).toHaveBeenCalledWith('/cloud-desktops/owned/release', expect.objectContaining({
      method: 'POST', body: { workspace_retention: 'delete' }, headers: { 'Idempotency-Key': expect.any(String) },
    })));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await screen.findByRole('button', { name: 'Provision and open my Dev Box' });
  });

  it('names a fenced lease as a state, not an error, and shows the bootstrap receipt when the read carries it', async () => {
    api.target = fenced;
    api.items = [{ ...running, crew_bootstrap_receipt: { status: 'ok', profile_revision: 'crew-20261017-abc123def456' } }];
    renderPage();
    await screen.findByRole('button', { name: 'Open my Dev Box' });
    await waitFor(() => expect(screen.getByTestId('crew-target')).toHaveTextContent('lease_fenced'));
    expect(screen.getByText(/Host enrolled; the controller is binding this host and verifying Crew on it/, { selector: '#open-my-dev-box-reason' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('bootstrap-receipt')).toHaveTextContent('ok (profile crew-20261017-abc123def456)');
  });

  it('shows the read-only release values only to admins', async () => {
    const member = renderPage();
    await screen.findByRole('button', { name: 'Open my Dev Box' });
    expect(screen.queryByRole('region', { name: 'Release values' })).not.toBeInTheDocument();
    member.unmount();
    hasRole.mockReturnValue(true);
    renderPage();
    const panel = await screen.findByRole('region', { name: 'Release values' });
    expect(panel).toHaveTextContent(/Crew bootstrap mode\s*user-data/);
    expect(panel).toHaveTextContent(/Template\s*approved \(revision 3\)/);
  });
});

describe('context backups on the Dev Box page', () => {
  it('shows five verified backups below lifecycle controls and badges the selected restore point', async () => {
    api.items = [stopped];
    backupRows = [1, 2, 3, 4, 5, 6].map(sequence => backupRecord(sequence));
    backupRows.push(backupRecord(7, 'failed'));
    selectedBackupId = 'private-4';
    renderPage();
    const panel = screen.getByRole('region', { name: 'Context backups' });
    await waitFor(() => expect(within(panel).getAllByRole('listitem')).toHaveLength(5));
    const rows = within(panel).getAllByRole('listitem');
    expect(rows.map(row => within(row).getByText(/MB/).textContent)).toEqual(['6.0 MB', '5.0 MB', '4.0 MB', '3.0 MB', '2.0 MB']);
    expect(rows[2]).toHaveTextContent('Restore point');
    expect(rows[0]).not.toHaveTextContent('Restore point');
    expect(within(panel).getByRole('alert')).toHaveTextContent('The newest context backup failed at');
    expect(panel).not.toHaveTextContent(/private-\d+/);
    expect(within(panel).queryByRole('button')).not.toBeInTheDocument();
    expect(within(panel).queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Dev Box lifecycle' }).compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Replace' })).toBeEnabled();
  });
});

describe('action matrix — buttons derive from status + observed_state', () => {
  const names = () => screen.getAllByRole('button').map(button => button.textContent?.trim()).sort();
  const enabled = () => screen.getAllByRole('button').filter(button => !button.hasAttribute('disabled')).map(button => button.textContent?.trim()).sort();

  it('leaves Replace usable when backup status is unavailable', async () => {
    api.items = [stopped];
    backupUnavailable = true;
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Backup status unavailable');
    const replace = screen.getByRole('button', { name: 'Replace' });
    expect(replace).toBeEnabled();
    fireEvent.click(replace);
    await waitFor(() => expect(apiClient.request).toHaveBeenCalledWith('/cloud-desktops/owned/replace', expect.objectContaining({ method: 'POST' })));
  });

  it.each([
    // 'Copy commands' is the inline SSH block's copy button (shown whenever a box exists and the launch section is).
    ['running', running, ['Copy commands', 'Open my Dev Box', 'Release', 'Replace', 'Stop'], ['Copy commands', 'Open my Dev Box', 'Release', 'Stop']],
    ['stopped', stopped, ['Copy commands', 'Release', 'Replace', 'Start and open my Dev Box'], ['Copy commands', 'Release', 'Replace', 'Start and open my Dev Box']],
    ['requested (launching)', { ...running, status: 'requested', observed_state: 'unknown', desired_state: 'running' }, ['Copy commands', 'Open my Dev Box', 'Refresh'], ['Copy commands', 'Open my Dev Box', 'Refresh']],
    ['replacing (requested, generation advanced)', { ...running, status: 'requested', observed_state: 'unknown', desired_state: 'running', machine_generation: 12 }, ['Copy commands', 'Open my Dev Box', 'Refresh'], ['Copy commands', 'Open my Dev Box', 'Refresh']],
    ['start admitted but not yet observed', { ...running, status: 'stopped', observed_state: 'stopped', desired_state: 'running' }, ['Copy commands', 'Open my Dev Box', 'Refresh'], ['Copy commands', 'Open my Dev Box', 'Refresh']],
    ['stop admitted but not yet observed', { ...running, status: 'running', observed_state: 'running', desired_state: 'stopped' }, ['Copy commands', 'Open my Dev Box', 'Refresh'], ['Copy commands', 'Open my Dev Box', 'Refresh']],
    ['failed', { ...running, status: 'failed', observed_state: 'unknown', desired_state: 'running' }, ['Release'], ['Release']],
  ] as const)('%s', async (_label, box, expectedButtons, expectedEnabled) => {
    api.items = [box];
    renderPage();
    await screen.findByRole('region', { name: 'Dev Box actions' });
    await waitFor(() => expect(names()).toEqual([...expectedButtons].sort()));
    await waitFor(() => expect(enabled()).toEqual([...expectedEnabled].sort()));
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open dashboard' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Enroll/ })).not.toBeInTheDocument();
    if (box.status === 'requested') expect(screen.getByTestId('transitioning')).toHaveTextContent('requested, observed unknown, wanted running');
  });

  it('released: the box is gone, so the only call to action is "Provision and open"', async () => {
    api.items = [{ ...running, status: 'released', observed_state: 'unknown', desired_state: 'released' }];
    renderPage();
    const provision = await screen.findByRole('button', { name: 'Provision and open my Dev Box' });
    expect(screen.getAllByRole('button')).toEqual([provision]);
  });

  it('running: no POST other than ensure, stop, replace or release can be triggered from the page', async () => {
    api.items = [running];
    // Keep the fixture running whatever is posted, so every running-state control stays reachable.
    vi.mocked(apiClient.request).mockImplementation(async endpoint => ({ contract_version: 1, desktop_id: 'owned', operation_id: 'op', status: 'pending', progress: endpoint }));
    renderPage();
    await screen.findByRole('region', { name: 'Dev Box actions' });
    await waitFor(() => expect(primary()).toBeEnabled());
    for (const label of ['Replace', 'Stop']) {
      const button = screen.getByRole('button', { name: label });
      if (!button.hasAttribute('disabled')) fireEvent.click(button);
      if (label === 'Stop') { await waitFor(() => expect(apiClient.request).toHaveBeenCalled()); await screen.findByRole('button', { name: 'Stop' }); }
    }
    fireEvent.click(screen.getByRole('button', { name: 'Release' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('radio', { name: /Retain the workspace/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Release' }));
    await waitFor(() => expect(apiClient.request).toHaveBeenCalledTimes(2));
    expect(posted()).toEqual(['stop', 'release']);
    expect(posted()).not.toContain('start');
  });
});


describe('manual enrolment lives under Advanced on the Dev Box page (#8864)', () => {
  it('shows the fallback only when the EC2 runtime is permitted', async () => {
    crewRuntime.hostAllowed = true;
    const { unmount } = renderPage();
    expect(await screen.findByText('Advanced: enroll my Crew box manually')).toBeInTheDocument();
    expect(screen.getByTestId('crew-host-enrollment')).toBeInTheDocument();
    unmount();
    crewRuntime.hostAllowed = false;
    renderPage();
    expect(screen.queryByText('Advanced: enroll my Crew box manually')).not.toBeInTheDocument();
    crewRuntime.hostAllowed = true;
  });
});
