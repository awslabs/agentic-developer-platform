import { act, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { FeatureGate } from '@/components/FeatureGate';
import { ALL_FEATURES_ENABLED, fetchFeatures, type FeatureFlags } from '@/services/features';

vi.mock('@/services/features', async (original) => ({
  ...await original<typeof import('@/services/features')>(),
  fetchFeatures: vi.fn(),
}));

function renderFlow() {
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/flows/test-flow']}>
        <Routes>
          <Route path="/" element={<div data-testid="home">Home</div>} />
          <Route path="/flows/:id" element={
            <FeatureGate feature="orchestration_engine"><div data-testid="flow">Flow</div></FeatureGate>
          } />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe('opening a flow before feature flags arrive', () => {
  it('preserves the deep link and waits without revealing the gated screen', async () => {
    let resolve!: (flags: FeatureFlags) => void;
    vi.mocked(fetchFeatures).mockReturnValue(new Promise((done) => { resolve = done; }));
    renderFlow();
    expect(screen.getByText('Loading feature…')).toBeInTheDocument();
    expect(screen.queryByTestId('home')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flow')).not.toBeInTheDocument();
    await act(async () => resolve({ ...ALL_FEATURES_ENABLED, orchestration_engine: true }));
    expect(await screen.findByTestId('flow')).toBeInTheDocument();
    expect(screen.queryByTestId('home')).not.toBeInTheDocument();
  });

  it('redirects only after the server confirms the feature is disabled', async () => {
    let resolve!: (flags: FeatureFlags) => void;
    vi.mocked(fetchFeatures).mockReturnValue(new Promise((done) => { resolve = done; }));
    renderFlow();
    expect(screen.queryByTestId('home')).not.toBeInTheDocument();
    await act(async () => resolve({ ...ALL_FEATURES_ENABLED, orchestration_engine: false }));
    expect(await screen.findByTestId('home')).toBeInTheDocument();
    expect(screen.queryByTestId('flow')).not.toBeInTheDocument();
  });
});
