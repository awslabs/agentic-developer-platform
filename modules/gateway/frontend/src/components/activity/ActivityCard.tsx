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

import { LiveStreamLink } from './LiveStreamLink';
import { useState, useCallback } from 'react';
import type { InvocationItem, TriggerKind } from '@/types/activity';
import { formatRelativeTime, formatDateTime } from '@/utils/format';
// Issue #4207: the local formatCost closed over item.status; formatRunCost takes
// it as an argument so the same no-data/pending policy is shared, not copied.
import { formatRunCost } from '@/utils/cost';
// Issue #4400: this card carried the second of three copies of STATUS_CONFIG.
// `compact` is the card's variant — the same short labels the table uses.
import { describeStatus } from '@/utils/status';
import { describeSkipReason, isNonRunStatus } from '@/utils/skipReason';
import { LivenessBadge } from '@/components/activity/LivenessBadge';

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
  liveStreamEnabled?: boolean;
  onDetailClick: (item: InvocationItem) => void;
  onTranscriptClick: (invocationId: string) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ActivityCard({ item, onDetailClick, onTranscriptClick, liveStreamEnabled }: ActivityCardProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  const statusConfig = describeStatus(item.status);
  // Issue #4020: on a non-run the reason is the only informative thing on the
  // card, so it goes in the always-visible area rather than behind "More".
  const skipReasonText = isNonRunStatus(item.status) ? describeSkipReason(item.skip_reason) : null;
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

  // Source link label
  const sourceLabel =
    item.repo && item.issue_number
      ? `${item.repo}#${item.issue_number}`
      : item.source_url
        ? 'link'
        : null;

  return (
    <div
      className="activity-run-card border-b border-gray-200 dark:border-gray-700 last:border-b-0 px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer transition-colors"
      onClick={handleCardClick}
      onKeyDown={handleCardKeyDown}
      tabIndex={0}
      role="button"
      aria-label={`Run: ${item.topic || 'untitled'}, Status: ${statusConfig.label}${
        skipReasonText ? ` (${skipReasonText})` : ''
      }${
        /* Issue #4176: an unverifiable run must be announced as such — the
           badge alone would leave a screen-reader user reading it as healthy. */
        item.liveness === 'unverifiable' ? ', Liveness: unverifiable' : ''
      }, ${formatRelativeTime(item.invoked_at)}`}
      data-testid={`activity-card-${item.invocation_id}`}
    >
      {/* Primary row: Topic + Status badge */}
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1 min-w-0">
          {item.topic || <span className="italic text-gray-400">untitled</span>}
        </h3>
        <div className="flex flex-col items-end gap-1">
          <span
            className={`activity-status-badge inline-flex items-center gap-1 text-xs font-medium whitespace-nowrap ${statusConfig.colorClass}`}
            data-status={item.status}
          >
            <span aria-hidden="true">{statusConfig.glyph}</span>
            <span>{statusConfig.label}</span>
          </span>
          {/* Issue #4176: attentionOnly — a card is narrow, so only the verdict
              that changes the operator's reading of the status earns the space. */}
          <LivenessBadge
            verdict={item.liveness}
            attentionOnly
            testIdSuffix={item.invocation_id}
          />
        </div>
      </div>

      {/* Issue #4020: reason line for non-runs — replaces the card's previously
          unexplained "✗ No-op" badge with the actual cause. */}
      {skipReasonText && (
        <p
          className="mt-1 text-xs text-gray-500 dark:text-gray-400"
          data-testid={`activity-card-skip-reason-${item.invocation_id}`}
        >
          {skipReasonText}
        </p>
      )}

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
          {formatRunCost(item.total_cost_usd, item.status)}
        </span>
      </div>

      <LiveStreamLink enabled={liveStreamEnabled} status={item.status} onOpen={() => onDetailClick(item)} />

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
