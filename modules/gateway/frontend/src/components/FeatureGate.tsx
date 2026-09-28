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

export function FeatureGate({ feature, children }: FeatureGateProps) {
  const { data, isPending } = useFeaturesQuery();
  const features = data ?? ALL_FEATURES_ENABLED;

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

  if (!features[feature]) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
