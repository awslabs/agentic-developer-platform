import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ExecutionWindowControl } from '@/components/orchestration/ExecutionWindowControl';
import { apiClient } from '@/services/api';
import type { ExecutionWindow } from '@/types/orchestration';

vi.mock('@/services/api', () => ({ apiClient: { post: vi.fn() } }));
const hasPermission = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({ hasPermission }) }));
const request = { expected_plan_version: 3, expected_plan_hash: 'a'.repeat(64), expires_at: '2026-10-01T00:00:00Z', max_wall_clock_seconds: 200000, resume_expired: true, reason: 'Resume after gate approval' };
const expired: ExecutionWindow = { status: 'expired', deadline_at: '2026-09-28T03:31:00Z', renewal_request: request };
function mount(window: ExecutionWindow = expired) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(<QueryClientProvider client={client}><ExecutionWindowControl flowId="flow-1" window={window} /></QueryClientProvider>);
  return invalidate;
}
beforeEach(() => { vi.clearAllMocks(); hasPermission.mockReturnValue(true); });

it('shows expiry before a worker exists and requires a preview before acceptance', async () => {
  const invalidate = mount();
  expect(screen.getByText('Execution window expired')).toBeInTheDocument();
  expect(apiClient.post).not.toHaveBeenCalled();
  vi.mocked(apiClient.post).mockResolvedValueOnce({ snapshot: 'snapshot-1', wall_clock_started_at: '2026-09-27T07:31:00Z', max_wall_clock_seconds: 200000, expires_at: request.expires_at });
  await userEvent.click(screen.getByRole('button', { name: 'Review renewal' }));
  await screen.findByRole('button', { name: 'Confirm renewal' });
  expect(apiClient.post).toHaveBeenCalledTimes(1);
  vi.mocked(apiClient.post).mockResolvedValueOnce({ accepted: true });
  await userEvent.click(screen.getByRole('button', { name: 'Confirm renewal' }));
  await waitFor(() => expect(invalidate).toHaveBeenCalled());
  expect(apiClient.post).toHaveBeenLastCalledWith('/orchestration/flows/flow-1/window/accept', { ...request, expected_snapshot: 'snapshot-1' });
});

it('does not allow viewers to renew', () => {
  hasPermission.mockReturnValue(false); mount();
  expect(screen.getByText('Execution window expired')).toBeInTheDocument();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

it('discards a stale preview and never claims a failed renewal succeeded', async () => {
  mount();
  vi.mocked(apiClient.post).mockResolvedValueOnce({ snapshot: 'old', wall_clock_started_at: '2026-09-27T07:31:00Z', max_wall_clock_seconds: 200000, expires_at: request.expires_at });
  await userEvent.click(screen.getByRole('button', { name: 'Review renewal' }));
  await screen.findByRole('button', { name: 'Confirm renewal' });
  vi.mocked(apiClient.post).mockRejectedValueOnce(new Error('stale snapshot'));
  await userEvent.click(screen.getByRole('button', { name: 'Confirm renewal' }));
  await screen.findByText(/The window was not renewed/);
  expect(screen.queryByText('Execution window renewed')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Confirm renewal' })).not.toBeInTheDocument();
});

it.each(['active', 'complete'] as const)('does not mark %s work expired', status => {
  mount({ status }); expect(screen.queryByText('Execution window expired')).not.toBeInTheDocument();
});
