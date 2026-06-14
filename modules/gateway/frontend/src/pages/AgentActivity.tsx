/**
 * Agent Activity page — paginated list of agent invocations.
 *
 * Issue #1457: Phase 3 of Agent Activity rollout.
 * Issue #1459: Phase 5 — Row detail + polish (row click → detail modal,
 * improved empty states, a11y).
 *
 * Key behaviors:
 * - Default view = "mine" (GET /me/agent-invocations)
 * - Admin toggle switches to "all" (GET /admin/agent-invocations)
 * - Cursor-based pagination via last_key (not offset)
 * - Empty page with non-null last_key is NOT end of results
 * - Status rendering with glyphs
 * - source_url → "repo#issue ↗" link (or "(no external link)")
 * - Relative date with absolute on hover
 * - Row click → detail modal (Phase 5)
 */

import { useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Input, Select } from '@/components/ui';
import { TableSkeleton } from '@/components/LoadingScreen';
import { InvocationDetail } from '@/components/InvocationDetail';
import { usePermissions } from '@/hooks/usePermissions';
import { getMyInvocations, getAllInvocations } from '@/services/activity';
import { formatRelativeTime, formatDateTime } from '@/utils/format';
import type {
  InvocationItem,
  InvocationStatus,
  InvocationChannel,
  InvocationQueryParams,
} from '@/types/activity';

// ---------------------------------------------------------------------------
// Status rendering config
// ---------------------------------------------------------------------------

const STATUS_CONFIG: Record<InvocationStatus, { glyph: string; label: string; colorClass: string }> = {
  webhook_received: { glyph: '∘', label: 'Webhook recv', colorClass: 'text-gray-500 dark:text-gray-400' },
  in_progress: { glyph: '●', label: 'In progress', colorClass: 'text-blue-600 dark:text-blue-400' },
  complete: { glyph: '✓', label: 'Complete', colorClass: 'text-green-600 dark:text-green-400' },
  failed: { glyph: '✗', label: 'Failed', colorClass: 'text-red-600 dark:text-red-400' },
  rejected: { glyph: '✗', label: 'Rejected', colorClass: 'text-orange-600 dark:text-orange-400' },
  rate_limited: { glyph: '✗', label: 'Rate limited', colorClass: 'text-yellow-600 dark:text-yellow-400' },
  no_op: { glyph: '✗', label: 'No-op', colorClass: 'text-gray-500 dark:text-gray-400' },
};

