import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { NodeChip } from '@/components/orchestration/NodeChip';
import type { GraphNode, StoryRun } from '@/types/orchestration';
import evaluationRefusal from '../../fixtures/evaluation-refusal-node.json';
import repositoryRefusal from '../../fixtures/repository-evaluation-refusal-node.json';

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

it('keeps post-merge acceptance visible while development history can be expanded', async () => {
  render(card({ ...story, state: 'passed', delivery_progress: {
    stage: 'deployment_pending', actor: 'engine', automation: 'engine', detail: 'Waiting for deployment evidence.', blockers: [],
    acceptance: { status: 'in_progress', detail: 'Merge is complete; engine acceptance is still in progress.', checks: [
      { id: 'merge', label: 'Merge verified', status: 'passed', detail: 'Verified merged revision.' },
      { id: 'deployment', label: 'Deployment and runtime verified', status: 'waiting', detail: 'Waiting for deployment evidence.' },
    ] },
  } }));
  expect(screen.getByRole('region', { name: 'Engine acceptance' })).toBeVisible();
  expect(screen.getByText('Merge verified')).toBeVisible();
  expect(screen.getByTestId('node-u1')).toHaveAttribute('data-display-state', 'in_progress');
  expect(screen.getByRole('list', { name: 'Story journey' })).not.toBeVisible();
  await userEvent.click(screen.getByText('Development and review history'));
  expect(screen.getByRole('list', { name: 'Story journey' })).toBeVisible();
  expect(screen.getByText('Code merged; engine acceptance continues.')).toBeVisible();
});

describe('planned story files', () => {
  const groups: NonNullable<GraphNode['file_groups']> = [
    { group: 'API and storage', purpose: 'Persist service configuration', create: ['modules/service/api.py'],
      modify: ['modules/gateway/src/app.py'], modify_after: [{ path: 'modules/service/models.py', created_by: 'SA02' }] },
    { group: 'Tests', create: ['modules/service/tests/test_api.py'], modify: [], modify_after: [] },
    { group: 'Documentation', create: [], modify: ['docs/service/README.md'], modify_after: [] },
  ];

  it('keeps file groups collapsed until opened and shows exact create/modify ownership', async () => {
    render(card({ ...story, file_groups: groups }));
    const summary = screen.getByText('Planned files').closest('summary')!;
    expect(summary).toHaveTextContent('2 new · 3 to modify');
    expect(summary).toHaveTextContent('API and storage · Tests · +1 group');
    expect(screen.getByText('modules/service/api.py')).not.toBeVisible();
    await userEvent.click(summary);
    expect(summary.parentElement).toHaveAttribute('open');
    expect(screen.getByRole('region', { name: 'API and storage' })).toBeVisible();
    expect(screen.getByText('Persist service configuration')).toBeVisible();
    expect(screen.getByText('Modify after SA02')).toBeVisible();
    for (const path of ['modules/service/api.py', 'modules/gateway/src/app.py', 'modules/service/models.py', 'docs/service/README.md']) {
      expect(screen.getByText(path)).toBeVisible();
    }
    await userEvent.click(summary);
    expect(screen.getByText('modules/service/api.py')).not.toBeVisible();
  });

  it('does not add an empty panel to legacy stories or evaluation nodes', () => {
    const view = render(card(story));
    expect(screen.queryByText('Planned files')).not.toBeInTheDocument();
    view.rerender(card({ ...story, kind: 'eval', file_groups: groups }));
    expect(screen.queryByText('Planned files')).not.toBeInTheDocument();
  });

  it('renders paths and purposes as literal text', async () => {
    render(card({ ...story, file_groups: [{ group: 'Notes', purpose: '<script>example</script>',
      create: ['docs/a very long folder/a-very-long-file-name.md'], modify: [], modify_after: [] }] }));
    await userEvent.click(screen.getByText('Planned files'));
    expect(screen.getByText('<script>example</script>')).toBeVisible();
    expect(screen.getByText('docs/a very long folder/a-very-long-file-name.md')).toHaveClass('[overflow-wrap:anywhere]');
    expect(screen.getByTestId('node-u1').querySelector('script')).toBeNull();
  });
});

