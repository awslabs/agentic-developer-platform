/**
 * Tests for the IndexingStatus admin page.
 *
 * Issue #1424: Knowledge-layer indexing status page (per-repo, per-stage).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import IndexingStatus from '@/pages/admin/IndexingStatus';

vi.mock('@/services/admin', () => ({
  getIndexingRuns: vi.fn(),
  getIndexingRunDetail: vi.fn(),
}));

import { getIndexingRuns, getIndexingRunDetail } from '@/services/admin';

const mockGetRuns = getIndexingRuns as ReturnType<typeof vi.fn>;
const mockGetDetail = getIndexingRunDetail as ReturnType<typeof vi.fn>;

const mockRunsResponse = {
  items: [
    {
      id: 'run-001',
      repoId: 'repo-001',
      startedAt: '2026-06-13T09:50:00Z',
      completedAt: '2026-06-13T10:05:00Z',
      durationMs: 900000,
      status: 'completed',
      commitSha: 'abc1234def',
      error: null,
      totalRepos: 5,
      reposVerified: 4,
      reposFailed: 1,
      reposPartial: 0,
    },
    {
      id: 'run-002',
      repoId: 'repo-001',
      startedAt: '2026-06-12T14:00:00Z',
      completedAt: '2026-06-12T14:12:00Z',
      durationMs: 720000,
      status: 'completed',
      commitSha: 'def5678abc',
      error: null,
      totalRepos: 5,
      reposVerified: 5,
      reposFailed: 0,
      reposPartial: 0,
    },
  ],
  total: 2,
  page: 1,
  pageSize: 20,
  hasMore: false,
  summary: {
    totalRepos: 5,
    fullyVerifiedPct: 80.0,
    failedStages: 1,
    driftCount: 0,
  },
};

const mockDetailResponse = {
  runId: 'run-001',
  startedAt: '2026-06-13T09:50:00Z',
  completedAt: '2026-06-13T10:05:00Z',
  status: 'completed',
  commitSha: 'abc1234def',
  stages: [
    {
      id: 'stage-1',
      runId: 'run-001',
      repo: 'aws-e/adp',
      stage: 'clone',
      status: 'verified',
      artifactRef: 's3://bucket/aws-e/adp/clone/latest',
      verifiedAt: '2026-06-13T10:00:00Z',
      attempts: 1,
      error: null,
      startedAt: '2026-06-13T09:50:00Z',
      completedAt: '2026-06-13T09:51:00Z',
    },
    {
      id: 'stage-2',
      runId: 'run-001',
      repo: 'aws-e/adp',
      stage: 'embed_vectors',
      status: 'failed',
      artifactRef: null,
      verifiedAt: null,
      attempts: 2,
      error: 'Connection timeout to vector store',
      startedAt: '2026-06-13T09:51:00Z',
      completedAt: '2026-06-13T09:55:00Z',
    },
    {
      id: 'stage-3',
      runId: 'run-001',
      repo: 'aws-e/infra-core',
      stage: 'clone',
      status: 'verified',
      artifactRef: 's3://bucket/aws-e/infra-core/clone/latest',
      verifiedAt: '2026-06-13T10:00:00Z',
      attempts: 1,
      error: null,
      startedAt: '2026-06-13T09:50:00Z',
      completedAt: '2026-06-13T09:51:00Z',
    },
  ],
};

function renderIndexingStatus() {
  return render(
    <MemoryRouter>
      <IndexingStatus />
    </MemoryRouter>,
  );
}

describe('IndexingStatus Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetRuns.mockResolvedValue(mockRunsResponse);
    mockGetDetail.mockResolvedValue(mockDetailResponse);
  });

  it('renders the page title', async () => {
    renderIndexingStatus();

    expect(screen.getByText('Indexing Status')).toBeInTheDocument();
    await waitFor(() => {
      expect(mockGetRuns).toHaveBeenCalledTimes(1);
    });
  });

  it('renders summary stat cards', async () => {
    renderIndexingStatus();

    await waitFor(() => {
      expect(screen.getByText('Total Repos')).toBeInTheDocument();
    });

    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('80%')).toBeInTheDocument();
    expect(screen.getByText('Fully Verified')).toBeInTheDocument();
    expect(screen.getByText('Failed Stages')).toBeInTheDocument();
    expect(screen.getByText('Drift Detected')).toBeInTheDocument();
  });

  it('renders the runs table with run entries', async () => {
    renderIndexingStatus();

    await waitFor(() => {
      // First run: partial status (4/5 verified, 1 failed)
      expect(screen.getByText('Partial')).toBeInTheDocument();
    });

    // Second run: all verified = Complete
    expect(screen.getByText('Complete')).toBeInTheDocument();

    // Repo summary
    expect(screen.getByText('4/5 indexed')).toBeInTheDocument();
    expect(screen.getByText('5/5 indexed')).toBeInTheDocument();
  });

  it('shows empty state when no runs', async () => {
    mockGetRuns.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      pageSize: 20,
      hasMore: false,
      summary: null,
    });

    renderIndexingStatus();

    await waitFor(() => {
      expect(
        screen.getByText(
          'No indexing runs found. Runs will appear here after the first index operation.',
        ),
      ).toBeInTheDocument();
    });
  });

  it('expands a run to show per-stage detail', async () => {
    const user = userEvent.setup();
    renderIndexingStatus();

    await waitFor(() => {
      expect(screen.getByText('Partial')).toBeInTheDocument();
    });

    // Click "Details" button for first run
    const detailButtons = screen.getAllByText('Details');
    await user.click(detailButtons[0]);

    await waitFor(() => {
      expect(mockGetDetail).toHaveBeenCalledWith('run-001');
    });

    // Stage chips should appear for each repo
    await waitFor(() => {
      expect(screen.getByText('aws-e/adp')).toBeInTheDocument();
      expect(screen.getByText('aws-e/infra-core')).toBeInTheDocument();
    });
  });

  it('shows failed stage chips in red', async () => {
    const user = userEvent.setup();
    renderIndexingStatus();

    await waitFor(() => {
      expect(screen.getByText('Partial')).toBeInTheDocument();
    });

    const detailButtons = screen.getAllByText('Details');
    await user.click(detailButtons[0]);

    await waitFor(() => {
      expect(screen.getByText('aws-e/adp')).toBeInTheDocument();
    });

    // The "1 error(s)" button should be visible for the repo with a failed stage
    expect(screen.getByText('1 error(s)')).toBeInTheDocument();
  });

  it('shows error details when clicking error button', async () => {
    const user = userEvent.setup();
    renderIndexingStatus();

    await waitFor(() => {
      expect(screen.getByText('Partial')).toBeInTheDocument();
    });

    const detailButtons = screen.getAllByText('Details');
    await user.click(detailButtons[0]);

    await waitFor(() => {
      expect(screen.getByText('1 error(s)')).toBeInTheDocument();
    });

    await user.click(screen.getByText('1 error(s)'));

    await waitFor(() => {
      expect(screen.getByText(/Connection timeout to vector store/)).toBeInTheDocument();
    });
  });

  it('collapses an expanded run', async () => {
    const user = userEvent.setup();
    renderIndexingStatus();

    await waitFor(() => {
      expect(screen.getByText('Partial')).toBeInTheDocument();
    });

    const detailButtons = screen.getAllByText('Details');
    await user.click(detailButtons[0]);

    await waitFor(() => {
      expect(screen.getByText('aws-e/adp')).toBeInTheDocument();
    });

    // Click Collapse
    await user.click(screen.getByText('Collapse'));

    await waitFor(() => {
      expect(screen.queryByText('aws-e/adp')).not.toBeInTheDocument();
    });
  });

  it('shows error state when API fails', async () => {
    mockGetRuns.mockRejectedValue(new Error('Network error'));
    renderIndexingStatus();

    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeInTheDocument();
    });
  });

  it('shows commit SHA in run row', async () => {
    renderIndexingStatus();

    await waitFor(() => {
      // Short SHA displayed
      expect(screen.getByText('@ abc1234')).toBeInTheDocument();
      expect(screen.getByText('@ def5678')).toBeInTheDocument();
    });
  });
});
