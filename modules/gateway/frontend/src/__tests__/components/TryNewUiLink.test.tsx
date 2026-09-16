/**
 * Tests for TryNewUiLink — Issue #5079.
 *
 * The entry point into the /next preview is the one place the current UI mentions
 * the new experience at all, so its gating is the "current UI is unchanged when
 * the flag is off" acceptance criterion in miniature.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { TryNewUiLink } from '@/components/TryNewUiLink';
import { ALL_FEATURES_ENABLED, type FeatureFlags } from '@/services/features';

// The entry link reads the REVALIDATING subscription, not `useFeatures`, so that
// an operator's disable reaches a tab that is already open. Mocking the hook it
// actually calls is what keeps this test honest about which one that is.
const mockUseNewUiEnabled = vi.fn();
vi.mock('@/hooks/useFeatures', () => ({
  useNewUiEnabled: () => mockUseNewUiEnabled(),
}));

function renderLink(features: Partial<FeatureFlags>, isPending = false) {
  const flags = { ...ALL_FEATURES_ENABLED, ...features };
  mockUseNewUiEnabled.mockReturnValue({ enabled: flags.new_ui, isPending });
  return render(
    <MemoryRouter>
      <TryNewUiLink />
    </MemoryRouter>,
  );
}

describe('TryNewUiLink', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing when new_ui is off', () => {
    const { container } = renderLink({ new_ui: false });
    expect(screen.queryByTestId('try-new-ui')).not.toBeInTheDocument();
    // Nothing at all, not an empty wrapper: the current header must be
    // byte-identical in an environment that has not opted in.
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a link to /next when new_ui is on', () => {
    renderLink({ new_ui: true });
    const link = screen.getByTestId('try-new-ui');
    expect(link).toHaveAttribute('href', '/next');
    expect(link).toHaveTextContent('Try the new UI');
  });

  it('is hidden by the fail-closed default, which is what /features pending or failed resolves to', () => {
    // useNewUiEnabled resolves `data ?? ALL_FEATURES_ENABLED`, so this object is what
    // renders while the fetch is in flight AND whenever it errors. If new_ui were
    // fail-open, a backend outage would advertise a preview whose routes the same
    // outage has gated off.
    expect(ALL_FEATURES_ENABLED.new_ui).toBe(false);
    renderLink(ALL_FEATURES_ENABLED);
    expect(screen.queryByTestId('try-new-ui')).not.toBeInTheDocument();
  });

  it('shows no invitation while the flags are still pending', () => {
    // The entry link has no URL to preserve, so unlike the /next gate it simply
    // stays hidden until the server has affirmatively enabled the preview.
    renderLink({ new_ui: false }, true);
    expect(screen.queryByTestId('try-new-ui')).not.toBeInTheDocument();
  });

  it('disappears when a later flag read turns the preview off, without a reload', () => {
    // The in-session rollback, at this component's level: re-rendering with a
    // fresh `false` from the revalidating query must remove the entry point. This
    // is what a role-only reliance on the session-long cache could not do.
    const { rerender } = renderLink({ new_ui: true });
    expect(screen.getByTestId('try-new-ui')).toBeInTheDocument();

    mockUseNewUiEnabled.mockReturnValue({ enabled: false, isPending: false });
    rerender(
      <MemoryRouter>
        <TryNewUiLink />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId('try-new-ui')).not.toBeInTheDocument();
  });
});
