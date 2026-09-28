/**
 * Tests for the LastUpdated freshness caption.
 *
 * Issue #4022: shared component behind the Agent Activity poll indicator.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { LastUpdated } from '@/components/LastUpdated';
import { formatDateTime } from '@/utils/format';

describe('LastUpdated', () => {
  it('renders a caption derived from dataUpdatedAt', () => {
    const updatedAt = Date.now() - 5 * 60 * 1000; // 5 minutes ago

    render(<LastUpdated dataUpdatedAt={updatedAt} isFetching={false} />);

    expect(screen.getByText(/Updated 5 minutes ago/)).toBeInTheDocument();
  });

  it('exposes the absolute timestamp on hover', () => {
    const updatedAt = Date.now() - 60 * 1000;

    render(<LastUpdated dataUpdatedAt={updatedAt} isFetching={false} />);

    expect(screen.getByText(/^Updated /)).toHaveAttribute(
      'title',
      formatDateTime(new Date(updatedAt)),
    );
  });

  it('shows the spinner only while fetching', () => {
    const { rerender } = render(
      <LastUpdated dataUpdatedAt={Date.now()} isFetching={false} />,
    );
    expect(screen.queryByRole('status')).not.toBeInTheDocument();

    rerender(<LastUpdated dataUpdatedAt={Date.now()} isFetching={true} />);
    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('renders no caption before the first successful fetch', () => {
    // dataUpdatedAt is 0 until a query resolves — a "Updated 56 years ago"
    // caption from the epoch would be worse than showing nothing.
    render(<LastUpdated dataUpdatedAt={0} isFetching={true} />);

    expect(screen.queryByText(/Updated/)).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toBeInTheDocument();
  });
});
