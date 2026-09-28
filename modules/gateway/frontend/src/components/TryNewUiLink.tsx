/**
 * TryNewUiLink — Issue #5079 (NUI-01 of EPIC #5078).
 *
 * The single entry point from the current UI into the opt-in /next experience.
 *
 * Renders nothing unless `new_ui` is on. That flag is fail-closed
 * (`ALL_FEATURES_ENABLED.new_ui === false`), so while the first /features read is pending, and
 * after a read exhausts its retry, the current UI shows no invitation to a preview that may not
 * be routable. Turning the flag off is the documented rollback and removes this
 * link with no SPA redeploy.
 *
 * It reads the flags through `useNewUiEnabled`, which revalidates on a bounded
 * interval, rather than through `useFeatures`' session-long cache. Without that,
 * an operator's disable left this invitation on screen in every already-open tab
 * until its user happened to reload — an entry point into an experience that had
 * just been withdrawn. Revalidation is scoped to the preview surfaces so the
 * current UI's other eleven flags keep their existing once-per-session semantics
 * through a separate cache entry.
 *
 * Entry is voluntary: this is a link the user chooses, never a redirect. Nothing
 * in the app navigates to /next on its own, which is what keeps the current UI
 * the default.
 */

import { Link } from 'react-router-dom';
import { useNewUiEnabled } from '@/hooks/useFeatures';

export function TryNewUiLink() {
  const { enabled } = useNewUiEnabled();

  // Also covers the pending and failed cases: `new_ui` is fail-closed, so the
  // invitation appears only once the server has affirmatively enabled it.
  if (!enabled) {
    return null;
  }

  return (
    <Link
      to="/next"
      data-testid="try-new-ui"
      className="px-3 py-2 text-sm font-medium text-primary-700 dark:text-primary-200 border border-primary-300 dark:border-primary-700 rounded-lg hover:bg-primary-50 dark:hover:bg-primary-900 transition-colors"
    >
      Try the new UI
    </Link>
  );
}
