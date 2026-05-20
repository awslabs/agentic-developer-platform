import { useQuery } from '@tanstack/react-query';
import { StatCard } from '@/components/dashboard/StatCard';
import { OrgCard } from '@/components/dashboard/OrgCard';
import { PoolHealthWidget } from '@/components/dashboard/PoolHealthWidget';
import { SystemMetricsWidget } from '@/components/dashboard/SystemMetricsWidget';
import { CardSkeleton, TableSkeleton } from '@/components/LoadingScreen';
import { Alert } from '@/components/ui';
import { getPlatformDashboard, getSystemMetrics } from '@/services/dashboard';
import { triggerHealthCheck } from '@/services/pool';
import { formatCurrency, formatNumber, formatPercent } from '@/utils/format';
import { usePermissions } from '@/hooks/usePermissions';
import { useState } from 'react';

export default function PlatformDashboard() {
  const { canViewPool, canViewMetrics } = usePermissions();
  const [isRefreshing, setIsRefreshing] = useState(false);

  const {
    data: dashboard,
    isLoading: isDashboardLoading,
    error: dashboardError,
    refetch: refetchDashboard,
  } = useQuery({
    queryKey: ['platformDashboard'],
    queryFn: getPlatformDashboard,
    refetchInterval: 60000, // Refresh every minute
  });

  const {
    data: metrics,
    isLoading: isMetricsLoading,
  } = useQuery({
    queryKey: ['systemMetrics'],
    queryFn: getSystemMetrics,
    enabled: canViewMetrics(),
    refetchInterval: 30000, // Refresh every 30 seconds
  });

  const handleRefreshPool = async () => {
    setIsRefreshing(true);
    try {
      await triggerHealthCheck();
      await refetchDashboard();
    } finally {
      setIsRefreshing(false);
    }
  };

  if (dashboardError) {
    return (
      <Alert variant="error" title="Error loading dashboard">
        {dashboardError instanceof Error ? dashboardError.message : 'Failed to load dashboard data'}
      </Alert>
    );
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Platform Dashboard</h1>
        <p className="text-gray-500 dark:text-gray-400 mt-1">
          Overview of the platform
        </p>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {isDashboardLoading ? (
          <>
            <CardSkeleton />
            <CardSkeleton />
            <CardSkeleton />
            <CardSkeleton />
          </>
        ) : dashboard ? (
          <>
            <StatCard
              title="Organizations"
              value={dashboard.totalOrganizations}
              icon="🏢"
            />
            <StatCard
              title="Requests (24h)"
              value={formatNumber(dashboard.totalRequests24h)}
              icon="📊"
            />
            <StatCard
              title="Tokens (24h)"
              value={formatNumber(dashboard.totalTokens24h)}
              icon="🔤"
            />
            <StatCard
              title="Cost (24h)"
              value={formatCurrency(dashboard.totalCost24h)}
              icon="💰"
            />
          </>
        ) : null}
      </div>

      {/* Secondary Stats */}
      {!isDashboardLoading && dashboard && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <StatCard
            title="Active Users (24h)"
            value={formatNumber(dashboard.activeUsers24h)}
            icon="👥"
          />
          <StatCard
            title="Error Rate (24h)"
            value={formatPercent(dashboard.errorRate24h)}
            icon={dashboard.errorRate24h > 5 ? '⚠️' : '✅'}
          />
          <StatCard
            title="Pool Health"
            value={`${dashboard.poolStatus?.healthyAccounts ?? 0}/${dashboard.poolStatus?.totalAccounts ?? 0}`}
            icon="🔄"
            subtitle="Healthy accounts"
          />
        </div>
      )}

      {/* Pool and Metrics Section */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
        {canViewPool() && dashboard?.poolStatus && (
          <PoolHealthWidget
            poolStatus={dashboard.poolStatus}
            onRefresh={handleRefreshPool}
            isRefreshing={isRefreshing}
          />
        )}

        {canViewMetrics() && (
          isMetricsLoading ? (
            <CardSkeleton />
          ) : metrics ? (
            <SystemMetricsWidget metrics={metrics} />
          ) : null
        )}
      </div>

      {/* Top Organizations */}
      <div>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
          Top Organizations (24h)
        </h2>
        {isDashboardLoading ? (
          <TableSkeleton rows={5} />
        ) : dashboard && dashboard.topOrganizations.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {dashboard.topOrganizations.map((org) => (
              <OrgCard
                key={org.id}
                id={org.id}
                name={org.name}
                requestCount={org.requestCount}
                tokenCount={org.tokenCount}
                costUsd={org.costUsd}
              />
            ))}
          </div>
        ) : (
          <div className="text-center py-8 text-gray-500 dark:text-gray-400">
            No organizations found
          </div>
        )}
      </div>
    </div>
  );
}
