import { useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button, Alert } from '@/components/ui';
import { LogFilters } from '@/components/logs/LogFilters';
import { LogTable } from '@/components/logs/LogTable';
import { Pagination } from '@/components/logs/Pagination';
import { getLogs, exportLogs, downloadLogs } from '@/services/logs';
import { usePermissions } from '@/hooks/usePermissions';
import { useToast } from '@/contexts/ToastContext';
import type { LogQueryRequest } from '@/types';

export default function LogViewer() {
  const { canViewLogs, canExportLogs } = usePermissions();
  const toast = useToast();

  const [filters, setFilters] = useState<LogQueryRequest>({});
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [sortBy, setSortBy] = useState<string>('timestamp');
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');
  const [isExporting, setIsExporting] = useState(false);

  // Check permissions
  if (!canViewLogs()) {
    return (
      <Alert variant="error" title="Access Denied">
        You don't have permission to view logs.
      </Alert>
    );
  }

  const {
    data: logsData,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ['logs', filters, page, pageSize],
    queryFn: () =>
      getLogs({
        ...filters,
        page,
        page_size: pageSize,
      }),
  });

  const handleFilter = useCallback((newFilters: LogQueryRequest) => {
    setFilters(newFilters);
    setPage(1); // Reset to first page when filtering
  }, []);

  const handleReset = useCallback(() => {
    setFilters({});
    setPage(1);
  }, []);

  const handleSort = useCallback((key: string) => {
    if (sortBy === key) {
      setSortOrder((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortBy(key);
      setSortOrder('desc');
    }
  }, [sortBy]);

  const handleExport = async () => {
    setIsExporting(true);
    try {
      const blob = await exportLogs(filters);
      const filename = `logs-${new Date().toISOString().split('T')[0]}.csv`;
      downloadLogs(blob, filename);
      toast.success('Logs exported successfully');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to export logs');
    } finally {
      setIsExporting(false);
    }
  };

  const handlePageSizeChange = (newPageSize: number) => {
    setPageSize(newPageSize);
    setPage(1);
  };

  if (error) {
    return (
      <Alert variant="error" title="Error loading logs">
        {error instanceof Error ? error.message : 'Failed to load logs'}
      </Alert>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Log Viewer</h1>
          <p className="text-gray-500 dark:text-gray-400 mt-1">
            View and filter API request logs
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Button variant="secondary" onClick={() => refetch()}>
            Refresh
          </Button>
          {canExportLogs() && (
            <Button onClick={handleExport} isLoading={isExporting}>
              Export CSV
            </Button>
          )}
        </div>
      </div>

      {/* Filters */}
      <LogFilters onFilter={handleFilter} onReset={handleReset} isLoading={isLoading} />

      {/* Results count */}
      {logsData && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {logsData.total} logs found
        </p>
      )}

      {/* Logs table */}
      <LogTable
        logs={logsData?.items || []}
        onSort={handleSort}
        sortBy={sortBy}
        sortOrder={sortOrder}
        isLoading={isLoading}
      />

      {/* Pagination */}
      {logsData && logsData.total > 0 && (
        <Pagination
          page={page}
          pageSize={pageSize}
          total={logsData.total}
          onPageChange={setPage}
          onPageSizeChange={handlePageSizeChange}
        />
      )}
    </div>
  );
}
