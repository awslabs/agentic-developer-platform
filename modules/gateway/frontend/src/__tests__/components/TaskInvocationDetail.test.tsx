import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { InvocationDetail } from '@/components/InvocationDetail';
import type { InvocationItem } from '@/types/activity';
vi.mock('@/components/TaskEventStream', () => ({ TaskEventStream: ({ taskId }: { taskId: string }) => <p>Task stream {taskId}</p> }));
vi.mock('@/components/LiveExplanations', () => ({ LiveExplanations: () => <p>Legacy explanations</p> }));
vi.mock('@/components/ControlPanel', () => ({ ControlPanel: () => <p>Legacy controls</p> }));
it('routes Task details to canonical stream and labels retained report', () => {
  const item = { invocation_id: 'invocation', source_type: 'task', task_id: 'tsk-owned', status: 'failed',
    invoked_at: '2026-09-26T00:00:00Z', transcript_status: 'available', transcript_key: null } as InvocationItem;
  render(<InvocationDetail item={item} isOpen onClose={vi.fn()} />);
  expect(screen.getByText('Task stream tsk-owned')).toBeInTheDocument();
  expect(screen.queryByText('Legacy explanations')).not.toBeInTheDocument();
  expect(screen.queryByText('Legacy controls')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'View retained Task report' })).toBeInTheDocument();
  expect(screen.queryByText('View full transcript')).not.toBeInTheDocument();
});
