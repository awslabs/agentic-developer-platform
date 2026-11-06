import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { FlowTimeline } from '@/components/orchestration/FlowTimeline';
import { StoryProgress } from '@/components/orchestration/StoryProgress';
import { timingRange } from '@/utils/flowTimeline';
import type { FlowGraph, FlowTimeline as Timeline, StoryTiming } from '@/types/orchestration';

const query = vi.hoisted(() => ({ data: undefined as Timeline | undefined, isPending: false, isError: false, refetch: vi.fn() }));
vi.mock('@/hooks/useFlowTimeline', () => ({ useFlowTimeline: () => query }));

const start = '2026-10-01T10:00:00Z';
const end = '2026-10-01T12:00:00Z';
const base: StoryTiming = { node_id: 'a', state: 'passed', attempts: 3, started_at: start, finished_at: end, transitions: [{ id: 'start', from_state: 'ready', to_state: 'running', at: start }, { id: 'finish', from_state: 'running', to_state: 'passed', at: end }], transitions_truncated: false };

describe('story wall times', () => {
  it('uses accepted boundaries and the server clock for work still underway', () => {
    expect(timingRange(base, '2026-11-01T00:00:00Z')?.duration).toBe(7_200_000);
    expect(timingRange({ ...base, state: 'awaiting_merge', finished_at: null }, end)).toMatchObject({ duration: 7_200_000, ongoing: true });
  });
  it('does not invent a finish for accepted or failed work with missing history', () => {
    expect(timingRange({ ...base, finished_at: null }, end)).toBeNull();
    expect(timingRange({ ...base, state: 'failed', finished_at: null }, end)).toBeNull();
    expect(timingRange({ ...base, started_at: null }, end)).toBeNull();
    expect(timingRange({ ...base, started_at: end, finished_at: start }, end)).toBeNull();
  });
  it('shows duration, attempts, partial/unknown spend and missing history separately', () => {
    query.data = { flow_id: 'flow', server_time: end, total: 2, limit: 100, offset: 0, nodes: [base, { ...base, node_id: 'b', started_at: null }] };
    const flow = { flow_id: 'flow', nodes: [
      { id: 'a', title: 'Build service', wave_ref: 'build', cost: { status: 'known', amount_usd: '12.50', partial: true } },
      { id: 'b', title: 'Older story', wave_ref: 'release', cost: { status: 'unknown', reason: 'no_usage_rows' } },
    ] } as FlowGraph;
    render(<MemoryRouter><FlowTimeline flow={flow} active onOpenNode={vi.fn()} /></MemoryRouter>);
    const first = within(screen.getByTestId('timeline-a'));
    expect(first.getByText('2h 0m')).toBeVisible();
    expect(first.getByText('3 attempts')).toBeVisible();
    expect(first.getByTestId('cost-figure')).toHaveTextContent('12.50');
    expect(first.queryByText(/until next transition/)).not.toBeInTheDocument();
    const details = first.getByText(/Recorded progress/).closest('details')!;
    details.open = true;
    fireEvent(details, new Event('toggle'));
    expect(first.getByText(/2h 0m until next transition/)).toBeVisible();
    const older = within(screen.getByTestId('timeline-b'));
    expect(older.getByText('Timing unavailable')).toBeVisible();
    expect(older.getByTestId('cost-figure')).not.toHaveTextContent('$0.00');
  });
});

it('anchors increasing completion to the left and exposes the same count accessibly', () => {
  const { rerender } = render(<StoryProgress complete={2} total={10} />);
  expect(screen.getByRole('progressbar')).toHaveAttribute('dir', 'ltr');
  expect(screen.getByTestId('story-progress-fill')).toHaveStyle({ width: '20%' });
  rerender(<StoryProgress complete={6} total={10} />);
  expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '6');
  expect(screen.getByTestId('story-progress-fill')).toHaveStyle({ width: '60%' });
});
