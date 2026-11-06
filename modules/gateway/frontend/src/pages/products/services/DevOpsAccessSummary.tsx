import { useEffect, useState } from 'react';
import type { DevOpsBinding, DevOpsConfiguration } from '@/services/l5Services';
import { consentScopeMatches, listOwnerGrants } from '@/services/l5OwnerConsent';
import { devOpsDate } from './DevOpsPresentation';

/** Permission summary only; the run still requires its current readiness check. */
export default function DevOpsAccessSummary({ configuration, binding, refreshKey }: {
  configuration: DevOpsConfiguration; binding?: DevOpsBinding | null; refreshKey: number;
}) {
  const [status, setStatus] = useState('Checking owner permission…');
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setStatus('Checking owner permission…');
    void listOwnerGrants(configuration.scope.product_id, configuration.scope.project_id, 'devops').then(page => {
      if (cancelled) return;
      if (page.items.some(grant => !consentScopeMatches(grant, configuration))) throw new Error('Scope changed');
      const grant = page.items.find(item => {
        const scope = item.execution_scope;
        return binding && scope && consentScopeMatches(scope, configuration) && !scope.purpose && !scope.control_action
          && scope.configuration_id === configuration.configuration_id && scope.configuration_revision === configuration.revision
          && scope.configuration_hash === configuration.hash && scope.created_by === configuration.scope.owner_id
          && scope.binding_id === binding.binding_id && scope.binding_generation === binding.generation
          && item.service_id === 'l5-devops-worker' && scope.service_id === 'l5-devops-worker'
          && item.active && !item.revoked_at && Date.parse(item.expires_at) > Date.now()
          && item.capabilities.includes('investigation') && scope.capabilities.includes('investigation');
      });
      if (grant) {
        setStatus(`Owner permission expires ${devOpsDate(grant.expires_at)}`);
        timer = setTimeout(() => setStatus('Owner permission expired — review access'), Math.max(0, Date.parse(grant.expires_at) - Date.now()));
      } else setStatus(configuration.settings.enabled ? 'Owner permission needs review' : 'Complete workload setup before granting run access');
    }).catch(() => { if (!cancelled) setStatus('Owner permission could not be checked — review access'); });
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [configuration, binding, refreshKey]);
  return <p className="devops-access-summary" role="status">{status}<small>Run readiness is checked when you start an investigation.</small></p>;
}
