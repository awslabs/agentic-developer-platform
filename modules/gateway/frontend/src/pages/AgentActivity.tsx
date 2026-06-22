/**
 * Agent Activity page — paginated list of agent invocations.
 *
 * Issue #1457: Phase 3 of Agent Activity rollout.
 * Issue #1461: Phase 6 — lineage (trigger badge + chain view).
 *
 * Key behaviors:
 * - Default view = "mine" (GET /me/agent-invocations)
 * - Admin toggle switches to "all" (GET /admin/agent-invocations)
 * - Cursor-based pagination via last_key (not offset)
 * - Empty page with non-null last_key is NOT end of results
 * - Status rendering with glyphs
 * - source_url → "repo#issue ↗" link (or "(no external link)")
 * - Relative date with absolute on hover
 * - Trigger badge: human/agent/bot indicator with parent link
 * - Chain view: click correlation chain to see indented tree
 */

import { useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Input, Select } from '@/components/ui';
import { TableSkeleton } from '@/components/LoadingScreen';
import InvocationChain from '@/components/InvocationChain';
import { InvocationDetail } from '@/components/InvocationDetail';
import { usePermissions } from '@/hooks/usePermissions';
import { getMyInvocations, getMyChains, getAllInvocations } from '@/services/activity';
import { formatRelativeTime, formatDateTime } from '@/utils/format';
import type {
  InvocationItem,
  InvocationStatus,
  InvocationChannel,
  InvocationQueryParams,
  TriggerKind,
  ChainSummary,
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
// Trigger badge rendering (Phase 6)
// ---------------------------------------------------------------------------

const TRIGGER_CONFIG: Record<TriggerKind, { label: string; colorClass: string; icon: string }> = {
  human: { label: 'Started by you', colorClass: 'text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20', icon: '👤' },
  agent: { label: 'Agent-triggered', colorClass: 'text-purple-700 dark:text-purple-400 bg-purple-50 dark:bg-purple-900/20', icon: '🤖' },
  bot: { label: 'Agent-initiated', colorClass: 'text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20', icon: '⚙️' },
};

interface TriggerBadgeProps {
  item: InvocationItem;
  onViewChain?: (correlationId: string) => void;
}

function TriggerBadge({ item, onViewChain }: TriggerBadgeProps) {
  const triggerKind: TriggerKind = item.trigger_kind || 'human';
  const config = TRIGGER_CONFIG[triggerKind];

  return (
    <div className="flex flex-col gap-1">
      <span
        className={`inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full ${config.colorClass}`}
        data-testid={`trigger-badge-${triggerKind}`}
      >
        <span aria-hidden="true">{config.icon}</span>
        <span>{config.label}</span>
      </span>
      {/* Show parent link for agent-triggered runs */}
      {triggerKind === 'agent' && item.triggered_by_topic && (
        <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[150px]">
          ← {item.triggered_by_topic}
        </span>
      )}
      {/* Chain link */}
      {item.correlation_id && onViewChain && (
        <button
          onClick={(e) => {
            e.stopPropagation(); // don't trigger the row's detail-open click
            onViewChain(item.correlation_id!);
          }}
          className="text-xs text-blue-600 dark:text-blue-400 hover:underline text-left"
          title="View invocation chain"
        >
          View chain
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cost rendering (Issue #1616)
// ---------------------------------------------------------------------------

function CostBadge({ item }: { item: InvocationItem }) {
  if (item.total_cost_usd === null || item.total_cost_usd === undefined) {
    // Not metered (non-gateway-mode run) or no usage_logs rows yet
    return <span className="text-gray-400 dark:text-gray-500 text-sm">—</span>;
  }
  if (item.total_cost_usd === 0 && item.status === 'in_progress') {
    // Run in progress, cost not yet backfilled
    return <span className="text-gray-400 dark:text-gray-500 text-sm italic">pending</span>;
  }
  // Format cost: show 4 decimal places for small amounts, 2 for larger
  const formatted = item.total_cost_usd < 0.01
    ? `$${item.total_cost_usd.toFixed(4)}`
    : `$${item.total_cost_usd.toFixed(2)}`;
  return (
    <span className="text-sm text-gray-900 dark:text-white font-mono" title={`${item.call_count ?? 0} calls, ${item.total_tokens ?? 0} tokens`}>
      {formatted}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Chain cost badge (aggregate for a chain)
// ---------------------------------------------------------------------------

function ChainCostBadge({ cost }: { cost: number | null }) {
  if (cost === null || cost === undefined) {
    return <span className="text-gray-400 dark:text-gray-500 text-sm">—</span>;
  }
  const formatted = cost < 0.01
    ? `$${cost.toFixed(4)}`
    : `$${cost.toFixed(2)}`;
  return (
    <span className="text-sm text-gray-900 dark:text-white font-mono">
      {formatted}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Chain row component (Issue #1662)
// ---------------------------------------------------------------------------

interface ChainRowProps {
  chain: ChainSummary;
  isExpanded: boolean;
  onToggle: () => void;
  onDetailClick: (item: InvocationItem) => void;
  onNodeClick: (invocationId: string) => void;
}

function ChainRow({ chain, isExpanded, onToggle, onDetailClick, onNodeClick }: ChainRowProps) {
  const { root } = chain;
  const statusConfig = STATUS_CONFIG[root.status as InvocationStatus] ?? STATUS_CONFIG.no_op;
  const isSingleton = chain.descendant_count === 0;

  return (
    <div className="border-b border-gray-200 dark:border-gray-700 last:border-b-0">
      {/* Chain row header */}
      <div
        className="flex items-center gap-3 px-6 py-4 hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer"
        onClick={() => {
          if (isSingleton) {
            onDetailClick(root);
          } else {
            onToggle();
          }
        }}
      >
        {/* Expand/collapse arrow (only for multi-run chains) */}
        <span className="w-4 text-gray-400 dark:text-gray-500 text-sm flex-shrink-0">
          {!isSingleton && (
            <span aria-hidden="true">{isExpanded ? '▼' : '▶'}</span>
          )}
        </span>

        {/* Topic / issue link */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-900 dark:text-white truncate font-medium">
              {root.topic || <span className="italic text-gray-400">untitled</span>}
            </span>
          </div>
          <div className="flex items-center gap-2 mt-0.5">
            {!isSingleton && (
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {chain.descendant_count} run{chain.descendant_count !== 1 ? 's' : ''}
              </span>
            )}
            {root.source_url && (
              <a
                href={root.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
                onClick={(e) => e.stopPropagation()}
              >
                {root.repo && root.issue_number
                  ? `${root.repo}#${root.issue_number}`
                  : 'link'} ↗
              </a>
            )}
          </div>
        </div>

        {/* Persona */}
        <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-2 py-0.5 rounded flex-shrink-0">
          {root.persona || root.channel || 'unknown'}
        </span>

        {/* Status */}
        <span className={`flex-shrink-0 ${statusConfig.colorClass}`}>
          <span className="text-sm font-medium">{statusConfig.glyph}</span>
          <span className="text-xs ml-1">{statusConfig.label}</span>
        </span>

        {/* Chain total cost */}
        <div className="flex-shrink-0 text-right min-w-[60px]">
          <ChainCostBadge cost={chain.chain_total_cost_usd} />
        </div>

        {/* Time */}
        <span
          className="text-xs text-gray-400 dark:text-gray-500 whitespace-nowrap flex-shrink-0"
          title={formatDateTime(root.invoked_at)}
        >
          {formatRelativeTime(root.invoked_at)}
        </span>
      </div>

      {/* Expanded descendants */}
      {isExpanded && !isSingleton && (
        <div className="bg-gray-50 dark:bg-gray-900/50 border-t border-gray-100 dark:border-gray-700 px-6 py-2">
          {chain.descendants.map((desc) => {
            const descStatus = STATUS_CONFIG[desc.status as InvocationStatus] ?? STATUS_CONFIG.no_op;
            return (
              <button
                key={desc.invocation_id}
                type="button"
                onClick={() => onNodeClick(desc.invocation_id)}
                className="w-full text-left flex items-center gap-2 py-2 px-3 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors"
              >
                <span className="text-gray-300 dark:text-gray-600 text-sm" aria-hidden="true">
                  └─
                </span>
                <span className={`${descStatus.colorClass} text-sm font-medium`} aria-hidden="true">
                  {descStatus.glyph}
                </span>
                <span className="text-sm text-gray-900 dark:text-white truncate flex-1">
                  {desc.topic || <span className="italic text-gray-400">untitled</span>}
                </span>
                {desc.total_cost_usd != null && (
                  <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded font-mono">
                    ${desc.total_cost_usd < 0.01 ? desc.total_cost_usd.toFixed(4) : desc.total_cost_usd.toFixed(2)}
                  </span>
                )}
                {desc.persona && (
                  <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-2 py-0.5 rounded">
                    {desc.persona}
                  </span>
                )}
                <span
                  className="text-xs text-gray-400 dark:text-gray-500 whitespace-nowrap"
                  title={formatDateTime(desc.invoked_at)}
                >
                  {formatRelativeTime(desc.invoked_at)}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
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

  // View toggle: "mine" or "all" (admin only)
  const [viewMode, setViewMode] = useState<'mine' | 'all'>('mine');

  // Issue #1662: Group-by toggle: "run" (flat) or "chain" (grouped)
  const [groupBy, setGroupBy] = useState<'run' | 'chain'>('chain');

  // Chain row expand/collapse state (issue #1662)
  const [expandedChains, setExpandedChains] = useState<Set<string>>(new Set());

  // Chain view state
  const [activeChainId, setActiveChainId] = useState<string | null>(null);
  const [chainHighlightId, setChainHighlightId] = useState<string | undefined>(undefined);

  // Detail modal state (#1653): the selected run to show in the detail panel
  const [detailItem, setDetailItem] = useState<InvocationItem | null>(null);

  // Filters
  const [statusFilter, setStatusFilter] = useState('');
  const [channelFilter, setChannelFilter] = useState('');
  const [personaFilter, setPersonaFilter] = useState('');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');

  // Issue #1658: "Show all events" toggle — when off (default), non-triggering
  // statuses (no_op, webhook_received) are hidden from the board.
  const [showAllEvents, setShowAllEvents] = useState(false);

  // Cursor-based pagination state
  const [cursorStack, setCursorStack] = useState<string[]>([]);
  const [currentCursor, setCurrentCursor] = useState<string | undefined>(undefined);

  // Build query params
  // Issue #1658: An explicit status filter takes precedence — if the user
  // selects a specific status (including no_op), send include_non_triggering=true
  // so the backend doesn't exclude it. Otherwise, respect the toggle.
  const queryParams: InvocationQueryParams = {
    status: (statusFilter || undefined) as InvocationStatus | undefined,
    channel: (channelFilter || undefined) as InvocationChannel | undefined,
    persona: personaFilter || undefined,
    start_date: startDate || undefined,
    end_date: endDate || undefined,
    limit: 20,
    last_key: currentCursor,
    include_non_triggering: (statusFilter || showAllEvents) ? true : undefined,
  };

  // Issue #1662: Choose fetch function based on view mode + group-by
  const isChainView = groupBy === 'chain' && viewMode === 'mine';
  const flatFetchFn = viewMode === 'all' && isAdmin ? getAllInvocations : getMyInvocations;

  // Flat list query (active when NOT in chain view)
  const flatQuery = useQuery({
    queryKey: ['agent-activity', viewMode, 'runs', queryParams],
    queryFn: () => flatFetchFn(queryParams),
    enabled: !isChainView,
  });

  // Chain list query (active when in chain view)
  const chainQuery = useQuery({
    queryKey: ['agent-activity', viewMode, 'chains', queryParams],
    queryFn: () => getMyChains(queryParams),
    enabled: isChainView,
  });

  // Unified state from whichever query is active
  const data = isChainView ? chainQuery.data : flatQuery.data;
  const isLoading = isChainView ? chainQuery.isLoading : flatQuery.isLoading;
  const error = isChainView ? chainQuery.error : flatQuery.error;
  const refetch = isChainView ? chainQuery.refetch : flatQuery.refetch;

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

  // Issue #1658: "Show all events" toggle handler
  const handleShowAllEventsChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setShowAllEvents(e.target.checked);
      resetPagination();
    },
    [resetPagination],
  );

  // Issue #1662: Group-by toggle handler
  const handleGroupByChange = useCallback(
    (mode: 'run' | 'chain') => {
      setGroupBy(mode);
      setExpandedChains(new Set());
      resetPagination();
    },
    [resetPagination],
  );

  // Issue #1662: Chain expand/collapse handler
  const toggleChainExpand = useCallback((chainId: string) => {
    setExpandedChains((prev) => {
      const next = new Set(prev);
      if (next.has(chainId)) {
        next.delete(chainId);
      } else {
        next.add(chainId);
      }
      return next;
    });
  }, []);

  // Chain view handler
  const handleViewChain = useCallback((correlationId: string, invocationId?: string) => {
    setActiveChainId(correlationId);
    setChainHighlightId(invocationId);
  }, []);

  const handleCloseChain = useCallback(() => {
    setActiveChainId(null);
    setChainHighlightId(undefined);
  }, []);

  // Determine pagination state (works for both flat and chain responses)
  const responseLastKey = data?.last_key ?? null;
  const hasNextPage = responseLastKey != null;
  const hasPrevPage = cursorStack.length > 0;
  const pageNumber = cursorStack.length + 1;

  // Issue #1662: Derive typed data from whichever query is active
  const chainData = isChainView ? chainQuery.data : undefined;
  const flatData = !isChainView ? flatQuery.data : undefined;

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

        <div className="flex items-center gap-3">
          {/* Issue #1662: Group-by toggle (by run / by chain) */}
          {viewMode === 'mine' && (
            <div className="flex gap-1 bg-gray-100 dark:bg-gray-800 rounded-lg p-1" role="tablist" aria-label="Group by">
              <button
                role="tab"
                aria-selected={groupBy === 'chain'}
                onClick={() => handleGroupByChange('chain')}
                className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                  groupBy === 'chain'
                    ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                    : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                }`}
              >
                By chain
              </button>
              <button
                role="tab"
                aria-selected={groupBy === 'run'}
                onClick={() => handleGroupByChange('run')}
                className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                  groupBy === 'run'
                    ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                    : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                }`}
              >
                By run
              </button>
            </div>
          )}

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
        {/* Issue #1658: Show all events toggle */}
        <div className="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700">
          <label className="inline-flex items-center gap-2 cursor-pointer text-sm text-gray-700 dark:text-gray-300">
            <input
              type="checkbox"
              checked={showAllEvents}
              onChange={handleShowAllEventsChange}
              className="rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-blue-500"
            />
            Show all events
            <span className="text-gray-500 dark:text-gray-400 text-xs">
              (include no-op &amp; webhook-received)
            </span>
          </label>
        </div>
      </div>

      {/* Chain view (shown when a chain is selected) */}
      {activeChainId && (
        <InvocationChain
          correlationId={activeChainId}
          isAdmin={viewMode === 'all' && isAdmin}
          highlightInvocationId={chainHighlightId}
          onClose={handleCloseChain}
          onNodeClick={(invocationId) => {
            // Open the detail modal for a clicked chain node. The node may not
            // be on the current list page, so find it in the loaded items;
            // if absent, the modal fetches by id is out of scope here — fall
            // back to highlighting it in the chain.
            const found = flatData?.items.find((i: InvocationItem) => i.invocation_id === invocationId);
            if (found) setDetailItem(found);
            else setChainHighlightId(invocationId);
          }}
        />
      )}

      {/* Run detail modal (#1653) — opened by clicking a list row or chain node */}
      <InvocationDetail
        item={detailItem}
        isOpen={detailItem !== null}
        onClose={() => setDetailItem(null)}
      />

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

      {/* Table / Chain list */}
      {!error && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
          {isLoading ? (
            <TableSkeleton rows={10} />
          ) : isChainView && chainData && chainData.chains.length > 0 ? (
            /* Issue #1662: Chain-grouped view */
            <>
              <div>
                {chainData.chains.map((chain: ChainSummary) => (
                  <ChainRow
                    key={chain.chain_id}
                    chain={chain}
                    isExpanded={expandedChains.has(chain.chain_id)}
                    onToggle={() => toggleChainExpand(chain.chain_id)}
                    onDetailClick={(item) => setDetailItem(item)}
                    onNodeClick={(invocationId) => {
                      // Try to find the root or descendant to show detail
                      const chainMatch = chainData.chains.find(
                        (c: ChainSummary) => c.root.invocation_id === invocationId ||
                          c.descendants.some((d) => d.invocation_id === invocationId)
                      );
                      if (chainMatch?.root.invocation_id === invocationId) {
                        setDetailItem(chainMatch.root);
                      } else {
                        // For descendants, open the chain view to show context
                        const parentChain = chainData.chains.find(
                          (c: ChainSummary) => c.descendants.some((d) => d.invocation_id === invocationId)
                        );
                        if (parentChain) {
                          handleViewChain(parentChain.chain_id, invocationId);
                        }
                      }
                    }}
                  />
                ))}
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
          ) : !isChainView && flatData && flatData.items.length > 0 ? (
            /* Flat list view (existing) */
            <>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                  <thead className="bg-gray-50 dark:bg-gray-900">
                    <tr>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Date
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Trigger
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
                      <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Cost
                      </th>
                      <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                        Link
                      </th>
                    </tr>
                  </thead>
                  <tbody className="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                    {flatData.items.map((item: InvocationItem) => (
                      <tr
                        key={item.invocation_id}
                        className="hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer"
                        onClick={() => setDetailItem(item)}
                      >
                        <td
                          className="px-6 py-4 whitespace-nowrap text-sm text-gray-900 dark:text-white"
                          title={formatDateTime(item.invoked_at)}
                        >
                          {formatRelativeTime(item.invoked_at)}
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <TriggerBadge
                            item={item}
                            onViewChain={(cid) => handleViewChain(cid, item.invocation_id)}
                          />
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
                        <td className="px-6 py-4 whitespace-nowrap text-right">
                          <CostBadge item={item} />
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
          ) : !isLoading && data && !hasNextPage && (
            (isChainView && chainData && chainData.chains.length === 0) ||
            (!isChainView && flatData && flatData.items.length === 0)
          ) ? (
            <div className="text-center py-12">
              <p className="text-gray-500 dark:text-gray-400">
                No agent activity yet
              </p>
            </div>
          ) : !isLoading && data && hasNextPage && (
            (isChainView && chainData && chainData.chains.length === 0) ||
            (!isChainView && flatData && flatData.items.length === 0)
          ) ? (
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
    </div>
  );
}
