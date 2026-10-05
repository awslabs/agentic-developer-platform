import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LiveExplanations } from '@/components/LiveExplanations';
import { readExplanations, type StreamUpdate } from '@/services/agentExplanations';
const flags = vi.hoisted(() => ({ data: { agent_explanations: true, agent_control: false }, isPending: false, isError: false }));
vi.mock('@/hooks/useFeatures', () => ({ useRevalidatingFeaturesQuery: () => flags }));
vi.mock('@/services/agentExplanations', async () => ({
  ...await vi.importActual<typeof import('@/services/agentExplanations')>('@/services/agentExplanations'), readExplanations: vi.fn(),
}));
let send: (event: StreamUpdate) => void, signal: AbortSignal;
function explanation(sequence: number, text: string): StreamUpdate {
  return { kind: 'explanation', cursor: `run:1:${sequence}`, event: { version: 1, invocation_id: 'run', generation: 1,
    sequence, timestamp: '2026-09-24T10:00:00Z', kind: 'explanation', payload: { text } } };
}
afterEach(() => vi.useRealTimers());
beforeEach(() => {
  vi.clearAllMocks(); flags.data.agent_explanations = true; flags.isError = false; flags.isPending = false;
  vi.mocked(readExplanations).mockImplementation((_id, _cursor, abort, callback) => {
    send = callback; signal = abort;
    return new Promise(resolve => abort.addEventListener('abort', () => resolve()));
  });
});
describe('live explanations', () => {
  it('shows explanations with controls off, deduplicates and never autoscrolls', async () => {
    const scroll = vi.fn(); Element.prototype.scrollIntoView = scroll;
    render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    act(() => { send(explanation(1, 'Mechanism: bounded replay.')); send(explanation(1, 'duplicate')); send(explanation(2, 'Evidence: source tests.')); });
    expect(screen.getByText('Mechanism: bounded replay.')).toBeInTheDocument();
    expect(screen.queryByText('duplicate')).not.toBeInTheDocument();
    expect(scroll).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Jump to latest/ }));
    await waitFor(() => expect(scroll).toHaveBeenCalledOnce());
  });
  it('keeps heartbeat freshness separate and safely renders hostile text', () => {
    const { container } = render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    act(() => send({ kind: 'heartbeat' }));
    expect(screen.queryByText(/Last explanation:/)).not.toBeInTheDocument();
    act(() => send(explanation(1, '<script>alert(1)</script>\n[bad](javascript:alert(1))\n![track](https://evil.test/pixel)')));
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a')?.getAttribute('href')).not.toContain('javascript:');
  });
  it('aborts on close and run switch and renders an explicit gap', () => {
    const view = render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    const first = signal;
    act(() => { send(explanation(1, 'First')); send({ kind: 'reset' }); send(explanation(9, 'Retained')); });
    expect(screen.getByText(/Some live history/)).toBeInTheDocument();
    expect(screen.queryByText('First')).not.toBeInTheDocument();
    view.rerender(<LiveExplanations invocationId="other" isOpen terminal={false} />);
    expect(first.aborted).toBe(true);
    view.unmount(); expect(signal.aborted).toBe(true);
  });
  it.each(['isPending', 'isError'] as const)('fails closed when %s', field => {
    flags[field] = true;
    render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    expect(readExplanations).not.toHaveBeenCalled();
  });
  it('retains transcript guidance on terminal runs without opening a stream', () => {
    render(<LiveExplanations invocationId="run" isOpen terminal />);
    expect(readExplanations).not.toHaveBeenCalled();
    expect(screen.getByText(/final transcript available/)).toBeInTheDocument();
  });
});

 it('replaces partial messages and tracks concurrent tools until each completes', () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-24T10:00:00Z'));
    render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    const progress = (seq: number, id: string, text: string, category: 'tool' | 'message', state: 'running' | 'completed') => {
      const update = explanation(seq, text);
      update.event!.payload.progress = { id, category, state, started_at: '2026-09-24T10:00:00Z' };
      send(update);
    };
    act(() => { progress(1, 'm', 'Partial message', 'message', 'running'); progress(2, 'm', 'Complete message', 'message', 'completed');
      progress(3, 'a', 'Running tests', 'tool', 'running'); progress(4, 'b', 'Reading files', 'tool', 'running'); });
    expect(screen.queryByText('Partial message')).not.toBeInTheDocument();
    expect(screen.getByText('Complete message')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(3000));
    expect(screen.getAllByText('Running · 3s elapsed')).toHaveLength(2);
    act(() => progress(5, 'a', 'Tests passed', 'tool', 'completed'));
    expect(screen.getAllByText('Running · 3s elapsed')).toHaveLength(1);
    expect(screen.queryByText('Running tests')).not.toBeInTheDocument();
    act(() => send({ kind: 'terminal', event: { ...explanation(6, '').event!, kind: 'terminal' } }));
    expect(screen.queryByText(/elapsed/)).not.toBeInTheDocument();
  });

