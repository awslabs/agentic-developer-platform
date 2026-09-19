import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { NodeChip } from '@/components/orchestration/NodeChip';
import type { GraphNode, StoryRun } from '@/types/orchestration';

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

const development: StoryRun = {
  invocation_id: 'developer-1', persona: 'developer', status: 'complete', liveness: 'exited', invoked_at: '2026-09-15T14:00:00Z',
};
const finishedReview: StoryRun = {
  ...review, status: 'complete', liveness: 'exited', invoked_at: '2026-09-15T14:10:00Z',
};
const repair: StoryRun = {
  ...development, invocation_id: 'repair-1', status: 'in_progress', liveness: 'live', invoked_at: '2026-09-15T14:20:00Z',
};
function withRuns(...runs: StoryRun[]): GraphNode {
  return { ...story, execution_history: { run_id: story.run_id!, runs, activity: null, history_complete: true } };
}

function journeyStep(stage: string) {
  return screen.getByRole('list', { name: 'Story journey' }).querySelector(`[data-stage="${stage}"]`)! as HTMLElement;
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
    expect(screen.queryByText('Review in progress')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'View review run' })).not.toBeInTheDocument();
  });

  it('shows the whole planned journey on queued stories with conditional fixes', () => {
    render(card({ ...story, state: 'pending', attempts: 0 }));
    const journey = screen.getByRole('list', { name: 'Story journey' });
    expect(within(journey).getAllByRole('listitem')).toHaveLength(4);
    for (const label of ['Development', 'Review', 'Fixes', 'Merged (Complete)']) {
      expect(within(journey).getByText(label)).toBeVisible();
    }
    expect(journeyStep('development')).toHaveAttribute('aria-current', 'step');
    expect(journeyStep('fixes')).toHaveTextContent('If requested · then review again');
    expect(journeyStep('merged')).toHaveAttribute('data-progress', 'upcoming');
  });

  it('retains a finished review without claiming approval or making merge current', () => {
    render(card(withRuns(development, finishedReview)));
    expect(screen.getByText('Review finished — check feedback')).toBeVisible();
    expect(journeyStep('review')).toHaveAttribute('aria-current', 'step');
    expect(journeyStep('review')).toHaveTextContent('Run finished · check feedback');
    expect(journeyStep('merged')).not.toHaveAttribute('aria-current');
    expect(screen.queryByText(/approved/i)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View review run' })).toHaveAttribute('href', '/activity?chain=developer-1&highlight=review%3A1');
    expect(screen.queryByText('In progress')).not.toBeInTheDocument();
  });

  it('moves from fixes back to review, retaining the observed repair run', () => {
    const view = render(card(withRuns(development, finishedReview, repair)));
    expect(screen.getByText('Fixes in progress')).toBeVisible();
    expect(journeyStep('fixes')).toHaveAttribute('aria-current', 'step');
    expect(journeyStep('review')).toHaveTextContent('review again after fixes');
    view.rerender(card(withRuns(development, finishedReview, { ...repair, status: 'complete', liveness: 'exited' }, {
      ...finishedReview, invocation_id: 'review-2', invoked_at: '2026-09-15T14:30:00Z', status: 'in_progress', liveness: 'live',
    })));
    expect(screen.getByText('Review in progress')).toBeVisible();
    expect(journeyStep('review')).toHaveAttribute('aria-current', 'step');
    expect(journeyStep('fixes')).toHaveTextContent('Run finished');
    expect(journeyStep('fixes')).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('link', { name: 'View review run' })).toHaveAttribute('href', '/activity?chain=developer-1&highlight=review-2');
  });

  it('makes merged the final complete milestone, without inventing fixes or approval', () => {
    render(card({ ...story, state: 'passed' }));
    expect(screen.getByText('Merged — story complete')).toBeVisible();
    expect(journeyStep('merged')).toHaveTextContent('Story complete');
    expect(journeyStep('merged')).toHaveAttribute('data-progress', 'complete');
    expect(journeyStep('fixes')).toHaveTextContent('History not recorded');
    expect(journeyStep('review')).toHaveTextContent('Status not recorded');
    expect(within(screen.getByRole('list', { name: 'Story journey' })).getAllByRole('listitem')).toHaveLength(4);
  });

  it('does not reuse previous-attempt history on retry', () => {
    render(card({ ...withRuns(development, finishedReview), attempts: 2, run_id: 'attempt-2', state: 'running', activity: review }));
    expect(screen.queryByText('Review finished — check feedback')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'View review run' })).not.toBeInTheDocument();
    expect(screen.getByText('Development status unconfirmed')).toBeVisible();
  });

  it('does not resurrect an old reviewer from capped history or legacy activity', () => {
    const node = withRuns(development, { ...finishedReview, status: 'in_progress', liveness: 'live' });
    node.execution_history!.history_complete = false;
    render(card({ ...node, activity: review }));
    expect(screen.queryByText('Review in progress')).not.toBeInTheDocument();
    expect(journeyStep('review')).not.toHaveAttribute('aria-current');
    expect(screen.getByText(/Stage history is incomplete/)).toBeVisible();
  });

  it('shows a failed review run as ended rather than finished or approved', () => {
    render(card(withRuns(development, { ...finishedReview, status: 'failed' })));
    expect(screen.getByText('Review run ended — check run')).toBeVisible();
    expect(journeyStep('review')).toHaveTextContent('Run ended: failed');
    expect(journeyStep('merged')).toHaveAttribute('data-progress', 'upcoming');
    expect(screen.queryByText('Review finished — check feedback')).not.toBeInTheDocument();
  });

  it('clears the current stage when history becomes unavailable on refresh', () => {
    const view = render(card(withRuns(development, finishedReview)));
    view.rerender(card({ ...withRuns(), activity: review, execution_history: { run_id: story.run_id!, runs: [], activity: null, history_complete: false } }));
    expect(screen.getByText('Awaiting merge')).toBeVisible();
    expect(screen.queryByRole('link', { name: 'View review run' })).not.toBeInTheDocument();
  });

  it('requires a committed run ID before showing legacy activity', () => {
    render(card({ ...story, run_id: null, activity: review }));
    expect(screen.queryByText('Review in progress')).not.toBeInTheDocument();
    expect(screen.getByText('Awaiting merge')).toBeVisible();
  });

  it.each(['failed', 'halted'] as const)('preserves recorded history but stops current-stage claims when %s', (state) => {
    render(card({ ...withRuns(development, finishedReview, repair), state }));
    expect(screen.queryByText('Fixes in progress')).not.toBeInTheDocument();
    expect(journeyStep('review')).toHaveTextContent('Run finished');
    expect(screen.getByRole('list', { name: 'Story journey' }).querySelector('[aria-current]')).toBeNull();
  });

  it('does not mistake an original developer still finalizing for repairs', () => {
    render(card({ ...story, activity: { ...development, status: 'in_progress', liveness: 'live' } }));
    expect(screen.getByText('Development in progress')).toBeVisible();
    expect(screen.queryByText('Fixes in progress')).not.toBeInTheDocument();
  });

  it('keeps the story lifecycle off gate and evaluation cards', () => {
    const view = render(card({ ...story, kind: 'gate', state: 'awaiting_gate' }));
    expect(screen.queryByRole('list', { name: 'Story journey' })).not.toBeInTheDocument();
    expect(screen.getByText('Waiting on a gate')).toBeVisible();
    view.rerender(card({ ...story, kind: 'eval', state: 'passed' }));
    expect(screen.queryByText('Merged (Complete)')).not.toBeInTheDocument();
    expect(screen.getByText('Complete')).toBeVisible();
  });
});


