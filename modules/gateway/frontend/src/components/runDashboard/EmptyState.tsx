/**
 * Empty state for the Agent Run Dashboard.
 *
 * Issue #3633: Shown when all counts === 0 and no active runs,
 * guiding new users to trigger their first agent run.
 */

import { useNavigate } from 'react-router-dom';
import { Card } from '@/components/ui';

export function EmptyState() {
  const navigate = useNavigate();

  return (
    <Card className="text-center max-w-lg mx-auto mt-12">
      <div className="py-8 px-4">
        <div className="text-5xl mb-4">🚀</div>
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
          No agent runs yet
        </h2>
        <p className="text-gray-500 dark:text-gray-400 mb-6">
          Get started by triggering your first agent run. Once agents start
          working, you&apos;ll see live stats and recent activity here.
        </p>
        <button
          onClick={() => navigate('/activity')}
          className="inline-flex items-center px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors font-medium"
        >
          View Activity
        </button>
      </div>
    </Card>
  );
}