it('pins the latest checklist across tool history eviction and clears it on a different run', () => {
  const view = render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
  const plan = (seq: number, text: string) => {
    const update = explanation(seq, text);
    update.event!.payload.progress = { id: 'plan', category: 'plan', state: 'running', started_at: '2026-09-24T10:00:00Z' };
    send(update);
  };
  act(() => {
    plan(1, '**0 of 2 tasks complete**\n\n- ☐ Implement history\n- ☐ Verify integration');
    plan(2, '**1 of 2 tasks complete**\n\n- ☑ Implement history\n- ☐ Verify integration');
    for (let i = 3; i < 150; i++) send(explanation(i, `Tool update ${i}`));
  });
  expect(screen.getByRole('region', { name: 'Task checklist' })).toBeInTheDocument();
  expect(screen.getByText('1 of 2 tasks complete')).toBeInTheDocument();
  expect(screen.queryByText('0 of 2 tasks complete')).not.toBeInTheDocument();
  expect(screen.getByText('☐ Verify integration')).toBeInTheDocument();
  view.rerender(<LiveExplanations invocationId="other" isOpen terminal />);
  expect(screen.queryByRole('region', { name: 'Task checklist' })).not.toBeInTheDocument();
});

it('shows plain task states and stops claiming current work after the run ends', () => {
  render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
  const update = explanation(1, '**Workspace**\n- ☑ `code` WS-c1 — Create workspace\n- ▶ `test` WS-t1 — Check readiness\n- ⛔ `infra` WS-i1 — Live check — Waiting for an account');
  update.event!.payload.progress = { id: 'board', category: 'plan', state: 'running', started_at: '2026-09-24T10:00:00Z' };
  act(() => send(update));
  expect(screen.getByText('1 of 3 tasks completed')).toBeInTheDocument();
  expect(screen.getByText('Working on')).toBeInTheDocument();
  expect(screen.getByRole('progressbar')).toHaveAttribute('max', '3');
  expect(screen.getByText('Blocked / deferred', { selector: 'span' })).toBeInTheDocument();
  act(() => send({ kind: 'finished' }));
  expect(screen.queryByText('Working on')).not.toBeInTheDocument();
  expect(screen.getAllByText('Last reported in progress').length).toBeGreaterThan(0);
});

it('filters tool activity while retaining the checklist and a single stream connection', () => {
  render(<LiveExplanations invocationId="run" isOpen terminal={false} workspace />);
  act(() => {
    const plan = explanation(1, '- ☐ Finish assignment');
    plan.event!.payload.progress = { id: 'plan', category: 'plan', state: 'running', started_at: '' };
    send(plan);
    send(explanation(2, 'Repair explanation'));
    const tool = explanation(3, 'npm test');
    tool.event!.payload.progress = { id: 'tool', category: 'tool', state: 'completed', started_at: '' };
    send(tool);
  });
  act(() => screen.getByRole('button', { name: 'Tools & logs' }).click());
  expect(screen.getByText('npm test')).toBeInTheDocument();
  expect(screen.queryByText('Repair explanation')).not.toBeInTheDocument();
  expect(screen.getByText('☐ Finish assignment')).toBeInTheDocument();
  expect(readExplanations).toHaveBeenCalledTimes(1);
});

