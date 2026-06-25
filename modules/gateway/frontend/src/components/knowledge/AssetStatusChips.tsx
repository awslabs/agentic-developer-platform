/**
 * Asset index-status chips — per-tool indexing status inline display.
 *
 * Issue #1796 (Story G of E10 #1736): Reuses statusColor() pattern from
 * IndexingStatus.tsx to show per-stage status chips for a knowledge asset.
 */

import { useState, useEffect } from 'react';
import { getAssetStatus } from '@/services/knowledge';
import type { AssetStatusResponse, AssetIndexStage } from '@/types';

/** The 6 canonical indexing stages (display order). */
const STAGES = [
  'clone',
  'cgc_structural',
  'embed_vectors',
  'sbom_source',
  'deepwiki',
  'zoekt_index',
] as const;

/** Human-friendly labels for stages. */
const STAGE_LABELS: Record<string, string> = {
  clone: 'Clone',
  cgc_structural: 'Structural',
  embed_vectors: 'Vectors',
  sbom_source: 'SBOM',
  deepwiki: 'Wiki',
  zoekt_index: 'Zoekt',
  sbom_image: 'SBOM (image)',
  graphrag: 'GraphRAG',
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

export interface AssetStatusChipsProps {
  assetId: string;
  /** Compact mode shows only chip icons inline in the asset list. */
  compact?: boolean;
}

/**
 * Renders per-stage indexing status chips for a knowledge asset.
 * Fetches status from the backend and displays chips in stage order.
 */
export function AssetStatusChips({ assetId, compact = false }: AssetStatusChipsProps) {
  const [status, setStatus] = useState<AssetStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function fetchStatus() {
      setIsLoading(true);
      setError(null);
      try {
        const result = await getAssetStatus(assetId);
        if (!cancelled) {
          setStatus(result);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load status');
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    fetchStatus();
    return () => {
      cancelled = true;
    };
  }, [assetId]);

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

  if (status.stages.length === 0) {
    return (
      <div className="flex gap-1" data-testid="asset-status-no-stages">
        <span className="text-xs text-gray-400 dark:text-gray-500">
          {compact ? '-' : 'No stage data'}
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
          const stageStatus = stage?.status || 'pending';
          return (
            <span
              key={stageName}
              className={`inline-block w-2 h-2 rounded-full ${statusDot(stageStatus)}`}
              title={`${STAGE_LABELS[stageName] || stageName}: ${stageStatus}`}
            />
          );
        })}
      </div>
    );
  }

  return (
    <div className="space-y-2" data-testid="asset-status-chips">
      <div className="flex gap-1 flex-wrap">
        {STAGES.map((stageName) => {
          const stage = stageMap.get(stageName);
          const stageStatus = stage?.status || 'pending';
          return (
            <StageChip
              key={stageName}
              stageName={stageName}
              stage={stage || null}
              status={stageStatus}
            />
          );
        })}
        {/* Show additional stages not in the canonical list */}
        {status.stages
          .filter((s) => !STAGES.includes(s.stage as (typeof STAGES)[number]))
          .map((s) => (
            <StageChip key={s.stage} stageName={s.stage} stage={s} status={s.status} />
          ))}
      </div>
      {/* Show errors for failed stages */}
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
  status: string;
}

function StageChip({ stageName, stage, status }: StageChipProps) {
  const label = STAGE_LABELS[stageName] || stageName;
  const tooltip = stage
    ? `${label}: ${status}${stage.artifactRef ? ` (${stage.artifactRef})` : ''}${stage.error ? ` — ${stage.error}` : ''}`
    : `${label}: pending`;

  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${statusColor(status)}`}
      title={tooltip}
      data-testid={`stage-chip-${stageName}`}
    >
      {label}
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
            <p key={s.stage} className="text-xs text-red-700 dark:text-red-300">
              <span className="font-medium">{STAGE_LABELS[s.stage] || s.stage}:</span>{' '}
              {s.error || 'Unknown error'}
            </p>
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
    default:
      return 'bg-gray-300 dark:bg-gray-600';
  }
}
