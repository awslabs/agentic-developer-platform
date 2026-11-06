import { beforeEach, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { OnboardingGuard } from '@/components/OnboardingGuard';
import { FeatureGate } from '@/components/FeatureGate';
import { ALL_FEATURES_ENABLED } from '@/services/features';

const fixture = vi.hoisted(() => ({ status: null as string | null, isLoading: true,
  fetch: vi.fn(), list: vi.fn(), graph: vi.fn(), page: vi.fn() }));
vi.mock('@/hooks/useAccessStatus', () => ({ useAccessStatus: () => fixture }));
vi.mock('@/services/features', async (original) => ({ ...await original<typeof import('@/services/features')>(),
  fetchFeatures: () => fixture.fetch() }));
vi.mock('@/routing/flowPages', () => ({ loadFlowsList: () => fixture.list(), loadFlowGraph: () => fixture.graph() }));
function Page() { fixture.page(); return <h1>Flow content</h1>; }
function mount(path = '/flows/flow-1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const tree = () => <QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>
    <Routes><Route element={<OnboardingGuard />}>
      <Route path="/flows" element={<FeatureGate feature="orchestration_engine"><Page /></FeatureGate>} />
      <Route path="/flows/:flowId" element={<FeatureGate feature="orchestration_engine"><Page /></FeatureGate>} />
      <Route path="/logs" element={<h1>Logs</h1>} />
    </Route><Route path="*" element={<h1>Other route</h1>} /></Routes>
  </MemoryRouter></QueryClientProvider>;
  const view = render(tree()); return { refresh: () => view.rerender(tree()) };
}
beforeEach(() => {
  vi.clearAllMocks(); fixture.status = null; fixture.isLoading = true;
  fixture.fetch.mockResolvedValue({ ...ALL_FEATURES_ENABLED, orchestration_engine: true });
  fixture.list.mockResolvedValue({}); fixture.graph.mockResolvedValue({});
});
it.each([['/flows', 'list'], ['/flows/flow-1', 'graph']] as const)(
  'prepares %s while onboarding is pending but withholds page data', async (path, kind) => {
    const view = mount(path);
    await waitFor(() => expect(fixture[kind]).toHaveBeenCalledTimes(1));
    expect(fixture[kind === 'list' ? 'graph' : 'list']).not.toHaveBeenCalled();
    expect(fixture.fetch).toHaveBeenCalledTimes(1);
    expect(fixture.page).not.toHaveBeenCalled();
    fixture.status = 'registered'; fixture.isLoading = false; view.refresh();
    expect(await screen.findByRole('heading', { name: 'Flow content' })).toBeVisible();
    expect(fixture.fetch).toHaveBeenCalledTimes(1);
  });
it('withholds code and content until the feature is explicitly enabled', async () => {
  let resolve!: (value: unknown) => void;
  fixture.fetch.mockImplementation(() => new Promise(r => { resolve = r; }));
  fixture.status = 'registered'; fixture.isLoading = false;
  mount(); await waitFor(() => expect(fixture.fetch).toHaveBeenCalledTimes(1));
  expect(fixture.graph).not.toHaveBeenCalled(); expect(fixture.page).not.toHaveBeenCalled();
  await act(async () => resolve({ ...ALL_FEATURES_ENABLED, orchestration_engine: false }));
  expect(await screen.findByRole('heading', { name: 'Other route' })).toBeVisible();
  expect(fixture.graph).not.toHaveBeenCalled(); expect(fixture.page).not.toHaveBeenCalled();
});
it('keeps denied onboarding from mounting or preloading the page', async () => {
  fixture.status = 'denied'; fixture.isLoading = false;
  mount(); expect(await screen.findByRole('heading', { name: 'Other route' })).toBeVisible();
  expect(fixture.graph).not.toHaveBeenCalled(); expect(fixture.page).not.toHaveBeenCalled();
});
it('does not prepare a flow on another route', async () => {
  fixture.status = 'registered'; fixture.isLoading = false;
  mount('/logs'); expect(await screen.findByRole('heading', { name: 'Logs' })).toBeVisible();
  expect(fixture.fetch).not.toHaveBeenCalled(); expect(fixture.graph).not.toHaveBeenCalled();
});
