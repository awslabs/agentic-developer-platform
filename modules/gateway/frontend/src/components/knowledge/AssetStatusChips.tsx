/**
 * Asset index-status chips — per-tool indexing status inline display.
 *
 * Issue #1796 (Story G of E10 #1736): Reuses statusColor() pattern from
 * IndexingStatus.tsx to show per-stage status chips for a knowledge asset.
 *
 * Issue #2309 (Story 3 of EPIC #2292): Detailed ingestion view — friendly
 * names, metrics, workerPod, 4-state stages (verified/failed/skipped/not-available).
 */

import { useState } from 'react';
import { usePollingStatus } from '@/hooks/usePollingStatus';
import type { AssetIndexStage } from '@/types';

/** The canonical indexing stages (display order). */
const STAGES = [
  'clone',
  'cgc_structural',
  'scip_structural',
  'embed_vectors',
  'sbom_source',
  'sbom_image',
  'deepwiki',
  'zoekt_index',
  'graphrag',
] as const;

/** Human-friendly labels for stages. */
const STAGE_LABELS: Record<string, string> = {
  clone: 'Clone',
  cgc_structural: 'Code Graph',
  scip_structural: 'Code Graph (Deep SCIP)',
  embed_vectors: 'Embeddings',
  sbom_source: 'Dependencies (SBOM)',
  sbom_image: 'Container SBOM',
  deepwiki: 'Wiki',
  zoekt_index: 'Code Search',
  graphrag: 'Knowledge Graph',
};

/** Status to chip color mapping (reused from IndexingStatus.tsx). */
export function statusColor(status: string): string {
  switch (status) {
    case 'verified':
      return 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200';
    case 'failed':
      return 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200';
    case 'running':
      return 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200';
    case 'skipped':
      return 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200';
    default:
      return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300';
  }
}

/** Status icon for the 4-state rendering. */
function statusIcon(status: string): string {
  switch (status) {
    case 'verified':
      return '✓';
    case 'failed':
      return '✗';
    default:
      return '';
  }
}

export interface AssetStatusChipsProps {
  assetId: string;
  /** Asset type — used to show appropriate empty-state for non-repo assets. */
  assetType?: string;
  /** Compact mode shows only chip icons inline in the asset list. */
  compact?: boolean;
  /** Enable live polling (stops on terminal state). Defaults to true. */
  enablePolling?: boolean;
  /** Called when the run status changes (e.g. queued→indexing→indexed). */
  onStatusChange?: (oldStatus: string | null, newStatus: string | null) => void;
}

/**
 * Renders per-stage indexing status chips for a knowledge asset.
 * Fetches status from the backend and displays chips in stage order.
 *
 * 4-state rendering (driven by row existence):
 * - verified → green ✓ + metrics
 * - failed → red ✗ + error + workerPod
 * - skipped (row exists with status=skipped) → muted "skipped (disabled)"
 * - no row → gray "not available"
 */
