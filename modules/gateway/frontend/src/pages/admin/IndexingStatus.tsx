/**
 * Admin page: Knowledge-layer indexing status.
 *
 * Issue #1424: Two-level drill-down showing per-run summary (Level 1)
 * and per-repo, per-stage detail (Level 2).
 *
 * Read-only view for platform admins. Backed by index_runs + index_run_stages
 * tables from the agent-context knowledge layer (#1423).
 */
import { useState, useEffect, useCallback } from 'react';
import { StatCard } from '@/components/dashboard/StatCard';
import { Card } from '@/components/ui';
import { getIndexingRuns, getIndexingRunDetail } from '@/services/admin';
import type { IndexRunSummary, IndexRunDetailResponse, IndexRunStage } from '@/types';

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

/** Status to chip color mapping. */
function statusColor(status: string): string {
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

/** Derive a run-level status label from stage stats. */
function deriveRunStatus(run: IndexRunSummary): string {
  if (run.status === 'running') return 'Running';
  if (run.reposFailed > 0 && run.reposVerified === 0) return 'Failed';
  if (run.reposFailed > 0) return 'Partial';
  if (run.reposVerified === run.totalRepos && run.totalRepos > 0) return 'Complete';
  if (run.totalRepos === 0) return run.status;
  return 'Partial';
}

function runStatusChipColor(run: IndexRunSummary): string {
  const label = deriveRunStatus(run);
  switch (label) {
    case 'Complete':
      return 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200';
    case 'Failed':
      return 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200';
    case 'Running':
      return 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200';
    default:
      return 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200';
  }
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return '-';
  return new Date(ts).toLocaleString();
}

function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return '-';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

export default function IndexingStatus() {
  const [runs, setRuns] = useState<IndexRunSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<IndexRunDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [summary, setSummary] = useState<{
    totalRepos: number;
    fullyVerifiedPct: number;
    failedStages: number;
    driftCount: number;
  } | null>(null);

  const fetchRuns = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await getIndexingRuns({ page: 1, pageSize: 20 });
      setRuns(response.items);
      setSummary(response.summary);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load indexing runs');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRuns();
  }, [fetchRuns]);

  const handleExpandRun = useCallback(
    async (runId: string) => {
      if (expandedRunId === runId) {
        setExpandedRunId(null);
        setRunDetail(null);
        return;
      }
      setExpandedRunId(runId);
      setDetailLoading(true);
      try {
        const detail = await getIndexingRunDetail(runId);
        setRunDetail(detail);
      } catch {
        setRunDetail(null);
      } finally {
        setDetailLoading(false);
      }
    },
    [expandedRunId],
  );

  // Group stages by repo for the detail view
  const stagesByRepo: Record<string, IndexRunStage[]> = {};
  if (runDetail) {
    for (const stage of runDetail.stages) {
      if (!stagesByRepo[stage.repo]) {
        stagesByRepo[stage.repo] = [];
      }
      stagesByRepo[stage.repo].push(stage);
    }
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          Indexing Status
        </h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Knowledge-layer indexing status per repository and stage.
        </p>
      </div>

      {/* Summary StatCards */}
      {summary && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard title="Total Repos" value={summary.totalRepos} icon="📦" />
          <StatCard
            title="Fully Verified"
            value={`${summary.fullyVerifiedPct}%`}
            icon="✅"
          />
          <StatCard title="Failed Stages" value={summary.failedStages} icon="❌" />
          <StatCard title="Drift Detected" value={summary.driftCount} icon="⚠️" />
        </div>
      )}

      {/* Error state */}
      {error && (
        <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-4" role="alert">
          <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
        </div>
      )}

      {/* Loading state */}
      {isLoading && (
        <div className="text-center py-8 text-gray-500 dark:text-gray-400">
          Loading indexing runs...
        </div>
      )}

      {/* Empty state */}
      {!isLoading && !error && runs.length === 0 && (
        <Card>
          <div className="text-center py-8">
            <p className="text-gray-500 dark:text-gray-400">
              No indexing runs found. Runs will appear here after the first index operation.
            </p>
          </div>
        </Card>
      )}

      {/* Runs table (Level 1) */}
      {!isLoading && runs.length > 0 && (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-3 px-4 font-medium text-gray-500 dark:text-gray-400">
                    Run
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500 dark:text-gray-400">
                    Status
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500 dark:text-gray-400">
                    Started
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500 dark:text-gray-400">
                    Duration
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500 dark:text-gray-400">
                    Repos
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500 dark:text-gray-400">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <RunRow
                    key={run.id}
                    run={run}
                    isExpanded={expandedRunId === run.id}
                    onToggle={() => handleExpandRun(run.id)}
                    detail={expandedRunId === run.id ? runDetail : null}
                    detailLoading={expandedRunId === run.id && detailLoading}
                    stagesByRepo={expandedRunId === run.id ? stagesByRepo : {}}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface RunRowProps {
  run: IndexRunSummary;
  isExpanded: boolean;
  onToggle: () => void;
  detail: IndexRunDetailResponse | null;
  detailLoading: boolean;
  stagesByRepo: Record<string, IndexRunStage[]>;
}

function RunRow({ run, isExpanded, onToggle, detail, detailLoading, stagesByRepo }: RunRowProps) {
  const statusLabel = deriveRunStatus(run);
  const repoSummary =
    run.totalRepos > 0
      ? `${run.reposVerified}/${run.totalRepos} indexed`
      : '-';

  return (
    <>
      <tr
        className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50 cursor-pointer"
        onClick={onToggle}
        role="button"
        aria-expanded={isExpanded}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onToggle();
          }
        }}
      >
        <td className="py-3 px-4">
          <span className="font-mono text-xs text-gray-700 dark:text-gray-300">
            {run.id.slice(0, 8)}
          </span>
          {run.commitSha && (
            <span className="ml-2 text-xs text-gray-400">
              @ {run.commitSha.slice(0, 7)}
            </span>
          )}
        </td>
        <td className="py-3 px-4">
          <span
            className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${runStatusChipColor(run)}`}
          >
            {statusLabel}
          </span>
        </td>
        <td className="py-3 px-4 text-gray-700 dark:text-gray-300">
          {formatTimestamp(run.startedAt)}
        </td>
        <td className="py-3 px-4 text-gray-700 dark:text-gray-300">
          {formatDuration(run.durationMs)}
        </td>
        <td className="py-3 px-4 text-gray-700 dark:text-gray-300">{repoSummary}</td>
        <td className="py-3 px-4">
          <button
            className="text-primary-600 hover:text-primary-800 dark:text-primary-400 dark:hover:text-primary-200 text-xs font-medium"
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
          >
            {isExpanded ? 'Collapse' : 'Details'}
          </button>
        </td>
      </tr>

      {/* Expanded detail (Level 2) */}
      {isExpanded && (
        <tr>
          <td colSpan={6} className="bg-gray-50 dark:bg-gray-800/30 px-4 py-4">
            {detailLoading && (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Loading stage details...
              </p>
            )}
            {!detailLoading && detail && Object.keys(stagesByRepo).length === 0 && (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                No stage data for this run.
              </p>
            )}
            {!detailLoading && Object.keys(stagesByRepo).length > 0 && (
              <div className="space-y-3">
                {Object.entries(stagesByRepo).map(([repo, stages]) => (
                  <RepoStageRow key={repo} repo={repo} stages={stages} />
                ))}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

interface RepoStageRowProps {
  repo: string;
  stages: IndexRunStage[];
}

function RepoStageRow({ repo, stages }: RepoStageRowProps) {
  const [showErrors, setShowErrors] = useState(false);
  const failedStages = stages.filter((s) => s.status === 'failed');

  // Build a lookup for quick stage chip rendering
  const stageMap = new Map(stages.map((s) => [s.stage, s]));

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-gray-800 dark:text-gray-200 min-w-[200px] truncate">
          {repo}
        </span>
        <div className="flex gap-1 flex-wrap">
          {STAGES.map((stageName) => {
            const stage = stageMap.get(stageName);
            const status = stage?.status || 'pending';
            return (
              <span
                key={stageName}
                className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${statusColor(status)}`}
                title={
                  stage
                    ? `${STAGE_LABELS[stageName] || stageName}: ${status}${stage.artifactRef ? ` (${stage.artifactRef})` : ''}${stage.error ? ` — ${stage.error}` : ''}`
                    : `${STAGE_LABELS[stageName] || stageName}: pending`
                }
              >
                {STAGE_LABELS[stageName] || stageName}
              </span>
            );
          })}
          {/* Show additional stages not in the canonical list */}
          {stages
            .filter((s) => !STAGES.includes(s.stage as (typeof STAGES)[number]))
            .map((s) => (
              <span
                key={s.stage}
                className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${statusColor(s.status)}`}
                title={`${STAGE_LABELS[s.stage] || s.stage}: ${s.status}${s.error ? ` — ${s.error}` : ''}`}
              >
                {STAGE_LABELS[s.stage] || s.stage}
              </span>
            ))}
        </div>
        {failedStages.length > 0 && (
          <button
            className="text-xs text-red-600 dark:text-red-400 hover:underline ml-auto"
            onClick={() => setShowErrors(!showErrors)}
          >
            {showErrors ? 'Hide errors' : `${failedStages.length} error(s)`}
          </button>
        )}
      </div>
      {showErrors && failedStages.length > 0 && (
        <div className="ml-[200px] pl-3 border-l-2 border-red-200 dark:border-red-800 space-y-1">
          {failedStages.map((s) => (
            <p key={s.id} className="text-xs text-red-700 dark:text-red-300">
              <span className="font-medium">{STAGE_LABELS[s.stage] || s.stage}:</span>{' '}
              {s.error || 'Unknown error'}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
