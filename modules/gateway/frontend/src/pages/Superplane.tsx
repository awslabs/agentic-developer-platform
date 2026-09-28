/**
 * Superplane domain app landing page — Issue #5730 (EPIC #4910).
 *
 * Was a placeholder stating that the interface had not shipped (#5037, which
 * created the gated route and the module skeleton). It now renders the workspace
 * and provider onboarding surface, which is the first thing an operator needs from
 * a freshly installed control plane with zero workspaces.
 *
 * The route remains behind the default-off `superplane` feature gate in App.tsx, so
 * enabling it is still a deliberate act in an environment whose backing
 * infrastructure may not be fully deployed. The view is built to say so honestly
 * when that is the case rather than to fail — see OnboardingView.
 */

import { OnboardingView } from '@superplane-ui/OnboardingView';

export default function Superplane() {
  return <OnboardingView />;
}
