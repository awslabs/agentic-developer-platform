/**
 * NewUiGate — Issue #5079 (NUI-01 of EPIC #5078).
 *
 * The route guard for the opt-in `/next` subtree. It behaves like the shared
 * `FeatureGate` — withhold the subtree and send the user to a working current-UI
 * page when the flag is off — but reads the flags through
 * `useRevalidatingFeaturesQuery` instead of the session-long cache.
 *
 * Why a separate component rather than `<FeatureGate feature="new_ui">`:
 *
 * `FeatureGate` is used by eleven current-UI routes and reads flags once per
 * session (`staleTime: Infinity`). That is correct for them, and changing it would
 * alter the timing of every gated screen in the app, including the two fail-closed
 * flags with their own reasoning (`budget_spend`, `agent_control`). But it left
 * this story's documented rollback incomplete: an operator who disabled the
 * preview was obeyed only by tabs that happened to reload, so a session that was
 * already inside `/next` stayed there indefinitely. Bounded revalidation belongs
 * to the preview alone, so it is opted into here.
 *
 * The pending case deliberately does NOT redirect. `new_ui` is fail-closed, so on
 * a first load the flags read `false` before the response arrives; redirecting
 * then would discard the requested URL and break deep links and bookmarks into the
 * preview — the same bug `FeatureGate` documents for `/flows/:id`. Holding renders
 * a spinner and keeps the URL until the real value is known.
 *
 * A completed refetch error also closes the preview, even if react-query retains
 * a previous true value. The pending screen offers an eager current-UI escape
 * while preserving the requested URL for a successful first read.
 */

import { Navigate } from 'react-router-dom';
import { useNewUiEnabled } from '@/hooks/useFeatures';
import { Spinner } from '@/components/ui';

interface NewUiGateProps {
  children: React.ReactNode;
}

export function NewUiGate({ children }: NewUiGateProps) {
  const { enabled, isPending } = useNewUiEnabled();

  // Withhold without discarding the URL — see the file comment.
  if (isPending && !enabled) {
    return (
      <div className="flex items-center gap-2 p-6" data-testid="next-gate-loading">
        <Spinner size="sm" />
        <span>Loading feature…</span>
        <a href="/" className="text-primary-700 underline dark:text-primary-200">
          Back to current UI
        </a>
      </div>
    );
  }

  // The documented rollback: "/" is a working current-UI page.
  if (!enabled) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
