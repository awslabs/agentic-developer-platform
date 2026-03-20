import { Card, CardTitle } from '@/components/ui';
import { formatNumber, formatPercent } from '@/utils/format';
import type { SystemMetrics } from '@/types';

export interface SystemMetricsWidgetProps {
  metrics: SystemMetrics;
  className?: string;
}

export function SystemMetricsWidget({ metrics, className = '' }: SystemMetricsWidgetProps) {
  const getUtilizationColor = (percent: number) => {
    if (percent >= 90) return 'text-red-600';
    if (percent >= 70) return 'text-yellow-600';
    return 'text-green-600';
  };

  const getProgressColor = (percent: number) => {
    if (percent >= 90) return 'bg-red-500';
    if (percent >= 70) return 'bg-yellow-500';
    return 'bg-green-500';
  };

  return (
    <Card className={className}>
      <CardTitle>System Metrics</CardTitle>

      <div className="mt-4 space-y-6">
        {/* API Calls */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm text-gray-600 dark:text-gray-400">API Calls/min</span>
            <span className="text-lg font-semibold text-gray-900 dark:text-white">
              {formatNumber(metrics.apiCallsPerMinute)}
            </span>
          </div>
        </div>

        {/* Latency */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm text-gray-600 dark:text-gray-400">Avg Latency</span>
            <span className="text-lg font-semibold text-gray-900 dark:text-white">
              {metrics.averageLatencyMs}ms
            </span>
          </div>
        </div>

        {/* Error Rate */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm text-gray-600 dark:text-gray-400">Error Rate</span>
            <span
              className={`text-lg font-semibold ${
                metrics.errorRate > 5 ? 'text-red-600' : 'text-gray-900 dark:text-white'
              }`}
            >
              {formatPercent(metrics.errorRate)}
            </span>
          </div>
        </div>

        {/* Active Connections */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm text-gray-600 dark:text-gray-400">Active Connections</span>
            <span className="text-lg font-semibold text-gray-900 dark:text-white">
              {formatNumber(metrics.activeConnections)}
            </span>
          </div>
        </div>

        {/* CPU Utilization */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-gray-600 dark:text-gray-400">CPU Usage</span>
            <span className={`text-sm font-semibold ${getUtilizationColor(metrics.cpuUtilization)}`}>
              {formatPercent(metrics.cpuUtilization)}
            </span>
          </div>
          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
            <div
              className={`${getProgressColor(metrics.cpuUtilization)} h-2 rounded-full transition-all`}
              style={{ width: `${Math.min(metrics.cpuUtilization, 100)}%` }}
            />
          </div>
        </div>

        {/* Memory Utilization */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-gray-600 dark:text-gray-400">Memory Usage</span>
            <span className={`text-sm font-semibold ${getUtilizationColor(metrics.memoryUtilization)}`}>
              {formatPercent(metrics.memoryUtilization)}
            </span>
          </div>
          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
            <div
              className={`${getProgressColor(metrics.memoryUtilization)} h-2 rounded-full transition-all`}
              style={{ width: `${Math.min(metrics.memoryUtilization, 100)}%` }}
            />
          </div>
        </div>
      </div>
    </Card>
  );
}
