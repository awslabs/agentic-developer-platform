import { act, render, screen } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { TaskEventStream } from '@/components/TaskEventStream';
import { readTaskEvents, type TaskUpdate } from '@/services/taskActivity';
vi.mock('@/services/taskActivity', () => ({ readTaskEvents: vi.fn() }));
let send: (update: TaskUpdate) => void;
let signal: AbortSignal;
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(readTaskEvents).mockImplementation((_task, _cursor, abort, callback) => {
    signal = abort; send = callback;
    return new Promise(resolve => abort.addEventListener('abort', () => resolve()));
  });
});
it('renders safe event evidence, deduplicates, reconnects with real cursor and aborts on close', () => {
  const view = render(<TaskEventStream taskId="task" isOpen />);
  const event = { task_id: 'task', sequence: 1, type: 'progress.updated', timestamp: 'now', data: { message: '<script>bad</script>' } };
  act(() => { send({ kind: 'snapshot', status: 'running' }); send({ kind: 'event', event, cursor: 'task:1' }); send({ kind: 'event', event, cursor: 'task:1' }); });
  expect(screen.getAllByText(/<script>bad/)).toHaveLength(1);
  expect(document.querySelector('script')).toBeNull();
  const first = signal;
  act(() => screen.getByRole('button', { name: 'Reconnect stream' }).click());
  expect(first.aborted).toBe(true);
  expect(vi.mocked(readTaskEvents).mock.calls.at(-1)?.[1]).toBe('task:1');
  view.unmount();
  expect(signal.aborted).toBe(true);
});
it('marks missing history and reports terminal outcome', () => {
  const finished = vi.fn();
  render(<TaskEventStream taskId="task" isOpen onTerminal={finished} />);
  act(() => {
    send({ kind: 'event', event: { task_id: 'task', sequence: 1, type: 'progress.updated', timestamp: 'now', data: {} }, cursor: 'task:1' });
    send({ kind: 'event', event: { task_id: 'task', sequence: 4, type: 'task.cancelled', timestamp: 'now', data: {} }, cursor: 'task:4' });
  });
  expect(screen.getByText(/Some event history/)).toBeInTheDocument();
  expect(screen.getByText('Task finished')).toBeInTheDocument();
  expect(finished).toHaveBeenCalledOnce();
});
it('marks truncation when the first retained event starts after sequence one', () => {
  render(<TaskEventStream taskId="task" isOpen />);
  act(() => send({ kind: 'event', event: { task_id: 'task', sequence: 9, type: 'progress.updated', timestamp: 'now', data: {} }, cursor: 'task:9' }));
  expect(screen.getByText(/Some event history/)).toBeInTheDocument();
});
