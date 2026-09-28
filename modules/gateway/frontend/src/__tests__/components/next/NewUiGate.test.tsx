/**
 * Tests for NewUiGate and the revalidating flags subscription — Issue #5079.
 *
 * These cover the acceptance criterion the first revision of this PR left
 * incomplete: "disabling /next leaves a working way into the current UI" has to
 * hold for a session that is ALREADY inside the preview, not only for one that
 * reloads afterwards.
 *
 * Unlike the other tests in this story, these mock the network (`fetchFeatures`)
 * rather than the hook, and drive a real `QueryClient`. That is deliberate — the
 * defect being fixed was a react-query caching policy, so a test that stubbed the
 * hook would assert nothing about it. Here a second `/features` response with
 * `new_ui: false` genuinely has to propagate through the cache and unmount the
 * subtree.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { NewUiGate } from '@/components/next/NewUiGate';
import { TryNewUiLink } from '@/components/TryNewUiLink';
import { FEATURES_REVALIDATE_MS, useFeatures } from '@/hooks/useFeatures';
import { ALL_FEATURES_ENABLED, type FeatureFlags } from '@/services/features';

const fetchFeatures = vi.fn<() => Promise<FeatureFlags>>();
vi.mock('@/services/features', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/features')>();
  return { ...actual, fetchFeatures: () => fetchFeatures() };
});

function flags(overrides: Partial<FeatureFlags> = {}): FeatureFlags {
  return { ...ALL_FEATURES_ENABLED, ...overrides };
}

function renderGate({
  client = new QueryClient(),
  extra,
}: { client?: QueryClient; extra?: React.ReactNode } = {}) {
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/next']}>
        {extra}
        <Routes>
          <Route path="/" element={<div data-testid="current-home">Current home</div>} />
          <Route
            path="/next"
            element={
              <NewUiGate>
                <div data-testid="preview">Preview</div>
              </NewUiGate>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

describe('NewUiGate — Issue #5079', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
    focusManager.setFocused(undefined);
  });

  it('renders the preview when the flag is on', async () => {
    fetchFeatures.mockResolvedValue(flags({ new_ui: true }));
    renderGate();
    expect(await screen.findByTestId('preview')).toBeInTheDocument();
  });

  it('redirects to a working current-UI page when the flag is off', async () => {
    fetchFeatures.mockResolvedValue(flags({ new_ui: false }));
    renderGate();
    expect(await screen.findByTestId('current-home')).toBeInTheDocument();
    expect(screen.queryByTestId('preview')).not.toBeInTheDocument();
  });

  it('holds the requested URL while the first read is pending instead of redirecting', async () => {
    // `new_ui` is fail-closed, so the flags read false before the response lands.
    // Redirecting on that would discard a deep link into the preview — the bug
    // FeatureGate documents for /flows/:id.
    let resolve: (f: FeatureFlags) => void = () => {};
    fetchFeatures.mockReturnValue(new Promise((r) => { resolve = r; }));

    renderGate();
    expect(screen.getByTestId('next-gate-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('current-home')).not.toBeInTheDocument();
    const escape = screen.getByRole('link', { name: 'Back to current UI' });
    expect(escape).toHaveAttribute('href', '/');
    expect(escape.tagName).toBe('A');

    await act(async () => { resolve(flags({ new_ui: true })); });
    expect(await screen.findByTestId('preview')).toBeInTheDocument();
  });

  it('closes an ALREADY-OPEN preview when the operator disables the flag', async () => {
    // The in-session rollback. First read enables the preview; the flag is then
    // turned off server-side, and the bounded revalidation must eject this session
    // WITHOUT it reloading. With staleTime: Infinity and no refetchInterval, the
    // second response would never be requested and this assertion would hang.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchFeatures.mockResolvedValueOnce(flags({ new_ui: true }));

    renderGate();
    expect(await screen.findByTestId('preview')).toBeInTheDocument();

    fetchFeatures.mockResolvedValue(flags({ new_ui: false }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FEATURES_REVALIDATE_MS + 1_000);
    });

    await waitFor(() => {
      expect(screen.getByTestId('current-home')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('preview')).not.toBeInTheDocument();
  });

  it('closes cached-true preview and hides entry after a completed refetch error', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchFeatures.mockResolvedValueOnce(flags({ new_ui: true }));
    const { client } = renderGate({ extra: <TryNewUiLink /> });
    expect(await screen.findByTestId('preview')).toBeInTheDocument();
    expect(screen.getByTestId('try-new-ui')).toBeInTheDocument();

    fetchFeatures.mockRejectedValue(new Error('gateway 503'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FEATURES_REVALIDATE_MS + 2_000);
    });
    // Assert retries have settled before asserting the UI. Cached true remains
    // in react-query even in error state; the preview must not trust it.
    await waitFor(() => {
      expect(client.getQueryState(['features', 'new-ui'])?.status).toBe('error');
    });
    expect(fetchFeatures.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(client.getQueryData<FeatureFlags>(['features', 'new-ui'])?.new_ui).toBe(true);
    expect(screen.getByTestId('current-home')).toBeInTheDocument();
    expect(screen.queryByTestId('preview')).not.toBeInTheDocument();
    expect(screen.queryByTestId('try-new-ui')).not.toBeInTheDocument();

    // A later successful response restores voluntary entry, never a redirect
    // back into the preview after the user has been returned to the current UI.
    fetchFeatures.mockResolvedValue(flags({ new_ui: true }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FEATURES_REVALIDATE_MS + 1_000);
    });
    expect(await screen.findByTestId('try-new-ui')).toBeInTheDocument();
    expect(client.getQueryState(['features', 'new-ui'])?.status).toBe('success');
    expect(screen.getByTestId('current-home')).toBeInTheDocument();
    expect(screen.queryByTestId('preview')).not.toBeInTheDocument();
  });

  it('does not replace the legacy flags snapshot while preview polling changes flags', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = new QueryClient();
    const legacy = flags({ new_ui: false, budget_spend: true, agent_control: true });
    client.setQueryData(['features'], legacy);
    const updated = flags({ new_ui: true, budget_spend: false, agent_control: false });
    fetchFeatures.mockResolvedValue(updated);

    function LegacyFlags() {
      const current = useFeatures();
      return <div data-testid="legacy-flags">{String(current.budget_spend)}:{String(current.agent_control)}</div>;
    }
    renderGate({ client, extra: <LegacyFlags /> });
    expect(await screen.findByTestId('preview')).toBeInTheDocument();
    expect(screen.getByTestId('legacy-flags')).toHaveTextContent('true:true');

    fetchFeatures.mockResolvedValue({ ...updated, new_ui: false });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FEATURES_REVALIDATE_MS + 1_000);
    });
    expect(await screen.findByTestId('current-home')).toBeInTheDocument();
    expect(client.getQueryData(['features'])).toEqual(legacy);
    expect(screen.getByTestId('legacy-flags')).toHaveTextContent('true:true');
    expect(fetchFeatures.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('revalidates on focus even before the polling interval expires', async () => {
    fetchFeatures.mockResolvedValue(flags({ new_ui: true }));
    const { client } = renderGate();
    expect(await screen.findByTestId('preview')).toBeInTheDocument();
    fetchFeatures.mockResolvedValue(flags({ new_ui: false }));
    await act(async () => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    expect(await screen.findByTestId('current-home')).toBeInTheDocument();
    expect(client.getQueryData<FeatureFlags>(['features', 'new-ui'])?.new_ui).toBe(false);
  });

  it('resolves to off for a session that never read the flags at all', async () => {
    fetchFeatures.mockRejectedValue(new Error('gateway 503'));
    renderGate();
    // Fail-closed: no successful read, so the fail-closed default applies and the
    // user lands on a working current-UI page rather than a preview that may not
    // be servable.
    //
    // The generous timeout is the hook's own `retry: 1` (kept from
    // `useFeaturesQuery`, since one retry is right for a transient blip): the gate
    // holds the URL on the spinner while that retry is outstanding and only
    // redirects once it is exhausted. So the user waits out one backoff and then
    // reaches the current UI — they are never stranded, but it is not instant.
    await waitFor(
      () => {
        expect(screen.getByTestId('current-home')).toBeInTheDocument();
      },
      { timeout: 5_000 },
    );
    expect(fetchFeatures).toHaveBeenCalledTimes(2);
  });

  it('polls an unfocused tab', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchFeatures.mockResolvedValue(flags({ new_ui: true }));
    renderGate();
    expect(await screen.findByTestId('preview')).toBeInTheDocument();
    focusManager.setFocused(false);
    fetchFeatures.mockResolvedValue(flags({ new_ui: false }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FEATURES_REVALIDATE_MS + 1_000);
    });
    expect(await screen.findByTestId('current-home')).toBeInTheDocument();
  });

  it('revalidates within the 30-second polling budget', () => {
    // Pins the policy itself, so a future edit that restores staleTime: Infinity
    // on this path fails here rather than silently reopening the rollback gap.
    expect(FEATURES_REVALIDATE_MS).toBeGreaterThan(0);
    expect(FEATURES_REVALIDATE_MS).toBeLessThanOrEqual(30_000);
  });
});
