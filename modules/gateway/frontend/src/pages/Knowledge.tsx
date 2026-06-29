/**
 * Knowledge Management Page — three-zone layout (NotebookLM-inspired).
 *
 * Issue #1794 (Story E of E10 #1736):
 * - Left rail: asset list with scope tabs + status chips + add button
 * - Center: asset detail panel
 * - Right: project context stub (until #1728)
 */

import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Tabs, TabsList, Tab, TabPanel } from '@/components/ui/Tabs';
import { AddAssetDialog } from '@/components/knowledge/AddAssetDialog';
import { BulkUploadDialog } from '@/components/knowledge/BulkUploadDialog';
import { AssetStatusChips } from '@/components/knowledge/AssetStatusChips';
import { listAssets, getAssetDetail, deleteAsset, reindexAsset } from '@/services/knowledge';
import { useToast } from '@/contexts/ToastContext';
import type { KnowledgeAsset, AssetQuotaInfo } from '@/types';

// ---------------------------------------------------------------------------
// Status chip variant mapping
// ---------------------------------------------------------------------------

function statusVariant(status: string): 'success' | 'warning' | 'danger' | 'info' | 'default' {
  switch (status) {
    case 'indexed':
      return 'success';
    case 'queued':
    case 'indexing':
      return 'info';
    case 'failed':
      return 'danger';
    case 'registered':
      return 'warning';
    default:
      return 'default';
  }
}

