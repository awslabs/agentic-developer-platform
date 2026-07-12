/**
 * Empty state for the Agent Run Dashboard.
 *
 * Issue #3633: Shown when all counts === 0 and no active runs,
 * guiding new users to trigger their first agent run.
 *
 * Issue #3772: Replaced the circular "View Activity" CTA (F-A5)
 * with concrete first-step guidance for triggering a run.
 */

import { Link } from 'react-router-dom';
import { Card } from '@/components/ui';

export function EmptyState() {
  return (
    <Card className="text-center max-w-lg mx-auto mt-12">
      <div className="py-8 px-4">
        <div className="text-5xl mb-4">🚀</div>
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
          No runs yet
        </h2>
        <p className="text-gray-500 dark:text-gray-400 mb-4">
          Mention the developer agent on a GitHub issue to trigger your first
          run. Once agents start working, you&apos;ll see live stats here.
        </p>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Need help?{' '}
          <Link
            to="/setup"
            className="text-primary-600 dark:text-primary-400 hover:underline font-medium"
          >
            View setup guide
          </Link>
        </p>
      </div>
    </Card>
  );
}
