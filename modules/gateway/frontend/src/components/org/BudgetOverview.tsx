import { Card, CardTitle, Badge } from '@/components/ui';
import { formatCurrency, formatPercent, getBudgetUtilizationColor } from '@/utils/format';
import type { BudgetStatus } from '@/types';

export interface BudgetOverviewProps {
  budgetStatus: BudgetStatus;
  className?: string;
}

export function BudgetOverview({ budgetStatus, className = '' }: BudgetOverviewProps) {
  const utilizationPercent = Math.round(budgetStatus.budgetUtilizationPercent);
  const utilizationColor = getBudgetUtilizationColor(utilizationPercent);

  const getProgressColor = () => {
    if (utilizationPercent >= 100) return 'bg-red-500';
    if (utilizationPercent >= 80) return 'bg-yellow-500';
    return 'bg-green-500';
  };

  return (
    <Card className={className}>
      <div className="flex items-start justify-between mb-4">
        <CardTitle>Budget Status</CardTitle>
        <Badge
          variant={budgetStatus.enforcementMode === 'hard' ? 'danger' : 'warning'}
        >
          {budgetStatus.enforcementMode === 'hard' ? 'Hard Limit' : 'Soft Limit'}
        </Badge>
      </div>

      {/* Budget bar */}
      <div className="mb-6">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-gray-600 dark:text-gray-400">Budget Utilization</span>
          <span className={`text-sm font-semibold ${utilizationColor}`}>
            {formatPercent(utilizationPercent)}
          </span>
        </div>
        <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-4">
          <div
            className={`${getProgressColor()} h-4 rounded-full transition-all duration-300`}
            style={{ width: `${Math.min(utilizationPercent, 100)}%` }}
          />
        </div>
      </div>

      {/* Budget amounts */}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <p className="text-sm text-gray-500 dark:text-gray-400">Budget</p>
          <p className="text-xl font-bold text-gray-900 dark:text-white">
            {formatCurrency(budgetStatus.budgetAmountUsd)}
          </p>
        </div>
        <div>
          <p className="text-sm text-gray-500 dark:text-gray-400">Spent</p>
          <p className={`text-xl font-bold ${utilizationColor}`}>
            {formatCurrency(budgetStatus.currentSpendUsd)}
          </p>
        </div>
        <div>
          <p className="text-sm text-gray-500 dark:text-gray-400">Remaining</p>
          <p className="text-xl font-bold text-gray-900 dark:text-white">
            {formatCurrency(budgetStatus.remainingBudgetUsd)}
          </p>
        </div>
        <div>
          <p className="text-sm text-gray-500 dark:text-gray-400">Period</p>
          <p className="text-sm font-medium text-gray-900 dark:text-white capitalize">
            {budgetStatus.periodType}
          </p>
          <p className="text-xs text-gray-500">
            {new Date(budgetStatus.periodStart).toLocaleDateString()} -{' '}
            {new Date(budgetStatus.periodEnd).toLocaleDateString()}
          </p>
        </div>
      </div>

      {/* Warnings */}
      {budgetStatus.warnings.length > 0 && (
        <div className="mt-4 p-3 bg-yellow-50 dark:bg-yellow-900/20 rounded-lg">
          <p className="text-sm font-medium text-yellow-800 dark:text-yellow-300 mb-1">
            Warnings
          </p>
          <ul className="text-sm text-yellow-700 dark:text-yellow-400 list-disc list-inside">
            {budgetStatus.warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Exceeded alert */}
      {budgetStatus.budgetExceeded && (
        <div className="mt-4 p-3 bg-red-50 dark:bg-red-900/20 rounded-lg">
          <p className="text-sm font-medium text-red-800 dark:text-red-300">
            ⚠️ Budget Exceeded
          </p>
          <p className="text-sm text-red-700 dark:text-red-400">
            {budgetStatus.enforcementMode === 'hard'
              ? 'Requests may be blocked until the next period.'
              : 'Requests will continue but are over budget.'}
          </p>
        </div>
      )}
    </Card>
  );
}
