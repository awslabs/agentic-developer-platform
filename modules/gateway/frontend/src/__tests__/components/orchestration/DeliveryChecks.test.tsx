import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DeliveryChecks } from '@/components/orchestration/DeliveryChecks';
import type { ValidationCheck } from '@/types/orchestration';

const failure: ValidationCheck = {
  code: 'delivery_cleanup_failed', status: 'failed', label: 'Cleanup',
  detail: 'Required cleanup has not succeeded.', next_action: 'Complete cleanup and publish the result.',
  observed_at: '2026-10-22T12:00:00Z', technical_detail: 'Detailed evidence reference',
};
const passed: ValidationCheck = { code: 'runtime_verified', status: 'passed', label: 'Deployment verification', detail: 'Release evidence verified.' };

describe('Delivery checks', () => {
  it('shows the reason and next step without expanding technical details', () => {
    render(<DeliveryChecks checks={[passed, failure]} />);
    const attention = within(screen.getByRole('list', { name: 'Checks needing attention' }));
    expect(attention.getByText('Needs attention · Cleanup')).toBeVisible();
    expect(attention.getByText(failure.detail)).toBeVisible();
    expect(attention.getByText(failure.next_action!)).toBeVisible();
    expect(screen.getByText(failure.code)).not.toBeVisible();
    expect(screen.getByText(failure.technical_detail!)).not.toBeVisible();
    expect(screen.getByText(passed.detail)).not.toBeVisible();
    fireEvent.click(screen.getByText('Check details · 1 passed'));
    expect(screen.getByText(failure.code)).toBeVisible();
    expect(screen.getByText(failure.technical_detail!)).toBeVisible();
    expect(screen.getByText(passed.detail)).toBeVisible();
  });

  it('distinguishes a normal wait from a failed check and clears old failures', () => {
    const { rerender } = render(<DeliveryChecks checks={[{ ...failure, status: 'waiting' }]} />);
    expect(screen.getByText('Waiting · Cleanup')).toBeVisible();
    expect(screen.queryByText(/Needs attention/)).not.toBeInTheDocument();
    rerender(<DeliveryChecks checks={[passed]} />);
    expect(screen.queryByRole('list', { name: 'Checks needing attention' })).not.toBeInTheDocument();
    expect(screen.queryByText(failure.detail)).not.toBeInTheDocument();
    expect(screen.getByText('Check details · 1 passed')).toBeVisible();
  });

  it('does not imply success when no checks were recorded', () => {
    const { container } = render(<DeliveryChecks checks={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
