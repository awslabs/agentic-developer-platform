import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FlowArchiveControl } from '@/components/orchestration/FlowArchiveControl';
import { apiClient } from '@/services/api';
import { Permission } from '@/types';

vi.mock('@/services/api', () => ({ apiClient: { post: vi.fn() } }));
const hasPermission = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({ hasPermission }) }));

function renderControl(archived = false) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(<QueryClientProvider client={client}><FlowArchiveControl flowId="flow-1" archived={archived} /></QueryClientProvider>);
  return invalidate;
}

beforeEach(() => {
  vi.clearAllMocks();
  hasPermission.mockReturnValue(true);
  vi.mocked(apiClient.post).mockResolvedValue({});
});

describe('flow archive controls', () => {
  it.each([[false, 'archive'], [true, 'unarchive']] as const)('saves a reason and refreshes all flow queries (archived=%s)', async (archived, action) => {
    const invalidate = renderControl(archived);
    await userEvent.click(screen.getByRole('button', { name: archived ? 'Unarchive flow' : 'Archive flow' }));
    await userEvent.type(screen.getByRole('textbox', { name: /Reason/ }), 'No active work remains');
    await userEvent.click(screen.getByRole('button', { name: archived ? 'Confirm unarchive' : 'Confirm archive' }));
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(`/orchestration/flows/flow-1/${action}`, { reason: 'No active work remains' }));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['orchestration'] }));
    expect(hasPermission).toHaveBeenCalledWith(Permission.PLAN_APPROVE);
  });

  it('shows the archive state without a mutation action to read-only callers', () => {
    hasPermission.mockReturnValue(false);
    renderControl(true);
    expect(screen.getByText('Archived')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('prevents short or whitespace-only reasons', async () => {
    renderControl();
    await userEvent.click(screen.getByRole('button', { name: 'Archive flow' }));
    await userEvent.type(screen.getByRole('textbox', { name: /Reason/ }), '       ');
    expect(screen.getByRole('button', { name: 'Confirm archive' })).toBeDisabled();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('explains active work and keeps the form open after a refusal', async () => {
    vi.mocked(apiClient.post).mockRejectedValue({ status: 409, detail: { code: 'flow_has_active_work' } });
    const invalidate = renderControl();
    await userEvent.click(screen.getByRole('button', { name: 'Archive flow' }));
    await userEvent.type(screen.getByRole('textbox', { name: /Reason/ }), 'No active work remains');
    await userEvent.click(screen.getByRole('button', { name: 'Confirm archive' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('This flow still has active work');
    expect(screen.getByRole('textbox', { name: /Reason/ })).toHaveValue('No active work remains');
    expect(invalidate).not.toHaveBeenCalled();
  });
});
