import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import AssistantInstallationChat from '@/pages/AssistantInstallationChat';
import { InstallationCard } from '@/pages/settings/components/InstallationCard';
import { getInstallationStatus } from '@/services/installationStatus';
import type { GitHubConnectionItem } from '@/services/connections';

vi.mock('@/services/installationStatus', () => ({ getInstallationStatus: vi.fn() }));
vi.mock('@/pages/AgentChat', () => ({ default: ({ installationId }: { installationId: string }) => <p>Chat for installation {installationId}</p> }));
let assistantEnabled = true;
let chatEnabled = true;
let featuresPending = false;
let featuresError = false;
let user = { id: 'installer', orgId: 'tenant-a' };
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user }) }));
vi.mock('@/hooks/useFeatures', () => ({
  useRevalidatingFeaturesQuery: () => ({ data: { chat: chatEnabled, assistant_ui: assistantEnabled }, isPending: featuresPending, isError: featuresError }),
}));

const status = { installation_id: '101', status: 'partial' as const, observed_at: '2026-10-01T00:00:00Z' };
const connection: GitHubConnectionItem = {
  provider: 'github', installation_id: 101, account_login: 'example-org', account_type: 'Organization',
  repository_selection: 'all', repository_count: 1, repositories: [], installed_at: null,
  configure_url: 'https://github.com/settings/installations/101', manage_url: 'https://github.com/settings/installations/101', can_manage: false,
};
let queryClient: QueryClient;
const readStatus = vi.mocked(getInstallationStatus);

function entry(path = '/assistant/installation/101', readOnly = false, revoked = false) {
  return <QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/" element={<p>Unavailable</p>} />
    <Route path="/settings/connections" element={<InstallationCard connection={{ ...connection, revocation_pending: revoked }} onDisconnect={vi.fn()} readOnly={readOnly} />} />
    <Route path="/assistant/installation/:installationId" element={<AssistantInstallationChat />} />
  </Routes></MemoryRouter></QueryClientProvider>;
}

describe('installation deployment context', () => {
  beforeEach(() => {
    assistantEnabled = true;
    chatEnabled = true;
    featuresPending = false;
    featuresError = false;
    user = { id: 'installer', orgId: 'tenant-a' };
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    readStatus.mockReset().mockResolvedValue(status);
  });

  it('opens chat from authorized deployment status and rechecks access on navigation', async () => {
    render(entry('/settings/connections'));
    const link = await screen.findByRole('link', { name: 'Ask about deployment' });
    expect(screen.getByRole('region', { name: 'Deployment status' })).toHaveTextContent('partial runtime observations');
    expect(screen.getByText(/Observed at/)).toHaveTextContent(status.observed_at);
    expect(readStatus).toHaveBeenCalledTimes(1);
    await userEvent.setup().click(link);
    expect(await screen.findByText('Chat for installation 101')).toBeInTheDocument();
    expect(readStatus).toHaveBeenCalledTimes(2);
    expect(readStatus).toHaveBeenLastCalledWith('101', expect.any(AbortSignal));
  });

  it.each(['denied', 'capability unavailable'])('hides deployment details and denies direct navigation when %s', async (reason) => {
    readStatus.mockRejectedValue(new Error(reason));
    const card = render(entry('/settings/connections'));
    await waitFor(() => expect(queryClient.getQueryState(['installation-status', 'installer', 'tenant-a', '101'])?.status).toBe('error'));
    expect(screen.queryByRole('region', { name: 'Deployment status' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Ask about deployment' })).not.toBeInTheDocument();
    card.unmount();
    render(entry());
    expect(await screen.findByRole('alert')).toHaveTextContent('unavailable');
    expect(screen.queryByText(/Chat for installation/)).not.toBeInTheDocument();
  });

  it.each(['flag off', 'chat off', 'pending', 'failed refresh'])('does not resolve installation references with %s', (mode) => {
    assistantEnabled = mode !== 'flag off';
    chatEnabled = mode !== 'chat off';
    featuresPending = mode === 'pending';
    featuresError = mode === 'failed refresh';
    const card = render(entry('/settings/connections'));
    expect(screen.queryByRole('region', { name: 'Deployment status' })).not.toBeInTheDocument();
    card.unmount();
    render(entry());
    expect(readStatus).not.toHaveBeenCalled();
    expect(screen.queryByText(/Chat for installation/)).not.toBeInTheDocument();
  });

  it.each(['other workspace', 'revoked connection'])('does not request status for %s cards', (reason) => {
    render(entry('/settings/connections', reason === 'other workspace', reason === 'revoked connection'));
    expect(readStatus).not.toHaveBeenCalled();
  });

  it('rejects a mismatched installation reference', async () => {
    readStatus.mockResolvedValue({ ...status, installation_id: '202' });
    render(entry());
    expect(await screen.findByRole('alert')).toHaveTextContent('unavailable');
    expect(screen.queryByText(/Chat for installation/)).not.toBeInTheDocument();
  });

  it('waits for a fresh authorization response instead of opening from cached data', async () => {
    queryClient.setQueryData(['installation-status', 'installer', 'tenant-a', '101'], status);
    readStatus.mockReturnValue(new Promise(() => {}));
    render(entry());
    await waitFor(() => expect(readStatus).toHaveBeenCalled());
    expect(screen.getByRole('status')).toHaveTextContent('Checking installation access');
    expect(screen.queryByText(/Chat for installation/)).not.toBeInTheDocument();
  });

  it('withdraws chat when entitlement revalidation fails', async () => {
    render(entry());
    expect(await screen.findByText('Chat for installation 101')).toBeInTheDocument();
    readStatus.mockRejectedValue(new Error('revoked'));
    await act(() => queryClient.refetchQueries({ queryKey: ['installation-status'] }));
    expect(await screen.findByRole('alert')).toHaveTextContent('unavailable');
    expect(screen.queryByText(/Chat for installation/)).not.toBeInTheDocument();
  });

  it('never reuses another principal’s authorized reference', async () => {
    const page = render(entry());
    expect(await screen.findByText('Chat for installation 101')).toBeInTheDocument();
    user = { id: 'member', orgId: 'tenant-b' };
    readStatus.mockRejectedValue(new Error('denied'));
    page.rerender(entry());
    expect(screen.queryByText(/Chat for installation/)).not.toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('unavailable');
  });

  it('withdraws contextual chat when the rollout flag is disabled', async () => {
    const page = render(entry());
    expect(await screen.findByText('Chat for installation 101')).toBeInTheDocument();
    assistantEnabled = false;
    page.rerender(entry());
    expect(await screen.findByText('Unavailable')).toBeInTheDocument();
    expect(screen.queryByText(/Chat for installation/)).not.toBeInTheDocument();
  });

  it('distinguishes missing runtime evidence from a healthy installation', async () => {
    readStatus.mockResolvedValue({ installation_id: '101', status: 'unavailable', capabilities: { installed_record: { status: 'unavailable' } } });
    render(entry('/settings/connections'));
    expect(await screen.findByText('Deployment status: no runtime observation available.')).toBeInTheDocument();
    expect(screen.getByText('Installed-release record unavailable.')).toBeInTheDocument();
  });
});