it('renders live tables and task checkboxes, with keyboard access to wide output', () => {
  render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
  act(() => send(explanation(1, '| Check | Result |\n| --- | --- |\n| Browser | Passed |\n\n- [x] Browser checked\n- [ ] CI pending\n\n```text\nnpm test\n```')));
  expect(screen.getByRole('table')).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: 'Passed' })).toBeInTheDocument();
  const boxes = screen.getAllByRole('checkbox');
  expect(boxes[0]).toBeChecked();
  expect(boxes[1]).not.toBeChecked();
  for (const box of boxes) expect(box).toBeDisabled();
  expect(screen.getByRole('region', { name: 'Activity table' })).toHaveAttribute('tabindex', '0');
  expect(screen.getByLabelText('Code or command output')).toHaveAttribute('tabindex', '0');
});

it('connects explicit task references without inventing tool ownership or reopening the stream', async () => {
  const view = render(<LiveExplanations invocationId="run" isOpen terminal={false} workspace />);
  const plan = explanation(1, '**Workspace**\n- ☑ `code` WS-c1 — Create workspace\n- ▶ `test` WS-t1 — Check readiness\n- ⛔ `infra` WS-i1 — Live check — Waiting for an account');
  plan.event!.payload.progress = { id: 'board', category: 'plan', state: 'running', started_at: '2026-09-24T10:00:00Z' };
  act(() => { send(plan); send(explanation(2, 'WS-c1 passed review.')); send(explanation(3, 'WS-c10 is unrelated.')); send(explanation(4, 'Running general tests.')); });
  fireEvent.click(screen.getByRole('button', { name: 'View activity mentioning WS-c1' }));
  const history = screen.getByLabelText('Explanation history');
  expect(within(history).getByText('WS-c1 passed review.')).toBeInTheDocument();
  expect(within(history).queryByText('WS-c10 is unrelated.')).not.toBeInTheDocument();
  expect(within(history).queryByText('Running general tests.')).not.toBeInTheDocument();
  expect(history).toHaveFocus();
  fireEvent.click(screen.getByRole('button', { name: 'View activity mentioning WS-i1' }));
  expect(screen.getByText(/No retained updates mention this task ID/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Show all activity' }));
  expect(within(history).getByText('Running general tests.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'View task WS-c1' }));
  expect(screen.getByRole('button', { name: 'View activity mentioning WS-c1' })).toHaveAttribute('aria-pressed', 'true');
  await waitFor(() => expect(screen.getByRole('button', { name: 'View activity mentioning WS-c1' }).closest('li')).toHaveFocus());
  expect(readExplanations).toHaveBeenCalledTimes(1);
  view.rerender(<LiveExplanations invocationId="another" isOpen terminal={false} workspace />);
  expect(screen.queryByText('Activity mentioning')).not.toBeInTheDocument();
  expect(screen.queryByText('WS-c1 passed review.')).not.toBeInTheDocument();
});

it('keeps blockers in remaining tasks and unreferenced updates outside a task filter', () => {
  render(<LiveExplanations invocationId="run" isOpen terminal={false} workspace />);
  const plan = explanation(1, '- ☑ `code` A-c1 — Completed task\n- ⛔ `infra` A-i1 — Waiting for access');
  plan.event!.payload.progress = { id: 'board', category: 'plan', state: 'running', started_at: '2026-09-24T10:00:00Z' };
  act(() => send(plan));
  fireEvent.change(screen.getByLabelText('Show tasks'), { target: { value: 'remaining' } });
  expect(screen.queryByRole('button', { name: 'View activity mentioning A-c1' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'View activity mentioning A-i1' }));
  act(() => { send(explanation(2, 'General update')); send(explanation(3, 'A-i1 still needs access')); });
  const history = screen.getByLabelText('Explanation history');
  expect(within(history).queryByText('General update')).not.toBeInTheDocument();
  expect(within(history).getByText('A-i1 still needs access')).toBeInTheDocument();
  act(() => send({ kind: 'reset' }));
  expect(screen.queryByRole('button', { name: 'Show all activity' })).not.toBeInTheDocument();
});