export function AssetStatusChips({
  assetId,
  assetType,
  compact = false,
  enablePolling = true,
  onStatusChange,
}: AssetStatusChipsProps) {
  const { status, isLoading, error, isPolling } = usePollingStatus({
    assetId,
    enabled: enablePolling,
    onStatusChange,
  });

  if (isLoading) {
    return (
      <div className="flex gap-1" data-testid="asset-status-loading">
        <span className="text-xs text-gray-400 dark:text-gray-500">Loading status...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex gap-1" data-testid="asset-status-error">
        <span className="text-xs text-red-500 dark:text-red-400">{error}</span>
      </div>
    );
  }

  if (!status || !status.repoFound) {
    return (
      <div className="flex gap-1" data-testid="asset-status-no-repo">
        <span className="text-xs text-gray-400 dark:text-gray-500">
          {compact ? '-' : 'Not yet indexed'}
        </span>
      </div>
    );
  }

  // For non-repo asset types with zero stages, show an appropriate message
  if (status.stages.length === 0) {
    const isNonRepoType = assetType && assetType !== 'repo';
    return (
      <div className="flex gap-1" data-testid="asset-status-no-stages">
        <span className="text-xs text-gray-400 dark:text-gray-500">
          {compact
            ? '-'
            : isNonRepoType
              ? 'Stage tracking not yet available for this asset type'
              : 'No stage data'}
        </span>
      </div>
    );
  }

  // Build a map for quick lookup
  const stageMap = new Map(status.stages.map((s) => [s.stage, s]));

  if (compact) {
    return (
      <div className="flex gap-0.5" data-testid="asset-status-chips-compact">
        {STAGES.map((stageName) => {
          const stage = stageMap.get(stageName);
          // 4-state: no row = "not available" (gray), row present = real status
          const stageStatus = stage ? stage.status : 'not_available';
          return (
            <span
              key={stageName}
              className={`inline-block w-2 h-2 rounded-full ${statusDot(stageStatus)}`}
              title={`${STAGE_LABELS[stageName] || stageName}: ${stageStatus === 'not_available' ? 'not available' : stageStatus}`}
            />
          );
        })}
      </div>
    );
  }

  return (
    <div className="space-y-2" data-testid="asset-status-chips">
      <div className="flex gap-1.5 flex-wrap">
        {STAGES.map((stageName) => {
          const stage = stageMap.get(stageName);
          return (
            <StageChip
              key={stageName}
              stageName={stageName}
              stage={stage || null}
            />
          );
        })}
        {/* Show additional stages not in the canonical list */}
        {status.stages
          .filter((s) => !STAGES.includes(s.stage as (typeof STAGES)[number]))
          .map((s) => (
            <StageChip key={s.stage} stageName={s.stage} stage={s} />
          ))}
      </div>
      {/* Live-update indicator when polling is active */}
      {isPolling && (
        <p className="text-xs text-gray-400 dark:text-gray-500" data-testid="polling-indicator">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse mr-1 align-middle" />
          Updating live
        </p>
      )}
      {/* Show errors for failed stages (with workerPod) */}
      {status.stages.some((s) => s.status === 'failed') && (
        <StageErrors stages={status.stages.filter((s) => s.status === 'failed')} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface StageChipProps {
  stageName: string;
  stage: AssetIndexStage | null;
}

/** Format metrics into a human-readable string. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- metrics schema is open-ended from backend
function formatMetrics(metrics: Record<string, any>): string {
  const parts: string[] = [];
  if (metrics.symbols != null) parts.push(`${Number(metrics.symbols).toLocaleString()} symbols`);
  if (metrics.nodes != null) parts.push(`${Number(metrics.nodes).toLocaleString()} nodes`);
  if (metrics.edges != null) parts.push(`${Number(metrics.edges).toLocaleString()} edges`);
  if (metrics.vectors != null) parts.push(`${Number(metrics.vectors).toLocaleString()} vectors`);
  if (metrics.packages != null) parts.push(`${Number(metrics.packages).toLocaleString()} packages`);
  if (metrics.pages != null) parts.push(`${Number(metrics.pages).toLocaleString()} pages`);
  if (metrics.files != null) parts.push(`${Number(metrics.files).toLocaleString()} files`);
  // Fallback: show any remaining keys not yet handled
  if (parts.length === 0) {
    for (const [key, value] of Object.entries(metrics)) {
      if (value != null && typeof value === 'number') {
        parts.push(`${value.toLocaleString()} ${key}`);
      }
    }
  }
  return parts.join(' / ');
}

function StageChip({ stageName, stage }: StageChipProps) {
  const label = STAGE_LABELS[stageName] || stageName;

  // 4-state rendering driven by row existence
  if (!stage) {
    // No row → "not available"
    return (
      <span
        className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-500"
        title={`${label}: not available`}
        data-testid={`stage-chip-${stageName}`}
      >
        {label}
        <span className="ml-1 text-gray-400 dark:text-gray-500">—</span>
      </span>
    );
  }

  const { status, metrics, error: stageError } = stage;
  const icon = statusIcon(status);

  // Skipped → muted rendering
  if (status === 'skipped') {
    return (
      <span
        className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-500 italic"
        title={`${label}: skipped (disabled)`}
        data-testid={`stage-chip-${stageName}`}
      >
        {label}
        <span className="ml-1">skipped</span>
      </span>
    );
  }

  // Build metrics text
  const metricsText = metrics ? formatMetrics(metrics) : '';

  // Build tooltip
  const tooltipParts = [`${label}: ${status}`];
  if (metricsText) tooltipParts.push(metricsText);
  if (stageError) tooltipParts.push(`Error: ${stageError}`);
  if (stage.workerPod && status === 'failed') tooltipParts.push(`Pod: ${stage.workerPod}`);
  const tooltip = tooltipParts.join(' · ');

  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${statusColor(status)}`}
      title={tooltip}
      data-testid={`stage-chip-${stageName}`}
    >
      {icon && <span className="mr-1">{icon}</span>}
      {label}
      {metricsText && (
        <span className="ml-1 opacity-75" data-testid={`stage-metrics-${stageName}`}>
          · {metricsText}
        </span>
      )}
    </span>
  );
}

interface StageErrorsProps {
  stages: AssetIndexStage[];
}

function StageErrors({ stages }: StageErrorsProps) {
  const [showErrors, setShowErrors] = useState(false);

  return (
    <div>
      <button
        className="text-xs text-red-600 dark:text-red-400 hover:underline"
        onClick={() => setShowErrors(!showErrors)}
        data-testid="toggle-errors"
      >
        {showErrors ? 'Hide errors' : `${stages.length} error(s)`}
      </button>
      {showErrors && (
        <div className="mt-1 pl-2 border-l-2 border-red-200 dark:border-red-800 space-y-1">
          {stages.map((s) => (
            <div key={s.stage} className="text-xs text-red-700 dark:text-red-300">
              <p>
                <span className="font-medium">{STAGE_LABELS[s.stage] || s.stage}:</span>{' '}
                {s.error || 'Unknown error'}
              </p>
              {s.workerPod && (
                <p
                  className="text-red-500 dark:text-red-400 font-mono mt-0.5"
                  data-testid={`worker-pod-${s.stage}`}
                >
                  pod: {s.workerPod}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Compact dot color for inline status indicators. */
function statusDot(status: string): string {
  switch (status) {
    case 'verified':
      return 'bg-green-500';
    case 'failed':
      return 'bg-red-500';
    case 'running':
      return 'bg-blue-500';
    case 'skipped':
      return 'bg-yellow-500';
    case 'not_available':
      return 'bg-gray-200 dark:bg-gray-700';
    default:
      return 'bg-gray-300 dark:bg-gray-600';
  }
}
