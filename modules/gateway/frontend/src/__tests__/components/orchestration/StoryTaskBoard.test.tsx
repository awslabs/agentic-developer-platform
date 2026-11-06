import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it } from 'vitest';
import { StoryTaskBoard } from '@/components/orchestration/StoryTaskBoard';
import type { StoryRunInsight } from '@/types/orchestration';

const insight: StoryRunInsight = {
  run_id: 'run:1', attempt: 2, persona: 'agent-codex-developer', outcome: null, failure: null,
  board_access: 'available', task_board: { generation: 1, sequence: 2, observed_at: '2026-10-30T10:00:00Z',
    text: '- ☑ `code` A — Save work\n- ▶ `test` B — Validate restart\n- ⛔ `infra` C — Await target access' },
};
const view = (value: StoryRunInsight, accepted = false) => <MemoryRouter><StoryTaskBoard insight={value} accepted={accepted} /></MemoryRouter>;

it('updates the collapsed summary and preserves expansion when tasks complete', () => {
  const { rerender } = render(view(insight));
  expect(screen.getByText('Task board · 1/3 tasks complete · 1 blocked')).toBeVisible();
  screen.getAllByText('Validate restart').forEach(element => expect(element).not.toBeVisible());
  fireEvent.click(screen.getByText('Task board · 1/3 tasks complete · 1 blocked'));
  screen.getAllByText('Validate restart').forEach(element => expect(element).toBeVisible());
  rerender(view({ ...insight, task_board: { ...insight.task_board!, sequence: 3, text: insight.task_board!.text.replace('▶', '☑') } }));
  expect(screen.getByText('Task board · 2/3 tasks complete · 1 blocked')).toBeVisible();
  expect(screen.getByText('Validate restart')).toBeVisible();
});

it('keeps failure cause, owner and next step visible while the board is collapsed', () => {
  const failed = { ...insight, outcome: 'failed' as const, failure: { code: 'turn_limit', detail: 'The agent reached its turn limit.',
    responsible: 'flow owner', next_action: 'Review remaining tasks before resuming.', exit_code: 1, report_available: true } };
  const { rerender } = render(view(failed));
  expect(screen.getByText(failed.failure.detail)).toBeVisible();
  expect(screen.getByText(failed.failure.next_action)).toBeVisible();
  expect(screen.getByRole('link', { name: 'Open run report and evidence' })).toHaveAttribute('href', '/activity?id=run%3A1');
  rerender(view(failed, true));
  expect(screen.queryByRole('region', { name: 'Run failure diagnosis' })).not.toBeInTheDocument();
});

it('does not invent counts for missing, restricted or truncated boards', () => {
  const { rerender } = render(view({ ...insight, task_board: null, board_access: 'restricted' }));
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('Task board'));
  expect(screen.getByText(/current product-team membership/)).toBeVisible();
  rerender(view({ ...insight, task_board: { ...insight.task_board!, text: insight.task_board!.text + '\n[Live preview truncated; see final transcript.]' } }));
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  expect(screen.getByText(/Task totals are unavailable/)).toBeVisible();
});
