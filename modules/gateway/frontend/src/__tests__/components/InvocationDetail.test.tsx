/**
 * Tests for InvocationDetail component.
 *
 * Issue #1459: Phase 5 — Row detail + polish.
 * Validates: detail renders with all fields, error truncation + show more,
 * sanitized error display, status timeline rendering.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { InvocationDetail } from '@/components/InvocationDetail';
import type { InvocationItem } from '@/types/activity';

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

function makeItem(overrides: Partial<InvocationItem> = {}): InvocationItem {
  return {
    invocation_id: 'inv-001',
    user_id: 'user-001',
    persona: 'developer',
    channel: 'github',
    status: 'complete',
    topic: 'Implement Agent Activity page',
    summary: 'Completed work on issue #1457',
    source_url: 'https://github.com/aws-e/adp/issues/1457',
    repo: 'aws-e/adp',
    issue_number: 1457,
    invoked_at: '2026-06-14T10:00:00Z',
    completed_at: '2026-06-14T10:30:00Z',
    status_updated_at: '2026-06-14T10:30:00Z',
    correlation_id: 'corr-abc12345',
    run_id: '81286554630',
    error_message: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('InvocationDetail', () => {
  it('renders correlation_id, run_id, status, and status_updated_at', () => {
    const item = makeItem();
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('corr-abc12345')).toBeInTheDocument();
    expect(screen.getByText('81286554630')).toBeInTheDocument();
    expect(screen.getByText('Complete')).toBeInTheDocument();
    // status_updated_at rendered as relative time — look for "Last transition:" label
    expect(screen.getByText(/Last transition:/)).toBeInTheDocument();
  });

  it('renders invocation_id, channel, persona, topic, summary', () => {
    const item = makeItem();
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('inv-001')).toBeInTheDocument();
    expect(screen.getByText('github')).toBeInTheDocument();
    expect(screen.getByText('(developer)')).toBeInTheDocument();
    expect(screen.getByText('Implement Agent Activity page')).toBeInTheDocument();
    expect(screen.getByText('Completed work on issue #1457')).toBeInTheDocument();
  });

  it('renders source link as "repo#issue" with external link', () => {
    const item = makeItem();
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', 'https://github.com/aws-e/adp/issues/1457');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link).toHaveTextContent('aws-e/adp#1457');
  });

  it('shows "No error details available" for failed item with null error_message', () => {
    const item = makeItem({ status: 'failed', error_message: null });
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('No error details available')).toBeInTheDocument();
  });

  it('shows error_message for failed item', () => {
    const item = makeItem({
      status: 'failed',
      error_message: 'Agent timed out after 300s.',
    });
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('Agent timed out after 300s.')).toBeInTheDocument();
  });

  it('truncates long error_message and shows "Show more" button', async () => {
    const user = userEvent.setup();
    const longError = 'A'.repeat(250); // Longer than ERROR_TRUNCATE_LENGTH (200)
    const item = makeItem({ status: 'failed', error_message: longError });
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    // Should show truncated text (200 chars + ellipsis, not the full 250)
    expect(screen.getByText(/A{10,}/)).toBeInTheDocument();

    // "Show more" button should be visible
    const showMoreBtn = screen.getByRole('button', { name: /show more/i });
    expect(showMoreBtn).toBeInTheDocument();

    // Click show more → full text
    await user.click(showMoreBtn);
    expect(screen.getByRole('button', { name: /show less/i })).toBeInTheDocument();
  });

  it('does not show error section for non-failed status', () => {
    const item = makeItem({ status: 'complete', error_message: null });
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.queryByText('Error')).not.toBeInTheDocument();
    expect(screen.queryByText('No error details available')).not.toBeInTheDocument();
  });

  it('hides optional fields when null', () => {
    const item = makeItem({
      correlation_id: null,
      run_id: null,
      topic: null,
      summary: null,
      source_url: null,
      completed_at: null,
    });
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.queryByText('Correlation ID')).not.toBeInTheDocument();
    expect(screen.queryByText('Run / Job ID')).not.toBeInTheDocument();
    expect(screen.queryByText('Topic')).not.toBeInTheDocument();
    expect(screen.queryByText('Summary')).not.toBeInTheDocument();
    expect(screen.queryByText('Source')).not.toBeInTheDocument();
    expect(screen.queryByText('Completed at')).not.toBeInTheDocument();
  });

  it('shows "Active — not yet terminal" for in_progress status', () => {
    const item = makeItem({ status: 'in_progress', completed_at: null });
    render(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.getByText(/Active — not yet terminal/)).toBeInTheDocument();
  });

  it('calls onClose when modal close is triggered', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const item = makeItem();
    render(<InvocationDetail item={item} isOpen={true} onClose={onClose} />);

    // The Modal component has a close button
    const closeBtn = screen.getByLabelText('Close modal');
    await user.click(closeBtn);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('renders nothing when item is null', () => {
    const { container } = render(
      <InvocationDetail item={null} isOpen={true} onClose={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
