/**
 * Settings → Dev Box → "Connecting to your Dev Box" interstitial (#8223 UX follow-up).
 *
 * The tab the Dev Box page opens on the click. With the click's intent it runs the one-click
 * flow (ensure/start → enroll → wait → ticket) and replaces its own location with the
 * validated handoff URL; without it, it only connects to a box that is already ready.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import DevBoxOpenPage from '@/pages/settings/DevBoxOpen';
import { apiClient } from '@/services/api';
import { resolveCrewDashboardDestination } from '@/services/crewDashboard';
import { enrollCrewHost } from '@/services/crewHostEnrollment';
import { beginDevBoxOpen } from '@/services/devBoxOpen';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), request: vi.fn() } }));
vi.mock('@/services/crewDashboard', () => ({ resolveCrewDashboardDestination: vi.fn() }));
vi.mock('@/services/crewHostEnrollment', () => ({ enrollCrewHost: vi.fn() }));
const features = vi.fn(() => ({ crew_dashboard: true, crew_dashboard_origin: 'https://crew.example.test' as string | undefined }));
vi.mock('@/hooks/useFeatures', () => ({ useFeatures: () => features() }));

const DESTINATION = 'https://crew.example.test/?token=test';
const cost = { estimated_cost_usd: '1.25', reconciled_cost_usd: null, total_cost_usd: null, coverage: 'partial_unknown', retained_resources: [] };
const running = { desktop_id: 'owned', name: 'assistant', template: 'approved', profile: 'crew', status: 'running', observed_state: 'running',
  desired_state: 'running', machine_generation: 2, crew_bootstrap: 'user-data', infrastructure_cost: cost };
const stopped = { ...running, status: 'stopped', observed_state: 'stopped', desired_state: 'stopped' };
const ready = { health: 'ready', lease: { target_kind: 'ec2-instance', target_ref: 'owned', generation: 2 } };
const notEnrolled = () => { throw { status: 404, detail: { error: 'crew_target_missing' } }; };

type Api = { items: unknown[]; target: () => unknown };
const api: Api = { items: [], target: () => ready };
const replace = vi.fn();

function renderAt(path: string) {
  return render(<MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/settings/dev-box/open" element={<DevBoxOpenPage />} />
      <Route path="/settings/dev-box" element={<h1>Dev Box page</h1>} />
    </Routes>
  </MemoryRouter>);
}
const withIntent = () => renderAt(beginDevBoxOpen());
const posted = () => vi.mocked(apiClient.request).mock.calls.map(([endpoint]) => endpoint.split('/').pop());

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  replace.mockReset();
  Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, replace } });
  features.mockReturnValue({ crew_dashboard: true, crew_dashboard_origin: 'https://crew.example.test' });
  api.items = [];
  api.target = () => ready;
  vi.mocked(enrollCrewHost).mockReset().mockResolvedValue(undefined);
  vi.mocked(apiClient.get).mockReset().mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items: api.items };
    if (endpoint === '/cloud-desktops/policy') return { contract_version: 1, admission_allowed: true, reason: null };
    if (endpoint === '/cloud-desktops/crew-template') return { contract_version: 1, template: 'approved' };
    if (endpoint === '/cloud-desktops/owned') return api.items[0] ?? running;
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/cloud-desktops/owned/operations/op') return { desktop_id: 'owned', operation_id: 'op', status: 'succeeded', progress: 'running' };
    if (endpoint === '/crew/target') return api.target();
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockReset().mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops/ensure') api.items = [running];
    return { contract_version: 1, desktop_id: 'owned', operation_id: 'op', status: 'pending', progress: 'requested' };
  });
  vi.mocked(resolveCrewDashboardDestination).mockReset().mockResolvedValue(DESTINATION);
});

describe('opened from the click (intent present)', () => {
  it('starts a stopped box, enrolls the host, waits for Crew, then replaces its location with the validated handoff URL', async () => {
    api.items = [stopped];
    api.target = () => (vi.mocked(enrollCrewHost).mock.calls.length ? ready : notEnrolled());
    withIntent();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Connecting to your Dev Box');
    await waitFor(() => expect(replace).toHaveBeenCalledExactlyOnceWith(DESTINATION), { timeout: 5000 });
    expect(posted()).toEqual(['ensure']);
    expect(enrollCrewHost).toHaveBeenCalledExactlyOnceWith('owned', expect.any(AbortSignal));
    expect(resolveCrewDashboardDestination).toHaveBeenCalledWith('https://crew.example.test/crew/handoff');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Opening the Crew dashboard…');
    expect(screen.getByTestId('devbox-open-status')).toHaveTextContent('Handing this tab to Kiro Crew.');
    const steps = within(screen.getByRole('list', { name: 'Provisioning timeline' })).getAllByRole('listitem').map(item => item.textContent);
    expect(steps).toHaveLength(7);
    expect(steps[6]).toMatch(/^✓ Crew ready — /);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('runs the flow once even though the intent URL stays in the address bar', async () => {
    api.items = [running];
    withIntent();
    await waitFor(() => expect(replace).toHaveBeenCalledOnce());
    expect(posted()).toEqual(['ensure']);
    expect(vi.mocked(resolveCrewDashboardDestination)).toHaveBeenCalledOnce();
  });

  it('names an enrollment refusal, offers Retry (same mode) and a way back, and never navigates', async () => {
    api.items = [running];
    api.target = notEnrolled;
    vi.mocked(enrollCrewHost).mockRejectedValueOnce({ status: 409, detail: 'Host enrollment requires a new public key' });
    withIntent();
    const failure = await screen.findByTestId('devbox-open-failure');
    expect(failure).toHaveTextContent('Host enrollment refused: Host enrollment requires a new public key');
    expect(failure.textContent).not.toMatch(/[{}"]/);
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'Back to Dev Box' })).toHaveAttribute('href', '/settings/dev-box');
    // Retry keeps the click's mode: the refusal is retried with a fresh enrollment, still no loop.
    vi.mocked(enrollCrewHost).mockResolvedValueOnce(undefined);
    api.target = () => (vi.mocked(enrollCrewHost).mock.calls.length > 1 ? ready : notEnrolled());
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(replace).toHaveBeenCalledExactlyOnceWith(DESTINATION), { timeout: 5000 });
    expect(enrollCrewHost).toHaveBeenCalledTimes(2);
  });

  it('shows the server refusal when EC2 Crew is not permitted, without a raw payload', async () => {
    vi.mocked(apiClient.get).mockImplementation(async endpoint => {
      if (endpoint === '/cloud-desktops') return { contract_version: 1, items: [] };
      if (endpoint === '/cloud-desktops/crew-template') throw { status: 403, detail: 'EC2 Crew is not permitted' };
      throw new Error(`Unexpected endpoint: ${endpoint}`);
    });
    withIntent();
    expect(await screen.findByTestId('devbox-open-failure')).toHaveTextContent('EC2 Crew is not permitted');
    expect(apiClient.request).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });
});

describe('visited without the click intent', () => {
  it('connects when the box is already running with Crew ready, without provisioning, starting or enrolling', async () => {
    api.items = [running];
    renderAt('/settings/dev-box/open');
    await waitFor(() => expect(replace).toHaveBeenCalledExactlyOnceWith(DESTINATION));
    expect(apiClient.request).not.toHaveBeenCalled();
    expect(enrollCrewHost).not.toHaveBeenCalled();
    expect(apiClient.get).not.toHaveBeenCalledWith('/cloud-desktops/crew-template', expect.anything());
  });

  it('a stale or foreign nonce is connect-only too', async () => {
    api.items = [stopped];
    renderAt('/settings/dev-box/open?intent=00000000-0000-4000-8000-000000000000');
    expect(await screen.findByTestId('devbox-open-failure')).toHaveTextContent('Your Dev Box is not running with Crew ready. Open it from the Dev Box page.');
    expect(apiClient.request).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it.each([
    ['no box', []],
    ['stopped box', [stopped]],
  ])('%s: names the state, points back to the Dev Box page and never starts anything', async (_label, items) => {
    api.items = items;
    renderAt('/settings/dev-box/open');
    expect(await screen.findByTestId('devbox-open-failure')).toHaveTextContent('Your Dev Box is not running with Crew ready');
    expect(screen.getByRole('link', { name: 'Back to Dev Box' })).toHaveAttribute('href', '/settings/dev-box');
    expect(screen.getByTestId('connect-only-note')).toHaveTextContent('This tab only connects to a running Dev Box');
    expect(apiClient.request).not.toHaveBeenCalled();
    expect(enrollCrewHost).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it('running but not enrolled is not ready: it does not enroll on its own', async () => {
    api.items = [running];
    api.target = notEnrolled;
    renderAt('/settings/dev-box/open');
    expect(await screen.findByTestId('devbox-open-failure')).toHaveTextContent('Your Dev Box is not running with Crew ready');
    expect(enrollCrewHost).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });
});

it('without a configured Crew origin it explains and offers the way back, calling nothing', () => {
  features.mockReturnValue({ crew_dashboard: true, crew_dashboard_origin: undefined });
  withIntent();
  expect(screen.getByTestId('devbox-open-failure')).toHaveTextContent('Crew dashboard origin is not configured.');
  expect(screen.getByRole('link', { name: 'Back to Dev Box' })).toBeInTheDocument();
  expect(apiClient.get).not.toHaveBeenCalled();
  expect(replace).not.toHaveBeenCalled();
});
