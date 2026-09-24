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
import { TranscriptContent } from '@/components/TranscriptViewer';
import { formatDateTime, formatRelativeTime } from '@/utils/format';
// Issue #4207: was a local formatCost with the same sub-cent convention.
import { formatCost } from '@/utils/cost';
import { describeSkipReason, isNonRunStatus } from '@/utils/skipReason';
import { describeStopReason, isBudgetStoppedStatus } from '@/utils/stopReason';
import { describeLiveness } from '@/utils/liveness';
// Issue #4400: this modal had its own STATUS_CONFIG, one of three near-identical
// copies. `describeStatus` is the single map; the `'full'` variant preserves this
// surface's longer `webhook_received` label, which the narrow table cannot fit.
import { describeStatus } from '@/utils/status';
import { LivenessBadge } from '@/components/activity/LivenessBadge';
// Issue #3966: live run controls. Renders nothing unless the feature flag is on
// AND the polled control state says this run is genuinely controllable, so this
// import does not change the modal for any existing deployment.
import { ControlPanel } from '@/components/ControlPanel';
import type { InvocationItem } from '@/types/activity';

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

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Max characters to show for error_message before truncation. */
const ERROR_TRUNCATE_LENGTH = 200;

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
  /** Use admin transcript endpoint. */
  isAdmin?: boolean;
  /**
   * Issue #3966: re-fetch this invocation after a live control command.
   *
   * `item` is a snapshot owned by the page, so the panel cannot refresh it
   * itself; without this the status row would keep showing the pre-command
   * snapshot while the control panel showed the new phase.
   */
  onRefreshItem?: () => void;
}

