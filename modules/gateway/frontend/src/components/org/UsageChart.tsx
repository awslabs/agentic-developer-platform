import { Card, CardTitle } from '@/components/ui';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import { formatCurrency, formatNumber } from '@/utils/format';
import type { UsageDataPoint } from '@/types';

export interface UsageChartProps {
  data: UsageDataPoint[];
  title?: string;
  className?: string;
  showCost?: boolean;
  showRequests?: boolean;
  showTokens?: boolean;
}

export function UsageChart({
  data,
  title = 'Usage Over Time',
  className = '',
  showCost = true,
  showRequests = true,
  showTokens = false,
}: UsageChartProps) {
  const chartData = data.map((point) => ({
    ...point,
    date: new Date(point.timestamp).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
    }),
  }));

  return (
    <Card className={className}>
      <CardTitle>{title}</CardTitle>
      <div className="h-80 mt-4">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={chartData}
            margin={{ top: 5, right: 30, left: 20, bottom: 5 }}
          >
            <CartesianGrid strokeDasharray="3 3" className="stroke-gray-200 dark:stroke-gray-700" />
            <XAxis
              dataKey="date"
              className="text-xs"
              tick={{ fill: '#6B7280' }}
            />
            <YAxis
              yAxisId="left"
              className="text-xs"
              tick={{ fill: '#6B7280' }}
              tickFormatter={(value) => formatNumber(value)}
            />
            {showCost && (
              <YAxis
                yAxisId="right"
                orientation="right"
                className="text-xs"
                tick={{ fill: '#6B7280' }}
                tickFormatter={(value) => `$${value}`}
              />
            )}
            <Tooltip
              contentStyle={{
                backgroundColor: 'var(--tooltip-bg, #1F2937)',
                border: 'none',
                borderRadius: '8px',
                color: '#F9FAFB',
              }}
              formatter={(value: number, name: string) => {
                if (name === 'costUsd') return [formatCurrency(value), 'Cost'];
                if (name === 'requestCount') return [formatNumber(value), 'Requests'];
                if (name === 'tokenCount') return [formatNumber(value), 'Tokens'];
                return [value, name];
              }}
            />
            <Legend />
            {showRequests && (
              <Line
                yAxisId="left"
                type="monotone"
                dataKey="requestCount"
                name="Requests"
                stroke="#3B82F6"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            )}
            {showTokens && (
              <Line
                yAxisId="left"
                type="monotone"
                dataKey="tokenCount"
                name="Tokens"
                stroke="#8B5CF6"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            )}
            {showCost && (
              <Line
                yAxisId="right"
                type="monotone"
                dataKey="costUsd"
                name="Cost"
                stroke="#10B981"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}

// Simple stats display for when charts aren't needed
export function UsageStats({
  requestCount,
  tokenCount,
  costUsd,
  className = '',
}: {
  requestCount: number;
  tokenCount: number;
  costUsd: number;
  className?: string;
}) {
  return (
    <div className={`grid grid-cols-3 gap-4 ${className}`}>
      <div className="text-center p-4 bg-gray-50 dark:bg-gray-800 rounded-lg">
        <p className="text-2xl font-bold text-gray-900 dark:text-white">
          {formatNumber(requestCount)}
        </p>
        <p className="text-sm text-gray-500 dark:text-gray-400">Requests</p>
      </div>
      <div className="text-center p-4 bg-gray-50 dark:bg-gray-800 rounded-lg">
        <p className="text-2xl font-bold text-gray-900 dark:text-white">
          {formatNumber(tokenCount)}
        </p>
        <p className="text-sm text-gray-500 dark:text-gray-400">Tokens</p>
      </div>
      <div className="text-center p-4 bg-gray-50 dark:bg-gray-800 rounded-lg">
        <p className="text-2xl font-bold text-gray-900 dark:text-white">
          {formatCurrency(costUsd)}
        </p>
        <p className="text-sm text-gray-500 dark:text-gray-400">Cost</p>
      </div>
    </div>
  );
}
