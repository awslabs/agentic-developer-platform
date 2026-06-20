/**
 * InvocationDetail — modal rendering full detail for a single agent invocation.
 *
 * Issue #1459: Phase 5 — Row detail + polish.
 * Issue #1653: Rich detail — duration, cost, call_count, run_log_url, lineage.
 *
 * Renders: status, IDs, timing + duration, cost/calls, summary, error,
 * source link, run-log link, lineage (triggered by / correlation).
 */

import { useState } from 'react';
import { Modal } from '@/components/ui';
import { formatDateTime, formatRelativeTime } from '@/utils/format';
import type { InvocationItem, InvocationStatus } from '@/types/activity';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a duration in milliseconds to a human-readable string (e.g. "2m 14s"). */
function formatDuration(startIso: string, endIso: string): string {
  const startMs = new Date(startIso).getTime();
  const endMs = new Date(endIso).getTime();
  const diffMs = endMs - startMs;
  if (diffMs < 0) return '—';
  const totalSeconds = Math.floor(diffMs / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes > 0 ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

/** Format a cost value to a readable string. */
function formatCost(costUsd: number): string {
  if (costUsd < 0.01) return `$${costUsd.toFixed(4)}`;
  return `$${costUsd.toFixed(2)}`;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Max characters to show for error_message before truncation. */
const ERROR_TRUNCATE_LENGTH = 200;

const STATUS_CONFIG: Record<InvocationStatus, { glyph: string; label: string; colorClass: string }> = {
  webhook_received: { glyph: '∘', label: 'Webhook received', colorClass: 'text-gray-500 dark:text-gray-400' },
  in_progress: { glyph: '●', label: 'In progress', colorClass: 'text-blue-600 dark:text-blue-400' },
  complete: { glyph: '✓', label: 'Complete', colorClass: 'text-green-600 dark:text-green-400' },
  failed: { glyph: '✗', label: 'Failed', colorClass: 'text-red-600 dark:text-red-400' },
  rejected: { glyph: '✗', label: 'Rejected', colorClass: 'text-orange-600 dark:text-orange-400' },
  rate_limited: { glyph: '✗', label: 'Rate limited', colorClass: 'text-yellow-600 dark:text-yellow-400' },
  no_op: { glyph: '✗', label: 'No-op', colorClass: 'text-gray-500 dark:text-gray-400' },
};

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="py-3 grid grid-cols-3 gap-4">
      <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className="text-sm text-gray-900 dark:text-white col-span-2 break-all">{children}</dd>
    </div>
  );
}

function ErrorDisplay({ message }: { message: string }) {
  const [expanded, setExpanded] = useState(false);
  const needsTruncation = message.length > ERROR_TRUNCATE_LENGTH;
  const displayText = !expanded && needsTruncation
    ? message.slice(0, ERROR_TRUNCATE_LENGTH) + '…'
    : message;

  return (
    <div className="space-y-1">
      <pre className="text-sm text-red-700 dark:text-red-400 whitespace-pre-wrap font-mono bg-red-50 dark:bg-red-900/20 p-2 rounded">
        {displayText}
      </pre>
      {needsTruncation && (
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export interface InvocationDetailProps {
  item: InvocationItem | null;
  isOpen: boolean;
  onClose: () => void;
}

export function InvocationDetail({ item, isOpen, onClose }: InvocationDetailProps) {
  if (!item) return null;

  const statusConfig = STATUS_CONFIG[item.status] ?? STATUS_CONFIG.no_op;
  const isTerminal = ['complete', 'failed', 'rejected', 'rate_limited', 'no_op'].includes(item.status);

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Invocation Detail" size="lg">
      <dl className="divide-y divide-gray-200 dark:divide-gray-700">
        {/* Status + timeline */}
        <DetailRow label="Status">
          <div className="space-y-1">
            <span className={`inline-flex items-center gap-1 font-medium ${statusConfig.colorClass}`}>
              <span aria-hidden="true">{statusConfig.glyph}</span>
              <span>{statusConfig.label}</span>
            </span>
            {item.status_updated_at && (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Last transition:{' '}
                <span title={formatDateTime(item.status_updated_at)}>
                  {formatRelativeTime(item.status_updated_at)}
                </span>
              </p>
            )}
            {!isTerminal && (
              <p className="text-xs text-gray-400 dark:text-gray-500 italic">
                Active — not yet terminal
              </p>
            )}
          </div>
        </DetailRow>

        {/* Identifiers */}
        <DetailRow label="Invocation ID">
          <code className="text-xs font-mono bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">
            {item.invocation_id}
          </code>
        </DetailRow>

        {item.correlation_id && (
          <DetailRow label="Correlation ID">
            <code className="text-xs font-mono bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">
              {item.correlation_id}
            </code>
          </DetailRow>
        )}

        {item.run_id && (
          <DetailRow label="Run / Job ID">
            <code className="text-xs font-mono bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">
              {item.run_id}
            </code>
          </DetailRow>
        )}

        {/* Timing */}
        <DetailRow label="Invoked at">
          <span title={formatDateTime(item.invoked_at)}>
            {formatRelativeTime(item.invoked_at)}
          </span>
          <span className="ml-2 text-xs text-gray-400">({formatDateTime(item.invoked_at)})</span>
        </DetailRow>

        {item.completed_at && (
          <DetailRow label="Completed at">
            <span title={formatDateTime(item.completed_at)}>
              {formatRelativeTime(item.completed_at)}
            </span>
            <span className="ml-2 text-xs text-gray-400">({formatDateTime(item.completed_at)})</span>
          </DetailRow>
        )}

        {/* Context */}
        <DetailRow label="Channel">
          <span className="capitalize">{item.channel}</span>
          {item.persona && (
            <span className="ml-2 text-gray-400">({item.persona})</span>
          )}
        </DetailRow>

        {item.topic && (
          <DetailRow label="Topic">
            {item.topic}
          </DetailRow>
        )}

        {item.summary && (
          <DetailRow label="Summary">
            {item.summary}
          </DetailRow>
        )}

        {/* Duration — Issue #1653 */}
        {item.completed_at && item.invoked_at && (
          <DetailRow label="Duration">
            <span className="font-medium">
              {formatDuration(item.invoked_at, item.completed_at)}
            </span>
          </DetailRow>
        )}
        {!item.completed_at && !isTerminal && item.invoked_at && (
          <DetailRow label="Duration">
            <span className="text-gray-400 dark:text-gray-500 italic">
              Running since {formatRelativeTime(item.invoked_at)}
            </span>
          </DetailRow>
        )}

        {/* Cost & Bedrock calls — Issue #1653 */}
        {(item.call_count != null || item.total_cost_usd != null) && (
          <DetailRow label="Bedrock usage">
            <div className="space-y-0.5">
              {item.call_count != null && (
                <p>{item.call_count} call{item.call_count !== 1 ? 's' : ''}</p>
              )}
              {item.total_cost_usd != null && (
                <p>{formatCost(item.total_cost_usd)}</p>
              )}
              {item.total_tokens != null && (
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {item.total_tokens.toLocaleString()} tokens
                </p>
              )}
            </div>
          </DetailRow>
        )}

        {/* Source link */}
        {item.source_url && (
          <DetailRow label="Source">
            <a
              href={item.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 hover:underline"
            >
              {item.repo && item.issue_number
                ? `${item.repo}#${item.issue_number}`
                : item.source_url}{' '}
              ↗
            </a>
          </DetailRow>
        )}

        {/* Run log link — Issue #1653 (Tier 2 populates; null until then) */}
        {item.run_log_url && (
          <DetailRow label="Run log">
            <a
              href={item.run_log_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 hover:underline"
            >
              View run log ↗
            </a>
          </DetailRow>
        )}

        {/* Lineage — Issue #1653 */}
        {item.triggered_by_invocation_id && (
          <DetailRow label="Triggered by">
            <div className="space-y-0.5">
              <code className="text-xs font-mono bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">
                {item.triggered_by_invocation_id}
              </code>
              {item.triggered_by_topic && (
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {item.triggered_by_topic}
                </p>
              )}
            </div>
          </DetailRow>
        )}

        {/* Error */}
        {item.status === 'failed' && (
          <DetailRow label="Error">
            {item.error_message ? (
              <ErrorDisplay message={item.error_message} />
            ) : (
              <span className="text-gray-400 dark:text-gray-500 italic">
                No error details available
              </span>
            )}
          </DetailRow>
        )}
      </dl>

      {/* Status timeline note */}
      <p className="mt-4 text-xs text-gray-400 dark:text-gray-500 italic">
        Status shows current state and last transition time. Full transition history is not retained.
      </p>
    </Modal>
  );
}
