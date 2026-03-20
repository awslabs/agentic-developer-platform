import { Card } from '@/components/ui';
import type { ReactNode } from 'react';

export interface StatCardProps {
  title: string;
  value: string | number;
  icon?: ReactNode;
  change?: {
    value: number;
    type: 'increase' | 'decrease';
  };
  subtitle?: string;
  className?: string;
}

export function StatCard({ title, value, icon, change, subtitle, className = '' }: StatCardProps) {
  return (
    <Card className={className}>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm font-medium text-gray-500 dark:text-gray-400">{title}</p>
          <p className="mt-2 text-3xl font-bold text-gray-900 dark:text-white">{value}</p>
          {subtitle && (
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{subtitle}</p>
          )}
          {change && (
            <p
              className={`mt-1 text-sm ${
                change.type === 'increase' ? 'text-green-600' : 'text-red-600'
              }`}
            >
              {change.type === 'increase' ? '↑' : '↓'} {Math.abs(change.value)}%{' '}
              <span className="text-gray-500">from yesterday</span>
            </p>
          )}
        </div>
        {icon && (
          <div className="p-3 bg-primary-100 dark:bg-primary-900 rounded-lg text-2xl">
            {icon}
          </div>
        )}
      </div>
    </Card>
  );
}
