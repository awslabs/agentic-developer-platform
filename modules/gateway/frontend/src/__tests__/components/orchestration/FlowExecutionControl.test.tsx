import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FlowExecutionControl } from '@/components/orchestration/FlowExecutionControl';
import { apiClient } from '@/services/api';
import { Permission } from '@/types';

vi.mock('@/services/api', () => ({ apiClient: { post: vi.fn() } }));
const hasPermission = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({ hasPermission }) }));

function control(paused: boolean | undefined, compact = false) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(<QueryClientProvider client={client}><FlowExecutionControl flowId="flow-1" paused={paused} compact={compact} /></QueryClientProvider>);
  return invalidate;
}

beforeEach(() => {
  vi.clearAllMocks();
  hasPermission.mockReturnValue(true);
  vi.mocked(apiClient.post).mockResolvedValue({ data: {} });
});

describe('Flow execution controls', () => {
  it.each([true, false])('saves the opposite pause state and refreshes list/detail queries (paused=%s)', async (paused) => {
    const invalidate = control(paused);
    await userEvent.click(screen.getByRole('button', { name: paused ? 'Resume flow' : 'Pause flow' }));
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith('/orchestration/flows/flow-1/execution', { paused: !paused }));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['orchestration'] }));
    expect(hasPermission).toHaveBeenCalledWith(Permission.PLAN_APPROVE);
  });

  it('shows state but no mutation to a reader', () => {
    hasPermission.mockReturnValue(false);
    control(true);
    expect(screen.getByText('Paused')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('does not invent a state or control with an old API response', () => {
    control(undefined);
    expect(screen.getByText(/Control unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('disables repeat clicks until the request settles', async () => {
    let finish!: () => void;
    vi.mocked(apiClient.post).mockImplementation(() => new Promise(resolve => { finish = () => resolve({ data: {} }); }));
    control(true);
    await userEvent.click(screen.getByRole('button', { name: 'Resume flow' }));
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled();
    finish();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume flow' })).toBeEnabled());
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });

  it('keeps the saved state visible after a failure and allows retry', async () => {
    vi.mocked(apiClient.post).mockRejectedValue(new Error('offline'));
    const invalidate = control(true);
    await userEvent.click(screen.getByRole('button', { name: 'Resume flow' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The change was not saved');
    expect(screen.getByText('Paused')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume flow' })).toBeEnabled();
    expect(invalidate).not.toHaveBeenCalled();
  });
});
