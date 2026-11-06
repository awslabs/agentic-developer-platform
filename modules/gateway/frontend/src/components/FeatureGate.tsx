/**
 * FeatureGate — Issue #3566.
 *
 * Route-level guard that redirects to "/" when a feature flag is disabled.
 * Uses each feature's default policy while loading; pending fail-closed flags
 * wait without discarding the requested URL.
 */

import { Navigate } from 'react-router-dom';
import { useFeaturesQuery } from '@/hooks/useFeatures';
import { ALL_FEATURES_ENABLED } from '@/services/features';
import { Spinner } from '@/components/ui';
import type { FeatureFlags } from '@/services/features';

interface FeatureGateProps {
  feature: keyof FeatureFlags;
  children: React.ReactNode;
}

/**
 * Features whose admission read can fail for an authorization reason — #8246.
 *
 * A failed /features read leaves the fail-closed default in place, so the
 * screen stays withheld either way; that part is deliberate and unchanged.
 * What was wrong was *where* the refusal was reported. An actor whose tenant
 * membership had been revoked was redirected to "/", where the organization
 * selector said "Could not load organizations." and offered a reload that
 * could never succeed — a transient-sounding story about a permanent denial,
 * on a screen the actor never asked for.
 *
 * For the features listed here the gate says so in place instead. The wording
 * covers both reasons the flag could be missing (the deployment could not be
 * read, or this actor may not read it) without asserting which one applies,
 * and without rendering any gated data or edit control.
 */
const UNAVAILABLE_NOTICE: Partial<Record<keyof FeatureFlags, string>> = {
  product_profile: 'Product unavailable or you do not have access.',
};

export function FeatureGate({ feature, children }: FeatureGateProps) {
  const { data, isPending, isError } = useFeaturesQuery();
  const features = data ?? ALL_FEATURES_ENABLED;
  const notice = UNAVAILABLE_NOTICE[feature];

  // A pending fail-closed flag must withhold the screen without discarding its
  // URL. Redirecting here made every fresh /flows/:id visit land on Dashboard.
  if (isPending && !features[feature]) {
    return (
      <div className="flex items-center gap-2 p-6">
        <Spinner size="sm" />
        <span>Loading feature…</span>
      </div>
    );
  }

  // The read completed without a flag payload: stay fail-closed, but report the
  // refusal here rather than on a screen that would misdescribe it. A flag the
  // server did answer and set to false still redirects, below, so turning a
  // feature off keeps its existing behaviour.
  if (isError && !data && notice) {
    return (
      <div className="p-6">
        <div role="alert" className="text-sm text-red-600 dark:text-red-400">
          {notice}
        </div>
      </div>
    );
  }

  if (!features[feature]) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
