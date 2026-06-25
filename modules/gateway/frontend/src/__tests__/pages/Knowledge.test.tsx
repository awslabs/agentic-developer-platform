/**
 * Component tests for the Knowledge management page.
 *
 * Issue #1794 (Story E of E10 #1736).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import Knowledge from '@/pages/Knowledge';

// Mock the knowledge service
vi.mock('@/services/knowledge', () => ({
  listAssets: vi.fn(),
  getAssetDetail: vi.fn(),
  deleteAsset: vi.fn(),
  reindexAsset: vi.fn(),
  createAsset: vi.fn(),
  getAccessibleRepos: vi.fn(),
}));

// Mock toast context
const mockToast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => mockToast,
}));

import { listAssets, getAssetDetail, deleteAsset, reindexAsset } from '@/services/knowledge';

const mockListAssets = listAssets as ReturnType<typeof vi.fn>;
const mockGetAssetDetail = getAssetDetail as ReturnType<typeof vi.fn>;
const mockDeleteAsset = deleteAsset as ReturnType<typeof vi.fn>;
const mockReindexAsset = reindexAsset as ReturnType<typeof vi.fn>;

const mockAsset1 = {
  id: 'asset-001',
  assetType: 'repo',
  sourceRef: 'https://github.com/acme/my-service',
  displayName: 'acme/my-service',
  tags: {},
  metadata: {},
  tenantId: 'org-001',
  ownerSub: null,
  projectId: null,
  status: 'indexed',
  lastError: null,
  retryCount: 0,
  registeredBy: 'user-001',
  createdAt: '2026-06-20T10:00:00Z',
  updatedAt: '2026-06-20T11:00:00Z',
};

const mockAsset2 = {
  id: 'asset-002',
  assetType: 'url',
  sourceRef: 'https://docs.example.com/api',
  displayName: 'API Docs',
  tags: {},
  metadata: {},
  tenantId: 'org-001',
  ownerSub: 'user-001',
  projectId: null,
  status: 'failed',
  lastError: 'Timeout during crawl',
  retryCount: 2,
  registeredBy: 'user-001',
  createdAt: '2026-06-19T08:00:00Z',
  updatedAt: '2026-06-19T09:00:00Z',
};

const mockListResponse = {
  items: [mockAsset1, mockAsset2],
  total: 2,
  page: 1,
  pageSize: 20,
  hasMore: false,
  quota: {
    repos: { used: 1, limit: 20 },
    urls: { used: 1, limit: 50 },
    docs: { used: 0, limit: 20 },
  },
};

function renderKnowledge() {
  return render(
    <MemoryRouter>
      <Knowledge />
    </MemoryRouter>,
  );
}

describe('Knowledge Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListAssets.mockResolvedValue(mockListResponse);
    mockGetAssetDetail.mockResolvedValue(mockAsset1);
    mockDeleteAsset.mockResolvedValue(undefined);
    mockReindexAsset.mockResolvedValue({ ...mockAsset1, status: 'registered' });
  });

  it('renders the page title and description', async () => {
    renderKnowledge();

    expect(screen.getByText('Knowledge')).toBeInTheDocument();
    expect(
      screen.getByText('Manage knowledge assets — repos, URLs, and documents.'),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(mockListAssets).toHaveBeenCalledTimes(1);
    });
  });

  it('renders the Add Asset button', async () => {
    renderKnowledge();

    await waitFor(() => {
      expect(mockListAssets).toHaveBeenCalled();
    });

    expect(screen.getByText('Add Asset')).toBeInTheDocument();
  });

  it('renders asset list with display names', async () => {
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('acme/my-service')).toBeInTheDocument();
    });

    expect(screen.getByText('API Docs')).toBeInTheDocument();
  });

  it('shows status badges on assets', async () => {
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('indexed')).toBeInTheDocument();
    });

    expect(screen.getByText('failed')).toBeInTheDocument();
  });

  it('shows asset count in footer', async () => {
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('2 assets')).toBeInTheDocument();
    });
  });

  it('shows quota info', async () => {
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('Repos: 1/20')).toBeInTheDocument();
    });
    expect(screen.getByText('URLs: 1/50')).toBeInTheDocument();
    expect(screen.getByText('Docs: 0/20')).toBeInTheDocument();
  });

  it('shows empty state when no assets', async () => {
    mockListAssets.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      pageSize: 20,
      hasMore: false,
      quota: null,
    });

    renderKnowledge();

    await waitFor(() => {
      expect(
        screen.getByText('No assets yet. Click "Add Asset" to get started.'),
      ).toBeInTheDocument();
    });
  });

  it('shows "Select an asset" message when nothing is selected', async () => {
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('Select an asset to view details')).toBeInTheDocument();
    });
  });

  it('loads and displays asset detail when an asset is clicked', async () => {
    const user = userEvent.setup();
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('acme/my-service')).toBeInTheDocument();
    });

    // Click on first asset
    await user.click(screen.getByText('acme/my-service'));

    await waitFor(() => {
      expect(mockGetAssetDetail).toHaveBeenCalledWith('asset-001');
    });

    // Detail pane should show asset info
    await waitFor(() => {
      // The detail has a heading with the display name
      expect(screen.getAllByText('acme/my-service').length).toBeGreaterThanOrEqual(1);
    });
  });

  it('shows project context stub', async () => {
    renderKnowledge();

    expect(screen.getByText('Project Context')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Project grouping and context will be available in a future release (#1728).',
      ),
    ).toBeInTheDocument();
  });

  it('shows scope tabs (All, Personal, Tenant)', async () => {
    renderKnowledge();

    expect(screen.getByRole('tab', { name: 'All' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Personal' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Tenant' })).toBeInTheDocument();
  });

  it('filters by scope when tab changes', async () => {
    const user = userEvent.setup();
    renderKnowledge();

    await waitFor(() => {
      expect(mockListAssets).toHaveBeenCalledTimes(1);
    });

    // Click "Personal" tab
    await user.click(screen.getByRole('tab', { name: 'Personal' }));

    await waitFor(() => {
      expect(mockListAssets).toHaveBeenCalledWith(
        expect.objectContaining({ scope: 'personal' }),
      );
    });
  });

  it('handles delete action', async () => {
    const user = userEvent.setup();
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('acme/my-service')).toBeInTheDocument();
    });

    // Click asset to select it
    await user.click(screen.getByText('acme/my-service'));

    await waitFor(() => {
      expect(mockGetAssetDetail).toHaveBeenCalled();
    });

    // Wait for detail to render, then click Remove
    await waitFor(() => {
      expect(screen.getByText('Remove')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Remove'));

    await waitFor(() => {
      expect(mockDeleteAsset).toHaveBeenCalledWith('asset-001');
    });
    expect(mockToast.success).toHaveBeenCalledWith('Asset removed');
  });

  it('handles reindex action', async () => {
    const user = userEvent.setup();
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('acme/my-service')).toBeInTheDocument();
    });

    await user.click(screen.getByText('acme/my-service'));

    await waitFor(() => {
      expect(screen.getByText('Reindex')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Reindex'));

    await waitFor(() => {
      expect(mockReindexAsset).toHaveBeenCalledWith('asset-001');
    });
    expect(mockToast.success).toHaveBeenCalledWith('Asset re-queued for indexing');
  });

  it('shows error state when list fails', async () => {
    mockListAssets.mockRejectedValue(new Error('Network error'));

    renderKnowledge();

    await waitFor(() => {
      expect(mockToast.error).toHaveBeenCalledWith('Network error');
    });
  });

  it('shows Load More when hasMore is true', async () => {
    mockListAssets.mockResolvedValue({
      ...mockListResponse,
      hasMore: true,
    });

    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('Load More')).toBeInTheDocument();
    });
  });
});
