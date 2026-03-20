import { Card, CardTitle, StatusBadge, Button } from '@/components/ui';
import { formatRelativeTime } from '@/utils/format';
import type { PoolStatus } from '@/types';

export interface PoolHealthWidgetProps {
  poolStatus: PoolStatus;
  onRefresh?: () => void;
  isRefreshing?: boolean;
}

export function PoolHealthWidget({ poolStatus, onRefresh, isRefreshing }: PoolHealthWidgetProps) {
  const healthPercentage = poolStatus.totalAccounts > 0
    ? Math.round((poolStatus.healthyAccounts / poolStatus.totalAccounts) * 100)
    : 0;

  const getHealthColor = () => {
    if (healthPercentage >= 80) return 'bg-green-500';
    if (healthPercentage >= 50) return 'bg-yellow-500';
    return 'bg-red-500';
  };

  return (
    <Card>
      <div className="flex items-center justify-between mb-4">
        <CardTitle>Pool Health</CardTitle>
        {onRefresh && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onRefresh}
            isLoading={isRefreshing}
          >
            Refresh
          </Button>
        )}
      </div>

      {/* Health bar */}
      <div className="mb-6">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-gray-600 dark:text-gray-400">Overall Health</span>
          <span className="text-sm font-semibold text-gray-900 dark:text-white">
            {healthPercentage}%
          </span>
        </div>
        <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
          <div
            className={`${getHealthColor()} h-3 rounded-full transition-all duration-300`}
            style={{ width: `${healthPercentage}%` }}
          />
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="text-center">
          <p className="text-2xl font-bold text-gray-900 dark:text-white">
            {poolStatus.totalAccounts}
          </p>
          <p className="text-xs text-gray-500 dark:text-gray-400">Total</p>
        </div>
        <div className="text-center">
          <p className="text-2xl font-bold text-green-600">{poolStatus.healthyAccounts}</p>
          <p className="text-xs text-gray-500 dark:text-gray-400">Healthy</p>
        </div>
        <div className="text-center">
          <p className="text-2xl font-bold text-red-600">{poolStatus.unhealthyAccounts}</p>
          <p className="text-xs text-gray-500 dark:text-gray-400">Unhealthy</p>
        </div>
      </div>

      {/* Account list */}
      <div className="space-y-2 max-h-48 overflow-y-auto">
        {poolStatus.accounts.slice(0, 5).map((account) => (
          <div
            key={account.id}
            className="flex items-center justify-between py-2 px-3 bg-gray-50 dark:bg-gray-800 rounded-lg"
          >
            <div>
              <p className="text-sm font-medium text-gray-900 dark:text-white">
                {account.accountId}
              </p>
              <p className="text-xs text-gray-500 dark:text-gray-400">{account.region}</p>
            </div>
            <div className="flex items-center gap-2">
              <StatusBadge status={account.isHealthy ? 'healthy' : 'unhealthy'} />
              {account.lastHealthCheck && (
                <span className="text-xs text-gray-500">
                  {formatRelativeTime(account.lastHealthCheck)}
                </span>
              )}
            </div>
          </div>
        ))}
        {poolStatus.accounts.length > 5 && (
          <p className="text-sm text-center text-gray-500 dark:text-gray-400">
            +{poolStatus.accounts.length - 5} more accounts
          </p>
        )}
      </div>
    </Card>
  );
}
