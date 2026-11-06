/**
 * Component tests for the Context page.
 *
 * Issue #1794 (Story E of E10 #1736).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import Knowledge from '@/pages/Knowledge';

// Mock the knowledge service
vi.mock('@/services/knowledge', () => ({
  listAssets: vi.fn(),
  searchKnowledge: vi.fn(),
  getAssetDetail: vi.fn(),
  deleteAsset: vi.fn(),
  reindexAsset: vi.fn(),
  createAsset: vi.fn(),
  getAccessibleRepos: vi.fn(),
  getAssetStatus: vi.fn(),
}));

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'user-001', orgId: 'org-001' } }) }));

// Mock toast context
const mockToast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => mockToast,
}));

import { listAssets, searchKnowledge, getAssetDetail, deleteAsset, reindexAsset, getAssetStatus } from '@/services/knowledge';

const mockListAssets = listAssets as ReturnType<typeof vi.fn>;
const mockSearchKnowledge = vi.mocked(searchKnowledge);
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
  productId: null,
  canManage: true,
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
  productId: null,
  canManage: true,
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

function renderKnowledge(productId?: string) {
  return render(
    <MemoryRouter>
      <Knowledge productId={productId} />
    </MemoryRouter>,
  );
}

describe('Context page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListAssets.mockResolvedValue(mockListResponse);
    mockSearchKnowledge.mockResolvedValue({ items: [{ asset: mockAsset2, excerpt: 'Deploy changes in stages.', heading: 'Deployment' }], query: 'API' });
    mockGetAssetDetail.mockResolvedValue(mockAsset1);
    mockDeleteAsset.mockResolvedValue(undefined);
    mockReindexAsset.mockResolvedValue({ ...mockAsset1, status: 'registered' });
    vi.mocked(getAssetStatus).mockResolvedValue({
      assetId: 'asset-001', sourceRef: mockAsset1.sourceRef, repoFound: true,
      runId: 'run-001', runStatus: 'completed', runStartedAt: null,
      stages: [{ stage: 'clone', status: 'verified', artifactRef: null, error: null,
        startedAt: null, completedAt: null, metrics: { files: 50 }, workerPod: null }],
    });
  });

  it('renders the page title and description', async () => {
    renderKnowledge();

    expect(screen.getByRole('heading', { name: 'Context', exact: true })).toBeInTheDocument();
    expect(
      screen.getByText('Your personal information, available only to you.'),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(mockListAssets).toHaveBeenCalledTimes(1);
    });
  });

  it('renders the Add information button', async () => {
    renderKnowledge();

    await waitFor(() => {
      expect(mockListAssets).toHaveBeenCalled();
    });

    expect(screen.getByText('Add information')).toBeInTheDocument();
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
      expect(screen.getByText('2 items')).toBeInTheDocument();
    });
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
        screen.getByText('No information yet. Add a link to get started.'),
      ).toBeInTheDocument();
    });
  });

  it('shows "Select an asset" message when nothing is selected', async () => {
    renderKnowledge();

    await waitFor(() => {
      expect(screen.getByText('Select information to view details')).toBeInTheDocument();
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

  it('omits the unavailable project context panel', async () => {
    renderKnowledge();

    await screen.findByText('acme/my-service');
    expect(screen.queryByText('Project Context')).not.toBeInTheDocument();
    expect(screen.queryByText(/future release/)).not.toBeInTheDocument();
  });

  it('shows completed indexing results for an indexed asset', async () => {
    const user = userEvent.setup();
    renderKnowledge();
    await user.click(await screen.findByText('acme/my-service'));

    expect(await screen.findByTestId('stage-chip-clone')).toHaveTextContent('verified:');
    expect(screen.getByTestId('stage-metrics-clone')).toHaveTextContent('50 files');
    expect(screen.queryByText('Not yet indexed')).not.toBeInTheDocument();
    expect(screen.queryByTestId('polling-indicator')).not.toBeInTheDocument();
  });

  it('allows reindexing a partial asset and preserves its stage failures', async () => {
    const user = userEvent.setup();
    mockGetAssetDetail.mockResolvedValue({ ...mockAsset1, status: 'partial' });
    vi.mocked(getAssetStatus).mockResolvedValue({
      assetId: 'asset-001', sourceRef: mockAsset1.sourceRef, repoFound: true,
      runId: 'run-001', runStatus: 'partial', runStartedAt: null,
      stages: [
        { stage: 'clone', status: 'verified', artifactRef: null, error: null,
          startedAt: null, completedAt: null, metrics: null, workerPod: null },
        { stage: 'deepwiki', status: 'failed', artifactRef: null, error: 'Generation failed',
          startedAt: null, completedAt: null, metrics: null, workerPod: null },
      ],
    });
    renderKnowledge();
    await user.click(await screen.findByText('acme/my-service'));
    expect(await screen.findByTestId('stage-chip-deepwiki')).toHaveTextContent('failed');
    expect(screen.getByRole('button', { name: /reindex/i })).toBeEnabled();
    expect(screen.queryByTestId('polling-indicator')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /reindex/i }));
    expect(mockReindexAsset).toHaveBeenCalledWith('asset-001');
  });

  it('returns to the list and restores focus after viewing details', async () => {
    const user = userEvent.setup();
    renderKnowledge();
    await user.click(await screen.findByText('acme/my-service'));
    await screen.findByTestId('stage-chip-clone');
    await user.click(screen.getByRole('button', { name: 'Back to information' }));

    const assetButton = await screen.findByRole('button', { name: /acme\/my-service/ });
    expect(assetButton).toHaveFocus();
    expect(assetButton).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();
  });

  it('keeps the current details when an earlier asset request finishes late', async () => {
    const user = userEvent.setup();
    let resolvePrevious!: (asset: typeof mockAsset1) => void;
    mockGetAssetDetail.mockImplementationOnce(() => new Promise((resolve) => { resolvePrevious = resolve; }));
    renderKnowledge();
    await user.click(await screen.findByText('acme/my-service'));
    mockGetAssetDetail.mockResolvedValue(mockAsset2);
    await user.click(screen.getByText('API Docs'));
    await screen.findByRole('heading', { name: 'API Docs' });
    await act(async () => { resolvePrevious(mockAsset1); });

    expect(screen.getByRole('heading', { name: 'API Docs' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'acme/my-service' })).not.toBeInTheDocument();
  });

  it('can retry a failed detail request', async () => {
    const user = userEvent.setup();
    mockGetAssetDetail.mockRejectedValueOnce(new Error('Unavailable'));
    renderKnowledge();
    await user.click(await screen.findByText('acme/my-service'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load this information');
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('heading', { name: 'acme/my-service' })).toBeInTheDocument();
  });

  it('opens personal context without sharing or category controls', async () => {
    renderKnowledge();
    await screen.findByText('API Docs');
    expect(mockListAssets).toHaveBeenCalledWith(expect.objectContaining({ scope: 'personal', productId: undefined }));
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.getByRole('searchbox', { name: 'Search context' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Bulk Upload' })).not.toBeInTheDocument();
  });

  it('requests product context using Product identity', async () => {
    renderKnowledge('product-123');
    await screen.findByText('API Docs');
    expect(mockListAssets).toHaveBeenCalledWith({ scope: 'product', productId: 'product-123', search: undefined, page: 1, pageSize: 20 });
    expect(screen.getByText('Shared with the Product team and platform admins.')).toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
  });

  it('hides management actions when the server denies management', async () => {
    mockGetAssetDetail.mockResolvedValue({ ...mockAsset1, canManage: false });
    renderKnowledge('product-123');
    await userEvent.click(await screen.findByText('acme/my-service'));
    await screen.findByRole('heading', { name: 'acme/my-service' });
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reindex' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add information' })).toBeEnabled();
  });

  it('appends pages and starts a new search from page one', async () => {
    mockListAssets.mockResolvedValueOnce({ ...mockListResponse, items: [mockAsset1], hasMore: true });
    mockListAssets.mockResolvedValueOnce({ ...mockListResponse, items: [mockAsset2], page: 2 });
    renderKnowledge('product-123');
    await userEvent.click(await screen.findByRole('button', { name: 'Load More' }));
    await screen.findByText('API Docs');
    expect(screen.getByText('acme/my-service')).toBeInTheDocument();
    expect(mockListAssets).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }));
    mockListAssets.mockResolvedValue({ ...mockListResponse, items: [mockAsset2], total: 1 });
    await userEvent.type(screen.getByRole('searchbox'), 'API{Enter}');
    await waitFor(() => expect(mockSearchKnowledge).toHaveBeenLastCalledWith({ scope: 'product', productId: 'product-123', query: 'API' }));
    await waitFor(() => expect(screen.queryByText('acme/my-service')).not.toBeInTheDocument());
  });

  it('keeps focus in search when closing the selected details', async () => {
    renderKnowledge('product-123');
    await userEvent.click(await screen.findByText('acme/my-service'));
    await screen.findByRole('heading', { name: 'acme/my-service' });
    await userEvent.type(screen.getByRole('searchbox'), 'API{Enter}');
    expect(screen.getByRole('searchbox')).toHaveFocus();
    expect(screen.getByRole('searchbox')).toHaveValue('API');
    await waitFor(() => expect(mockSearchKnowledge).toHaveBeenLastCalledWith(expect.objectContaining({ query: 'API' })));
  });

  it('ignores an old Product list response after changing Products', async () => {
    let finish!: (value: typeof mockListResponse) => void;
    mockListAssets.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<Knowledge productId="old-product" />);
    mockListAssets.mockResolvedValue({ ...mockListResponse, items: [mockAsset2] });
    view.rerender(<Knowledge productId="new-product" />);
    await screen.findByText('API Docs');
    await act(async () => finish({ ...mockListResponse, items: [mockAsset1] }));
    expect(screen.queryByText('acme/my-service')).not.toBeInTheDocument();
    expect(mockListAssets).toHaveBeenLastCalledWith(expect.objectContaining({ productId: 'new-product' }));
  });

  it('clears selection and discards late detail responses when changing Products', async () => {
    let finish!: (value: typeof mockAsset1) => void;
    mockGetAssetDetail.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<Knowledge productId="old-product" />);
    await userEvent.click(await screen.findByText('acme/my-service'));
    mockListAssets.mockResolvedValue({ ...mockListResponse, items: [mockAsset2] });
    view.rerender(<Knowledge productId="new-product" />);
    await screen.findByText('API Docs');
    await act(async () => finish(mockAsset1));
    expect(screen.queryByRole('heading', { name: 'acme/my-service' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();
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
    expect(mockToast.success).toHaveBeenCalledWith('Information removed');
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
    expect(mockToast.success).toHaveBeenCalledWith('Information queued for indexing');
  });

  it('shows error state when list fails', async () => {
    mockListAssets.mockRejectedValue(new Error('Network error'));

    renderKnowledge();

    await waitFor(() => {
      expect(mockToast.error).toHaveBeenCalledWith('Network error');
    });
  });


  it('searches contents only on submission and opens a cited source', async () => {
    renderKnowledge();
    await screen.findByText('API Docs');
    await userEvent.type(screen.getByRole('searchbox'), 'API');
    expect(mockSearchKnowledge).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Search', exact: true }));
    expect(await screen.findByText('Deploy changes in stages.')).toBeInTheDocument();
    expect(mockSearchKnowledge).toHaveBeenCalledWith({ scope: 'personal', productId: undefined, query: 'API' });
    await userEvent.click(screen.getByRole('button', { name: /API Docs.*Deploy changes/s }));
    expect(mockGetAssetDetail).toHaveBeenCalledWith('asset-002');
  });

  it('clears search to restore browsing', async () => {
    renderKnowledge('product-123');
    await screen.findByText('API Docs');
    await userEvent.type(screen.getByRole('searchbox'), 'API{Enter}');
    await screen.findByText('Deploy changes in stages.');
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    await screen.findByText('acme/my-service');
    expect(screen.queryByText('Deploy changes in stages.')).not.toBeInTheDocument();
    expect(screen.getByRole('searchbox')).toHaveValue('');
  });

  it('shows search failure and retries instead of reporting no matches', async () => {
    mockSearchKnowledge.mockRejectedValueOnce({ detail: 'Content search is unavailable. Please try again.', status: 503 });
    renderKnowledge();
    await screen.findByText('API Docs');
    await userEvent.type(screen.getByRole('searchbox'), 'API{Enter}');
    expect(await screen.findByRole('alert')).toHaveTextContent('Content search is unavailable');
    expect(screen.queryByText(/No matches/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('Deploy changes in stages.')).toBeInTheDocument();
  });

  it('explains a collection search limit instead of reporting a transient outage', async () => {
    mockSearchKnowledge.mockRejectedValueOnce({ detail: { code: 'knowledge_scope_limit', message: 'This collection has too many indexed sources to search at once.' }, status: 422 });
    renderKnowledge();
    await screen.findByText('API Docs');
    await userEvent.type(screen.getByRole('searchbox'), 'API{Enter}');
    expect(await screen.findByRole('alert')).toHaveTextContent('too many indexed sources');
    expect(screen.queryByText(/Content search is unavailable/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
  });

  it('discards late content results after switching Products' , async () => {
    let finish!: (value: Awaited<ReturnType<typeof searchKnowledge>>) => void;
    mockSearchKnowledge.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<Knowledge productId="old-product" />);
    await screen.findByText('API Docs');
    await userEvent.type(screen.getByRole('searchbox'), 'API{Enter}');
    expect(await screen.findByRole('status')).toHaveAttribute('aria-label', 'Searching context');
    view.rerender(<Knowledge productId="new-product" />);
    await screen.findByText('API Docs');
    await act(async () => finish({ items: [{ asset: mockAsset1, excerpt: 'Old product confidential snippet', heading: '' }], query: 'API' }));
    expect(screen.queryByText('Old product confidential snippet')).not.toBeInTheDocument();
    expect(screen.getByRole('searchbox')).toHaveValue('');
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
