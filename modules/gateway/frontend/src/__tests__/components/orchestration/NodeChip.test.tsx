import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { NodeChip } from '@/components/orchestration/NodeChip';
import type { GraphNode } from '@/types/orchestration';

const story: GraphNode = {
  id: 'story-1', epic_ref: 'epic-a', wave_ref: 'wave-1', node_ref: 'u1',
  kind: 'story', title: 'Superplane skeleton', state: 'awaiting_merge',
  stalled: false, issue_ref: '5037', attempts: 1, run_id: 'developer-1',
  cost: { status: 'unknown', amount_usd: null },
};
const review: NonNullable<GraphNode['activity']> = {
  invocation_id: 'review:1', persona: 'reviewer', status: 'in_progress', liveness: 'live',
};

function card(node: GraphNode) {
  return <MemoryRouter><ul><NodeChip node={node} /></ul></MemoryRouter>;
}

describe('story delivery stage', () => {
  it('shows the active review and its own run link while retaining the overall state', () => {
    render(card({ ...story, activity: review, result_summary: 'Agent finished. Waiting for merge.' }));
    expect(screen.getByText('Review in progress')).toBeVisible();
    expect(screen.getByText('Development finished. Waiting for merge.')).toBeVisible();
    expect(screen.getByTestId('node-u1')).toHaveAttribute('data-display-state', 'in_progress');
    expect(screen.getByRole('link', { name: 'View review run' })).toHaveAttribute('href', '/activity?chain=developer-1&highlight=review%3A1');
    expect(screen.getByRole('link', { name: 'View run' })).toHaveAttribute('href', '/activity?id=developer-1');
  });

  it('updates to awaiting merge when the refreshed response has no active review', () => {
    const view = render(card({ ...story, activity: review }));
    view.rerender(card({ ...story, activity: null }));
    expect(screen.getByText('Awaiting merge')).toBeVisible();
    expect(screen.queryByText('Review in progress')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'View review run' })).not.toBeInTheDocument();
  });

  it('renders awaiting merge for older API responses without activity', () => {
    render(card(story));
    expect(screen.getByText('Awaiting merge')).toBeVisible();
    expect(screen.queryByText('Review in progress')).not.toBeInTheDocument();
  });

  it('does not call an unverifiable reviewer live', () => {
    render(card({ ...story, activity: { ...review, liveness: 'unverifiable' } }));
    expect(screen.getByText('Review status unconfirmed')).toBeVisible();
    expect(screen.queryByText('Review in progress')).not.toBeInTheDocument();
  });

  it('shows a repair run as fixes in progress', () => {
    render(card({ ...story, activity: { ...review, persona: 'developer' } }));
    expect(screen.getByText('Fixes in progress')).toBeVisible();
    expect(screen.getByRole('link', { name: 'View fixes run' })).toHaveAttribute('href', '/activity?chain=developer-1&highlight=review%3A1');
  });

  it('shows development while the original worker runs', () => {
    render(card({ ...story, state: 'running', activity: { ...review, persona: 'developer', invocation_id: 'developer-1' } }));
    expect(screen.getByText('Development in progress')).toBeVisible();
    expect(screen.getAllByRole('link')).toHaveLength(1);
  });

  it.each(['passed', 'halted', 'superseded'] as const)('does not show stale review activity for a %s story', (state) => {
    render(card({ ...story, state, activity: review }));
    expect(screen.queryByTestId('node-stage-u1')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'View review run' })).not.toBeInTheDocument();
  });
});
