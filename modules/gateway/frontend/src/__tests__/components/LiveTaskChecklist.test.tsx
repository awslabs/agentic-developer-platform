import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it } from 'vitest';
import { LiveTaskChecklist } from '@/components/LiveTaskChecklist';
import { parseLiveTasks } from '@/utils/liveTasks';

const board = '**▶ Plan `recover` — Keep conversations after disconnects**\n- ☑ `code` A-c1 — Save conversation\n- ▶ `test` A-t1 — Verify recovery\n**☐ Plan `cleanup` — Remove expired sessions**\n- ☐ `code` B-c1 — Add expiration';
it('shows collapsed readable steps, preserves expansion through updates, and opens a selected child', async () => {
  const tasks = parseLiveTasks(board)!;
  const { rerender, container } = render(<LiveTaskChecklist tasks={tasks} counts={new Map()} live />);
  const parents = container.querySelectorAll('details');
  expect(parents).toHaveLength(2);
  expect(parents[0].open).toBe(false);
  expect(screen.getByText('0 of 2 steps completed')).toBeInTheDocument();
  expect(screen.getByText('1 of 3 detailed tasks completed')).toBeInTheDocument();
  fireEvent.click(parents[0].querySelector('summary')!);
  await waitFor(() => expect(parents[0].open).toBe(true));
  rerender(<LiveTaskChecklist tasks={parseLiveTasks(board.replace('Verify recovery', 'Verify recovery — tests passed'))!} counts={new Map()} live />);
  expect(parents[0].open).toBe(true);
  fireEvent.click(parents[0].querySelector('summary')!);
  await waitFor(() => expect(parents[0].open).toBe(false));
  rerender(<LiveTaskChecklist tasks={tasks} selected="B-c1" counts={new Map()} live={false} />);
  await waitFor(() => expect(parents[1].open).toBe(true));
  expect(screen.queryByRole('button', { name: /View activity/ })).not.toBeInTheDocument();
});