describe('accepted story binding and publication prerequisites', () => {
  it('shows an unbound story and its execution hold without inventing an issue link', () => {
    render(card({ ...story, state: 'ready', run_id: null, repository_status: 'unbound',
      readiness_hold: 'repository_verification_pending', issue_url: null }));
    const status = screen.getByTestId('node-repository-u1');
    expect(status).toHaveTextContent('Story target: unbound · provider ID unbound · connection unbound');
    expect(status).toHaveTextContent('Repository binding: unbound');
    expect(status).toHaveTextContent('Execution hold: repository verification pending');
    expect(screen.queryByRole('link', { name: 'View issue and evidence' })).not.toBeInTheDocument();
  });

  it('shows issue creation while the native parent link is pending, separately from accepted repository status', () => {
    render(card({ ...story, state: 'ready', run_id: null, issue_ref: '42',
      repository_identity: 'delivery/api', provider_repository_id: 11,
      repository_connection: { provider: 'github', installation_id: 3 },
      repository_status: 'verified', issue_publication_status: 'issue_created_link_pending',
      readiness_hold: 'amendment_issue_link_unconfirmed', issue_url: 'https://github.com/delivery/api/issues/42' }));
    const status = screen.getByTestId('node-repository-u1');
    expect(status).toHaveTextContent('Story target: delivery/api · provider ID 11 · connection 3');
    expect(status).toHaveTextContent('Repository binding: verified binding');
    expect(screen.getByRole('link', { name: 'delivery/api#42' })).toHaveAttribute('href', 'https://github.com/delivery/api/issues/42');
    expect(status).toHaveTextContent('Issue publication: issue created; native epic link pending');
    expect(status).toHaveTextContent('Execution hold: native epic link unconfirmed');
    expect(screen.getByRole('link', { name: 'View issue and evidence' })).toHaveAttribute('href', 'https://github.com/delivery/api/issues/42');
  });

  it.each(['denied', 'stale'] as const)('keeps a %s admission state visible despite populated metadata', (status) => {
    render(card({ ...story, state: 'ready', run_id: null, repository_identity: 'delivery/api',
      provider_repository_id: 11, repository_connection: { provider: 'github', installation_id: 3 },
      repository_status: status, issue_publication_status: 'unconfirmed',
      delivery_progress: { stage: 'admission', actor: 'flow-owner', detail: 'Admission refused',
        blocker: 'repository_not_permitted', blockers: ['repository_not_permitted'], automation: 'blocked' },
    }));
    const view = screen.getByTestId('node-repository-u1');
    expect(view).toHaveTextContent(status === 'denied' ? 'denied by admission' : 'stale; review required');
    expect(view).toHaveTextContent('publication cannot be confirmed');
    expect(screen.getByText('Admission refused')).toBeVisible();
    expect(screen.queryByRole('link', { name: 'View issue and evidence' })).not.toBeInTheDocument();
  });
});