function StatusBadge({ status }: { status: InvocationStatus }) {
  const config = STATUS_CONFIG[status] ?? STATUS_CONFIG.no_op;
  return (
    <span className={`inline-flex items-center gap-1 font-medium text-sm ${config.colorClass}`}>
      <span aria-hidden="true">{config.glyph}</span>
      <span>{config.label}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Source link rendering
// ---------------------------------------------------------------------------

function SourceLink({ item }: { item: InvocationItem }) {
  if (item.source_url) {
    // Extract "repo#issue_number" label from GitHub URLs
    const label =
      item.repo && item.issue_number
        ? `${item.repo}#${item.issue_number}`
        : item.source_url;
    return (
      <a
        href={item.source_url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 text-sm hover:underline"
      >
        {label} ↗
      </a>
    );
  }
  return <span className="text-gray-400 dark:text-gray-500 text-sm italic">(no external link)</span>;
}

// ---------------------------------------------------------------------------
// Filter options
// ---------------------------------------------------------------------------

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'webhook_received', label: 'Webhook recv' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'complete', label: 'Complete' },
  { value: 'failed', label: 'Failed' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'rate_limited', label: 'Rate limited' },
  { value: 'no_op', label: 'No-op' },
];

const CHANNEL_OPTIONS = [
  { value: '', label: 'All sources' },
  { value: 'github', label: 'GitHub' },
  { value: 'slack', label: 'Slack' },
  { value: 'api', label: 'API' },
  { value: 'manual', label: 'Manual' },
];

const PERSONA_OPTIONS = [
  { value: '', label: 'All personas' },
  { value: 'developer', label: 'Developer' },
  { value: 'architect', label: 'Architect' },
  { value: 'reviewer', label: 'Reviewer' },
  { value: 'ops', label: 'Ops' },
];

// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------

export default function AgentActivity() {
  const { isPlatformAdmin, isOrgAdmin } = usePermissions();
  const isAdmin = isPlatformAdmin() || isOrgAdmin();

  // Detail modal state (Phase 5)
  const [selectedItem, setSelectedItem] = useState<InvocationItem | null>(null);

  // View toggle: "mine" or "all" (admin only)
  const [viewMode, setViewMode] = useState<'mine' | 'all'>('mine');

  // Filters
  const [statusFilter, setStatusFilter] = useState('');
  const [channelFilter, setChannelFilter] = useState('');
  const [personaFilter, setPersonaFilter] = useState('');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');

  // Cursor-based pagination state
  const [cursorStack, setCursorStack] = useState<string[]>([]);
  const [currentCursor, setCurrentCursor] = useState<string | undefined>(undefined);

  // Build query params
  const queryParams: InvocationQueryParams = {
    status: (statusFilter || undefined) as InvocationStatus | undefined,
    channel: (channelFilter || undefined) as InvocationChannel | undefined,
    persona: personaFilter || undefined,
    start_date: startDate || undefined,
    end_date: endDate || undefined,
    limit: 20,
    last_key: currentCursor,
  };

  const fetchFn = viewMode === 'all' && isAdmin ? getAllInvocations : getMyInvocations;

  const {
    data,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ['agent-activity', viewMode, queryParams],
    queryFn: () => fetchFn(queryParams),
  });

  // Pagination handlers
  const handleNextPage = useCallback(() => {
    if (data?.last_key) {
      setCursorStack((prev) => [...prev, currentCursor ?? '']);
      setCurrentCursor(data.last_key);
    }
  }, [data?.last_key, currentCursor]);

  const handlePrevPage = useCallback(() => {
    setCursorStack((prev) => {
      const newStack = [...prev];
      const prevCursor = newStack.pop();
      setCurrentCursor(prevCursor || undefined);
      return newStack;
    });
  }, []);

  // Reset pagination when filters or view mode change
  const resetPagination = useCallback(() => {
    setCursorStack([]);
    setCurrentCursor(undefined);
  }, []);

  const handleViewModeChange = useCallback(
    (mode: 'mine' | 'all') => {
      setViewMode(mode);
      resetPagination();
    },
    [resetPagination],
  );

  const handleStatusChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      setStatusFilter(e.target.value);
      resetPagination();
    },
    [resetPagination],
  );

  const handleChannelChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      setChannelFilter(e.target.value);
      resetPagination();
    },
    [resetPagination],
  );

  const handlePersonaChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      setPersonaFilter(e.target.value);
      resetPagination();
    },
    [resetPagination],
  );

  const handleStartDateChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setStartDate(e.target.value);
      resetPagination();
    },
    [resetPagination],
  );

  const handleEndDateChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setEndDate(e.target.value);
      resetPagination();
    },
    [resetPagination],
  );

  // Row click → detail (Phase 5)
  const handleRowClick = useCallback((item: InvocationItem) => {
    setSelectedItem(item);
  }, []);

  const handleDetailClose = useCallback(() => {
    setSelectedItem(null);
  }, []);

  // Determine pagination state
  const hasNextPage = data?.last_key != null;
  const hasPrevPage = cursorStack.length > 0;
  const pageNumber = cursorStack.length + 1;

  // Determine which empty state to show (Phase 5 polish)
  const hasActiveFilters = !!(statusFilter || channelFilter || personaFilter || startDate || endDate);

  return (
    <div className="space-y-6">
      {/* Header + view toggle */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Agent Activity
          </h1>
          <p className="text-gray-500 dark:text-gray-400 mt-1">
            {viewMode === 'mine'
              ? 'Your agent invocations'
              : 'All agent invocations (admin)'}
          </p>
        </div>

        {isAdmin && (
          <div className="flex gap-1 bg-gray-100 dark:bg-gray-800 rounded-lg p-1" role="tablist">
            <button
              role="tab"
              aria-selected={viewMode === 'mine'}
              onClick={() => handleViewModeChange('mine')}
              className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                viewMode === 'mine'
                  ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                  : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
              }`}
            >
              Mine
            </button>
            <button
              role="tab"
              aria-selected={viewMode === 'all'}
              onClick={() => handleViewModeChange('all')}
              className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                viewMode === 'all'
                  ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                  : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
              }`}
            >
              All (Admin)
            </button>
          </div>
        )}
      </div>

      {/* Filters */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Status
            </label>
            <Select
              value={statusFilter}
              onChange={handleStatusChange}
              options={STATUS_OPTIONS}
              aria-label="Filter by status"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Source
            </label>
            <Select
              value={channelFilter}
              onChange={handleChannelChange}
              options={CHANNEL_OPTIONS}
              aria-label="Filter by source"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Persona
            </label>
            <Select
              value={personaFilter}
              onChange={handlePersonaChange}
              options={PERSONA_OPTIONS}
              aria-label="Filter by persona"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Start Date
            </label>
            <Input
              type="date"
              value={startDate}
              onChange={handleStartDateChange}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              End Date
            </label>
            <Input
              type="date"
              value={endDate}
              onChange={handleEndDateChange}
            />
          </div>
        </div>
      </div>

      {/* Error state */}
      {error && (
        <Alert variant="error" title="Failed to load agent activity">
          <div className="flex items-center justify-between">
            <span>
              {error instanceof Error ? error.message : 'An unexpected error occurred'}
            </span>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              Retry
            </Button>
          </div>
        </Alert>
      )}

      {/* Table */}
      {!error && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
          {isLoading ? (
            <TableSkeleton rows={10} />
          ) : data && data.items.length > 0 ? (
            <>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                  <thead className="bg-gray-50 dark:bg-gray-900">
                    <tr>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Date
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Event Source
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Status
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Topic
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Summary
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Link
                      </th>
                    </tr>
                  </thead>
                  <tbody className="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                    {data.items.map((item: InvocationItem) => (
                      <tr
                        key={item.invocation_id}
                        className="hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer focus:outline-none focus:bg-blue-50 dark:focus:bg-blue-900/20"
                        onClick={() => handleRowClick(item)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            handleRowClick(item);
                          }
                        }}
                        tabIndex={0}
                        role="button"
                        aria-label={`View details for invocation: ${item.topic || item.invocation_id}`}
                      >
                        <td
                          className="px-6 py-4 whitespace-nowrap text-sm text-gray-900 dark:text-white"
                          title={formatDateTime(item.invoked_at)}
                        >
                          {formatRelativeTime(item.invoked_at)}
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                          <span className="capitalize">{item.channel}</span>
                          {item.persona && (
                            <span className="ml-2 text-xs text-gray-400 dark:text-gray-500">
                              ({item.persona})
                            </span>
                          )}
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <StatusBadge status={item.status} />
                        </td>
                        <td className="px-6 py-4 text-sm text-gray-900 dark:text-white max-w-xs truncate">
                          {item.topic || <span className="text-gray-400 italic">—</span>}
                        </td>
                        <td className="px-6 py-4 text-sm text-gray-500 dark:text-gray-400 max-w-xs truncate">
                          {item.summary || <span className="text-gray-400 italic">—</span>}
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <SourceLink item={item} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Pagination */}
              <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
                <div className="text-sm text-gray-500 dark:text-gray-400">
                  Page {pageNumber}
                </div>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!hasPrevPage}
                    onClick={handlePrevPage}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!hasNextPage}
                    onClick={handleNextPage}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          ) : !isLoading && data && data.items.length === 0 && !hasNextPage ? (
            <div className="text-center py-12">
              {hasActiveFilters ? (
                <>
                  <p className="text-gray-500 dark:text-gray-400">
                    No matching results for the current filters
                  </p>
                  <p className="text-gray-400 dark:text-gray-500 text-sm mt-1">
                    Try adjusting your filters to see more results.
                  </p>
                </>
              ) : (
                <>
                  <p className="text-gray-500 dark:text-gray-400">
                    No agent activity yet
                  </p>
                  <p className="text-gray-400 dark:text-gray-500 text-sm mt-1">
                    Agent invocations will appear here once triggered.
                  </p>
                </>
              )}
            </div>
          ) : !isLoading && data && data.items.length === 0 && hasNextPage ? (
            /* Empty page with non-null last_key — more results exist beyond this cursor */
            <div className="text-center py-12">
              <p className="text-gray-500 dark:text-gray-400 mb-4">
                No matching results on this page. More results may exist.
              </p>
              <Button variant="outline" size="sm" onClick={handleNextPage}>
                Load next page
              </Button>
            </div>
          ) : null}
        </div>
      )}

      {/* Detail modal (Phase 5) */}
      <InvocationDetail
        item={selectedItem}
        isOpen={selectedItem !== null}
        onClose={handleDetailClose}
      />
    </div>
  );
}
