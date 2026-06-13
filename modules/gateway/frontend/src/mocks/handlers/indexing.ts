/**
 * MSW mock handlers for the indexing admin API.
 *
 * Issue #1424: Knowledge-layer indexing status page (per-repo, per-stage).
 * These mocks provide realistic data for component tests while the real
 * index_run_stages table is empty (populated after re-index runs).
 */
import { http, HttpResponse } from 'msw';

// Mock data: simulates 2 index runs with per-stage details
const MOCK_STAGES = ['clone', 'cgc_structural', 'embed_vectors', 'sbom_source', 'deepwiki', 'zoekt_index'];

function STAGES_FOR_REPO(
  runId: string,
  repo: string,
  allVerified: boolean,
  failedStage?: string,
) {
  return MOCK_STAGES.map((stage, i) => ({
    id: `stage-${runId}-${repo.replace(/\//g, '-')}-${stage}`,
    run_id: runId,
    repo,
    stage,
    status: failedStage === stage ? 'failed' : allVerified ? 'verified' : (i < 3 ? 'verified' : 'pending'),
    artifact_ref: failedStage === stage ? null : `s3://agent-context-dev/${repo}/${stage}/latest`,
    verified_at: failedStage === stage ? null : '2026-06-13T10:00:00Z',
    attempts: failedStage === stage ? 2 : 1,
    error: failedStage === stage ? `Stage ${stage} timed out after 300s` : null,
    started_at: '2026-06-13T09:50:00Z',
    completed_at: failedStage === stage ? '2026-06-13T09:55:00Z' : '2026-06-13T10:00:00Z',
  }));
}

const mockRun1Stages = [
  ...STAGES_FOR_REPO('run-001', 'aws-e/adp', true),
  ...STAGES_FOR_REPO('run-001', 'aws-e/infra-core', true),
  ...STAGES_FOR_REPO('run-001', 'aws-e/gateway-service', false, 'embed_vectors'),
  ...STAGES_FOR_REPO('run-001', 'aws-e/agent-runtime', true),
  ...STAGES_FOR_REPO('run-001', 'aws-e/docs', true),
];

const mockRun2Stages = [
  ...STAGES_FOR_REPO('run-002', 'aws-e/adp', true),
  ...STAGES_FOR_REPO('run-002', 'aws-e/infra-core', false, 'zoekt_index'),
  ...STAGES_FOR_REPO('run-002', 'aws-e/gateway-service', true),
  ...STAGES_FOR_REPO('run-002', 'aws-e/agent-runtime', true),
  ...STAGES_FOR_REPO('run-002', 'aws-e/docs', false, 'deepwiki'),
];

const mockRuns = [
  {
    id: 'run-001',
    repo_id: 'repo-catalog-001',
    started_at: '2026-06-13T09:50:00Z',
    completed_at: '2026-06-13T10:05:00Z',
    duration_ms: 900000,
    status: 'completed',
    commit_sha: 'abc1234',
    error: null,
    total_repos: 5,
    repos_verified: 4,
    repos_failed: 1,
    repos_partial: 0,
  },
  {
    id: 'run-002',
    repo_id: 'repo-catalog-001',
    started_at: '2026-06-12T14:00:00Z',
    completed_at: '2026-06-12T14:12:00Z',
    duration_ms: 720000,
    status: 'completed',
    commit_sha: 'def5678',
    error: null,
    total_repos: 5,
    repos_verified: 3,
    repos_failed: 2,
    repos_partial: 0,
  },
];

export const indexingHandlers = [
  // Level 1: List indexing runs
  http.get('/api/admin/indexing/runs', ({ request }) => {
    const url = new URL(request.url);
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '20');

    const start = (page - 1) * pageSize;
    const items = mockRuns.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: mockRuns.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < mockRuns.length,
      summary: {
        total_repos: 5,
        fully_verified_pct: 80.0,
        failed_stages: 1,
        drift_count: 0,
      },
    });
  }),

  // Level 2: Get run detail (per-stage)
  http.get('/api/admin/indexing/runs/:runId', ({ params }) => {
    const runId = params.runId as string;
    const run = mockRuns.find((r) => r.id === runId);

    if (!run) {
      return HttpResponse.json(
        { error: 'Not found', message: 'Index run not found' },
        { status: 404 },
      );
    }

    const stages = runId === 'run-001' ? mockRun1Stages : mockRun2Stages;

    return HttpResponse.json({
      run_id: run.id,
      started_at: run.started_at,
      completed_at: run.completed_at,
      status: run.status,
      commit_sha: run.commit_sha,
      stages,
    });
  }),
];
