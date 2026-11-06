/**
 * Context page — responsive asset list and detail cards.
 */

import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Input } from '@/components/ui/Input';
import { useAuth } from '@/hooks/useAuth';
import { AddAssetDialog } from '@/components/knowledge/AddAssetDialog';
import { AssetStatusChips } from '@/components/knowledge/AssetStatusChips';
import { AssetTypeIcon } from '@/components/knowledge/AssetTypeIcon';
import { ProductSourcesPanel } from '@/components/knowledge/ProductSourcesPanel';
import { ProductStoragePanel } from '@/components/knowledge/ProductStoragePanel';
import { contextDocumentDownload } from '@/services/contextStorage';
import { listAssets, searchKnowledge, getAssetDetail, deleteAsset, reindexAsset } from '@/services/knowledge';
import { useToast } from '@/contexts/ToastContext';
import type { KnowledgeAsset } from '@/types';

// ---------------------------------------------------------------------------
// Status chip variant mapping
// ---------------------------------------------------------------------------

function statusVariant(status: string): 'success' | 'warning' | 'danger' | 'info' | 'default' {
  switch (status) {
    case 'indexed':
    case 'complete':
    case 'completed':
      return 'success';
    case 'queued':
    case 'indexing':
      return 'info';
    case 'failed':
      return 'danger';
    case 'partial':
    case 'registered':
      return 'warning';
    default:
      return 'default';
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '-';
  return new Date(dateStr).toLocaleDateString();
}

// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------

export default function Knowledge({ productId }: { productId?: string }) {
  const { user } = useAuth();
  return <KnowledgeContent key={`${user?.orgId}:${user?.id}:${productId ?? 'personal'}`} productId={productId} />;
}

function KnowledgeContent({ productId }: { productId?: string }) {
  const scope = productId ? 'product' : 'personal';
  const toast = useToast();
  const toastRef = useRef(toast);
  toastRef.current = toast;

  // Asset list state
  const [assets, setAssets] = useState<KnowledgeAsset[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [excerpts, setExcerpts] = useState<Record<string, { excerpt: string; heading: string }>>({});
  const [listError, setListError] = useState('');
  const [canRetry, setCanRetry] = useState(true);
  const request = useRef(0);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; request.current += 1; };
  }, []);

  // Selected asset for detail
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const selectedId = useRef(selectedAssetId);
  selectedId.current = selectedAssetId;
  const [selectedAsset, setSelectedAsset] = useState<KnowledgeAsset | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const selectedButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreListFocus = useRef(false);

  // Add dialog
  const [showAddDialog, setShowAddDialog] = useState(false);

  // Ignore responses for a previous search or a collection that was left.
  const loadAssets = useCallback(async (nextPage = 1) => {
    const attempt = ++request.current;
    setIsLoading(true);
    setListError('');
    try {
      if (query) {
        const response = await searchKnowledge({ scope, productId, query });
        if (!active.current || attempt !== request.current) return;
        setAssets(response.items.map(item => item.asset));
        setExcerpts(Object.fromEntries(response.items.map(item => [item.asset.id, { excerpt: item.excerpt, heading: item.heading }])));
        setTotal(response.items.length);
        setHasMore(false);
        setPage(1);
        return;
      }
      const response = await listAssets({ scope, productId, search: undefined, page: nextPage, pageSize: 20 });
      if (!active.current || attempt !== request.current) return;
      setAssets((previous) => nextPage === 1 ? response.items : [...previous, ...response.items.filter(item => !previous.some(existing => existing.id === item.id))]);
      setExcerpts({});
      setPage(nextPage);
      setTotal(response.total);
      setHasMore(response.hasMore);
    } catch (err: unknown) {
      if (!active.current || attempt !== request.current) return;
      const detail = (err as { detail?: unknown })?.detail;
      setCanRetry(!(typeof detail === 'object' && detail !== null && 'code' in detail && detail.code === 'knowledge_scope_limit'));
      const detailMessage = typeof detail === 'object' && detail !== null && 'message' in detail ? detail.message : undefined;
      const message = err instanceof Error ? err.message : typeof detail === 'string' ? detail : typeof detailMessage === 'string' ? detailMessage : query ? 'Content search is unavailable. Please try again.' : 'Could not load information.';
      setListError(message);
      setAssets([]);
      setExcerpts({});
      setTotal(0);
      setHasMore(false);
      setSelectedAssetId(null);
      toastRef.current.error(message);
    } finally {
      if (active.current && attempt === request.current) setIsLoading(false);
    }
  }, [scope, productId, query]);

  useEffect(() => {
    void loadAssets();
    return () => { request.current += 1; };
  }, [loadAssets]);

  // Load asset detail
  useEffect(() => {
    let cancelled = false;
    setSelectedAsset(null);
    if (!selectedAssetId) {
      setDetailLoading(false);
      if (restoreListFocus.current) selectedButtonRef.current?.focus();
      restoreListFocus.current = false;
      return;
    }
    setDetailLoading(true);
    getAssetDetail(selectedAssetId)
      .then((asset) => {
        if (!cancelled) setSelectedAsset(asset);
      })
      .catch(() => {
        if (cancelled) return;
        setSelectedAsset(null);
        toastRef.current.error('Could not load information details');
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });

    return () => { cancelled = true; };
  }, [selectedAssetId, detailAttempt]);

  useEffect(() => {
    if (selectedAsset?.id === selectedAssetId) detailHeadingRef.current?.focus();
  }, [selectedAsset?.id, selectedAssetId]);

  // Handlers
  const handleAssetAdded = () => {
    loadAssets();
    toastRef.current.success('Information added');
  };

  const handleDelete = async (assetId: string) => {
    try {
      await deleteAsset(assetId);
      if (!active.current) return;
      toastRef.current.success('Information removed');
      if (selectedId.current === assetId) {
        setSelectedAssetId(null);
      }
      loadAssets();
    } catch {
      if (!active.current) return;
      toastRef.current.error('Could not remove information');
    }
  };

  const handleReindex = async (assetId: string) => {
    try {
      const updated = await reindexAsset(assetId);
      if (!active.current) return;
      toastRef.current.success('Information queued for indexing');
      if (selectedId.current === assetId) {
        setSelectedAsset(updated);
      }
      loadAssets();
    } catch {
      if (!active.current) return;
      toastRef.current.error('Could not reindex information');
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
    const terminalStatuses = new Set(['indexed', 'complete', 'completed', 'partial', 'failed', 'removed']);
    return !terminalStatuses.has(selectedAsset.status);
  }, [selectedAsset]);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          {productId ? <h2 className="text-xl font-semibold text-gray-900 dark:text-white">Product Context</h2>
            : <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Context</h1>}
          <p className="text-gray-600 dark:text-gray-400">
            {productId ? 'Shared product information, evidence, and insights for your team and agents.' : 'Your personal information, available only to you.'}
          </p>
          {productId && <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Shared with the Product team and platform admins.</p>}
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button onClick={() => setShowAddDialog(true)}>Add information</Button>
        </div>
      </div>

      {productId && <ProductSourcesPanel key={productId} productId={productId} onChanged={() => void loadAssets()} />}
      {productId && <ProductStoragePanel key={productId} productId={productId} onChanged={() => void loadAssets()} />}

      <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <section aria-label="Information" className={`min-w-0 ${selectedAssetId ? 'hidden xl:block' : ''}`}>
          <Card padding="none" className="overflow-hidden">
            <form role="search" className="space-y-3 px-6 pt-6 pb-4" onSubmit={(event) => {
              event.preventDefault();
              const nextQuery = search.trim();
              setSelectedAssetId(null);
              if (nextQuery === query) void loadAssets();
              else setQuery(nextQuery);
            }}>
              <Input id="knowledge-search" type="search" label="Search context" value={search}
                placeholder="Ask a question or enter a phrase" maxLength={4096}
                onChange={(event) => {
                  setSearch(event.target.value);
                  setSelectedAssetId(null);
                  if (!event.target.value.trim()) setQuery('');
                }} />
              <div className="flex items-center gap-3">
                <Button type="submit" size="sm" disabled={!search.trim()}>Search</Button>
                {query && <Button type="button" variant="ghost" size="sm" onClick={() => {
                  setSearch(''); setQuery(''); setSelectedAssetId(null);
                }}>Clear search</Button>}
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400">Search inside indexed information.</p>
            </form>

            {/* Asset list */}
            <div className="max-h-[32rem] overflow-y-auto">
              {isLoading && (
                <div role="status" aria-label={query ? 'Searching context' : 'Loading information'} className="flex justify-center py-8">
                  <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary-600" />
                </div>
              )}

              {listError && <div className="space-y-3 px-6 py-6">
                <p role="alert" className="text-sm text-red-700 dark:text-red-300">{listError}</p>
                {canRetry && <Button variant="secondary" onClick={() => void loadAssets()}>Try again</Button>}
              </div>}
              {!isLoading && !listError && assets.length === 0 && (
                <div className="text-center py-8 px-4">
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    {query ? 'No matches in indexed information. Try different words.' : 'No information yet. Add a link to get started.'}
                  </p>
                </div>
              )}

              {!isLoading &&
                assets.map((asset) => (
                  <button
                    key={asset.id}
                    type="button"
                    ref={(button) => {
                      if (button && asset.id === selectedAssetId) selectedButtonRef.current = button;
                    }}
                    aria-pressed={selectedAssetId === asset.id}
                    aria-controls="knowledge-asset-detail"
                    className={`w-full text-left px-6 py-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary-500 ${
                      selectedAssetId === asset.id
                        ? 'bg-primary-50 dark:bg-primary-900/20'
                        : 'hover:bg-gray-50 dark:hover:bg-gray-700'
                    }`}
                    onClick={(event) => {
                      selectedButtonRef.current = event.currentTarget;
                      setSelectedAssetId(asset.id);
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <AssetTypeIcon type={asset.assetType} />
                      <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-900 dark:text-white">
                        {asset.displayName || asset.sourceRef}
                      </span>
                      <Badge variant={statusVariant(asset.status)} size="sm">
                        {asset.status}
                      </Badge>
                    </div>
                    <div className="ml-7 mt-0.5 text-xs text-gray-500 dark:text-gray-400 truncate">
                      {asset.sourceRef}
                    </div>
                    {query && excerpts[asset.id] && <div className="ml-7 mt-2 space-y-1 text-sm text-gray-600 dark:text-gray-300">
                      {excerpts[asset.id].heading && <p className="font-medium">{excerpts[asset.id].heading}</p>}
                      <p className="line-clamp-4 whitespace-pre-wrap break-words">{excerpts[asset.id].excerpt}</p>
                    </div>}
                  </button>
                ))}

              {/* Pagination */}
              {hasMore && !isLoading && (
                <div className="p-3 text-center">
                  <Button variant="secondary" size="sm" onClick={() => void loadAssets(page + 1)}>
                    Load More
                  </Button>
                </div>
              )}
            </div>

            {/* List footer */}
            {!isLoading && total > 0 && (
              <div className="px-6 py-3 text-sm text-gray-500 dark:text-gray-400 border-t border-gray-200 dark:border-gray-700">
                {query ? 'Top matches: ' : ''}{total} item{total !== 1 ? 's' : ''}
              </div>
            )}
          </Card>
        </section>

        <section id="knowledge-asset-detail" aria-label="Information details" className={`min-w-0 ${selectedAssetId ? '' : 'hidden xl:block'}`}>
          <Card>
            {selectedAssetId && (
              <Button variant="ghost" size="sm" className="mb-4 xl:hidden" onClick={() => { restoreListFocus.current = true; setSelectedAssetId(null); }}>
                Back to information
              </Button>
            )}
            {!selectedAssetId && (
              <div className="flex min-h-48 items-center justify-center text-gray-500 dark:text-gray-400">
                <p className="text-sm">Select information to view details</p>
              </div>
            )}

            {selectedAssetId && detailLoading && (
              <div className="flex justify-center py-8">
                <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary-600" />
              </div>
            )}

            {selectedAssetId && !detailLoading && !selectedAsset && (
              <div className="space-y-4 py-8">
                <p role="alert" className="text-sm text-red-700 dark:text-red-300">
                  Unable to load this information.
                </p>
                <Button variant="secondary" onClick={() => setDetailAttempt((attempt) => attempt + 1)}>
                  Try again
                </Button>
              </div>
            )}

            {selectedAsset && !detailLoading && (
              <div className="space-y-6">
                {/* Header */}
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <h2 ref={detailHeadingRef} tabIndex={-1} className="text-lg font-semibold text-gray-900 dark:text-white [overflow-wrap:anywhere] focus:outline-none">
                      {selectedAsset.displayName || selectedAsset.sourceRef}
                    </h2>
                    <p className="text-sm text-gray-500 dark:text-gray-400 mt-1 [overflow-wrap:anywhere]">
                      {selectedAsset.sourceRef}
                    </p>
                  </div>
                  <Badge variant={statusVariant(selectedAsset.status)}>
                    {selectedAsset.status}
                  </Badge>
                </div>

                {/* Metadata grid */}
                <div className="grid grid-cols-2 gap-4 text-sm [overflow-wrap:anywhere]">
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
                    <span className="text-gray-500 dark:text-gray-400">Added by</span>
                    <p className="font-medium text-gray-900 dark:text-white">
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
                    reportedStagesOnly
                    assetId={selectedAsset.id}
                    assetType={selectedAsset.assetType}
                    enablePolling={shouldPollDetail}
                    onStatusChange={handleStatusChange}
                  />
                </div>

                {/* Error display */}
                {selectedAsset.lastError && (
                  <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-3 [overflow-wrap:anywhere]">
                    <p className="text-xs font-medium text-red-800 dark:text-red-200 mb-1">
                      Last Error
                    </p>
                    <p className="text-sm text-red-700 dark:text-red-300">
                      {selectedAsset.lastError}
                    </p>
                  </div>
                )}

                {/* Actions */}
                {productId && selectedAsset.canDownload && <Button variant="secondary" size="sm" onClick={async () => {
                  try {
                    const url = await contextDocumentDownload(productId, selectedAsset.id);
                    if (active.current && selectedId.current === selectedAsset.id) window.location.assign(url);
                  } catch { if (active.current) toastRef.current.error('Could not download the original document'); }
                }}>Download original</Button>}
                {selectedAsset.canManage && <div className="flex flex-wrap gap-2 pt-4 border-t border-gray-200 dark:border-gray-700">
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
                </div>}
              </div>
            )}
          </Card>
        </section>
      </div>

      {/* Add asset dialog */}
      <AddAssetDialog
        isOpen={showAddDialog}
        onClose={() => setShowAddDialog(false)}
        onAssetAdded={handleAssetAdded}
        scope={scope}
        productId={productId}
      />

    </div>
  );
}
