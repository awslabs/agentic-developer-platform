/**
 * NextNotFound — Issue #5079 (NUI-01 of EPIC #5078).
 *
 * Catch-all for an unknown path under /next/*. Needed because the preview
 * currently has exactly one page: without it, a stale link or a hand-typed
 * /next/anything would fall through to the app-level 404, which renders outside
 * this layout and therefore without the "Back to current UI" control.
 *
 * It says the page does not exist *in the preview yet* rather than that it does
 * not exist, because for most of these paths the capability does exist — in the
 * current UI. So it offers both routes back: the preview home and the current UI.
 */

import { Link } from 'react-router-dom';
import { CURRENT_UI_HOME } from '@/layouts/NextLayout';

export default function NextNotFound() {
  return (
    <div className="max-w-2xl" data-testid="next-not-found">
      <h2 className="text-2xl font-bold text-gray-900 dark:text-white">
        Not part of the preview yet
      </h2>
      <p className="mt-2 text-gray-600 dark:text-gray-400">
        This page has not been built in the new UI. The capability may still be
        available in the current UI, which is unchanged.
      </p>
      <div className="mt-6 flex flex-wrap gap-3">
        <Link
          to="/next"
          className="px-4 py-2 text-sm font-medium border border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
        >
          New UI home
        </Link>
        <Link
          to={CURRENT_UI_HOME}
          className="px-4 py-2 text-sm font-medium bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
        >
          Go to the current UI
        </Link>
      </div>
    </div>
  );
}
