/**
 * Tests for the FilterChips component.
 *
 * Issue #3768: Active filter indication and clear affordance.
 * Validates: chip rendering for active filters, individual dismiss,
 * "Clear all" action, and hidden state when no filters are active.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FilterChips, type ActiveFilter } from '@/components/activity/FilterChips';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const mockFilters: ActiveFilter[] = [
  { key: 'status', label: 'Status', displayValue: 'Failed' },
  { key: 'startDate', label: 'Since', displayValue: '2026-07-12' },
];

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('FilterChips', () => {
  it('renders nothing when no filters are active', () => {
    const { container } = render(
      <FilterChips filters={[]} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders chips for each active filter', () => {
    render(
      <FilterChips filters={mockFilters} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    );

    // Both chips should be visible
    expect(screen.getByTestId('filter-chip-status')).toBeInTheDocument();
    expect(screen.getByTestId('filter-chip-startDate')).toBeInTheDocument();

    // Labels and values displayed
    expect(screen.getByText('Status:')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText('Since:')).toBeInTheDocument();
    expect(screen.getByText('2026-07-12')).toBeInTheDocument();
  });

  it('shows "Clear all" button when filters are active', () => {
    render(
      <FilterChips filters={mockFilters} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    );

    expect(screen.getByTestId('filter-chips-clear-all')).toBeInTheDocument();
    expect(screen.getByText('Clear all')).toBeInTheDocument();
  });

  it('calls onRemove with the filter key when × is clicked', async () => {
    const user = userEvent.setup();
    const onRemove = vi.fn();

    render(
      <FilterChips filters={mockFilters} onRemove={onRemove} onClearAll={vi.fn()} />,
    );

    // Click dismiss on the status chip
    const removeBtn = screen.getByTestId('filter-chip-remove-status');
    await user.click(removeBtn);

    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(onRemove).toHaveBeenCalledWith('status');
  });

  it('calls onClearAll when "Clear all" is clicked', async () => {
    const user = userEvent.setup();
    const onClearAll = vi.fn();

    render(
      <FilterChips filters={mockFilters} onRemove={vi.fn()} onClearAll={onClearAll} />,
    );

    await user.click(screen.getByTestId('filter-chips-clear-all'));

    expect(onClearAll).toHaveBeenCalledTimes(1);
  });

  it('renders correct aria-label on dismiss buttons', () => {
    render(
      <FilterChips filters={mockFilters} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    );

    expect(screen.getByLabelText('Remove Status filter')).toBeInTheDocument();
    expect(screen.getByLabelText('Remove Since filter')).toBeInTheDocument();
  });

  it('renders all five filter types when all are active', () => {
    const allFilters: ActiveFilter[] = [
      { key: 'status', label: 'Status', displayValue: 'In progress' },
      { key: 'channel', label: 'Source', displayValue: 'GitHub' },
      { key: 'persona', label: 'Persona', displayValue: 'Developer' },
      { key: 'startDate', label: 'Since', displayValue: '2026-07-01' },
      { key: 'endDate', label: 'Until', displayValue: '2026-07-12' },
    ];

    render(
      <FilterChips filters={allFilters} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    );

    expect(screen.getByTestId('filter-chip-status')).toBeInTheDocument();
    expect(screen.getByTestId('filter-chip-channel')).toBeInTheDocument();
    expect(screen.getByTestId('filter-chip-persona')).toBeInTheDocument();
    expect(screen.getByTestId('filter-chip-startDate')).toBeInTheDocument();
    expect(screen.getByTestId('filter-chip-endDate')).toBeInTheDocument();
  });

  it('has correct region role and aria-label for accessibility', () => {
    render(
      <FilterChips filters={mockFilters} onRemove={vi.fn()} onClearAll={vi.fn()} />,
    );

    expect(screen.getByRole('region', { name: 'Active filters' })).toBeInTheDocument();
  });
});