export function InvocationDetail({
  item,
  isOpen,
  onClose,
  isAdmin = false,
  onRefreshItem,
}: InvocationDetailProps) {
  const [showTranscript, setShowTranscript] = useState(false);

  if (!item) return null;

  const statusConfig = describeStatus(item.status, 'full');
  // Issue #4020: blocked/skipped are terminal — without them the modal would
  // claim "Active — not yet terminal" on a row that will never move again.
  // Issue #4187: budget_stopped is terminal too — the run is over.
  // Issue #3964: and so is aborted. Omitting it here would tell the operator who
  // just stopped the run that it is "Active — not yet terminal", i.e. that their
  // own stop did not take — the single most misleading thing this modal could say
  // about an aborted run (AC-A9).
  const isTerminal = [
    'complete',
    'failed',
    'rejected',
    'rate_limited',
    'no_op',
    'blocked',
    'skipped',
    'budget_stopped',
    'aborted',
  ].includes(item.status);

  // ---------------------------------------------------------------------------
  // Row fragments — extracted for conditional ordering (Issue #3765)
  // ---------------------------------------------------------------------------

  /**
   * Issue #4176: the derived liveness verdict, shown beside the status.
   *
   * On the detail view we show it for ALL verdicts, not just `unverifiable` —
   * unlike the dense table, this surface has room, and an operator who opened a
   * run specifically to understand its state deserves the explicit answer.
   */
  const livenessConfig = describeLiveness(item.liveness);

  const statusRow = (
    <DetailRow label="Status">
      <div className="space-y-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`inline-flex items-center gap-1 font-medium ${statusConfig.colorClass}`}>
            <span aria-hidden="true">{statusConfig.glyph}</span>
            <span>{statusConfig.label}</span>
          </span>
          <LivenessBadge verdict={item.liveness} />
        </div>
        {item.status_updated_at && (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Last transition:{' '}
            <span title={formatDateTime(item.status_updated_at)}>
              {formatRelativeTime(item.status_updated_at)}
            </span>
          </p>
        )}
        {/*
          Issue #4176: this line used to read a flat "Active — not yet terminal"
          on every non-terminal run, including ones that stopped reporting days
          ago. When the verdict says we cannot confirm the run, say that instead —
          it is the whole point of the field. Carefully worded as "cannot
          confirm", never "stopped": loss of contact is not evidence of exit, and
          a run described as ended is a run someone will retry.
        */}
        {!isTerminal &&
          (livenessConfig && item.liveness === 'unverifiable' ? (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              {livenessConfig.description}
            </p>
          ) : (
            <p className="text-xs text-gray-400 dark:text-gray-500 italic">
              Active — not yet terminal
            </p>
          ))}
      </div>
    </DetailRow>
  );

  const errorRow = item.status === 'failed' ? (
    <DetailRow label="Error">
      {item.error_message ? (
        <ErrorDisplay message={item.error_message} />
      ) : (
        <span className="text-gray-400 dark:text-gray-500 italic">
          No error details available
        </span>
      )}
    </DetailRow>
  ) : null;

  /**
   * Issue #4020: the "why didn't anything run" row.
   *
   * Only rendered for the three non-run statuses. Presented as a plain
   * informational row, NOT through ErrorDisplay — the red error styling would
   * misreport a correct guard decision (a loop that was stopped, a redelivery
   * that was deduplicated) as a fault.
   *
   * When the reason is absent this still renders, saying so explicitly: rows
   * written before this change carry no reason, and "we don't have a reason for
   * this one" is a more honest answer than an absent row that looks identical to
   * the old unexplained badge.
   */
  const skipReasonRow = isNonRunStatus(item.status) ? (
    <DetailRow label="Reason">
      {describeSkipReason(item.skip_reason) ? (
        <div className="space-y-1">
          <p className="text-gray-900 dark:text-white">{describeSkipReason(item.skip_reason)}</p>
          {/* The enum itself is what appears in CloudWatch logs and metrics, so
              showing it gives an operator the exact term to search on. */}
          <p className="text-xs font-mono text-gray-400 dark:text-gray-500">{item.skip_reason}</p>
        </div>
      ) : (
        <span className="text-gray-400 dark:text-gray-500 italic">
          No reason recorded — this event predates reason tracking.
        </span>
      )}
    </DetailRow>
  ) : null;

  /**
   * Issue #4187: the "why did this stop early" row.
   *
   * Same shape and rationale as `skipReasonRow` above, and deliberately NOT
   * `ErrorDisplay`: a spend cap firing is the control working. The operator's
   * next action is a budget decision, not a bug hunt, and red styling points
   * them at the wrong one.
   */
  const stopReasonRow = isBudgetStoppedStatus(item.status) ? (
    <DetailRow label="Stopped because">
      {describeStopReason(item.stop_reason) ? (
        <div className="space-y-1">
          <p className="text-gray-900 dark:text-white">{describeStopReason(item.stop_reason)}</p>
          {/* The enum is what appears in logs and metrics, so showing it gives
              the operator the exact term to search on. */}
          <p className="text-xs font-mono text-gray-400 dark:text-gray-500">{item.stop_reason}</p>
        </div>
      ) : (
        <span className="text-gray-400 dark:text-gray-500 italic">
          A spend cap stopped this run; no specific cap was recorded.
        </span>
      )}
    </DetailRow>
  ) : null;

  const durationRow = (
    <>
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
    </>
  );

  const costRow = (item.call_count != null || item.total_cost_usd != null) ? (
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
  ) : null;

  const topicRow = item.topic ? (
    <DetailRow label="Topic">{item.topic}</DetailRow>
  ) : null;

  const sourceRow = item.source_url ? (
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
  ) : null;

  const runLogRow = item.run_log_url ? (
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
  ) : null;

  const transcriptRow = item.transcript_key ? (
    <DetailRow label="Transcript">
      <button
        type="button"
        onClick={() => setShowTranscript(true)}
        className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 hover:underline text-sm"
      >
        View full transcript
      </button>
    </DetailRow>
  ) : null;

  const lineageRow = item.triggered_by_invocation_id ? (
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
  ) : null;

  const identifierRows = (
    <>
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
    </>
  );

  const timingRows = (
    <>
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
    </>
  );

  const channelRow = (
    <DetailRow label="Channel">
      <span className="capitalize">{item.channel}</span>
      {item.persona && (
        <span className="ml-2 text-gray-400">({item.persona})</span>
      )}
    </DetailRow>
  );

  const summaryRow = item.summary ? (
    <DetailRow label="Summary">{item.summary}</DetailRow>
  ) : null;

  // ---------------------------------------------------------------------------
  // Layout: error-first for failed runs (Issue #3765), default order otherwise
  // ---------------------------------------------------------------------------

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={showTranscript ? 'Run Transcript' : 'Invocation Detail'} size="lg">
      {showTranscript ? (
        /* Issue #3767: Inline transcript content swap (replaces nested modal) */
        <div>
          <button
            type="button"
            onClick={() => setShowTranscript(false)}
            className="mb-4 inline-flex items-center gap-1 text-sm text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 hover:underline"
          >
            ← Back to detail
          </button>
          <TranscriptContent
            invocationId={item.invocation_id}
            isAdmin={isAdmin}
          />
        </div>
      ) : (
        <>
          <dl className="divide-y divide-gray-200 dark:divide-gray-700">
            {item.status === 'failed' ? (
              <>
                {/* Failed-run order: Status → Error → Duration → Cost → Topic →
                    Source → Transcript → Lineage → IDs → Timing → Channel → Summary */}
                {statusRow}
                {errorRow}
                {durationRow}
                {costRow}
                {topicRow}
                {sourceRow}
                {runLogRow}
                {transcriptRow}
                {lineageRow}
                {identifierRows}
                {timingRows}
                {channelRow}
                {summaryRow}
              </>
            ) : (
              <>
                {/* Default order (non-failed runs) */}
                {statusRow}
                {/* Issue #4020: directly under Status, mirroring where Error sits
                    in the failed-run order — for a non-run the reason IS the
                    headline fact, so it must not be buried below the ID block. */}
                {skipReasonRow}
                {/* Issue #4187: same placement, same reasoning — for a run a cap
                    stopped, why it stopped is the headline fact. */}
                {stopReasonRow}
                {identifierRows}
                {timingRows}
                {channelRow}
                {topicRow}
                {summaryRow}
                {durationRow}
                {costRow}
                {sourceRow}
                {runLogRow}
                {transcriptRow}
                {lineageRow}
              </>
            )}
          </dl>

          {/*
            Issue #3966: live controls.

            Placed after the detail list rather than inside it: these are actions,
            not facts, and interleaving buttons into a definition list would put
            interactive controls inside `<dd>` elements.

            `isTerminalRun` is passed only to avoid polling a run that has
            demonstrably ended. It is not the gate — the panel decides what to
            offer from the polled control state, because a non-terminal status
            does not imply the run is reachable or controllable.
          */}
          <ControlPanel
            invocationId={item.invocation_id}
            isOpen={isOpen}
            isTerminalRun={isTerminal}
            onCommandApplied={onRefreshItem}
          />

          {/* Status timeline note */}
          <p className="mt-4 text-xs text-gray-400 dark:text-gray-500 italic">
            Status shows current state and last transition time. Full transition history is not retained.
          </p>
        </>
      )}
    </Modal>
  );
}
