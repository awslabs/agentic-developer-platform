/**
 * Responsive card component for individual agent invocation items.
 *
 * Issue #3770: Part of UX EPIC #3753, Wave 3.
 *
 * Renders a single invocation as a card for narrow viewports (<1024px).
 * Primary info (Topic, Status, Time, Source, Cost) is always visible.
 * Secondary info (Trigger, Channel, Summary, Transcript) is in a
 * collapsible "More" section.
 */

import { useState, useCallback } from 'react';
import type { InvocationItem, InvocationStatus, TriggerKind } from '@/types/activity';
import { formatRelativeTime, formatDateTime } from '@/utils/format';

// ---------------------------------------------------------------------------
// Status rendering (mirrors AgentActivity.tsx STATUS_CONFIG)
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

const TRIGGER_CONFIG: Record<TriggerKind, { label: string; icon: string }> = {
  human: { label: 'Started by you', icon: '👤' },
  agent: { label: 'Agent-triggered', icon: '🤖' },
  bot: { label: 'Agent-initiated', icon: '⚙️' },
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ActivityCardProps {
  item: InvocationItem;
  onDetailClick: (item: InvocationItem) => void;
  onTranscriptClick: (invocationId: string) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ActivityCard({ item, onDetailClick, onTranscriptClick }: ActivityCardProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  const statusConfig = STATUS_CONFIG[item.status] ?? STATUS_CONFIG.no_op;
  const triggerKind: TriggerKind = item.trigger_kind || 'human';
  const triggerConfig = TRIGGER_CONFIG[triggerKind];

  const handleToggleExpand = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    setIsExpanded((prev) => !prev);
  }, []);

  const handleCardClick = useCallback(() => {
    onDetailClick(item);
  }, [onDetailClick, item]);

  const handleCardKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onDetailClick(item);
      }
    },
    [onDetailClick, item],
  );

  const handleTranscriptClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onTranscriptClick(item.invocation_id);
    },
    [onTranscriptClick, item.invocation_id],
  );

  // Format cost
  const formatCost = (cost: number | null | undefined): string => {
    if (cost === null || cost === undefined) return '—';
    if (cost === 0 && item.status === 'in_progress') return 'pending';
    return cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`;
  };

  // Source link label
  const sourceLabel =
    item.repo && item.issue_number
      ? `${item.repo}#${item.issue_number}`
      : item.source_url
        ? 'link'
        : null;

  return (
    <div
      className="border-b border-gray-200 dark:border-gray-700 last:border-b-0 px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer transition-colors"
      onClick={handleCardClick}
      onKeyDown={handleCardKeyDown}
      tabIndex={0}
      role="button"
      aria-label={`Run: ${item.topic || 'untitled'}, Status: ${statusConfig.label}, ${formatRelativeTime(item.invoked_at)}`}
      data-testid={`activity-card-${item.invocation_id}`}
    >
      {/* Primary row: Topic + Status badge */}
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1 min-w-0">
          {item.topic || <span className="italic text-gray-400">untitled</span>}
        </h3>
        <span
          className={`inline-flex items-center gap-1 text-xs font-medium whitespace-nowrap ${statusConfig.colorClass}`}
        >
          <span aria-hidden="true">{statusConfig.glyph}</span>
          <span>{statusConfig.label}</span>
        </span>
      </div>

      {/* Secondary row: Time, Source, Cost */}
      <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-500 dark:text-gray-400">
        <span title={formatDateTime(item.invoked_at)}>
          {formatRelativeTime(item.invoked_at)}
        </span>

        {sourceLabel && (
          <a
            href={item.source_url!}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 dark:text-blue-400 hover:underline"
            onClick={(e) => e.stopPropagation()}
          >
            {sourceLabel} &uarr;
          </a>
        )}

        <span className="ml-auto font-mono text-gray-700 dark:text-gray-300">
          {formatCost(item.total_cost_usd)}
        </span>
      </div>

      {/* Expand/collapse toggle */}
      <button
        type="button"
        onClick={handleToggleExpand}
        className="mt-2 text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 hover:underline"
        aria-expanded={isExpanded}
        aria-controls={`activity-card-details-${item.invocation_id}`}
        data-testid={`activity-card-toggle-${item.invocation_id}`}
      >
        {isExpanded ? 'Less' : 'More'}
      </button>

      {/* Collapsible secondary details */}
      {isExpanded && (
        <div
          id={`activity-card-details-${item.invocation_id}`}
          className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-700 space-y-1.5 text-xs text-gray-600 dark:text-gray-400"
          data-testid={`activity-card-details-${item.invocation_id}`}
        >
          {/* Trigger */}
          <div className="flex items-center gap-2">
            <span className="text-gray-500 dark:text-gray-500 w-16 flex-shrink-0">Trigger</span>
            <span>
              <span aria-hidden="true">{triggerConfig.icon}</span>{' '}
              {triggerConfig.label}
            </span>
          </div>

          {/* Channel / Persona */}
          <div className="flex items-center gap-2">
            <span className="text-gray-500 dark:text-gray-500 w-16 flex-shrink-0">Source</span>
            <span className="capitalize">{item.channel}</span>
            {item.persona && (
              <span className="text-gray-400 dark:text-gray-500">({item.persona})</span>
            )}
          </div>

          {/* Summary */}
          {item.summary && (
            <div className="flex items-start gap-2">
              <span className="text-gray-500 dark:text-gray-500 w-16 flex-shrink-0">Summary</span>
              <span className="text-gray-700 dark:text-gray-300 line-clamp-2">{item.summary}</span>
            </div>
          )}

          {/* Transcript link */}
          {item.transcript_key && (
            <div className="flex items-center gap-2">
              <span className="text-gray-500 dark:text-gray-500 w-16 flex-shrink-0">Log</span>
              <button
                type="button"
                onClick={handleTranscriptClick}
                className="text-blue-600 dark:text-blue-400 hover:underline"
              >
                View transcript
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
