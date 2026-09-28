import { ENDPOINTS, type EndpointName } from '@superplane-ui/contract';

export const ONBOARDING_ENDPOINTS: EndpointName[] = [
  'adoptWorkspace', 'previewWorkspace', 'getOperation', 'recoverOperation',
  'requestApproval', 'getApproval', 'decideApproval',
  'listLifecycleProposals', 'previewLifecycleProposal', 'continueLifecycleProposal',
];

/** Model an older deployment explicitly; contract tests check the shipped flags. */
export function withoutOnboardingEndpoints() {
  const saved = ONBOARDING_ENDPOINTS.map((name) => [name, ENDPOINTS[name].served] as const);
  for (const name of ONBOARDING_ENDPOINTS) {
    (ENDPOINTS[name] as { served: boolean }).served = false;
  }
  return () => {
    for (const [name, served] of saved) {
      (ENDPOINTS[name] as { served: boolean }).served = served;
    }
  };
}
