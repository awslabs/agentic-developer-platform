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

// Mock useFeatures
const mockFeatures: FeatureFlags = {
  chat: true,
  knowledge: true,
  indexing: true,
  connections: true,
  credentials: true,
  system_dashboard: true,
};

vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockFeatures,
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
  });

  it('renders children when feature is enabled', () => {
    renderWithRouter('/chat');
    expect(screen.getByTestId('chat-page')).toBeInTheDocument();
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument();
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

  it('renders children for enabled feature when other features are disabled', () => {
    mockFeatures.chat = false;
    mockFeatures.knowledge = false;
    // connections still enabled
    renderWithRouter('/settings/connections');
    expect(screen.getByTestId('connections-page')).toBeInTheDocument();
  });
});
