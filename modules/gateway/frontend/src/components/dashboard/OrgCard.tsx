import { Link } from 'react-router-dom';
import { Card, Badge } from '@/components/ui';
import { formatCurrency, formatNumber } from '@/utils/format';

export interface OrgCardProps {
  id: string;
  name: string;
  requestCount: number;
  tokenCount: number;
  costUsd: number;
  isActive?: boolean;
}

export function OrgCard({ id, name, requestCount, tokenCount, costUsd, isActive = true }: OrgCardProps) {
  return (
    <Link to={`/org/${id}`}>
      <Card className="hover:shadow-lg transition-shadow cursor-pointer">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white">{name}</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400">{id}</p>
          </div>
          <Badge variant={isActive ? 'success' : 'default'}>
            {isActive ? 'Active' : 'Inactive'}
          </Badge>
        </div>

        <div className="grid grid-cols-3 gap-4 text-center">
          <div>
            <p className="text-2xl font-bold text-gray-900 dark:text-white">
              {formatNumber(requestCount)}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400">Requests (24h)</p>
          </div>
          <div>
            <p className="text-2xl font-bold text-gray-900 dark:text-white">
              {formatNumber(tokenCount)}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400">Tokens (24h)</p>
          </div>
          <div>
            <p className="text-2xl font-bold text-gray-900 dark:text-white">
              {formatCurrency(costUsd)}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400">Cost (24h)</p>
          </div>
        </div>
      </Card>
    </Link>
  );
}