describe('story delivery stage', () => {
  it('names the accepted operational executor without changing developer stories', () => {
    render(card({ ...story, executor: { role: 'operate', persona: 'agent-codex-operations' } }));
    expect(screen.getByText('Operations story')).toBeVisible();
    expect(screen.getByTestId('node-persona-u1')).toHaveTextContent('agent-codex-operations');
  });

  it('shows the active review and its own run link while retaining the overall state', () => {
    render(card({ ...story, activity: review, result_summary: 'Agent finished. Waiting for merge.' }));
    expect(screen.getByText('Review in progress')).toBeVisible();
    expect(screen.getByText('Development finished. Waiting for merge.')).toBeVisible();
    expect(screen.getByTestId('node-u1')).toHaveAttribute('data-display-state', 'in_progress');
    expect(screen.getByRole('link', { name: 'View review run' })).toHaveAttribute('href', '/activity?chain=developer-1&highlight=review%3A1');
    expect(within(journeyStep('development')).getByRole('link', { name: 'View development run' })).toHaveAttribute('href', '/activity?id=developer-1');
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
    expect(journeyStep('review')).toHaveAttribute('data-progress', 'complete');
    expect(journeyStep('review')).not.toHaveAttribute('aria-current');
    expect(journeyStep('review')).toHaveTextContent('Run finished · check feedback');
    expect(journeyStep('merged')).not.toHaveAttribute('aria-current');
    expect(screen.queryByText(/approved/i)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View review run' })).toHaveAttribute('href', '/activity?chain=developer-1&highlight=review%3A1');
    expect(screen.queryByText('In progress')).not.toBeInTheDocument();
  });

  it('shows a completed Codex review with green ticks and links beside the completed steps', () => {
    const codexReview = { ...finishedReview, persona: 'agent-codex-reviewer' as const };
    render(card({ ...withRuns(development, codexReview), state: 'passed', bound_pull_request: {
      repo: 'aws-e/adp', pr_number: 5862, url: 'https://github.com/aws-e/adp/pull/5862',
      head_sha: 'reviewed-head', role: 'implementation', state: 'active',
    } }));
    for (const stage of ['development', 'review', 'merged']) {
      const step = journeyStep(stage);
      expect(step).toHaveAttribute('data-progress', 'complete');
      expect(step.querySelector('[aria-hidden="true"]')).toHaveTextContent('✓');
      expect(step.querySelector('[aria-hidden="true"]')).toHaveClass('text-green-700');
    }
    expect(within(journeyStep('review')).queryByText('Upcoming')).not.toBeInTheDocument();
    expect(within(journeyStep('review')).getByRole('link', { name: 'View review run' }))
      .toHaveAttribute('href', '/activity?chain=developer-1&highlight=review%3A1');
    expect(within(journeyStep('development')).getByRole('link', { name: 'View development run' }))
      .toHaveAttribute('href', '/activity?id=developer-1');
    expect(within(journeyStep('merged')).getByRole('link', { name: 'Pull request #5862' }))
      .toHaveAttribute('href', 'https://github.com/aws-e/adp/pull/5862');
    expect(journeyStep('fixes')).toHaveTextContent('No separate fixes run recorded');
    expect(journeyStep('fixes')).not.toHaveAttribute('data-progress', 'complete');
  });

  it('recognizes an active Codex review without calling it complete', () => {
    render(card(withRuns(development, { ...finishedReview, persona: 'agent-codex-reviewer', status: 'in_progress', liveness: 'live' })));
    expect(screen.getByText('Review in progress')).toBeVisible();
    expect(journeyStep('review')).toHaveAttribute('data-progress', 'current');
    expect(journeyStep('development')).toHaveAttribute('data-progress', 'complete');
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
    expect(journeyStep('fixes')).toHaveAttribute('data-progress', 'complete');
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


describe('actionable delivery status', () => {
  const diagnostic: NonNullable<GraphNode['delivery_progress']> = {
    stage: 'repair', actor: 'developer', detail: 'CI has failed on the current PR revision.',
    blocker: 'ci_failed', blockers: ['ci_failed', 'changes_requested', 'automation_not_configured'],
    next_action: 'Repair the failing checks, then request a fresh review.', automation: 'not_configured',
    checks_state: 'FAILURE', review_state: 'changes_requested',
    scheduled_action: null, next_check_at: null, observed_at: '2026-09-21T00:00:00Z',
  };

  it('shows stage, responsible actor, all blockers and next action together', () => {
    render(card({ ...story, delivery_progress: diagnostic, binding_hold: 'The pull request is not merged yet.', result_summary: 'Old diagnosis' }));
    expect(screen.getByTestId('node-stage-u1')).toHaveTextContent('Repairs');
    const status = within(screen.getByRole('region', { name: 'Current delivery status' }));
    expect(status.getByText('Developer')).toBeVisible();
    expect(status.getByText('CI failed')).toBeVisible();
    expect(status.getByText('Review requested changes')).toBeVisible();
    expect(status.getByText(diagnostic.next_action!)).toBeVisible();
    expect(status.getByText('No automatic action is scheduled.')).toBeVisible();
    expect(status.getByText('Automatic review and repair are not configured for this flow.')).toBeVisible();
    expect(screen.queryByText('The pull request is not merged yet.')).not.toBeInTheDocument();
    expect(screen.queryByText('Old diagnosis')).not.toBeInTheDocument();
  });

  it('makes an unfunded review visible and clears the hold after admission', () => {
    const held: GraphNode = { ...story, state: 'running', display_state: 'stalled', delivery_progress: {
      ...diagnostic, stage: 'awaiting_review', actor: 'flow-owner', automation: 'blocked',
      blocker: 'budget_exhausted', blockers: ['budget_exhausted'],
      detail: 'Review or repair is blocked by budget. The next worker cannot start.',
      next_action: 'Review spend and outstanding reservations against the flow, run, chain and daily limits.',
      scheduled_action: 'Reconcile this execution', next_check_at: '2026-09-21T00:05:00Z',
    } };
    const view = render(card(held));
    expect(screen.getByTestId('node-stage-u1')).toHaveTextContent('Review blocked by budget');
    expect(screen.getByTestId('node-reason-u1')).toHaveTextContent('Budget blocked');
    expect(screen.getByTestId('node-u1')).toHaveAttribute('data-display-state', 'stalled');
    expect(screen.getByText(held.delivery_progress!.next_action!)).toBeVisible();
    expect(screen.getByText('Scheduled check:')).toBeVisible();
    expect(screen.queryByRole('link', { name: 'View review run' })).not.toBeInTheDocument();
    view.rerender(card({ ...held, display_state: 'in_progress', activity: review, delivery_progress: {
      ...held.delivery_progress!, actor: 'reviewer', automation: 'engine', blocker: null, blockers: [],
      detail: 'Reviewing the current PR head in a distinct execution.', next_action: 'Reconcile the recorded delivery stage.',
    } }));
    expect(screen.getByTestId('node-stage-u1')).toHaveTextContent(/^Current stage: Review$/);
    expect(screen.queryByText('Budget blocked')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View review run' })).toBeVisible();
  });

  it('shows only a recorded controller check and removes it on a stale-execution refresh', () => {
    const due = '2026-09-21T00:05:00Z';
    const view = render(card({ ...story, delivery_progress: {
      ...diagnostic, stage: 'awaiting_review', actor: 'reviewer', automation: 'engine', blockers: [], blocker: null,
      detail: 'Review is pending.', scheduled_action: 'Reconcile this execution', next_check_at: due,
    } }));
    const status = screen.getByRole('region', { name: 'Current delivery status' });
    expect(within(status).getByText('Scheduled check:')).toBeVisible();
    expect(status.querySelector(`time[datetime="${due}"]`)).not.toBeNull();
    expect(screen.queryByText('No automatic action is scheduled.')).not.toBeInTheDocument();
    view.rerender(card({ ...story, delivery_progress: {
      ...diagnostic, stage: 'continuation', actor: 'operator', automation: 'paused',
      blocker: 'execution_stale', blockers: ['execution_stale'], detail: 'The execution belongs to an earlier attempt.',
    } }));
    expect(screen.queryByText('Scheduled check:')).not.toBeInTheDocument();
    expect(screen.getByText('Execution belongs to a previous attempt or plan')).toBeVisible();
  });

  it('replaces a CI failure with provider unavailability after refresh', () => {
    const view = render(card({ ...story, delivery_progress: diagnostic }));
    view.rerender(card({ ...story, delivery_progress: {
      ...diagnostic, stage: 'provider_unavailable', actor: 'engine', blocker: 'provider_unavailable', blockers: ['provider_unavailable'],
      detail: 'GitHub evidence could not be verified.', next_action: 'Recheck GitHub evidence.', checks_state: null, review_state: null,
    } }));
    expect(screen.getByTestId('node-stage-u1')).toHaveTextContent('GitHub evidence unavailable');
    expect(screen.queryByText('CI failed')).not.toBeInTheDocument();
    expect(screen.queryByText('Review requested changes')).not.toBeInTheDocument();
  });

  it('describes an undispatched story as not started instead of unconfigured automation', () => {
    render(card({ ...story, state: 'pending', attempts: 0, run_id: null, delivery_progress: {
      ...diagnostic, stage: 'pending', actor: 'engine', automation: 'not_started', blockers: [], blocker: null,
      detail: "Waiting for this story's dependencies and admission requirements.",
      next_action: 'Reassess readiness when prerequisites complete.', checks_state: null, review_state: null, observed_at: null,
    } }));
    expect(screen.getByTestId('node-stage-u1')).toHaveTextContent('Pending');
    expect(screen.getByText(/Not started\. The engine dispatches this story/)).toBeVisible();
    expect(screen.queryByText('Automatic review and repair are not configured for this flow.')).not.toBeInTheDocument();
    expect(screen.queryByText('No automatic action is scheduled.')).not.toBeInTheDocument();
  });

  it('flags an attempt the engine has not initialized without calling the flow unconfigured', () => {
    render(card({ ...story, delivery_progress: {
      ...diagnostic, stage: 'merge', actor: 'operator', automation: 'not_initialized',
      blocker: 'execution_not_initialized', blockers: ['execution_not_initialized'],
      detail: 'Checks and approval are verified and GitHub reports the PR mergeable.',
    } }));
    expect(screen.getByText('Automatic delivery is not initialized')).toBeVisible();
    expect(screen.getByText(/has not initialized delivery tracking for this attempt/)).toBeVisible();
    expect(screen.queryByText('Automatic review and repair are not configured for this flow.')).not.toBeInTheDocument();
  });

  it('shows adopted historical delivery without claiming a worker ran or is scheduled', () => {
    render(card({ ...story, attempts: 0, run_id: null, delivery_progress: {
      ...diagnostic, stage: 'historical_delivery', actor: 'engine', automation: 'reconciliation_only',
      blocker: 'predecessor_pending', blockers: ['predecessor_pending'],
      detail: 'Historical delivery is waiting for predecessor nodes.', next_action: 'Recheck delivery and predecessor requirements.',
    } }));
    expect(screen.getByTestId('node-stage-u1')).toHaveTextContent('Historical delivery');
    expect(screen.getByText('Delivered before engine tracking')).toBeVisible();
    expect(screen.getByText(/Historical delivery; no worker was dispatched/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'View run' })).not.toBeInTheDocument();
    expect(screen.queryByText('Automatic review and repair are not configured for this flow.')).not.toBeInTheDocument();
  });
});


describe('evaluation exception', () => {
  it('renders repository refusal owner, required input and recorded time, then clears on current graph refresh', () => {
    const graphNode: GraphNode = { ...story, ...repositoryRefusal, kind: 'eval', state: 'ready', display_state: 'stalled' };
    const view = render(card(graphNode));
    const diagnostic = screen.getByTestId('node-eval-progress-u1');
    expect(screen.getByTestId('node-reason-u1')).toHaveTextContent('Evidence needed');
    expect(diagnostic).toHaveTextContent('Responsible: flow owner');
    expect(diagnostic).toHaveTextContent(repositoryRefusal.delivery_progress.required_input);
    expect(diagnostic).toHaveTextContent(repositoryRefusal.delivery_progress.next_action);
    expect(diagnostic.querySelector('time')?.getAttribute('datetime')).toBe(repositoryRefusal.delivery_progress.observed_at);
    view.rerender(card({ ...graphNode, display_state: 'queued', delivery_progress: {
      stage: 'ready', actor: 'engine', detail: 'Evaluation is ready for the next engine check.',
      automation: 'not_started', blockers: [], blocker: null,
    } }));
    expect(screen.queryByTestId('node-eval-progress-u1')).not.toBeInTheDocument();
    expect(screen.queryByText('Evidence needed')).not.toBeInTheDocument();
    expect(screen.getByText('Ready for evaluation')).toBeVisible();
  });

  it('renders the graph API admission refusal contract as an actionable evaluation node', () => {
    const graphNode: GraphNode = { ...story, ...evaluationRefusal, kind: 'eval', state: 'ready', display_state: 'stalled' };
    render(card(graphNode));
    expect(screen.getByTestId('node-u1')).toHaveAttribute('data-display-state', evaluationRefusal.display_state);
    expect(screen.getByTestId('node-reason-u1')).toHaveTextContent('Admission refused');
    expect(screen.getByTestId('node-eval-progress-u1')).toHaveTextContent(evaluationRefusal.delivery_progress.detail);
    expect(screen.getByTestId('node-eval-progress-u1')).toHaveTextContent(evaluationRefusal.delivery_progress.next_action);
    expect(screen.queryByText('Queued')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /run/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/Link an evaluation issue/)).not.toBeInTheDocument();
  });

  it('prioritizes a typed graph refusal over a stale evaluation configuration hint', () => {
    const graphNode: GraphNode = {
      ...story, ...evaluationRefusal, kind: 'eval', state: 'ready', display_state: 'stalled',
      configuration_problem: 'Link an evaluation issue in the plan before this evaluation can run.',
    };
    render(card(graphNode));
    expect(screen.getByTestId('node-reason-u1')).toHaveTextContent('Admission refused');
    expect(screen.getByTestId('node-eval-progress-u1')).toHaveTextContent(evaluationRefusal.delivery_progress.next_action);
    expect(screen.queryByText(/Link an evaluation issue/)).not.toBeInTheDocument();
  });

  it.each([
    ['admission', 'evaluation_specification_missing', 'Admission refused', 'Accept a machine specification or choose human acceptance.'],
    ['evidence_wait', 'verified_deployment_required', 'Evidence needed', 'Verify deployment of the current predecessor revision.'],
  ])('shows %s as an actionable evaluation hold, not queued work', (stage, blocker, badge, action) => {
    render(card({ ...story, kind: 'eval', state: 'ready', run_id: null, attempts: 0, issue_ref: null,
      delivery_progress: { stage, actor: 'flow-owner', blocker, blockers: [blocker],
        detail: 'Evidence unavailable for this evaluation.', next_action: action,
        next_check_at: null, automation: 'blocked' } }));
    expect(screen.getByTestId('node-u1')).toHaveAttribute('data-display-state', 'stalled');
    expect(screen.getByTestId('node-reason-u1')).toHaveTextContent(badge);
    expect(screen.getByTestId('node-eval-progress-u1')).toHaveTextContent(action);
    expect(screen.getByText('Evidence unavailable for this evaluation.')).toBeVisible();
    expect(screen.queryByText('Queued')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /run/i })).not.toBeInTheDocument();
  });

  it('replaces a stale refusal with the current human gate', () => {
    const view = render(card({ ...story, kind: 'eval', state: 'ready', run_id: null, attempts: 0,
      delivery_progress: { stage: 'admission', actor: 'flow-owner', detail: 'Admission refused.',
        blocker: 'budget_unavailable', blockers: ['budget_unavailable'], automation: 'blocked' } }));
    view.rerender(card({ ...story, kind: 'eval', state: 'awaiting_gate', run_id: null, attempts: 0,
      delivery_progress: { stage: 'awaiting_gate', actor: 'flow-owner', detail: 'Human evaluation is ready for review.',
        blockers: [], automation: 'paused' } }));
    expect(screen.getByTestId('node-u1')).toHaveAttribute('data-display-state', 'gate');
    expect(screen.getByText('Waiting on a gate')).toBeVisible();
    expect(screen.queryByText('Queued')).not.toBeInTheDocument();
    expect(screen.queryByTestId('node-eval-progress-u1')).not.toBeInTheDocument();
  });

  it('shows owner waiver and its reason without a passing test verdict or run', () => {
    render(card({ ...story, kind: 'eval', state: 'waived', run_id: null, attempts: 0,
      evaluation_waiver: { decision_id: 'waiver-1', actor_id: 'owner-1', created_at: '2026-09-24T12:00:00Z',
        reason: 'Owner accepts the independent evaluation gap', criterion_ids: ['V0-01'], plan_version: 7 } }));
    expect(screen.getByText('Waived by owner approval')).toBeVisible();
    expect(screen.getByText('Independent evaluation was not run.')).toBeVisible();
    expect(screen.getByText('Owner accepts the independent evaluation gap')).toBeVisible();
    expect(screen.queryByText('Complete')).not.toBeInTheDocument();
    expect(screen.queryByText('✓')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /run/i })).not.toBeInTheDocument();
    expect(screen.getByTestId('node-u1')).not.toHaveAttribute('aria-current');
  });
});
