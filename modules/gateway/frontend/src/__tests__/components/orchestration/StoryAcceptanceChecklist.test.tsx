import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StoryAcceptanceChecklist } from '@/components/orchestration/StoryAcceptanceChecklist';
import type { StoryAcceptance } from '@/types/orchestration';

const acceptance: StoryAcceptance = {
  status: 'in_progress', detail: 'Merge is complete; engine acceptance is still in progress.',
  next_action: 'Observe deployment', next_check_at: '2026-11-02T12:00:00Z',
  checks: [
    { id: 'merge', label: 'Merge verified', status: 'passed', detail: 'Merged revision verified.', evidence_id: 'ledger-record', observed_at: '2026-11-02T11:00:00Z' },
    { id: 'deployment', label: 'Deployment and runtime verified', status: 'running', detail: 'Observing deployment.' },
    { id: 'completion', label: 'Story delivery completed', status: 'pending', detail: 'Waiting for required checks.' },
  ],
};

describe('Story acceptance', () => {
  it('keeps passed checks visible, distinguishes merge from completion, and exposes evidence on demand', () => {
    render(<StoryAcceptanceChecklist acceptance={acceptance} />);
    const section = within(screen.getByRole('region', { name: 'Engine acceptance' }));
    expect(section.getByRole('status')).toHaveTextContent('1 of 3 checks complete');
    expect(section.getByText('Merge verified')).toBeVisible();
    expect(section.getByText('Passed')).toBeVisible();
    expect(section.getByText('Running')).toBeVisible();
    expect(section.getByText('Pending')).toBeVisible();
    expect(section.getByText('Observe deployment')).toBeVisible();
    expect(section.getByText('ledger-record')).not.toBeVisible();
    fireEvent.click(section.getByText('Merge verified'));
    expect(section.getByText('ledger-record')).toBeVisible();
  });

  it('updates the same list as observations arrive and retains the completed result', () => {
    const { rerender } = render(<StoryAcceptanceChecklist acceptance={acceptance} />);
    rerender(<StoryAcceptanceChecklist acceptance={{ ...acceptance, status: 'complete', next_action: null, next_check_at: null,
      detail: 'Story delivery accepted.', checks: acceptance.checks.map(check => ({ ...check, status: 'passed' })) }} />);
    expect(screen.getByRole('status')).toHaveTextContent('3 of 3 checks complete');
    expect(screen.getAllByText('Passed')).toHaveLength(3);
    expect(screen.queryByText('Running')).not.toBeInTheDocument();
    expect(screen.queryByText('Observe deployment')).not.toBeInTheDocument();
  });

  it('does not fabricate a checklist for historical acceptance', () => {
    render(<StoryAcceptanceChecklist acceptance={{ status: 'history_unavailable', checks: [], detail: 'Accepted previously; detailed checks were not recorded.' }} />);
    expect(screen.getByText(/Accepted previously/)).toBeVisible();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
