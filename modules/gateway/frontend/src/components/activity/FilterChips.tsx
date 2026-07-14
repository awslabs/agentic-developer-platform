/**
 * Active filter chips — visual indication of active filters with clear affordance.
 *
 * Issue #3768: Part of UX EPIC #3753, Wave 2.
 *
 * Renders a row of removable chips between the filter panel and the results
 * table on the Agent Activity page. Each active filter shows as a pill with
 * a label, value, and dismiss (×) button. A "Clear all" button appears when
 * any filter is active. The entire row is hidden when no filters are active.
 */

import { useCallback } from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ActiveFilter {
  /** Unique key identifying this filter (e.g. 'status', 'channel'). */
  key: string;
  /** Human-readable label (e.g. 'Status', 'Source'). */
  label: string;
  /** Human-readable display value (e.g. 'Failed', 'GitHub'). */
  displayValue: string;
}

export interface FilterChipsProps {
  /** List of currently active filters to display as chips. */
  filters: ActiveFilter[];
  /** Called when a single filter chip is dismissed. */
  onRemove: (key: string) => void;
  /** Called when the "Clear all" button is clicked. */
  onClearAll: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function FilterChips({ filters, onRemove, onClearAll }: FilterChipsProps) {
  if (filters.length === 0) {
    return null;
  }

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      data-testid="filter-chips"
      role="region"
      aria-label="Active filters"
    >
      {filters.map((filter) => (
        <FilterChip
          key={filter.key}
          filter={filter}
          onRemove={onRemove}
        />
      ))}
      <button
        type="button"
        onClick={onClearAll}
        className="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 underline underline-offset-2 transition-colors"
        data-testid="filter-chips-clear-all"
      >
        Clear all
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: individual chip
// ---------------------------------------------------------------------------

interface FilterChipProps {
  filter: ActiveFilter;
  onRemove: (key: string) => void;
}

function FilterChip({ filter, onRemove }: FilterChipProps) {
  const handleRemove = useCallback(() => {
    onRemove(filter.key);
  }, [onRemove, filter.key]);

  return (
    <span
      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-sm font-medium bg-blue-50 text-blue-800 dark:bg-blue-900/30 dark:text-blue-200 border border-blue-200 dark:border-blue-700"
      data-testid={`filter-chip-${filter.key}`}
    >
      <span className="text-blue-600 dark:text-blue-300">{filter.label}:</span>
      <span>{filter.displayValue}</span>
      <button
        type="button"
        onClick={handleRemove}
        className="ml-0.5 inline-flex items-center justify-center w-4 h-4 rounded-full text-blue-500 dark:text-blue-300 hover:bg-blue-200 dark:hover:bg-blue-800 hover:text-blue-700 dark:hover:text-blue-100 transition-colors"
        aria-label={`Remove ${filter.label} filter`}
        data-testid={`filter-chip-remove-${filter.key}`}
      >
        <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </span>
  );
}
