/**
 * FeatureGate — Issue #3566.
 *
 * Route-level guard that redirects to "/" when a feature flag is disabled.
 * Renders children when the feature is enabled (or when flags haven't loaded yet — fail-open).
 */

import { Navigate } from 'react-router-dom';
import { useFeatures } from '@/hooks/useFeatures';
import type { FeatureFlags } from '@/services/features';

interface FeatureGateProps {
  feature: keyof FeatureFlags;
  children: React.ReactNode;
}

export function FeatureGate({ feature, children }: FeatureGateProps) {
  const features = useFeatures();

  if (!features[feature]) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
