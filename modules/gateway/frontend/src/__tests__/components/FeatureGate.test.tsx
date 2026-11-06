/**
 * Tests for FeatureGate component — Issue #3566.
 *
 * Verifies that FeatureGate redirects to "/" when a feature is disabled,
 * and renders children when the feature is enabled.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { FeatureGate } from '@/components/FeatureGate';
import type { FeatureFlags } from '@/services/features';

// Mock useFeatures.
//
// Annotated `FeatureFlags` but only ever declared six of its keys, so the four
// flags added since (#3566 logs, #3773 gitlab, #4209 orchestration_engine, #4402
// budget_spend) were `undefined` here rather than `false`. That happens to gate
// the same way, but it means this fixture silently stopped describing the real
// payload. Completed while adding agent_control (#3960) so the annotation is true
// again — tsconfig.json excludes `__tests__`, so `tsc` never reported the drift.
const mockFeatures: FeatureFlags = {
  product_profile: true,
  chat: true,
  knowledge: true,
  indexing: true,
  connections: true,
  credentials: true,
  system_dashboard: true,
  logs: true,
  gitlab: false,
  orchestration_engine: false,
  budget_spend: false,
  agent_control: false,
  new_ui: false,
  superplane: false,
  agent_models: false,
};

/** The flag read's own outcome, not just its payload — #8246. A completed read
 *  with no payload (the /features call was refused or failed) is a different
 *  state from a completed read that answered `false`, and the gate owes the
 *  actor a different thing in each. */
const queryState: { data: FeatureFlags | undefined; isPending: boolean; isError: boolean } = {
  data: mockFeatures,
  isPending: false,
  isError: false,
};

vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockFeatures,
  useFeaturesQuery: () => queryState,
}));

function renderWithRouter(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/" element={<div data-testid="home-page">Home</div>} />
        <Route
          path="/chat"
          element={
            <FeatureGate feature="chat">
              <div data-testid="chat-page">Chat Page</div>
            </FeatureGate>
          }
        />
        <Route
          path="/knowledge"
          element={
            <FeatureGate feature="knowledge">
              <div data-testid="knowledge-page">Knowledge Page</div>
            </FeatureGate>
          }
        />
        <Route
          path="/settings/connections"
          element={
            <FeatureGate feature="connections">
              <div data-testid="connections-page">Connections Page</div>
            </FeatureGate>
          }
        />
        <Route path="/superplane" element={
          <FeatureGate feature="superplane"><div data-testid="superplane-page">Superplane</div></FeatureGate>
        } />
        <Route path="/products/:productId/projects/:projectId" element={
          <FeatureGate feature="product_profile"><div data-testid="product-page">Product workspace</div></FeatureGate>
        } />
      </Routes>
    </MemoryRouter>
  );
}

describe('FeatureGate', () => {
  beforeEach(() => {
    // Reset to all-enabled
    mockFeatures.chat = true;
    mockFeatures.knowledge = true;
    mockFeatures.indexing = true;
    mockFeatures.connections = true;
    mockFeatures.credentials = true;
    mockFeatures.system_dashboard = true;
    mockFeatures.product_profile = true;
    queryState.data = mockFeatures;
    queryState.isPending = false;
    queryState.isError = false;
  });

  it('renders children when feature is enabled', () => {
    renderWithRouter('/chat');
    expect(screen.getByTestId('chat-page')).toBeInTheDocument();
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument();
  });

  it('preserves ordinary navigation with Superplane off and reveals it only when enabled', () => {
    mockFeatures.superplane = false;
    renderWithRouter('/superplane');
    expect(screen.getByTestId('home-page')).toBeInTheDocument();
    expect(screen.queryByTestId('superplane-page')).not.toBeInTheDocument();

    mockFeatures.superplane = true;
    renderWithRouter('/superplane');
    expect(screen.getByTestId('superplane-page')).toBeInTheDocument();
    mockFeatures.superplane = false;
  });

  it('redirects to "/" when feature is disabled', () => {
    mockFeatures.chat = false;
    renderWithRouter('/chat');
    expect(screen.getByTestId('home-page')).toBeInTheDocument();
    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument();
  });

  it('gates knowledge route correctly', () => {
    mockFeatures.knowledge = false;
    renderWithRouter('/knowledge');
    expect(screen.getByTestId('home-page')).toBeInTheDocument();
    expect(screen.queryByTestId('knowledge-page')).not.toBeInTheDocument();
  });

  it('gates connections route correctly', () => {
    mockFeatures.connections = false;
    renderWithRouter('/settings/connections');
    expect(screen.getByTestId('home-page')).toBeInTheDocument();
    expect(screen.queryByTestId('connections-page')).not.toBeInTheDocument();
  });

  // #8246: a revoked tenant membership makes GET /features fail, which leaves
  // product_profile at its fail-closed default. The screen must stay withheld,
  // but the actor has to be told that here — redirecting to "/" handed them a
  // dashboard whose organization selector blamed a loading failure and offered
  // a reload that a revoked membership can never satisfy.
  it('reports a refused Product admission read in place, without the workspace', () => {
    queryState.data = undefined;
    queryState.isError = true;
    renderWithRouter('/products/prod-1/projects/proj-1');
    expect(screen.getByRole('alert')).toHaveTextContent('Product unavailable or you do not have access');
    expect(screen.queryByTestId('product-page')).not.toBeInTheDocument();
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument();
  });

  it('still redirects when the server answers that product_profile is off', () => {
    mockFeatures.product_profile = false;
    renderWithRouter('/products/prod-1/projects/proj-1');
    expect(screen.getByTestId('home-page')).toBeInTheDocument();
    expect(screen.queryByTestId('product-page')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('leaves other fail-closed features redirecting on a failed flag read', () => {
    queryState.data = undefined;
    queryState.isError = true;
    renderWithRouter('/superplane');
    expect(screen.getByTestId('home-page')).toBeInTheDocument();
    expect(screen.queryByTestId('superplane-page')).not.toBeInTheDocument();
  });

  it('renders children for enabled feature when other features are disabled', () => {
    mockFeatures.chat = false;
    mockFeatures.knowledge = false;
    // connections still enabled
    renderWithRouter('/settings/connections');
    expect(screen.getByTestId('connections-page')).toBeInTheDocument();
  });
});
