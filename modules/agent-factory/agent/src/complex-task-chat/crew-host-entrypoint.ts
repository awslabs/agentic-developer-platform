import { readFileSync } from 'node:fs';
import { startCrewHostDashboard } from './crew-host-ingress';
import { HOST_AUTHORITY, HOST_READY, currentCrewHostAuthority } from './crew-host-trusted';

// Re-exported for existing importers and tests; new code imports crew-host-trusted directly so
// that bundling it never pulls in this file's entry point (see crew-host-trusted.ts).
export { HOST_AUTHORITY, HOST_READY, currentCrewHostAuthority, readTrustedJson } from './crew-host-trusted';

export async function runCrewHostDashboard(authorityFile = HOST_AUTHORITY, readyFile = HOST_READY) {
  const initial = currentCrewHostAuthority(authorityFile, readyFile);
  const current = () => {
    const next = currentCrewHostAuthority(authorityFile, readyFile);
    if (next.runId !== initial.runId || next.generation !== initial.generation ||
        next.privateIP !== initial.privateIP || next.gatewayFingerprint !== initial.gatewayFingerprint) {
      throw new Error('Crew host authority was replaced');
    }
    return next;
  };
  const client = {
    modelDecision: async () => {
      const scope = current();
      return { runId: scope.runId, tenantId: scope.tenantId,
        userId: scope.userId, generation: scope.generation };
    },
    dashboardVerificationKeys: async () => current().keys,
  };
  return startCrewHostDashboard(client, initial.runId, {
    privateIP: initial.privateIP, generation: initial.generation, gatewayFingerprint: initial.gatewayFingerprint,
    certificate: readFileSync('/run/adp/crew-host/server.pem'),
    key: readFileSync('/run/adp/crew-host/server.key'),
    gatewayCA: readFileSync('/run/adp/crew-host/client-ca.pem'),
    isCurrent: () => { try { current(); return true; } catch { return false; } },
  });
}

if (require.main === module) {
  runCrewHostDashboard().catch(() => { process.exitCode = 1; });
}