function assetTypeIcon(type: string): string {
  switch (type) {
    case 'repo':
      return '📦';
    case 'url':
      return '🔗';
    case 'doc':
      return '📄';
    default:
      return '📁';
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '-';
  return new Date(dateStr).toLocaleDateString();
}

// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------

export default function Knowledge() {
  const toast = useToast();
  const toastRef = useRef(toast);
  toastRef.current = toast;

  // Asset list state
  const [assets, setAssets] = useState<KnowledgeAsset[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [quota, setQuota] = useState<AssetQuotaInfo | null>(null);

  // Scope filter (tab)
  const [scopeFilter, setScopeFilter] = useState<string | undefined>(undefined);

  // Selected asset for detail
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedAsset, setSelectedAsset] = useState<KnowledgeAsset | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Add dialog
  const [showAddDialog, setShowAddDialog] = useState(false);
  const [showBulkUpload, setShowBulkUpload] = useState(false);

  // Load assets
  const loadAssets = useCallback(async () => {
    setIsLoading(true);
    try {
      const response = await listAssets({
        scope: scopeFilter,
        page,
        pageSize: 20,
      });
      setAssets(response.items);
      setTotal(response.total);
      setHasMore(response.hasMore);
      setQuota(response.quota);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load assets';
      toastRef.current.error(message);
    } finally {
      setIsLoading(false);
    }
  }, [scopeFilter, page]);

  useEffect(() => {
    loadAssets();
  }, [loadAssets]);

  // Load asset detail
  useEffect(() => {
    if (!selectedAssetId) {
      setSelectedAsset(null);
      return;
    }
    setDetailLoading(true);
    getAssetDetail(selectedAssetId)
      .then(setSelectedAsset)
      .catch(() => {
        setSelectedAsset(null);
        toastRef.current.error('Failed to load asset detail');
      })
      .finally(() => setDetailLoading(false));
  }, [selectedAssetId]);

  // Handlers
  const handleScopeChange = (value: string) => {
    setScopeFilter(value === 'all' ? undefined : value);
    setPage(1);
    setSelectedAssetId(null);
  };

  const handleAssetAdded = () => {
    loadAssets();
    toastRef.current.success('Asset added successfully');
  };

  const handleDelete = async (assetId: string) => {
    try {
      await deleteAsset(assetId);
      toastRef.current.success('Asset removed');
      if (selectedAssetId === assetId) {
        setSelectedAssetId(null);
      }
      loadAssets();
    } catch {
      toastRef.current.error('Failed to remove asset');
    }
  };

  const handleReindex = async (assetId: string) => {
    try {
      const updated = await reindexAsset(assetId);
      toastRef.current.success('Asset re-queued for indexing');
      if (selectedAssetId === assetId) {
        setSelectedAsset(updated);
      }
      loadAssets();
    } catch {
      toastRef.current.error('Failed to reindex asset');
    }
  };

  // Refresh asset list when ingestion status transitions (live polling callback)
  const handleStatusChange = useCallback(
    (_oldStatus: string | null, newStatus: string | null) => {
      // Refresh the list to update the badge when a terminal state is reached
      // or when the status transitions (queued→indexing, indexing→indexed, etc.)
      if (newStatus) {
        loadAssets();
      }
    },
    [loadAssets],
  );

  // Determine if the selected asset is in a non-terminal state (should poll)
  const shouldPollDetail = useMemo(() => {
    if (!selectedAsset) return false;
    const terminalStatuses = new Set(['indexed', 'failed', 'removed']);
    return !terminalStatuses.has(selectedAsset.status);
  }, [selectedAsset]);

  return (
    <div className="h-full flex flex-col">
      {/* Page header */}
      <div className="flex justify-between items-center mb-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Knowledge</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Manage knowledge assets — repos, URLs, and documents.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => setShowBulkUpload(true)}>
            Bulk Upload
          </Button>
          <Button onClick={() => setShowAddDialog(true)}>Add Asset</Button>
        </div>
      </div>

      {/* Three-zone layout */}
      <div className="flex-1 grid grid-cols-12 gap-4 min-h-0">
        {/* LEFT RAIL: Asset list */}
        <div className="col-span-4 flex flex-col border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          {/* Scope tabs */}
          <Tabs defaultValue="all" onChange={handleScopeChange}>
            <TabsList className="px-2">
              <Tab value="all">All</Tab>
              <Tab value="personal">Personal</Tab>
              <Tab value="tenant">Tenant</Tab>
            </TabsList>

            <TabPanel value="all" className="!py-0"><span /></TabPanel>
            <TabPanel value="personal" className="!py-0"><span /></TabPanel>
            <TabPanel value="tenant" className="!py-0"><span /></TabPanel>
          </Tabs>

          {/* Quota bar */}
          {quota && (
            <div className="px-3 py-1.5 text-xs text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700 flex gap-3">
              {quota.repos && (
                <span>
                  Repos: {quota.repos.used}/{quota.repos.limit}
                </span>
              )}
              {quota.urls && (
                <span>
                  URLs: {quota.urls.used}/{quota.urls.limit}
                </span>
              )}
              {quota.docs && (
                <span>
                  Docs: {quota.docs.used}/{quota.docs.limit}
                </span>
              )}
            </div>
          )}

          {/* Asset list */}
          <div className="flex-1 overflow-y-auto">
            {isLoading && (
              <div className="flex justify-center py-8">
                <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary-600" />
              </div>
            )}

            {!isLoading && assets.length === 0 && (
              <div className="text-center py-8 px-4">
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  No assets yet. Click "Add Asset" to get started.
                </p>
              </div>
            )}

            {!isLoading &&
              assets.map((asset) => (
                <button
                  key={asset.id}
                  type="button"
                  className={`w-full text-left px-3 py-2.5 border-b border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors ${
                    selectedAssetId === asset.id
                      ? 'bg-primary-50 dark:bg-primary-900/20 border-l-2 border-l-primary-500'
                      : ''
                  }`}
                  onClick={() => setSelectedAssetId(asset.id)}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-base" aria-hidden="true">
                      {assetTypeIcon(asset.assetType)}
                    </span>
                    <span className="flex-1 truncate text-sm font-medium text-gray-900 dark:text-white">
                      {asset.displayName || asset.sourceRef}
                    </span>
                    <Badge variant={statusVariant(asset.status)} size="sm">
                      {asset.status}
                    </Badge>
                  </div>
                  <div className="ml-7 mt-0.5 text-xs text-gray-500 dark:text-gray-400 truncate">
                    {asset.sourceRef}
                  </div>
                </button>
              ))}

            {/* Pagination */}
            {hasMore && !isLoading && (
              <div className="p-3 text-center">
                <Button variant="secondary" size="sm" onClick={() => setPage(page + 1)}>
                  Load More
                </Button>
              </div>
            )}
          </div>

          {/* List footer */}
          {!isLoading && total > 0 && (
            <div className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400 border-t border-gray-200 dark:border-gray-700">
              {total} asset{total !== 1 ? 's' : ''}
            </div>
          )}
        </div>

        {/* CENTER: Asset detail */}
        <div className="col-span-5 border border-gray-200 dark:border-gray-700 rounded-lg overflow-y-auto">
          {!selectedAssetId && (
            <div className="flex items-center justify-center h-full text-gray-400 dark:text-gray-500">
              <p className="text-sm">Select an asset to view details</p>
            </div>
          )}

          {selectedAssetId && detailLoading && (
            <div className="flex justify-center py-8">
              <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary-600" />
            </div>
          )}

          {selectedAsset && !detailLoading && (
            <div className="p-4 space-y-4">
              {/* Header */}
              <div className="flex items-start justify-between">
                <div>
                  <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                    {selectedAsset.displayName || selectedAsset.sourceRef}
                  </h2>
                  <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
                    {selectedAsset.sourceRef}
                  </p>
                </div>
                <Badge variant={statusVariant(selectedAsset.status)}>
                  {selectedAsset.status}
                </Badge>
              </div>

              {/* Metadata grid */}
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Type</span>
                  <p className="font-medium text-gray-900 dark:text-white capitalize">
                    {selectedAsset.assetType}
                  </p>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Scope</span>
                  <p className="font-medium text-gray-900 dark:text-white">
                    {selectedAsset.ownerSub ? 'Personal' : 'Tenant'}
                  </p>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Created</span>
                  <p className="font-medium text-gray-900 dark:text-white">
                    {formatDate(selectedAsset.createdAt)}
                  </p>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Updated</span>
                  <p className="font-medium text-gray-900 dark:text-white">
                    {formatDate(selectedAsset.updatedAt)}
                  </p>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Retries</span>
                  <p className="font-medium text-gray-900 dark:text-white">
                    {selectedAsset.retryCount}
                  </p>
                </div>
                <div>
                  <span className="text-gray-500 dark:text-gray-400">Registered By</span>
                  <p className="font-medium text-gray-900 dark:text-white truncate">
                    {selectedAsset.registeredBy || '-'}
                  </p>
                </div>
              </div>

              {/* Index status chips (Story G — per-tool status; Story 3 #2309 — all types; Story 5 #2310 — live) */}
              <div>
                <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">
                  Indexing Status
                </p>
                <AssetStatusChips
                  assetId={selectedAsset.id}
                  assetType={selectedAsset.assetType}
                  enablePolling={shouldPollDetail}
                  onStatusChange={handleStatusChange}
                />
              </div>

              {/* Error display */}
              {selectedAsset.lastError && (
                <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-3">
                  <p className="text-xs font-medium text-red-800 dark:text-red-200 mb-1">
                    Last Error
                  </p>
                  <p className="text-sm text-red-700 dark:text-red-300">
                    {selectedAsset.lastError}
                  </p>
                </div>
              )}

              {/* Actions */}
              <div className="flex gap-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => handleReindex(selectedAsset.id)}
                >
                  Reindex
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => handleDelete(selectedAsset.id)}
                >
                  Remove
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* RIGHT: Project context (stub) */}
        <div className="col-span-3 border border-gray-200 dark:border-gray-700 rounded-lg overflow-y-auto">
          <div className="p-4">
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
              Project Context
            </h3>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Project grouping and context will be available in a future release (#1728).
            </p>
          </div>
        </div>
      </div>

      {/* Add asset dialog */}
      <AddAssetDialog
        isOpen={showAddDialog}
        onClose={() => setShowAddDialog(false)}
        onAssetAdded={handleAssetAdded}
        scope={scopeFilter === 'tenant' ? 'tenant' : 'personal'}
      />

      {/* Bulk upload dialog */}
      <BulkUploadDialog
        isOpen={showBulkUpload}
        onClose={() => setShowBulkUpload(false)}
        onAssetsAdded={handleAssetAdded}
        scope={scopeFilter === 'tenant' ? 'tenant' : 'personal'}
      />
    </div>
  );
}
