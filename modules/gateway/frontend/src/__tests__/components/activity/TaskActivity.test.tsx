import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { TaskActivity } from '@/components/activity/TaskActivity';
import { getMyTaskActivity } from '@/services/taskActivity';
vi.mock('@/services/taskActivity', () => ({ getMyTaskActivity: vi.fn() }));
beforeEach(() => vi.clearAllMocks());
it('loads only on request, follows empty pages and opens exact Task stream detail', async () => {
  const item = { invocation_id: 'owned-invocation', task_id: 'tsk-owned', source_type: 'task' as const,
    persona: 'coding', task_snapshot: { task_id: 'tsk-owned', invocation_id: 'owned-invocation', status: 'running' } };
  vi.mocked(getMyTaskActivity).mockResolvedValueOnce({ items: [], last_key: 'next' })
    .mockResolvedValueOnce({ items: [item as never], last_key: null });
  const open = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><TaskActivity onOpen={open} /></QueryClientProvider>);
  expect(getMyTaskActivity).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: 'View my Tasks' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Next Tasks' })).toBeEnabled());
  await userEvent.click(screen.getByRole('button', { name: 'Next Tasks' }));
  await userEvent.click(await screen.findByRole('button', { name: 'View Task stream' }));
  expect(open).toHaveBeenCalledWith(item);
  expect(getMyTaskActivity).toHaveBeenLastCalledWith('next');
  expect(screen.getByText(/Older Tasks/)).toBeInTheDocument();
});
it('shows failed authorization as unavailable rather than empty success', async () => {
  vi.mocked(getMyTaskActivity).mockRejectedValue({ status: 403 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><TaskActivity onOpen={vi.fn()} /></QueryClientProvider>);
  await userEvent.click(screen.getByRole('button', { name: 'View my Tasks' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Tasks unavailable');
  expect(screen.queryByText('No Tasks on this page.')).not.toBeInTheDocument();
});