describe('bound pull request evidence', () => {
  const bound = {
    repo: 'aws-e/adp', pr_number: 5293, url: 'https://github.com/aws-e/adp/pull/5293',
    head_sha: 'abc1234', role: 'implementation' as const, state: 'active' as const,
  };

  it('shows the bound PR and refreshed hold without stale result prose', () => {
    const view = render(card({ ...story, binding_hold: 'No pull request is registered.' }));
    view.rerender(card({ ...story, bound_pull_request: bound,
      binding_hold: 'The pull request needs an independent approving review.',
      result_summary: 'No pull request is registered.' }));
    expect(screen.getByRole('link', { name: 'Pull request #5293' })).toHaveAttribute('href', bound.url);
    expect(screen.getByText('The pull request needs an independent approving review.')).toBeVisible();
    expect(screen.queryByText('No pull request is registered.')).not.toBeInTheDocument();
    expect(screen.getByTestId('node-u1')).toHaveAttribute('data-display-state', 'in_progress');
  });

  it('keeps the delivery PR accessible on a completed story', () => {
    render(card({ ...story, state: 'passed', bound_pull_request: bound, binding_hold: null }));
    expect(screen.getByRole('link', { name: 'Pull request #5293' })).toHaveAttribute('href', bound.url);
    expect(screen.getByText('Merged — story complete')).toBeVisible();
  });
});
